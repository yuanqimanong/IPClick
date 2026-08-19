"""浏览器渲染适配器（Playwright 系）。

覆盖三个引擎——``playwright`` / ``patchright`` / ``camoufox``。它们拉起浏览器的
方式不同（见 :mod:`ipclick.adapters.browser_engines`），但拿到的都是同一种
``playwright.async_api.Browser``，所以下面这套线程模型、上下文隔离、资源拦截
完全共用。DrissionPage 是另一套 API，单独在
:mod:`ipclick.adapters.drission_adapter`。

**它解决的问题**：curl_cffi / niquests 拿到的是服务端返回的原始 HTML。
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
from ipclick.adapters.base import (
    DownloaderAdapter,
    normalize_js,
    raise_if_permanent_navigation_error,
    raise_if_script_error,
    retry,
)
from ipclick.adapters.browser_settings import (
    BLOCKABLE_RESOURCES,
    WAIT_UNTIL_CHOICES,
    BrowserSettings,
    resolve_max_pages,
)
from ipclick.adapters.settings import AdapterSettings
from ipclick.dto.response import Response
from ipclick.exceptions import AdapterError, ValidationError
from ipclick.utils.log_util import log
from ipclick.utils.url_util import merge_query_params


_SUPPORTED_METHODS = frozenset({"GET"})

_SHUTDOWN_TIMEOUT = 30.0

_COLD_START_ALLOWANCE = 60.0

_OVERHEAD_ALLOWANCE = 15.0

_MAX_WAIT_FOR_TIMEOUT_MS = 60_000


class _BrowserWorker:
    """拥有事件循环与浏览器实例的专属线程。

    浏览器是懒启动的：只有第一个请求真正到来才付启动代价，导入这个模块或者
    实例化适配器都不会拉起一个进程。
    """

    def __init__(self, settings: BrowserSettings, engine: str):
        self._settings: BrowserSettings = settings
        self._engine: str = engine
        self._resolved_pages: int = 0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._start_lock: threading.Lock = threading.Lock()

        self._playwright: Any = None
        self._browser: Any = None
        self._browser_lock: asyncio.Lock | None = None
        self._semaphore: asyncio.Semaphore | None = None

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
        """在事件循环线程里拿到一个**活着的**浏览器，必要时重建。

        以前这里只判 ``is not None``，于是浏览器进程一旦死掉（小内存机器上被
        OOM killer 干掉是常事，日志里会出现 "Network service crashed"），这个节点
        之后每个浏览器请求都必败，而且要走满重试才返回——运维现象是"browser
        适配器突然全挂，重启进程才好"。

        判定用 ``is_connected()``：它查的是 CDP/WebSocket 连接还在不在，
        进程被杀之后这个连接必然断，比自己去 poll 进程状态可靠。
        """
        if self._browser_lock is None:
            self._browser_lock = asyncio.Lock()

        async with self._browser_lock:
            if self._browser is not None:
                if self._is_alive(self._browser):
                    return self._browser
                log.warning(f"{self._engine} 浏览器进程已失联（可能被 OOM killer 杀掉），正在重建")
                await self._discard_browser()

            launched = await browser_engines.launch(self._engine, self._settings)
            self._playwright = launched.driver
            self._browser = launched.browser
            limit = resolve_max_pages(self._settings.max_pages, self._engine)
            if limit != self._resolved_pages:
                self._resolved_pages = limit
                log.info(f"{self._engine} 页面并发上限：{limit}（max_pages={self._settings.max_pages or 'auto'}）")
            self._semaphore = asyncio.Semaphore(limit)
            return self._browser

    @staticmethod
    def _is_alive(browser: Any) -> bool:
        """浏览器连接还在不在。探测失败时按"还活着"处理——
        误判成死掉会白白重启一个好浏览器，代价比多失败一次大。
        """
        checker = getattr(browser, "is_connected", None)
        if not callable(checker):
            return True
        try:
            return bool(checker())
        except Exception:  # pragma: no cover - 驱动自身出问题
            return False

    async def _discard_browser(self) -> None:
        """扔掉当前这套浏览器 + driver，失败也要把引用清干净。

        清不干净的后果是下次 ``_ensure_browser`` 又拿到那个死对象，
        于是永远重建不了。
        """
        browser, driver = self._browser, self._playwright
        self._browser = None
        self._playwright = None
        self._semaphore = None
        for closer, what in ((getattr(browser, "close", None), "浏览器"), (getattr(driver, "stop", None), "driver")):
            if closer is None:
                continue
            try:
                await closer()
            except Exception as e:
                log.debug(f"回收已失联的{what}时报错（忽略）：{e}")

    @property
    def browser_started(self) -> bool:
        """浏览器**当前可用**吗。用来决定要不要给这次请求留冷启动余量。

        判据必须和 :meth:`_ensure_browser` 完全一致（都用 ``is_connected``），
        否则会错在最糟的方向上：浏览器被 OOM killer 杀掉后 ``self._browser``
        仍然非 None，只看它的话这里说"已经起来了"→ 不给冷启动余量，而
        ``_ensure_browser`` 那边判定失联、正在重建 → 这次请求要付完整的冷启动
        代价却只拿到热路径的预算，必然超时。而这恰好是内存最紧张、最需要它
        成功的时候。
        """
        return self._browser is not None and self._is_alive(self._browser)

    def run(self, make_coro: Any, timeout: float) -> Any:
        """把一个协程投递到浏览器线程执行，并等待结果。"""
        loop = self._ensure_loop()
        future: Future[Any] = asyncio.run_coroutine_threadsafe(make_coro(), loop)
        try:
            return future.result(timeout=timeout)
        except TimeoutError:
            if future.done():
                raise
            future.cancel()
            raise AdapterError(f"浏览器任务超过 {timeout:.0f} 秒未返回") from None

    def close(self) -> None:
        """关闭浏览器并停掉事件循环。可重复调用。"""
        loop = self._loop
        if loop is None:
            return

        async def shutdown() -> None:
            if self._browser is not None:
                try:
                    await self._browser.close()
                except Exception as e:
                    log.debug(f"关闭 {self._engine} 浏览器时报错（继续停 driver）：{e}")
                finally:
                    self._browser = None
            if self._playwright is not None:
                try:
                    await self._playwright.stop()
                finally:
                    self._playwright = None

        try:
            asyncio.run_coroutine_threadsafe(shutdown(), loop).result(timeout=_SHUTDOWN_TIMEOUT)
        except Exception as e:
            log.warning(f"关闭 {self._engine} 浏览器失败: {e}")
        finally:
            self._browser = None
            self._playwright = None

        loop.call_soon_threadsafe(loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=_SHUTDOWN_TIMEOUT)
        self._loop = None
        self._thread = None
        self._browser_lock = None
        self._semaphore = None

    async def render(self, plan: _RenderPlan) -> Response:
        browser = await self._ensure_browser()
        semaphore = self._semaphore
        if semaphore is None:
            raise AdapterError(f"{self._engine} 浏览器未就绪（页面额度未初始化），请重试")

        async with semaphore:
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
        try:
            response = await page.goto(plan.url, wait_until=plan.wait_until, timeout=plan.page_timeout_ms)
        except Exception as e:
            raise_if_permanent_navigation_error(e)
            raise
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
            js = normalize_js(plan.script)
            try:
                script_result = await asyncio.wait_for(page.evaluate(js), timeout=plan.script_timeout)
            except Exception as e:
                raise_if_script_error(e, plan.script)
                raise

        headers = dict(await response.all_headers())
        if plan.screenshot:
            body = await page.screenshot(full_page=True, type="png")
            headers["content-type"] = "image/png"
            text = ""
        else:
            text = await page.content()
            body = text.encode("utf-8", errors="replace")

        if script_result is not None:
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

    - 只支持 GET。浏览器导航就是 GET，其余方法请走 curl_cffi / niquests。
    - 不支持 ``allow_redirects=False``。浏览器总是跟随重定向。
    - ``stream=True`` 无意义：渲染必须等页面跑完才有结果。
    """

    adapter_name: str = "browser"

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
            raise AdapterError(f"{type(self).__name__} 不支持引擎 {engine!r}")
        if not browser_engines.package_installed(engine):
            raise AdapterError(
                f"浏览器引擎 {engine!r} 的 Python 包未安装：{browser_engines.INSTALL_HINTS.get(engine, '缺少依赖')}"
            )

        super().__init__(settings)
        self.browser_settings: BrowserSettings = resolved
        self.resolved_engine: str = engine
        self._worker: _BrowserWorker = _BrowserWorker(self.browser_settings, engine)

    @staticmethod
    def _positive_number(config: dict[str, Any], key: str) -> int:
        """从 automation_config 里取一个非负数值项。

        直接 ``int(float(...))`` 的话，``{"wait_for_timeout": "abc"}`` 会抛一个
        裸的 ValueError 冒出适配器——调用方看到的是一句 Python 内部错误，而不是
        "你这个参数写错了"。JSON 里塞列表/字典还会变成 TypeError。统一转成
        ValidationError，和 automation_config 本身解析失败的处理保持一致。
        """
        raw = config.get(key)
        if raw is None or raw == "":
            return 0
        try:
            value = float(raw)
        except (TypeError, ValueError) as e:
            raise ValidationError(f"automation_config.{key} 必须是数字，收到 {raw!r}") from e
        if value != value or value == float("inf") or value == float("-inf"):
            raise ValidationError(f"automation_config.{key} 必须是有限数字，收到 {raw!r}")
        return max(0, int(value))

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

    def _budget_for(self, plan: _RenderPlan) -> float:
        """这次渲染最多给多少秒。

        以前是 ``page_timeout + script_timeout + 60`` 无条件相加，于是调用方填
        30 秒、实际单次能挂 150 秒——5 倍偏差，页面上还没有任何地方解释这个数字
        从哪来。现在按**这次请求真正会做的事**算：

        * 页面加载：总是要留。
        * 等元素出现：``wait_for_selector`` 用的也是 ``page_timeout_ms``，是**导航
          之后**的第二段等待，所以要再留一份。漏掉它的话，选择器一直等不到的
          请求会先撞上外层预算，报成"浏览器任务超过 N 秒未返回"——把排查方向从
          "这个选择器不对"引到"浏览器是不是卡了"。
        * 脚本执行：只有真带了 ``automation_script`` 才留。
        * 显式的等待：调用方自己要求的 ``wait_for_timeout`` 也得算进去。
        * 冷启动：只有浏览器还没起来时才留，起过一次之后不再付。
        """
        budget = plan.page_timeout_ms / 1000
        if plan.wait_for_selector:
            budget += plan.page_timeout_ms / 1000
        if plan.script:
            budget += plan.script_timeout
        if plan.wait_for_timeout_ms:
            budget += plan.wait_for_timeout_ms / 1000
        if not self._worker.browser_started:
            budget += _COLD_START_ALLOWANCE
        return budget + _OVERHEAD_ALLOWANCE

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
            wait_for_timeout_ms=min(_MAX_WAIT_FOR_TIMEOUT_MS, self._positive_number(config, "wait_for_timeout")),
            scroll_to_bottom=bool(config.get("scroll_to_bottom")),
            screenshot=bool(config.get("screenshot")),
            script=automation_script or None,
            script_timeout=s.script_timeout,
        )

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
        """用真实浏览器打开页面，返回渲染后的 DOM。"""
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

        budget = self._budget_for(plan)
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
