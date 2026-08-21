"""面向脚本和 AI 代理的结构化查询、请求与运维命令。"""

from __future__ import annotations

import base64
from pathlib import Path
import sys
import time
from typing import TYPE_CHECKING, Any

import click

from ipclick import __version__
from ipclick.cli.output import DEFAULT_BODY_LIMIT, Exit, classify, emit, fail, json_option, note
from ipclick.config_loader import load_config, placeholders
from ipclick.ports import DEFAULT_GRPC_PORT, DEFAULT_WEB_PORT
from ipclick.utils.config_util import Settings, section


if TYPE_CHECKING:
    from ipclick.trace import TraceReader


def config_option(func: Any) -> Any:
    """为命令添加统一的配置文件选项。"""
    return click.option(
        "--config",
        "-c",
        type=click.Path(path_type=Path),
        default=None,
        help="配置文件路径（默认找当前目录的 ipclick.toml）",
    )(func)


def server_options(func: Any) -> Any:
    """为需要连接 gRPC 服务端的命令添加地址、端口和令牌选项。"""
    func = click.option("--token", default=None, help="gRPC 鉴权令牌（覆盖 IPCLICK_AUTH_TOKEN 与配置文件）")(func)
    func = click.option("--port", "-p", type=int, default=None, help="服务端端口（默认取配置）")(func)
    func = click.option("--host", default=None, help="服务端地址（默认取配置，[::]/0.0.0.0 会当成 127.0.0.1）")(func)
    return config_option(func)


def _load(config: Path | None, as_json: bool = False) -> Settings:
    """预检显式 TOML 后加载配置，并按输出模式报告错误。"""
    if config is not None:
        import tomllib

        try:
            _ = tomllib.loads(config.read_text(encoding="utf-8"))
        except OSError as e:
            fail(f"读不了配置文件 {config}：{e}", Exit.REJECTED, as_json=as_json)
        except tomllib.TOMLDecodeError as e:
            fail(f"配置文件 {config} 不是合法 TOML：{e}", Exit.REJECTED, as_json=as_json)

    try:
        return load_config(str(config) if config else None)
    except Exception as e:
        fail(f"读取配置失败：{type(e).__name__}: {e}", Exit.REJECTED, as_json=as_json)


def _quiet_logs() -> None:
    """将内部日志压到 ERROR，保护 CLI 的 stdout 输出契约。"""
    from ipclick.utils.log_util import LogUtil

    LogUtil.init(level="ERROR")


def _server_port(config: Settings, port: int | None) -> int:
    """解析命令行优先的 gRPC 端口。"""
    # `if port` 会把 --port 0 当成"没传"而静默回落到配置端口；0 是非法端口，
    # 该让它走到 format_grpc_target 里被明确拒绝，而不是伪装成默认值。
    if port is not None:
        return port
    try:
        return int(section(config, "SERVER").get("port", DEFAULT_GRPC_PORT))
    except (TypeError, ValueError):
        return DEFAULT_GRPC_PORT


def _server_host(config: Settings, host: str | None) -> str:
    """解析命令行优先的连接主机，并把监听通配地址映射到本机。"""
    resolved = str(host if host is not None else section(config, "SERVER").get("host") or "127.0.0.1").strip()
    return "127.0.0.1" if resolved in ("", "*", "[::]", "::", "0.0.0.0") else resolved


def format_grpc_target(host: str, port: int) -> str:
    """组装兼容 IPv4、主机名和 IPv6 字面量的 gRPC target。

    端口必须先落在 1..65535 内。gRPC 底层会把超范围的端口按 mod 65536 回绕，
    于是 ``health -p 75064`` 实际连的是 9528 并回答 SERVING、退出码 0——
    对着一个不可能存在的端口报健康，比直接报错危险得多。
    """
    if not 1 <= port <= 65535:
        raise click.UsageError(f"端口必须在 1..65535 之间，收到 {port}")
    normalized = host.strip()
    target_host = normalized if normalized.startswith("[") else f"[{normalized}]" if ":" in normalized else normalized
    return f"{target_host}:{port}"


def _parse_pairs(values: tuple[str, ...], separator: str, what: str) -> dict[str, str]:
    """解析可重复的 ``key<separator>value`` 命令行选项。"""
    out: dict[str, str] = {}
    for raw in values:
        key, sep, value = raw.partition(separator)
        if not sep or not key.strip():
            raise click.UsageError(f"{what} 应写成 {'名称' + separator + '值'!r} 的形式，收到 {raw!r}")
        out[key.strip()] = value.strip()
    return out


