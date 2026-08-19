from __future__ import annotations

import grpc
import pytest

from ipclick.adapters import registry
from ipclick.async_limiter import AsyncHostLimiter
from ipclick.cluster.async_forwarder import AsyncForwardingTaskService
from ipclick.cluster.forwarder import ForwardingTaskService
from ipclick.cluster.node import ClusterConfig
from ipclick.cluster.pool import NodePool
from ipclick.exceptions import AdapterError, URLNotAllowedError, ValidationError
from ipclick.limiter import HostLimitTimeout
from ipclick.services.async_task_service import AsyncTaskService
from ipclick.services.errors import CALLER_GONE_MESSAGE, CallerGone, classify
from ipclick.services.task_service import TaskService
from ipclick.utils.config_util import Settings

from .helpers import StubAdapter


@pytest.fixture(autouse=True)
def stub_adapter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(registry.ADAPTER_CLASSES, StubAdapter.adapter_name, StubAdapter)


def _cluster_settings() -> Settings:
    return Settings(
        {
            "SERVER": {"host": "127.0.0.1", "port": 19528, "max_workers": 2},
            "SECURITY": {},
            "DOWNLOADER": {},
            "BROWSER": {"enabled": False},
            "CLUSTER": {"forward": "on", "nodes": [{"id": "n1", "address": "127.0.0.1:19601"}]},
            "TRACE": {"sqlite_enabled": False},
        }
    )


def test_async_service_owns_its_limiter(settings: Settings) -> None:
    service = AsyncTaskService(settings)
    try:
        assert isinstance(service.async_limiter, AsyncHostLimiter)
        assert service.limiters_for_sharding() == [service.async_limiter]
    finally:
        service.cleanup()


def test_async_forwarding_service_initialises_both_branches() -> None:
    config = _cluster_settings()
    cluster = ClusterConfig.from_config(config["CLUSTER"])
    pool = NodePool(cluster, start_probing=False)
    service = AsyncForwardingTaskService(config, cluster, pool=pool, server_host="127.0.0.1", server_port=19601)
    try:
        assert isinstance(service.async_limiter, AsyncHostLimiter)
        assert service.self_id == "n1"
        assert service.forward_enabled is True
        assert service.host_limiter is not None
    finally:
        service.cleanup()
        pool.stop()


def test_forwarding_service_reports_itself_as_forwarding() -> None:
    config = _cluster_settings()
    cluster = ClusterConfig.from_config(config["CLUSTER"])
    pool = NodePool(cluster, start_probing=False)
    service = ForwardingTaskService(config, cluster, pool=pool, server_host="127.0.0.1", server_port=19601)
    try:
        assert service.forward_enabled is True
        assert service.node_id == "n1"
    finally:
        service.cleanup()
        pool.stop()


def test_plain_service_is_not_forwarding(settings: Settings) -> None:
    service = TaskService(settings)
    try:
        assert service.forward_enabled is False
    finally:
        service.cleanup()


@pytest.mark.parametrize(
    ("error", "code", "label"),
    [
        (CallerGone(), None, ""),
        (URLNotAllowedError("blocked"), grpc.StatusCode.PERMISSION_DENIED, "url_not_allowed"),
        (HostLimitTimeout("slow down"), grpc.StatusCode.RESOURCE_EXHAUSTED, "host_limit"),
        (AdapterError("missing"), grpc.StatusCode.FAILED_PRECONDITION, "failed_precondition"),
        (ValidationError("bad"), grpc.StatusCode.INVALID_ARGUMENT, "invalid_argument"),
        (ValueError("bad"), grpc.StatusCode.INVALID_ARGUMENT, "invalid_argument"),
        (RuntimeError("boom"), None, "internal_error"),
    ],
)
def test_failures_are_classified_once_for_every_rpc(error: Exception, code: grpc.StatusCode | None, label: str) -> None:
    failure = classify(error)
    assert failure.code is code
    assert failure.label == label


def test_url_policy_rejection_beats_the_plain_value_error_rule() -> None:
    assert classify(URLNotAllowedError("x")).code is grpc.StatusCode.PERMISSION_DENIED


def test_caller_gone_carries_its_own_message() -> None:
    assert classify(CallerGone()).message == CALLER_GONE_MESSAGE


def test_internal_errors_do_not_leak_their_text() -> None:
    failure = classify(RuntimeError("db password is hunter2"))
    assert "hunter2" not in failure.message
    assert failure.level == "exception"
