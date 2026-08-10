"""浏览器渲染适配器（Playwright 系）。

覆盖三个引擎——``playwright`` / ``patchright`` / ``camoufox``。它们拉起浏览器的
方式不同（见 :mod:`ipclick.adapters.browser_engines`），但拿到的都是同一种
``playwright.async_api.Browser``，所以下面这套线程模型、上下文隔离、资源拦截
完全共用。DrissionPage 是另一套 API，单独在
:mod:`ipclick.adapters.drission_adapter`。

**它解决的问题**：curl_cffi / httpx / requests 拿到的是服务端返回的原始 HTML。
对于内容全靠 JS 渲染出来的页面，那份 HTML 里什么都没有。这里起一个真正的浏览器
把页面跑完，返回渲染后的 DOM。

**代价**：一次请求几百毫秒到几秒，每个页面几十上百 MB 内存。能用 HTTP 适配器
解决的就别用它。

线程模型
--------
Playwright 的同步 API 绑定创建它的线程，而 gRPC 服务端的请求落在线程池里的任意
线程上——直接用同步 API 必然出问题。这里改用异步 API，跑在一个专属线程的事件
循环上：所有请求都跨线程投递到那个循环，浏览器实例始终只被一个线程碰。
顺带的好处是多个页面能在同一个循环里并发渲染，而不是被串行化。

安全
----
``automation_script`` 默认**禁用**（``[BROWSER].allow_scripts``）。页面里的 JS
能自己发请求，服务端那套 URL 策略（禁云元数据、禁内网）对它完全不起作用——放开
它等于把 SSRF 防线让开一整条。另外，渲染本身就会加载页面引用的子资源，同样不经过
URL 策略；介意的话把 ``block_resources`` 配严一些，并打开 URL 策略的内网拦截。
"""

from __future__ import annotations

import asyncio
from concurrent.futures import Future
from dataclasses import dataclass
import json as jsonlib
import threading
from typing import Any

from typing_extensions import override

from ipclick.adapters import browser_engines
from ipclick.adapters.base import DownloaderAdapter, retry
from ipclick.adapters.browser_settings import BLOCKABLE_RESOURCES, WAIT_UNTIL_CHOICES, BrowserSettings
from ipclick.adapters.settings import AdapterSettings
from ipclick.dto.response import Response
from ipclick.exceptions import AdapterError, ValidationError
from ipclick.utils.log_util import log
from ipclick.utils.url_util import merge_query_params


#: 浏览器导航只能是 GET。其余方法请走 HTTP 适配器。
_SUPPORTED_METHODS = frozenset({"GET"})

#: 等待浏览器优雅关闭的上限（秒）。超时就放弃，不能让 close() 卡住调用方。
_SHUTDOWN_TIMEOUT = 30.0


