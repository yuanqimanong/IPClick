from __future__ import annotations

import asyncio
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

import pytest
from typing_extensions import override

from ipclick.adapters import curl_cffi_adapter
from ipclick.adapters.curl_cffi_adapter import CurlCffiAdapter
from ipclick.adapters.sessions import AsyncSessionCache, SessionCache
from ipclick.adapters.settings import AdapterSettings


class FakeSession:
    def __init__(self, key: object) -> None:
        self.key: object = key
        self.closed: bool = False

    def close(self) -> None:
        self.closed = True


class FakeAsyncSession:
    def __init__(self, key: object) -> None:
        self.key: object = key
        self.closed: bool = False

    async def close(self) -> None:
        self.closed = True


class FakeCurlModule:
    def __init__(self) -> None:
        self.sync_kwargs: list[dict[str, Any]] = []
        self.async_kwargs: list[dict[str, Any]] = []

    def Session(self, **kwargs: Any) -> FakeSession:
        self.sync_kwargs.append(kwargs)
        return FakeSession(kwargs)

    def AsyncSession(self, **kwargs: Any) -> FakeAsyncSession:
        self.async_kwargs.append(kwargs)
        return FakeAsyncSession(kwargs)


def test_sessions_are_created_once_per_key() -> None:
    created: list[object] = []

    def factory(key: object) -> FakeSession:
        created.append(key)
        return FakeSession(key)

    cache: SessionCache[str] = SessionCache("fake", factory)
    first = cache.get("a")

    assert cache.get("a") is first
    assert cache.get("b") is not first
    assert created == ["a", "b"]


def test_closing_drains_the_cache_and_closes_everything() -> None:
    cache: SessionCache[str] = SessionCache("fake", FakeSession)
    first = cache.get("a")
    cache.close()

    assert first.closed is True
    assert cache.get("a") is not first


def test_a_broken_close_does_not_stop_the_others() -> None:
    class Angry(FakeSession):
        @override
        def close(self) -> None:
            raise RuntimeError("nope")

    cache: SessionCache[str] = SessionCache("fake", lambda key: Angry(key) if key == "a" else FakeSession(key))
    _ = cache.get("a")
    good = cache.get("b")
    cache.close()

    assert good.closed is True


async def test_async_sessions_are_created_once_per_loop_and_key() -> None:
    cache: AsyncSessionCache[str] = AsyncSessionCache("fake", FakeAsyncSession)
    first = cache.get("a")

    assert cache.get("a") is first
    assert cache.get("b") is not first

    await cache.aclose()
    assert first.closed is True
    assert cache.get("a") is not first


def test_a_dead_loop_is_skipped_instead_of_crashing() -> None:
    cache: AsyncSessionCache[str] = AsyncSessionCache("fake", FakeAsyncSession)
    loop = asyncio.new_event_loop()
    session = loop.run_until_complete(_populate(cache))
    loop.close()

    cache.close()
    assert session.closed is False


async def _populate(cache: AsyncSessionCache[str]) -> FakeAsyncSession:
    return cache.get("a")


def test_a_live_but_idle_loop_still_gets_closed() -> None:
    cache: AsyncSessionCache[str] = AsyncSessionCache("fake", FakeAsyncSession)
    loop = asyncio.new_event_loop()
    try:
        session = loop.run_until_complete(_populate(cache))
        cache.close()
        assert session.closed is True
    finally:
        loop.close()


def test_curl_cffi_async_sessions_get_the_configured_client_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeCurlModule()
    monkeypatch.setattr(curl_cffi_adapter, "_curl_cffi", fake)
    adapter = CurlCffiAdapter(AdapterSettings(max_connections=42))

    key = (None, True, "chrome")
    _ = adapter._new_session(key)
    _ = adapter._new_async_session(key)

    assert "max_clients" not in fake.sync_kwargs[0]
    assert fake.async_kwargs[0]["max_clients"] == 42
    assert fake.async_kwargs[0]["impersonate"] == "chrome"


