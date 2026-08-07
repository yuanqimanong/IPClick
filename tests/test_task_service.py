"""TaskService：proto3 显式存在性、适配器缓存、错误处理、凭证不回传。"""

from typing import Any

import pytest

from ipclick.adapters.base import DownloaderAdapter
from ipclick.dto.models import DownloadTask, IPClickAdapter
from ipclick.dto.proto import task_pb2
from ipclick.dto.response import Response
from ipclick.services.task_service import TaskService
from ipclick.utils.config_util import Settings


class RecordingAdapter(DownloaderAdapter):
    """记录收到的 download() 参数，不发真实请求。"""

    adapter_name = "curl_cffi"  # 冒充默认适配器，便于注入

    def __init__(self):
        super().__init__()
        self.last_kwargs: dict[str, Any] = {}
        self.last_url: str = ""
        self.call_count = 0
        self.closed = False

    def download(self, url: str, **kwargs: Any) -> Response:  # type: ignore[override]
        self.call_count += 1
        self.last_url = url
        self.last_kwargs = kwargs
        return Response(url=url, status_code=200, content=b"ok", headers={"X-Test": "1"})

    def close(self) -> None:
        self.closed = True


class FakeContext:
    """最小化的 grpc.ServicerContext 替身。"""

    def __init__(self):
        self.code = None
        self.details_text = ""

    def set_code(self, code: Any) -> None:
        self.code = code

    def set_details(self, details: str) -> None:
        self.details_text = details


@pytest.fixture
def service(monkeypatch: pytest.MonkeyPatch) -> TaskService:
    adapter = RecordingAdapter()
    monkeypatch.setattr("ipclick.services.task_service.get_default_adapter", lambda: adapter)
    monkeypatch.setattr("ipclick.services.task_service.get_adapter", lambda name: adapter)
    return TaskService(Settings({"SECURITY": {"block_private_networks": False}}))


def _adapter_of(service: TaskService) -> RecordingAdapter:
    return service.default_adapter  # type: ignore[return-value]


class TestProto3Presence:
    def test_unset_verify_ssl_defaults_to_true(self, service: TaskService):
        """核心回归：以前未设置 verify_ssl 会被当成 False，
        于是服务端对每个请求都关闭了证书校验。"""
        request = task_pb2.ReqTask(url="http://example.com", uuid="u1")
        assert not request.HasField("verify_ssl")

        service.Send(request, FakeContext())
        assert _adapter_of(service).last_kwargs["verify"] is True

    def test_explicit_verify_false_is_respected(self, service: TaskService):
        request = task_pb2.ReqTask(url="http://example.com", uuid="u1", verify_ssl=False)
        service.Send(request, FakeContext())
        assert _adapter_of(service).last_kwargs["verify"] is False

    def test_unset_timeout_does_not_become_zero(self, service: TaskService):
        """回归：proto3 隐式默认让 timeout 变成 0，等于立刻超时。"""
        request = task_pb2.ReqTask(url="http://example.com", uuid="u1")
        service.Send(request, FakeContext())
        assert _adapter_of(service).last_kwargs["timeout"] == 60.0

    def test_unset_allow_redirects_defaults_to_true(self, service: TaskService):
        request = task_pb2.ReqTask(url="http://example.com", uuid="u1")
        service.Send(request, FakeContext())
        assert _adapter_of(service).last_kwargs["allow_redirects"] is True

    def test_explicit_zero_retries_respected(self, service: TaskService):
        request = task_pb2.ReqTask(url="http://example.com", uuid="u1", max_retries=0)
        service.Send(request, FakeContext())
        assert _adapter_of(service).last_kwargs["max_retries"] == 0

    def test_sdk_task_round_trip_preserves_verify(self, service: TaskService):
        """SDK 构造的任务经 protobuf 到服务端后，verify 必须保持 True。"""
        request = DownloadTask(url="http://example.com", verify=True).to_protobuf()
        service.Send(request, FakeContext())
        assert _adapter_of(service).last_kwargs["verify"] is True


