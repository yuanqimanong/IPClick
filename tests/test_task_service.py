from __future__ import annotations

import json
import math
from typing import cast

import grpc
from grpc import ServicerContext
import pytest

from ipclick.adapters import registry
from ipclick.adapters.base import StreamHeader
from ipclick.dto.proto import task_pb2
from ipclick.exceptions import AdapterError, URLNotAllowedError, ValidationError
from ipclick.limiter import HostLimitTimeout
from ipclick.services.errors import CallerGone
from ipclick.services.task_service import TaskService, caller_still_waiting, is_forwarded
from ipclick.trace import RequestTrace
from ipclick.utils.config_util import Settings

from .helpers import RecordingContext, StubAdapter


@pytest.fixture
def service(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> TaskService:
    monkeypatch.setitem(registry.ADAPTER_CLASSES, StubAdapter.adapter_name, StubAdapter)
    return TaskService(settings)


@pytest.fixture
def stub(service: TaskService) -> StubAdapter:
    return cast(StubAdapter, service.default_adapter)


def _context() -> tuple[RecordingContext, ServicerContext]:
    recording = RecordingContext()
    return recording, cast(ServicerContext, cast(object, recording))


def test_unset_optional_fields_fall_back_to_adapter_settings(service: TaskService) -> None:
    kwargs = service._build_download_kwargs(task_pb2.ReqTask(url="http://127.0.0.1/x"))

    assert kwargs["timeout"] == service.adapter_settings.download_timeout
    assert kwargs["max_retries"] == service.adapter_settings.max_attempts
    assert kwargs["retry_delay"] == service.adapter_settings.initial_backoff
    assert kwargs["verify"] is True
    assert kwargs["allow_redirects"] is True
    assert kwargs["stream"] is False
    assert kwargs["allowed_status_codes"] is None


def test_explicit_zero_values_survive_the_wire(service: TaskService) -> None:
    request = task_pb2.ReqTask(url="http://127.0.0.1/x", max_retries=0, verify_ssl=False, allow_redirects=False)
    kwargs = service._build_download_kwargs(request)

    assert kwargs["max_retries"] == 0
    assert kwargs["verify"] is False
    assert kwargs["allow_redirects"] is False


def test_non_positive_timeout_falls_back(service: TaskService) -> None:
    request = task_pb2.ReqTask(url="http://127.0.0.1/x", timeout_seconds=0)
    assert service._build_download_kwargs(request)["timeout"] == service.adapter_settings.download_timeout


@pytest.mark.parametrize(
    "rpc_request",
    [
        task_pb2.ReqTask(url="http://127.0.0.1/x", max_retries=21),
        task_pb2.ReqTask(url="http://127.0.0.1/x", max_retries=-1),
        task_pb2.ReqTask(url="http://127.0.0.1/x", timeout_seconds=math.nan),
        task_pb2.ReqTask(url="http://127.0.0.1/x", retry_backoff_seconds=math.inf),
        task_pb2.ReqTask(url="http://127.0.0.1/x", retry_backoff_seconds=-1),
    ],
)
def test_hostile_retry_and_timeout_values_are_rejected(service: TaskService, rpc_request: task_pb2.ReqTask) -> None:
    with pytest.raises(ValidationError):
        service._build_download_kwargs(rpc_request)


def test_bodies_are_decoded_when_possible(service: TaskService) -> None:
    request = task_pb2.ReqTask(
        url="http://127.0.0.1/x",
        params=json.dumps({"q": "1"}),
        data=b'{"a": 1}',
        json=json.dumps({"b": 2}),
    )
    kwargs = service._build_download_kwargs(request)

    assert kwargs["params"] == {"q": "1"}
    assert kwargs["data"] == {"a": 1}
    assert kwargs["json"] == {"b": 2}


def test_binary_body_is_passed_through(service: TaskService) -> None:
    request = task_pb2.ReqTask(url="http://127.0.0.1/x", data=b"\xff\xfe raw")
    assert service._build_download_kwargs(request)["data"] == b"\xff\xfe raw"


def test_plain_text_body_stays_a_string(service: TaskService) -> None:
    request = task_pb2.ReqTask(url="http://127.0.0.1/x", data=b"a=1&b=2")
    assert service._build_download_kwargs(request)["data"] == "a=1&b=2"


@pytest.mark.parametrize(
    ("error", "code", "label"),
    [
        (URLNotAllowedError("blocked"), grpc.StatusCode.PERMISSION_DENIED, "url_not_allowed"),
        (HostLimitTimeout("throttled"), grpc.StatusCode.RESOURCE_EXHAUSTED, "host_limit"),
        (AdapterError("no dependency"), grpc.StatusCode.FAILED_PRECONDITION, "failed_precondition"),
        (ValidationError("bad argument"), grpc.StatusCode.INVALID_ARGUMENT, "invalid_argument"),
        (ValueError("bad value"), grpc.StatusCode.INVALID_ARGUMENT, "invalid_argument"),
    ],
)
def test_exceptions_map_to_grpc_codes(
    service: TaskService, error: Exception, code: grpc.StatusCode, label: str
) -> None:
    recording, context = _context()
    request = task_pb2.ReqTask(uuid="u1", url="http://127.0.0.1/x")
    trace = RequestTrace(adapter="curl_cffi", method="GET")

    response = service._response_for_exception(error, request, trace, context)

    assert recording.code is code
    assert recording.details == str(error)
    assert response.status_code == -1
    assert response.error_message == str(error)
    assert service._recorder.counters.rejected == {label: 1}


def test_unexpected_exception_is_reported_without_leaking_details(service: TaskService) -> None:
    recording, context = _context()
    request = task_pb2.ReqTask(uuid="u1", url="http://127.0.0.1/x")

    response = service._response_for_exception(
        RuntimeError("secret internals"), request, RequestTrace(adapter="a", method="GET"), context
    )

    assert recording.code is None
    assert "secret internals" not in response.error_message
    assert response.error_message == "内部错误: RuntimeError"
    assert service._recorder.counters.rejected == {"internal_error": 1}


def test_caller_gone_is_not_counted_as_a_rejection(service: TaskService) -> None:
    recording, context = _context()
    response = service._response_for_exception(
        CallerGone(), task_pb2.ReqTask(url="http://127.0.0.1/x"), RequestTrace(adapter="a", method="GET"), context
    )

    assert recording.code is None
    assert "已断开" in response.error_message
    assert service._recorder.counters.rejected == {}


def test_send_returns_the_adapter_response_with_a_trace(service: TaskService, stub: StubAdapter) -> None:
    _, context = _context()
    response = service.Send(task_pb2.ReqTask(uuid="u7", url="http://127.0.0.1/x"), context)

    assert response.request_uuid == "u7"
    assert response.status_code == 200
    assert response.content == b"body"
    assert dict(response.response_headers) == {"x-stub": "1"}
    assert response.trace.adapter == "curl_cffi"
    assert response.trace.node_id == service.node_id
    assert response.trace.forwarded is False
    assert stub.seen[0][0] == "http://127.0.0.1/x"


def test_send_refuses_a_blocked_url(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(registry.ADAPTER_CLASSES, StubAdapter.adapter_name, StubAdapter)
    settings["SECURITY"] = {"block_private_networks": True}
    service = TaskService(settings)
    recording, context = _context()

    response = service.Send(task_pb2.ReqTask(url="http://127.0.0.1/x"), context)

    assert recording.code is grpc.StatusCode.PERMISSION_DENIED
    assert response.status_code == -1


def test_send_gives_up_when_the_caller_disconnected(service: TaskService, stub: StubAdapter) -> None:
    recording = RecordingContext(active=False)
    response = service.Send(task_pb2.ReqTask(url="http://127.0.0.1/x"), cast(ServicerContext, cast(object, recording)))

    assert stub.seen == []
    assert response.status_code == -1
    assert "已断开" in response.error_message


def test_forward_marker_is_detected() -> None:
    assert is_forwarded(RecordingContext(metadata=(("ipclick-forwarded", "1"),))) is True
    assert is_forwarded(RecordingContext(metadata=(("IPCLICK-FORWARDED", "1"),))) is True
    assert is_forwarded(RecordingContext()) is False
    assert is_forwarded(object()) is False


def test_caller_still_waiting_defaults_to_true_when_unknowable() -> None:
    assert caller_still_waiting(RecordingContext(active=True)) is True
    assert caller_still_waiting(RecordingContext(active=False)) is False
    assert caller_still_waiting(object()) is True


def test_ping_reports_the_node_identity(service: TaskService) -> None:
    _, context = _context()
    response = service.Ping(task_pb2.PingReq(from_node="peer"), context)

    assert response.node_id == service.node_id
    assert response.forward is False
    assert response.auth_required is False


def test_stream_falls_back_to_chunked_download(service: TaskService) -> None:
    _, context = _context()
    chunks = list(service.SendStream(task_pb2.ReqTask(uuid="s1", url="http://127.0.0.1/x"), context))

    assert chunks[0].HasField("header")
    assert chunks[0].header.status_code == 200
    assert b"".join(c.chunk for c in chunks if c.HasField("chunk")) == b"body"
    assert chunks[-1].HasField("trailer")
    assert chunks[-1].trailer.total_bytes == 4


def test_stream_reports_a_blocked_url_in_the_header(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(registry.ADAPTER_CLASSES, StubAdapter.adapter_name, StubAdapter)
    settings["SECURITY"] = {"block_private_networks": True}
    service = TaskService(settings)
    recording, context = _context()

    chunks = list(service.SendStream(task_pb2.ReqTask(url="http://127.0.0.1/x"), context))

    assert recording.code is grpc.StatusCode.PERMISSION_DENIED
    assert chunks[0].header.status_code == -1
    assert chunks[0].header.error_message


def test_stream_body_error_uses_trailer_without_sending_a_second_header(
    service: TaskService, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken_stream(_adapter: object, _request: task_pb2.ReqTask):
        yield StreamHeader(url="https://example.com/final", status_code=200, headers={"x-test": "1"})
        yield b"partial"
        raise RuntimeError("connection lost")

    monkeypatch.setattr(service, "_open_stream", broken_stream)
    recording, context = _context()

    chunks = list(service.SendStream(task_pb2.ReqTask(uuid="mid-error", url="https://example.com/start"), context))

    assert sum(chunk.HasField("header") for chunk in chunks) == 1
    assert b"".join(chunk.chunk for chunk in chunks if chunk.HasField("chunk")) == b"partial"
    assert chunks[-1].HasField("trailer")
    assert chunks[-1].trailer.total_bytes == len(b"partial")
    assert chunks[-1].trailer.error_message == "内部错误: RuntimeError"
    assert recording.code is None


def test_default_adapter_also_gets_the_redirect_validator(service: TaskService) -> None:
    """默认适配器必须和惰性创建的适配器一样带上逐跳校验器。

    默认适配器是在 __init__ 里直接塞进 _adapter_cache 的，走不到
    _get_cached_adapter 里注入 url_validator 那一支。漏了它，默认路径
    （curl_cffi，也就是绝大多数请求走的那条）就完全不校验重定向目标：
    max_redirects 形同虚设，一次 302 能跳到云元数据地址绕开整套 [SECURITY]。
    """
    validator = service.default_adapter.url_validator

    assert validator is not None
    with pytest.raises(URLNotAllowedError):
        validator("http://169.254.169.254/latest/meta-data/")


def test_raw_data_body_is_sent_verbatim(service: TaskService) -> None:
    """data_is_raw 的请求体原样透传，不得被还原成结构化对象。

    否则 data=b'{"a": 1}' 会被 json.loads 成 dict，再被适配器编码成
    a=1 的 form-urlencoded body——调用方发的是 JSON，站点收到的是表单。
    data=b'12345' 更糟：还原成 int 后适配器直接抛 "data must be dict/list/..."。
    """
    for body in (b'{"a": 1}', b"12345", b"true", b"plain-text"):
        request = task_pb2.ReqTask(url="https://example.com/", data=body, data_is_raw=True)
        assert service._build_download_kwargs(request)["data"] == body


def test_structured_data_body_is_still_restored(service: TaskService) -> None:
    """不带 data_is_raw 的旧客户端保持原来的猜测行为，线上不断。"""
    request = task_pb2.ReqTask(url="https://example.com/", data=b'{"a": 1}')

    assert service._build_download_kwargs(request)["data"] == {"a": 1}


def test_key_value_pair_bodies_survive_the_json_round_trip(service: TaskService) -> None:
    """data=[("a", "1")] 过线时被 json.dumps 成 [["a", "1"]]，回来是嵌套 list。

    curl_cffi 的表单编码只认 2 元 tuple，于是报
    "not a valid non-string sequence or mapping object"——键值对形式的 data
    根本发不出去。服务端要把它还原回去。
    """
    request = task_pb2.ReqTask(url="https://example.com/", data=b'[["a", "1"], ["b", "2"]]')

    assert service._build_download_kwargs(request)["data"] == [("a", "1"), ("b", "2")]


def test_ordinary_json_lists_are_left_alone(service: TaskService) -> None:
    """只还原成对的那种；普通 JSON 数组不能被改成 tuple。"""
    request = task_pb2.ReqTask(url="https://example.com/", data=b"[1, 2, 3]")

    assert service._build_download_kwargs(request)["data"] == [1, 2, 3]


def test_fetch_failure_documents_have_the_same_shape_as_successful_ones() -> None:
    """同一个命令、同一个 -J，失败文档不该少一半的键。

    原先"连不上目标站点"因为走完了适配器有完整 15 个键，而"令牌不对"在构造客户端时
    就抛了、只剩 4 个键——脚本读 d["status"] 一个能过一个 KeyError。
    """
    from ipclick.cli.agent import _fetch_failure_shape

    shape = _fetch_failure_shape("http://example.com/")
    expected = {
        "reached_server",
        "url",
        "status",
        "elapsed_ms",
        "size",
        "adapter",
        "request_uuid",
        "trace",
        "headers",
        "body",
        "body_encoding",
        "body_truncated",
    }

    assert set(shape) == expected
    assert shape["url"] == "http://example.com/"
    assert shape["status"] is None
