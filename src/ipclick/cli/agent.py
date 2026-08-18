"""给程序（尤其是 AI 代理）调用的命令面。

0.4 之前的 CLI 只服务于"人在终端里部署这套东西"：``init`` / ``run`` / ``health``
/ ``config-info``。想让一个 AI 用上 IPClick，只有两条路——要么让它写 Python 调
SDK，要么让它去点那个网页。前者要求宿主环境能跑任意 Python（很多代理不给），
后者根本不是给程序用的。

0.5 补上中间这一层：**每一件能在 Web 端做的观测与诊断，都有一条对应的命令**，
输出是结构化的，退出码是分类的。形态上是"资源 + 动词"（``trace list`` /
``node probe`` / ``component install``），和多数面向机器的 CLI 一致——这种形状对
模型友好，因为它能从一个子命令的用法推断出另一个。

三条贯穿全局的约定，见 :mod:`ipclick.cli.output`：

* ``--json`` 时 stdout 上只有一个 JSON 文档，**失败也是**；
* 退出码分类（0/1/3/4/5），指向"该往哪儿查"；
* 响应体默认截断，别把调用方的上下文窗口撑爆。

**这一层不是新的能力，是新的入口。** ``fetch`` 走的是和 SDK 完全相同的
:class:`~ipclick.sdk.Downloader`，``component install`` 和 Web 端共用
:func:`ipclick.web.installer.plan` 那份白名单。任何"只在 CLI 上成立"的分支都是
bug——那意味着有一条没被另外两个入口测到的代码路径。
"""

from __future__ import annotations

import base64
from pathlib import Path
import sys
import time
from typing import Any

import click

from ipclick import __version__
from ipclick.cli.output import DEFAULT_BODY_LIMIT, Exit, classify, emit, fail, json_option, note
from ipclick.config_loader import load_config, placeholders
from ipclick.ports import DEFAULT_GRPC_PORT, DEFAULT_WEB_PORT
from ipclick.utils.config_util import Settings


# --------------------------------------------------------------------------- #
# 公共选项
# --------------------------------------------------------------------------- #


def config_option(func: Any) -> Any:
    return click.option(
        "--config",
        "-c",
        type=click.Path(path_type=Path),
        default=None,
        help="配置文件路径（默认找当前目录的 ipclick.toml）",
    )(func)


def server_options(func: Any) -> Any:
    """连服务端要用的三项。顺序反着写，因为装饰器是自下而上应用的。"""
    func = click.option("--token", default=None, help="gRPC 鉴权令牌（覆盖 IPCLICK_AUTH_TOKEN 与配置文件）")(func)
    func = click.option("--port", "-p", type=int, default=None, help="服务端端口（默认取配置）")(func)
    func = click.option("--host", default=None, help="服务端地址（默认取配置，[::]/0.0.0.0 会当成 127.0.0.1）")(func)
    return config_option(func)


def _load(config: Path | None, as_json: bool = False) -> Settings:
    """读配置。读不出来直接退出——后面每一条命令都依赖它。

    显式给了 ``-c`` 时会先自己校验一遍。:func:`~ipclick.config_loader.load_config`
    对读不了 / 解析不了的文件是**跳过并打日志**（服务端的取舍：一个坏掉的可选配置
    不该让进程起不来）。但对这一组命令那是最坏的行为——调用方以为在用自己指定的
    配置，实际拿到的是内置默认值，然后对着一个"端口怎么不对"排查半天。显式指定
    的文件必须要么生效、要么报错。
    """
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
    """把日志压到 ERROR。

    这些命令的 stdout 是结果、stderr 是提示，中间夹一串 INFO 只会让人（和解析
    stderr 的脚本）以为出事了。真出错时 ERROR 仍然会打出来。
    """
    from ipclick.utils.log_util import LogUtil

    LogUtil.init(level="ERROR")


def _server_port(config: Settings, port: int | None) -> int:
    if port:
        return port
    try:
        return int(dict(config.get("SERVER", {})).get("port", DEFAULT_GRPC_PORT))
    except (TypeError, ValueError):
        return DEFAULT_GRPC_PORT


# --------------------------------------------------------------------------- #
# fetch
# --------------------------------------------------------------------------- #


def _parse_pairs(values: tuple[str, ...], separator: str, what: str) -> dict[str, str]:
    """把 ``-H 'Name: value'`` / ``--param k=v`` 这类重复选项解析成字典。

    分隔符只认**第一个**：``-H 'Referer: https://a/b'`` 里的第二个冒号属于值。
    """
    out: dict[str, str] = {}
    for raw in values:
        key, sep, value = raw.partition(separator)
        if not sep or not key.strip():
            raise click.UsageError(f"{what} 应写成 {'名称' + separator + '值'!r} 的形式，收到 {raw!r}")
        out[key.strip()] = value.strip()
    return out


