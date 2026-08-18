import os
from pathlib import Path
import re
import unicodedata

import click

from ipclick import __version__
from ipclick.adapters.browser_engines import engine_status, resolve_engine
from ipclick.adapters.browser_settings import BrowserSettings
from ipclick.auth import load_tokens

# 从子模块直接导入，不走 `from ipclick.cli import agent`：``cli/__init__.py`` 里的
# `from .main import main` 会让后者变成 __init__ ↔ main 的导入环（运行时能过，
# 但类型检查器会正确地报 reportImportCycles）。
from ipclick.cli.agent import component, config_group, fetch, node, status, trace
from ipclick.cli.skill_cmd import skill
from ipclick.config_loader import load_config
from ipclick.config_loader.loader import example_config, example_env
from ipclick.factory import resolve_mode
from ipclick.health import check_health
from ipclick.limiter import LimiterSettings
from ipclick.ports import DEFAULT_GRPC_PORT, DEFAULT_WEB_PORT
from ipclick.secrets import SECRETS, describe_source
from ipclick.server import serve
from ipclick.tls import TLSSettings, describe
from ipclick.trace import TraceSettings
from ipclick.utils.log_util import LogUtil
from ipclick.web.auth import generate_password
from ipclick.web.server import is_public_host


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
    """IPClick - 分布式HTTP请求代理工具

    \b
    部署：  init / run / health / config-info
    调用：  fetch / status / trace / node / component / config
    接入：  skill —— 输出或安装给 AI 代理用的技能包

    "调用"那一组是给程序（尤其是 AI）用的：每条命令都支持 --json，
    输出一个 JSON 文档到 stdout；退出码分类（0 成功 / 1 失败 / 3 连不上 /
    4 鉴权失败 / 5 参数或配置被拒）。先跑 `ipclick skill show` 看完整用法。
    """
    # 不带子命令也不带 --example 时，给出帮助而不是静默退出
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@main.command()
@click.option("--force", "-f", is_flag=True, help="覆盖已存在的文件")
@click.option("--dir", "-d", "target_dir", type=click.Path(path_type=Path), default=".", help="生成到哪个目录")
@click.option(
    "--port",
    "-p",
    type=int,
    default=None,
    help="按端口命名：生成 ipclick-<端口>.toml 并把端口填进去。同机起多个实例时用",
)
def init(force: bool, target_dir: Path, port: int | None):
    """在当前目录生成 ipclick.toml 与 .env。

    比 `ipclick -e > 文件` 强在：.env 用 600 权限创建、预填一个随机的 Web 密码、
    已存在时不会闷头覆盖、并把 .env 追加进 .gitignore。

    \b
    同一台机器上起多个实例：
        ipclick init --port 8001 && ipclick init --port 8002
        ipclick run --port 8001      # 自动读 ipclick-8001.toml
    """
    target_dir.mkdir(parents=True, exist_ok=True)
    # 带 --port 时按端口命名，且 run --port 会优先找这个文件名（见 loader.candidate_names）。
    # 多实例共用一份配置的症状很隐蔽：两个进程往同一个 trace 库里写，界面上看不出来。
    toml_path = target_dir / (f"ipclick-{port}.toml" if port else "ipclick.toml")
    env_path = target_dir / ".env"

    existing = [p for p in (toml_path, env_path) if p.exists()]
    if existing and not force:
        for path in existing:
            click.echo(f"已存在，跳过: {path}", err=True)
        click.echo("要覆盖请加 --force。注意 .env 里可能有正在用的密钥。", err=True)
        raise click.Abort()

    template = example_config()
    if port:
        # 把端口填进模板：不填的话生成出来的 ipclick-8001.toml 里写着默认端口，
        # `run --port 8001` 能读到它但 `run` 不带参数时读的又是另一个值，
        # 文件名和内容对不上是最容易看走眼的一种。
        template = re.sub(
            r"(?ms)(^\[SERVER\].*?^)port = \d+$",
            lambda m: m.group(1) + f"port = {port}",
            template,
            count=1,
        )
    toml_path.write_text(template, encoding="utf-8")
    click.echo(f"已生成 {toml_path}" + (f"（[SERVER].port = {port}）" if port else ""))

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
    click.echo(
        f"下一步：把令牌等机密填进 .env，行为配置改 {toml_path.name}，然后 "
        f"ipclick run{f' --port {port}' if port else ''}"
    )
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
@click.option(
    "--web-host",
    default=None,
    help="Web 管理端绑定地址（覆盖 [WEB].host）。填 0.0.0.0 让局域网内其他设备也能访问",
)
@click.option(
    "--web-lan",
    is_flag=True,
    default=False,
    help="--web-host 0.0.0.0 的简写：监听所有网卡，供局域网访问",
)
def run(
    config: Path | None,
    port: int | None,
    host: str | None,
    verbose: bool,
    web: bool | None,
    web_port: int | None,
    web_host: str | None,
    web_lan: bool,
):
    """启动IPClick服务"""
    try:
        # 两个都给且不一致时直接报错。悄悄让其中一个赢，会让人对着一个自己没写过
        # 的监听地址排查半天。
        if web_lan:
            if web_host and web_host != "0.0.0.0":
                raise click.UsageError(f"--web-lan 等于 --web-host 0.0.0.0，与 --web-host {web_host} 冲突")
            web_host = "0.0.0.0"

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
        if web_host:
            click.echo(f"Override web host: {web_host}")
            if is_public_host(web_host):
                # 这一条要在启动前就说：等日志刷起来之后没人会往回翻。
                click.echo(
                    "⚠️  Web 管理端将对本机以外开放，且是明文 HTTP（密码在网络上裸奔）。"
                    "请确认这是可信网络，并已配置 [SECURITY].auth_token。",
                    err=True,
                )

        serve(
            config_path=str(config) if config else None,
            port=port,
            host=host,
            web=web,
            web_port=web_port,
            web_host=web_host,
        )

    except click.UsageError:
        raise
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

    resolved_port = port or int(
        dict(load_config(str(config) if config else None).get("SERVER", {})).get("port", DEFAULT_GRPC_PORT)
    )
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

        # 两个端口都打出来，而且各自标明用途。
        #
        # 0.5.0 之前这里只打 "Server port"，Web 端口一个字都不提——而人恰恰是拿
        # 这条命令去回答"我这台机器到底在哪几个端口上"的。少了一半，剩下那一半
        # 又不说自己是 gRPC 的，于是浏览器地址栏里的号、这里打出来的号、文档里
        # 的号三者对不上账。
        web = dict(cfg.get("WEB", {}))
        click.echo("Current configuration:")
        click.echo(f"  Server host:  {server.get('host', '[::]')}")
        click.echo(f"  gRPC port:    {server.get('port', DEFAULT_GRPC_PORT)}   ← 客户端、SDK、其他节点连这个")
        click.echo(
            f"  Web port:     {web.get('port', DEFAULT_WEB_PORT)}   "
            f"← 浏览器打开这个（{'已启用' if web.get('enabled') else '未启用，用 run -w 开'}）"
        )
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


# --------------------------------------------------------------------------- #
# 给程序 / AI 调用的那一组
# --------------------------------------------------------------------------- #
#
# 在文件末尾注册，而不是把命令定义搬进来：那一组有近千行，混在部署命令中间会让
# 这个文件失去"一眼看完 CLI 有哪些入口"的价值。见 ipclick.cli.agent。

main.add_command(fetch)
main.add_command(status)
main.add_command(trace)
main.add_command(node)
main.add_command(component)
main.add_command(config_group)
main.add_command(skill)


if __name__ == "__main__":
    main()
