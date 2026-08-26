from __future__ import annotations

from pathlib import Path

import pytest

from ipclick.config_loader.dotenv import parse_env
from ipclick.config_loader.env_writer import format_env_value, set_env_values, update_env_file
from ipclick.dto.models import ProxyConfig
from ipclick.exceptions import ValidationError
from ipclick.web.proxy_tunnel import (
    AUTH_KEY_PLACEHOLDER,
    AUTH_PASSWORD_PLACEHOLDER,
    parse_tunnel,
    render_masked,
    render_masked_endpoint,
)


@pytest.mark.parametrize(
    ("raw", "fmt"),
    [
        ("socks5://user1:pass2@gate.example.com:7000", "url"),
        ("gate.example.com:7000:user1:pass2", "host_colon_auth"),
        ("user1:pass2@gate.example.com:7000", "auth_at_host"),
        ("gate.example.com:7000@user1:pass2", "host_at_auth"),
    ],
)
def test_every_format_lands_on_the_same_pieces(raw: str, fmt: str) -> None:
    """四种排法拆出来的东西必须一致，否则"换个格式重贴一次"会得到不同配置。"""
    parsed = parse_tunnel(raw, fmt)

    assert (parsed.host, parsed.port) == ("gate.example.com", 7000)
    assert (parsed.username, parsed.password) == ("user1", "pass2")


@pytest.mark.parametrize(
    "raw",
    [
        "socks5://user1:pass2@gate.example.com:7000",
        "gate.example.com:7000:user1:pass2",
        "user1:pass2@gate.example.com:7000",
        "gate.example.com:7000@user1:pass2",
    ],
)
def test_auto_detection_covers_all_four(raw: str) -> None:
    """默认就是自动识别：多数人不会先去数自己那一行属于哪种格式。"""
    parsed = parse_tunnel(raw)

    assert (parsed.host, parsed.port, parsed.username, parsed.password) == (
        "gate.example.com",
        7000,
        "user1",
        "pass2",
    )


def test_only_the_url_format_carries_a_scheme() -> None:
    """另外三种格式里没有协议字段，只能落到默认的 http——不能凭空猜 socks5。"""
    assert parse_tunnel("socks5://u:p@h.example.com:7000").scheme == "socks5"
    assert parse_tunnel("h.example.com:7000:u:p").scheme == "http"


@pytest.mark.parametrize(
    ("raw", "fmt", "password"),
    [
        ("http://u:p@ss:w0rd@h.example.com:7000", "url", "p@ss:w0rd"),
        ("h.example.com:7000:u:p:w0rd", "host_colon_auth", "p:w0rd"),
        ("u:p@ss@h.example.com:7000", "auth_at_host", "p@ss"),
        ("h.example.com:7000@u:p@ss", "host_at_auth", "p@ss"),
    ],
)
def test_passwords_keep_their_colons_and_at_signs(raw: str, fmt: str, password: str) -> None:
    """密码里带 ':' 和 '@' 很常见。切错一刀，密码会被截断成一个"看起来对"的短串，
    然后你对着 407 排查半天——所以每种格式都得从正确的那一端切。
    """
    parsed = parse_tunnel(raw, fmt)

    assert parsed.host == "h.example.com"
    assert parsed.username == "u"
    assert parsed.password == password


def test_ambiguous_shapes_refuse_to_guess() -> None:
    """a:b@c:d 两边都像 主机:端口 时不猜。

    猜错的后果不是报错，而是把密码当主机名写进 toml、把主机名当账号写进 .env——
    两个文件都"保存成功"，错的地方却在四个字段里。宁可让人手动选一次格式。
    """
    with pytest.raises(ValidationError, match="两种排法都讲得通"):
        _ = parse_tunnel("1.2.3.4:8080@5.6.7.8:9090")


def test_a_numeric_password_is_ambiguous_too() -> None:
    """纯数字密码会让 user:12345 也像一个 主机:端口。真实存在，所以要拦住而不是猜。"""
    with pytest.raises(ValidationError, match="两种排法都讲得通"):
        _ = parse_tunnel("user:12345@gate.example.com:7000")

    explicit = parse_tunnel("user:12345@gate.example.com:7000", "auth_at_host")
    assert (explicit.username, explicit.password) == ("user", "12345")


def test_an_endpoint_without_credentials_is_accepted() -> None:
    """自建的免密隧道是合法用法，不能因为没账号密码就拒绝。"""
    parsed = parse_tunnel("gate.example.com:7000")

    assert (parsed.host, parsed.port) == ("gate.example.com", 7000)
    assert (parsed.username, parsed.password) == ("", "")


def test_ipv6_endpoints_survive_the_url_format() -> None:
    parsed = parse_tunnel("http://u:p@[2001:db8::1]:7000")

    assert (parsed.host, parsed.port) == ("2001:db8::1", 7000)