def _read_body(value: str) -> bytes:
    """读取字面量、``@文件`` 或 ``@-`` 标准输入形式的请求体。"""
    if not value.startswith("@"):
        return value.encode("utf-8")
    path = value[1:]
    if path == "-":
        return sys.stdin.buffer.read()
    try:
        return Path(path).read_bytes()
    except OSError as e:
        raise click.UsageError(f"读不了请求体文件 {path!r}：{e}") from e


def _body_payload(content: bytes, limit: int) -> dict[str, Any]:
    """为 JSON 输出编码响应体，并保证不返回无法解码的半截 base64。"""
    try:
        decoded = content.decode("utf-8")
    except UnicodeDecodeError:
        encoded = base64.b64encode(content).decode("ascii")
        if limit and len(encoded) > limit:
            return {
                "body": "",
                "body_encoding": "base64",
                "body_truncated": True,
                "body_note": (
                    f"响应体是二进制（{len(content)} 字节），base64 后 {len(encoded)} 字符，超过上限 {limit}。"
                    f"截断的 base64 解不出东西，所以这里不给——请用 -o 文件 取完整字节，或 --max-body 0"
                ),
            }
        return {
            "body": encoded,
            "body_encoding": "base64",
            "body_truncated": False,
            "body_note": f"响应体不是合法 UTF-8，已按 base64 给出（原始 {len(content)} 字节）",
        }

    if limit and len(decoded) > limit:
        return {
            "body": decoded[:limit],
            "body_encoding": "utf-8",
            "body_truncated": True,
            "body_note": f"已截断到 {limit} 字符（共 {len(decoded)}）。要完整内容请用 -o 文件 或 --max-body 0",
        }
    return {"body": decoded, "body_encoding": "utf-8", "body_truncated": False}


