from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
import json as jsonlib
import threading
from typing import Any

from typing_extensions import override

from ipclick.adapters.base import DownloaderAdapter, raise_if_script_error
from ipclick.adapters.browser_settings import BrowserSettings
from ipclick.adapters.retry import retry
from ipclick.adapters.settings import AdapterSettings
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
    adapter_name: str = "DrissionPage"

    def __init__(
        self,
        settings: AdapterSettings | None = None,
        browser_settings: BrowserSettings | None = None,
    ):
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
        automation_config: str | None = None,
        automation_script: str | None = None,
        allowed_status_codes: list[int] | None = None,
        kwargs: str | None = None,
    ) -> Response:
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

        config = self._parse_automation_config(automation_config)
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

        target = merge_query_params(url, params)
        page_timeout = timeout if timeout and timeout > 0 else self.browser_settings.page_load_timeout

        future: Future[Response] = self._pool.submit(
            self._render, target, headers, cookies, config, automation_script, page_timeout
        )
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
        browser = self._ensure_browser()
        tab = browser.new_tab()
        try:
            if headers:
                tab.set.headers({k: str(v) for k, v in headers.items()})
            if cookies:
                tab.set.cookies(cookies)
            blocked = config.get("block_resources", self.browser_settings.block_resources)
            if blocked:
                tab.set.blocked_urls(_blocked_patterns(blocked))

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
        height = tab.run_js("return document.body ? document.body.scrollHeight : 0")
        if height == previous:
            return
        previous = height
        tab.run_js("window.scrollTo(0, document.body.scrollHeight)")
        tab.wait(0.3)


def is_available() -> bool:
    from ipclick.utils import module_probe

    return module_probe.installed(DRISSIONPAGE_MODULE)


def __getattr__(name: str) -> Any:
    if name == "DRISSIONPAGE_AVAILABLE":
        return is_available()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["DRISSIONPAGE_MODULE", "DrissionPageAdapter", "is_available"]
