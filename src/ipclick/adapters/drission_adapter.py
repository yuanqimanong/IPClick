"""DrissionPage 浏览器渲染适配器（Windows 上的默认引擎）。

DrissionPage 直接走 CDP 控制本机的 Chrome/Chromium，不需要 Playwright，也不用
额外下载浏览器——Windows 上基本都装了 Chrome，这让它成为那边最省事的选择。

与 Playwright 系（:mod:`ipclick.adapters.browser_adapter`）的结构性差异：

**没有 BrowserContext。** Playwright 可以在一个浏览器进程里开若干互相隔离的
context，cookie / localStorage 各自独立。DrissionPage 只有 tab，同一个浏览器里
所有 tab 共享一份 profile。对一个代任意调用方发请求的服务来说，那意味着上一个
调用方的登录态会泄漏给下一个。这里的处理是：浏览器以 ``--incognito`` 启动，并且
每个请求用完自己的 tab 就关掉，请求之间再显式清一次 cookie。

**状态码要靠抓包拿。** ``tab.get()`` 只返回 bool，没有 HTTP 状态码。用
``tab.listen`` 监听主文档那一条请求才能拿到 ``response.status``。

**同步 API。** DrissionPage 本身是同步的、且不是线程安全的，所以这里同样把所有
操作串到一个专属线程上，用队列投递——gRPC 请求落在线程池的任意线程上，直接并发
访问同一个浏览器对象会出问题。
"""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
import json as jsonlib
import threading
from typing import Any

from typing_extensions import override

from ipclick.adapters.base import DownloaderAdapter, retry
from ipclick.adapters.browser_settings import BrowserSettings
from ipclick.adapters.settings import AdapterSettings
from ipclick.dto.response import Response
from ipclick.exceptions import AdapterError, ValidationError
from ipclick.utils.log_util import log
from ipclick.utils.url_util import merge_query_params


_Chromium: Any
_ChromiumOptions: Any

try:
    from DrissionPage import Chromium as _Chromium
    from DrissionPage import ChromiumOptions as _ChromiumOptions
except ImportError:  # pragma: no cover - 取决于安装环境
    _Chromium = None
    _ChromiumOptions = None

DRISSIONPAGE_AVAILABLE: bool = _Chromium is not None

_SUPPORTED_METHODS = frozenset({"GET"})

#: 等待浏览器关闭的上限（秒）
_SHUTDOWN_TIMEOUT = 30.0


