from abc import ABC, abstractmethod
import asyncio
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass, field
import functools
from random import randrange, uniform
import re
import threading
import time
from typing import Any, cast

from ipclick.adapters.settings import DEFAULT_RETRY_STATUS_CODES, AdapterSettings
from ipclick.dto.response import Response
from ipclick.exceptions import AdapterError, ValidationError
from ipclick.trace import get_recorder
from ipclick.utils.log_util import log


#: 流式传输的默认分片大小（字节）。64KB 是吞吐与 gRPC 消息开销之间的常见折中。
DEFAULT_CHUNK_SIZE = 64 * 1024

#: 预生成的 User-Agent 池大小。
#: fake_useragent 取一次要 2.82ms（纯 Python，持有 GIL），放在每请求的热路径上
#: 实测让吞吐掉到 1/4.4。池化之后每请求只剩一次 list 索引，而轮换 UA 的效果
#: 依然存在。32 个的多样性对反检测足够，一次性构建成本约 90ms 且是惰性的。
UA_POOL_SIZE = 32


@dataclass
class StreamHeader:
    """流式响应的元信息，总是第一个 yield 出来的元素。"""

    url: str
    status_code: int
    headers: dict[str, str] = field(default_factory=dict)
    error: str | None = None
    #: 服务端声明的总长度；未知时为 -1
    content_length: int = -1


#: 流式迭代产出的元素：首个是 StreamHeader，之后都是 bytes 分片。
StreamEvent = StreamHeader | bytes


# 单次重试等待的上限（秒）。服务端每个请求占用一个 gRPC worker 线程，
# 原来的 min(2**attempt, 600) 在 max_retries 稍大时会让线程睡上十分钟。
# 现在可由 [DOWNLOADER.retry].max_backoff 覆盖，此处仅作为未配置时的默认值。
MAX_RETRY_DELAY = AdapterSettings().max_backoff


def _coerce_delay(value: Any, default: float) -> float:
    """把 retry_delay 归一成秒数。

    历史上它既可能是 float（服务端传来的 retry_backoff_seconds），
    也可能是 (min, max) 元组（适配器自身的默认值）。
    """
    if value is None:
        return default
    try:
        if isinstance(value, (tuple, list)):
            if len(value) != 2:
                return default
            low, high = float(value[0]), float(value[1])
            return uniform(low, high)
        return float(value)
    except (TypeError, ValueError):
        return default