class _BrowserWorker:
    """拥有事件循环与浏览器实例的专属线程。

    浏览器是懒启动的：只有第一个请求真正到来才付启动代价，导入这个模块或者
    实例化适配器都不会拉起一个进程。
    """

    def __init__(self, settings: BrowserSettings, engine: str):
        self._settings: BrowserSettings = settings
        self._engine: str = engine
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._start_lock: threading.Lock = threading.Lock()

        # 下面这些只在事件循环线程里访问
        self._playwright: Any = None
        self._browser: Any = None
        self._browser_lock: asyncio.Lock | None = None
        self._semaphore: asyncio.Semaphore | None = None

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        loop = self._loop
        if loop is not None:
            return loop

        with self._start_lock:
            if self._loop is not None:
                return self._loop

            ready = threading.Event()
            created: dict[str, asyncio.AbstractEventLoop] = {}

            def run() -> None:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                created["loop"] = loop
                ready.set()
                loop.run_forever()

            self._thread = threading.Thread(target=run, name=f"ipclick-{self._engine}", daemon=True)
            self._thread.start()
            ready.wait()
            self._loop = created["loop"]
            return self._loop

    async def _ensure_browser(self) -> Any:
        """在事件循环线程里启动浏览器（只启动一次）。"""
        if self._browser_lock is None:
            self._browser_lock = asyncio.Lock()

        async with self._browser_lock:
            if self._browser is not None:
                return self._browser

            launched = await browser_engines.launch(self._engine, self._settings)
            self._playwright = launched.driver
            self._browser = launched.browser
            self._semaphore = asyncio.Semaphore(self._settings.max_pages)
            return self._browser

    def run(self, make_coro: Any, timeout: float) -> Any:
        """把一个协程投递到浏览器线程执行，并等待结果。"""
        loop = self._ensure_loop()
        future: Future[Any] = asyncio.run_coroutine_threadsafe(make_coro(), loop)
        try:
            return future.result(timeout=timeout)
        except TimeoutError:
            future.cancel()
            raise AdapterError(f"浏览器任务超过 {timeout} 秒未返回") from None

    def close(self) -> None:
        """关闭浏览器并停掉事件循环。可重复调用。"""
        loop = self._loop
        if loop is None:
            return

        async def shutdown() -> None:
            if self._browser is not None:
                await self._browser.close()
                self._browser = None
            if self._playwright is not None:
                await self._playwright.stop()
                self._playwright = None

        try:
            asyncio.run_coroutine_threadsafe(shutdown(), loop).result(timeout=_SHUTDOWN_TIMEOUT)
        except Exception as e:
            log.warning(f"关闭 {self._engine} 浏览器失败: {e}")

        loop.call_soon_threadsafe(loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=_SHUTDOWN_TIMEOUT)
        self._loop = None
        self._thread = None
        self._browser_lock = None
        self._semaphore = None

    # ------------------------------------------------------------------ #
    # 渲染
    # ------------------------------------------------------------------ #

    async def render(self, plan: _RenderPlan) -> Response:
        browser = await self._ensure_browser()
        assert self._semaphore is not None

        async with self._semaphore:
            # 每个请求一个全新的 context：cookie、localStorage、缓存彼此隔离。
            # 共用 context 会让上一个调用方的登录态泄漏给下一个。
            context = await browser.new_context(**plan.context_options)
            try:
                return await self._render_in_context(context, plan)
            finally:
                await context.close()

    async def _render_in_context(self, context: Any, plan: _RenderPlan) -> Response:
        if plan.cookies:
            await context.add_cookies(plan.cookies)

        if plan.block_resources:
            blocked = set(plan.block_resources)

            async def route_handler(route: Any) -> None:
                if route.request.resource_type in blocked:
                    await route.abort()
                else:
                    await route.continue_()

            await context.route("**/*", route_handler)

        page = await context.new_page()
        response = await page.goto(plan.url, wait_until=plan.wait_until, timeout=plan.page_timeout_ms)
        if response is None:
            raise AdapterError(f"浏览器没有为 {plan.url} 产生任何响应（可能是下载或 about: 跳转）")

        if plan.wait_for_selector:
            await page.wait_for_selector(plan.wait_for_selector, timeout=plan.page_timeout_ms)
        if plan.scroll_to_bottom:
            await self._scroll_to_bottom(page)
        if plan.wait_for_timeout_ms:
            await page.wait_for_timeout(plan.wait_for_timeout_ms)

        script_result: Any = None
        if plan.script:
            script_result = await asyncio.wait_for(page.evaluate(plan.script), timeout=plan.script_timeout)

        headers = dict(await response.all_headers())
        if plan.screenshot:
            body = await page.screenshot(full_page=True, type="png")
            headers["content-type"] = "image/png"
            text = ""
        else:
            text = await page.content()
            body = text.encode("utf-8", errors="replace")

        if script_result is not None:
            # 脚本返回值不适合塞进响应体（那是页面内容），用一个自定义头带出去
            headers["x-ipclick-script-result"] = jsonlib.dumps(script_result, ensure_ascii=False, default=str)

        return Response(
            url=page.url,
            status_code=response.status,
            content=body,
            text=text,
            headers=headers,
            raw_response=None,
        )

    @staticmethod
    async def _scroll_to_bottom(page: Any) -> None:
        """滚到底以触发懒加载。

        高度不再变化就停，同时设一个轮次上限——无限滚动的页面永远不会"到底"。
        """
        previous = -1
        for _ in range(20):
            height = await page.evaluate("() => document.body ? document.body.scrollHeight : 0")
            if height == previous:
                return
            previous = height
            await page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(300)


