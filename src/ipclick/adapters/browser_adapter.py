"""Playwright 家族浏览器适配器及专用后台事件循环。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass
import json as jsonlib
import threading
from typing import Any

from typing_extensions import override

from ipclick.adapters import browser_engines
from ipclick.adapters.base import (
    DownloaderAdapter,
    mark_utf8_charset,
    normalize_js,
    raise_if_permanent_navigation_error,
    raise_if_script_error,
    reject_disallowed_urls,
)
from ipclick.adapters.browser_settings import (
    BLOCKABLE_RESOURCES,
    WAIT_UNTIL_CHOICES,
    BrowserSettings,
    resolve_max_pages,
)
from ipclick.adapters.retry import retry
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

NETWORK_IDLE = "networkidle"

_GOTO_FALLBACK_STATE = "load"


def _goto_state(wait_until: str) -> str:
    return _GOTO_FALLBACK_STATE if wait_until == NETWORK_IDLE else wait_until


async def _settle(page: Any, plan: _RenderPlan) -> None:
    if plan.wait_until != NETWORK_IDLE or plan.settle_timeout_ms <= 0:
        return
    budget = min(plan.settle_timeout_ms, plan.page_timeout_ms)
    try:
        await page.wait_for_load_state(NETWORK_IDLE, timeout=budget)
    except Exception as e:
        log.warning(
            f"{plan.url} 在 {budget}ms 内没有进入 networkidle（{type(e).__name__}），"
            f"按 load 时的页面内容返回。长连接页面属正常；要缩短这段等待请调 [BROWSER.timeout].settle，"
            f"要完全不等就把 [BROWSER].wait_until 设为 load"
        )


class _BrowserWorker:
    """在专用线程事件循环中复用浏览器并控制页面并发。"""

    def __init__(self, settings: BrowserSettings, engine: str):
        """保存引擎配置；线程和浏览器均按需启动。"""
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
        checker = getattr(browser, "is_connected", None)
        if not callable(checker):
            return True
        try:
            return bool(checker())
        except Exception:
            return False

    async def _discard_browser(self) -> None:
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
        """返回浏览器对象存在且仍连接。"""
        return self._browser is not None and self._is_alive(self._browser)

    def run(self, make_coro: Any, timeout: float) -> Any:
        """把协程提交到专用事件循环，并同步等待有界结果。"""
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
        """依次关闭浏览器、驱动、事件循环和工作线程。"""
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
        """获取页面额度，在隔离 context 中执行一次渲染。"""
        browser = await self._ensure_browser()
        semaphore = self._semaphore
        if semaphore is None:
            raise AdapterError(f"{self._engine} 浏览器未就绪（页面额度未初始化），请重试")

        async with semaphore:
            # 每次请求使用独立 context，避免 cookie、代理和页面状态串扰。
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
            response = await page.goto(plan.url, wait_until=_goto_state(plan.wait_until), timeout=plan.page_timeout_ms)
        except Exception as e:
            raise_if_permanent_navigation_error(e)
            raise
        if response is None:
            raise AdapterError(f"浏览器没有为 {plan.url} 产生任何响应（可能是下载或 about: 跳转）")

        _reject_disallowed_redirects(response, plan)

        await _settle(page, plan)

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
            mark_utf8_charset(headers)

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
        previous = -1
        for _ in range(20):
            height = await page.evaluate("() => document.body ? document.body.scrollHeight : 0")
            if height == previous:
                return
            previous = height
            # 与上一行同样要防 document.body 为空：XML / 纯 SVG 文档没有 body，
            # 少了这层保护会抛 TypeError，被当成用户脚本错误报成 ValidationError。
            await page.evaluate("() => { if (document.body) window.scrollTo(0, document.body.scrollHeight); }")
            await page.wait_for_timeout(300)


def _reject_disallowed_redirects(response: Any, plan: _RenderPlan) -> None:
    """走一遍 Playwright 的重定向链，交给共享校验器逐跳判定。

    ``context.route`` 处理器对重定向目标不会再次触发（重定向由浏览器网络栈内部跟随
    完），所以这里只能事后校验。语义与边界见 ``base.reject_disallowed_urls``。
    """
    if plan.url_validator is None:
        return

    chain: list[str] = []
    request = getattr(response, "request", None)
    seen = 0
    while request is not None and seen < 20:
        chain.append(str(getattr(request, "url", "") or ""))
        request = getattr(request, "redirected_from", None)
        seen += 1
    chain.append(str(getattr(response, "url", "") or ""))

    reject_disallowed_urls(plan.url_validator, plan.url, chain)


@dataclass(frozen=True)
class _RenderPlan:
    url: str
    context_options: dict[str, Any]
    cookies: list[dict[str, Any]]
    block_resources: tuple[str, ...]
    wait_until: str
    page_timeout_ms: int
    script_timeout: float
    settle_timeout_ms: int = 0
    wait_for_selector: str | None = None
    wait_for_timeout_ms: int = 0
    scroll_to_bottom: bool = False
    screenshot: bool = False
    script: str | None = None
    # 逐跳重定向校验器，由适配器从自身的 url_validator 传下来。
    url_validator: Callable[[str], None] | None = None


class BrowserAdapter(DownloaderAdapter):
    """Playwright、Patchright 与 Camoufox 的共享同步适配层。"""

    adapter_name: str = "browser"

    engine: str | None = None

    def __init__(
        self,
        settings: AdapterSettings | None = None,
        browser_settings: BrowserSettings | None = None,
    ):
        """解析具体引擎并创建惰性后台浏览器 worker。"""
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
        budget = plan.page_timeout_ms / 1000
        if plan.wait_for_selector:
            budget += plan.page_timeout_ms / 1000
        if plan.script:
            budget += plan.script_timeout
        if plan.wait_for_timeout_ms:
            budget += plan.wait_for_timeout_ms / 1000
        if plan.wait_until == NETWORK_IDLE:
            # networkidle 是默认值，导航之后还要额外等 settle 秒。不算进预算的话，
            # 长连接页面（WebSocket / SSE / 轮询）每次都会把这几秒花掉，然后被看门狗
            # 判成"浏览器任务超时"——而它其实正常返回了 load 时的内容。
            budget += plan.settle_timeout_ms / 1000
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
            settle_timeout_ms=int(s.settle_timeout * 1000),
            wait_for_selector=(str(config["wait_for_selector"]) if config.get("wait_for_selector") else None),
            wait_for_timeout_ms=min(_MAX_WAIT_FOR_TIMEOUT_MS, self._positive_number(config, "wait_for_timeout")),
            scroll_to_bottom=bool(config.get("scroll_to_bottom")),
            screenshot=bool(config.get("screenshot")),
            script=automation_script or None,
            script_timeout=s.script_timeout,
            url_validator=self.url_validator,
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
        """校验浏览器导航约束并执行一次 GET 页面渲染。"""
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
        """关闭后台浏览器及其专用事件循环。"""
        self._worker.close()


class PlaywrightAdapter(BrowserAdapter):
    """使用官方 Playwright 的浏览器适配器。"""

    adapter_name: str = "playwright"
    engine: str | None = "playwright"


class PatchrightAdapter(BrowserAdapter):
    """使用 Patchright 反检测 Chromium 的浏览器适配器。"""

    adapter_name: str = "patchright"
    engine: str | None = "patchright"


class CamoufoxAdapter(BrowserAdapter):
    """使用 Camoufox Firefox 指纹管理的浏览器适配器。"""

    adapter_name: str = "camoufox"
    engine: str | None = "camoufox"


def _normalize_cookies(cookies: dict[str, Any] | str | None, url: str) -> list[dict[str, Any]]:
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