@click.command()
@click.argument("url")
@server_options
@click.option("--method", "-X", default="GET", show_default=True, help="HTTP 方法")
@click.option("--header", "-H", "headers", multiple=True, help="请求头，形如 'Name: value'，可重复")
@click.option("--cookie", "cookies", multiple=True, help="Cookie，形如 'k=v'，可重复")
@click.option("--param", "params", multiple=True, help="查询参数，形如 'k=v'，可重复")
@click.option("--data", "-d", default=None, help="请求体。@路径 从文件读，@- 从 stdin 读")
@click.option("--json-body", default=None, help="JSON 请求体（一段 JSON 文本；@路径 从文件读）")
@click.option("--adapter", "-a", default=None, help="适配器：curl_cffi / niquests / browser / camoufox …")
@click.option("--proxy", default=None, help="代理 URL；填 'config' 表示用配置文件里的 [PROXY]")
@click.option("--timeout", type=float, default=60.0, show_default=True, help="单次请求超时（秒）")
@click.option("--retries", type=int, default=3, show_default=True, help="适配器内部重试次数")
@click.option("--impersonate", default=None, help="curl_cffi 的浏览器指纹，如 chrome124")
@click.option("--no-verify", is_flag=True, default=False, help="不校验目标站点的 TLS 证书")
@click.option("--no-redirects", is_flag=True, default=False, help="不跟随重定向")
@click.option("--output", "-o", type=click.Path(path_type=Path), default=None, help="把响应体完整写进文件（不截断）")
@click.option(
    "--max-body",
    type=click.IntRange(min=0),
    default=DEFAULT_BODY_LIMIT,
    show_default=True,
    help="--json 输出里响应体的字符上限；0 = 不限制",
)
@click.option("--ignore-status", is_flag=True, default=False, help="只要拿到了响应就算成功（4xx/5xx 也退出 0）")
@json_option
def fetch(
    url: str,
    config: Path | None,
    host: str | None,
    port: int | None,
    token: str | None,
    method: str,
    headers: tuple[str, ...],
    cookies: tuple[str, ...],
    params: tuple[str, ...],
    data: str | None,
    json_body: str | None,
    adapter: str | None,
    proxy: str | None,
    timeout: float,
    retries: int,
    impersonate: str | None,
    no_verify: bool,
    no_redirects: bool,
    output: Path | None,
    max_body: int,
    ignore_status: bool,
    as_json: bool,
) -> None:
    """经 IPClick 服务端发送一次 HTTP 请求并输出稳定结果结构。"""
    import json as json_lib

    from ipclick.dto.models import HttpMethod, IPClickAdapter
    from ipclick.sdk import Downloader

    _quiet_logs()
    _ = _load(config, as_json)

    try:
        http_method = HttpMethod[method.strip().upper()]
    except KeyError:
        raise click.UsageError(f"未知的 HTTP 方法 {method!r}，可选：{', '.join(m.name for m in HttpMethod)}") from None

    resolved_adapter: str | IPClickAdapter | None = None
    if adapter:
        try:
            resolved_adapter = IPClickAdapter.from_str(adapter)
        except ValueError as e:
            fail(str(e), Exit.USAGE, as_json=as_json)

    payload: Any = None
    if data is not None:
        payload = _read_body(data)
    parsed_json: Any = None
    if json_body is not None:
        raw = _read_body(json_body).decode("utf-8", errors="replace")
        try:
            parsed_json = json_lib.loads(raw)
        except ValueError as e:
            raise click.UsageError(f"--json-body 不是合法 JSON：{e}") from None

    started = time.monotonic()
    downloader: Downloader | None = None
    try:
        downloader = Downloader(
            config_path=str(config) if config else None,
            host=host,
            port=port,
            token=token,
        )
        response = downloader.request(
            url=url,
            method=http_method,
            adapter=resolved_adapter,
            headers=_parse_pairs(headers, ":", "请求头") or None,
            cookies=_parse_pairs(cookies, "=", "Cookie") or None,
            params=_parse_pairs(params, "=", "查询参数") or None,
            data=payload,
            json=parsed_json,
            proxy=True if proxy == "config" else proxy,
            timeout=timeout,
            max_retries=retries,
            verify=not no_verify,
            allow_redirects=not no_redirects,
            impersonate=impersonate,
        )
    except click.ClickException:
        raise
    except Exception as e:
        fail(f"{type(e).__name__}: {e}", classify(e), as_json=as_json, url=url)
    finally:
        if downloader is not None:
            downloader.close()

    if output is not None:
        try:
            _ = output.write_bytes(response.content)
        except OSError as e:
            fail(f"写文件失败：{e}", Exit.FAILED, as_json=as_json)

    reached_server = response.status_code >= 0 or bool(response.trace.node_id) or bool(response.request_uuid)
    attempt_failed = (
        response.status_code < 0 or bool(response.error) or (response.status_code >= 400 and not ignore_status)
    )
    if not reached_server:
        code = Exit.UNREACHABLE
    elif attempt_failed:
        code = Exit.FAILED
    else:
        code = Exit.OK

    result: dict[str, Any] = {
        "ok": code is Exit.OK,
        "exit_code": int(code),
        "reached_server": reached_server,
        "url": response.url or url,
        "status": response.status_code,
        "elapsed_ms": response.elapsed_ms or int((time.monotonic() - started) * 1000),
        "size": len(response.content),
        "adapter": response.adapter_type,
        "request_uuid": response.request_uuid,
        "error": response.error,
        "trace": {
            "node_id": response.trace.node_id,
            "adapter": response.trace.adapter,
            "attempts": response.trace.attempts,
            "forwarded": response.trace.forwarded,
            "queued_ms": response.trace.queued_ms,
        },
        "headers": response.headers,
    }
    if output is not None:
        result["saved_to"] = str(output)
    if output is None:
        result.update(_body_payload(response.content, max_body))
    else:
        # ``-o`` 已承担完整正文交付；JSON 再复制一份会让大文件占用双倍内存并淹没 stdout。
        result.update({"body_omitted": True, "body_note": "响应体已完整写入 saved_to，未在 JSON 中重复输出"})

    if as_json:
        emit(result, as_json=True)
    else:
        note(f"{response.status_code} {response.url or url}  {response.elapsed_ms}ms  {len(response.content)}B")
        if response.trace.node_id or response.trace.attempts > 1:
            note(
                f"  节点 {response.trace.node_id or '—'} · 适配器 {response.trace.adapter or response.adapter_type}"
                f" · 尝试 {response.trace.attempts} 次"
                + (f" · 排队 {response.trace.queued_ms}ms" if response.trace.queued_ms else "")
            )
        if response.error:
            note(f"  错误：{response.error}")
        if output is not None:
            note(f"  已写入 {output}")
        else:
            click.get_binary_stream("stdout").write(response.content)

    if code is not Exit.OK:
        raise SystemExit(int(code))


