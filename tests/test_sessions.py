from __future__ import annotations

import asyncio
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

        def get(self, _key: object) -> StreamSession:
            return self.session

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
    assert session.request_kwargs["stream"] is True
    assert session.request_kwargs["allow_redirects"] is False
    assert session.request_kwargs["ja3"] == "fingerprint"
    assert session.request_kwargs["cert"] == "client.pem"
    assert "unknown" not in session.request_kwargs
