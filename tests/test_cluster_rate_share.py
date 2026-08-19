"""客户端分发下的 QPS 分片（0.7.0）。

要解决的问题：``[CLUSTER].forward = "off"`` 时调用方直连每一台节点，每台
各算各的限额，加起来是 **N × per_host_qps**——配了 100 QPS、部署四台，
目标站点实际挨 400。这是"配了限流还是被封"的典型原因，而且**从任何单台
机器的视角看都完全正常**，所以极难自查。

服务端转发（``forward = "on"``）不受影响：所有任务过入口节点，在那一台上
算就是全局的。
"""

import pytest

from ipclick.async_limiter import AsyncHostLimiter
from ipclick.limiter import HostLimiter, LimiterSettings, cluster_share


class TestShareMath:
    @pytest.mark.parametrize(
        ("qps", "nodes", "expected"),
        [
            (100.0, 4, 25.0),
            (100.0, 1, 100.0),
            (100.0, 0, 100.0),  # 0 个节点按"就我自己"处理，不该除以零
            (10.0, 3, pytest.approx(10 / 3)),
        ],
    )
    def test_divides_evenly(self, qps: float, nodes: int, expected: float) -> None:
        assert cluster_share(qps, nodes) == expected

    def test_unlimited_stays_unlimited(self) -> None:
        """没配限速就不该凭空造出一个限速。"""
        assert cluster_share(0.0, 8) == 0.0


class TestLimiterIntegration:
    @pytest.mark.parametrize("cls", [HostLimiter, AsyncHostLimiter])
    def test_default_is_unsharded(self, cls: type) -> None:
        """不调 set_cluster_size 就等于单机——分片必须是显式行为。"""
        limiter = cls(LimiterSettings(per_host_qps=100.0))
        assert limiter.effective_qps == 100.0

    @pytest.mark.parametrize("cls", [HostLimiter, AsyncHostLimiter])
    def test_share_applies(self, cls: type) -> None:
        limiter = cls(LimiterSettings(per_host_qps=100.0))
        limiter.set_cluster_size(4)
        assert limiter.effective_qps == 25.0

    @pytest.mark.parametrize("cls", [HostLimiter, AsyncHostLimiter])
    def test_single_node_is_not_sharded(self, cls: type) -> None:
        """只剩一个节点时应当拿回全部额度，而不是继续守着 1/N。"""
        limiter = cls(LimiterSettings(per_host_qps=100.0))
        limiter.set_cluster_size(4)
        limiter.set_cluster_size(1)
        assert limiter.effective_qps == 100.0

    @pytest.mark.parametrize("cls", [HostLimiter, AsyncHostLimiter])
    def test_share_grows_when_nodes_die(self, cls: type) -> None:
        """节点挂掉后幸存者自动分到更多——这是"自适应"的全部意义。

        做不到的话，四台挂到只剩一台时，那一台仍只用 1/4 的额度，
        集群整体吞吐掉到四分之一，而没有任何地方报错。
        """
        limiter = cls(LimiterSettings(per_host_qps=120.0))
        limiter.set_cluster_size(4)
        assert limiter.effective_qps == 30.0
        limiter.set_cluster_size(2)
        assert limiter.effective_qps == 60.0

    @pytest.mark.parametrize("cls", [HostLimiter, AsyncHostLimiter])
    def test_unlimited_is_not_turned_into_a_limit(self, cls: type) -> None:
        """per_host_qps = 0（默认）时分片不该凭空造出限速。"""
        limiter = cls(LimiterSettings())
        limiter.set_cluster_size(8)
        assert limiter.effective_qps == 0.0


class TestActualRateAfterSharding:
    async def test_async_limiter_honours_the_share(self) -> None:
        """分片之后实际速率要按份额走，不是按配置的全局值。"""
        import time

        limiter = AsyncHostLimiter(LimiterSettings(per_host_qps=100.0, per_host_burst=1, wait_timeout=30))
        limiter.set_cluster_size(4)  # 本节点 25 QPS

        started = time.perf_counter()
        for _ in range(10):
            async with limiter.acquire("http://example.com/x"):
                pass
        elapsed = time.perf_counter() - started

        expected = 9 / 25.0  # 9 个间隔 @ 25 QPS
        assert expected * 0.7 < elapsed < expected * 1.8, (
            f"10 个请求用了 {elapsed:.2f}s，按份额 25 QPS 应当约 {expected:.2f}s——"
            "若接近 0.09s 说明分片没生效，仍在按 100 QPS 跑"
        )


