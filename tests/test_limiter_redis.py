"""分布式限流（Redis 后端）。

用 fakeredis 跑，包括 Lua 脚本——限流器的正确性全在那两段 Lua 的原子性上，
不真跑一遍等于没测。
"""

from concurrent.futures import ThreadPoolExecutor
import threading
import time

import pytest

from ipclick.exceptions import ConfigError
from ipclick.limiter import HostLimiter, HostLimitTimeout, LimiterSettings
from ipclick.limiter_redis import RedisHostLimiter, RedisSettings, build_limiter


fakeredis = pytest.importorskip("fakeredis", reason="分布式限流测试需要 fakeredis")


@pytest.fixture
def client():
    return fakeredis.FakeStrictRedis()


def _limiter(client, **overrides) -> RedisHostLimiter:
    settings = LimiterSettings(**overrides)
    return RedisHostLimiter(settings, RedisSettings(slot_ttl=60), client=client)


class TestBackendSelection:
    def test_memory_is_default(self):
        assert isinstance(build_limiter({}), HostLimiter)

    def test_memory_aliases(self):
        assert isinstance(build_limiter({"rate_limit": {"backend": "local"}}), HostLimiter)

    def test_unknown_backend_raises(self):
        """静默回退到内存后端的话，集群里以为开了共享限额、实际每个节点各算各的，
        问题要到把对方站点打挂才会暴露。"""
        with pytest.raises(ConfigError, match="未知的限流后端"):
            build_limiter({"rate_limit": {"backend": "memcached"}})

    def test_redis_settings_parsed(self):
        s = RedisSettings.from_config(
            {
                "redis_url": "redis://h:1/2",
                "redis_slot_ttl": 120,
                "redis_socket_timeout": 1,
                "redis_key_prefix": "x",
            }
        )
        assert (s.url, s.slot_ttl, s.socket_timeout, s.key_prefix) == ("redis://h:1/2", 120.0, 1.0, "x")

    def test_bad_values_fall_back(self):
        s = RedisSettings.from_config({"redis_slot_ttl": "abc", "redis_socket_timeout": -1})
        assert (s.slot_ttl, s.socket_timeout) == (600.0, 3.0)


class TestDisabled:
    def test_no_limits_is_noop(self, client):
        limiter = _limiter(client)
        with limiter.acquire("http://a.com/x"):
            pass
        assert not client.keys("*"), "未启用时不该往 Redis 里写任何东西"

    def test_url_without_host(self, client):
        with _limiter(client, per_host_max_concurrent=1).acquire("garbage"):
            pass


class TestConcurrency:
    def test_caps_in_flight(self, client):
        limiter = _limiter(client, per_host_max_concurrent=3, wait_timeout=10)
        peak = current = 0
        lock = threading.Lock()

        def work(_i: int) -> None:
            nonlocal peak, current
            with limiter.acquire("http://a.com/x"):
                with lock:
                    current += 1
                    peak = max(peak, current)
                time.sleep(0.02)
                with lock:
                    current -= 1

        with ThreadPoolExecutor(12) as pool:
            list(pool.map(work, range(24)))
        assert peak <= 3, f"并发峰值 {peak} 超过上限 3"
        assert peak > 1, "峰值恒为 1 说明没并发起来，断言就没意义"

    def test_shared_across_limiter_instances(self, client):
        """核心价值：两个"进程"（这里用两个 limiter 实例模拟）共用同一份额度。

        内存后端在这里必然失败——那正是要用 Redis 的原因。
        """
        a = _limiter(client, per_host_max_concurrent=1, wait_timeout=0.3)
        b = _limiter(client, per_host_max_concurrent=1, wait_timeout=0.3)
        with (
            a.acquire("http://a.com/x"),
            pytest.raises(HostLimitTimeout, match="并发额度"),
            b.acquire("http://a.com/x"),
        ):
            pass

    def test_different_hosts_independent(self, client):
        a = _limiter(client, per_host_max_concurrent=1, wait_timeout=0.5)
        with a.acquire("http://a.com/x"), a.acquire("http://b.com/x"):
            pass

    def test_slot_released_on_exception(self, client):
        limiter = _limiter(client, per_host_max_concurrent=1, wait_timeout=0.5)
        for _ in range(3):
            with pytest.raises(RuntimeError), limiter.acquire("http://a.com/x"):
                raise RuntimeError("boom")
        with limiter.acquire("http://a.com/x"):
            pass

    def test_stale_holder_is_reclaimed(self, client):
        """进程拿着名额崩了，名额不能永远还不回来——那个 host 会被锁死。"""
        limiter = RedisHostLimiter(
            LimiterSettings(per_host_max_concurrent=1, wait_timeout=2),
            RedisSettings(slot_ttl=0.2),
            client=client,
        )
        # 直接往 ZSET 里塞一个"很久以前"的持有者，模拟崩掉的进程
        client.zadd(limiter._slot_key("a.com"), {b"dead-process": int((time.time() - 100) * 1000)})
        with limiter.acquire("http://a.com/x"):
            pass

    def test_release_removes_holder(self, client):
        limiter = _limiter(client, per_host_max_concurrent=2)
        key = limiter._slot_key("a.com")
        with limiter.acquire("http://a.com/x"):
            assert client.zcard(key) == 1
        assert client.zcard(key) == 0