@dataclass(frozen=True)
class _RenderPlan:
    """一次渲染需要的全部参数。

    单独一个对象是为了让参数解析（在调用线程上做，出错立刻抛）和真正的渲染
    （在浏览器线程上做）彻底分开。
    """

    url: str
    context_options: dict[str, Any]
    cookies: list[dict[str, Any]]
    block_resources: tuple[str, ...]
    wait_until: str
    page_timeout_ms: int
    script_timeout: float
    wait_for_selector: str | None = None
    wait_for_timeout_ms: int = 0
    scroll_to_bottom: bool = False
    screenshot: bool = False
    script: str | None = None


class BrowserAdapter(DownloaderAdapter):
    """走 Playwright API 的浏览器渲染适配器（playwright / patchright / camoufox）。

    三个引擎的差别只在"怎么把浏览器拉起来"，见
    :mod:`ipclick.adapters.browser_engines`；拉起来之后拿到的都是同一种
    ``playwright.async_api.Browser``，线程模型、上下文隔离、资源拦截全部共用。

    与 HTTP 适配器的差异（都是浏览器本身的限制，不是没写）：

    - 只支持 GET。浏览器导航就是 GET，其余方法请走 curl_cffi / httpx / requests。
    - 不支持 ``allow_redirects=False``。浏览器总是跟随重定向。
    - ``stream=True`` 无意义：渲染必须等页面跑完才有结果。
    """

    adapter_name: str = "browser"

    #: 子类固定用哪个引擎；None 表示听 [BROWSER].engine 的（含平台默认）
    engine: str | None = None

    def __init__(
        self,
        settings: AdapterSettings | None = None,
        browser_settings: BrowserSettings | None = None,
    ):
        resolved = browser_settings or BrowserSettings()
        if not resolved.enabled:
            raise AdapterError("浏览器渲染已被关闭（[BROWSER].enabled = false）")

        engine = self.engine or browser_engines.resolve_engine(resolved.engine)
        if engine not in browser_engines.PLAYWRIGHT_FAMILY:
            # 只有 registry 绕过 get_adapter 直接实例化才会走到这里
            raise AdapterError(f"{type(self).__name__} 不支持引擎 {engine!r}")
        if not browser_engines.is_available(engine):
            raise AdapterError(f"浏览器引擎 {engine!r} 不可用：{browser_engines.INSTALL_HINTS.get(engine, '缺少依赖')}")

        super().__init__(settings)
        self.browser_settings: BrowserSettings = resolved
        self.resolved_engine: str = engine
        self._worker: _BrowserWorker = _BrowserWorker(self.browser_settings, engine)

    # ------------------------------------------------------------------ #
    # 参数解析
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

    def _build_plan(
        self,
        url: str,
        *,
        headers: dict[str, Any] | None,
        cookies: dict[str, Any] | str | None,
        params: dict[str, Any] | None,
        proxy: str | None,
        timeout: float,
        verify: bool,
        automation_config: str | None,
        automation_script: str | None,
    ) -> _RenderPlan:
        s = self.browser_settings
        config = self._parse_automation_config(automation_config)

        wait_until = str(config.get("wait_until", s.wait_until)).strip().lower()
        if wait_until not in WAIT_UNTIL_CHOICES:
            raise ValidationError(f"wait_until 只能是 {sorted(WAIT_UNTIL_CHOICES)} 之一，收到 {wait_until!r}")

        if automation_script and not s.allow_scripts:
            raise ValidationError(
                "服务端未开启 automation_script（[BROWSER].allow_scripts = false）。"
                "页面内 JS 可绕过 URL 安全策略访问内网，请确认调用方可信后再开启。"
            )

        blocked = config.get("block_resources")
        if blocked is None:
            block_resources = s.block_resources
        elif isinstance(blocked, (list, tuple)):
            block_resources = tuple(str(b).strip().lower() for b in blocked)
            unknown = set(block_resources) - BLOCKABLE_RESOURCES
            if unknown:
                raise ValidationError(f"block_resources 含未知资源类型: {sorted(unknown)}")
        else:
            raise ValidationError("block_resources 必须是数组")

        context_options: dict[str, Any] = {"ignore_https_errors": not verify}
        if self.resolved_engine in browser_engines.FINGERPRINT_MANAGED:
            # camoufox 这类引擎自己生成一整套自洽的指纹（UA、屏幕、字体、WebGL…）。
            # 在 context 上再盖一层 viewport / User-Agent 只会和它给出的指纹自相
            # 矛盾——navigator.userAgent 说 Firefox、屏幕尺寸却对不上——反而比不
            # 伪装更容易被识别。要改就去 [BROWSER] 里配 locale 等它认识的项。
            if s.user_agent:
                log.debug(f"{self.resolved_engine} 自带指纹伪装，忽略 [BROWSER].user_agent")
        else:
            context_options["viewport"] = s.viewport
            context_options["user_agent"] = s.user_agent or self._get_user_agent()
        if headers:
            context_options["extra_http_headers"] = {k: str(v) for k, v in headers.items()}

        effective_proxy = proxy or s.proxy_gateway
        if effective_proxy:
            proxy_option: dict[str, Any] = {"server": effective_proxy}
            if s.proxy_bypass:
                proxy_option["bypass"] = ",".join(s.proxy_bypass)
            context_options["proxy"] = proxy_option

        page_timeout = timeout if timeout and timeout > 0 else s.page_load_timeout

        return _RenderPlan(
            url=merge_query_params(url, params),
            context_options=context_options,
            cookies=_normalize_cookies(cookies, url),
            block_resources=block_resources,
            wait_until=wait_until,
            page_timeout_ms=int(page_timeout * 1000),
            wait_for_selector=(str(config["wait_for_selector"]) if config.get("wait_for_selector") else None),
            wait_for_timeout_ms=max(0, int(float(config.get("wait_for_timeout") or 0))),
            scroll_to_bottom=bool(config.get("scroll_to_bottom")),
            screenshot=bool(config.get("screenshot")),
            script=automation_script or None,
            script_timeout=s.script_timeout,
        )

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

        plan = self._build_plan(
            url,
            headers=headers,
            cookies=cookies,
            params=params,
            proxy=proxy,
            timeout=timeout,
            verify=verify,
            automation_config=automation_config,
            automation_script=automation_script,
        )

        # 给渲染留出比页面超时更宽的余量：启动浏览器、排队等页面额度都算在里面。
        budget = plan.page_timeout_ms / 1000 + self.browser_settings.script_timeout + 60
        try:
            return self._worker.run(lambda: self._worker.render(plan), timeout=budget)
        except AdapterError:
            raise
        except Exception as e:
            log.warning(f"playwright render failed for {url}: {e}")
            raise

    @override
    def close(self) -> None:
        """关闭浏览器与事件循环线程。"""
        self._worker.close()


