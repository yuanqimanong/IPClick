"""带准入校验的逐跳重定向跟随。

SSRF 准入原本只在请求发出**之前**校验调用方给的那一个 URL，而适配器默认
``allow_redirects=True`` 交给底层库自行跟随，跟随时不再过策略。于是目标站点回一个
``302 Location: http://169.254.169.254/...`` 就把云元数据取回来了——策略只看过第一跳。

事后补检没有意义：``resp.history`` 是请求已经发出之后的事，机密那时已经取回来了。
所以只能自己逐跳跟随，每跳**发出之前**校验。
"""

from __future__ import annotations

from collections.abc import Callable
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
        status = int(getattr(response, "status_code", 0) or 0)
        if status not in REDIRECT_CODES:
            return response

        target = _location(response)
        if not target:
            # 声明了重定向却没给 Location，没有下一跳可走，原样返回让调用方看到。
            return response

        if hop >= max_redirects:
            raise ValidationError(f"重定向次数超过上限 {max_redirects}：最后停在 {current_url}")

        # Location 可以是相对路径，必须按当前 URL 解析成绝对地址再校验，
        # 否则 "/latest/meta-data" 这种相对跳转会绕过基于主机名的策略。
        next_url = urljoin(current_url, target)
        validate(next_url)

        if status in _METHOD_REWRITING_CODES and current_method not in ("GET", "HEAD"):
            current_method = "GET"
            current_body = None
        log.debug(f"重定向 {status}：{current_url} -> {next_url}（已通过 URL 准入）")
        current_url = next_url

    # for 循环里 hop == max_redirects 那一轮已经抛过了，走不到这里。
    raise ValidationError(f"重定向处理异常：{url}")


__all__ = ["DEFAULT_MAX_REDIRECTS", "REDIRECT_CODES", "follow_with_policy"]
