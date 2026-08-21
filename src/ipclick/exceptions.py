"""IPClick 对外暴露的异常层次。"""


class IPClickError(Exception):
    """所有 IPClick 领域异常的基类。"""


class ConfigError(IPClickError):
    """配置缺失、格式错误或配置写入失败。"""


class AdapterError(IPClickError):
    """HTTP 或浏览器适配器执行失败。"""


class TransportError(IPClickError):
    """客户端与 gRPC 服务端之间的传输失败。

    ``grpc_code`` 保留原始的 gRPC 状态码（没有对应码时为 ``None``）。集群客户端要靠它
    区分"连都没连上"和"连上了但服务端内部出错"——前者该立刻把节点摘掉，后者不该，
    否则抓一个让服务端报错的目标就能把整个集群摘空。
    """

    def __init__(self, *args: object, grpc_code: object = None) -> None:
        super().__init__(*args)
        self.grpc_code: object = grpc_code


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


class HostResolutionError(IPClickError):
    """目标主机的 DNS 解析失败。

    刻意不继承 ``URLNotAllowedError``：解析不出来是网络故障，不是"被策略拒绝"。
    两者的排查方向完全相反——一个查 DNS/网络，一个去改 [SECURITY] 白名单。
    服务端把它映射成普通的失败响应（``status_code == -1`` 且 error 非空），
    与 README 承诺的"不会因为网络问题抛异常"一致。
    """


__all__ = [
    "AdapterError",
    "AuthenticationError",
    "ClientClosedError",
    "ConfigError",
    "HostResolutionError",
    "IPClickError",
    "RequestError",
    "TransportError",
    "URLNotAllowedError",
    "ValidationError",
]
