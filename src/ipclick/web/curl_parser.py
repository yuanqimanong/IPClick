"""把一条 ``curl`` 命令解析成「试一试」表单。

价值全在一个动作上：浏览器 DevTools 里对着请求「复制为 cURL」，粘进来就能跑。
手动把 URL、方法、十几个请求头、一段 body 逐个拆进表单，既慢又容易漏——而漏掉
一个 header 往往就是"为什么我用 IPClick 抓不到、浏览器却可以"的答案。

**不追求覆盖 curl 的全部参数。** curl 有两百多个选项，绝大多数与这里无关
（连接复用、输出重定向、进度条…）。认得出常用的那十几个就够，认不出的一律
忽略并**报给用户**——静默丢掉一个 ``--data-urlencode`` 比不支持它更糟。

引号交给 :func:`shlex.split` 处理，不自己写状态机：DevTools 导出的命令里
单引号包着 JSON、JSON 里又有双引号是常态，手写解析必然出错。
"""

from __future__ import annotations

from dataclasses import dataclass, field
import shlex
from typing import final


#: 认得的、**带一个参数**的选项。值是它在表单里的去处。
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
    "--connect-timeout": "timeout",
}

#: 认得的、**不带参数**的开关。这些对 IPClick 来说要么是默认行为、要么无关，
#: 静静吃掉即可——但必须列出来，否则会被当成"不认识的参数"而误报。
_BOOL_FLAGS: frozenset[str] = frozenset(
    {
        "--compressed",  # IPClick 总是接受压缩响应
        "-s",
        "--silent",
        "-S",
        "--show-error",
        "-L",
        "--location",  # 默认就跟随重定向
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

#: 带参数、但**要提醒用户**的选项：它们会改变请求语义，而这里对应不上。
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

#: 不带参数、要提醒的开关
_UNSUPPORTED_BOOL_FLAGS: dict[str, str] = {
    "-k": "-k / --insecure 会跳过证书校验，本页不支持——请确认目标证书有效",
    "--insecure": "-k / --insecure 会跳过证书校验，本页不支持——请确认目标证书有效",
}

#: 有 body 但没写 -X 时 curl 用的方法
_IMPLIED_METHOD_WITH_BODY = "POST"

_KNOWN_METHODS = frozenset({"GET", "POST", "HEAD", "PUT", "PATCH", "DELETE", "OPTIONS"})


@final
@dataclass
class ParsedCurl:
    """解析结果。``notes`` 是要显示给用户看的"我没处理这些"。"""

    url: str = ""
    method: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    body: str = ""
    timeout: str = ""
    #: 认不出或没导入的东西。**必须**展示——静默丢弃会让人以为已经导入了。
    notes: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error and bool(self.url)

    def as_form(self) -> dict[str, str]:
        """转成「试一试」表单的字段。"""
        return {
            "url": self.url,
            "method": self.method or "GET",
            "headers": "\n".join(f"{k}: {v}" for k, v in self.headers.items()),
            "body": self.body,
            "timeout": self.timeout,
        }


def parse_curl(command: str) -> ParsedCurl:
    """解析一条 curl 命令。**绝不抛异常**——粘错东西是常态，要给可读的提示。"""
    text = (command or "").strip()
    if not text:
        return ParsedCurl(error="请先粘贴一条 curl 命令")

    # DevTools 导出的命令是多行的，行尾用 \ 续行；PowerShell 版本用 `。
    # 两种都先摊平成一行再交给 shlex。
    text = text.replace("\\\n", " ").replace("^\n", " ").replace("`\n", " ")

    try:
        tokens = shlex.split(text)
    except ValueError as e:
        # 引号没配平是最常见的粘贴事故（选中范围少了半行）
        return ParsedCurl(error=f"命令解析失败（引号没有配对？）：{e}")

    if not tokens:
        return ParsedCurl(error="请先粘贴一条 curl 命令")
    if tokens[0].lower() not in ("curl", "curl.exe"):
        return ParsedCurl(error=f"不像一条 curl 命令（开头是 {tokens[0]!r}）")

    return _consume(tokens[1:])


def _consume(tokens: list[str]) -> ParsedCurl:
    result = ParsedCurl()
    positional: list[str] = []
    index = 0

    while index < len(tokens):
        token = tokens[index]
        index += 1

        if not token.startswith("-") or token == "-":
            positional.append(token)
            continue

        # --header=value 与 --header value 都要认
        flag, sep, inline = token.partition("=")
        if not sep:
            flag, inline = token, ""

        if flag in _BOOL_FLAGS:
            continue
        if flag in _UNSUPPORTED_BOOL_FLAGS:
            _note(result, _UNSUPPORTED_BOOL_FLAGS[flag])
            continue

        target = _VALUE_FLAGS.get(flag)
        unsupported = _UNSUPPORTED_VALUE_FLAGS.get(flag)
        if target is None and unsupported is None:
            # 合并短选项（-sS）在 DevTools 的输出里很常见，逐个拆开看
            if _is_bundled_bools(flag):
                continue
            _note(result, f"未识别的参数 {flag}，已忽略")
            continue

        value = inline if sep else (tokens[index] if index < len(tokens) else "")
        if not sep:
            index += 1

        if unsupported is not None:
            _note(result, unsupported)
            # --data-urlencode 仍然有 body，尽量别丢
            if flag == "--data-urlencode":
                result.body = _join_body(result.body, value)
            continue

        _apply(result, target or "", value)

    if not result.url:
        result.url = next((p for p in positional if _looks_like_url(p)), "")
    if not result.url:
        return ParsedCurl(
            error="没找到网址。请确认粘贴的是完整命令（DevTools 里用「Copy as cURL」）",
            notes=result.notes,
        )
    if not result.url.startswith(("http://", "https://")):
        # curl 允许省略协议，IPClick 的准入策略要求写全
        result.url = f"https://{result.url}"
        _note(result, "命令里没写协议，已按 https:// 补全")

    if not result.method:
        result.method = _IMPLIED_METHOD_WITH_BODY if result.body else "GET"
    return result


def _apply(result: ParsedCurl, target: str, value: str) -> None:
    if target == "method":
        method = value.strip().upper()
        if method in _KNOWN_METHODS:
            result.method = method
        else:
            _note(result, f"不支持的方法 {value!r}，已按 GET 处理")
    elif target == "url":
        result.url = value.strip()
    elif target == "header":
        name, sep, header_value = value.partition(":")
        if sep and name.strip():
            result.headers[name.strip()] = header_value.strip()
        else:
            _note(result, f"请求头 {value!r} 不是 Name: value 格式，已忽略")
    elif target == "data":
        result.body = _join_body(result.body, value)
    elif target == "cookie":
        # -b 可以是 "a=1; b=2" 也可以是一个文件名。后者这里处理不了。
        if "=" in value:
            result.headers.setdefault("Cookie", value)
        else:
            _note(result, f'-b {value!r} 看起来是 cookie 文件，本页只支持 "名=值" 形式')
    elif target == "user-agent":
        result.headers.setdefault("User-Agent", value)
    elif target == "referer":
        result.headers.setdefault("Referer", value)
    elif target == "timeout":
        try:
            result.timeout = str(int(float(value)))
        except ValueError:
            _note(result, f"超时 {value!r} 不是数字，已忽略")


def _join_body(existing: str, addition: str) -> str:
    """curl 允许多个 -d，它们用 & 连起来（这是 form 编码的语义）。"""
    if not existing:
        return addition
    return f"{existing}&{addition}"


def _is_bundled_bools(flag: str) -> bool:
    """``-sS`` 这种把多个短开关粘在一起的写法。"""
    if len(flag) < 3 or flag.startswith("--"):
        return False
    return all(f"-{char}" in _BOOL_FLAGS for char in flag[1:])


def _looks_like_url(token: str) -> bool:
    if token.startswith(("http://", "https://")):
        return True
    # curl 允许省略协议：example.com/path。要求带点或斜杠，免得把
    # 某个漏配对的参数值当成网址。
    return "." in token and not token.startswith("-")


def _note(result: ParsedCurl, message: str) -> None:
    if message not in result.notes:
        result.notes.append(message)


__all__ = ["ParsedCurl", "parse_curl"]