def retry(
    max_retries_attr: str = "max_retries", retry_delay_attr: str = "retry_delay"
) -> Callable[[Callable[..., Response]], Callable[..., Response]]:
    """
    重试装饰器，支持指数退避和随机延迟

    返回类型写全（而不是 ``Callable[..., Any]``）是为了让被装饰的方法保住
    ``-> Response``：写成 Any 的话，类型检查器认为 ``self.download(...)`` 返回
    Any，之后对结果的任何误用都查不出来——而这个装饰器套在每一个适配器的
    download 上。

    Args:
        max_retries_attr: 最大重试次数属性名
        retry_delay_attr: 重试延迟属性名
    """

    def decorator(func: Callable[..., Response]) -> Callable[..., Response]:
        @functools.wraps(func)
        def wrapper(self: Any, *args: Any, **kwargs: Any) -> Response:
            # 用 `is None` 判断而不是 `or`：max_retries=0 / retry_delay=0
            # 是合法取值（表示"不重试"/"不等待"），用 `or` 会被当成未传而回落到默认值。
            requested_retries = kwargs.get("max_retries")
            max_retries = (
                int(requested_retries) if requested_retries is not None else getattr(self, max_retries_attr, 3)
            )
            max_retries = max(0, int(max_retries))

            requested_delay = kwargs.get("retry_delay")
            base_delay = _coerce_delay(
                requested_delay if requested_delay is not None else getattr(self, retry_delay_attr, 1.0),
                default=1.0,
            )

            url = args[0] if args else kwargs.get("url", "unknown")
            allowed = kwargs.get("allowed_status_codes") or None

            # 退避参数取自适配器的 settings（来自 [DOWNLOADER.retry]），
            # 没有 settings 的适配器（如测试里的假实现）回落到模块默认值。
            settings: AdapterSettings | None = getattr(self, "settings", None)
            retry_codes = settings.retry_codes if settings else DEFAULT_RETRY_STATUS_CODES
            exponent = settings.backoff_exponent if settings else 2.0
            max_backoff = settings.max_backoff if settings else MAX_RETRY_DELAY

            last_exception: Exception | None = None

            for attempt in range(max_retries + 1):
                start_time = time.monotonic()
                try:
                    result = func(self, *args, **kwargs)

                    if hasattr(result, "elapsed_ms") and result.elapsed_ms == 0:
                        result.elapsed_ms = int((time.monotonic() - start_time) * 1000)

                    # 记下这是第几次尝试。适配器返回的对象大多是 Response，
                    # 但测试里也有别的假实现，所以先确认属性存在。
                    if hasattr(result, "attempts"):
                        result.attempts = attempt + 1

                    # 状态码级重试：allowed_status_codes 给出的是"可接受"的状态码，
                    # 其余落在重试名单里的（429/5xx）值得再试一次。
                    status = getattr(result, "status_code", None)
                    if (
                        attempt < max_retries
                        and isinstance(status, int)
                        and status in retry_codes
                        and not (allowed and status in allowed)
                    ):
                        sleep_time = _backoff(attempt, base_delay, exponent, max_backoff)
                        get_recorder().record_retry(getattr(self, "adapter_name", "unknown"), "status_code")
                        log.warning(
                            f"Download {url} returned {status}, "
                            f"retrying {attempt + 1}/{max_retries} in {sleep_time:.1f}s..."
                        )
                        time.sleep(sleep_time)
                        continue

                    return result

                except ValidationError:
                    # 参数错误重试多少次都是同样的结果，纯属浪费——默认配置下
                    # 一个"不支持的方法"要先睡够 1+2+4 秒才返回。而且吞成 -1
                    # 响应等于把调用方的用法错误伪装成一次网络故障；
                    # TaskService 那边本来就会把它映射成 INVALID_ARGUMENT。
                    raise

                except AdapterError:
                    # 同理：AdapterError 的含义是"本服务端做不到"——依赖没装、
                    # 浏览器本体没下、渲染被关掉、浏览器起不来或超时。重试改变不了
                    # 其中任何一条。
                    #
                    # 代价还特别大：浏览器请求一次的预算本身就是几十上百秒，
                    # 被这里重试 3 次就变成四倍。实测一次「试一试」点击因此挂了
                    # 296 秒，而用户在页面上什么提示都看不到。
                    raise

                except Exception as e:
                    last_exception = e

                    if attempt >= max_retries:
                        return Response.error_response(url, e, attempts=attempt + 1)

                    sleep_time = _backoff(attempt, base_delay, exponent, max_backoff)
                    get_recorder().record_retry(getattr(self, "adapter_name", "unknown"), "exception")
                    # 原来这行日志裹在 `if hasattr(self, "logger")` 里，而适配器
                    # 从来没有 logger 属性，等于重试全程静默。
                    log.warning(
                        f"Download {url} failed, retrying {attempt + 1}/{max_retries} "
                        f"in {sleep_time:.1f}s... Error: {e}"
                    )
                    time.sleep(sleep_time)

            return Response.error_response(
                url, last_exception or Exception("Max retries exceeded"), attempts=max_retries + 1
            )

        return wrapper

    return decorator


def _backoff(
    attempt: int,
    base_delay: float,
    exponent: float = 2.0,
    max_backoff: float = MAX_RETRY_DELAY,
) -> float:
    """指数退避 + 抖动，并封顶到 max_backoff。

    抖动可以避免多个并发任务在同一时刻集体重试（惊群）。
    exponent / max_backoff 来自 [DOWNLOADER.retry] 配置。
    """
    delay = min(base_delay * (exponent**attempt), max_backoff)
    return delay * uniform(0.8, 1.2)


#: JS 引擎在脚本本身写错时用的错误名。这些名字由 ECMAScript 规范定义，
#: Playwright 与 CDP 都原样带出来，所以按文本判断比按异常类型判断可靠
#: （两条路径抛的异常类型不同，但错误文本里的 ``SyntaxError:`` 是一样的）。
_JS_AUTHOR_ERRORS = ("SyntaxError", "ReferenceError", "TypeError: ")


#: 已经是函数形式的脚本的开头。这类原样交给 evaluate。
_JS_FUNCTION_PREFIXES = ("function", "async", "(", "=>")


