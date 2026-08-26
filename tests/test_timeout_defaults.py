from __future__ import annotations

import inspect
import tomllib

import pytest

from ipclick.adapters.curl_cffi_adapter import CurlCffiAdapter
from ipclick.adapters.settings import (
    DEFAULT_CONNECT_TIMEOUT,
    DEFAULT_DOWNLOAD_TIMEOUT,
    DEFAULT_STREAM_TIMEOUT,
    AdapterSettings,
)
from ipclick.config_loader.loader import DEFAULT_CONFIG_PATH
from ipclick.web.editable import FIELDS


def _template_downloader() -> dict[str, object]:
    return dict(tomllib.loads(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))["DOWNLOADER"])


def test_every_place_that_declares_a_default_timeout_agrees() -> None:
    """ "默认超时是多少"只能有一个答案。

    这四处曾经各写各的：随包模板 300、AdapterSettings 300、各适配器 download() 签名 60、
    「试一试」页面预填 30。于是"我在试一试里跑通了"和"线上跑通了"根本不是同一件事，
    而页面上看不出任何差别。
    """
    template = _template_downloader()

    assert template["download_timeout"] == DEFAULT_DOWNLOAD_TIMEOUT
    assert template["connect_timeout"] == DEFAULT_CONNECT_TIMEOUT
    assert AdapterSettings().download_timeout == DEFAULT_DOWNLOAD_TIMEOUT
    assert AdapterSettings().connect_timeout == DEFAULT_CONNECT_TIMEOUT
    assert inspect.signature(CurlCffiAdapter.download).parameters["timeout"].default == DEFAULT_DOWNLOAD_TIMEOUT
    assert FIELDS["DOWNLOADER.download_timeout"].default == DEFAULT_DOWNLOAD_TIMEOUT
    assert FIELDS["DOWNLOADER.connect_timeout"].default == DEFAULT_CONNECT_TIMEOUT
    assert template["stream_timeout"] == DEFAULT_STREAM_TIMEOUT
    assert AdapterSettings().stream_timeout == DEFAULT_STREAM_TIMEOUT
    assert FIELDS["DOWNLOADER.stream_timeout"].default == DEFAULT_STREAM_TIMEOUT


def test_the_default_connect_budget_leaves_room_to_receive_data() -> None:
    """连接段是从总预算里**先**划走的，所以默认值里它必须明显小于总预算。

    两者相等的话，默认配置下建连一慢，收数据就是 0 秒——请求必然超时，而错误信息
    只会说"超时"，不会说"预算被建连吃光了"。
    """
    assert DEFAULT_CONNECT_TIMEOUT * 2 <= DEFAULT_DOWNLOAD_TIMEOUT

    connect, read = CurlCffiAdapter(AdapterSettings())._timeout_pair(None)
    assert connect == DEFAULT_CONNECT_TIMEOUT
    assert read > 0
    assert connect + read == DEFAULT_DOWNLOAD_TIMEOUT


@pytest.mark.parametrize("total", [1.0, 5.0, 30.0, 60.0, 600.0])
def test_the_two_segments_always_add_up_to_the_requested_total(total: float) -> None:
    """总预算是权威值：连接段只决定怎么切，永远不会让总预算凭空变大。"""
    connect, read = CurlCffiAdapter(AdapterSettings())._timeout_pair(total)

    assert connect + read == pytest.approx(total)
    assert connect <= total


def test_streaming_gets_a_bigger_budget_than_an_ordinary_request() -> None:
    """流式要在一个预算里收完整段响应体，共用普通请求那份的话大文件必然中途断。"""
    assert DEFAULT_STREAM_TIMEOUT > DEFAULT_DOWNLOAD_TIMEOUT


@pytest.mark.parametrize("absent", [None, 0, 0.0])
def test_a_stream_without_an_explicit_timeout_falls_back_to_the_stream_budget(absent: object) -> None:
    kwargs: dict[str, object] = {"timeout": absent} if absent is not None else {}
    CurlCffiAdapter(AdapterSettings()).apply_stream_timeout(kwargs)

    assert kwargs["timeout"] == DEFAULT_STREAM_TIMEOUT


def test_an_explicit_stream_timeout_is_never_overridden() -> None:
    """调用方给了就用调用方的——兜底只兜"没给"。"""
    kwargs: dict[str, object] = {"timeout": 5.0}
    CurlCffiAdapter(AdapterSettings()).apply_stream_timeout(kwargs)

    assert kwargs["timeout"] == 5.0


def test_the_service_picks_the_budget_by_whether_it_is_streaming() -> None:
    """同一个 _build_download_kwargs 服务两条路，兜底的那个默认值必须分开。"""
    from typing import Any, cast

    from ipclick.dto.proto import task_pb2
    from ipclick.services.task_service import TaskService

    service = cast(Any, object.__new__(TaskService))
    service.adapter_settings = AdapterSettings()
    request = task_pb2.ReqTask(url="https://example.com/big.bin")

    assert service._build_download_kwargs(request)["timeout"] == DEFAULT_DOWNLOAD_TIMEOUT
    assert service._build_download_kwargs(request, stream=True)["timeout"] == DEFAULT_STREAM_TIMEOUT