class DrissionPageAdapter(DownloaderAdapter):
    """基于 DrissionPage 的浏览器渲染适配器。

    对外契约与 :class:`~ipclick.adapters.browser_adapter.BrowserAdapter` 一致：
    只支持 GET、不能禁用重定向、不能带请求体，``automation_config`` 的键也一样。
    """

    adapter_name: str = "DrissionPage"

    def __init__(
        self,
        settings: AdapterSettings | None = None,
        browser_settings: BrowserSettings | None = None,
    ):
        if _Chromium is None:
            raise AdapterError('DrissionPage is not installed. Install it with: pip install "ipclick[drissionpage]"')

        resolved = browser_settings or BrowserSettings()
        if not resolved.enabled:
            raise AdapterError("浏览器渲染已被关闭（[BROWSER].enabled = false）")

        super().__init__(settings)
        self.browser_settings: BrowserSettings = resolved
        self.resolved_engine: str = "drissionpage"

        self._browser: Any = None
        self._browser_lock: threading.Lock = threading.Lock()
        # DrissionPage 不是线程安全的，所有浏览器操作都串到这一个线程上。
        # max_pages 由信号量控制并发页数，但真正的操作仍然是串行的——
        # 这比 Playwright 那套弱，是 CDP 同步 API 的固有限制。
        self._pool: ThreadPoolExecutor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ipclick-drissionpage")
        self._closed: bool = False

    # ------------------------------------------------------------------ #
    # 浏览器
    # ------------------------------------------------------------------ #

    def _options(self) -> Any:
        s = self.browser_settings
        options = _ChromiumOptions()
        if s.executable_path:
            options.set_browser_path(s.executable_path)
        options.headless(s.headless)
        # 无痕：DrissionPage 没有 BrowserContext，只能靠这个把 profile 隔离开，
        # 至少保证服务重启之间、以及与用户日常浏览器之间不共享数据。
        options.incognito(True)
        if s.no_sandbox:
            options.set_argument("--no-sandbox")
            options.set_argument("--disable-dev-shm-usage")
        for arg in s.args:
            options.set_argument(arg)
        if s.user_agent:
            options.set_user_agent(s.user_agent)
        if s.proxy_gateway:
            options.set_proxy(s.proxy_gateway)
        options.set_timeouts(base=s.page_load_timeout, page_load=s.page_load_timeout, script=s.script_timeout)
        # 随机端口：固定端口会和用户已经开着的浏览器抢，也会让两个 IPClick
        # 实例互相接管对方的浏览器。
        options.auto_port()
        return options

    def _ensure_browser(self) -> Any:
        if self._closed:
            raise AdapterError("适配器已关闭")
        if self._browser is not None:
            return self._browser
        with self._browser_lock:
            if self._browser is None:
                try:
                    self._browser = _Chromium(self._options())
                except Exception as e:
                    raise AdapterError(f"浏览器启动失败（DrissionPage）：{e}") from e
                log.info(f"DrissionPage 浏览器已启动：headless={self.browser_settings.headless}")
            return self._browser

    # ------------------------------------------------------------------ #
    # 参数
    # ------------------------------------------------------------------ #

    def _parse_automation_config(self, automation_config: str | None) -> dict[str, Any]:
        if not automation_config:
            return {}
        try:
            parsed = jsonlib.loads(automation_config)
        except (TypeError, ValueError) as e:
            raise ValidationError(f"automation_config 不是合法的 JSON: {e}") from e
        if not isinstance(parsed, dict):
            raise ValidationError(f"automation_config 必须是 JSON 对象，收到 {type(parsed).__name__}")
        return parsed

    # ------------------------------------------------------------------ #
    # 下载
    # ------------------------------------------------------------------ #

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
        allowed_status_codes: list[int] | None = None,
        kwargs: str | None = None,
    ) -> Response:
        """用真实浏览器打开页面，返回渲染后的 DOM。"""
        method = method.upper()
        if method not in _SUPPORTED_METHODS:
            raise ValidationError(
                f"浏览器渲染只支持 GET，收到 {method}。"
                "浏览器导航本身就是 GET，需要其他方法请改用 curl_cffi / httpx / requests 适配器。"
            )
        if not allow_redirects:
            raise ValidationError(
                "浏览器渲染无法禁用重定向。需要看原始 3xx 响应请改用 curl_cffi / httpx / requests 适配器。"
            )
        if data is not None or json is not None or files is not None:
            raise ValidationError("浏览器渲染不能携带请求体（data / json / files），请改用 HTTP 适配器。")

        config = self._parse_automation_config(automation_config)
        if automation_script and not self.browser_settings.allow_scripts:
            raise ValidationError(
                "服务端未开启 automation_script（[BROWSER].allow_scripts = false）。"
                "页面内 JS 可绕过 URL 安全策略访问内网，请确认调用方可信后再开启。"
            )
        if proxy and proxy != self.browser_settings.proxy_gateway:
            # DrissionPage 的代理是浏览器进程级的，启动后改不了。默默忽略等于
            # 让请求从错误的出口 IP 发出去——对一个代理服务来说是严重的静默错误。
            raise ValidationError(
                "DrissionPage 的代理是浏览器进程级的，不支持按请求指定。"
                "请改用 [BROWSER.proxy].gateway 全局配置，或换用 camoufox / patchright / playwright 引擎。"
            )

        target = merge_query_params(url, params)
        page_timeout = timeout if timeout and timeout > 0 else self.browser_settings.page_load_timeout

        future: Future[Response] = self._pool.submit(
            self._render, target, headers, cookies, config, automation_script, page_timeout
        )
        # 线程池是单线程的，排队时间要算进去，否则并发请求会误判超时
        budget = page_timeout + self.browser_settings.script_timeout + 60
        try:
            return future.result(timeout=budget)
        except TimeoutError:
            future.cancel()
            raise AdapterError(f"浏览器任务超过 {budget:.0f} 秒未返回") from None

    def _render(
        self,
        url: str,
        headers: dict[str, Any] | None,
        cookies: dict[str, Any] | str | None,
        config: dict[str, Any],
        script: str | None,
        page_timeout: float,
    ) -> Response:
        """在专属线程里跑。"""
        browser = self._ensure_browser()
        tab = browser.new_tab()
        try:
            if headers:
                tab.set.headers({k: str(v) for k, v in headers.items()})
            if cookies:
                tab.set.cookies(cookies)
            blocked = config.get("block_resources", self.browser_settings.block_resources)
            if blocked:
                # DrissionPage 只能按 URL 模式拦，没有 Playwright 那种 resource_type。
                # 用后缀近似——拦不全，但图片/字体这类大头能挡住。
                tab.set.blocked_urls(_blocked_patterns(blocked))

            # 主文档那一条请求，用来取状态码与响应头
            tab.listen.start(targets=url, method="GET")
            ok = tab.get(url, timeout=page_timeout, retry=0)
            packet = tab.listen.wait(timeout=max(1.0, page_timeout), raise_err=False)
            tab.listen.stop()

            if not ok and packet is None:
                raise AdapterError(f"DrissionPage 打开 {url} 失败")

            status = int(getattr(packet.response, "status", 0) or 0) if packet else 0
            resp_headers = dict(packet.response.headers or {}) if packet else {}

            selector = config.get("wait_for_selector")
            if selector:
                tab.wait.ele_displayed(str(selector), timeout=page_timeout)
            if config.get("scroll_to_bottom"):
                _scroll_to_bottom(tab)
            wait_ms = int(float(config.get("wait_for_timeout") or 0))
            if wait_ms > 0:
                tab.wait(wait_ms / 1000)

            script_result = tab.run_js(script) if script else None

            if config.get("screenshot"):
                body = tab.get_screenshot(as_bytes="png", full_page=True)
                resp_headers["content-type"] = "image/png"
                text = ""
            else:
                text = tab.html
                body = text.encode("utf-8", errors="replace")

            if script_result is not None:
                resp_headers["x-ipclick-script-result"] = jsonlib.dumps(script_result, ensure_ascii=False, default=str)

            return Response(
                url=tab.url,
                status_code=status if status else -1,
                content=body,
                text=text,
                headers=resp_headers,
                raw_response=None,
            )
        finally:
            # tab 之间共享 profile，用完清掉再关，免得下一个调用方捡到上一个的
            # 登录态。这不如 Playwright 的 context 隔离干净，但比什么都不做好。
            try:
                tab.set.cookies.clear()
            except Exception as e:
                log.debug(f"清理 DrissionPage cookie 失败: {e}")
            try:
                tab.close()
            except Exception as e:
                log.debug(f"关闭 DrissionPage tab 失败: {e}")

    @override
    def close(self) -> None:
        """关闭浏览器与工作线程。可重复调用。"""
        self._closed = True
        browser = self._browser
        self._browser = None
        if browser is not None:
            try:
                self._pool.submit(browser.quit).result(timeout=_SHUTDOWN_TIMEOUT)
            except Exception as e:
                log.warning(f"关闭 DrissionPage 浏览器失败: {e}")
        self._pool.shutdown(wait=False)