@click.command()
@server_options
@click.option("--timeout", type=float, default=5.0, show_default=True, help="健康检查超时（秒）")
@click.option("--probe-nodes", is_flag=True, default=False, help="顺带探测集群里每个节点（会慢一些）")
@json_option
def status(
    config: Path | None,
    host: str | None,
    port: int | None,
    token: str | None,
    timeout: float,
    probe_nodes: bool,
    as_json: bool,
) -> None:
    """汇总服务健康、端口、安全、适配器、链路和集群状态。"""
    from ipclick.adapters.browser_settings import BrowserSettings
    from ipclick.auth import load_tokens
    from ipclick.components import snapshot as components_snapshot
    from ipclick.factory import resolve_mode
    from ipclick.health import check_health
    from ipclick.tls import TLSSettings, describe
    from ipclick.trace import TraceSettings

    _quiet_logs()
    _ = token
    config_data = _load(config, as_json)
    resolved_port = _server_port(config_data, port)
    resolved_host = _server_host(config_data, host)
    target = format_grpc_target(resolved_host, resolved_port)

    try:
        security = section(config_data, "SECURITY")
        tls_settings = TLSSettings.from_config(security)
        browser = BrowserSettings.from_config(section(config_data, "BROWSER"))
        trace_settings = TraceSettings.from_config(
            placeholders.resolve_for("TRACE", section(config_data, "TRACE"), resolved_port)
        )
        web_port = int(section(config_data, "WEB").get("port", DEFAULT_WEB_PORT))
        components = components_snapshot(browser)
        nodes = _node_entries(config_data)
    except Exception as e:
        fail(f"状态配置无效：{type(e).__name__}: {e}", Exit.REJECTED, as_json=as_json)

    healthy, health_detail = check_health(target, timeout=timeout, tls=tls_settings)
    try:
        mode = resolve_mode(config_data)
    except Exception as e:
        mode = f"配置错误: {e}"

    probes: list[dict[str, Any]] = []
    if probe_nodes and nodes:
        probes = [_probe_one(config_data, entry, timeout) for entry in nodes]

    payload: dict[str, Any] = {
        "ok": healthy,
        "exit_code": int(Exit.OK if healthy else Exit.UNREACHABLE),
        "version": __version__,
        "server": {
            "target": target,
            "healthy": healthy,
            "detail": health_detail,
            "mode": mode,
            "grpc_port": resolved_port,
            "web_port": web_port,
            "web_enabled_in_config": bool(section(config_data, "WEB").get("enabled", False)),
            "web_reachable": _port_open(resolved_host.strip("[]"), web_port),
        },
        "security": {
            "auth_token_configured": bool(load_tokens(security)),
            "tls": describe(tls_settings),
            "block_private_networks": bool(security.get("block_private_networks", False)),
            "block_metadata_endpoints": bool(security.get("block_metadata_endpoints", True)),
        },
        "adapters": {
            "ready": ["curl_cffi", *[c["name"] for c in components if c["ready"]]],
            "components": components,
            "browser_enabled": browser.enabled,
            "browser_engine": browser.engine,
            "note": "以本机磁盘为准；服务端进程启动之后才装的组件，要重启它才认得",
        },
        "trace": {
            "memory_size": trace_settings.memory_size,
            "sqlite_enabled": trace_settings.sqlite_enabled,
            "sqlite_path": trace_settings.sqlite_path if trace_settings.sqlite_enabled else None,
            "only_errors": trace_settings.only_errors,
        },
        "cluster": {"nodes": nodes, "probes": probes},
    }

    if as_json:
        emit(payload, as_json=True)
    else:
        click.echo(f"IPClick {__version__}")
        click.echo(f"  服务端      {target} — {'健康' if healthy else '不可用'}（{health_detail}）")
        web_state = "在听" if payload["server"]["web_reachable"] else "没人听"
        click.echo(f"  端口        gRPC {resolved_port} · Web {web_port}（{web_state}）")
        click.echo(f"  运行模式    {mode}")
        click.echo(f"  令牌鉴权    {'已配置' if payload['security']['auth_token_configured'] else '未配置'}")
        click.echo(f"  可用适配器  {', '.join(payload['adapters']['ready'])}")
        not_ready = [f"{c['name']}（{c['install']}）" for c in components if not c["ready"]]
        if not_ready:
            click.echo(f"  未就绪      {', '.join(not_ready)}")
        click.echo(
            "  链路落盘    "
            + (f"{trace_settings.sqlite_path}" if trace_settings.sqlite_enabled else "未启用（只有内存缓冲）")
        )
        click.echo(f"  集群节点    {len(nodes)} 个")
        for probe in probes:
            click.echo(f"    {probe['node_id']:<16} {'OK' if probe['ok'] else '失败'} — {probe['detail']}")

    if not healthy:
        raise SystemExit(int(Exit.UNREACHABLE))


