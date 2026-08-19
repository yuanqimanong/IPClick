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