#: 资源类型 -> 用来拦截的 URL 后缀。DrissionPage 没有 resource_type 概念，
#: 只能按 URL 近似匹配，因此拦不全（比如没有扩展名的图片接口）。
_RESOURCE_PATTERNS: dict[str, tuple[str, ...]] = {
    "image": ("*.png", "*.jpg", "*.jpeg", "*.gif", "*.webp", "*.svg", "*.ico", "*.bmp"),
    "media": ("*.mp4", "*.webm", "*.ogg", "*.mp3", "*.wav", "*.m4a", "*.avi", "*.mov"),
    "font": ("*.woff", "*.woff2", "*.ttf", "*.otf", "*.eot"),
    "stylesheet": ("*.css",),
    "script": ("*.js",),
}


def _blocked_patterns(resources: tuple[str, ...] | list[str]) -> list[str]:
    patterns: list[str] = []
    for name in resources:
        patterns.extend(_RESOURCE_PATTERNS.get(str(name).lower(), ()))
    return patterns


def _scroll_to_bottom(tab: Any) -> None:
    """滚到底触发懒加载。高度不再变化就停，同时设轮次上限——
    无限滚动的页面永远不会"到底"。"""
    previous = -1
    for _ in range(20):
        height = tab.run_js("return document.body ? document.body.scrollHeight : 0")
        if height == previous:
            return
        previous = height
        tab.run_js("window.scrollTo(0, document.body.scrollHeight)")
        tab.wait(0.3)


def is_available() -> bool:
    return DRISSIONPAGE_AVAILABLE


__all__ = ["DRISSIONPAGE_AVAILABLE", "DrissionPageAdapter", "is_available"]
