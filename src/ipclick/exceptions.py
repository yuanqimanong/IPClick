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


class ClientClosedError(IPClickError):
    """在已关闭的客户端上继续发请求。

    与 AuthenticationError 同理，刻意**不**继承 TransportError——这是调用方的
    使用错误，重试多少次都不会好，不该被伪装成一次网络失败吞成 -1 响应。
    """


class AuthenticationError(IPClickError):
    """鉴权失败：令牌缺失或不正确。

    刻意**不**继承 TransportError——令牌错了重试多少次都没用，属于配置问题，
    应该直接抛给调用方，而不是伪装成一次网络失败被吞掉。
    """


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
    "AuthenticationError",
    "ClientClosedError",
    "ConfigError",
    "IPClickError",
    "RequestError",
    "TransportError",
    "URLNotAllowedError",
    "ValidationError",
]
