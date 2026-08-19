from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
import threading
from typing import Any, Self, final

from typing_extensions import override

from ipclick.config_loader import load_config
from ipclick.dto.models import DownloadResponse, DownloadTask
from ipclick.exceptions import ConfigError
from ipclick.protocols import DownloadClient, StreamedBody
from ipclick.utils.coerce import as_text
from ipclick.utils.config_util import section
from ipclick.utils.log_util import log
from ipclick.utils.secure_util import SecureUtil


CLIENT_MODES: frozenset[str] = frozenset({"standalone", "cluster", "auto"})

STANDALONE = "standalone"

CLUSTER = "cluster"


def resolve_mode(config: Mapping[str, Any] | None) -> str:
    mode = as_text(section(config, "GENERAL").get("mode"), STANDALONE).lower()
    if mode not in CLIENT_MODES:
        raise ConfigError(f"未知的 [GENERAL].mode {mode!r}，可选：{'、'.join(sorted(CLIENT_MODES))}")

    cluster = section(config, "CLUSTER")
    has_nodes = bool(cluster.get("nodes"))
    has_discovery = as_text(section(cluster, "discovery").get("mode"), "static").lower() != "static"

    if mode == CLUSTER:
        if not (has_nodes or has_discovery):
            raise ConfigError(
                '[GENERAL].mode = "cluster" 但既没有 [CLUSTER].nodes 也没有配置 [CLUSTER.discovery]。'
                "静默退回单机会让你以为集群生效了，实际所有流量都打在一个节点上、也没有故障转移。"
            )
        return CLUSTER

    if mode == "auto":
        return CLUSTER if (has_nodes or has_discovery) else STANDALONE
    return STANDALONE


def create_client(config_path: str | None = None, **kwargs: Any) -> DownloadClient:
    from ipclick.sdk import Downloader

    config = load_config(config_path)

    if resolve_mode(config) == CLUSTER:
        from ipclick.cluster import ClusterDownloader

        for key in ("host", "port"):
            if kwargs.pop(key, None) is not None:
                log.warning(f"集群模式下忽略 {key} 参数——目标地址来自 [CLUSTER] 的节点池")
        log.info("按 [GENERAL].mode 使用集群客户端")
        return ClusterDownloader(config_path=config_path, **kwargs)

    return Downloader(config_path=config_path, **kwargs)


_downloader_cache: dict[str, DownloadClient] = {}
_cache_lock = threading.Lock()


def get_downloader(config_path: str | None = None, host: str | None = None, port: int | None = None) -> DownloadClient:
    key = SecureUtil.md5([config_path, host, port])
    instance = _downloader_cache.get(key)
    if instance is not None:
        return instance

    with _cache_lock:
        if key not in _downloader_cache:
            _downloader_cache[key] = _build_client(config_path, host, port)
        return _downloader_cache[key]


def _build_client(config_path: str | None, host: str | None, port: int | None) -> DownloadClient:
    if host is None and port is None:
        return create_client(config_path)

    from ipclick.sdk import Downloader

    return Downloader(config_path=config_path, host=host, port=port)


def close_all_downloaders() -> None:
    with _cache_lock:
        for instance in _downloader_cache.values():
            instance.close()
        _downloader_cache.clear()


@final
class _LazyDownloader:
    __slots__: tuple[str, ...] = ()

    @property
    def client(self) -> DownloadClient:
        return get_downloader()

    def download(self, task: DownloadTask) -> DownloadResponse:
        return self.client.download(task)

    def request(self, *, url: str, **kwargs: Any) -> DownloadResponse:
        return self.client.request(url=url, **kwargs)

    def stream(self, url: str, **kwargs: Any) -> StreamedBody:
        return self.client.stream(url, **kwargs)

    def batch(self, tasks: Iterable[DownloadTask], timeout: float | None = None) -> Iterator[DownloadResponse]:
        return self.client.batch(tasks, timeout=timeout)

    def get(self, url: str, params: dict[str, Any] | None = None, **kwargs: Any) -> DownloadResponse:
        return self.client.get(url, params=params, **kwargs)

    def post(self, url: str, data: Any = None, json: dict[str, Any] | None = None, **kwargs: Any) -> DownloadResponse:
        return self.client.post(url, data=data, json=json, **kwargs)

    def put(self, url: str, data: Any = None, **kwargs: Any) -> DownloadResponse:
        return self.client.put(url, data=data, **kwargs)

    def patch(self, url: str, data: Any = None, **kwargs: Any) -> DownloadResponse:
        return self.client.patch(url, data=data, **kwargs)

    def delete(self, url: str, **kwargs: Any) -> DownloadResponse:
        return self.client.delete(url, **kwargs)

    def head(self, url: str, **kwargs: Any) -> DownloadResponse:
        return self.client.head(url, **kwargs)

    def options(self, url: str, **kwargs: Any) -> DownloadResponse:
        return self.client.options(url, **kwargs)

    def close(self) -> None:
        close_all_downloaders()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        self.close()

    @override
    def __repr__(self) -> str:
        return "<ipclick.downloader (lazy)>"


downloader: DownloadClient = _LazyDownloader()


__all__ = [
    "CLIENT_MODES",
    "close_all_downloaders",
    "create_client",
    "downloader",
    "get_downloader",
    "resolve_mode",
]
