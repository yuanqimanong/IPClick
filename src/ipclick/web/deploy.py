"""生成集群子节点配置、启动命令和可下载部署包。"""

from __future__ import annotations

from dataclasses import dataclass
import io
import json
import re
from typing import Any, final
import zipfile

from ipclick.ports import DEFAULT_GRPC_PORT, DEFAULT_WEB_PORT


_BANNER = "# 由 IPClick 主控的「配置 → 集群设置」生成。改完记得两边保持一致。"

_DOTENV_PLAIN = re.compile(r"[A-Za-z0-9._~+/=:-]*\Z")


def _toml_string(value: Any) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def _dotenv_value(value: str) -> str:
    # dotenv 的未加引号值会把 ``#``、空白和换行解释成语法；必要时改用兼容的双引号形式。
    return value if _DOTENV_PLAIN.fullmatch(value) else json.dumps(value, ensure_ascii=False)


def _comment_text(value: Any) -> str:
    return " ".join(str(value).splitlines())


@final
@dataclass(frozen=True)
class NodePlan:
    """单个集群节点的部署产物集合。"""

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
        """返回可安全用作 ZIP 目录名的节点标识。"""
        safe = "".join(c if c.isalnum() or c in "-_." else "-" for c in self.node_id).strip(".")
        # ``.``/``..`` 作为 ZIP 路径段会在解压时指向当前目录或父目录。
        return safe or f"node-{self.port}"

    @property
    def toml_name(self) -> str:
        """返回包含监听端口的配置文件名。"""
        return f"ipclick-{self.port}.toml"

    def snapshot(self) -> dict[str, Any]:
        """返回供模板与 JSON 接口消费的可序列化视图。"""
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
    """生成一台子节点的 TOML 配置文本。"""
    # 节点字段来自可编辑配置，必须按 TOML 字符串转义，不能直接拼进生成文件。
    entries = "".join(
        f"    {{ id = {_toml_string(n['id'])}, address = {_toml_string(n['address'])} }},\n"
        for n in nodes
        if n.get("address")
    )
    return f"""{_BANNER}
# 这一台是 {_comment_text(node_id)}。

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
self_id = {_toml_string(node_id)}
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
    """生成子节点的鉴权环境变量文件内容。"""
    lines = [
        _BANNER,
        "# 权限应为 600：chmod 600 .env",
        "",
        "# 调用方 -> 服务端的鉴权。整个集群用同一个，听主控的。",
        f"IPCLICK_AUTH_TOKEN={_dotenv_value(auth_token)}",
        "",
        "# 节点 -> 节点的内部鉴权。由它派生出每台各不相同的令牌，",
        "# 所以所有机器必须是**同一个值**（各自生成一个就全对不上了）。",
        f"IPCLICK_CLUSTER_SECRET={_dotenv_value(cluster_secret)}",
        "",
    ]
    return "\n".join(lines)


def node_commands(*, port: int, version: str, extras: str = "") -> tuple[tuple[str, str], ...]:
    """生成 pip、uv 与本地 wheel 三组启动命令。"""
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
    """根据节点条目与集群公共配置构建完整部署计划。"""
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
    """将多个节点部署计划打包为内存中的 ZIP 字节串。"""
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
        used_roots: set[str] = set()
        for index, plan in enumerate(plans, start=1):
            root = plan.filename_prefix
            if root in used_roots:
                root = f"{root}-{plan.port}"
            if root in used_roots:
                root = f"{root}-{index}"
            used_roots.add(root)
            readme.append(f"  {root:<24} {plan.address}")
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
