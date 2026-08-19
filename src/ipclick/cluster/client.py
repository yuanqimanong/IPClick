from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
import threading
from typing import Any, TypeVar

from ipclick.cluster.discovery import create_discovery
from ipclick.cluster.node import ClusterConfig, NodeState
from ipclick.cluster.pool import NodePool
from ipclick.dto.models import DownloadResponse, DownloadTask, HttpMethod
from ipclick.exceptions import ClientClosedError, IPClickError, TransportError
from ipclick.sdk import ClientBase, Downloader, StreamedResponse
from ipclick.tls import TLSSettings
from ipclick.utils.config_util import section
from ipclick.utils.log_util import log


_T = TypeVar("_T")


class ClusterDownloader(ClientBase):
    def __init__(
        self,
        config_path: str | None = None,
        token: str | None = None,
        *,
        cluster_config: ClusterConfig | None = None,
        start_probing: bool = True,
        tls: TLSSettings | None = None,
    ):
        super().__init__(config_path=config_path, token=token, tls=tls)
        self.cluster_config: ClusterConfig = cluster_config or ClusterConfig.from_config(
            section(self.config, "CLUSTER")
        )
        discovery, discovery_config = create_discovery(section(self.config, "CLUSTER"), self.cluster_config.nodes)
        self.pool: NodePool = NodePool(
            self.cluster_config,
            start_probing=start_probing,
            tls=self.tls,
            discovery=discovery,
            discovery_config=discovery_config,
        )

        self._config_path: str | None = config_path
        self._token: str | None = token
        self._clients: dict[str, Downloader] = {}
        self._clients_lock: threading.Lock = threading.Lock()

        log.info(
            f"集群客户端已启动：{len(self.pool)} 个节点，策略 {self.pool.balancer.name}，"
            f"最多故障转移 {self.cluster_config.max_failover} 次"
        )

    def _client_for(self, state: NodeState) -> Downloader:
        if self._closed:
            raise ClientClosedError("ClusterDownloader 已关闭，无法继续发送请求")

        node_id = state.node.id
        client = self._clients.get(node_id)
        if client is not None:
            return client

        with self._clients_lock:
            if node_id not in self._clients:
                self._clients[node_id] = Downloader(
                    config_path=self._config_path,
                    host=state.node.host,
                    port=state.node.port,
                    token=self._token,
                    tls=self.tls,
                )
            return self._clients[node_id]

    def close(self) -> None:
        self._closed: bool = True
        self.pool.stop()
        with self._clients_lock:
            for client in self._clients.values():
                client.close()
            self._clients.clear()

    def __enter__(self) -> ClusterDownloader:
        return self

    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        self.close()

    def _with_failover(self, operation: Callable[[Downloader], _T], description: str) -> _T:
        tried: set[str] = set()
        attempts = self.cluster_config.max_failover + 1
        last_error: Exception | None = None

        for attempt in range(attempts):
            state = self.pool.acquire(exclude=tried)
            tried.add(state.node.id)
            client = self._client_for(state)

            try:
                result = operation(client)
            except TransportError as e:
                last_error = e
                state.record_request(success=False)
                state.mark_unhealthy(str(e))
                log.warning(
                    f"节点 {state.node.id} 处理「{description}」失败（第 {attempt + 1}/{attempts} 次尝试）：{e}"
                )
                continue
            except IPClickError:
                state.record_request(success=True)
                raise

            state.record_request(success=True)
            return result

        raise TransportError(
            f"「{description}」在 {len(tried)} 个节点上均失败，最后一次错误：{last_error}"
        ) from last_error

    def request(self, **kwargs: Any) -> DownloadResponse:
        url = str(kwargs.get("url", ""))
        task = self._build_task(**kwargs)
        try:
            return self.download(task)
        except TransportError as e:
            log.error(f"请求 {url} 在所有节点上均失败：{e}")
            return DownloadResponse.from_error(str(e), url=url)

    def download(self, task: DownloadTask) -> DownloadResponse:
        return self._with_failover(lambda c: c.download(task), f"下载 {task.url}")

    def stream(self, url: str, **kwargs: Any) -> StreamedResponse:
        return self._with_failover(lambda c: c.stream(url, **kwargs), f"流式下载 {url}")

    def batch(self, tasks: Iterable[DownloadTask], timeout: float | None = None) -> Iterator[DownloadResponse]:
        materialized = list(tasks)
        call = self._with_failover(
            lambda c: list(c.batch(materialized, timeout=timeout)), f"批量 {len(materialized)} 个任务"
        )
        yield from call

    def get(self, url: str, params: dict[str, Any] | None = None, **kwargs: Any) -> DownloadResponse:
        return self.request(method=HttpMethod.GET, url=url, params=params, **kwargs)

    def post(self, url: str, data: Any = None, json: dict[str, Any] | None = None, **kwargs: Any) -> DownloadResponse:
        return self.request(method=HttpMethod.POST, url=url, data=data, json=json, **kwargs)

    def put(self, url: str, data: Any = None, **kwargs: Any) -> DownloadResponse:
        return self.request(method=HttpMethod.PUT, url=url, data=data, **kwargs)

    def patch(self, url: str, data: Any = None, **kwargs: Any) -> DownloadResponse:
        return self.request(method=HttpMethod.PATCH, url=url, data=data, **kwargs)

    def delete(self, url: str, **kwargs: Any) -> DownloadResponse:
        return self.request(method=HttpMethod.DELETE, url=url, **kwargs)

    def head(self, url: str, **kwargs: Any) -> DownloadResponse:
        return self.request(method=HttpMethod.HEAD, url=url, **kwargs)

    def options(self, url: str, **kwargs: Any) -> DownloadResponse:
        return self.request(method=HttpMethod.OPTIONS, url=url, **kwargs)

    def snapshot(self) -> dict[str, Any]:
        return self.pool.snapshot()


__all__ = ["ClusterDownloader"]
