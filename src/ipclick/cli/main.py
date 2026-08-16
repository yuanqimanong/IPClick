import os
from pathlib import Path
import unicodedata

import click

from ipclick import __version__
from ipclick.adapters.browser_engines import engine_status, resolve_engine
from ipclick.adapters.browser_settings import BrowserSettings
from ipclick.auth import load_tokens
from ipclick.config_loader import load_config
from ipclick.config_loader.loader import example_config, example_env
from ipclick.factory import resolve_mode
from ipclick.health import check_health
from ipclick.limiter import LimiterSettings
from ipclick.secrets import SECRETS, describe_source
from ipclick.server import serve
from ipclick.tls import TLSSettings, describe
from ipclick.trace import TraceSettings
from ipclick.utils.log_util import LogUtil
from ipclick.web.auth import generate_password


def _display_width(text: str) -> int:
    """终端显示宽度。CJK 字符占两列，按 len() 补空格会对不齐。"""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


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
@click.option("--force", "-f", is_flag=True, help="覆盖已存在的文件")
@click.option("--dir", "-d", "target_dir", type=click.Path(path_type=Path), default=".", help="生成到哪个目录")
def init(force: bool, target_dir: Path):
    """在当前目录生成 ipclick.toml 与 .env。

    比 `ipclick -e > 文件` 强在：.env 用 600 权限创建、预填一个随机的 Web 密码、
    已存在时不会闷头覆盖、并把 .env 追加进 .gitignore。
    """
    target_dir.mkdir(parents=True, exist_ok=True)
    toml_path = target_dir / "ipclick.toml"
    env_path = target_dir / ".env"

    existing = [p for p in (toml_path, env_path) if p.exists()]
    if existing and not force:
        for path in existing:
            click.echo(f"已存在，跳过: {path}", err=True)
        click.echo("要覆盖请加 --force。注意 .env 里可能有正在用的密钥。", err=True)
        raise click.Abort()

    toml_path.write_text(example_config(), encoding="utf-8")
    click.echo(f"已生成 {toml_path}")

    # 预填一个随机 Web 密码：留空的话每次重启都会重新生成，运维得盯着控制台。
    password = generate_password()
    env_text = example_env().replace("IPCLICK_WEB_PASSWORD=", f"IPCLICK_WEB_PASSWORD={password}")
    # 先建成 600 再写：先写后 chmod 的话，中间那一瞬间密钥是全局可读的
    with os.fdopen(os.open(env_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as f:
        f.write(env_text)
    click.echo(f"已生成 {env_path}（权限 600，已预填随机 Web 密码）")

    gitignore = target_dir / ".gitignore"
    if gitignore.exists():
        lines = gitignore.read_text(encoding="utf-8").splitlines()
        if not any(line.strip() in (".env", "/.env") for line in lines):
            with gitignore.open("a", encoding="utf-8") as f:
                f.write("\n# IPClick 机密配置\n.env\n")
            click.echo(f"已把 .env 追加进 {gitignore}")
    else:
        click.echo("提示：本目录没有 .gitignore —— 请务必确保 .env 不会被提交", err=True)

    click.echo("")
    click.echo("下一步：把令牌等机密填进 .env，行为配置改 ipclick.toml，然后 ipclick run")
    click.echo("")
    click.echo("组集群时：在**一台**机器上生成共享密钥，再原样复制到其余机器的 .env——")
    click.echo(f"  IPCLICK_CLUSTER_SECRET={generate_password()}")
    click.echo("（刻意不自动写进 .env：每台机器各自生成一个就对不上了）")


@main.command()
@click.option("--config", "-c", type=click.Path(exists=True, path_type=Path), help="配置文件路径")
@click.option("--port", "-p", type=int, help="服务端口号")
@click.option("--host", default=None, help="绑定地址")
@click.option("--verbose", "-v", is_flag=True, help="输出 DEBUG 级别日志")
@click.option("--web", "-w", is_flag=True, default=None, help="同时启动 Web 管理端（登录信息打印到控制台）")
@click.option(
    "--web-port",
    type=int,
    default=None,
    help="Web 管理端端口（覆盖 [WEB].port）。同目录起多个实例时必须岔开，否则第二个起不来",
)
def run(
    config: Path | None,
    port: int | None,
    host: str | None,
    verbose: bool,
    web: bool | None,
    web_port: int | None,
):
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
        if web_port:
            click.echo(f"Override web port: {web_port}")

        serve(config_path=str(config) if config else None, port=port, host=host, web=web, web_port=web_port)

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

        # 机密来源：配错了地方（比如以为写进 toml 生效、其实被环境变量盖了）
        # 是很难自己发现的，这里直接摊开
        click.echo("  机密来源:")
        # 中文是双宽字符，按字符数补空格会对不齐，得按显示宽度算
        width = max(_display_width(s.label) for s in SECRETS)
        for spec in SECRETS:
            pad = " " * (width - _display_width(spec.label))
            click.echo(f"    {spec.label}{pad}  {describe_source(cfg, spec)}")
        click.echo(f"  拦截内网地址: {security.get('block_private_networks', False)}")
        click.echo(f"  拦截元数据端点: {security.get('block_metadata_endpoints', True)}")

        # --- 限流 ---
        limits = LimiterSettings.from_config(downloader_cfg)
        click.echo("")
        click.echo("Per-host limits:")
        if limits.enabled:
            click.echo(f"  并发上限:     {limits.per_host_max_concurrent or '不限'}")
            click.echo(f"  QPS 上限:     {limits.per_host_qps or '不限'}")
            click.echo(f"  等待超时:     {limits.wait_timeout}s")
        else:
            click.echo("  未启用")

        # --- 浏览器渲染 ---
        browser = BrowserSettings.from_config(dict(cfg.get("BROWSER", {})))
        click.echo("")
        click.echo("Browser rendering:")
        if browser.enabled:
            try:
                engine = resolve_engine(browser.engine)
                status = engine_status(engine, browser)
                click.echo(f"  引擎:         {engine}（配置为 {browser.engine}）— {status.label}")
                # 浏览器本体单独一行：只装 Python 包不下本体是最常见的半成品状态，
                # 而 camoufox 在那种状态下第一次用会当场下 1.3 GB
                click.echo(f"  浏览器本体:   {status.detail or '—'}")
            except Exception as e:  # 引擎名配错
                click.echo(f"  引擎:         {browser.engine} — 配置错误: {e}")
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

        # --- 监控与链路 ---
        click.echo("")
        click.echo("Monitoring:")
        click.echo(f"  健康检查:     {monitor.get('health_check', True)}")
        trace = TraceSettings.from_config(dict(cfg.get("TRACE", {})))
        click.echo(f"  链路内存缓冲: {f'最近 {trace.memory_size} 条' if trace.memory_size else '已关闭'}")
        if trace.sqlite_enabled:
            retention = f"保留 {trace.retention_days} 天" if trace.retention_days else "永久保留"
            click.echo(f"  链路落盘:     {trace.sqlite_path}（{retention}）")
        else:
            click.echo("  链路落盘:     未启用（[TRACE].sqlite_enabled = false）")
        if trace.only_errors:
            click.echo("  记录范围:     仅失败请求")

    except click.Abort:
        raise
    except Exception as e:
        click.echo(f"Error loading config: {e}", err=True)
        raise click.Abort() from e


if __name__ == "__main__":
    main()
