"""集群客户端：把请求分发到多个 IPClick 服务端，失败自动转移。

对外接口与单节点的 :class:`~ipclick.sdk.Downloader` 一致，调用方基本不用改代码。
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
import threading
from typing import Any

from ipclick.cluster.discovery import create_discovery
from ipclick.cluster.node import ClusterConfig, NodeState
from ipclick.cluster.pool import NodePool
from ipclick.dto.models import DownloadResponse, DownloadTask, HttpMethod
from ipclick.exceptions import ClientClosedError, IPClickError, TransportError
from ipclick.sdk import ClientBase, Downloader, StreamedResponse
from ipclick.tls import TLSSettings
from ipclick.utils.log_util import log


class ClusterDownloader(ClientBase):
    """跨多个 IPClick 服务端的客户端，带负载均衡与故障转移。

    ::

        with ClusterDownloader() as d:          # 节点取自 [CLUSTER].nodes
            resp = d.get("https://example.com")

    每个节点持有一个独立的 :class:`~ipclick.sdk.Downloader`（因而各自复用
    自己的 gRPC channel）。
    """

    def __init__(
        self,
        config_path: str | None = None,
        token: str | None = None,
        *,
        cluster_config: ClusterConfig | None = None,
        start_probing: bool = True,
        tls: TLSSettings | None = None,
    ):
        # ClientBase 提供配置加载、令牌解析与 _build_task；host/port 在集群模式下
        # 用不到（真正的目标地址来自节点池），但复用它能保证任务组装规则一致。
        super().__init__(config_path=config_path, token=token, tls=tls)
        self.cluster_config: ClusterConfig = cluster_config or ClusterConfig.from_config(
            dict(self.config.get("CLUSTER", {}))
        )
        discovery, discovery_config = create_discovery(dict(self.config.get("CLUSTER", {})), self.cluster_config.nodes)
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

    # ---------------------------------------------------------------- #
    # 连接管理
    # ---------------------------------------------------------------- #

    def _client_for(self, state: NodeState) -> Downloader:
        """取（或创建）某节点的 Downloader。"""
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
                    # 显式透传：调用方可能是用参数而不是配置文件给的 TLS 设置
                    tls=self.tls,
                )
            return self._clients[node_id]

    def close(self) -> None:
        """停止探活并关闭所有节点连接。可重复调用。"""
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

    # ---------------------------------------------------------------- #
    # 故障转移
    # ---------------------------------------------------------------- #

    def _with_failover(self, operation: Any, description: str) -> Any:
        """在节点间重试直到成功或用尽次数。

        只对 :class:`TransportError` 转移——那意味着"这个节点有问题"。
        其他 IPClickError（参数非法、鉴权失败）换个节点也是一样的结果，
        转移只会把同一个错误重复 N 遍，还拖慢失败反馈。
        """
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
                # 参数 / 鉴权类错误换节点没意义，直接上抛
                state.record_request(success=True)
                raise

            state.record_request(success=True)
            return result

        raise TransportError(
            f"「{description}」在 {len(tried)} 个节点上均失败，最后一次错误：{last_error}"
        ) from last_error

    # ---------------------------------------------------------------- #
    # 请求
    # ---------------------------------------------------------------- #

    def request(self, **kwargs: Any) -> DownloadResponse:
        """发送一次请求，失败自动换节点。

        与单节点 :meth:`~ipclick.sdk.Downloader.request` 契约一致：
        传输失败返回 ``status_code == -1`` 的响应而不抛异常；参数错误仍会抛。
        """
        url = str(kwargs.get("url", ""))
        # 关键：走 download() 而不是 request()。
        # Downloader.request() 会把 TransportError 吞成 -1 响应，故障转移逻辑
        # 就看不到"这个节点挂了"，永远不会换节点——只有 download() 会把传输
        # 失败原样抛出来。吞异常这一步留到最外层统一做。
        task = self._build_task(**kwargs)
        try:
            return self.download(task)
        except TransportError as e:
            log.error(f"请求 {url} 在所有节点上均失败：{e}")
            return DownloadResponse.from_error(str(e), url=url)

    def download(self, task: DownloadTask) -> DownloadResponse:
        """执行下载任务，失败自动换节点。

        Raises:
            TransportError: 所有可用节点都失败。
        """
        return self._with_failover(lambda c: c.download(task), f"下载 {task.url}")

    def stream(self, url: str, **kwargs: Any) -> StreamedResponse:
        """流式下载。

        注意：只有**建流**这一步会故障转移。流建立之后中途断掉不会自动重连——
        那需要断点续传（Range 请求）才能不重复数据，目前没做。
        """
        return self._with_failover(lambda c: c.stream(url, **kwargs), f"流式下载 {url}")

    def batch(self, tasks: Iterable[DownloadTask], timeout: float | None = None) -> Iterator[DownloadResponse]:
        """批量下载。

        整批发给同一个节点。按任务拆散分发到多个节点会打乱"按完成顺序返回"
        的语义，也让部分失败难以归因；需要跨节点分摊时请自行切分成多批。
        """
        materialized = list(tasks)
        call = self._with_failover(
            lambda c: list(c.batch(materialized, timeout=timeout)), f"批量 {len(materialized)} 个任务"
        )
        yield from call

    # ---------------------------------------------------------------- #
    # 便捷方法
    # ---------------------------------------------------------------- #

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

    # ---------------------------------------------------------------- #
    # 观测
    # ---------------------------------------------------------------- #

    def snapshot(self) -> dict[str, Any]:
        """集群状态快照，供状态页与 CLI 使用。"""
        return self.pool.snapshot()


__all__ = ["ClusterDownloader"]
