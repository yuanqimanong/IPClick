"""为集群里的子节点生成一份可直接落地的部署材料。

要解决的是"主控上点几下加好了节点，然后呢"。0.4 的答案是"你自己去每台机器上装
一遍、把配置抄对"——而抄配置正是集群最容易出错的一步：端口写错、``self_id``
忘了改、共享密钥少复制一个字符，症状分别是连不上、不分活、``UNAUTHENTICATED``，
三种都不好查。

所以这里由主控**生成**每台子节点该有的东西：

* ``ipclick.toml`` —— 节点列表与主控完全一致（对等入口的前提），只有
  ``self_id`` 和监听端口是这一台自己的；
* ``.env`` —— 只有机密：gRPC 令牌与集群共享密钥，值取自主控**当前生效**的那份，
  所以复制过去必然对得上；
* **启动命令** —— pip 与 uv 两种写法，因为这两种环境的建法完全不同。

**只生成，不推送。** 加一个"把配置写到远端"的 RPC 就等于：拿下主控 = 能改所有
机器上的配置文件，包括 SSRF 拦截开关。生成的东西由人复制过去，攻击面一点没变。

机密的处理：``.env`` 里确实有真实的令牌值——那是它的用途，不给值就没法部署。
但它只在**已登录**的管理端里出现，且和页面上其他地方的规矩一致：不写日志、
不落盘、不进任何缓存。
"""

from __future__ import annotations

from dataclasses import dataclass
import io
from typing import Any, final
import zipfile

from ipclick.ports import DEFAULT_GRPC_PORT, DEFAULT_WEB_PORT


_BANNER = "# 由 IPClick 主控的「配置 → 集群设置」生成。改完记得两边保持一致。"


@final
@dataclass(frozen=True)
class NodePlan:
    """一台子节点的部署材料。"""

    node_id: str
    address: str
    host: str
    port: int
    web_port: int
    toml: str
    env: str
    commands: tuple[tuple[str, str], ...]

    @property
    def filename_prefix(self) -> str:
        """打包时用的文件名前缀。节点 id 可能带冒号（``host:port`` 形式的自动 id），
        那在 Windows 上不是合法文件名。"""
        safe = "".join(c if c.isalnum() or c in "-_." else "-" for c in self.node_id)
        return safe or f"node-{self.port}"

    @property
    def toml_name(self) -> str:
        """配置文件叫什么。

        按端口命名（``ipclick-9002.toml``）而不是统一的 ``ipclick.toml``：这样几台
        节点的配置可以**并排放在同一个目录里**而不打架，``run --port 9002`` 会自己
        找到对应那份。逐台建子目录当然也行，但那是额外的纪律，而忘记建目录的后果是
        第二台默默覆盖第一台的配置。

        ``.env`` 反过来——全集群同一份（同一个令牌、同一个共享密钥），共用一个正好。
        """
        return f"ipclick-{self.port}.toml"

    def snapshot(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "address": self.address,
            "port": self.port,
            "web_port": self.web_port,
            "toml": self.toml,
            "toml_name": self.toml_name,
            "env": self.env,
            "commands": [{"title": title, "command": command} for title, command in self.commands],
        }


def _split_address(address: str) -> tuple[str, int]:
    host, _, port_text = address.rpartition(":")
    try:
        port = int(port_text)
    except ValueError:
        return address, DEFAULT_GRPC_PORT
    return host.strip("[]") or "0.0.0.0", port


