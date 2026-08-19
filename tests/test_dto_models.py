from __future__ import annotations

import json

import pytest

from ipclick.dto.models import DownloadResponse, DownloadTask, HttpMethod, IPClickAdapter, ProxyConfig, ResponseTrace
from ipclick.dto.proto import task_pb2
from ipclick.exceptions import RequestError, ValidationError


def test_url_is_required() -> None:
    with pytest.raises(ValidationError):
        DownloadTask()


@pytest.mark.parametrize("url", ["ftp://example.com", "example.com", "//example.com", "file:///etc/passwd"])
def test_url_must_be_http_or_https(url: str) -> None:
    with pytest.raises(ValidationError):
        DownloadTask(url=url)


def test_data_and_json_are_mutually_exclusive() -> None:
    with pytest.raises(ValidationError):
        DownloadTask(url="http://example.com", data="a", json={"b": 1})


def test_negative_retries_are_rejected() -> None:
    with pytest.raises(ValidationError):
        DownloadTask(url="http://example.com", max_retries=-1)


@pytest.mark.parametrize("timeout", [0, -3.5])
def test_non_positive_timeout_is_rejected(timeout: float) -> None:
    with pytest.raises(ValidationError):
        DownloadTask(url="http://example.com", timeout=timeout)


def test_curl_cffi_gets_a_default_impersonate() -> None:
    task = DownloadTask(url="http://example.com")
    assert task.adapter is IPClickAdapter.CURL_CFFI
    assert task.impersonate == "chrome"


def test_other_adapters_keep_impersonate_unset() -> None:
    task = DownloadTask(url="http://example.com", adapter=IPClickAdapter.NIQUESTS)
    assert task.impersonate is None


def test_allowed_status_codes_default() -> None:
    assert DownloadTask(url="http://example.com").allowed_status_codes == [200, 404]
    assert DownloadTask(url="http://example.com", allowed_status_codes=[204]).allowed_status_codes == [204]


def test_to_protobuf_generates_a_uuid_when_missing() -> None:
    pb = DownloadTask(url="http://example.com").to_protobuf()
    assert pb.uuid
    assert DownloadTask(url="http://example.com", uuid="fixed").to_protobuf().uuid == "fixed"


def test_to_protobuf_maps_the_core_fields() -> None:
    task = DownloadTask(
        url="http://example.com/p",
        method=HttpMethod.POST,
        adapter="niquests",
        headers={"X-Str": "v", "X-Num": 7},
        params={"q": "x"},
        json={"a": 1},
        timeout=12.5,
        max_retries=0,
        verify=False,
        allow_redirects=False,
        stream=True,
    )
    pb = task.to_protobuf()

    assert pb.adapter == task_pb2.NIQUESTS
    assert pb.method == task_pb2.POST
    assert pb.url == "http://example.com/p"
    assert dict(pb.headers) == {"X-Str": "v", "X-Num": "7"}
    assert json.loads(pb.params) == {"q": "x"}
    assert json.loads(pb.json) == {"a": 1}
    assert pb.timeout_seconds == pytest.approx(12.5)
    assert pb.HasField("max_retries") and pb.max_retries == 0
    assert pb.HasField("verify_ssl") and pb.verify_ssl is False
    assert pb.allow_redirects is False
    assert pb.stream is True


def test_to_protobuf_parses_a_cookie_string() -> None:
    pb = DownloadTask(url="http://example.com", cookies="a=1; b = 2 ;broken").to_protobuf()
    assert dict(pb.cookies) == {"a": "1", "b": "2"}


def test_to_protobuf_encodes_binary_and_text_bodies() -> None:
    assert DownloadTask(url="http://example.com", data=b"\x00\xff").to_protobuf().data == b"\x00\xff"
    assert DownloadTask(url="http://example.com", data="text").to_protobuf().data == b"text"
    assert json.loads(DownloadTask(url="http://example.com", data={"k": 1}).to_protobuf().data) == {"k": 1}


def test_proxy_true_cannot_reach_the_wire() -> None:
    with pytest.raises(ValidationError):
        DownloadTask(url="http://example.com", proxy=True).to_protobuf()


def test_proxy_config_is_rendered_to_a_url() -> None:
    pb = DownloadTask(url="http://example.com", proxy=ProxyConfig(host="1.2.3.4", port=8080)).to_protobuf()
    assert pb.proxy == "http://1.2.3.4:8080"


def test_unknown_adapter_name_is_rejected() -> None:
    with pytest.raises(ValidationError):
        DownloadTask(url="http://example.com", adapter="nope").to_protobuf()


def test_adapter_lookup_by_name_and_enum_value() -> None:
    assert IPClickAdapter.from_str("CURL_cffi") is IPClickAdapter.CURL_CFFI
    assert IPClickAdapter.from_pb(task_pb2.CAMOUFOX) is IPClickAdapter.CAMOUFOX
    with pytest.raises(ValueError):
        IPClickAdapter.from_pb(999)
    with pytest.raises(ValueError):
        IPClickAdapter.from_str("wget")


@pytest.mark.parametrize(
    ("config", "expected"),
    [
        (ProxyConfig(), None),
        (ProxyConfig(host="h", port=1), "http://h:1"),
        (ProxyConfig(scheme="socks5", tunnel_server="gw:9000"), "socks5://gw:9000"),
        (ProxyConfig(host="h", port=1, auth_key="u", auth_password="p"), "http://u:p@h:1"),
        (ProxyConfig(host="h", port=1, channel_name="c", session_ttl=5, country_code="US"), "http://:Cc:T5:AUS@h:1"),
    ],
)
def test_proxy_config_to_url(config: ProxyConfig, expected: str | None) -> None:
    assert config.to_url() == expected


def test_response_from_protobuf_without_trace() -> None:
    pb = task_pb2.TaskResp(
        request_uuid="u1",
        adapter=task_pb2.CURL_CFFI,
        effective_url="http://example.com/final",
        status_code=200,
        response_headers={"content-type": "text/plain"},
        content=b"hello",
        response_time_ms=42,
    )
    response = DownloadResponse.from_protobuf(pb)

    assert response.adapter_type == "curl_cffi"
    assert response.url == "http://example.com/final"
    assert response.text == "hello"
    assert response.elapsed_ms == 42
    assert response.error is None
    assert response.ok
    assert response.trace == ResponseTrace()


def test_response_from_protobuf_keeps_the_trace() -> None:
    pb = task_pb2.TaskResp(
        status_code=200,
        trace=task_pb2.Trace(node_id="n1", adapter="curl_cffi", attempts=0, forwarded=True, queued_ms=7),
    )
    trace = DownloadResponse.from_protobuf(pb).trace

    assert trace.node_id == "n1"
    assert trace.forwarded is True
    assert trace.queued_ms == 7
    assert trace.attempts == 1


def test_response_error_paths() -> None:
    failed = DownloadResponse.from_error("boom", url="http://example.com")
    assert failed.status_code == -1
    assert not failed.ok
    with pytest.raises(RequestError):
        failed.raise_for_status()

    with pytest.raises(ValueError):
        DownloadResponse(status_code=200, text="not json").json()


def test_response_is_not_success_when_error_is_set() -> None:
    assert not DownloadResponse(status_code=200, error="partial failure").is_success()
