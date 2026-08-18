"""Web 端可编辑的配置项白名单。

这个文件回答的是"网页能改什么"。名单是白名单而不是黑名单——漏写一项的后果是
"改不了"（有人来问），而黑名单漏写一项的后果是"能改本不该改的"（没人会来问）。

**刻意不可编辑**的几类，页面上只展示、要改就去改文件：

* ``[SECURITY]`` 全部。令牌、TLS 证书路径、SSRF 三个开关（``block_private_networks``
  / ``block_metadata_endpoints`` / ``allowed_schemes``）。这个服务能代任意 URL 发
  请求，一个能从网页关掉内网拦截的管理端，等于给自己装了个 SSRF 跳板。
* ``[WEB]`` 的用户名密码。改自己的登录凭据必须经过文件（那还要求有机器的 shell）。
* ``[CLUSTER].secret`` 与节点的 ``token``。机密不进 toml，正规位置是 ``.env``。
* ``[BROWSER].allow_scripts``。它等于允许调用方在服务端的浏览器里跑任意 JS，
  而页面内的 JS 会自己发请求，``[SECURITY]`` 那套 URL 策略对它完全不起作用。
* ``[BROWSER].executable_path``。它是喂给浏览器启动器的可执行文件路径——能从网页
  写它，等于能让服务端进程去执行本机任意二进制。
* ``[CLUSTER].allow_remote_install``。打开它之后，能调本节点 gRPC 的人就可以在这台
  机器上跑 pip。这是从"能代发 HTTP 请求"到"能改本机 Python 环境"的实质提权，
  必须由这台机器的主人在文件里点头。

后三项和 ``[SECURITY]`` 那几个的共同点是：**它们都不是"配置"，是"授权"**。
授权的开关不该和超时、线程数摆在同一个表单里，让人顺手划过去。

节点列表（``[CLUSTER].nodes``）是**可编辑**的：加减机器是这套集群的日常操作，
而且节点地址本身不是机密。但节点的 ``token`` 字段不接受从网页写入。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, final

from ipclick.adapters.browser_engines import ENGINE_NAMES
from ipclick.adapters.browser_settings import BROWSER_KINDS, WAIT_UNTIL_CHOICES
from ipclick.exceptions import ValidationError
from ipclick.ports import DEFAULT_GRPC_PORT, DEFAULT_WEB_PORT


FieldKind = Literal["int", "float", "bool", "str", "choice"]


@final
@dataclass(frozen=True)
class Field:
    """一个可编辑项。"""

    section: str
    key: str
    label: str
    kind: FieldKind
    #: 改完是否要重启才生效。要如实标出来——改完没反应会让人以为保存失败。
    restart: bool = True
    minimum: float | None = None
    maximum: float | None = None
    choices: tuple[str, ...] = ()
    hint: str = ""
    #: 配置文件里没写这一项时展示什么。必须填**代码里真正的默认值**——
    #: 展示成空白的话，用户一保存就把空值写进配置，等于悄悄改了行为。
    default: Any = None

    @property
    def name(self) -> str:
        """表单字段名。"""
        return f"{self.section}.{self.key}"

    def parse(self, raw: str) -> Any:
        """把表单里的字符串转成配置值。

        Raises:
            ValidationError: 取值非法。宁可拒绝也不要写一个会让服务起不来的值。
        """
        text = (raw or "").strip()
        if self.kind == "bool":
            return text.lower() in ("1", "true", "on", "yes")
        if self.kind == "choice":
            if text not in self.choices:
                raise ValidationError(f"{self.label}：{text!r} 不在可选值 {list(self.choices)} 内")
            return text
        if self.kind == "str":
            return text
        try:
            value = int(text) if self.kind == "int" else float(text)
        except ValueError as e:
            raise ValidationError(f"{self.label}：{text!r} 不是{'整数' if self.kind == 'int' else '数字'}") from e
        if self.minimum is not None and value < self.minimum:
            raise ValidationError(f"{self.label}：不能小于 {self.minimum}")
        if self.maximum is not None and value > self.maximum:
            raise ValidationError(f"{self.label}：不能大于 {self.maximum}")
        return value


#: 可编辑项，按展示分组。
#:
#: 组名前缀决定它落在配置页的哪个分页：``集群`` 开头的进「集群设置」，
#: 其余进「基础设置」。见 :func:`groups_for`。
GROUPS: tuple[tuple[str, tuple[Field, ...]], ...] = (
    (
        "服务端",
        (
            Field(
                "SERVER",
                "port",
                "gRPC 端口",
                "int",
                minimum=1,
                maximum=65535,
                default=DEFAULT_GRPC_PORT,
                hint="客户端和其他节点连的就是它",
            ),
            Field(
                "WEB",
                "port",
                "Web 管理端端口",
                "int",
                minimum=1,
                maximum=65535,
                default=DEFAULT_WEB_PORT,
                hint="就是你现在打开的这个界面",
            ),
            Field(
                "SERVER",
                "host",
                "gRPC 监听地址",
                "str",
                default="[::]",
                hint="默认 [::] 监听全部网卡。填 127.0.0.1 就只有本机连得上——"
                "别的机器连不上、而 Web 管理端却开着，多半就是这一项（--web-lan 只管 Web，不管 gRPC）",
            ),
            Field(
                "WEB",
                "host",
                "Web 管理端监听地址",
                "str",
                default="127.0.0.1",
                hint="默认 127.0.0.1 只有本机能开。填 0.0.0.0 让局域网可访问——"
                "那是明文 HTTP，密码会在网络上裸奔，改之前先确认 [SECURITY].auth_token 已配",
            ),
            Field(
                "SERVER",
                "max_workers",
                "worker 线程数",
                "int",
                minimum=1,
                maximum=10000,
                hint="每个请求占一个线程做阻塞 IO；同时也是批量的并发上限",
            ),
            Field(
                "GENERAL", "debug", "调试模式", "bool", restart=False, hint="强制 DEBUG 级别日志，覆盖下面的日志级别"
            ),
        ),
    ),
    (
        "日志",
        (
            Field("LOG", "level", "日志级别", "choice", restart=False, choices=("debug", "info", "warning", "error")),
            Field("LOG", "output", "输出位置", "str", hint="stdout 或文件路径"),
            Field(
                "LOG",
                "format",
                "日志格式",
                "str",
                hint="留空用内置带颜色的格式。写错了不会让服务起不来，会告警后回落内置格式",
            ),
            Field(
                "LOG.rotation",
                "max_size",
                "单个日志文件上限（MB）",
                "int",
                minimum=1,
                maximum=10000,
                default=100,
                hint="只在「输出位置」是文件路径时有意义",
            ),
            Field(
                "LOG.rotation",
                "max_backups",
                "保留几个历史日志",
                "int",
                minimum=0,
                maximum=1000,
                default=5,
            ),
        ),
    ),
    (
        "下载行为",
        (
            Field("DOWNLOADER", "download_timeout", "单次请求超时（秒）", "float", minimum=0.1, maximum=3600),
            Field("DOWNLOADER", "connect_timeout", "连接超时（秒）", "float", minimum=0.1, maximum=600),
            Field(
                "DOWNLOADER",
                "trust_env",
                "跟随系统代理环境变量",
                "bool",
                hint="开了之后出站请求会读本机的 HTTP_PROXY / HTTPS_PROXY / ALL_PROXY。"
                "默认关，免得「在我机器上好好的」变成「在服务器上悄悄走了别的出口」",
            ),
            Field(
                "DOWNLOADER",
                "chunk_size",
                "流式分片大小（字节）",
                "int",
                minimum=1024,
                maximum=8 * 1024 * 1024,
                default=64 * 1024,
            ),
        ),
    ),
    (
        "重试",
        (
            Field("DOWNLOADER.retry", "max_attempts", "最大重试次数", "int", minimum=0, maximum=20),
            Field("DOWNLOADER.retry", "initial_backoff", "退避基数（秒）", "float", minimum=0, maximum=600),
            Field("DOWNLOADER.retry", "backoff_exponent", "退避指数", "float", minimum=1, maximum=10),
            Field("DOWNLOADER.retry", "max_backoff", "单次等待上限（秒）", "float", minimum=0.1, maximum=3600),
        ),
    ),
    (
        # 和「按 host 限流」分开：这一组是**总量**（整个进程对外开多少连接），
        # 那一组是**单个 host** 的配额。混在一起时人会以为 max_connections 是
        # "每个站点最多 100 条"。
        "连接池",
        (
            Field(
                "DOWNLOADER.concurrency",
                "max_connections",
                "连接池总上限",
                "int",
                minimum=1,
                maximum=10000,
                default=100,
                hint="压测时同时挂多少条连接就靠它。⚠️ 只有 niquests 适配器读这一项，"
                "默认的 curl_cffi 不读——改了对 curl_cffi 没有任何影响",
            ),
            Field(
                "DOWNLOADER.concurrency",
                "max_keepalive_connections",
                "长连接保活上限",
                "int",
                minimum=1,
                maximum=10000,
                default=20,
                hint="其中保持复用、不关闭的那部分。同样只对 niquests 生效",
            ),
        ),
    ),
    (
        "按 host 限流",
        (
            Field(
                "DOWNLOADER.concurrency",
                "per_host_max_concurrent",
                "并发上限",
                "int",
                minimum=0,
                maximum=10000,
                hint="0 = 不限制",
            ),
            Field(
                "DOWNLOADER.concurrency",
                "per_host_wait_timeout",
                "等待额度超时（秒）",
                "float",
                minimum=0,
                maximum=3600,
            ),
            Field(
                "DOWNLOADER.concurrency",
                "per_host_idle_ttl",
                "闲置额度回收（秒）",
                "float",
                minimum=0,
                maximum=86400,
                default=300,
                hint="某个 host 多久没请求就把它的额度条目回收掉。爬很多不同域名时靠它防内存涨",
            ),
            Field(
                "DOWNLOADER.concurrency",
                "max_tracked_hosts",
                "最多跟踪多少个 host",
                "int",
                minimum=16,
                maximum=1000000,
                default=10000,
                hint="到顶之后日志会直接点名让你调大这一项——所以它必须能在这里改",
            ),
            Field(
                "DOWNLOADER.rate_limit",
                "per_host_qps",
                "QPS 上限",
                "float",
                minimum=0,
                maximum=100000,
                hint="0 = 不限制；可以是小数，0.5 表示两秒一个",
            ),
            Field(
                "DOWNLOADER.rate_limit",
                "per_host_burst",
                "突发额度",
                "int",
                minimum=0,
                maximum=100000,
                hint="0 = 取 QPS 上限向上取整",
            ),
        ),
    ),
    (
        "浏览器渲染",
        (
            Field("BROWSER", "enabled", "启用浏览器适配器", "bool"),
            Field("BROWSER", "engine", "渲染引擎", "choice", choices=("auto", *sorted(ENGINE_NAMES))),
            Field("BROWSER", "headless", "无头模式", "bool"),
            Field("BROWSER", "max_pages", "并发页面上限", "int", minimum=1, maximum=200),
            # 这一项在配置里是 [BROWSER.timeout].page_load，不是 [BROWSER].page_timeout
            Field("BROWSER.timeout", "page_load", "页面加载超时（秒）", "float", minimum=1, maximum=600, default=30),
            Field("BROWSER.timeout", "script_exec", "脚本执行超时（秒）", "float", minimum=1, maximum=600, default=60),
            # 配置键叫 browser，BrowserSettings 里的属性叫 kind。这里必须写配置键——
            # 按属性名写会生成一个 [BROWSER].kind，谁都不读，而页面上看着像生效了。
            Field(
                "BROWSER",
                "browser",
                "浏览器内核",
                "choice",
                choices=tuple(sorted(BROWSER_KINDS)),
                default="chromium",
            ),
            Field(
                "BROWSER",
                "wait_until",
                "页面等到什么时候算加载完",
                "choice",
                choices=tuple(sorted(WAIT_UNTIL_CHOICES)),
                default="load",
                hint="浏览器渲染里对耗时影响最大的一项，load 和 networkidle 常常差几倍",
            ),
            Field("BROWSER.viewport", "width", "视口宽度", "int", minimum=100, maximum=10000, default=1920),
            Field("BROWSER.viewport", "height", "视口高度", "int", minimum=100, maximum=10000, default=1080),
            Field(
                "BROWSER",
                "user_agent",
                "固定 User-Agent",
                "str",
                hint="留空则每次请求随机生成一个。调反爬时最常动的一项",
            ),
            Field("BROWSER", "locale", "语言环境", "str", hint="留空由引擎自己生成，例如 zh-CN"),
            Field(
                "BROWSER",
                "geoip",
                "指纹跟随代理出口地",
                "bool",
                hint="让时区、语言、经纬度和代理出口 IP 对上。只对下面那个「浏览器代理网关」生效，"
                "按请求单独指定的代理来不及影响指纹",
            ),
            Field(
                "BROWSER",
                "no_sandbox",
                "关闭浏览器沙箱",
                "bool",
                hint="⚠️ 只在容器里 chromium 起不来时才开（缺 user namespace）。"
                "关掉沙箱后，目标页面的代码更容易影响服务端进程——能不开就别开",
            ),
            Field(
                "BROWSER.proxy",
                "gateway",
                "浏览器代理网关",
                "str",
                hint="浏览器渲染走的代理。和上面的「指纹跟随代理出口地」配套使用",
            ),
        ),
    ),
    (
        # [PROXY] 是给 HTTP 适配器用的"配置级代理"，也就是请求里写 proxy=true
        # 和「试一试」里选「用配置里的 [PROXY]」时走的那一份。
        # 和 [BROWSER.proxy] 不是一回事，后者只管浏览器渲染。
        "代理",
        (
            Field("PROXY", "host", "代理主机", "str", hint="留空表示不配置级代理"),
            Field("PROXY", "port", "代理端口", "int", minimum=0, maximum=65535, default=0),
            Field(
                "PROXY",
                "scheme",
                "协议",
                "str",
                default="http",
                hint="http / https / socks5。用输入框而不是下拉：各适配器对 socks5 的支持不一致，"
                "写死一份候选反而会挡掉本来能用的值",
            ),
            Field(
                "PROXY",
                "tunnel_server",
                "隧道代理接入地址",
                "str",
                hint="用三方隧道代理时填它，填了就不用上面的主机+端口。下面三项是它的参数",
            ),
            Field("PROXY", "channel_name", "隧道通道名", "str"),
            # toml 里默认是空字符串、ProxyConfig 声明的是 int|None。用 str 才能
            # 保住"留空"这个状态——用 int 会被迫写一个 0 进去，那是另一个意思。
            Field("PROXY", "session_ttl", "会话保持时长", "str", hint="留空表示不指定"),
            Field("PROXY", "country_code", "出口国家", "str", hint="隧道代理支持时才有意义，例如 US"),
        ),
    ),
    (
        "链路记录",
        (
            Field(
                "TRACE",
                "memory_size",
                "内存保留条数",
                "int",
                minimum=0,
                maximum=100000,
                restart=False,
                hint="0 = 关闭内存缓冲",
            ),
            Field("TRACE", "sqlite_enabled", "落盘到 SQLite", "bool", hint="开了才能查历史与跨天统计"),
            Field("TRACE", "sqlite_path", "数据库路径", "str"),
            Field("TRACE", "retention_days", "保留天数", "int", minimum=0, maximum=3650, hint="0 = 永久保留"),
            Field("TRACE", "only_errors", "只记失败请求", "bool", restart=False),
            Field(
                "TRACE",
                "queue_size",
                "落盘队列容量",
                "int",
                minimum=100,
                maximum=1000000,
                default=5000,
                hint="写盘跟不上时，超出这个数的记录直接丢弃。压测时「链路记录怎么少了一截」就是它",
            ),
            Field("TRACE", "record_url", "记录完整 URL", "bool", restart=False, hint="关掉后只记 host"),
        ),
    ),
    (
        # 注意：``forward`` **不在**这里。它在集群设置页顶部单独一个开关——
        # 那一项决定了下面的节点参不参与路由，摊在 fieldset 中间会被当成一个
        # 普通选项划过去。同一个配置项在一页上出现两次则更糟：两个控件显示的是
        # 同一个值，用户改了其中一个、另一个没跟着变，保存时以谁为准就成了谜。
        "集群",
        (
            Field("CLUSTER", "self_id", "本节点 id", "str", hint="留空则按监听地址自动识别"),
            Field("CLUSTER", "load_balancer", "负载均衡策略", "choice", choices=("round_robin", "random", "weight")),
            Field("CLUSTER", "max_failover", "最多换几个节点重试", "int", minimum=0, maximum=20),
            Field(
                "CLUSTER",
                "forward_timeout",
                "转发超时（秒）",
                "float",
                minimum=0,
                maximum=3600,
                restart=False,
                default=0,
                hint="0 = 按任务自己的超时自动推算。「子节点还在跑、入口先超时返回了」调这一项",
            ),
            Field("CLUSTER", "probe_interval", "探活间隔（秒）", "float", minimum=1, maximum=3600, restart=False),
            Field(
                "CLUSTER",
                "probe_timeout",
                "单次探活超时（秒）",
                "float",
                minimum=0.1,
                maximum=600,
                restart=False,
                default=3,
                hint="节点被打满、探活拿不到响应会被判成挂掉并摘走流量——压测时正是要调它的时候",
            ),
            Field("CLUSTER", "failure_threshold", "连续失败几次摘除", "int", minimum=1, maximum=100),
            Field("CLUSTER", "recovery_threshold", "连续成功几次恢复", "int", minimum=1, maximum=100),
        ),
    ),
    (
        # [WEB] 里只有这一项可编辑。用户名密码仍然只能改文件（见模块开头），
        # 而主题是纯外观、改错了最坏结果是"页面颜色不对"，没有安全后果。
        "Web 管理端",
        (
            Field(
                "WEB",
                "theme",
                "页面主题",
                "choice",
                choices=("light", "dark"),
                default="light",
                hint="这台机器上打开时的默认值；浏览器里手动点过的选择优先于它",
            ),
        ),
    ),
    (
        "客户端与压缩",
        (
            Field("CLIENT", "rpc_max_retries", "RPC 重试次数", "int", minimum=0, maximum=20),
            Field("CLIENT", "rpc_retry_backoff", "RPC 退避基数（秒）", "float", minimum=0, maximum=60),
            Field(
                "CLIENT",
                "compression",
                "请求压缩",
                "choice",
                choices=("auto", "gzip", "none"),
                hint="自动化脚本压缩后通常只剩几十分之一",
            ),
            Field("CLIENT", "compression_threshold", "压缩门槛（字节）", "int", minimum=0, maximum=100 * 1024 * 1024),
        ),
    ),
)

#: 按表单字段名索引。
FIELDS: dict[str, Field] = {f.name: f for _, fields in GROUPS for f in fields}

#: 归到「集群设置」那一页的组名。其余全在「基础设置」。
CLUSTER_GROUPS: frozenset[str] = frozenset({"集群"})


def groups_for(tab: str) -> tuple[tuple[str, tuple[Field, ...]], ...]:
    """某个分页该显示哪些组。

    分页只影响**展示**，不影响权限——两页提交的都是同一个 ``parse_form``，
    白名单还是那一份。把它做成"按组名归类"而不是再列一张表，是为了加一组配置项
    时不必记得同步两个地方；漏同步的症状是"新加的项在页面上找不到"。
    """
    cluster = tab == "cluster"
    return tuple((name, fields) for name, fields in GROUPS if (name in CLUSTER_GROUPS) is cluster)


def current_value(config: Any, field: Field) -> Any:
    """取一项的当前生效值。配置里没写就回落到该项的默认值。

    刻意不返回 None 让页面显示空白：那样用户一点保存就把空值写进配置文件，
    等于在不知不觉中改了行为。
    """
    node: Any = config
    for part in field.section.split("."):
        if not isinstance(node, dict):
            return field.default
        node = node.get(part) or {}
    if not isinstance(node, dict) or field.key not in node:
        return field.default
    return node[field.key]


def parse_form(form: dict[str, str]) -> tuple[dict[str, dict[str, Any]], list[str], list[str]]:
    """把提交的表单解析成写回用的结构。

    只处理白名单里、且与当前提交值有关的字段；表单里出现的其他键一律忽略——
    不能让一个手工构造的 POST 写进任意配置项。

    Returns:
        ``(更新内容, 需要重启的项, 错误信息)``。
    """
    updates: dict[str, dict[str, Any]] = {}
    restart_needed: list[str] = []
    errors: list[str] = []

    for name, field in FIELDS.items():
        if field.kind == "bool":
            # 复选框未勾选时浏览器不会提交这个键，所以要靠一个同名的隐藏标记
            # 判断"这一项到底在不在本次表单里"，否则每次保存都会把未展示的
            # 复选框写成 false。
            if f"__present__{name}" not in form:
                continue
            value: Any = name in form
        else:
            if name not in form:
                continue
            try:
                value = field.parse(form[name])
            except ValidationError as e:
                errors.append(str(e))
                continue
        updates.setdefault(field.section, {})[field.key] = value
        if field.restart:
            restart_needed.append(field.label)

    return updates, restart_needed, errors


def parse_nodes(form: dict[str, str]) -> list[dict[str, Any]]:
    """从表单解析节点列表。

    表单用 ``node_id_0`` / ``node_address_0`` … 这种带序号的命名，序号不连续也没关系
    （删掉中间一行时就会不连续）。地址为空的行视为删除。
    """
    indexes: set[int] = set()
    for key in form:
        if key.startswith("node_address_"):
            suffix = key[len("node_address_") :]
            if suffix.isdigit():
                indexes.add(int(suffix))

    nodes: list[dict[str, Any]] = []
    for index in sorted(indexes):
        address = form.get(f"node_address_{index}", "").strip()
        if not address:
            continue  # 清空地址 = 删除这一行
        node_id = form.get(f"node_id_{index}", "").strip() or address
        try:
            weight = max(1, int(form.get(f"node_weight_{index}", "100") or 100))
        except ValueError:
            weight = 100
        nodes.append({"id": node_id, "address": address, "weight": weight})

    # 新增行
    new_address = form.get("new_node_address", "").strip()
    if new_address:
        new_id = form.get("new_node_id", "").strip() or new_address
        try:
            weight = max(1, int(form.get("new_node_weight", "100") or 100))
        except ValueError:
            weight = 100
        nodes.append({"id": new_id, "address": new_address, "weight": weight})

    return nodes


def validate_nodes(nodes: list[dict[str, Any]]) -> list[str]:
    """校验节点列表。返回错误信息列表（空表示通过）。"""
    from ipclick.cluster.node import Node

    errors: list[str] = []
    seen: set[str] = set()
    for index, entry in enumerate(nodes):
        try:
            _ = Node.from_config(entry, index)
        except Exception as e:
            errors.append(f"第 {index + 1} 行：{e}")
            continue
        if entry["id"] in seen:
            # 重复 id 会让轮询与健康状态错乱：两台机器共用一份状态
            errors.append(f"节点 id {entry['id']!r} 重复")
        seen.add(entry["id"])
    return errors


__all__ = [
    "FIELDS",
    "GROUPS",
    "Field",
    "current_value",
    "parse_form",
    "parse_nodes",
    "validate_nodes",
]
