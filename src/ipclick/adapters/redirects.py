"""带准入校验的逐跳重定向跟随。

SSRF 准入原本只在请求发出**之前**校验调用方给的那一个 URL，而适配器默认
``allow_redirects=True`` 交给底层库自行跟随，跟随时不再过策略。于是目标站点回一个
``302 Location: http://169.254.169.254/...`` 就把云元数据取回来了——策略只看过第一跳。

事后补检没有意义：``resp.history`` 是请求已经发出之后的事，机密那时已经取回来了。
所以只能自己逐跳跟随，每跳**发出之前**校验。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
import contextlib
from typing import Any, Protocol
from urllib.parse import urljoin

from ipclick.exceptions import ValidationError
from ipclick.utils.log_util import log


REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})

DEFAULT_MAX_REDIRECTS = 10

# 301/302 历史上被所有浏览器实现成"POST 之后改用 GET"，303 是规范明确要求改。
# 307/308 则明确要求保持方法与请求体不变。
_METHOD_REWRITING_CODES = frozenset({301, 302, 303})


class _Response(Protocol):
    """跟随重定向只需要状态码和响应头。"""

    @property
    def status_code(self) -> int: ...

    @property
    def headers(self) -> Any: ...


def _location(response: _Response) -> str:
    headers = response.headers or {}
    getter = getattr(headers, "get", None)
    if getter is None:
        return ""
    for key in ("location", "Location"):
        value = getter(key)
        if value:
            return str(value).strip()
    return ""


def _release(response: Any) -> None:
    """关掉一条已经用不上的中间响应，把连接还给连接池。

    流式跟随时这条很重要：每一跳都是一个真的 ``stream=True`` 响应，只读了它的
    Location 就不再需要，不关就一路占着连接直到 GC。
    """
    closer = getattr(response, "close", None)
    if callable(closer):
        with contextlib.suppress(Exception):
            closer()


def _next_hop(response: Any, current_url: str, hop: int, max_redirects: int) -> tuple[str, int] | None:
    """判断是否还有下一跳；有则返回（目标绝对地址，状态码）。

    ``None`` 表示"这条响应就是最终结果"。不做校验也不发请求，只负责解析——
    同步与异步两条跟随循环共用这一份逻辑，避免两边跑偏。
    """
    status = int(getattr(response, "status_code", 0) or 0)
    if status not in REDIRECT_CODES:
        return None

    target = _location(response)
    if not target:
        # 声明了重定向却没给 Location，没有下一跳可走，原样返回让调用方看到。
        return None

    # Location 可以是相对路径，必须按当前 URL 解析成绝对地址再校验，
    # 否则 "/latest/meta-data" 这种相对跳转会绕过基于主机名的策略。
    next_url = urljoin(current_url, target)
    _release(response)
    if hop >= max_redirects:
        raise ValidationError(f"重定向次数超过上限 {max_redirects}：最后停在 {current_url}")
    return next_url, status


def _rewrite(status: int, method: str, body: Any) -> tuple[str, Any]:
    """按状态码决定下一跳的方法与请求体。"""
    if status in _METHOD_REWRITING_CODES and method not in ("GET", "HEAD"):
        return "GET", None
    return method, body


def follow_with_policy(
    send: Callable[[str, str, Any], Any],
    url: str,
    method: str,
    body: Any,
    validate: Callable[[str], None],
    *,
    max_redirects: int = DEFAULT_MAX_REDIRECTS,
) -> Any:
    """逐跳跟随重定向，每跳发出之前先过一次 ``validate``。

    ``send(url, method, body)`` 必须以**不自动跟随**的方式发一次请求。``validate``
    在 URL 不被允许时抛异常（``URLNotAllowedError``），异常直接向上传播——这正是我们
    要的：请求根本不会发出去。
    """
    current_url = url
    current_method = method
    current_body = body

    for hop in range(max_redirects + 1):
        response = send(current_url, current_method, current_body)
        hop_target = _next_hop(response, current_url, hop, max_redirects)
        if hop_target is None:
            return response
        next_url, status = hop_target
        validate(next_url)
        current_method, current_body = _rewrite(status, current_method, current_body)
        log.debug(f"重定向 {status}：{current_url} -> {next_url}（已通过 URL 准入）")
        current_url = next_url

    # for 循环里 hop == max_redirects 那一轮已经抛过了，走不到这里。
    raise ValidationError(f"重定向处理异常：{url}")


async def afollow_with_policy(
    send: Callable[[str, str, Any], Awaitable[Any]],
    url: str,
    method: str,
    body: Any,
    validate: Callable[[str], None],
    *,
    max_redirects: int = DEFAULT_MAX_REDIRECTS,
) -> Any:
    """``follow_with_policy`` 的异步版本。

    异步路径原来直接把 ``allow_redirects=True`` 交给底层库，跟随时完全不过准入——
    也就是说 ``[SERVER].async_mode = true`` 一开，整套逐跳 SSRF 校验就不存在了。
    """
    current_url = url
    current_method = method
    current_body = body

    for hop in range(max_redirects + 1):
        response = await send(current_url, current_method, current_body)
        hop_target = _next_hop(response, current_url, hop, max_redirects)
        if hop_target is None:
            return response
        next_url, status = hop_target
        validate(next_url)
        current_method, current_body = _rewrite(status, current_method, current_body)
        log.debug(f"重定向 {status}：{current_url} -> {next_url}（已通过 URL 准入）")
        current_url = next_url

    raise ValidationError(f"重定向处理异常：{url}")


class HopFollowingMixin:
    """把"逐跳跟随重定向并逐跳校验"从各 HTTP 适配器里抽出来共用。

    curl_cffi 与 niquests 原来各有一份逐字相同的实现（``_hop_sender`` 加三个
    ``*_following_policy``）。能共用是因为差异在更下层就已经收敛：两个库的调用形态都是
    ``session.request(method, url, **kwargs)``。

    合并的直接理由是那份复制已经付过代价——``files`` 只在同步路径上被拦、
    ``connect_timeout`` 只有 niquests 那边真的用上，两处都是"改了一边忘了另一边"。
    这一层压着 SSRF 准入，只写一遍才谈得上"三条路径行为一致"。

    使用方需要提供 ``url_validator``（由 ``DownloaderAdapter`` 声明）。
    """

    url_validator: Callable[[str], None] | None = None

    @staticmethod
    def _hop_sender(request_kwargs: dict[str, Any], *, stream: bool = False) -> tuple[Any, Any]:
        """构造逐跳请求所需的 kwargs 与初始请求体快照。

        每跳都必须关掉底层库自己的跟随（``allow_redirects=False``），否则第一跳就被它
        一路跟到底，校验器再也插不进去。
        """
        hop_kwargs = dict(request_kwargs)
        hop_kwargs["allow_redirects"] = False
        if stream:
            hop_kwargs["stream"] = True
        body_keys = ("data", "json", "files")
        saved_body = {key: hop_kwargs.get(key) for key in body_keys}

        def prepare(body: Any) -> dict[str, Any]:
            kw = dict(hop_kwargs)
            if body is None:
                for key in body_keys:
                    _ = kw.pop(key, None)
            return kw

        return prepare, saved_body

    def _request_following_policy(
        self,
        session: Any,
        method: str,
        url: str,
        request_kwargs: dict[str, Any],
        allow_redirects: bool,
    ) -> Any:
        """同步：装了 url_validator 时自己逐跳跟随，每跳发出之前校验一次。"""
        validator = self.url_validator
        if validator is None or not allow_redirects:
            return session.request(method, url, **request_kwargs)

        prepare, saved_body = self._hop_sender(request_kwargs)

        def send(hop_url: str, hop_method: str, body: Any) -> Any:
            return session.request(hop_method, hop_url, **prepare(body))

        return follow_with_policy(send, url, method, saved_body, validator)

    async def _arequest_following_policy(
        self,
        session: Any,
        method: str,
        url: str,
        request_kwargs: dict[str, Any],
        allow_redirects: bool,
    ) -> Any:
        """协程版。这条路曾经把 allow_redirects 直接交给底层库、一次校验都不做：
        ``[SERVER].async_mode = true`` 一开，整套逐跳准入就等于不存在。
        """
        validator = self.url_validator
        if validator is None or not allow_redirects:
            return await session.request(method, url, **request_kwargs)

        prepare, saved_body = self._hop_sender(request_kwargs)

        async def send(hop_url: str, hop_method: str, body: Any) -> Any:
            return await session.request(hop_method, hop_url, **prepare(body))

        return await afollow_with_policy(send, url, method, saved_body, validator)

    def _stream_following_policy(
        self,
        session: Any,
        method: str,
        url: str,
        request_kwargs: dict[str, Any],
        allow_redirects: bool,
    ) -> Any:
        """流式版，返回的是**最后一跳**那条流。

        中间跳也用 ``stream=True`` 发：重定向响应的正文本来就是空的或极小，读完
        ``Location`` 立刻由 ``_release`` 关掉还连接。这条路同样曾经完全不校验。
        """
        validator = self.url_validator
        if validator is None or not allow_redirects:
            return session.request(method, url, stream=True, **request_kwargs)

        prepare, saved_body = self._hop_sender(request_kwargs, stream=True)

        def send(hop_url: str, hop_method: str, body: Any) -> Any:
            return session.request(hop_method, hop_url, **prepare(body))

        return follow_with_policy(send, url, method, saved_body, validator)


__all__ = [
    "DEFAULT_MAX_REDIRECTS",
    "REDIRECT_CODES",
    "HopFollowingMixin",
    "afollow_with_policy",
    "follow_with_policy",
]
