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

import threading
from typing import Any

from typing_extensions import override

from ipclick.config_loader import load_config
from ipclick.exceptions import ConfigError
from ipclick.utils.log_util import log
from ipclick.utils.secure_util import SecureUtil


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


# ---------------------------------------------------------------------- #
# 缓存实例与全局代理
#
# 这几个原本在 sdk.py 里，但让 sdk 去解释 [GENERAL].mode 就得 import 本模块，
# 而本模块要 import cluster、cluster 又 import sdk——形成导入环。放在这里，
# 依赖方向就只剩单向的 factory -> {sdk, cluster}。
# 公开路径 `from ipclick import downloader / get_downloader` 完全不变。
# ---------------------------------------------------------------------- #

#: 值可能是 Downloader 或 ClusterDownloader，取决于 [GENERAL].mode。
#: 用普通 dict：defaultdict 会在任何一次误访问时凭空造出一个客户端。
_downloader_cache: dict[str, Any] = {}
_cache_lock = threading.Lock()


def get_downloader(config_path: str | None = None, host: str | None = None, port: int | None = None) -> Any:
    """获取（并缓存）下载器实例。相同参数返回同一个实例，以复用 gRPC 连接。

    **按 [GENERAL].mode 决定返回单机还是集群客户端**，与 :func:`create_client`
    一致。显式给了 host/port 则总是单机——那是在点名某个具体节点。

    回归：这里以前硬编码 Downloader。于是配了 mode = "cluster" 的人只要用
    ``from ipclick import downloader`` 就会静默拿到单机客户端——所有流量打在
    一个节点上、没有故障转移，而 create_client() 那边却明确拒绝这种静默降级。
    同一个配置项在两条路径上表现不同，比不支持还糟。
    """
    key = SecureUtil.md5([config_path, host, port])
    instance = _downloader_cache.get(key)
    if instance is not None:
        return instance

    with _cache_lock:
        if key not in _downloader_cache:
            if host is not None or port is not None:
                # 点名了地址就别再去解释 mode——调用方要的就是这个节点
                from ipclick.sdk import Downloader

                _downloader_cache[key] = Downloader(config_path=config_path, host=host, port=port)
            else:
                _downloader_cache[key] = create_client(config_path)
        return _downloader_cache[key]


def close_all_downloaders() -> None:
    """关闭所有缓存的下载器（进程退出前调用，或在测试中隔离状态）。"""
    with _cache_lock:
        for instance in _downloader_cache.values():
            instance.close()
        _downloader_cache.clear()


class _LazyDownloader:
    """``ipclick.downloader`` 的惰性代理。

    以前这里是模块导入时就 ``Downloader()``，于是 ``import ipclick`` 会立刻
    读配置文件、打日志；服务端也 import 了 sdk，等于起服务先造一个客户端。
    改成首次真正使用时才构造。
    """

    __slots__: tuple[str, ...] = ()

    def __getattr__(self, name: str) -> Any:
        return getattr(get_downloader(), name)

    @override
    def __repr__(self) -> str:
        return "<ipclick.downloader (lazy)>"


#: 向后兼容的别名：downloader.get(...) 等用法保持不变
downloader: Any = _LazyDownloader()


__all__ = [
    "CLIENT_MODES",
    "close_all_downloaders",
    "create_client",
    "downloader",
    "get_downloader",
    "resolve_mode",
]
