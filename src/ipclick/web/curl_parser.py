"""将常见的 curl 命令安全地转换为 Web 测试页表单数据。"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
import shlex
from typing import final
from urllib.parse import urlsplit, urlunsplit


# 提示条数上限。去重是对已有提示的线性扫描，而"未识别参数"这类提示是各不相同的：
# 实测 8000 个不同的未知参数（刚好在沙箱页 64 KiB 输入上限内）要 0.65 秒 CPU，
# 渲染出 390 KB 的 HTML。
CURL_MAX_NOTES = 40

# str.splitlines() 认作换行的全部字符。请求头会被渲染成 textarea 再按行切回 dict，
# 所以这些字符一个都不能留在里面——而 \x85 / \x1c / U+2028 / U+2029 在 textarea 里
# **只占一行**（实测 scrollHeight 未变），"发送前自己看一眼"挡不住。
_LINE_BREAKS = re.compile(r"[\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029]")

# 没有协议时不能把这些当成主机名。未识别的带值参数会把它的值留成位置参数，
# 而"含一个点"就当网址的话，cookies.txt / client.pem 就会顶替真正的目标。
_NOT_A_TLD = frozenset(
    [
        "txt",
        "pem",
        "crt",
        "cer",
        "der",
        "key",
        "json",
        "html",
        "htm",
        "bin",
        "cfg",
        "conf",
        "ini",
        "log",
        "xml",
        "csv",
        "yaml",
        "yml",
        "p12",
        "pfx",
        "jks",
        "sh",
        "py",
        "js",
        "css",
        "ico",
        "png",
        "jpg",
        "jpeg",
        "gif",
        "gz",
        "tgz",
        "zip",
        "bz2",
        "xz",
        "sql",
        "db",
        "sqlite",
        "md",
        "pdf",
        "doc",
        "docx",
        "xls",
        "xlsx",
    ]
)

_VALUE_FLAGS: dict[str, str] = {
    "-X": "method",
    "--request": "method",
    "-H": "header",
    "--header": "header",
    "-d": "data",
    "--data": "data",
    "--data-raw": "data",
    "--data-binary": "data",
    "--data-ascii": "data",
    "-b": "cookie",
    "--cookie": "cookie",
    "-A": "user-agent",
    "--user-agent": "user-agent",
    "-e": "referer",
    "--referer": "referer",
    "--url": "url",
    "-m": "timeout",
    "--max-time": "timeout",
    # 连接超时和总超时是两件事，共用一个字段的话谁写在后面谁赢：
    # `curl -m 60 --connect-timeout 5` 实测解析出 timeout=5，总超时被顶掉了。
    "--connect-timeout": "connect-timeout",
}

# 认识、但对本页无意义的带值参数。关键是**把它的值一起吃掉**：不吃的话那个值会变成
# 位置参数，再被当成网址。实测 `curl -c cookies.txt https://real.example.com/api`
# 解析出的 url 是 https://cookies.txt；`-sd 'user=alice@evil.example'` 更糟——目标
# 主机变成了攻击者选的 evil.example，而这串命令正是"照着这条复现一下"给过来的。
_IGNORED_VALUE_FLAGS: frozenset[str] = frozenset(
    {
        "-c",
        "--cookie-jar",
        "-D",
        "--dump-header",
        "-w",
        "--write-out",
        "--cacert",
        "--capath",
        "--cert",
        "-E",
        "--key",
        "--cert-type",
        "--key-type",
        "--pass",
        "--ciphers",
        "--tlsv1.0",
        "--tls-max",
        "--resolve",
        "--interface",
        "--dns-servers",
        "--local-port",
        "--unix-socket",
        "--retry",
        "--retry-delay",
        "--retry-max-time",
        "--limit-rate",
        "-Y",
        "--speed-limit",
        "-y",
        "--speed-time",
        "--keepalive-time",
        "--proxy-user",
        "--noproxy",
        "--oauth2-bearer",
        "-K",
        "--config",
        "--trace",
        "--trace-ascii",
        "--stderr",
        "-C",
        "--continue-at",
        "--range",
        "-r",
        "-T",
        "--upload-file",
    }
)

# 决定 HTTP 方法的布尔参数。原来它们都落进"未识别"，于是 `-I` 静默变成 GET。
_METHOD_BOOL_FLAGS: dict[str, str] = {"-I": "HEAD", "--head": "HEAD"}

_BOOL_FLAGS: frozenset[str] = frozenset(
    {
        "--compressed",
        "-s",
        "--silent",
        "-S",
        "--show-error",
        "-L",
        "--location",
        "-i",
        "--include",
        "-v",
        "--verbose",
        "-g",
        "--globoff",
        "--http1.1",
        "--http2",
        "--no-buffer",
        "-#",
        "--progress-bar",
        "-f",
        "--fail",
    }
)

_UNSUPPORTED_VALUE_FLAGS: dict[str, str] = {
    "-F": "文件上传（-F / --form）没有对应的表单项，请改用请求体",
    "--form": "文件上传（-F / --form）没有对应的表单项，请改用请求体",
    "--data-urlencode": "--data-urlencode 需要先做 URL 编码，已按原文放进请求体，请自行确认",
    "-u": "-u / --user 是 HTTP Basic 认证，请自行换算成 Authorization 请求头",
    "--user": "-u / --user 是 HTTP Basic 认证，请自行换算成 Authorization 请求头",
    "-x": "-x / --proxy 指定的代理未导入——IPClick 的代理走服务端配置",
    "--proxy": "-x / --proxy 指定的代理未导入——IPClick 的代理走服务端配置",
    "-o": "-o / --output 指定的输出文件与本页无关，已忽略",
    "--output": "-o / --output 指定的输出文件与本页无关，已忽略",
}

_UNSUPPORTED_BOOL_FLAGS: dict[str, str] = {
    "-k": "-k / --insecure 会跳过证书校验，本页不支持——请确认目标证书有效",
    "--insecure": "-k / --insecure 会跳过证书校验，本页不支持——请确认目标证书有效",
}

# 短参数可以把值直接贴在后面（-XPOST / -m30 / -H'X: y'），必须认。
_SHORT_VALUE_FLAGS: frozenset[str] = frozenset(f for f in _VALUE_FLAGS if len(f) == 2 and not f.startswith("--"))

_IMPLIED_METHOD_WITH_BODY = "POST"

_KNOWN_METHODS = frozenset({"GET", "POST", "HEAD", "PUT", "PATCH", "DELETE", "OPTIONS"})


@final
@dataclass
class ParsedCurl:
    """curl 导入结果；不支持的选项以提示而非静默丢弃。"""

    url: str = ""
    method: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    body: str = ""
    timeout: str = ""
    notes: list[str] = field(default_factory=list)
    error: str = ""
    # --max-time 是否显式给过；用来让它稳定地优先于 --connect-timeout，不看先后顺序。
    explicit_total_timeout: bool = False
    # -X 给了个不认识的方法；提示等最终方法定下来再发。
    rejected_method: str = ""

    @property
    def ok(self) -> bool:
        """表示命令已成功解析且包含目标 URL。"""
        return not self.error and bool(self.url)

    def as_form(self) -> dict[str, str]:
        """转换为测试页可直接回填的字段字典。"""
        return {
            "url": self.url,
            "method": self.method or "GET",
            "headers": "\n".join(f"{k}: {v}" for k, v in self.headers.items()),
            "body": self.body,
            "timeout": self.timeout,
        }


def parse_curl(command: str) -> ParsedCurl:
    """解析单条 curl 命令，不执行命令或读取命令引用的本地文件。"""
    text = (command or "").strip()
    if not text:
        return ParsedCurl(error="请先粘贴一条 curl 命令")

    text = _join_continuations(_decode_ansi_c(text))

    try:
        tokens = shlex.split(text)
    except ValueError as e:
        return ParsedCurl(error=f"命令解析失败（引号没有配对？）：{e}")

    if not tokens:
        return ParsedCurl(error="请先粘贴一条 curl 命令")
    if tokens[0].lower() not in ("curl", "curl.exe"):
        return ParsedCurl(error=f"不像一条 curl 命令（开头是 {tokens[0]!r}）")

    return _consume(tokens[1:])


def _split_flag(token: str) -> tuple[str, str, bool]:
    """把一个参数拆成（参数名，贴在后面的值，值是否给出）。

    三种写法都要认：``--flag=value``、``-Xvalue``（短参数贴值）、以及只有参数名。

    短参数贴值必须先判：``-H'Cookie: sid=abc'`` 里的 ``=`` 属于值，先按 ``=`` 拆会得到
    一个并不存在的参数名 ``-HCookie: sid``，请求头随之整条丢掉。同理 ``-d'a=1&b=2'``
    会连请求体带方法一起丢。``=`` 拆分只对长参数成立。
    """
    if len(token) > 2 and not token.startswith("--") and token[:2] in _SHORT_VALUE_FLAGS:
        return token[:2], token[2:], True
    if token.startswith("--"):
        flag, sep, inline = token.partition("=")
        if sep:
            return flag, inline, True
    return token, "", False


# bash 的 $'...' 写法。Chrome DevTools 的「Copy as cURL」在 Linux/macOS 上只要值里
# 有需要转义的字符就会输出它，而这正是本功能最主要的输入来源。
_ANSI_C_QUOTED = re.compile(r"\$'((?:\\.|[^'\\])*)'")


def _decode_ansi_c(text: str) -> str:
    """把 ``$'...'`` 换成等价的普通单引号串。

    shlex 不认这种写法，``$`` 会原样留在 token 里——实测 ``-H $'Accept: text/html'``
    解析出的请求头名字是 ``$Accept``。
    """

    def replace(match: re.Match[str]) -> str:
        raw = match.group(1)
        # 先取 UTF-8 字节再走 unicode_escape：DevTools 输出的 \xNN 字节转义与值里原样
        # 的非 ASCII 字符走同一条路都能还原。此前是按 latin-1 取字节，中文这类字符不在
        # latin-1 里，被 "replace" 压成了 "?"——$'{"name":"张三"}' 静默变成 {"name":"??"}。
        try:
            unescaped = raw.encode("utf-8").decode("unicode_escape")
        except UnicodeDecodeError:
            return shlex.quote(raw)
        try:
            decoded = unescaped.encode("latin-1").decode("utf-8")
        except (UnicodeDecodeError, UnicodeEncodeError):
            # \uHHHH 这类转义已经直接产出目标字符，不必再过一次字节还原；
            # 半截 UTF-8 序列也落在这里，保留 unicode_escape 的结果。
            decoded = unescaped
        return shlex.quote(decoded)

    return _ANSI_C_QUOTED.sub(replace, text)


def _join_continuations(text: str) -> str:
    r"""把行尾续行符换成空格，但**只在引号外面**。

    原来是在 tokenize 之前全文 replace，于是引号里的那一个也被换掉：
    ``-d 'line1\<换行>line2'`` 的请求体静默变成 ``line1 line2``。
    """
    out: list[str] = []
    quote = ""
    index = 0
    while index < len(text):
        char = text[index]
        if quote:
            out.append(char)
            if char == quote:
                quote = ""
            index += 1
            continue
        if char in "\"'":
            quote = char
            out.append(char)
            index += 1
            continue
        if char in "\\^`" and text[index + 1 : index + 2] == "\n":
            out.append(" ")
            index += 2
            continue
        out.append(char)
        index += 1
    return "".join(out)


def _consume(tokens: list[str]) -> ParsedCurl:
    result = ParsedCurl()
    positional: list[str] = []
    index = 0
    get_with_body = False

    while index < len(tokens):
        token = tokens[index]
        index += 1

        if not token.startswith("-") or token == "-":
            positional.append(token)
            continue

        flag, inline, sep = _split_flag(token)

        if flag in ("-G", "--get"):
            get_with_body = True
            continue
        if flag in _METHOD_BOOL_FLAGS:
            result.method = _METHOD_BOOL_FLAGS[flag]
            continue
        if flag in _BOOL_FLAGS:
            continue
        if flag in _UNSUPPORTED_BOOL_FLAGS:
            _note(result, _UNSUPPORTED_BOOL_FLAGS[flag])
            continue

        target = _VALUE_FLAGS.get(flag)
        unsupported = _UNSUPPORTED_VALUE_FLAGS.get(flag)
        ignored = flag in _IGNORED_VALUE_FLAGS
        if target is None and unsupported is None and not ignored:
            if _is_bundled_bools(flag):
                continue
            _note(result, f"未识别的参数 {flag}，已忽略（它的值如果紧跟在后面，也不会被当成网址）")
            continue

        value = inline if sep else (tokens[index] if index < len(tokens) else "")
        if not sep:
            index += 1

        if ignored:
            _note(result, f"{flag} 与本页无关，已忽略（含它的取值）")
            continue
        if unsupported is not None:
            _note(result, unsupported)
            if flag == "--data-urlencode":
                result.body = _join_body(result.body, value)
            continue

        _apply(result, target or "", value)

    candidates = [p for p in positional if _looks_like_url(p)]
    if not result.url:
        result.url = candidates[0] if candidates else ""
    if len(candidates) > 1:
        _note(result, f"命令里有多个网址，只导入了第一个（其余 {len(candidates) - 1} 个已忽略）")
    if not result.url:
        return ParsedCurl(
            error="没找到网址。请确认粘贴的是完整命令（DevTools 里用「Copy as cURL」）",
            notes=result.notes,
        )
    if not result.url.startswith(("http://", "https://")):
        result.url = f"https://{result.url}"
        _note(result, "命令里没写协议，已按 https:// 补全")

    if get_with_body:
        # -G 是"把 -d 的内容拼到查询串上、用 GET 发"。原来 -G 未识别，于是解析成
        # POST + 请求体：操作员以为只是读一下，实际对目标发了个带请求体的 POST。
        if result.body:
            result.url = _append_query(result.url, result.body)
            result.body = ""
        result.method = "HEAD" if result.method == "HEAD" else "GET"

    if not result.method:
        result.method = _IMPLIED_METHOD_WITH_BODY if result.body else "GET"
    if result.rejected_method:
        _note(result, f"不支持的方法 {result.rejected_method!r}，已按 {result.method} 处理")
    return result


def _append_query(url: str, extra: str) -> str:
    parts = urlsplit(url)
    query = f"{parts.query}&{extra}" if parts.query else extra
    return urlunsplit(parts._replace(query=query))


def _apply(result: ParsedCurl, target: str, value: str) -> None:
    if target == "method":
        method = value.strip().upper()
        if method in _KNOWN_METHODS:
            result.method = method
        else:
            # 提示留到最后再发：-X 可能排在 -d 之前，那时 body 还是空的，此刻算出来的
            # "已按 GET 处理"会和最终的 POST 对不上。
            result.rejected_method = value
    elif target == "url":
        result.url = value.strip()
    elif target == "header":
        name, sep, header_value = value.partition(":")
        if not sep or not name.strip():
            _note(result, f"请求头 {value!r} 不是 Name: value 格式，已忽略")
        elif _LINE_BREAKS.search(name) or _LINE_BREAKS.search(header_value):
            _note(result, f"请求头 {name.strip()!r} 里含换行字符，已忽略——那会被拆成两条请求头发出去")
        else:
            result.headers[name.strip()] = header_value.strip()
    elif target == "data":
        if value.startswith("@"):
            # 本页不读本地文件（模块文档就是这么写的）。问题不是不读，而是原来把字面量
            # "@payload.json" 当成请求体交出去，一句提示都没有。
            _note(result, f"-d {value!r} 要从本地文件取请求体，本页不读本地文件，已忽略这一段")
        else:
            result.body = _join_body(result.body, value)
    elif target == "cookie":
        if "=" in value:
            _set_header(result, "Cookie", value)
        else:
            _note(result, f'-b {value!r} 看起来是 cookie 文件，本页只支持 "名=值" 形式')
    elif target == "user-agent":
        _set_header(result, "User-Agent", value)
    elif target == "referer":
        _set_header(result, "Referer", value)
    elif target in ("timeout", "connect-timeout"):
        seconds = _seconds(result, value)
        if seconds is None:
            return
        # --max-time（总超时）优先于 --connect-timeout，且不受先后顺序影响。
        if target == "timeout" or not result.timeout or result.explicit_total_timeout is False:
            if target == "timeout":
                result.explicit_total_timeout = True
                result.timeout = seconds
            elif not result.explicit_total_timeout:
                result.timeout = seconds


def _seconds(result: ParsedCurl, value: str) -> str | None:
    """把超时值解析成整数秒；解析不出来只提示，绝不抛异常。

    ``int(float("inf"))`` 抛的是 OverflowError，原来只接了 ValueError——而解析器抛
    异常的后果不是"报个错"：沙箱页那两条调用路径都没有 try/except，异常一路到
    BaseHTTPRequestHandler，连接被直接关掉，客户端一个字节的响应都收不到。
    """
    try:
        return str(int(float(value)))
    except (TypeError, ValueError, OverflowError):
        _note(result, f"超时 {value!r} 不是有限数字，已忽略")
        return None


def _set_header(result: ParsedCurl, name: str, value: str) -> None:
    """按大小写不敏感的方式补一个请求头；已经有同名的就不动。

    原来用 setdefault，而 -H 是原样保留大小写的：``-H 'cookie: a=1' -b 'b=2'``
    产出 cookie 和 Cookie 两条冲突的请求头，而 curl 只会发一条。
    """
    if _LINE_BREAKS.search(value):
        _note(result, f"{name} 的取值里含换行字符，已忽略")
        return
    lowered = name.lower()
    if any(existing.lower() == lowered for existing in result.headers):
        return
    result.headers[name] = value


def _join_body(existing: str, addition: str) -> str:
    if not existing:
        return addition
    return f"{existing}&{addition}"


def _is_bundled_bools(flag: str) -> bool:
    if len(flag) < 3 or flag.startswith("--"):
        return False
    return all(f"-{char}" in _BOOL_FLAGS for char in flag[1:])


def _looks_like_url(token: str) -> bool:
    """判断一个位置参数是否像网址。

    原来只要"含一个点"就算，于是未识别参数留下的值会顶替真正的目标：cookies.txt、
    client.pem、/etc/ca.pem、example.com:443:127.0.0.1、user=alice@evil.example
    实测都被当成了网址。这里是兜底——主要防线是把带值参数的值一起吃掉。
    """
    if token.startswith(("http://", "https://")):
        return True
    if not token or token.startswith(("-", "/", "@", ".")) or "." not in token:
        return False
    if any(ch.isspace() for ch in token):
        return False
    host = re.split(r"[/?#]", token, maxsplit=1)[0]
    # "=" 只有出现在 host 段里才说明这不是网址（user=alice@evil.example）。此前对整个
    # token 判，于是 example.com/api?q=1 这种 curl 能接受的写法被判成"没找到网址"。
    if "=" in host:
        return False
    if host.count(":") > 1:  # --resolve 的 host:port:addr 形式
        return False
    last_label = host.split(":", 1)[0].rsplit(".", 1)[-1].lower()
    return bool(last_label) and last_label not in _NOT_A_TLD


def _note(result: ParsedCurl, message: str) -> None:
    if message in result.notes:
        return
    if len(result.notes) >= CURL_MAX_NOTES - 1:
        overflow = f"提示过多，只显示前 {CURL_MAX_NOTES} 条"
        if overflow not in result.notes:
            result.notes.append(overflow)
        return
    result.notes.append(message)


__all__ = ["CURL_MAX_NOTES", "ParsedCurl", "parse_curl"]
