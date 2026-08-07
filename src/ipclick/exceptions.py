"""IPClick 的异常层次。

原先各处直接 ``raise Exception(...)``，调用方只能用 ``except Exception`` 兜底，
既无法按类型区分"连不上服务端"和"目标站点返回错误"，也会顺手吞掉
KeyboardInterrupt 之外的所有程序 bug。
"""


class IPClickError(Exception):
    """所有 IPClick 异常的基类。"""


class ConfigError(IPClickError):
    """配置文件缺失、格式错误或取值非法。"""


class AdapterError(IPClickError):
    """适配器不存在、未安装依赖或初始化失败。"""


class TransportError(IPClickError):
    """与 IPClick 服务端之间的 gRPC 通信失败（连不上、超时、被拒绝）。"""


class RequestError(IPClickError):
    """下载任务本身失败（目标站点不可达、状态码异常等）。"""


class ValidationError(IPClickError, ValueError):
    """任务参数校验失败。

    同时继承 ValueError，兼容原先 ``except ValueError`` 的调用方。
    """


class URLNotAllowedError(ValidationError):
    """目标 URL 被服务端安全策略拒绝（协议不允许 / 命中内网或元数据地址）。"""


__all__ = [
    "AdapterError",
    "ConfigError",
    "IPClickError",
    "RequestError",
    "TransportError",
    "URLNotAllowedError",
    "ValidationError",
]