@click.group()
def trace() -> None:
    """查询已落盘的请求链路。"""


def _reader(config_data: Settings, port: int | None, as_json: bool) -> TraceReader:
    """按端口解析链路库路径，并验证该库可读。"""
    from ipclick.trace import TraceReader, TraceSettings

    resolved_port = _server_port(config_data, port)
    settings = TraceSettings.from_config(
        placeholders.resolve_for("TRACE", section(config_data, "TRACE"), resolved_port)
    )
    if not settings.sqlite_enabled:
        fail(
            "[TRACE].sqlite_enabled = false —— 没有可查的库。"
            "链路只在服务端进程的内存里，请打开落盘后重启，或用 Web 端的「请求流」页。",
            Exit.REJECTED,
            as_json=as_json,
        )
    reader = TraceReader(settings.sqlite_path)
    if not reader.exists():
        fail(
            f"找不到链路库 {reader.path}。检查是不是在别的工作目录起的服务，"
            f"或 [TRACE].sqlite_path 里的 {{port}} 占位符解析成了别的端口（当前按 {resolved_port} 解析）。",
            Exit.REJECTED,
            as_json=as_json,
        )
    return reader


def _record_dict(record: Any) -> dict[str, Any]:
    """把链路记录转换为稳定的 CLI 字段集合。"""
    return {
        "ts": record.ts,
        "when": record.when,
        "uuid": record.uuid,
        "node_id": record.node_id,
        "adapter": record.adapter,
        "method": record.method,
        "url": record.url,
        "host": record.host,
        "status": record.status_code,
        "status_class": record.status_class,
        "duration_ms": record.duration_ms,
        "size": record.size,
        "attempts": record.attempts,
        "forwarded": record.forwarded,
        "queued_ms": record.queued_ms,
        "error": record.error,
        "stream": record.stream,
    }


@trace.command("list")
@config_option
@click.option("--port", "-p", type=int, default=None, help="服务端端口（只用于解析路径里的 {port} 占位符）")
@click.option("--limit", "-n", type=int, default=20, show_default=True, help="最多几条")
@click.option("--offset", type=int, default=0, show_default=True, help="跳过前几条")
@click.option(
    "--status",
    "status_class",
    type=click.Choice(["", "2xx", "3xx", "4xx", "5xx", "failure", "error"]),
    default="",
    help="按状态类筛选。error = 所有失败（4xx/5xx/连接失败）",
)
@click.option("--adapter", default="", help="按适配器筛选（精确匹配）")
@click.option("--keyword", "-k", default="", help="URL 里包含这个子串")
@click.option("--since", type=float, default=None, help="只看最近多少小时")
@json_option
def trace_list(
    config: Path | None,
    port: int | None,
    limit: int,
    offset: int,
    status_class: str,
    adapter: str,
    keyword: str,
    since: float | None,
    as_json: bool,
) -> None:
    """按过滤条件倒序列出请求链路。"""
    _quiet_logs()
    config_data = _load(config, as_json)
    reader = _reader(config_data, port, as_json)

    records = reader.query(
        limit=limit,
        offset=offset,
        since=time.time() - since * 3600 if since else None,
        status_class=status_class,
        adapter=adapter,
        keyword=keyword,
    )
    rows = [_record_dict(r) for r in records]

    if as_json:
        emit({"ok": True, "source": "sqlite", "path": reader.path, "count": len(rows), "records": rows}, as_json=True)
        return

    if not rows:
        click.echo("（没有匹配的记录）")
        return
    for row in rows:
        marker = "✓" if 200 <= row["status"] < 400 else "✗"
        click.echo(
            f"{row['when']}  {marker} {row['status']:>4}  {row['method']:<7} {row['duration_ms']:>6}ms  "
            f"{row['adapter']:<12} {row['url']}"
        )
        if row["error"]:
            click.echo(f"{'':>19}   └ {row['error']}")


