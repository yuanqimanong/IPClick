from pathlib import Path

import click

from ipclick import __version__
from ipclick.adapters.browser_engines import is_available, resolve_engine
from ipclick.adapters.browser_settings import BrowserSettings
from ipclick.auth import load_tokens
from ipclick.config_loader import load_config
from ipclick.config_loader.loader import example_config, example_env
from ipclick.factory import resolve_mode
from ipclick.health import check_health
from ipclick.limiter import LimiterSettings
from ipclick.server import serve
from ipclick.tls import TLSSettings, describe
from ipclick.utils.log_util import LogUtil


def _print_example(ctx: click.Context, _param: click.Parameter, value: str | None) -> None:
    """输出模板然后退出。

    直接打到 stdout（不加任何前缀），这样 `ipclick -e > ipclick.toml`
    出来的就是一个能直接用的文件。
    """
    if not value or ctx.resilient_parsing:
        return
    click.echo(example_env() if value == "env" else example_config(), nl=False)
    ctx.exit()


@click.group(invoke_without_command=True)
@click.version_option(version=__version__, prog_name="IPClick")
@click.option(
    "--example",
    "-e",
    type=click.Choice(["toml", "env"]),
    is_flag=False,
    flag_value="toml",
    default=None,
    is_eager=True,
    expose_value=False,
    callback=_print_example,
    help="输出模板到 stdout。-e 或 -e toml 出配置文件，-e env 出 .env（可重定向）",
)
@click.pass_context
def main(ctx: click.Context):
    """IPClick - 分布式HTTP请求代理工具"""
    # 不带子命令也不带 --example 时，给出帮助而不是静默退出
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@main.command()
@click.option("--config", "-c", type=click.Path(exists=True, path_type=Path), help="配置文件路径")
@click.option("--port", "-p", type=int, help="服务端口号")
@click.option("--host", default=None, help="绑定地址")
@click.option("--verbose", "-v", is_flag=True, help="输出 DEBUG 级别日志")
@click.option("--web", "-w", is_flag=True, default=None, help="同时启动 Web 管理端（登录信息打印到控制台）")
def run(config: Path | None, port: int | None, host: str | None, verbose: bool, web: bool | None):
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

        serve(config_path=str(config) if config else None, port=port, host=host, web=web)

    except KeyboardInterrupt:
        click.echo("\nShutting down...")
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        raise click.Abort() from e


@main.command()
@click.option("--host", default="127.0.0.1", show_default=True, help="服务端地址")
@click.option("--port", "-p", type=int, help="服务端端口（默认取配置）")
@click.option("--config", "-c", type=click.Path(path_type=Path), help="配置文件路径")
@click.option("--service", default="", help="要查询的服务名，默认查总体状态")
@click.option("--timeout", type=float, default=5.0, show_default=True, help="超时（秒）")
def health(host: str, port: int | None, config: Path | None, service: str, timeout: float):
    """检查服务端健康状态（grpc.health.v1）。

    健康时退出码 0，否则 1 —— 可直接用于 Docker HEALTHCHECK 或就绪探针。
    该接口免鉴权，无需提供令牌。
    """
    LogUtil.init(level="ERROR")  # 探活只输出结论，不要日志噪音

    resolved_port = port or int(dict(load_config(str(config) if config else None).get("SERVER", {})).get("port", 9527))
    target = f"{host}:{resolved_port}"

    healthy, status = check_health(target, service=service, timeout=timeout)
    click.echo(f"{target} -> {status}")
    if not healthy:
        raise SystemExit(1)


