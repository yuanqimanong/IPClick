"""部署、运行、健康检查和配置诊断命令。"""

from collections.abc import Sequence
import os
from pathlib import Path
import re
import sys
from typing import Any
import unicodedata

import click
from typing_extensions import override

from ipclick import __version__
from ipclick.adapters.browser_engines import engine_status, resolve_engine
from ipclick.adapters.browser_settings import BrowserSettings, describe_max_pages
from ipclick.cli.agent import (
    auth_state,
    component,
    config_group,
    fetch,
    format_grpc_target,
    node,
    status,
    trace,
)
from ipclick.cli.output import Exit, dumps
from ipclick.cli.skill_cmd import skill
from ipclick.config_loader import load_config, placeholders
from ipclick.config_loader.loader import example_config, example_env
from ipclick.factory import resolve_mode
from ipclick.health import check_health
from ipclick.limiter import LimiterSettings
from ipclick.ports import DEFAULT_GRPC_PORT, DEFAULT_WEB_PORT
from ipclick.secrets import SECRETS, describe_source, proxy_config
from ipclick.server import serve
from ipclick.server_settings import ServerSettings
from ipclick.tls import TLSSettings, describe
from ipclick.trace import TraceSettings
from ipclick.utils.config_util import section
from ipclick.utils.log_util import LogUtil
from ipclick.web.auth import generate_password
from ipclick.web.server import is_public_host


def _display_width(text: str) -> int:
    """计算中英文混排终端文本的近似显示宽度。"""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def _print_example(ctx: click.Context, _param: click.Parameter, value: str | None) -> None:
    """Click eager callback：输出配置模板后立即结束命令。"""
    if not value or ctx.resilient_parsing:
        return
    click.echo(example_env() if value == "env" else example_config(), nl=False)
    ctx.exit()


class _JsonAwareGroup(click.Group):
    """带 ``--json`` 时，把参数错误也变成 stdout 上的那一个 JSON 文档。

    SKILL.md 承诺"加 --json 时 stdout 上有且只有一个 JSON 文档，成功失败都是，
    所以 ipclick ... --json | jq 永远安全"。但参数错误由 Click 在命令体运行**之前**
    抛出，走的是它自带的 usage 文本 + stderr 路径——于是 `-H BAD --json` 之类
    stdout 是 0 字节，jq 直接崩，而这正是契约里列为退出码 2 的那一类失败。
    """

    @override
    def main(
        self,
        args: Sequence[str] | None = None,
        prog_name: str | None = None,
        complete_var: str | None = None,
        standalone_mode: bool = True,
        **extra: Any,
    ) -> Any:
        """只在真的要 JSON 时接管错误路径，其余原样交给 Click。"""
        argv = list(sys.argv[1:]) if args is None else list(args)
        if not (standalone_mode and ("--json" in argv or "-J" in argv)):
            return super().main(args, prog_name, complete_var, standalone_mode, **extra)

        try:
            _ = super().main(args, prog_name, complete_var, standalone_mode=False, **extra)
        except click.exceptions.Exit as e:
            # --help / --version / ctx.exit()：Click 自己已经把该印的印完了。
            raise SystemExit(e.exit_code) from None
        except click.UsageError as e:
            click.echo(dumps({"ok": False, "error": e.format_message(), "exit_code": int(Exit.USAGE)}))
            raise SystemExit(int(Exit.USAGE)) from None
        except click.Abort:
            click.echo(dumps({"ok": False, "error": "已中止", "exit_code": int(Exit.FAILED)}))
            raise SystemExit(int(Exit.FAILED)) from None
        except click.ClickException as e:
            click.echo(dumps({"ok": False, "error": e.format_message(), "exit_code": e.exit_code}))
            raise SystemExit(e.exit_code) from None
        raise SystemExit(int(Exit.OK))