@pytest.mark.parametrize("raw", ["gate.example.com", "gate.example.com:0", "gate.example.com:70000", "  "])
def test_unusable_endpoints_are_rejected(raw: str) -> None:
    """端口缺失或超范围时必须当场拒绝：写进 toml 之后 ipclick 自己也加载不了。"""
    with pytest.raises(ValidationError):
        _ = parse_tunnel(raw)


def test_echoed_placeholders_mean_leave_the_credentials_alone() -> None:
    """回显串原样交回来时，凭据位置是占位符名字，不是真值。

    不识别这一点的话，点一次保存就会把账号密码改成 "{IPCLICK_PROXY_AUTH_KEY}" 这个
    字面字符串——代理立刻失效，而页面上看起来一切正常。
    """
    echoed = render_masked("socks5", "gate.example.com", 7000, with_credentials=True)
    assert echoed == f"socks5://{AUTH_KEY_PLACEHOLDER}:{AUTH_PASSWORD_PLACEHOLDER}@gate.example.com:7000"

    parsed = parse_tunnel(echoed)
    assert parsed.credentials_are_placeholders
    assert (parsed.host, parsed.port, parsed.scheme) == ("gate.example.com", 7000, "socks5")


def test_echo_omits_the_credential_slot_when_there_are_none() -> None:
    assert render_masked("http", "gate.example.com", 7000, with_credentials=False) == "http://gate.example.com:7000"


@pytest.mark.parametrize("port", [0, 70000, "", None, "abc"])
def test_echo_is_empty_when_the_endpoint_is_unusable(port: object) -> None:
    """端口还是模板里的 0 时不该回显出 http://host:0 —— 那是个能骗过人的假地址。"""
    assert render_masked("http", "gate.example.com", port, with_credentials=True) == ""


def test_env_update_keeps_comments_and_other_keys() -> None:
    """.env 里的注释是模板自带的说明。整份重写会把它们冲掉，下次谁都不知道该填什么。"""
    original = "# IPClick 机密配置\n\n# 代理账号\nIPCLICK_PROXY_AUTH_KEY=\n\n# 其他\nOTHER=keep\n"

    text, changed = set_env_values(original, {"IPCLICK_PROXY_AUTH_KEY": "user1"})

    assert changed == ["IPCLICK_PROXY_AUTH_KEY"]
    assert "# 代理账号" in text
    assert "OTHER=keep" in text
    assert parse_env(text)["IPCLICK_PROXY_AUTH_KEY"] == "user1"


def test_env_update_reports_nothing_when_the_value_is_unchanged() -> None:
    """每次保存都会把整组机密传进来。不比一遍的话「已更新」永远等于项数。"""
    _, changed = set_env_values("IPCLICK_PROXY_AUTH_KEY=user1\n", {"IPCLICK_PROXY_AUTH_KEY": "user1"})

    assert changed == []


def test_env_update_appends_a_missing_key() -> None:
    text, changed = set_env_values("OTHER=keep\n", {"IPCLICK_PROXY_AUTH_PASSWORD": "pass2"})

    assert changed == ["IPCLICK_PROXY_AUTH_PASSWORD"]
    assert parse_env(text)["IPCLICK_PROXY_AUTH_PASSWORD"] == "pass2"


def test_env_update_does_not_append_an_empty_key() -> None:
    """文件里本来没有、要设的又是空值：那就是"保持不设置"，别追加一行噪音。"""
    text, changed = set_env_values("OTHER=keep\n", {"IPCLICK_PROXY_AUTH_KEY": ""})

    assert changed == []
    assert "IPCLICK_PROXY_AUTH_KEY" not in text


@pytest.mark.parametrize("value", ["p@ss w0rd", 'quo"te', "back\\slash", "hash#tag", " lead", "trail "])
def test_awkward_values_round_trip_through_the_dotenv_parser(value: str) -> None:
    """写出去的必须能被 dotenv 原样读回来——密码里出现这些字符不算稀奇。"""
    text, _ = set_env_values("K=\n", {"K": value})

    assert parse_env(text)["K"] == value
    assert format_env_value(value).startswith('"')


def test_creating_a_missing_env_file_starts_from_the_template(tmp_path: Path) -> None:
    """新目录里没有 .env 时不能只写一行——模板里的说明和其余五项都要在。"""
    target = tmp_path / ".env"

    path, changed = update_env_file({"IPCLICK_PROXY_AUTH_KEY": "user1"}, target)

    assert path == target
    assert changed == ["IPCLICK_PROXY_AUTH_KEY"]
    text = target.read_text(encoding="utf-8")
    assert "IPCLICK_WEB_PASSWORD" in text
    assert parse_env(text)["IPCLICK_PROXY_AUTH_KEY"] == "user1"