def node_toml(
    *,
    node_id: str,
    port: int,
    web_port: int,
    nodes: list[dict[str, Any]],
    forward: bool,
    max_workers: int = 100,
) -> str:
    """子节点的 ``ipclick.toml``。

    节点列表和主控**一模一样**——这是"任意节点都能当入口"的前提。区别只有
    ``self_id``（这台是谁）和监听端口。

    刻意不生成完整的那份带几百行注释的模板：子节点要的是"能起来、能被主控调到"，
    其余项留空就走内置默认值。要细调再去 ``ipclick -e > ipclick.toml`` 拿完整版。
    """
    entries = "".join(f'    {{ id = "{n["id"]}", address = "{n["address"]}" }},\n' for n in nodes if n.get("address"))
    return f"""{_BANNER}
# 这一台是 {node_id}。

[SERVER]
# 监听所有网卡：主控要从别的机器连过来。
host = "[::]"
port = {port}
max_workers = {max_workers}

[WEB]
# 每台子节点也留一个管理端，出问题时能单独打开这一台看它自己的请求流。
enabled = true
port = {web_port}
host = "127.0.0.1"

[CLUSTER]
# 和主控保持一致：谁被访问谁就是入口，所以每台都要有完整的节点列表。
forward = "{"on" if forward else "off"}"
# 这一行是这台机器**唯一**和别人不同的地方。
self_id = "{node_id}"
# 允许主控远程装 / 卸可选组件，省掉逐台 SSH 上去敲命令。
# 生成的部署包里默认打开——你既然是从主控生成的这份配置，就已经信任它了。
# 不想要的话把它改成 false：那之后主控的组件页对这台会返回"未开启"。
allow_remote_install = true
nodes = [
{entries}]

[SECURITY]
# 子节点通常只对内网开放。若这台机器能被不受信任的网络连到，改成 true。
block_private_networks = false
"""


def node_env(*, auth_token: str, cluster_secret: str) -> str:
    """子节点的 ``.env``。只放机密。

    两个值都取自主控当前生效的那份，所以复制过去必然对得上——集群最常见的故障
    "两边密钥差一个字符"在这里就没有发生的机会了。
    """
    lines = [
        _BANNER,
        "# 权限应为 600：chmod 600 .env",
        "",
        "# 调用方 -> 服务端的鉴权。整个集群用同一个，听主控的。",
        f"IPCLICK_AUTH_TOKEN={auth_token}",
        "",
        "# 节点 -> 节点的内部鉴权。由它派生出每台各不相同的令牌，",
        "# 所以所有机器必须是**同一个值**（各自生成一个就全对不上了）。",
        f"IPCLICK_CLUSTER_SECRET={cluster_secret}",
        "",
    ]
    return "\n".join(lines)


def node_commands(*, port: int, version: str, extras: str = "") -> tuple[tuple[str, str], ...]:
    """这台子节点的部署命令。

    三条踩过的坑，都写进这里了：

    1. **版本要钉死。** ``uv pip install ipclick`` 会去 PyPI 抓"最新的"，而你要的
       那个版本可能压根没发布——实测装到的是旧版，起来之后行为和主控对不上，
       症状是各种"这个参数怎么不认"。所以一律 ``ipclick=={version}``。
    2. **不要 ``uv run``。** 它会重新解析一遍依赖，可能装到别的地方去；而且这里
       目录里没有 ``pyproject.toml``，``uv run`` 的语义并不是"跑我刚装的那个"。
       直接用 ``.venv/bin/ipclick``，指哪打哪。
    3. **本地 wheel 那条不能省。** 自己 build 的版本不在任何索引上，前两条都装不到，
       只能从主控把 wheel 拷过去。

    ``--port`` 让它去读 ``ipclick-<端口>.toml``（见 loader.candidate_names），
    所以多个节点的配置可以并排放在同一个目录里。
    """
    suffix = f"[{extras}]" if extras else ""
    pinned = f'"ipclick{suffix}=={version}"'
    wheel = f"ipclick-{version}-py3-none-any.whl{suffix}"
    return (
        (
            "pip",
            f"python3 -m venv .venv && .venv/bin/pip install {pinned} && .venv/bin/ipclick run --port {port}",
        ),
        (
            "uv",
            f"uv venv && uv pip install --python .venv/bin/python {pinned} && .venv/bin/ipclick run --port {port}",
        ),
        (
            "本地 wheel（版本没发布到 PyPI 时用这条）",
            f"# 先从主控把 wheel 拷过来，比如：\n"
            f"#   scp 主控:/路径/dist/ipclick-{version}-py3-none-any.whl .\n"
            f'uv venv && uv pip install --python .venv/bin/python "./{wheel}" '
            f"&& .venv/bin/ipclick run --port {port}",
        ),
    )