def normalize_js(script: str) -> str:
    """把 ``automation_script`` 归一成 Playwright ``evaluate`` 能吃的形式。

    统一三个写法，因为两套引擎的原生要求本来就不同：

    * DrissionPage 的 ``run_js`` 要求用 ``return x`` 取值。
    * Playwright 的 ``evaluate`` 要的是**表达式或函数**，顶层 ``return`` 直接
      ``SyntaxError: Illegal return statement``。

    调用方不该为了换个引擎重写脚本，所以这里做转换而不是把差异甩给调用方：

    ==============================  ==========================================
    写法                            处理
    ==============================  ==========================================
    ``() => ...`` / ``function...``  原样透传
    含 ``return`` 的语句块          包成 ``() => { ... }``
    单个表达式                      包成 ``() => (...)``
    ==============================  ==========================================
    """
    text = script.strip()
    if not text:
        return text
    if text.startswith(_JS_FUNCTION_PREFIXES):
        return text
    # 用词边界匹配，避免把 `returnValue` 这种标识符误判成 return 语句
    if re.search(r"\breturn\b", text):
        return f"() => {{ {text} }}"
    return f"() => ({text})"


#: 浏览器导航的**永久性**失败。重试它们改变不了结果——URL 本身或目标就是这样。
#:
#: 与之相对，ERR_CONNECTION_REFUSED / ERR_TIMED_OUT 这类是瞬时的，照常重试。
_PERMANENT_NAV_ERRORS = (
    "ERR_UNSAFE_PORT",  # Chromium 拒绝连的端口（1、7、25 等），换多少次都一样
    "ERR_UNKNOWN_URL_SCHEME",
    "ERR_INVALID_URL",
    "ERR_DISALLOWED_URL_SCHEME",
    "ERR_BLOCKED_BY_CLIENT",
)


def raise_if_permanent_navigation_error(error: Exception) -> None:
    """浏览器说"这个 URL 我压根不会去连"时，转成参数错误。

    这类失败重试多少次都是同样的结果，而浏览器路径重试一次的代价是几秒到几十秒
    （实测一个 ERR_UNSAFE_PORT 的 URL 要 15.9 秒才返回）。而且报成 -1 会让调用方
    以为是网络故障，实际是自己 URL 写错了。
    """
    text = str(error)
    for marker in _PERMANENT_NAV_ERRORS:
        if marker in text:
            raise ValidationError(f"浏览器拒绝访问该 URL（{marker}）：{text}") from error


def raise_if_script_error(error: Exception, script: str | None) -> None:
    """脚本本身写错了就转成 ValidationError（不重试、报 INVALID_ARGUMENT）。

    ``automation_script`` 是**调用方提供的 JavaScript**，在页面里 evaluate。写错了
    （语法错、引用了不存在的变量）重试多少次都是同样的结果——默认配置下一个拼错的
    脚本要先起三次浏览器、睡够 15 秒才返回，而且最终报成 -1，看起来像网络故障。
    """
    if not script:
        return
    text = str(error)
    if any(name in text for name in _JS_AUTHOR_ERRORS):
        raise ValidationError(
            f"automation_script 有错（它是在页面里执行的 JavaScript，不是 Python）：{text}"
        ) from error


