"""由调用方直接连接各节点的同步集群客户端。"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
import threading
from typing import Any, TypeVar

from ipclick.cluster.discovery import create_discovery
from ipclick.cluster.node import ClusterConfig, NodeState
from ipclick.cluster.pool import NodePool
from ipclick.cluster.tokens import cluster_secret, token_for
from ipclick.dto.models import DownloadResponse, DownloadTask, HttpMethod
from ipclick.exceptions import ClientClosedError, IPClickError, TransportError
from ipclick.sdk import ClientBase, Downloader, StreamedResponse
from ipclick.tls import TLSSettings
from ipclick.utils.config_util import section
from ipclick.utils.log_util import log


_T = TypeVar("_T")


# 与服务端转发的方法白名单保持一致：只有无副作用的读取方法在结果未知时可以重投。
_REPLAYABLE_METHODS = frozenset({HttpMethod.GET, HttpMethod.HEAD, HttpMethod.OPTIONS})


def _is_replayable(task: DownloadTask) -> bool:
    """判断一个任务在结果未知时能否安全地换节点重投。"""
    return task.method in _REPLAYABLE_METHODS


class ClusterDownloader(ClientBase):
    """在多个直连节点间负载均衡并执行有限故障转移。"""

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
        # cluster_config 可能由调用方直接注入，不能再次只从文件读取 secret。
        self._cluster_secret: str = cluster_secret({"secret": self.cluster_config.secret})
        self._clients: dict[str, Downloader] = {}
        self._clients_lock: threading.Lock = threading.Lock()

        log.info(
            f"集群客户端已启动：{len(self.pool)} 个节点，策略 {self.pool.balancer.name}，"
            f"最多故障转移 {self.cluster_config.max_failover} 次"
        )

    def _client_for(self, state: NodeState) -> Downloader:
        """获取节点专属客户端；节点地址变化时关闭并重建旧连接。"""
        if self._closed:
            raise ClientClosedError("ClusterDownloader 已关闭，无法继续发送请求")

        node_id = state.node.id
        raw_host = state.node.host.strip("[]")
        client_host = f"[{raw_host}]" if ":" in raw_host else raw_host
        client = self._clients.get(node_id)
        if client is not None and (client.host, client.port) == (client_host, state.node.port):
            return client

        with self._clients_lock:
            if self._closed:
                raise ClientClosedError("ClusterDownloader 已关闭，无法继续发送请求")
            client = self._clients.get(node_id)
            if client is not None and (client.host, client.port) != (client_host, state.node.port):
                # 服务发现允许稳定 ID 的节点迁移地址，旧 channel 不能继续复用。
                client.close()
                client = None
                self._clients.pop(node_id, None)
            if client is None:
                # 显式 token= 的优先级最高；否则按节点显式令牌/共享密钥派生，
                # 两者都没有时让 Downloader 回退到 [SECURITY].auth_token。
                auth_token = self._token
                if auth_token is None:
                    auth_token = token_for(state.node.id, state.node.token, self._cluster_secret)
                self._clients[node_id] = Downloader(
                    config_path=self._config_path,
                    host=client_host,
                    port=state.node.port,
                    token=auth_token,
                    tls=self.tls,
                )
            return self._clients[node_id]

    def close(self) -> None:
        """停止探活并关闭所有已创建的节点连接。"""
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

    def _with_failover(
        self, operation: Callable[[Downloader], _T], description: str, *, replayable: bool = True
    ) -> _T:
        """仅对传输层错误换节点，业务错误直接返回给调用方。

        ``replayable=False`` 的调用**不换节点**：传输层错误意味着结果未知，下游可能
        已经执行完了只是回复没赶上，重投一次就是重复下单。服务端转发那一侧早就按
        方法白名单挡住了（只有 GET/HEAD/OPTIONS 会换），客户端分发这一侧口径要一致。
        """
        tried: set[str] = set()
        attempts = self.cluster_config.max_failover + 1
        last_error: Exception | None = None

        for attempt in range(attempts):
            try:
                state = self.pool.acquire(exclude=tried)
            except TransportError as e:
                if last_error is None:
                    last_error = e
                break
            tried.add(state.node.id)
            client = self._client_for(state)

            try:
                result = operation(client)
            except TransportError as e:
                last_error = e
                state.record_request(success=False)
                state.mark_unhealthy(str(e))
                if not replayable:
                    log.warning(
                        f"节点 {state.node.id} 处理非幂等的「{description}」时结果未知，"
                        f"为避免重复执行，不再换节点：{e}"
                    )
                    raise
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
        """构造任务并请求集群；传输失败转换为错误响应以兼容 Downloader。"""
        url = str(kwargs.get("url", ""))
        task = self._build_task(**kwargs)
        try:
            return self.download(task)
        except TransportError as e:
            log.error(f"请求 {url} 在所有节点上均失败：{e}")
            return DownloadResponse.from_error(str(e), url=url)

    def download(self, task: DownloadTask) -> DownloadResponse:
        """下载单个已构造任务。"""
        return self._with_failover(
            lambda c: c.download(task), f"下载 {task.url}", replayable=_is_replayable(task)
        )

    def stream(self, url: str, **kwargs: Any) -> StreamedResponse:
        """建立流式响应；流开始后的中断不会跨节点续传。"""
        return self._with_failover(lambda c: c.stream(url, **kwargs), f"流式下载 {url}")

    def batch(self, tasks: Iterable[DownloadTask], timeout: float | None = None) -> Iterator[DownloadResponse]:
        """将一批任务固定派发到同一健康节点。"""
        materialized = list(tasks)
        # 整批重投的话，批里任何一个写请求都会被重复执行一次。
        call = self._with_failover(
            lambda c: list(c.batch(materialized, timeout=timeout)),
            f"批量 {len(materialized)} 个任务",
            replayable=all(_is_replayable(task) for task in materialized),
        )
        yield from call

    def get(self, url: str, params: dict[str, Any] | None = None, **kwargs: Any) -> DownloadResponse:
        """向集群发送 GET 请求。"""
        return self.request(method=HttpMethod.GET, url=url, params=params, **kwargs)

    def post(self, url: str, data: Any = None, json: dict[str, Any] | None = None, **kwargs: Any) -> DownloadResponse:
        """向集群发送 POST 请求。"""
        return self.request(method=HttpMethod.POST, url=url, data=data, json=json, **kwargs)

    def put(self, url: str, data: Any = None, **kwargs: Any) -> DownloadResponse:
        """向集群发送 PUT 请求。"""
        return self.request(method=HttpMethod.PUT, url=url, data=data, **kwargs)

    def patch(self, url: str, data: Any = None, **kwargs: Any) -> DownloadResponse:
        """向集群发送 PATCH 请求。"""
        return self.request(method=HttpMethod.PATCH, url=url, data=data, **kwargs)

    def delete(self, url: str, **kwargs: Any) -> DownloadResponse:
        """向集群发送 DELETE 请求。"""
        return self.request(method=HttpMethod.DELETE, url=url, **kwargs)

    def head(self, url: str, **kwargs: Any) -> DownloadResponse:
        """向集群发送 HEAD 请求。"""
        return self.request(method=HttpMethod.HEAD, url=url, **kwargs)

    def options(self, url: str, **kwargs: Any) -> DownloadResponse:
        """向集群发送 OPTIONS 请求。"""
        return self.request(method=HttpMethod.OPTIONS, url=url, **kwargs)

    def snapshot(self) -> dict[str, Any]:
        """返回节点池健康与请求计数快照。"""
        return self.pool.snapshot()


__all__ = ["ClusterDownloader"]
