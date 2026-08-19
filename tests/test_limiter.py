from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import threading
import time

import pytest

from ipclick.exceptions import ConfigError
from ipclick.limiter import HostLimiter, HostLimitTimeout, LimiterSettings, build_limiter, cluster_share, host_of


def test_disabled_by_default() -> None:
    settings = LimiterSettings.from_config({})
    assert settings.enabled is False
    assert settings.burst == 0


def test_burst_defaults_to_ceil_of_qps() -> None:
    assert LimiterSettings.from_config({"rate_limit": {"per_host_qps": 2.4}}).burst == 3
    assert LimiterSettings.from_config({"rate_limit": {"per_host_qps": 2.4, "per_host_burst": 10}}).burst == 10


@pytest.mark.parametrize(
    "config",
    [
        {"concurrency": {"per_host_max_concurrent": True}},
        {"concurrency": {"per_host_max_concurrent": "many"}},
        {"concurrency": {"per_host_max_concurrent": -1}},
        {"rate_limit": {"per_host_qps": "fast"}},
        {"rate_limit": {"per_host_qps": -0.5}},
        {"concurrency": {"per_host_idle_ttl": 0}},
        {"concurrency": {"max_tracked_hosts": 4}},
    ],
)
def test_invalid_values_raise_config_error(config: dict[str, object]) -> None:
    with pytest.raises(ConfigError):
        LimiterSettings.from_config(config)


def test_unknown_rate_limit_backend_is_refused() -> None:
    with pytest.raises(ConfigError, match="限流后端"):
        build_limiter({"rate_limit": {"backend": "redis"}})


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://Example.COM/p", "example.com"),
        ("https://1.2.3.4:8443/p", "1.2.3.4"),
        ("not a url", ""),
        ("http://[::1]/p", "::1"),
    ],
)
def test_host_of(url: str, expected: str) -> None:
    assert host_of(url) == expected


def test_disabled_limiter_is_a_no_op() -> None:
    limiter = HostLimiter(LimiterSettings())
    with limiter.acquire("http://example.com/a"):
        pass
    assert limiter.snapshot()["tracked_hosts"] == 0


def test_concurrency_cap_is_enforced_per_host() -> None:
    limiter = HostLimiter(LimiterSettings(per_host_max_concurrent=2, wait_timeout=0.05))
    entered = threading.Event()
    release = threading.Event()

    def hold() -> None:
        with limiter.acquire("http://example.com/a"):
            entered.set()
            release.wait(2)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(hold)
        second = pool.submit(hold)
        assert entered.wait(2)
        while limiter.snapshot()["active"].get("example.com", 0) < 2:
            time.sleep(0.005)

        with pytest.raises(HostLimitTimeout, match="并发额度"), limiter.acquire("http://example.com/b"):
            pass

        with limiter.acquire("http://other.com/a"):
            pass

        release.set()
        first.result(timeout=2)
        second.result(timeout=2)

    assert limiter.snapshot()["active"] == {}


def test_token_bucket_paces_requests_beyond_the_burst() -> None:
    limiter = HostLimiter(LimiterSettings(per_host_qps=50.0, per_host_burst=1, wait_timeout=5.0))
    started = time.monotonic()
    for _ in range(4):
        with limiter.acquire("http://example.com/a"):
            pass
    elapsed = time.monotonic() - started

    assert elapsed >= 3 / 50.0


def test_token_bucket_timeout_is_reported() -> None:
    limiter = HostLimiter(LimiterSettings(per_host_qps=1.0, per_host_burst=1, wait_timeout=0.0))
    with limiter.acquire("http://example.com/a"):
        pass
    with pytest.raises(HostLimitTimeout, match="速率额度"), limiter.acquire("http://example.com/a"):
        pass


def test_cluster_sharding_splits_the_configured_qps() -> None:
    limiter = HostLimiter(LimiterSettings(per_host_qps=8.0, per_host_burst=4))
    assert limiter.effective_qps == 8.0
    assert limiter.effective_burst == 4.0

    limiter.set_cluster_size(4)
    assert limiter.effective_qps == 2.0
    assert limiter.effective_burst == 1.0

    limiter.set_cluster_size(1)
    assert limiter.effective_qps == 8.0


@pytest.mark.parametrize(("qps", "nodes", "expected"), [(0.0, 4, 0.0), (10.0, 0, 10.0), (10.0, 4, 2.5)])
def test_cluster_share(qps: float, nodes: int, expected: float) -> None:
    assert cluster_share(qps, nodes) == expected


def test_idle_hosts_are_swept_when_the_table_is_full() -> None:
    limiter = HostLimiter(LimiterSettings(per_host_max_concurrent=1, max_tracked_hosts=16, idle_ttl=1.0))
    for index in range(20):
        with limiter.acquire(f"http://h{index}.example.com/a"):
            pass
    assert limiter.snapshot()["tracked_hosts"] <= 16


def test_sync_host_table_is_a_hard_limit_while_all_slots_are_active() -> None:
    limiter = HostLimiter(LimiterSettings(per_host_max_concurrent=1, max_tracked_hosts=2, idle_ttl=60.0))
    first = limiter.acquire("http://one.example/a")
    second = limiter.acquire("http://two.example/a")
    first.__enter__()
    second.__enter__()
    try:
        with pytest.raises(HostLimitTimeout, match="host 数已达上限"), limiter.acquire("http://three.example/a"):
            pass
        assert limiter.snapshot()["tracked_hosts"] == 2
    finally:
        second.__exit__(None, None, None)
        first.__exit__(None, None, None)


def test_sync_host_table_evicts_idle_lru_at_capacity() -> None:
    limiter = HostLimiter(LimiterSettings(per_host_max_concurrent=1, max_tracked_hosts=2, idle_ttl=60.0))
    with limiter.acquire("http://old.example/a"):
        pass
    with limiter.acquire("http://new.example/a"):
        pass
    now = time.monotonic()
    with limiter._slots["old.example"].lock:
        limiter._slots["old.example"].last_used = now - 2.0
    with limiter._slots["new.example"].lock:
        limiter._slots["new.example"].last_used = now - 1.0
    with limiter.acquire("http://third.example/a"):
        pass

    assert set(limiter._slots) == {"new.example", "third.example"}


async def test_async_host_table_is_a_hard_limit_while_all_slots_are_active() -> None:
    from ipclick.async_limiter import AsyncHostLimiter

    limiter = AsyncHostLimiter(LimiterSettings(per_host_max_concurrent=1, max_tracked_hosts=2, idle_ttl=60.0))
    first = limiter.acquire("http://one.example/a")
    second = limiter.acquire("http://two.example/a")
    await first.__aenter__()
    await second.__aenter__()
    try:
        with pytest.raises(HostLimitTimeout, match="host 数已达上限"):
            async with limiter.acquire("http://three.example/a"):
                pass
        assert len(limiter) == 2
    finally:
        await second.__aexit__(None, None, None)
        await first.__aexit__(None, None, None)


async def test_async_host_table_evicts_idle_lru_at_capacity() -> None:
    from ipclick.async_limiter import AsyncHostLimiter

    limiter = AsyncHostLimiter(LimiterSettings(per_host_max_concurrent=1, max_tracked_hosts=2, idle_ttl=60.0))
    async with limiter.acquire("http://old.example/a"):
        pass
    async with limiter.acquire("http://new.example/a"):
        pass
    now = time.monotonic()
    limiter._slots["old.example"].last_used = now - 2.0
    limiter._slots["new.example"].last_used = now - 1.0
    async with limiter.acquire("http://third.example/a"):
        pass

    assert set(limiter._slots) == {"new.example", "third.example"}