@trace.command("stats")
@config_option
@click.option("--port", "-p", type=int, default=None, help="服务端端口（只用于解析路径里的 {port} 占位符）")
@click.option("--days", type=int, default=7, show_default=True, help="统计最近多少天；0 = 全部")
@click.option("--top", type=int, default=10, show_default=True, help="目标站点排行取前几名")
@json_option
def trace_stats(config: Path | None, port: int | None, days: int, top: int, as_json: bool) -> None:
    """统计链路库的成功率、适配器分布和目标站点排行。"""
    _quiet_logs()
    config_data = _load(config, as_json)
    reader = _reader(config_data, port, as_json)

    since = time.time() - days * 86400 if days > 0 else None
    summary = reader.summary(since)
    rows = reader.count()
    db_bytes = reader.db_size()
    top_hosts = reader.top_hosts(since, limit=top)
    payload: dict[str, Any] = {
        "ok": True,
        "path": reader.path,
        "window_days": days,
        "rows": rows,
        "db_bytes": db_bytes,
        "summary": summary,
        "daily": reader.daily(days) if days > 0 else [],
        "top_hosts": top_hosts,
    }

    if as_json:
        emit(payload, as_json=True)
        return

    click.echo(f"库 {reader.path}（{rows} 行，{db_bytes / 1024 / 1024:.1f} MB）")
    click.echo(f"最近 {days} 天" if days > 0 else "全部记录")
    click.echo(
        f"  总数 {summary['total']} · 成功 {summary['ok']} · 失败 {summary['failed']} · "
        f"成功率 {summary['success_rate']}% · 平均 {summary['avg_ms']}ms"
    )
    for name, stat in summary.get("by_adapter", {}).items():
        click.echo(f"    {name:<14} {stat['total']:>7} 次 · 失败 {stat['failed']:>5} · 平均 {stat['avg_ms']}ms")
    if top_hosts:
        click.echo("  目标站点：")
        for entry in top_hosts:
            click.echo(f"    {entry['host']:<32} {entry['total']:>7} 次 · 失败 {entry['failed']:>5}")


@click.group()
def node() -> None:
    """查看和探测集群节点。"""


def _port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    """用短 TCP 连接判断指定端口是否有人监听。"""
    import socket

    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _node_entries(config_data: Settings) -> list[dict[str, Any]]:
    """生成不泄露节点令牌的集群配置摘要。"""
    raw = config_data.get("CLUSTER", {}).get("nodes", []) or []
    out: list[dict[str, Any]] = []
    for index, entry in enumerate(raw):
        item = dict(entry or {})
        address = str(item.get("address", "")).strip()
        out.append(
            {
                "id": str(item.get("id") or address or f"#{index}"),
                "address": address,
                "weight": item.get("weight", 100),
                "region": item.get("region", ""),
                "has_token": bool(str(item.get("token") or "").strip()),
            }
        )
    return out


def _probe_one(config_data: Settings, entry: dict[str, Any], timeout: float) -> dict[str, Any]:
    """解析并探测单个节点的连通性和内部鉴权。"""
    from ipclick.cluster.node import Node
    from ipclick.cluster.probe import probe_node
    from ipclick.cluster.tokens import cluster_secret
    from ipclick.tls import TLSSettings

    try:
        parsed = Node.from_config({"id": entry["id"], "address": entry["address"]})
    except Exception as e:
        return {"node_id": entry["id"], "address": entry["address"], "ok": False, "detail": f"节点配置不合法：{e}"}

    result = probe_node(
        parsed,
        secret=cluster_secret(section(config_data, "CLUSTER")),
        tls=TLSSettings.from_config(section(config_data, "SECURITY")),
        timeout=timeout,
    )
    return result.snapshot()


@node.command("list")
@config_option
@json_option
def node_list(config: Path | None, as_json: bool) -> None:
    """列出配置中的集群节点与负载均衡策略。"""
    _quiet_logs()
    config_data = _load(config, as_json)
    nodes = _node_entries(config_data)
    cluster = section(config_data, "CLUSTER")

    if as_json:
        emit(
            {
                "ok": True,
                "forward": str(cluster.get("forward", "off")),
                "self_id": str(cluster.get("self_id", "") or ""),
                "load_balancer": str(cluster.get("load_balancer", "round_robin")),
                "count": len(nodes),
                "nodes": nodes,
            },
            as_json=True,
        )
        return

    if not nodes:
        click.echo("（配置里没有集群节点）")
        return
    click.echo(f"转发 {cluster.get('forward', 'off')} · 策略 {cluster.get('load_balancer', 'round_robin')}")
    for entry in nodes:
        click.echo(f"  {entry['id']:<20} {entry['address']:<24} 权重 {entry['weight']}")


