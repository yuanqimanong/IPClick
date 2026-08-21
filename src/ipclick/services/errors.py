"""将服务层异常统一映射为 gRPC 状态、响应消息和追踪标签。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, final

import grpc

from ipclick.exceptions import AdapterError, HostResolutionError, URLNotAllowedError
from ipclick.limiter import HostLimitTimeout
from ipclick.trace import TraceRecorder
from ipclick.utils.log_util import log


LogLevel = Literal["info", "warning", "exception"]

CALLER_GONE_MESSAGE = "调用方已断开，请求未执行"

INTERNAL_ERROR_LABEL = "internal_error"


class CallerGone(Exception):
    """任务执行前发现调用方已断开。"""

    pass


@final
@dataclass(frozen=True)
class Failure:
    """对外安全的失败描述及其观测属性。"""

    message: str
    code: grpc.StatusCode | None = None
    label: str = ""
    level: LogLevel = "warning"
    reason: str = ""


@final
@dataclass(frozen=True)
class _Rule:
    error: type[Exception]
    code: grpc.StatusCode | None
    label: str
    reason: str
    level: LogLevel = "warning"


_RULES: tuple[_Rule, ...] = (
    _Rule(CallerGone, None, "", "调用方已断开", level="info"),
    # code=None：不设 gRPC 错误状态，于是它变成一条普通的失败响应（status_code == -1、
    # error 非空），而不是被 SDK 还原成异常抛出去。DNS 解析不出来是网络故障，
    # 和"连不上目标站点"应当表现一致。必须排在 URLNotAllowedError 之前——虽然
    # 现在两者没有继承关系，但排前面能保证以后也不会被更宽的规则先接走。
    _Rule(HostResolutionError, None, "dns_failure", "目标主机 DNS 解析失败", level="info"),
    _Rule(URLNotAllowedError, grpc.StatusCode.PERMISSION_DENIED, "url_not_allowed", "被 URL 策略拒绝"),
    _Rule(HostLimitTimeout, grpc.StatusCode.RESOURCE_EXHAUSTED, "host_limit", "被按 host 限流挡住"),
    _Rule(AdapterError, grpc.StatusCode.FAILED_PRECONDITION, "failed_precondition", "服务端无法处理"),
    _Rule(ValueError, grpc.StatusCode.INVALID_ARGUMENT, "invalid_argument", "参数不合法"),
)


def classify(error: Exception) -> Failure:
    """按最具体规则分类异常，未知异常不泄露原始文本。"""
    for rule in _RULES:
        if isinstance(error, rule.error):
            message = CALLER_GONE_MESSAGE if isinstance(error, CallerGone) else str(error)
            return Failure(message=message, code=rule.code, label=rule.label, level=rule.level, reason=rule.reason)
    return Failure(
        message=f"内部错误: {type(error).__name__}",
        label=INTERNAL_ERROR_LABEL,
        level="exception",
        reason="未预期的异常",
    )


def report(
    failure: Failure,
    error: Exception,
    *,
    request_uuid: str,
    recorder: TraceRecorder,
    context: object,
) -> None:
    """设置 gRPC 状态、记录拒绝计数，并按严重度写日志。"""
    if failure.label:
        recorder.record_rejected(failure.label)

    if failure.code is not None:
        setter = getattr(context, "set_code", None)
        if callable(setter):
            setter(failure.code)
        detail_setter = getattr(context, "set_details", None)
        if callable(detail_setter):
            detail_setter(failure.message)

    text = f"请求 {request_uuid} {failure.reason}：{error}"
    if failure.level == "exception":
        log.exception(text)
    elif failure.level == "info":
        log.info(text)
    else:
        log.warning(text)


__all__ = ["CALLER_GONE_MESSAGE", "CallerGone", "Failure", "LogLevel", "classify", "report"]
