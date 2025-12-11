# -*- coding:utf-8 -*-

"""
@time: 2025-12-10
@author: Hades
@file: sdk.py
"""
import logging

import grpc

from ipclick import load_config, DownloadTask, DownloadResponse, HttpMethod
from ipclick.dto.proto import task_pb2_grpc


class Downloader:
    """IPClick下载器客户端"""

    def __init__(self, config_path=None, host=None, port=None):
        """
        初始化下载器

        Args:
            config_path: 配置文件路径
            host: 服务器地址 (覆盖配置文件)
            port: 服务器端口 (覆盖配置文件)
        """
        self.logger = logging.getLogger(__name__)
        self.config = load_config(config_path)
        if self.config['GENERAL']['mode'] == 'standalone':
            self.host = host or '127.0.0.1'
            self.port = port or self.config['SERVER']['port']
        else:
            ...

        self.default_adapter = 'curl_cffi'
        self.downloader_cfg = self.config['DOWNLOADER']
        self.browser_cfg = self.config['BROWSER']

    def request(
            self,
            *,
            adapter=None,
            method,
            url,
            headers=None,
            cookies=None,
            params=None,
            data=None,
            json=None,
            files=None,
            proxy=None,
            timeout=60,
            max_retries=3,
            verify=None,
            follow_redirects=None,
            stream=None,
            impersonate=None,
            **kwargs
    ):

        task = DownloadTask(
            adapter=adapter or kwargs.get('adapter') or self.default_adapter,
            url=url,
            method=method.value,
            headers=headers,
            cookies=cookies,
            params=params,
            data=data,
            json=json,
            files=files,
            proxy=proxy,
            timeout=timeout,
            max_retries=max_retries,
            verify=verify,
            follow_redirects=follow_redirects,
            stream=stream,
            impersonate=impersonate,
            **kwargs
        )
        return self.download(task)

    def download(self, task: DownloadTask) -> DownloadResponse:
        """
        执行下载任务

        Args:
            task: 下载任务对象

        Returns:
            下载响应对象

        Raises:
            Exception: 当连接失败或任务执行失败时
        """
        pb_request = task.to_protobuf()

        try:
            with grpc.insecure_channel(f'{self.host}:{self.port}') as channel:
                stub = task_pb2_grpc.TaskServiceStub(channel)
                pb_response = stub.Send(pb_request)
                return DownloadResponse.from_protobuf(pb_response)
        except grpc.RpcError as e:
            raise Exception(f"gRPC error: {e.details()}") from e
        except Exception as e:
            raise Exception(f"Connection error: {str(e)}") from e

    def get(self, url: str, params=None, **kwargs) -> DownloadResponse:
        """
        发送GET请求

        Args:
            url: 请求URL
            params: URL参数
            **kwargs:  其他DownloadTask参数

        Returns:
            响应对象
        """
        return self.request(method=HttpMethod.GET, url=url, params=params, **kwargs)

    # def post(self, url: str,
    #          data: Optional[Union[str, bytes]] = None,
    #          json_data: Optional[Dict[str, Any]] = None,
    #          headers: Optional[Dict[str, str]] = None,
    #          timeout: Optional[int] = None,
    #          **kwargs) -> DownloadResponse:
    #     """
    #     发送POST请求
    #
    #     Args:
    #         url: 请求URL
    #         data: 请求体数据
    #         json_data:  JSON数据
    #         headers: 请求头
    #         timeout: 超时时间
    #         **kwargs: 其他DownloadTask参数
    #
    #     Returns:
    #         响应对象
    #     """
    #     task = DownloadTask(
    #         url=url,
    #         method=HttpMethod.POST,
    #         data=data,
    #         json_data=json_data,
    #         headers=headers or {},
    #         timeout=timeout or self.default_timeout,
    #         max_retries=self.default_retries,
    #         **kwargs
    #     )
    #     return self.download(task)
    #
    # def put(self, url: str,
    #         data: Optional[Union[str, bytes]] = None,
    #         json_data: Optional[Dict[str, Any]] = None,
    #         headers: Optional[Dict[str, str]] = None,
    #         **kwargs) -> DownloadResponse:
    #     """发送PUT请求"""
    #     task = DownloadTask(
    #         url=url,
    #         method=HttpMethod.PUT,
    #         data=data,
    #         json_data=json_data,
    #         headers=headers or {},
    #         timeout=self.default_timeout,
    #         max_retries=self.default_retries,
    #         **kwargs
    #     )
    #     return self.download(task)
    #
    # def delete(self, url: str,
    #            headers: Optional[Dict[str, str]] = None,
    #            **kwargs) -> DownloadResponse:
    #     """发送DELETE请求"""
    #     task = DownloadTask(
    #         url=url,
    #         method=HttpMethod.DELETE,
    #         headers=headers or {},
    #         timeout=self.default_timeout,
    #         max_retries=self.default_retries,
    #         **kwargs
    #     )
    #     return self.download(task)


# 提供默认的全局下载器实例
_default_downloader = None


def get_downloader() -> Downloader:
    """获取默认下载器实例"""
    global _default_downloader
    if _default_downloader is None:
        _default_downloader = Downloader()
    return _default_downloader


# 向后兼容的别名
downloader = get_downloader()
