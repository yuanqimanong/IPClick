"""Web 端可编辑的配置项白名单。

这个文件回答的是"网页能改什么"。名单是白名单而不是黑名单——漏写一项的后果是
"改不了"（有人来问），而黑名单漏写一项的后果是"能改本不该改的"（没人会来问）。

**刻意不可编辑**的几类，页面上只展示、要改就去改文件：

* ``[SECURITY]`` 全部。令牌、TLS 证书路径、SSRF 三个开关（``block_private_networks``
  / ``block_metadata_endpoints`` / ``allowed_schemes``）。这个服务能代任意 URL 发
  请求，一个能从网页关掉内网拦截的管理端，等于给自己装了个 SSRF 跳板。
* ``[WEB]`` 的用户名密码。改自己的登录凭据必须经过文件（那还要求有机器的 shell）。
* ``[CLUSTER].secret`` 与节点的 ``token``。机密不进 toml，正规位置是 ``.env``。
* ``[BROWSER].allow_scripts``。它等于允许调用方在服务端的浏览器里跑任意 JS。

节点列表（``[CLUSTER].nodes``）是**可编辑**的：加减机器是这套集群的日常操作，
而且节点地址本身不是机密。但节点的 ``token`` 字段不接受从网页写入。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, final

from ipclick.adapters.browser_engines import ENGINE_NAMES
from ipclick.exceptions import ValidationError


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
GROUPS: tuple[tuple[str, tuple[Field, ...]], ...] = (
    (
        "服务端",
        (
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
        ),
    ),
    (
        "下载行为",
        (
            Field("DOWNLOADER", "download_timeout", "单次请求超时（秒）", "float", minimum=0.1, maximum=3600),
            Field("DOWNLOADER", "connect_timeout", "连接超时（秒）", "float", minimum=0.1, maximum=600),
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
            Field("TRACE", "record_url", "记录完整 URL", "bool", restart=False, hint="关掉后只记 host"),
        ),
    ),
    (
        "集群",
        (
            Field(
                "CLUSTER",
                "forward",
                "服务端转发",
                "choice",
                choices=("off", "on"),
                hint="on = 本节点收到任务后按策略分发给其他节点",
            ),
            Field("CLUSTER", "self_id", "本节点 id", "str", hint="留空则按监听地址自动识别"),
            Field("CLUSTER", "load_balancer", "负载均衡策略", "choice", choices=("round_robin", "random", "weight")),
            Field("CLUSTER", "max_failover", "最多换几个节点重试", "int", minimum=0, maximum=20),
            Field("CLUSTER", "probe_interval", "探活间隔（秒）", "float", minimum=1, maximum=3600),
            Field("CLUSTER", "failure_threshold", "连续失败几次摘除", "int", minimum=1, maximum=100),
            Field("CLUSTER", "recovery_threshold", "连续成功几次恢复", "int", minimum=1, maximum=100),
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