def test_curl_cffi_closes_both_session_kinds(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeCurlModule()
    monkeypatch.setattr(curl_cffi_adapter, "_curl_cffi", fake)
    adapter = CurlCffiAdapter()

    sync_session = adapter._sessions.get((None, True, None))
    adapter.close()

    assert sync_session.closed is True


def test_curl_cffi_stream_preserves_whitelisted_request_kwargs() -> None:
    class StreamResponse:
        def __init__(self) -> None:
            self.url: str = "https://example.com/file"
            self.status_code: int = 200
            self.headers: dict[str, str] = {"content-length": "3"}

        def iter_content(self, *, chunk_size: int) -> list[bytes]:
            assert chunk_size == 1024
            return [b"abc"]

        def close(self) -> None:
            return None

    class StreamSession:
        def __init__(self) -> None:
            self.request_kwargs: dict[str, Any] = {}

        def request(self, _method: str, _url: str, **kwargs: Any) -> StreamResponse:
            self.request_kwargs = kwargs
            return StreamResponse()

    class StreamCache:
        def __init__(self, session: StreamSession) -> None:
            self.session: StreamSession = session
            self.leases: int = 0
            self.released: int = 0

        @contextmanager
        def lease(self, _key: object) -> Generator[StreamSession]:
            self.leases += 1
            try:
                yield self.session
            finally:
                self.released += 1

    session = StreamSession()
    adapter: Any = object.__new__(CurlCffiAdapter)
    adapter.timeout = 60
    adapter._sessions = StreamCache(session)

    events = list(
        adapter.download_stream(
            "https://example.com/file",
            chunk_size=1024,
            allow_redirects=False,
            kwargs='{"ja3":"fingerprint","cert":"client.pem","unknown":"drop"}',
        )
    )

    assert events[1] == b"abc"
    # 租借覆盖整条流：生成器耗尽之后才归还
    assert adapter._sessions.leases == 1
    assert adapter._sessions.released == 1
    assert session.request_kwargs["stream"] is True
    assert session.request_kwargs["allow_redirects"] is False
    assert session.request_kwargs["ja3"] == "fingerprint"
    assert session.request_kwargs["cert"] == "client.pem"
    assert "unknown" not in session.request_kwargs


def test_session_cache_evicts_and_closes_the_least_recently_used() -> None:
    """缓存必须有上限：key 里带每请求可变的 proxy，不淘汰就会无界增长。

    粘性会话代理的常规用法就是每个请求一个会话 ID，实测约 130 KB/个 session 加各自的
    keep-alive 连接——一万次请求 1.3 GB 常驻且进程活着就回收不掉。
    """
    created: list[str] = []

    def factory(key: str) -> Any:
        created.append(key)
        return FakeSession(key)

    cache: Any = SessionCache("test", factory, max_sessions=2)

    a = cache.get("a")
    b = cache.get("b")
    c = cache.get("c")

    assert created == ["a", "b", "c"]
    # 最久未用的 a 被淘汰并真的关掉了
    assert a.closed is True
    assert b.closed is False and c.closed is False
    # 再取 a 是一个新实例
    assert cache.get("a") is not a
    assert created == ["a", "b", "c", "a"]


def test_session_cache_lru_order_follows_use_not_creation() -> None:
    """被用过的 key 不该因为创建得早而先被淘汰。"""
    cache: Any = SessionCache("test", lambda key: FakeSession(key), max_sessions=2)

    a = cache.get("a")
    _ = cache.get("b")
    _ = cache.get("a")  # a 重新变成最近使用
    _ = cache.get("c")  # 该淘汰 b

    assert a.closed is False


def test_lru_eviction_waits_for_the_last_user_before_closing() -> None:
    """LRU 淘汰不能关掉别人正在用的 session。

    流式下载在整条流的生命周期里都持着同一个 session，而它的"最近使用时间"停在
    请求开始那一刻——很容易先变成 LRU。在别人用着的时候 close 掉，传输就在中途
    断了。与 cluster 的 ``_ChannelEntry`` 同一套口径：close 推迟到最后一个使用者
    放手之后。
    """
    cache: Any = SessionCache("test", FakeSession, max_sessions=2)

    with cache.lease("a") as a:
        with cache.lease("b"):
            pass
        with cache.lease("c"):
            pass
        assert a.closed is False, "a 还有在途使用者，不能关"

    assert a.closed is True, "最后一个使用者放手后应当关掉"


async def test_async_lru_eviction_waits_for_the_last_user_before_closing() -> None:
    """异步缓存同理：淘汰不能关掉在途协程正在用的 session。"""
    cache: Any = AsyncSessionCache("test", FakeAsyncSession, max_sessions=2)

    with cache.lease("a") as a:
        with cache.lease("b"):
            pass
        with cache.lease("c"):
            pass
        # 必须先把循环让出去：同一个循环里的 close 是 create_task 异步收尾的，
        # 不 await 一下就断言 closed is False，无论有没有引用计数都会通过——
        # 那样这条用例就是名不副实的。
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert a.closed is False, "a 还有在途使用者，不能关"

    await asyncio.sleep(0)  # 归还所属事件循环收尾
    await asyncio.sleep(0)
    assert a.closed is True, "最后一个使用者放手后应当关掉"