@click.group(invoke_without_command=True, cls=_JsonAwareGroup)
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
def main(ctx: click.Context) -> None:
    """IPClick 命令行根命令。"""
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@main.command()
@click.option("--force", "-f", is_flag=True, help="覆盖已存在的文件")
@click.option("--dir", "-d", "target_dir", type=click.Path(path_type=Path), default=".", help="生成到哪个目录")
@click.option(
    "--port",
    "-p",
    # IntRange 而不是裸 int：不校验的话 --port 70000 会生成一份 ipclick 自己都加载不了的
    # 配置（加载器只认 1..65535），--port -1 更是生成出文件名带负号的 ipclick--1.toml。
    type=click.IntRange(1, 65535),
    default=None,
    help="按端口命名：生成 ipclick-<端口>.toml 并把端口填进去。同机起多个实例时用",
)
def init(force: bool, target_dir: Path, port: int | None) -> None:
    """生成行为配置和权限收紧的机密环境文件。"""
    target_dir.mkdir(parents=True, exist_ok=True)
    # `if port` 在这里是安全的（IntRange 已经排除了 0），但仍写成 is not None：
    # 端口的"没传"只有 None 一种，别再把它和某个合法值混为一谈。
    toml_path = target_dir / (f"ipclick-{port}.toml" if port is not None else "ipclick.toml")
    env_path = target_dir / ".env"

    existing = [p for p in (toml_path, env_path) if p.exists()]
    if existing and not force:
        for path in existing:
            # 措辞要说实话：下面 raise Abort 是整体中止，一个文件都不会生成，
            # 说"跳过"会让人以为缺的那个已经补上了。
            click.echo(f"已存在，中止: {path}", err=True)
        click.echo("要覆盖请加 --force。注意 .env 里可能有正在用的密钥。", err=True)
        raise click.Abort()

    template = example_config()
    if port is not None:
        template = re.sub(
            r"(?ms)(^\[SERVER\].*?^)port = \d+$",
            lambda m: m.group(1) + f"port = {port}",
            template,
            count=1,
        )
    toml_path.write_text(template, encoding="utf-8")
    click.echo(f"已生成 {toml_path}" + (f"（[SERVER].port = {port}）" if port else ""))

    password = generate_password()
    env_text = example_env().replace("IPCLICK_WEB_PASSWORD=", f"IPCLICK_WEB_PASSWORD={password}")
    # os.open 的 mode 只在新建文件时生效；覆盖旧文件前也要主动收紧原权限。
    if os.name == "posix" and env_path.exists():
        env_path.chmod(0o600)
    with os.fdopen(os.open(env_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as f:
        _ = f.write(env_text)
    # 上面那个 0o600 只在 POSIX 上真的生效：Windows 的 os.open 把 mode 当成"是否置只读位"，
    # ACL 一动没动。照着 POSIX 报"权限 600"，等于跟 Windows 用户说这份密钥文件已经受保护
    # 了——它没有，而 dotenv 那边的宽权限告警也只在 POSIX 上跑，不会有人来纠正这句话。
    if os.name == "posix":
        click.echo(f"已生成 {env_path}（权限 600，已预填随机 Web 密码）")
    else:
        click.echo(f"已生成 {env_path}（已预填随机 Web 密码）")
        click.echo(
            f"注意：Windows 上没有收紧 {env_path.name} 的权限（600 是 POSIX 的说法）——"
            "里面是密钥，请自行确认本机其他账户读不到。",
            err=True,
        )

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
# IntRange 而不是裸 int：0 和超范围值原先要么被真假值判断当成"没传"，要么一路走到
# 绑定失败才在日志里报错。端口是启动前就能判定的参数，该在参数层直接拒。
@click.option("--port", "-p", type=click.IntRange(1, 65535), help="服务端口号")
@click.option("--host", default=None, help="绑定地址")
@click.option("--verbose", "-v", is_flag=True, help="输出 DEBUG 级别日志")
@click.option("--web", "-w", is_flag=True, default=None, help="同时启动 Web 管理端（登录信息打印到控制台）")
@click.option(
    "--web-port",
    type=click.IntRange(1, 65535),
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
) -> None:
    """按配置和命令行覆盖项启动 gRPC 服务及可选 Web 管理端。"""
    try:
        if web_lan:
            if web_host and web_host != "0.0.0.0":
                raise click.UsageError(f"--web-lan 等于 --web-host 0.0.0.0，与 --web-host {web_host} 冲突")
            web_host = "0.0.0.0"

        LogUtil.init(level="DEBUG" if verbose else "INFO")

        click.echo("Starting IPClick server...")
        if config:
            click.echo(f"Using config file: {config}")
        if port is not None:
            click.echo(f"Override port: {port}")
        if host:
            click.echo(f"Override host: {host}")
        if web_port:
            click.echo(f"Override web port: {web_port}")
        if web_host:
            click.echo(f"Override web host: {web_host}")
            if is_public_host(web_host):
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
            # 不传的话，IPClickServer 里那次 init_from_config 会把上面 LogUtil.init 设的
            # DEBUG 换回 [LOG].level，-v 就只对建 server 之前的几行生效。
            verbose=verbose,
        )

    except click.UsageError:
        raise
    except KeyboardInterrupt:
        click.echo("\nShutting down...")
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        raise click.Abort() from e


@main.command()
@click.option("--host", default=None, help="服务端地址（默认取配置，[::]/0.0.0.0 会当成 127.0.0.1）")
@click.option("--port", "-p", type=int, help="服务端端口（默认取配置）")
@click.option("--config", "-c", type=click.Path(path_type=Path), help="配置文件路径")
@click.option("--service", default="", help="要查询的服务名，默认查总体状态")
# FloatRange 而不是裸 float：--timeout -5 会让 gRPC 立刻 DEADLINE_EXCEEDED，
# 于是一个健康的服务端被报成挂了——而 --timeout abc 早就是 exit 2 了，两者该一致。
@click.option(
    "--timeout",
    type=click.FloatRange(min=0, min_open=True),
    default=5.0,
    show_default=True,
    help="超时（秒）",
)
def health(host: str | None, port: int | None, config: Path | None, service: str, timeout: float) -> None:
    """调用标准 gRPC health 服务并通过退出码报告结果。"""
    from ipclick.cli.agent import server_host, server_port

    LogUtil.init(level="ERROR")

    config_data = load_config(str(config) if config else None)
    # 复用 agent 里那两个解析函数，别再实现一遍：原来这里的 int(...) 没有 try/except，
    # 配置里写了 port = "abc"（合法 TOML）时 health 会抛裸 ValueError，而 status 处理得了。
    # 顺带 host 也统一成"通配监听地址映射回本机"的口径。
    resolved_port = server_port(config_data, port)
    target = format_grpc_target(server_host(config_data, host), resolved_port)
    tls = TLSSettings.from_config(section(config_data, "SECURITY"))

    healthy, status = check_health(target, service=service, timeout=timeout, tls=tls)
    click.echo(f"{target} -> {status}")
    if not healthy:
        raise SystemExit(1)


@main.command("config-info")
@click.option("--config", "-c", type=click.Path(path_type=Path), help="配置文件路径")
def config_info(config: Path | None) -> None:
    """展示经过默认值、覆盖项和路径占位符解析后的关键配置。"""
    try:
        cfg = load_config(str(config) if config else None)

        server = section(cfg, "SERVER")
        # 用 proxy_config 而不是裸 section：凭据走 IPCLICK_PROXY_AUTH_KEY 环境变量时
        # （文档推荐的方式）裸 section 里没有 auth_key，"代理鉴权: 已配置"永远不显示。
        proxy = proxy_config(cfg)
        security = section(cfg, "SECURITY")
        server_settings = ServerSettings.from_config(server)
        log_cfg = placeholders.resolve_for("LOG", section(cfg, "LOG"), server_settings.port)
        downloader_cfg = section(cfg, "DOWNLOADER")
        monitor = section(cfg, "MONITOR")

        web = section(cfg, "WEB")
        click.echo("Current configuration:")
        click.echo(f"  Server host:  {server.get('host', '[::]')}")
        click.echo(f"  gRPC port:    {server.get('port', DEFAULT_GRPC_PORT)}   ← 客户端、SDK、其他节点连这个")
        click.echo(
            f"  Web port:     {web.get('port', DEFAULT_WEB_PORT)}   "
            f"← 浏览器打开这个（{'已启用' if web.get('enabled') else '未启用，用 run -w 开'}）"
        )
        click.echo(f"  Max workers:  {server_settings.max_workers}")
        click.echo(f"  Log level:    {log_cfg.get('level', 'info')}")
        click.echo(f"  Log output:   {log_cfg.get('output', 'stdout')}")

        click.echo("")
        click.echo("Security:")
        click.echo(f"  传输层:       {describe(TLSSettings.from_config(security))}")
        _required, token_note = auth_state(cfg)
        click.echo(f"  令牌鉴权:     {token_note}")

        click.echo("  机密来源:")
        width = max(_display_width(s.label) for s in SECRETS)
        for spec in SECRETS:
            pad = " " * (width - _display_width(spec.label))
            click.echo(f"    {spec.label}{pad}  {describe_source(cfg, spec)}")
        click.echo(f"  拦截内网地址: {security.get('block_private_networks', False)}")
        click.echo(f"  拦截元数据端点: {security.get('block_metadata_endpoints', True)}")

        limits = LimiterSettings.from_config(downloader_cfg)
        click.echo("")
        click.echo("Per-host limits:")
        if limits.enabled:
            click.echo(f"  并发上限:     {limits.per_host_max_concurrent or '不限'}")
            click.echo(f"  QPS 上限:     {limits.per_host_qps or '不限'}")
            click.echo(f"  等待超时:     {limits.wait_timeout}s")
        else:
            click.echo("  未启用")

        browser = BrowserSettings.from_config(section(cfg, "BROWSER"))
        click.echo("")
        click.echo("Browser rendering:")
        if browser.enabled:
            engine = ""
            try:
                engine = resolve_engine(browser.engine)
                status = engine_status(engine, browser)
                click.echo(f"  引擎:         {engine}（配置为 {browser.engine}）— {status.label}")
                click.echo(f"  浏览器本体:   {status.detail or '—'}")
            except Exception as e:
                click.echo(f"  引擎:         {browser.engine} — 配置错误: {e}")
            click.echo(f"  页面上限:     {describe_max_pages(browser.max_pages, engine)}")
            click.echo(f"  允许页内 JS:  {browser.allow_scripts}")
        else:
            click.echo("  已关闭")

        click.echo("")
        click.echo("Client:")
        try:
            click.echo(f"  运行模式:     {resolve_mode(cfg)}")
        except Exception as e:
            click.echo(f"  运行模式:     配置错误: {e}")
        nodes = cfg.get("CLUSTER", {}).get("nodes", [])
        discovery = str(dict(cfg.get("CLUSTER", {}).get("discovery") or {}).get("mode") or "static")
        click.echo(f"  集群节点:     {len(nodes)} 个（发现方式 {discovery}）")

        proxy_host = str(proxy.get("host") or "").strip()
        tunnel_server = str(proxy.get("tunnel_server") or "").strip()
        proxy_display = f"{proxy_host}:{proxy.get('port', '')}" if proxy_host else tunnel_server or "未配置"
        click.echo(f"  代理:         {proxy_display}")
        if proxy.get("auth_key"):
            click.echo("  代理鉴权:     已配置（已隐藏）")

        click.echo("")
        click.echo("Monitoring:")
        click.echo(f"  健康检查:     {monitor.get('health_check', True)}")
        trace = TraceSettings.from_config(
            placeholders.resolve_for("TRACE", section(cfg, "TRACE"), server_settings.port)
        )
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


main.add_command(fetch)
main.add_command(status)
main.add_command(trace)
main.add_command(node)
main.add_command(component)
main.add_command(config_group)
main.add_command(skill)


if __name__ == "__main__":
    main()
