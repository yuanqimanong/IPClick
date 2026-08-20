"""IPClick 对外暴露的异常层次。"""


class IPClickError(Exception):
    """所有 IPClick 领域异常的基类。"""


class ConfigError(IPClickError):
    """配置缺失、格式错误或配置写入失败。"""


class AdapterError(IPClickError):
    """HTTP 或浏览器适配器执行失败。"""


class TransportError(IPClickError):
    """客户端与 gRPC 服务端之间的传输失败。"""


class ClientClosedError(IPClickError):
    """调用已经关闭的客户端。"""


class AuthenticationError(IPClickError):
    """服务端拒绝了客户端凭据。"""


class RequestError(IPClickError):
    """请求已执行，但响应或目标站点状态不符合预期。"""


class ValidationError(IPClickError, ValueError):
    """调用参数无法转换为合法请求。"""


class URLNotAllowedError(ValidationError):
    """URL 被协议或 SSRF 准入策略拒绝。"""


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
