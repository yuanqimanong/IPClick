"""按 ``[GENERAL].mode`` 选择单机或集群客户端。

``mode`` 这个配置项从项目一开始就在，但一直没有任何消费方——写 ``"cluster"``
和写 ``"standalone"`` 完全一样。现在 :class:`~ipclick.cluster.ClusterDownloader`
有了，把它接上。

``mode`` 的取值：

* ``standalone``（默认）：连单个 ``[SERVER].host:port``
* ``cluster``：用 ``[CLUSTER]`` 的节点池、负载均衡与故障转移
* ``auto``：配了节点就走集群，没配就单机

放在单独的模块而不是 ``sdk.py`` 里，是为了避免 ``sdk`` 反过来依赖
``cluster``——后者已经 import 了前者。
"""

from typing import Any

from ipclick.config_loader import load_config
from ipclick.exceptions import ConfigError
from ipclick.utils.log_util import log


#: ``[GENERAL].mode`` 的合法取值
CLIENT_MODES: frozenset[str] = frozenset({"standalone", "cluster", "auto"})


def resolve_mode(config: Any) -> str:
    """把 ``[GENERAL].mode`` 解析成 ``standalone`` 或 ``cluster``。

    Raises:
        ConfigError: mode 取值非法，或 ``mode = "cluster"`` 却没配节点。
            静默退回单机的话，以为在用集群、实际所有流量都打在一个节点上，
            而且故障转移完全没有——这种"配了没生效"正是本项目一直在清理的。
    """
    general = dict(config.get("GENERAL", {}))
    mode = str(general.get("mode") or "standalone").strip().lower()
    if mode not in CLIENT_MODES:
        raise ConfigError(f"未知的 [GENERAL].mode {mode!r}，可选：{'、'.join(sorted(CLIENT_MODES))}")

    cluster = dict(config.get("CLUSTER", {}))
    has_nodes = bool(cluster.get("nodes"))
    discovery_mode = str(dict(cluster.get("discovery") or {}).get("mode") or "static").strip().lower()
    has_discovery = discovery_mode != "static"

    if mode == "cluster":
        if not (has_nodes or has_discovery):
            raise ConfigError(
                '[GENERAL].mode = "cluster" 但既没有 [CLUSTER].nodes 也没有配置 [CLUSTER.discovery]。'
                "静默退回单机会让你以为集群生效了，实际所有流量都打在一个节点上、也没有故障转移。"
            )
        return "cluster"

    if mode == "auto":
        return "cluster" if (has_nodes or has_discovery) else "standalone"
    return "standalone"


def create_client(config_path: str | None = None, **kwargs: Any) -> Any:
    """按配置造出 :class:`~ipclick.sdk.Downloader` 或
    :class:`~ipclick.cluster.ClusterDownloader`。

    两者的请求接口一致（``get`` / ``post`` / ``stream`` / ``batch`` / ``download``），
    调用方通常不需要关心拿到的是哪个。
    """
    from ipclick.sdk import Downloader

    config = load_config(config_path)
    mode = resolve_mode(config)

    if mode == "cluster":
        # 延迟导入：cluster 依赖 sdk，顶层导入会形成循环
        from ipclick.cluster import ClusterDownloader

        # host/port 在集群模式下没有意义，传了说明调用方误会了
        for key in ("host", "port"):
            if kwargs.pop(key, None) is not None:
                log.warning(f"集群模式下忽略 {key} 参数——目标地址来自 [CLUSTER] 的节点池")
        log.info("按 [GENERAL].mode 使用集群客户端")
        return ClusterDownloader(config_path=config_path, **kwargs)

    return Downloader(config_path=config_path, **kwargs)


__all__ = ["CLIENT_MODES", "create_client", "resolve_mode"]
