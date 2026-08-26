"""基于 DrissionPage 的串行浏览器渲染适配器。"""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
import json as jsonlib
import threading
from typing import Any

from typing_extensions import override

from ipclick.adapters.base import (
    DownloaderAdapter,
    mark_utf8_charset,
    raise_if_script_error,
    reject_disallowed_urls,
)
from ipclick.adapters.browser_settings import (
    BLOCKABLE_RESOURCES,
    DOCUMENT_HEIGHT_JS,
    SCROLL_TO_BOTTOM_JS,
    BrowserSettings,
    parse_automation_config,
    wait_for_timeout_ms,
)
from ipclick.adapters.retry import retry
from ipclick.adapters.settings import DEFAULT_DOWNLOAD_TIMEOUT, AdapterSettings
from ipclick.dto.response import Response
from ipclick.exceptions import AdapterError, ValidationError
from ipclick.utils.log_util import log
from ipclick.utils.url_util import merge_query_params


_UNPROBED: Any = object()
_Chromium: Any = _UNPROBED
_ChromiumOptions: Any = _UNPROBED

DRISSIONPAGE_MODULE = "DrissionPage"


def _load_drission() -> tuple[Any, Any]:
    global _Chromium, _ChromiumOptions
    if _Chromium is _UNPROBED:
        try:
            from DrissionPage import Chromium, ChromiumOptions

            _Chromium, _ChromiumOptions = Chromium, ChromiumOptions
        except ImportError:
            _Chromium, _ChromiumOptions = None, None
    return _Chromium, _ChromiumOptions


_SUPPORTED_METHODS = frozenset({"GET"})

_SHUTDOWN_TIMEOUT = 30.0


