"""配置里路径值的 ``{port}`` 占位符。

要解决的是"同一目录起多个实例"（本地模拟集群、或一台机器上跑多个环境）：
用 ``--port`` 就能把 gRPC 端口岔开，但另外两项会**静默**撞车——

* ``[TRACE].sqlite_path``：多实例往同一个库里写，链路记录混在一起，界面上完全
  看不出来，用户以为在看单个实例的数据；
* ``[LOG].output``：多进程抢写同一个日志文件。

两者都不报错、不提示，这比"起不来"糟得多。

**为什么用占位符，而不是在代码里自动加端口后缀。** 自动加后缀会改掉已有部署里
用户自己写死的路径——旧日志和 trace 数据看起来就像丢了。占位符则是：新用户从
模板拿到的默认值就带 ``{port}``，天然按端口分离；已有部署一个字符都不变，想要
这个行为就自己把路径改成带 ``{port}``。而且这条规则写在 toml 里是**看得见的**，
不是藏在代码里的隐式行为。

**替换时机**：必须在命令行参数解析**之后**。``--port 9528`` 覆盖掉配置文件里的
9527 时，要替换成 9528——拿配置文件里那个原始值去替换等于白做。
"""

from __future__ import annotations

from typing import Any


#: 占位符字面量
PORT_PLACEHOLDER = "{port}"

#: 支持占位符的配置项：``节 -> (键, ...)``。
#:
#: 刻意只覆盖这两项而不是"所有看起来像路径的值"：证书路径、可执行文件路径
#: 那些按端口分离没有任何意义，无差别替换只会制造惊吓。
PORT_AWARE_KEYS: dict[str, tuple[str, ...]] = {
    "TRACE": ("sqlite_path",),
    "LOG": ("output",),
}


def substitute_port(value: Any, port: int) -> Any:
    """把字符串里的 ``{port}`` 换成实际端口。非字符串原样返回。

    占位符出现在路径**中间**也要能替换（``logs/{port}/app.log`` 是合理写法），
    所以用 replace 而不是判断结尾。
    """
    if not isinstance(value, str) or PORT_PLACEHOLDER not in value:
        return value
    return value.replace(PORT_PLACEHOLDER, str(port))


def resolve_section(section: dict[str, Any], keys: tuple[str, ...], port: int) -> dict[str, Any]:
    """返回一份替换过占位符的**副本**。

    刻意返回副本而不是就地改：``load_config`` 带 lru_cache，返回的是进程内共享
    的那一个对象，改它会波及所有调用方——包括 Web 的配置页，那里必须显示文件里
    的原始写法（``ipclick-trace.{port}.db`` 本身就是要让人看见的）。
    """
    resolved = dict(section)
    for key in keys:
        if key in resolved:
            resolved[key] = substitute_port(resolved[key], port)
    return resolved


def resolve_for(section_name: str, section: dict[str, Any], port: int) -> dict[str, Any]:
    """按 :data:`PORT_AWARE_KEYS` 解析某一节。不支持占位符的节原样返回副本。"""
    return resolve_section(section, PORT_AWARE_KEYS.get(section_name, ()), port)


def has_placeholder(value: Any) -> bool:
    return isinstance(value, str) and PORT_PLACEHOLDER in value


__all__ = [
    "PORT_AWARE_KEYS",
    "PORT_PLACEHOLDER",
    "has_placeholder",
    "resolve_for",
    "resolve_section",
    "substitute_port",
]
