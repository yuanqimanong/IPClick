from pathlib import Path

import click

from ipclick import __version__
from ipclick.config_loader import load_config
from ipclick.server import serve
from ipclick.utils.log_util import LogUtil


@click.group()
@click.version_option(version=__version__, prog_name="IPClick")
def main():
    """IPClick - 分布式HTTP请求代理工具"""


@main.command()
@click.option("--config", "-c", type=click.Path(exists=True, path_type=Path), help="配置文件路径")
@click.option("--port", "-p", type=int, help="服务端口号")
@click.option("--host", default=None, help="绑定地址")
@click.option("--verbose", "-v", is_flag=True, help="输出 DEBUG 级别日志")
def run(config: Path | None, port: int | None, host: str | None, verbose: bool):
    """启动IPClick服务"""
    try:
        LogUtil.init(level="DEBUG" if verbose else "INFO")

        click.echo("Starting IPClick server...")
        if config:
            click.echo(f"Using config file: {config}")
        if port:
            click.echo(f"Override port: {port}")
        if host:
            click.echo(f"Override host: {host}")

        serve(config_path=str(config) if config else None, port=port, host=host)

    except KeyboardInterrupt:
        click.echo("\nShutting down...")
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        raise click.Abort() from e


@main.command("config-info")
@click.option("--config", "-c", type=click.Path(path_type=Path), help="配置文件路径")
def config_info(config: Path | None):
    """显示配置信息"""
    try:
        cfg = load_config(str(config) if config else None)

        # 配置节名是大写的（[SERVER]/[DOWNLOADER]/...）。此前这里读的是小写的
        # server/client/workers，永远取不到值，于是原样打印一串假的默认值。
        server = dict(cfg.get("SERVER", {}))
        downloader_cfg = dict(cfg.get("DOWNLOADER", {}))
        proxy = dict(cfg.get("PROXY", {}))
        security = dict(cfg.get("SECURITY", {}))

        click.echo("Current configuration:")
        click.echo(f"  Server host:  {server.get('host', '[::]')}")
        click.echo(f"  Server port:  {server.get('port', 9527)}")
        click.echo(f"  Max workers:  {server.get('max_workers', 10)}")
        click.echo(f"  Connect timeout:  {downloader_cfg.get('connect_timeout', 10)}s")
        click.echo(f"  Download timeout: {downloader_cfg.get('download_timeout', 300)}s")

        # 只显示代理是否配置，不打印账号密码
        proxy_host = proxy.get("host") or proxy.get("tunnel_server")
        click.echo(f"  Proxy:  {proxy_host + ':' + str(proxy.get('port', '')) if proxy_host else 'not configured'}")
        if proxy.get("auth_key"):
            click.echo("  Proxy auth: configured (已隐藏)")

        click.echo(f"  Block private networks: {security.get('block_private_networks', False)}")

        nodes = cfg.get("CLUSTER", {}).get("nodes", [])
        if nodes:
            click.echo(f"  Cluster nodes: {len(nodes)}")
            for i, node in enumerate(nodes, 1):
                click.echo(f"    {i}. {node.get('id', '?')} @ {node.get('address', '?')}")
        else:
            click.echo("  Cluster nodes: None configured")

    except Exception as e:
        click.echo(f"Error loading config: {e}", err=True)
        raise click.Abort() from e


if __name__ == "__main__":
    main()