class TestRateLimit:
    def test_paces_requests(self, client):
        limiter = _limiter(client, per_host_qps=10, per_host_burst=1, wait_timeout=5)
        start = time.monotonic()
        for _ in range(4):
            with limiter.acquire("http://a.com/x"):
                pass
        assert time.monotonic() - start >= 0.25, "限速没生效"

    def test_burst_allowed_upfront(self, client):
        limiter = _limiter(client, per_host_qps=1, per_host_burst=5, wait_timeout=5)
        start = time.monotonic()
        for _ in range(5):
            with limiter.acquire("http://a.com/x"):
                pass
        assert time.monotonic() - start < 0.6

    def test_shared_bucket_across_instances(self, client):
        """两个实例共用同一个令牌桶——这是分布式限速的意义。"""
        a = _limiter(client, per_host_qps=1, per_host_burst=1, wait_timeout=0.2)
        b = _limiter(client, per_host_qps=1, per_host_burst=1, wait_timeout=0.2)
        with a.acquire("http://a.com/x"):
            pass
        with pytest.raises(HostLimitTimeout, match="速率额度"), b.acquire("http://a.com/x"):
            pass

    def test_separate_buckets_per_host(self, client):
        limiter = _limiter(client, per_host_qps=1, per_host_burst=1, wait_timeout=0.2)
        with limiter.acquire("http://a.com/x"):
            pass
        with limiter.acquire("http://b.com/x"):
            pass


class TestFailureHandling:
    def test_redis_outage_fails_open(self, client):
        """Redis 挂了应当**放行**，而不是把所有请求拒掉。

        限流是保护性措施，让它的故障演变成全站不可用是本末倒置。
        但必须留日志，否则限流悄悄失效没人知道。
        """

        class _Broken:
            def register_script(self, _src: str):
                def _fail(**_kwargs):
                    raise ConnectionError("redis is down")

                return _fail

            def zrem(self, *_args):
                raise ConnectionError("redis is down")

        limiter = RedisHostLimiter(
            LimiterSettings(per_host_max_concurrent=1, per_host_qps=1, wait_timeout=1),
            RedisSettings(),
            client=_Broken(),
        )

        # 本项目用 loguru，它不走 pytest 的 caplog，得直接挂 loguru 的 sink
        from loguru import logger

        messages: list[str] = []
        sink_id = logger.add(lambda m: messages.append(str(m)), level="ERROR")
        try:
            start = time.monotonic()
            with limiter.acquire("http://a.com/x"):
                pass
            elapsed = time.monotonic() - start
        finally:
            logger.remove(sink_id)

        assert elapsed < 0.5, "Redis 挂了应当立刻放行，而不是一路轮询到超时"
        assert any("限流后端不可用" in m for m in messages), "Redis 故障必须留下日志"

    def test_missing_redis_library_reports_extra(self, monkeypatch: pytest.MonkeyPatch):
        import ipclick.limiter_redis as mod

        monkeypatch.setattr(mod, "_redis", None)
        with pytest.raises(ConfigError, match=r"ipclick\[redis\]"):
            RedisHostLimiter()


class TestSnapshot:
    def test_reports_backend(self, client):
        snap = _limiter(client, per_host_max_concurrent=2).snapshot()
        assert snap["backend"] == "redis"
        assert snap["per_host_max_concurrent"] == 2
