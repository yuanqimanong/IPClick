from __future__ import annotations

import json
from typing import cast

import grpc
from grpc import ServicerContext
import pytest

from ipclick.adapters import registry
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
