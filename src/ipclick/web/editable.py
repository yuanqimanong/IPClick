"""声明 Web 可编辑配置字段，并把表单输入转换为受校验的配置更新。"""

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
    """一个可编辑配置项的类型、约束和展示元数据。"""

    section: str
    key: str
    label: str
    kind: FieldKind
    restart: bool = True
    minimum: float | None = None
    maximum: float | None = None
    choices: tuple[str, ...] = ()
    hint: str = ""
    default: Any = None

    @property
    def name(self) -> str:
        """返回表单使用的稳定全限定字段名。"""
        return f"{self.section}.{self.key}"

    def parse(self, raw: str) -> str | int | float | bool:
        """按字段类型和边界解析原始表单文本。"""
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
                hint="每个请求占一个线程做阻塞 IO；同时也是批量的并发上限。"
                "⚠️ 吞吐上不去时调它没用——实测 32 / 100 / 256 三档吞吐一样（GIL 才是天花板），"
                "要加吞吐请看下面的「工作进程数」",
            ),
            Field(
                "SERVER",
                "processes",
                "工作进程数",
                "int",
                minimum=0,
                maximum=64,
                default=1,
                hint="1 = 单进程（默认）；0 = 按 CPU 核数自动（上限 8）。"
                "多个进程靠 SO_REUSEPORT 共享同一个端口，分发由内核做，对调用方完全透明。"
                "这是唯一能突破 GIL 的办法——实测 1→4 进程吞吐 313→663 QPS。"
                "代价：内存按进程数近似线性增长；链路记录变成每进程一份，本页只看得到 0 号进程那一份",
            ),
            Field(
                "SERVER",
                "max_concurrent_rpcs",
                "在途 RPC 上限",
                "int",
                minimum=0,
                maximum=1000000,
                default=0,
                hint="0 = worker 线程数 × 8。这一项决定「排队能排多长」，和 worker 线程数"
                "（「同时能干多少活」）是两件事。"
                "0.5.0 里它被写死成线程数×2，于是 500 并发就开始拒流——实测成功率掉到 25.8%，"
                "而那时服务端 CPU 只用了 1.45 个核。调大它不额外消耗资源",
            ),
            Field(
                "SERVER",
                "max_concurrent_streams",
                "单连接并发流上限",
                "int",
                minimum=0,
                maximum=1000000,
                default=0,
                hint="0 = 跟随上面的在途 RPC 上限（不低于 100）。"
                "SDK 一个 Downloader 就是一条 TCP 连接，所以这个值直接就是单个客户端的并发天花板",
            ),
            Field(
                "SERVER",
                "compression",
                "响应压缩",
                "choice",
                choices=("gzip", "deflate", "none"),
                default="gzip",
                hint="几百字节到几 KB 的 JSON 压它省不下什么；抓整页 HTML、下载文件时才有用。"
                "实测大响应体时瓶颈通常是链路带宽而不是压缩本身",
            ),
            Field(
                "SERVER",
                "async_mode",
                "异步模式（实验性）",
                "bool",
                default=False,
                hint="换成 grpc.aio + 协程，不再一请求一线程。实测端到端 1.8×。"
                "⚠️ 实验性，默认关：自定义适配器会被丢进线程池跑（线程安全假设变了）；"
                "0.8 视反馈再考虑翻默认值。想吃满多核仍要靠上面的「工作进程数」——"
                "协程解决并发模型，多进程解决多核",
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
            Field(
                "BROWSER",
                "max_pages",
                "并发页面上限（0 = 按可用内存自动推导）",
                "int",
                minimum=0,
                maximum=200,
            ),
            Field("BROWSER.timeout", "page_load", "页面加载超时（秒）", "float", minimum=1, maximum=600, default=30),
            Field("BROWSER.timeout", "script_exec", "脚本执行超时（秒）", "float", minimum=1, maximum=600, default=60),
            Field(
                "BROWSER.timeout",
                "settle",
                "等网络空闲的上限（秒）",
                "float",
                minimum=0,
                maximum=120,
                default=5,
                hint="仅 wait_until = networkidle 时生效；等不到不算失败，按 load 时的内容返回",
            ),
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
                default="networkidle",
                hint="对耗时影响最大的一项；load 更快但会静默丢掉 load 之后才出现的内容",
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

FIELDS: dict[str, Field] = {f.name: f for _, fields in GROUPS for f in fields}

CLUSTER_GROUPS: frozenset[str] = frozenset({"集群"})


def groups_for(tab: str) -> tuple[tuple[str, tuple[Field, ...]], ...]:
    """返回基础配置或集群配置标签页对应的字段组。"""
    cluster = tab == "cluster"
    return tuple((name, fields) for name, fields in GROUPS if (name in CLUSTER_GROUPS) is cluster)


def current_value(config: Any, field: Field) -> Any:
    """沿嵌套 section 读取字段值，缺失或结构异常时使用默认值。"""
    node: Any = config
    for part in field.section.split("."):
        if not isinstance(node, dict):
            return field.default
        node = node.get(part) or {}
    if not isinstance(node, dict) or field.key not in node:
        return field.default
    return node[field.key]


def parse_form(form: dict[str, str]) -> tuple[dict[str, dict[str, Any]], list[str], list[str]]:
    """解析配置表单，分别返回更新、需重启字段和校验错误。"""
    updates: dict[str, dict[str, Any]] = {}
    restart_needed: list[str] = []
    errors: list[str] = []

    for name, field in FIELDS.items():
        if field.kind == "bool":
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
    """按页面行号顺序解析已有节点和待新增节点。"""
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
            continue
        node_id = form.get(f"node_id_{index}", "").strip() or address
        try:
            weight = max(1, int(form.get(f"node_weight_{index}", "100") or 100))
        except ValueError:
            weight = 100
        nodes.append({"id": node_id, "address": address, "weight": weight})

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
    """复用集群领域模型校验节点，并额外拒绝重复 id。"""
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
