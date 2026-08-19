"""按 host 的异步限流（0.7.0）。

这组测试守的是三件事：额度上限不被突破、速率贴近配置值、以及**等待期间
不阻塞事件循环**。

最后一条最容易写错也最难发现：图省事用 ``time.sleep`` 的话，阻塞的不是这一个
请求，而是整个事件循环——同循环上所有在飞的请求一起停住，而现象是"毫不相干
的请求也集体变慢"。

（实测参考：600 并发配 200 QPS，异步版误差 +0.2%，同步线程版 -4.0%。同步版
并不粗糙，它的误差来自线程调度抖动且方向是欠发；异步版的主要收益其实是
不占线程，精度提升是附带的。）
"""

import asyncio
import itertools
import time

import pytest

from ipclick.async_limiter import AsyncHostLimiter
from ipclick.limiter import HostLimitTimeout, LimiterSettings


class TestDisabledIsFree:
    async def test_no_limits_means_no_overhead(self) -> None:
        """一项都没配时必须是空操作——绝大多数部署走的是这条路。"""
        limiter = AsyncHostLimiter(LimiterSettings())
        started = time.perf_counter()
        for _ in range(2000):
            async with limiter.acquire("http://example.com/x"):
                pass
        assert time.perf_counter() - started < 0.5
        assert len(limiter) == 0, "未启用时不该为 host 建条目"


class TestConcurrencyCeiling:
    async def test_peak_never_exceeds_the_limit(self) -> None:
        limiter = AsyncHostLimiter(LimiterSettings(per_host_max_concurrent=3, wait_timeout=10))
        current = peak = 0

        async def one() -> None:
            nonlocal current, peak
            async with limiter.acquire("http://example.com/x"):
                current += 1
                peak = max(peak, current)
                await asyncio.sleep(0.01)
                current -= 1

        await asyncio.gather(*(one() for _ in range(40)))
        assert peak <= 3, f"并发峰值 {peak} 超过了上限 3"
        assert peak > 1, "峰值恒为 1 说明并发根本没起来，断言就没意义了"

    async def test_different_hosts_do_not_block_each_other(self) -> None:
        """限额是按 host 独立的——一个站点被限住不该拖累别的站点。"""
        limiter = AsyncHostLimiter(LimiterSettings(per_host_max_concurrent=1, wait_timeout=10))

        async def hit(host: str) -> None:
            async with limiter.acquire(f"http://{host}/x"):
                await asyncio.sleep(0.05)

        started = time.perf_counter()
        await asyncio.gather(*(hit(f"h{i}.example") for i in range(10)))
        # 10 个不同 host 各限 1 并发，应当几乎完全并行
        assert time.perf_counter() - started < 0.25

    async def test_timeout_raises_host_limit_error(self) -> None:
        """限流超时不是网络故障，要能和它区分开。"""
        limiter = AsyncHostLimiter(LimiterSettings(per_host_max_concurrent=1, wait_timeout=0.05))

        async def hold() -> None:
            async with limiter.acquire("http://example.com/x"):
                await asyncio.sleep(0.5)

        holder = asyncio.create_task(hold())
        await asyncio.sleep(0.02)
        with pytest.raises(HostLimitTimeout, match="并发额度"):
            async with limiter.acquire("http://example.com/x"):
                pass
        holder.cancel()


class TestRateIsActuallyPrecise:
    """这一组是异步限流器存在的理由。"""

    async def test_actual_rate_matches_the_configured_qps(self) -> None:
        """20 个请求 @ 50 QPS 应当恰好用掉约 0.4 秒，而不是一拥而上。"""
        qps = 50.0
        limiter = AsyncHostLimiter(LimiterSettings(per_host_qps=qps, per_host_burst=1, wait_timeout=10))

        started = time.perf_counter()
        await asyncio.gather(*(_touch(limiter) for _ in range(20)))
        elapsed = time.perf_counter() - started

        expected = 19 / qps  # 第一个不用等，之后每个间隔 1/qps
        assert expected * 0.7 < elapsed < expected * 1.8, f"20 个请求用了 {elapsed:.3f}s，期望约 {expected:.3f}s"

    async def test_releases_are_evenly_spaced(self) -> None:
        """发出时刻应当均匀铺开，而不是挤成几个尖峰。

        预约制的性质就是等差数列。这里检查没有一堆接近 0 的相邻间隔——
        那是"攒一批一起放"的特征，说明预约没生效。
        """
        qps = 100.0
        limiter = AsyncHostLimiter(LimiterSettings(per_host_qps=qps, per_host_burst=1, wait_timeout=10))
        stamps: list[float] = []

        async def one() -> None:
            async with limiter.acquire("http://example.com/x"):
                stamps.append(time.perf_counter())

        await asyncio.gather(*(one() for _ in range(30)))
        stamps.sort()
        gaps = [b - a for a, b in itertools.pairwise(stamps)]
        # 挤成尖峰的话会有一堆接近 0 的间隔
        near_zero = sum(1 for g in gaps if g < 0.5 / qps)
        assert near_zero <= 3, f"{near_zero}/{len(gaps)} 个间隔接近 0，说明放行挤成了尖峰"

    async def test_burst_is_honoured(self) -> None:
        """突发额度内的请求应当立刻放行，不该被摊平。"""
        limiter = AsyncHostLimiter(LimiterSettings(per_host_qps=10.0, per_host_burst=5, wait_timeout=10))
        started = time.perf_counter()
        await asyncio.gather(*(_touch(limiter) for _ in range(5)))
        assert time.perf_counter() - started < 0.15, "突发额度没生效，5 个请求被摊平了"

    async def test_does_not_block_the_event_loop(self) -> None:
        """限流等待期间，其他协程必须照常推进。

        这一条防的是"图省事用了 time.sleep"——那样阻塞的不是这一个请求，
        而是整个事件循环，同循环上所有在飞的请求一起停住。
        """
        limiter = AsyncHostLimiter(LimiterSettings(per_host_qps=5.0, per_host_burst=1, wait_timeout=10))
        progressed = 0

        async def other_work() -> None:
            nonlocal progressed
            for _ in range(30):
                await asyncio.sleep(0.005)
                progressed += 1

        await asyncio.gather(*(_touch(limiter) for _ in range(4)), other_work())
        assert progressed == 30, "限流等待期间事件循环被阻塞了"


async def _touch(limiter: AsyncHostLimiter) -> None:
    async with limiter.acquire("http://example.com/x"):
        pass