def test_a_bom_prefixed_env_file_is_still_updated_in_place(tmp_path: Path) -> None:
    """记事本 / Set-Content 存出来的 .env 带 BOM。按 utf-8 读会把首个键名读成 '\\ufeffKEY'，
    于是"更新"变成在文件末尾追加一行同名键——dotenv 后写覆盖前写，看着能用，
    但文件里从此有两行同名配置。
    """
    target = tmp_path / ".env"
    target.write_text("IPCLICK_PROXY_AUTH_KEY=old\n", encoding="utf-8-sig")

    _, changed = update_env_file({"IPCLICK_PROXY_AUTH_KEY": "user1"}, target)

    assert changed == ["IPCLICK_PROXY_AUTH_KEY"]
    text = target.read_text(encoding="utf-8-sig")
    assert text.count("IPCLICK_PROXY_AUTH_KEY") == 1
    assert parse_env(text)["IPCLICK_PROXY_AUTH_KEY"] == "user1"


def test_a_password_without_an_account_is_refused_instead_of_dropped() -> None:
    """只填密码不填账号必须报错，不能静默丢掉整个凭据段。

    原先 `if self.auth_key` 一挡，密码就没了：拼出来是个不带鉴权的代理 URL，请求
    照发、被隧道商拒掉（多半 407）。于是"我明明填了密码"和"代理没认证"同时成立，
    而报错里一个字都不提凭据——这类静默降级最难查。
    """
    with pytest.raises(ValidationError, match="配了代理密码却没配代理账号"):
        _ = ProxyConfig(host="gate.example.com", port=7000, auth_password="pass2").to_url()


def test_an_account_without_a_password_is_still_allowed() -> None:
    """反过来是合法的：有些隧道只认账号（或把整串放在账号位）。"""
    url = ProxyConfig(host="gate.example.com", port=7000, auth_key="user1").to_url()

    assert url == "http://user1:@gate.example.com:7000"


def test_no_credentials_at_all_stays_unauthenticated() -> None:
    assert ProxyConfig(host="gate.example.com", port=7000).to_url() == "http://gate.example.com:7000"


def test_credentials_are_url_encoded_into_the_proxy_url() -> None:
    """密码里的 ':' '@' 不转义的话，代理 URL 会被 curl 拆错。"""
    url = ProxyConfig(host="gate.example.com", port=7000, auth_key="user1", auth_password="p@ss:w0rd").to_url()

    assert url == "http://user1:p%40ss%3Aw0rd@gate.example.com:7000"


def test_a_hand_written_tunnel_server_is_what_gets_echoed() -> None:
    """toml 里手写的 tunnel_server 压过 host/port，回显就必须照着它来。

    照着 host/port 回显的话，页面显示的是一个（多半空着的）地址，实际在用的是另一个——
    "页面在说谎"这类问题没人会怀疑到配置文件上，只会一路去查代理商。
    """
    echoed = render_masked_endpoint("socks5", "gw.example.com:9000", with_credentials=True)

    assert echoed == f"socks5://{AUTH_KEY_PLACEHOLDER}:{AUTH_PASSWORD_PLACEHOLDER}@gw.example.com:9000"


def test_an_endpoint_without_a_port_still_echoes() -> None:
    """tunnel_server 允许不带端口（to_url 原样用），回显不能因此变空。"""
    assert render_masked_endpoint("http", "gw.example.com", with_credentials=False) == "http://gw.example.com"


def test_tunnel_server_is_no_longer_a_web_editable_field() -> None:
    """页面上两个都叫「隧道代理接入地址」的输入框是误导，只保留粘贴框那一个。"""
    from ipclick.web.editable import FIELDS

    assert "PROXY.tunnel_server" not in FIELDS
    assert "PROXY.host" in FIELDS


def test_the_proxy_group_never_claims_a_restart_is_needed() -> None:
    """[PROXY] 不参与 gRPC 服务端的请求处理：服务端用调用方带来的 proxy。

    这一节只被「试一试」（保存后立即生效）、SDK 的 proxy=True（调用方自己的进程）
    和 CLI 读。打「需重启」会让人白重启一次，还以为问题出在别处。
    """
    from ipclick.web.editable import FIELDS

    # 按**配置节**筛，不按页面分组：分组是展示，可以挪（trust_env 就挪进了「代理」组，
    # 但它是 [DOWNLOADER] 的、适配器启动时才建，确实需要重启）。
    proxy_fields = [f for f in FIELDS.values() if f.section == "PROXY"]
    assert len(proxy_fields) == 6
    assert not [f.key for f in proxy_fields if f.restart]


def test_every_group_holds_more_than_one_field() -> None:
    """一项一组的折叠块纯属噪音：点开只有一行，找起来还多一次点击。

    「Web 管理端」原来就只有一个主题选择，已经并进「服务端与 Web 管理端」——那一组
    本来就管着 WEB.port / WEB.host。
    """
    from ipclick.web.editable import GROUPS

    assert [name for name, fields in GROUPS if len(fields) < 2] == []


def test_related_downloader_knobs_are_not_split_across_groups() -> None:
    """[DOWNLOADER.concurrency] 的八项曾经被拆在「连接池」和「按 host 限流」两组里，
    于是同一个配置节要在两个折叠块之间来回找。
    """
    from ipclick.web.editable import GROUPS

    homes = {name for name, fields in GROUPS for f in fields if f.section == "DOWNLOADER.concurrency"}
    assert homes == {"并发与限流"}
