import threading
from typing import Any

from typing_extensions import override

from ipclick.adapters.base import DownloaderAdapter, retry
from ipclick.dto.response import Response
from ipclick.exceptions import AdapterError
from ipclick.utils.log_util import log


# 可选依赖：缺失时降级为 None，由 __init__ 抛 AdapterError。
_httpx: Any
_user_agent_cls: Any

try:
    import httpx as _httpx
except ImportError:  # pragma: no cover - 取决于安装环境
    _httpx = None

try:
    from fake_useragent import UserAgent as _user_agent_cls
except ImportError:  # pragma: no cover - 取决于安装环境
    _user_agent_cls = None

HTTPX_AVAILABLE: bool = _httpx is not None
FAKE_UA_AVAILABLE: bool = _user_agent_cls is not None


# Client.request() 层面支持、且允许调用方通过 kwargs 透传的参数。
# 注意 verify / cert / trust_env 是 Client 构造参数，不能传给 request()。
_PASSTHROUGH_KWARGS = frozenset({"content", "auth", "extensions"})


class HttpxAdapter(DownloaderAdapter):
    """
    httpx适配器 - 现代HTTP客户端

    特点：
    - 支持异步操作
    - HTTP/2支持
    - 完善的API
    """

    adapter_name: str = "httpx"

    def __init__(self):
        if _httpx is None:
            raise AdapterError("httpx is not installed. Install it with: pip install httpx")

        super().__init__()
        # 按 (proxy, verify) 缓存 Client，以复用连接池
        self._clients: dict[tuple[str | None, bool], Any] = {}
        self._clients_lock: threading.Lock = threading.Lock()
        # 是否读取环境变量里的代理配置，默认关闭（见 _get_client 的说明）
        self.trust_env: bool = False

        # User Agent生成器
        self.ua_generator: Any = _user_agent_cls(platforms="desktop") if _user_agent_cls is not None else None

    def _get_client(self, proxy: str | None, verify: bool) -> Any:
        """取得（并缓存）一个 httpx.Client。

        原实现走的是模块级 ``httpx.request()``，每次调用都新建一个 Client、
        建新连接、再丢弃——完全没有连接池可言。
        """
        key = (proxy, verify)
        client = self._clients.get(key)
        if client is not None:
            return client

        with self._clients_lock:
            if key not in self._clients:
                self._clients[key] = _httpx.Client(
                    proxy=proxy,
                    verify=verify,
                    follow_redirects=True,
                    # 不继承环境里的 HTTP_PROXY/ALL_PROXY：调用方通过 proxy 参数
                    # 显式指定代理才是本工具的语义，静默套用服务端所在机器的
                    # 环境代理会让请求走到意料之外的出口（gRPC channel 也同样
                    # 设了 enable_http_proxy=0）。需要时可通过 kwargs 传
                    # trust_env=true 显式开启。
                    trust_env=bool(self.trust_env),
                    limits=_httpx.Limits(max_connections=100, max_keepalive_connections=20),
                )
            return self._clients[key]

    @override
    @retry()
    def download(
        self,
        url: str,
        *,
        method: str = "GET",
        headers: dict[str, Any] | None = None,
        cookies: dict[str, Any] | str | None = None,
        params: dict[str, Any] | None = None,
        data: Any = None,
        json: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
        proxy: str | None = None,
        timeout: float = 60,
        max_retries: int = 3,
        retry_delay: float = 2.0,
        verify: bool = True,
        allow_redirects: bool = True,
        stream: bool = False,
        impersonate: str | None = None,
        extensions: dict[str, Any] | None = None,
        automation_config: str | None = None,
        automation_script: str | None = None,
        allowed_status_codes: list[Any] | None = None,
        kwargs: str | None = None,
    ) -> Response:
        """
        使用httpx执行HTTP请求
        """
        method = method.upper()
        extra = self.parse_extra_kwargs(kwargs)

        # 设置默认headers
        if headers is None:
            headers = {
                "User-Agent": self._get_user_agent(),
                "Accept": "*/*",
                "Accept-Encoding": "gzip, deflate, br",
                "Connection": "keep-alive",
            }
        elif "User-Agent" not in headers and "user-agent" not in headers:
            headers = {**headers, "User-Agent": self._get_user_agent()}

        request_kwargs: dict[str, Any] = {
            "params": params,
            "headers": headers,
            "cookies": cookies,
            "timeout": timeout or self.timeout,
            # 这三个以前被完全忽略：json 体直接丢失，allow_redirects 形同虚设。
            "json": json,
            "files": files,
            "follow_redirects": allow_redirects,
        }
        # data 与 content 互斥，按调用方给的为准
        if data is not None:
            request_kwargs["data"] = data

        # 透传调用方显式指定的额外参数
        for key in _PASSTHROUGH_KWARGS:
            if key in extra:
                request_kwargs[key] = extra[key]

        # 移除 None 值（httpx 对 None 与"未传"处理不同）
        request_kwargs = {k: v for k, v in request_kwargs.items() if v is not None}

        client = self._get_client(proxy, verify)

        try:
            httpx_resp = client.request(method, url, **request_kwargs)

            return Response(
                url=str(httpx_resp.url),
                status_code=httpx_resp.status_code,
                content=httpx_resp.content,
                text=httpx_resp.text,
                headers=dict(httpx_resp.headers),
                raw_response=httpx_resp,
            )

        except Exception as e:
            # 只记一行，堆栈交给 retry 装饰器最终失败时处理——
            # 否则每次重试都会打一份完整 traceback。
            log.warning(f"httpx request failed for {url}: {e}")
            raise

    @override
    def close(self) -> None:
        """关闭所有缓存的 Client"""
        with self._clients_lock:
            for client in self._clients.values():
                try:
                    client.close()
                except Exception as e:
                    log.debug(f"关闭 httpx client 失败: {e}")
            self._clients.clear()


def is_available() -> bool:
    """检查httpx是否可用"""
    return HTTPX_AVAILABLE