class DrissionPageAdapter(DownloaderAdapter):
    """在单工作线程中复用一个 DrissionPage 浏览器进程。"""

    adapter_name: str = "DrissionPage"

    def __init__(
        self,
        settings: AdapterSettings | None = None,
        browser_settings: BrowserSettings | None = None,
    ):
        """校验依赖与配置并创建专用串行线程池。"""
        if _load_drission()[0] is None:
            raise AdapterError('DrissionPage is not installed. Install it with: pip install "ipclick[drissionpage]"')

        resolved = browser_settings or BrowserSettings()
        if not resolved.enabled:
            raise AdapterError("浏览器渲染已被关闭（[BROWSER].enabled = false）")

        super().__init__(settings)
        self.browser_settings: BrowserSettings = resolved
        self.resolved_engine: str = "drissionpage"

        self._browser: Any = None
        self._browser_lock: threading.Lock = threading.Lock()
        self._pool: ThreadPoolExecutor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ipclick-drissionpage")
        self._closed: bool = False
        # 超时后被放弃、但仍在占着那个唯一工作线程的渲染任务数。
        self._abandoned: int = 0

    def _options(self) -> Any:
        s = self.browser_settings
        options = _load_drission()[1]()
        if s.executable_path:
            options.set_browser_path(s.executable_path)
        options.headless(s.headless)
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
                    self._browser = _load_drission()[0](self._options())
                except Exception as e:
                    raise AdapterError(f"浏览器启动失败（DrissionPage）：{e}") from e
                log.info(f"DrissionPage 浏览器已启动：headless={self.browser_settings.headless}")
            return self._browser

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
        timeout: float = DEFAULT_DOWNLOAD_TIMEOUT,
        max_retries: int = 3,
        retry_delay: float = 2.0,
        verify: bool = True,
        allow_redirects: bool = True,
        stream: bool = False,
        impersonate: str | None = None,
        automation_config: str | None = None,
        automation_script: str | None = None,
        allowed_status_codes: list[int] | None = None,
        kwargs: str | None = None,
    ) -> Response:
        """校验浏览器约束，并在线程池中执行一次 GET 渲染。"""
        self.reject_impersonate(impersonate)
        method = method.upper()
        if method not in _SUPPORTED_METHODS:
            raise ValidationError(
                f"浏览器渲染只支持 GET，收到 {method}。"
                "浏览器导航本身就是 GET，需要其他方法请改用 curl_cffi / niquests 适配器。"
            )
        if not allow_redirects:
            raise ValidationError("浏览器渲染无法禁用重定向。需要看原始 3xx 响应请改用 curl_cffi / niquests 适配器。")
        if data is not None or json is not None or files is not None:
            raise ValidationError("浏览器渲染不能携带请求体（data / json / files），请改用 HTTP 适配器。")

        config = parse_automation_config(automation_config)
        if automation_script and not self.browser_settings.allow_scripts:
            raise ValidationError(
                "服务端未开启 automation_script（[BROWSER].allow_scripts = false）。"
                "页面内 JS 可绕过 URL 安全策略访问内网，请确认调用方可信后再开启。"
            )
        if proxy and proxy != self.browser_settings.proxy_gateway:
            raise ValidationError(
                "DrissionPage 的代理是浏览器进程级的，不支持按请求指定。"
                "请改用 [BROWSER.proxy].gateway 全局配置，或换用 camoufox / patchright / playwright 引擎。"
            )
        if not verify:
            # 之前是静默忽略：verify=False 照样按校验证书执行，请求自签名站点仍然失败，
            # 而调用方明明传了参数、从结果里看不出它没生效。证书校验对 DrissionPage
            # 是浏览器启动开关（同 proxy 一样进程级），按请求切不了，所以明确报错。
            raise ValidationError(
                "DrissionPage 不支持按请求关闭证书校验（verify=False）：那是浏览器进程级的启动开关。"
                "需要跳过证书校验请换用 camoufox / patchright / playwright 引擎——"
                "它们按 context 设置 ignore_https_errors，可以按请求生效。"
            )

        target = merge_query_params(url, params)
        page_timeout = timeout if timeout and timeout > 0 else self.browser_settings.page_load_timeout

        budget = page_timeout + self.browser_settings.script_timeout + 60

        # 上一次超时的渲染可能还在占着那个唯一的工作线程。此时排队进去只会白等一整个
        # budget 再报同样的超时——每个后续请求都付一次这个代价。直接快速失败，
        # 并说清原因和处理方向。
        with self._browser_lock:
            stuck = self._abandoned
        if stuck:
            raise AdapterError(
                f"DrissionPage 的渲染线程仍被 {stuck} 个已超时的任务占用（它只有一个工作线程，"
                f"且超时后无法真正中断）。请等它自行结束，或重启服务；"
                f"需要并发渲染请换用 playwright / patchright / camoufox 引擎"
            )

        # DrissionPage 对象不是线程安全的，所有浏览器操作固定在同一工作线程。
        future: Future[Response] = self._pool.submit(
            self._render, target, headers, cookies, config, automation_script, page_timeout
        )
        try:
            return future.result(timeout=budget)
        except TimeoutError:
            # cancel() 对已经在跑的任务是空操作——它会继续占着工作线程直到自己结束。
            # 这里只能如实记账，并在它结束时把账销掉。
            if not future.cancel():
                with self._browser_lock:
                    self._abandoned += 1
                future.add_done_callback(self._release_abandoned)
            raise AdapterError(f"浏览器任务超过 {budget:.0f} 秒未返回") from None

    def _release_abandoned(self, _future: Future[Response]) -> None:
        """被放弃的渲染任务终于结束了，把占用计数销掉。"""
        with self._browser_lock:
            self._abandoned = max(0, self._abandoned - 1)

    def _render(
        self,
        url: str,
        headers: dict[str, Any] | None,
        cookies: dict[str, Any] | str | None,
        config: dict[str, Any],
        script: str | None,
        page_timeout: float,
    ) -> Response:
        browser = self._ensure_browser()
        tab = browser.new_tab()
        try:
            if headers:
                tab.set.headers({k: str(v) for k, v in headers.items()})
            if cookies:
                tab.set.cookies(cookies)
            blocked = config.get("block_resources", self.browser_settings.block_resources)
            if not isinstance(blocked, (list, tuple)):
                raise ValidationError("block_resources 必须是数组")
            unknown = {str(name).strip().lower() for name in blocked} - BLOCKABLE_RESOURCES
            if unknown:
                raise ValidationError(f"block_resources 含未知资源类型: {sorted(unknown)}")
            if blocked:
                tab.set.blocked_urls(_blocked_patterns(blocked))

            tab.listen.start(targets=url, method="GET")
            ok = tab.get(url, timeout=page_timeout, retry=0)
            packet = tab.listen.wait(timeout=max(1.0, page_timeout), raise_err=False)
            tab.listen.stop()

            # 用 not packet 而不是 packet is None：listen.wait() 超时返回的是 False，
            # 写成 `packet is None` 这个分支就永远进不来，于是打不开的页面会被当成
            # 一条 status=0 的"正常响应"返回，调用方看不出请求根本没成功。
            if not ok and not packet:
                raise AdapterError(f"DrissionPage 打开 {url} 失败（页面未加载，且未捕获到响应）")

            # 网络级失败（域名解析不了、端口关闭、证书被拒）时 packet 在、
            # packet.response 是 None。直接取 .headers 会抛 AttributeError，
            # 被上层兜成 "'NoneType' object has no attribute ..."，
            # 把真实的失败原因替换成一句看不懂的内部错误。
            response = getattr(packet, "response", None) if packet else None
            if not ok and response is None:
                raise AdapterError(f"DrissionPage 打开 {url} 失败（未收到响应，多为 DNS / 连接 / 证书问题）")

            status = int(getattr(response, "status", 0) or 0) if response is not None else 0
            resp_headers = dict(getattr(response, "headers", None) or {}) if response is not None else {}

            # 逐跳重定向校验此前只覆盖 curl_cffi / niquests / browser 三条路，这一条
            # 一次都不校验：url_validator 在本文件里根本没被读过。于是
            # 302 -> http://169.254.169.254/ 会被 chromium 跟到底，云元数据连同
            # tab.url 一起交回调用方，而 SSRF 准入只看过入口 URL。
            # 必须在读 tab.html / 跑脚本之前拦下来。
            reject_disallowed_urls(
                self.url_validator,
                url,
                (str(tab.url or ""), str(getattr(packet, "url", "") or "") if packet else ""),
            )

            selector = config.get("wait_for_selector")
            if selector:
                tab.wait.ele_displayed(str(selector), timeout=page_timeout)
            if config.get("scroll_to_bottom"):
                _scroll_to_bottom(tab)
            wait_ms = wait_for_timeout_ms(config)
            if wait_ms > 0:
                tab.wait(wait_ms / 1000)

            try:
                script_result = tab.run_js(script) if script else None
            except Exception as e:
                raise_if_script_error(e, script)
                raise

            if config.get("screenshot"):
                body = tab.get_screenshot(as_bytes="png", full_page=True)
                resp_headers["content-type"] = "image/png"
                text = ""
            else:
                text = tab.html
                body = text.encode("utf-8", errors="replace")
                mark_utf8_charset(resp_headers)

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
        """停止浏览器并关闭专用线程池；可重复调用。"""
        self._closed = True
        browser = self._browser
        self._browser = None
        if browser is not None:
            try:
                self._pool.submit(browser.quit).result(timeout=_SHUTDOWN_TIMEOUT)
            except Exception as e:
                log.warning(f"关闭 DrissionPage 浏览器失败: {e}")
        self._pool.shutdown(wait=False)


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
    previous = -1
    for _ in range(20):
        height = tab.run_js(f"return {DOCUMENT_HEIGHT_JS}")
        if height == previous:
            return
        previous = height
        # 和上一行同样要防 document.body 为空：XML / 纯 SVG / text-plain 文档没有 body，
        # 少了这层保护会抛 TypeError，被当成用户脚本错误报成 ValidationError。
        # 上一行的高度读取已经带守卫，于是无 body 的文档 height=0 != previous=-1，
        # 每次都会走到这里。
        tab.run_js(SCROLL_TO_BOTTOM_JS)
        tab.wait(0.3)


def is_available() -> bool:
    """返回 DrissionPage 模块当前是否可导入。"""
    from ipclick.utils import module_probe

    return module_probe.installed(DRISSIONPAGE_MODULE)


def __getattr__(name: str) -> Any:
    if name == "DRISSIONPAGE_AVAILABLE":
        return is_available()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["DRISSIONPAGE_MODULE", "DrissionPageAdapter", "is_available"]