class DownloaderAdapter(ABC):
    """下载器抽象基类"""

    adapter_name: str = "base_downloader_adapter"

    def __init__(self, settings: AdapterSettings | None = None):
        """
        Args:
            settings: 来自配置文件 ``[DOWNLOADER]`` 节的默认行为。
                请求级参数（timeout / max_retries / ...）优先于这里的值。
        """
        self.settings: AdapterSettings = settings or AdapterSettings()
        #: 保护 UA 池的惰性构建（适配器实例在多个 worker 线程间共享）
        self._ua_lock: threading.Lock = threading.Lock()
        #: 预生成的 UA 池，None 表示还没建。见 _get_user_agent 的说明。
        self._ua_pool_cache: list[str] | None = None

        self.proxy: str | None = None
        # 这几个属性是 retry 装饰器和各适配器读取的"未显式传参时的默认值"
        self.max_retries: int = self.settings.max_attempts
        self.retry_delay: float = self.settings.initial_backoff
        self.timeout: float = self.settings.download_timeout
        self.connect_timeout: float = self.settings.connect_timeout
        self.verify_ssl: bool = True
        self.trust_env: bool = self.settings.trust_env
        # 兜底 UA：fake_useragent 不可用或抛错时使用
        self.user_agent: str = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )

    @abstractmethod
    def download(
        self,
        url: str,
        *,
        # 协议
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
        # 渲染
        automation_config: str | None = None,
        automation_script: str | None = None,
        allowed_status_codes: list[Any] | None = None,
        kwargs: str | None = None,
    ) -> Response:
        """
        执行HTTP请求

        Args:
            url: 请求URL
            method: 请求方法
            headers: 请求头
            cookies: 请求cookies
            params: 请求参数
            data: 请求数据
            json: 请求JSON数据
            files: multipart 文件上传（仅本地直接调用适配器时可用——
                gRPC 协议里没有这个字段，跨网络请自己拼 multipart 体走 data）
            proxy: 代理地址
            timeout: 超时时间
            max_retries: 最大重试次数
            retry_delay: 重试退避基数（秒）
            verify: SSL证书验证
            allow_redirects: 允许重定向
            stream: 是否流式读取
            impersonate: 浏览器指纹伪装（只有 curl_cffi 支持；其余适配器收到会报错）
            automation_config: 自动化配置
            automation_script: 自动化脚本
            allowed_status_codes: 可接受的状态码（不触发重试）
            kwargs: 透传给底层客户端的额外参数（JSON 字符串）

        Returns:
            Response: 统一的响应对象
        """
        raise NotImplementedError

    #: 这个适配器有没有自己的异步实现。
    #:
    #: 服务端的异步模式据此决定走哪条路：True 就 await adownload()，
    #: False 就把同步的 download() 丢进线程池。**默认 False**——这一条是
    #: 整套异步化能做成"加法"而不是破坏性变更的关键：项目支持注册自定义
    #: 适配器（见 README），它们只实现了同步 download()，不该因为服务端换了
    #: 并发模型就集体失效。
    supports_async: bool = False

    async def adownload(self, url: str, **kwargs: Any) -> Response:
        """异步执行一次请求。

        默认实现把同步的 :meth:`download` 丢进线程池——**语义完全一致，
        只是拿不到协程的好处**（那个线程仍然是稀缺资源）。真正想要收益的
        适配器覆写此方法并把 ``supports_async`` 置 True。

        为什么不把 download 直接改成 async：那会打断每一个第三方适配器，
        而它们看不到任何提示——基类方法签名变了，子类的同步实现会被当成
        协程去 await，报出来的错误（"object Response can't be used in
        'await' expression"）和真正的原因（"你的适配器该改成 async 了"）
        之间没有任何字面联系。
        """
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, functools.partial(self.download, url, **kwargs))

    async def adownload_stream(self, url: str, **kwargs: Any) -> "AsyncIterator[StreamEvent]":
        """异步流式。默认把同步迭代器逐个搬到线程池里取。

        逐个搬而不是一次性取完：流式的意义就在于响应体不整个进内存，
        先 list() 再 yield 等于把这个性质丢掉。
        """
        loop = asyncio.get_running_loop()
        iterator = await loop.run_in_executor(None, functools.partial(self.download_stream, url, **kwargs))
        sentinel = object()
        while True:
            item = await loop.run_in_executor(None, next, iterator, sentinel)
            if item is sentinel:
                return
            yield cast(StreamEvent, item)

    def download_stream(
        self,
        url: str,
        *,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        **kwargs: Any,
    ) -> "Iterator[StreamEvent]":
        """流式执行请求：先 yield 一个 :class:`StreamHeader`，随后 yield 若干 bytes 分片。

        与 :meth:`download` 的区别在于响应体不会整个进内存。适配器可以覆写此方法
        提供真正的流式实现；基类给出一个回退实现——先整体下载再切片，
        接口一致但省不了内存，仅用于尚未支持流式的适配器。

        注意这里**不套 @retry**：重试意味着要么缓存已发出的分片、要么让调用方
        看到重复数据，两者都不可接受。流式请求失败就是失败，由调用方决定重来。

        Yields:
            第一个元素是 :class:`StreamHeader`，之后是 ``bytes`` 分片。
        """
        response = self.download(url, **kwargs)
        yield StreamHeader(
            url=response.url,
            status_code=response.status_code,
            headers=response.headers or {},
            error=str(response.exception) if response.exception else None,
            content_length=len(response.content) if response.content else 0,
        )
        content = response.content or b""
        for start in range(0, len(content), chunk_size):
            yield content[start : start + chunk_size]

    def reject_impersonate(self, impersonate: str | None) -> None:
        """本适配器不支持浏览器指纹伪装时，显式报错而不是静默忽略。

        原来这个参数在 niquests 上是被默默丢掉的：调用方以为自己带了
        Chrome 指纹，实际发出去的是裸 TLS 握手，被 Cloudflare 挡下来时根本
        想不到是这里的问题。指纹伪装是反爬场景的核心诉求，"我以为开了但没开"
        比"明确告诉我做不到"糟得多。

        Raises:
            ValidationError: 显式指定了 impersonate。服务端会把它映射成
                INVALID_ARGUMENT，调用方能直接看到该换 curl_cffi。
        """
        if impersonate:
            raise ValidationError(
                f"{self.adapter_name} 不支持浏览器指纹伪装（impersonate={impersonate!r}）。"
                f"需要指纹伪装请用 adapter=curl_cffi，或去掉 impersonate 参数"
            )

    @staticmethod
    def parse_extra_kwargs(raw: str | None) -> dict[str, Any]:
        """解析透传的 kwargs JSON 字符串。

        SDK 总会发 ``"{}"``，但直接构造 DownloadTask 或用第三方 gRPC 客户端时
        可能是 ``None`` / 空串，此时不应该抛 JSONDecodeError。
        """
        if not raw:
            return {}
        try:
            import json as _json

            parsed = _json.loads(raw)
        except (ValueError, TypeError):
            log.warning("kwargs 不是合法 JSON，已忽略")
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def _get_user_agent(self) -> str:
        """获取 User-Agent，fake_useragent 不可用时回退到内置 UA。

        **不要**每次都调 ``generator.random``——实测那一次调用要 2.82ms，
        单线程上限只有 355 次/秒，而且是纯 Python、全程持有 GIL。它此前
        在 niquests 适配器的热路径上每请求调一次，实测把吞吐压到了 1/4.4
        （543 → 2368 QPS，并发 100）。这是整个服务端最贵的一处单点开销，
        比 GIL 本身、比 gRPC 序列化都贵。

        所以改成**预生成一个池子，请求时随机取一个**：轮换 UA 的意图
        （反检测）完整保留，但每请求的成本从 2.82ms 降到一次 list 索引。
        池子只建一次，摊到成千上万个请求上可以忽略。
        """
        pool = self._ua_pool
        if pool:
            return pool[randrange(len(pool))]
        return self.user_agent

    @property
    def _ua_pool(self) -> list[str]:
        """惰性构建的 User-Agent 池。

        惰性是必要的：curl_cffi 适配器靠 impersonate 决定指纹，从不取 UA，
        没理由让它在启动时白等一次池子构建（32 次取值约 90ms）。
        """
        cached = self._ua_pool_cache
        if cached is not None:
            return cached
        with self._ua_lock:
            # 双检：等锁期间可能已经有别的线程建好了
            if self._ua_pool_cache is not None:
                return self._ua_pool_cache
            generator = getattr(self, "ua_generator", None)
            pool: list[str] = []
            if generator is not None:
                for _ in range(UA_POOL_SIZE):
                    try:
                        pool.append(str(generator.random))
                    except Exception:
                        log.debug("fake_useragent 取值失败，使用内置 User-Agent")
                        break
            # 去重后仍然为空就退回内置 UA，调用方永远拿得到一个可用的值
            self._ua_pool_cache = sorted(set(pool)) or [self.user_agent]
            return self._ua_pool_cache

    def get(self, url: str, **kwargs: Any) -> Response:
        """GET请求快捷方法"""
        return self.download(url, method="GET", **kwargs)

    def post(self, url: str, **kwargs: Any) -> Response:
        """POST请求快捷方法"""
        return self.download(url, method="POST", **kwargs)

    def put(self, url: str, **kwargs: Any) -> Response:
        """PUT请求快捷方法"""
        return self.download(url, method="PUT", **kwargs)

    def delete(self, url: str, **kwargs: Any) -> Response:
        """DELETE请求快捷方法"""
        return self.download(url, method="DELETE", **kwargs)

    def head(self, url: str, **kwargs: Any) -> Response:
        """HEAD请求快捷方法"""
        return self.download(url, method="HEAD", **kwargs)

    def options(self, url: str, **kwargs: Any) -> Response:
        """OPTIONS请求快捷方法"""
        return self.download(url, method="OPTIONS", **kwargs)

    def close(self) -> None:  # noqa: B027 - 基类默认无资源可关，子类按需覆写
        """关闭连接，释放资源"""

    def __enter__(self) -> "DownloaderAdapter":
        """上下文管理器入口"""
        return self

    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        """上下文管理器退出"""
        self.close()
