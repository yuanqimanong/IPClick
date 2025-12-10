# -*- coding:utf-8 -*-

"""
IPClick命令行工具

@time: 2025-12-10
@author: Hades
@file: main.py
"""

import logging
from pathlib import Path

import click

from ipclick import load_config
from ipclick.server import serve


@click.group()
@click.version_option(version="1.0.0", prog_name="IPClick")
def main():
    """IPClick - 分布式HTTP请求代理工具"""
    pass


@main.command()
@click.option('--config', '-c',
              type=click.Path(exists=True, path_type=Path),
              help='配置文件路径')
@click.option('--port', '-p',
              type=int,
              help='服务端口号')
@click.option('--host', '-h',
              default=None,
              help='绑定地址')
@click.option('--verbose', '-v',
              is_flag=True,
              help='显示详细日志')
def run(config, port, host, verbose):
    """启动IPClick服务"""
    if verbose:
        logging.basicConfig(level=logging.DEBUG)

    try:
        click.echo("Starting IPClick server...")
        if config:
            click.echo(f"Using config file: {config}")
        if port:
            click.echo(f"Override port: {port}")
        if host:
            click.echo(f"Override host: {host}")

        serve(config_path=config, port=port, host=host)

    except KeyboardInterrupt:
        click.echo("\nShutting down...")
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        raise click.Abort()


@main.command()
@click.option('--config', '-c',
              type=click.Path(path_type=Path),
              help='配置文件路径')
def config_info(config):
    """显示配置信息"""
    try:
        cfg = load_config(config)
        click.echo("Current configuration:")
        click.echo(f"  Server port: {cfg.get('server', {}).get('port', 9527)}")
        click.echo(f"  Server host: {cfg.get('server', {}).get('host', '0.0.0.0')}")
        click.echo(f"  Max workers: {cfg.get('server', {}).get('max_workers', 10)}")
        click.echo(f"  Client timeout: {cfg.get('client', {}).get('default_timeout', 30)}")
        click.echo(f"  Log level: {cfg.get('logging', {}).get('level', 'INFO')}")

        remote_servers = cfg.get('workers', {}).get('remote_servers', [])
        if remote_servers:
            click.echo(f"  Remote servers: {len(remote_servers)}")
            for i, server in enumerate(remote_servers, 1):
                click.echo(f"    {i}. {server}")
        else:
            click.echo("  Remote servers: None configured")

    except Exception as e:
        click.echo(f"Error loading config: {e}", err=True)


@main.command()
@click.argument('url')
@click.option('--method', '-m', default='GET', help='HTTP方法')
@click.option('--headers', '-H', multiple=True, help='HTTP头 (格式: "Key: Value")')
@click.option('--data', '-d', help='请求体数据')
@click.option('--timeout', '-t', type=int, default=30, help='超时时间')
@click.option('--config', '-c', help='配置文件路径')
def test(url, method, headers, data, timeout, config):
    """测试HTTP请求"""
    from ..sdk import Downloader
    from ..dto.models import DownloadTask, HttpMethod

    try:
        # 解析headers
        header_dict = {}
        for header in headers:
            if ':' in header:
                key, value = header.split(':', 1)
                header_dict[key.strip()] = value.strip()

        # 创建下载器
        downloader = Downloader(config_path=config)

        # 创建任务
        task = DownloadTask(
            url=url,
            method=HttpMethod(method.upper()),
            headers=header_dict,
            data=data,
            timeout=timeout
        )

        # 执行请求
        click.echo(f"Sending {method.upper()} request to {url}...")
        response = downloader.download(task)

        # 显示结果
        click.echo(f"Status: {response.status_code}")
        click.echo(f"URL: {response.url}")
        click.echo(f"Time: {response.elapsed_ms}ms")

        if response.headers:
            click.echo("Headers:")
            for key, value in response.headers.items():
                click.echo(f"  {key}: {value}")

        if response.text:
            click.echo("Response:")
            click.echo(response.text[: 500] + "..." if len(response.text) > 500 else response.text)

    except Exception as e:
        click.echo(f"Request failed: {e}", err=True)
        raise click.Abort()


if __name__ == '__main__':
    main()