def _read_body(value: str) -> bytes:
    """``-d`` 的取值。``@路径`` 从文件读（``@-`` 读 stdin），其余按字面量。

    和 curl 同一套写法。AI 生成的命令里请求体经常很大，逼它塞进一个 shell 参数
    既会撞上 ARG_MAX，也会让引号转义出错。
    """
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
    """响应体在 JSON 里怎么表示。

    能解码成 UTF-8 就给文本；否则给 base64——图片、gzip、非 UTF-8 的表单都属于
    后者，硬塞进 JSON 字符串只会得到一串替换字符，调用方还原不出原始字节。
    ``body_truncated`` 必须显式给出：静默截断会让调用方把半截 HTML 当成完整页面。

    文本和二进制的截断规则不一样，因为"半截"的价值不一样：**半截 HTML 还能看出
    是什么页面，半截 base64 解不出任何东西**。所以文本按上限切一刀照给，二进制则
    要么整份给、要么不给——超限时返回空串并说明去哪儿取，而不是发一段注定解码
    失败的字符。
    """
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
    type=int,
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
    """通过 IPClick 服务端发一次请求。

    走的是和 SDK 完全相同的路径——SSRF 准入、按 host 限流、开了转发时的分发都在。
    这不是"CLI 版的 curl"，而是"命令行形态的 IPClick 客户端"。

    \b
    退出码：0 成功；1 拿到响应但状态码 >= 400（或 --ignore-status 时不判）；
            3 连不上服务端；4 鉴权失败；5 参数被拒绝。
    """
    import json as json_lib

    from ipclick.dto.models import HttpMethod, IPClickAdapter
    from ipclick.sdk import Downloader

    _quiet_logs()
    # 配置读一遍只为"写错了要早点报"：Downloader 自己也会读，但它在构造函数里
    # 出错时抛的是原始异常，落到用户眼里是个 traceback。
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
            # 退出码仍是 2：这个名字是**本地**校验掉的，压根没联系服务端，
            # 属于"命令行参数写错了"。（5 留给"适配器存在但没装"，那是服务端
            # 回的 FAILED_PRECONDITION。）
            #
            # 但不能用 click.UsageError——click 会直接把错误打到 stderr 并退出，
            # stdout 上一个 JSON 都没有，而 --json 承诺的是"成功失败都有且只有
            # 一个 JSON 文档"。调用方（尤其是 AI）拿到空 stdout 只能靠猜。
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
    downloader = Downloader(
        config_path=str(config) if config else None,
        host=host,
        port=port,
        token=token,
    )
    try:
        response = downloader.request(
            url=url,
            method=http_method,
            adapter=resolved_adapter,
            headers=_parse_pairs(headers, ":", "请求头") or None,
            cookies=_parse_pairs(cookies, "=", "Cookie") or None,
            params=_parse_pairs(params, "=", "查询参数") or None,
            data=payload,
            json=parsed_json,
            # proxy=True 的语义是"用配置文件里的 [PROXY]"。命令行上没法传布尔，
            # 所以约定一个字面量 config —— 比再加一个 --use-config-proxy 标志少
            # 一个概念，而 "config" 不可能是一个真实的代理 URL。
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
        downloader.close()
        fail(f"{type(e).__name__}: {e}", classify(e), as_json=as_json, url=url)
    finally:
        downloader.close()

    if output is not None:
        try:
            _ = output.write_bytes(response.content)
        except OSError as e:
            fail(f"写文件失败：{e}", Exit.FAILED, as_json=as_json)

    # status_code == -1 有两种完全不同的成因，退出码必须分开：
    #   * 没连上 IPClick 服务端 —— 请求根本没发出去，该查进程和端口（3）；
    #   * 服务端连不上**目标站点** —— IPClick 一切正常，该查那个 URL（1）。
    # 区分靠 trace.node_id：只有真正处理过这个请求的节点才会填它。
    reached_server = response.status_code >= 0 or bool(response.trace.node_id) or bool(response.request_uuid)
    # 抓取本身算不算失败：没拿到响应、带了错误，或状态码 >= 400（除非说了不判）
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
    # 已经用 -o 写了完整文件时不再限制 JSON 里那份——两边都截断等于哪儿都拿不到全文
    result.update(_body_payload(response.content, 0 if output is not None else max(0, max_body)))

    if as_json:
        emit(result, as_json=True)
    else:
        # 元信息走 stderr、响应体走 stdout —— 于是 `ipclick fetch URL > page.html`
        # 拿到的是干净的页面，而屏幕上仍然看得见状态码和链路。
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


# --------------------------------------------------------------------------- #
# status
# --------------------------------------------------------------------------- #


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
    """服务端在不在、这台机器能干什么。

    比 health 多的是"能干什么"：装了哪些适配器、浏览器本体就绪没、链路落盘
    开没开、集群里有几个节点。一个 AI 在发第一个请求之前该先问这一句——否则它会
    指定一个本机根本没装的适配器，然后对着 FAILED_PRECONDITION 猜半天。
    """
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
    target = f"{host or '127.0.0.1'}:{resolved_port}"

    healthy, health_detail = check_health(target, timeout=timeout)

    security = dict(config_data.get("SECURITY", {}))
    browser = BrowserSettings.from_config(dict(config_data.get("BROWSER", {})))
    trace_settings = TraceSettings.from_config(
        placeholders.resolve_for("TRACE", dict(config_data.get("TRACE", {})), resolved_port)
    )
    try:
        mode = resolve_mode(config_data)
    except Exception as e:
        mode = f"配置错误: {e}"

    web_port = int(dict(config_data.get("WEB", {})).get("port", DEFAULT_WEB_PORT))
    components = components_snapshot(browser)
    nodes = _node_entries(config_data)
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
            # 两个端口分开报。调用方（含 AI）拿到 target 只知道 gRPC 那一个，
            # 想告诉人"管理界面在哪"时无从可查，于是只能去猜默认值——而这台机器
            # 未必用的是默认值。
            "grpc_port": resolved_port,
            "web_port": web_port,
            # 名字里带 _in_config 是必须的：这条命令是**另一个进程**，只看得到
            # 文件。`ipclick run -w` 用命令行打开 Web 端时并不改文件，那时文件里
            # 写着 false 而 Web 端正开着。叫 web_enabled 会让人（和 AI）以为这是
            # 运行状态，进而得出"Web 端没开"的错误结论。
            "web_enabled_in_config": bool(dict(config_data.get("WEB", {})).get("enabled", False)),
            # 这一条才是运行状态：直接连一下那个端口。文件怎么写不重要，
            # 端口上有没有人听才是人真正想知道的。
            "web_reachable": _port_open(host or "127.0.0.1", web_port),
        },
        "security": {
            "auth_token_configured": bool(load_tokens(security)),
            "tls": describe(TLSSettings.from_config(security)),
            "block_private_networks": bool(security.get("block_private_networks", False)),
            "block_metadata_endpoints": bool(security.get("block_metadata_endpoints", True)),
        },
        "adapters": {
            "ready": ["curl_cffi", *[c["name"] for c in components if c["ready"]]],
            "components": components,
            "browser_enabled": browser.enabled,
            "browser_engine": browser.engine,
            # 探的是**本机磁盘**，不是服务端进程此刻的注册表。两者只在一种情况下
            # 不一致：服务端启动之后才从命令行装的组件——那时这里说"就绪"，而
            # fetch 会收到"需要额外依赖"。说清楚比让人去猜便宜得多。
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


# --------------------------------------------------------------------------- #
# trace
# --------------------------------------------------------------------------- #


@click.group()
def trace() -> None:
    """查链路记录（只读 [TRACE].sqlite_path 那个库）。

    只能查落盘的那部分。内存环形缓冲活在服务端进程里，别的进程够不到——
    要看那一份请开 [TRACE].sqlite_enabled，或用 Web 端的「请求流」。
    """


def _reader(config_data: Settings, port: int | None, as_json: bool) -> Any:
    """按配置打开链路库。没开落盘 / 文件不在时直接退出并说清原因。"""
    from ipclick.trace import TraceReader, TraceSettings

    resolved_port = _server_port(config_data, port)
    settings = TraceSettings.from_config(
        placeholders.resolve_for("TRACE", dict(config_data.get("TRACE", {})), resolved_port)
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
    """列出最近的请求记录。"""
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
    """成功率、耗时、按天趋势、目标站点排行。"""
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


# --------------------------------------------------------------------------- #
# node
# --------------------------------------------------------------------------- #


@click.group()
def node() -> None:
    """集群节点：看列表、探连通性与鉴权。"""


def _port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    """那个端口上有没有人在听。

    只做 TCP 连接，不发 HTTP：这里要回答的是"Web 端起没起"，而不是"它健不健康"。
    连上就断，1 秒超时——status 是个要秒回的命令，不该为一个附带信息卡住。
    """
    import socket

    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _node_entries(config_data: Settings) -> list[dict[str, Any]]:
    """配置里声明的节点。token 只报有没有，绝不回显。"""
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
    """探一个节点。绝不抛——这是诊断入口，失败也要变成可读的结论。"""
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
        secret=cluster_secret(dict(config_data.get("CLUSTER", {}))),
        tls=TLSSettings.from_config(dict(config_data.get("SECURITY", {}))),
        timeout=timeout,
    )
    return result.snapshot()


@node.command("list")
@config_option
@json_option
def node_list(config: Path | None, as_json: bool) -> None:
    """列出 [CLUSTER].nodes 里声明的节点。"""
    _quiet_logs()
    config_data = _load(config, as_json)
    nodes = _node_entries(config_data)
    cluster = dict(config_data.get("CLUSTER", {}))

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
    """探测节点：连得上吗、集群内部鉴权配对吗。

    不给 NODE_ID 就探配置里的全部。任一节点不通时退出码为 1。
    """
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


# --------------------------------------------------------------------------- #
# component
# --------------------------------------------------------------------------- #


@click.group()
def component() -> None:
    """可选组件（五个 extras）：看状态、装、卸、下浏览器本体。

    和 Web 端的「组件」页共用同一份白名单与命令规划——包名永远来自常量表，
    绝不拼接输入（见 ipclick.web.installer.plan）。
    """


@component.command("list")
@config_option
@json_option
def component_list(config: Path | None, as_json: bool) -> None:
    """列出五个可选组件的安装状态。"""
    from ipclick.adapters.browser_settings import BrowserSettings
    from ipclick.components import snapshot as components_snapshot
    from ipclick.web.installer import detect_toolchain

    _quiet_logs()
    config_data = _load(config, as_json)
    browser = BrowserSettings.from_config(dict(config_data.get("BROWSER", {})))
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
    """装 / 卸的公共执行路径。"""
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
        lines.append(line)
        if not as_json:
            # 装包可能要几分钟（camoufox 本体约 1 GB）。不实时回显的话，调用方
            # 只能对着一个没有任何输出的进程等——那和卡死在观感上没有区别。
            click.echo(line, err=True)

    if prepared.note:
        sink(f"（{prepared.note}）")
    returncode = execute(prepared.command, sink)
    ok = returncode == 0

    # 装完要重启服务端，而且这一条必须说出来。
    #
    # Web 端装完会自己刷新适配器注册表（装的就是它那个进程），所以那边不需要重启。
    # 从 CLI 装是**另一个进程**——磁盘上有了，正在跑的服务端却仍然按启动时那份
    # 注册表工作。症状极具迷惑性：`ipclick status` 说 niquests 就绪（它探的是磁盘），
    # 而 `ipclick fetch -a niquests` 收到"需要额外依赖"。不明写出来，调用方会去
    # 反复重装那个已经装好的包。
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
    """装一个组件的 Python 包（EXTRA：niquests / camoufox / patchright / playwright / drissionpage）。"""
    _quiet_logs()
    _run_plan("install", extra, "chromium", as_json, dry_run)


@component.command("uninstall")
@click.argument("extra")
@click.option("--dry-run", is_flag=True, default=False, help="只打印将要执行的命令，不真的卸")
@json_option
def component_uninstall(extra: str, dry_run: bool, as_json: bool) -> None:
    """卸一个组件的 Python 包。

    不动浏览器本体——那可能是 1 GB，从命令行递归删一个 GB 级目录是不可逆操作。
    用 `component list` 看它在哪、占多大，自己决定要不要删。
    """
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
    """下载浏览器本体（camoufox 约 1 GB，慢网络下可能十几分钟）。"""
    _quiet_logs()
    _run_plan("browser", extra, kind, as_json, dry_run)


# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #


@click.group("config")
def config_group() -> None:
    """读生效配置。机密一律脱敏，只报"有没有"。

    和 config-info 的分工：那一条是给人看的一屏摘要，这里是给程序读的原始值。
    """


#: 脱敏的键名（小写子串匹配）。宁可多脱一个不该脱的，也不要漏一个真机密——
#: 这条命令的输出很可能被原样贴进日志、issue 或者一个模型的上下文里。
_SECRET_HINTS = ("token", "secret", "password", "auth_key", "passwd", "credential")


def _redact(value: Any, key: str = "") -> Any:
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
    """输出生效配置（机密已脱敏）。"""
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
    """取一项配置的值，如 SERVER.port、DOWNLOADER.retry.max_attempts。"""
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
