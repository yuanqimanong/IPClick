from pathlib import Path

import click

from ipclick.config_loader import load_config
from ipclick.server import serve


@click.group()
@click.version_option(version="1.0.0", prog_name="IPClick")
def main():
    """IPClick - 分布式HTTP请求代理工具"""
    pass


@main.command()
@click.option("--config", "-c", type=click.Path(exists=True, path_type=Path), help="配置文件路径")
@click.option("--port", "-p", type=int, help="服务端口号")
@click.option("--host", "-h", default=None, help="绑定地址")
def run(config, port, host):
    """启动IPClick服务"""
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
@click.option("--config", "-c", type=click.Path(path_type=Path), help="配置文件路径")
def config_info(config):
    """显示配置信息"""
    try:
        cfg = load_config(config)
        click.echo("Current configuration:")
        click.echo(f"  Server port: {cfg.get('server', {}).get('port', 9527)}")
        click.echo(f"  Server host: {cfg.get('server', {}).get('host', '0.0.0.0')}")
        click.echo(f"  Max workers: {cfg.get('server', {}).get('max_workers', 10)}")
        click.echo(f"  Client timeout: {cfg.get('client', {}).get('default_timeout', 30)}")

        remote_servers = cfg.get("workers", {}).get("remote_servers", [])
        if remote_servers:
            click.echo(f"  Remote servers: {len(remote_servers)}")
            for i, server in enumerate(remote_servers, 1):
                click.echo(f"    {i}. {server}")
        else:
            click.echo("  Remote servers: None configured")

    except Exception as e:
        click.echo(f"Error loading config: {e}", err=True)


if __name__ == "__main__":
    main()
