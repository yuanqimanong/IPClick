import threading
from typing import Any

from typing_extensions import override

from ipclick.config_loader import load_config
from ipclick.exceptions import ConfigError
from ipclick.utils.log_util import log
from ipclick.utils.secure_util import SecureUtil


CLIENT_MODES: frozenset[str] = frozenset({"standalone", "cluster", "auto"})


def resolve_mode(config: Any) -> str:
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
    from ipclick.sdk import Downloader

    config = load_config(config_path)
    mode = resolve_mode(config)

    if mode == "cluster":
        from ipclick.cluster import ClusterDownloader

        for key in ("host", "port"):
            if kwargs.pop(key, None) is not None:
                log.warning(f"集群模式下忽略 {key} 参数——目标地址来自 [CLUSTER] 的节点池")
        log.info("按 [GENERAL].mode 使用集群客户端")
        return ClusterDownloader(config_path=config_path, **kwargs)

    return Downloader(config_path=config_path, **kwargs)


_downloader_cache: dict[str, Any] = {}
_cache_lock = threading.Lock()


def get_downloader(config_path: str | None = None, host: str | None = None, port: int | None = None) -> Any:
    key = SecureUtil.md5([config_path, host, port])
    instance = _downloader_cache.get(key)
    if instance is not None:
        return instance

    with _cache_lock:
        if key not in _downloader_cache:
            if host is not None or port is not None:
                from ipclick.sdk import Downloader

                _downloader_cache[key] = Downloader(config_path=config_path, host=host, port=port)
            else:
                _downloader_cache[key] = create_client(config_path)
        return _downloader_cache[key]


def close_all_downloaders() -> None:
    with _cache_lock:
        for instance in _downloader_cache.values():
            instance.close()
        _downloader_cache.clear()


class _LazyDownloader:
    __slots__: tuple[str, ...] = ()

    def __getattr__(self, name: str) -> Any:
        return getattr(get_downloader(), name)

    @override
    def __repr__(self) -> str:
        return "<ipclick.downloader (lazy)>"


downloader: Any = _LazyDownloader()


__all__ = [
    "CLIENT_MODES",
    "close_all_downloaders",
    "create_client",
    "downloader",
    "get_downloader",
    "resolve_mode",
]