class PlaywrightAdapter(BrowserAdapter):
    """原版 Playwright。最稳、行为最可预期，没有反检测处理。"""

    adapter_name: str = "playwright"
    engine: str | None = "playwright"


class PatchrightAdapter(BrowserAdapter):
    """Playwright 的反检测分支，API 完全兼容。需要 ``patchright install chromium``。"""

    adapter_name: str = "patchright"
    engine: str | None = "patchright"


class CamoufoxAdapter(BrowserAdapter):
    """基于 Firefox 的反检测浏览器，自带指纹伪装。需要 ``python -m camoufox fetch``。"""

    adapter_name: str = "camoufox"
    engine: str | None = "camoufox"


def _normalize_cookies(cookies: dict[str, Any] | str | None, url: str) -> list[dict[str, Any]]:
    """把 dict / ``a=1; b=2`` 字符串转成 playwright 要的 cookie 列表。"""
    if not cookies:
        return []

    if isinstance(cookies, str):
        pairs: dict[str, Any] = {}
        for item in cookies.split(";"):
            name, sep, value = item.partition("=")
            if sep and name.strip():
                pairs[name.strip()] = value.strip()
    else:
        pairs = cookies

    return [{"name": str(k), "value": str(v), "url": url} for k, v in pairs.items()]


#: 引擎名 -> 适配器类。registry 据此按需注册。
ENGINE_ADAPTERS: dict[str, type[BrowserAdapter]] = {
    "playwright": PlaywrightAdapter,
    "patchright": PatchrightAdapter,
    "camoufox": CamoufoxAdapter,
}


__all__ = [
    "ENGINE_ADAPTERS",
    "BrowserAdapter",
    "CamoufoxAdapter",
    "PatchrightAdapter",
    "PlaywrightAdapter",
]