@node.command("probe")
@config_option
@click.argument("node_id", required=False, default="")
@click.option("--address", default="", help="直接探这个地址（host:port），不必在配置里声明")
@click.option("--timeout", type=float, default=5.0, show_default=True, help="单次探测超时（秒）")
@json_option
def node_probe(config: Path | None, node_id: str, address: str, timeout: float, as_json: bool) -> None:
    """探测一个、全部或临时指定的集群节点。"""
    _quiet_logs()
    config_data = _load(config, as_json)

    if address:
        targets = [{"id": node_id or address, "address": address, "weight": 100, "region": "", "has_token": False}]
    else:
        targets = _node_entries(config_data)
        if node_id:
            targets = [t for t in targets if t["id"] == node_id]
            if not targets:
                fail(f"配置里没有 id 为 {node_id!r} 的节点", Exit.REJECTED, as_json=as_json)
    if not targets:
        fail("配置里没有集群节点，也没给 --address", Exit.REJECTED, as_json=as_json)

    results = [_probe_one(config_data, entry, timeout) for entry in targets]
    all_ok = all(r.get("ok") for r in results)

    if as_json:
        emit(
            {
                "ok": all_ok,
                "exit_code": int(Exit.OK if all_ok else Exit.FAILED),
                "count": len(results),
                "probes": results,
            },
            as_json=True,
        )
    else:
        for result in results:
            mark = "✓" if result.get("ok") else "✗"
            click.echo(f"{mark} {result.get('node_id', '')} {result.get('address', '')}")
            click.echo(f"    {result.get('detail', '')}")

    if not all_ok:
        raise SystemExit(int(Exit.FAILED))


@click.group()
def component() -> None:
    """查看、安装或卸载白名单内的可选组件。"""


@component.command("list")
@config_option
@json_option
def component_list(config: Path | None, as_json: bool) -> None:
    """列出组件、浏览器本体及可用包管理器状态。"""
    from ipclick.adapters.browser_settings import BrowserSettings
    from ipclick.components import snapshot as components_snapshot
    from ipclick.web.installer import detect_toolchain

    _quiet_logs()
    config_data = _load(config, as_json)
    browser = BrowserSettings.from_config(section(config_data, "BROWSER"))
    components = components_snapshot(browser)
    toolchain = detect_toolchain()

    if as_json:
        emit(
            {
                "ok": True,
                "toolchain": toolchain.describe() if toolchain else None,
                "components": components,
            },
            as_json=True,
        )
        return

    click.echo(f"包管理器：{toolchain.describe() if toolchain else '既没有 pip 也没有 uv —— 无法从这里安装'}")
    for item in components:
        state = "就绪" if item["ready"] else ("包已装、本体未就绪" if item["package"] else "未安装")
        click.echo(f"  {item['name']:<14} {state:<20} {item['summary']}")
        if not item["ready"]:
            click.echo(f"    {item['install']}" + (f" && {item['browser_command']}" if item["browser_command"] else ""))


def _run_plan(op: str, extra: str, browser_kind: str, as_json: bool, dry_run: bool) -> None:
    """规划并执行经过白名单约束的组件操作。"""
    from ipclick.web.installer import execute, plan

    prepared, reason = plan(op, extra, browser_kind=browser_kind)
    if prepared is None:
        fail(reason, Exit.REJECTED, as_json=as_json, op=op, extra=extra)

    if dry_run:
        emit(
            {"ok": True, "dry_run": True, "op": op, "extra": extra, "command": list(prepared.command)},
            as_json=as_json,
            human=f"将执行：{prepared.shell_form}",
        )
        return

    lines: list[str] = []

    def sink(line: str) -> None:
        """收集安装输出，并仅在人类模式同步写入 stderr。"""
        lines.append(line)
        if not as_json:
            click.echo(line, err=True)

    if prepared.note:
        sink(f"（{prepared.note}）")
    returncode = execute(prepared.command, sink)
    ok = returncode == 0

    restart_required = ok and op in ("install", "uninstall")
    hint = (
        "正在跑的服务端进程还不认识它——重启 `ipclick run` 才生效"
        "（或改用 Web 端的「组件」页装，那边装完会自动刷新注册表）"
        if restart_required
        else ""
    )

    payload = {
        "ok": ok,
        "exit_code": int(Exit.OK if ok else Exit.FAILED),
        "op": op,
        "extra": extra,
        "title": prepared.title,
        "command": list(prepared.command),
        "returncode": returncode,
        "restart_required": restart_required,
        "hint": hint,
        "output": lines,
    }
    if as_json:
        emit(payload, as_json=True)
    else:
        click.echo(f"{prepared.title}：{'成功' if ok else f'失败（退出码 {returncode}）'}")
        if hint:
            click.echo(f"⚠️  {hint}")
    if not ok:
        raise SystemExit(int(Exit.FAILED))