class TestAdapterCache:
    def test_adapter_is_reused_across_requests(self, service: TaskService):
        """回归：_adapter_cache 只读不写，每个请求都新建适配器。"""
        for i in range(5):
            service.Send(task_pb2.ReqTask(url="http://example.com", uuid=f"u{i}"), FakeContext())
        assert len(service._adapter_cache) == 1
        assert _adapter_of(service).call_count == 5

    def test_cleanup_closes_cached_adapters(self, service: TaskService):
        """回归：缓存永远为空，cleanup() 什么也关不掉。"""
        service.Send(task_pb2.ReqTask(url="http://example.com", uuid="u1"), FakeContext())
        adapter = _adapter_of(service)
        service.cleanup()
        assert adapter.closed is True

    def test_cleanup_does_not_wipe_global_registry(self, service: TaskService):
        """回归：cleanup() 里的 ADAPTER_CLASSES.clear() 会让本进程再也造不出适配器。"""
        from ipclick.adapters.registry import ADAPTER_CLASSES

        service.cleanup()
        assert "curl_cffi" in ADAPTER_CLASSES
        assert "httpx" in ADAPTER_CLASSES


class TestErrorHandling:
    def test_blocked_url_returns_permission_denied(self, monkeypatch: pytest.MonkeyPatch):
        adapter = RecordingAdapter()
        monkeypatch.setattr("ipclick.services.task_service.get_default_adapter", lambda: adapter)
        monkeypatch.setattr("ipclick.services.task_service.get_adapter", lambda name: adapter)
        service = TaskService(Settings({"SECURITY": {"block_private_networks": True}}))

        context = FakeContext()
        response = service.Send(task_pb2.ReqTask(url="http://127.0.0.1:8000/", uuid="u1"), context)

        assert response.status_code == -1
        assert "内网" in response.error_message
        assert adapter.call_count == 0

    def test_unknown_adapter_returns_invalid_argument(self, service: TaskService):
        context = FakeContext()
        request = task_pb2.ReqTask(url="http://example.com", uuid="u1")
        request.adapter = 999  # 非法枚举值
        response = service.Send(request, context)
        assert response.status_code == -1

    def test_adapter_exception_does_not_escape_rpc(self, monkeypatch: pytest.MonkeyPatch):
        """适配器炸了也要返回结构化响应，而不是让 RPC 以 UNKNOWN + 堆栈结束。"""

        class ExplodingAdapter(RecordingAdapter):
            def download(self, url: str, **kwargs: Any) -> Response:  # type: ignore[override]
                raise RuntimeError("boom")

        adapter = ExplodingAdapter()
        monkeypatch.setattr("ipclick.services.task_service.get_default_adapter", lambda: adapter)
        monkeypatch.setattr("ipclick.services.task_service.get_adapter", lambda name: adapter)
        service = TaskService(Settings({}))

        response = service.Send(task_pb2.ReqTask(url="http://example.com", uuid="u1"), FakeContext())
        assert response.status_code == -1
        assert "RuntimeError" in response.error_message
        # 内部错误信息不应把原始异常文本原样吐给调用方
        assert "boom" not in response.error_message


class TestResponseBuilding:
    def test_proxy_credentials_not_echoed_back(self, service: TaskService):
        """回归：original_request 被整个塞回响应，代理账号密码随之泄漏。"""
        request = task_pb2.ReqTask(
            url="http://example.com",
            uuid="u1",
            proxy="http://user:secret@proxy.example.com:8080",
        )
        response = service.Send(request, FakeContext())
        assert "secret" not in str(response)
        assert not response.HasField("original_request")

    def test_response_carries_headers_and_body(self, service: TaskService):
        response = service.Send(task_pb2.ReqTask(url="http://example.com", uuid="u1"), FakeContext())
        assert response.status_code == 200
        assert response.content == b"ok"
        assert dict(response.response_headers) == {"X-Test": "1"}
        assert response.request_uuid == "u1"

    def test_non_json_body_passed_through_as_string(self, service: TaskService):
        request = task_pb2.ReqTask(url="http://example.com", uuid="u1", data="raw=1&b=2")
        service.Send(request, FakeContext())
        assert _adapter_of(service).last_kwargs["data"] == "raw=1&b=2"

    def test_json_body_decoded(self, service: TaskService):
        request = task_pb2.ReqTask(url="http://example.com", uuid="u1", json='{"a": 1}')
        service.Send(request, FakeContext())
        assert _adapter_of(service).last_kwargs["json"] == {"a": 1}

    def test_method_is_translated(self, service: TaskService):
        request = task_pb2.ReqTask(url="http://example.com", uuid="u1", method=task_pb2.POST)
        service.Send(request, FakeContext())
        assert _adapter_of(service).last_kwargs["method"] == "POST"

    def test_adapter_enum_echoed(self, service: TaskService):
        request = task_pb2.ReqTask(url="http://example.com", uuid="u1", adapter=IPClickAdapter.CURL_CFFI.pb_value)
        response = service.Send(request, FakeContext())
        assert response.adapter == IPClickAdapter.CURL_CFFI.pb_value
