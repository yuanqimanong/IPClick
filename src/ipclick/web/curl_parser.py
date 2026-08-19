from __future__ import annotations

from dataclasses import dataclass, field
import shlex
from typing import final


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

_IMPLIED_METHOD_WITH_BODY = "POST"

_KNOWN_METHODS = frozenset({"GET", "POST", "HEAD", "PUT", "PATCH", "DELETE", "OPTIONS"})


@final
@dataclass
class ParsedCurl:
    url: str = ""
    method: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    body: str = ""
    timeout: str = ""
    notes: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error and bool(self.url)

    def as_form(self) -> dict[str, str]:
        return {
            "url": self.url,
            "method": self.method or "GET",
            "headers": "\n".join(f"{k}: {v}" for k, v in self.headers.items()),
            "body": self.body,
            "timeout": self.timeout,
        }


def parse_curl(command: str) -> ParsedCurl:
    text = (command or "").strip()
    if not text:
        return ParsedCurl(error="请先粘贴一条 curl 命令")

    text = text.replace("\\\n", " ").replace("^\n", " ").replace("`\n", " ")

    try:
        tokens = shlex.split(text)
    except ValueError as e:
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
            if _is_bundled_bools(flag):
                continue
            _note(result, f"未识别的参数 {flag}，已忽略")
            continue

        value = inline if sep else (tokens[index] if index < len(tokens) else "")
        if not sep:
            index += 1

        if unsupported is not None:
            _note(result, unsupported)
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
    if not existing:
        return addition
    return f"{existing}&{addition}"


def _is_bundled_bools(flag: str) -> bool:
    if len(flag) < 3 or flag.startswith("--"):
        return False
    return all(f"-{char}" in _BOOL_FLAGS for char in flag[1:])


def _looks_like_url(token: str) -> bool:
    if token.startswith(("http://", "https://")):
        return True
    return "." in token and not token.startswith("-")


def _note(result: ParsedCurl, message: str) -> None:
    if message not in result.notes:
        result.notes.append(message)


__all__ = ["ParsedCurl", "parse_curl"]