@component.command("install")
@click.argument("extra")
@click.option("--dry-run", is_flag=True, default=False, help="只打印将要执行的命令，不真的装")
@json_option
def component_install(extra: str, dry_run: bool, as_json: bool) -> None:
    """安装指定可选 extra。"""
    _quiet_logs()
    _run_plan("install", extra, "chromium", as_json, dry_run)


@component.command("uninstall")
@click.argument("extra")
@click.option("--dry-run", is_flag=True, default=False, help="只打印将要执行的命令，不真的卸")
@json_option
def component_uninstall(extra: str, dry_run: bool, as_json: bool) -> None:
    """卸载指定可选 extra 的 Python 包。"""
    _quiet_logs()
    _run_plan("uninstall", extra, "chromium", as_json, dry_run)


@component.command("browser")
@click.argument("extra")
@click.option(
    "--kind",
    type=click.Choice(["chromium", "firefox", "webkit"]),
    default="chromium",
    show_default=True,
    help="playwright / patchright 要装哪个内核（camoufox 只有 Firefox）",
)
@click.option("--dry-run", is_flag=True, default=False, help="只打印将要执行的命令")
@json_option
def component_browser(extra: str, kind: str, dry_run: bool, as_json: bool) -> None:
    """为浏览器 extra 下载所选浏览器本体。"""
    _quiet_logs()
    _run_plan("browser", extra, kind, as_json, dry_run)


@click.group("config")
def config_group() -> None:
    """查看实际生效且已脱敏的配置。"""


_SECRET_HINTS = ("token", "secret", "password", "auth_key", "passwd", "credential")


def _redact(value: Any, key: str = "") -> Any:
    """递归隐藏键名疑似机密的配置值。"""
    lowered = key.lower()
    if any(hint in lowered for hint in _SECRET_HINTS):
        if isinstance(value, (list, tuple)):
            return [f"<已配置：{len(value)} 项>"] if value else []
        return "<已配置>" if str(value or "").strip() else ""
    if isinstance(value, dict):
        return {k: _redact(v, k) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact(v, key) for v in value]
    return value


@config_group.command("show")
@config_option
@click.option("--section", "-s", default="", help="只看某一节，如 SERVER、DOWNLOADER.retry")
@json_option
def config_show(config: Path | None, section: str, as_json: bool) -> None:
    """展示完整配置或指定配置节，并隐藏机密。"""
    _quiet_logs()
    config_data = _load(config, as_json)

    node_data: Any = {k: dict(v) if hasattr(v, "keys") else v for k, v in config_data.items()}
    if section:
        for part in section.split("."):
            if not isinstance(node_data, dict) or part not in node_data:
                fail(f"配置里没有 {section!r} 这一节", Exit.REJECTED, as_json=as_json)
            node_data = node_data[part]
        node_data = dict(node_data) if hasattr(node_data, "keys") else node_data

    redacted = _redact(node_data)
    if as_json:
        emit({"ok": True, "section": section or None, "config": redacted}, as_json=True)
    else:
        import json as json_lib

        click.echo(json_lib.dumps(redacted, ensure_ascii=False, indent=2, default=str))


@config_group.command("get")
@config_option
@click.argument("path")
@json_option
def config_get(config: Path | None, path: str, as_json: bool) -> None:
    """按点分路径读取单个生效配置值，并隐藏机密。"""
    _quiet_logs()
    config_data = _load(config, as_json)

    node_data: Any = config_data
    for part in path.split("."):
        if not hasattr(node_data, "get"):
            fail(f"{path!r} 走不通：{part!r} 的上一级不是一个配置节", Exit.REJECTED, as_json=as_json)
        if part not in node_data:
            fail(f"配置里没有 {path!r}", Exit.REJECTED, as_json=as_json)
        node_data = node_data[part]

    key = path.rsplit(".", 1)[-1]
    value = _redact(dict(node_data) if hasattr(node_data, "keys") else node_data, key)
    if as_json:
        emit({"ok": True, "path": path, "value": value}, as_json=True)
    else:
        import json as json_lib

        click.echo(value if isinstance(value, str) else json_lib.dumps(value, ensure_ascii=False, default=str))


__all__ = ["component", "config_group", "fetch", "node", "status", "trace"]