WEB_PORT_OFFSET = 10000


def _web_port_for(port: int, nodes: list[dict[str, Any]]) -> int:
    """给一台子节点挑 Web 管理端端口。

    默认端口的部署保持 9528/9527 那一对——那是文档和横幅里反复出现的组合，
    换掉只会多一个要记的数。其余情况加一万，然后**对着真实的节点列表核对**：
    算出来的值不能等于任何一台的 gRPC 端口。

    0.5.0 之前这里是 ``port - 1``，配上连续分配的 19001/19002/19003，生成出的
    Web 端口是 19000/19001/19002 —— node-2 的 Web 端口正好等于 node-1 的 gRPC
    端口。同机部署直接起不来（服务端没开 SO_REUSEPORT），跨机部署则是凭空又
    造一批"这个端口到底是谁的"。
    """
    if port == DEFAULT_GRPC_PORT:
        return DEFAULT_WEB_PORT
    taken = {p for _, p in (_split_address(str(n.get("address") or "")) for n in nodes)}
    candidate = port + WEB_PORT_OFFSET
    if candidate > 65535:
        candidate = max(1, port - WEB_PORT_OFFSET)
    while candidate in taken and candidate < 65535:
        candidate += 1
    return candidate


def build_plan(
    node: dict[str, Any],
    *,
    nodes: list[dict[str, Any]],
    forward: bool,
    auth_token: str,
    cluster_secret: str,
    version: str = "",
    extras: str = "",
    max_workers: int = 100,
) -> NodePlan:
    """一台子节点的完整部署材料。"""
    if not version:
        from ipclick import __version__

        version = __version__
    address = str(node.get("address") or "")
    host, port = _split_address(address)
    node_id = str(node.get("id") or address)
    web_port = _web_port_for(port, nodes)
    return NodePlan(
        node_id=node_id,
        address=address,
        host=host,
        port=port,
        web_port=web_port,
        toml=node_toml(
            node_id=node_id,
            port=port,
            web_port=web_port,
            nodes=nodes,
            forward=forward,
            max_workers=max_workers,
        ),
        env=node_env(auth_token=auth_token, cluster_secret=cluster_secret),
        commands=node_commands(port=port, version=version, extras=extras),
    )


def bundle(plans: list[NodePlan]) -> bytes:
    """把所有节点的材料打成一个 zip。

    一台一台点"下载"在三五台时还行，再多就是纯苦力。zip 里按节点分目录，
    外加一个 ``README.txt`` 说明每台该怎么用——解压出来的东西必须自解释，
    否则过两周就没人记得哪个目录对应哪台机器。
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        readme = [
            "IPClick 集群部署包",
            "=" * 40,
            "",
            "每个目录对应一台子节点。在那台机器上：",
            "",
            "  1. 把该节点目录里的 ipclick-<端口>.toml 与 .env 放到它的工作目录",
            "  2. chmod 600 .env       （里面是令牌）",
            "  3. 按 启动命令.txt 里的任意一种起服务",
            "",
            "配置按端口命名，所以几台节点的配置可以并排放在同一个目录里而不打架——",
            "`run --port 9002` 会自己去找 ipclick-9002.toml。.env 反过来是全集群共用的。",
            "",
            "节点列表在每台机器上都是完整且相同的——谁被访问谁就是入口。",
            "唯一逐台不同的是 [CLUSTER].self_id 和监听端口，已经填好了。",
            "",
            "包含的节点：",
        ]
        for plan in plans:
            readme.append(f"  {plan.filename_prefix:<24} {plan.address}")
            root = plan.filename_prefix
            archive.writestr(f"{root}/{plan.toml_name}", plan.toml)
            archive.writestr(f"{root}/.env", plan.env)
            archive.writestr(
                f"{root}/启动命令.txt",
                "\n\n".join(f"# {title}\n{command}" for title, command in plan.commands) + "\n",
            )
        archive.writestr("README.txt", "\n".join(readme) + "\n")
    return buffer.getvalue()


__all__ = [
    "NodePlan",
    "build_plan",
    "bundle",
    "node_commands",
    "node_env",
    "node_toml",
]