class TestShardingIsWiredToTheRightLimiter:
    """份额必须设到**真正参与限流的那个**对象上。

    异步服务继承了同步的 ``host_limiter``，但实际用的是自己那个 asyncio 版。
    接错的话份额会被设到一个根本不参与限流的对象上——**功能静默失效**，
    而日志还会照常打出"分片已启用"，是最难发现的一类 bug。
    """

    def test_sync_service_targets_the_sync_limiter(self) -> None:
        from ipclick.limiter import HostLimiter
        from ipclick.services.task_service import TaskService
        from ipclick.utils.config_util import Settings

        limiters = TaskService(Settings({})).limiters_for_sharding()
        assert limiters and isinstance(limiters[0], HostLimiter)

    def test_async_service_targets_the_async_limiter(self) -> None:
        from ipclick.async_limiter import AsyncHostLimiter
        from ipclick.services.async_task_service import AsyncTaskService
        from ipclick.utils.config_util import Settings

        limiters = AsyncTaskService(Settings({})).limiters_for_sharding()
        assert limiters and isinstance(limiters[0], AsyncHostLimiter), (
            "异步服务把份额接到了同步限流器上——那个对象在异步模式下根本不参与限流"
        )

    def test_pool_callback_reaches_the_limiter(self) -> None:
        """端到端：节点池报告健康数 → 限流器份额跟着变。"""
        from ipclick.limiter import HostLimiter, LimiterSettings

        limiter = HostLimiter(LimiterSettings(per_host_qps=100.0))
        callbacks: list = []
        callbacks.append(limiter.set_cluster_size)  # 模拟 pool.on_health_change

        for callback in callbacks:
            callback(4)
        assert limiter.effective_qps == 25.0
        for callback in callbacks:
            callback(2)
        assert limiter.effective_qps == 50.0


class TestBurstIsShardedToo:
    """分片必须**同时**切稳态速率和桶容量。

    只切速率不切容量的漏法极难自查：稳态完全正确（压测一分钟取平均看不出任何
    问题），只在**流量刚起来的那一下**暴露——每个节点都攒着一整份未分片的突发
    额度，N 台一起放出来就是 N 倍。而那一刻恰恰是目标站点风控最容易触发的时候，
    于是现象是"平时好好的，一重启 / 一扩容就被封"。
    """

    @staticmethod
    def _both(qps: float, nodes: int) -> tuple[float, float]:
        settings = LimiterSettings.from_config({"rate_limit": {"per_host_qps": qps}})
        sync_limiter = HostLimiter(settings)
        sync_limiter.set_cluster_size(nodes)
        async_limiter = AsyncHostLimiter(settings)
        async_limiter.set_cluster_size(nodes)
        assert sync_limiter.effective_burst == async_limiter.effective_burst, "同步与异步的分片结果必须一致"
        return sync_limiter.effective_qps, sync_limiter.effective_burst

    @pytest.mark.parametrize("nodes", [1, 2, 4, 10])
    def test_cluster_wide_burst_stays_within_budget(self, nodes: int) -> None:
        """把每节点的桶容量乘回节点数，不能超过配置的总额度。"""
        _, burst = self._both(100, nodes)
        assert burst * nodes <= 100 + 1e-9, f"{nodes} 个节点瞬时能放出 {burst * nodes}，预算只有 100"

    @pytest.mark.parametrize("nodes", [1, 2, 4, 10])
    def test_steady_rate_still_adds_up(self, nodes: int) -> None:
        """切容量不能把稳态速率也切坏了。"""
        qps, _ = self._both(100, nodes)
        assert qps * nodes == pytest.approx(100)

    def test_burst_and_qps_shrink_by_the_same_ratio(self) -> None:
        qps, burst = self._both(100, 4)
        assert qps == pytest.approx(25)
        assert burst == pytest.approx(25)

    def test_unsharded_keeps_the_configured_burst(self) -> None:
        """单机（或未调用 set_cluster_size）时不该有任何变化。"""
        settings = LimiterSettings.from_config({"rate_limit": {"per_host_qps": 100}})
        assert HostLimiter(settings).effective_burst == float(settings.burst)
        assert AsyncHostLimiter(settings).effective_burst == float(settings.burst)

    def test_never_shrinks_to_zero(self) -> None:
        """容量为 0 的令牌桶永远拿不到令牌——那是挂死，不是限流。

        节点数极多时宁可让总突发略微超预算，也不能造出一个不可能通过的闸门。
        """
        _, burst = self._both(100, 128)
        assert burst >= 1.0

    def test_explicit_burst_is_sharded_as_well(self) -> None:
        """显式配的 per_host_burst 同样是"全集群的额度"，一样要切。"""
        settings = LimiterSettings.from_config({"rate_limit": {"per_host_qps": 50, "per_host_burst": 200}})
        limiter = HostLimiter(settings)
        limiter.set_cluster_size(4)
        assert limiter.effective_burst == pytest.approx(50)