@main.command("config-info")
@click.option("--config", "-c", type=click.Path(path_type=Path), help="配置文件路径")
def config_info(config: Path | None):
    """显示配置信息"""
    try:
        cfg = load_config(str(config) if config else None)

        # 配置节名是大写的（[SERVER]/[DOWNLOADER]/...）。此前这里读的是小写的
        # server/client/workers，永远取不到值，于是原样打印一串假的默认值。
        server = dict(cfg.get("SERVER", {}))
        proxy = dict(cfg.get("PROXY", {}))
        security = dict(cfg.get("SECURITY", {}))
        log_cfg = dict(cfg.get("LOG", {}))
        downloader_cfg = dict(cfg.get("DOWNLOADER", {}))
        monitor = dict(cfg.get("MONITOR", {}))

        click.echo("Current configuration:")
        click.echo(f"  Server host:  {server.get('host', '[::]')}")
        click.echo(f"  Server port:  {server.get('port', 9527)}")
        click.echo(f"  Max workers:  {server.get('max_workers', 10)}")
        click.echo(f"  Log level:    {log_cfg.get('level', 'info')}")
        click.echo(f"  Log output:   {log_cfg.get('output', 'stdout')}")

        # --- 安全 ---
        # 这一组是最该被看见的：配错了不会报错，只会悄悄少一层防护。
        click.echo("")
        click.echo("Security:")
        click.echo(f"  传输层:       {describe(TLSSettings.from_config(security))}")
        # 只说有没有，不打印令牌本身
        has_token = bool(load_tokens(security))
        click.echo(f"  令牌鉴权:     {'已配置' if has_token else '未配置（任何人都能调用）'}")
        click.echo(f"  拦截内网地址: {security.get('block_private_networks', False)}")
        click.echo(f"  拦截元数据端点: {security.get('block_metadata_endpoints', True)}")

        # --- 限流 ---
        limits = LimiterSettings.from_config(downloader_cfg)
        rate_cfg = dict(downloader_cfg.get("rate_limit") or {})
        backend = str(rate_cfg.get("backend") or "memory")
        click.echo("")
        click.echo("Per-host limits:")
        if limits.enabled:
            click.echo(f"  并发上限:     {limits.per_host_max_concurrent or '不限'}")
            click.echo(f"  QPS 上限:     {limits.per_host_qps or '不限'}")
            click.echo(f"  后端:         {backend}{'（每进程各算各的）' if backend != 'redis' else '（集群共享）'}")
        else:
            click.echo("  未启用")

        # --- 浏览器渲染 ---
        browser = BrowserSettings.from_config(dict(cfg.get("BROWSER", {})))
        click.echo("")
        click.echo("Browser rendering:")
        if browser.enabled:
            try:
                engine = resolve_engine(browser.engine)
                ready = "可用" if is_available(engine) else "依赖未安装"
            except Exception as e:  # 引擎名配错
                engine, ready = browser.engine, f"配置错误: {e}"
            click.echo(f"  引擎:         {engine}（配置为 {browser.engine}）— {ready}")
            click.echo(f"  页面上限:     {browser.max_pages}")
            click.echo(f"  允许页内 JS:  {browser.allow_scripts}")
        else:
            click.echo("  已关闭")

        # --- 客户端 ---
        click.echo("")
        click.echo("Client:")
        try:
            click.echo(f"  运行模式:     {resolve_mode(cfg)}")
        except Exception as e:
            click.echo(f"  运行模式:     配置错误: {e}")
        nodes = cfg.get("CLUSTER", {}).get("nodes", [])
        discovery = str(dict(cfg.get("CLUSTER", {}).get("discovery") or {}).get("mode") or "static")
        click.echo(f"  集群节点:     {len(nodes)} 个（发现方式 {discovery}）")

        # 只显示代理是否配置，不打印账号密码
        proxy_host = proxy.get("host") or proxy.get("tunnel_server")
        click.echo(f"  代理:         {proxy_host + ':' + str(proxy.get('port', '')) if proxy_host else '未配置'}")
        if proxy.get("auth_key"):
            click.echo("  代理鉴权:     已配置（已隐藏）")

        # --- 监控 ---
        click.echo("")
        click.echo("Monitoring:")
        click.echo(f"  健康检查:     {monitor.get('health_check', True)}")
        metrics_on = monitor.get("metrics_enabled", False)
        endpoint = f"{monitor.get('metrics_host', '0.0.0.0')}:{monitor.get('metrics_port', 9528)}"
        click.echo(f"  Prometheus:   {endpoint if metrics_on else '未启用'}")

    except click.Abort:
        raise
    except Exception as e:
        click.echo(f"Error loading config: {e}", err=True)
        raise click.Abort() from e


if __name__ == "__main__":
    main()
