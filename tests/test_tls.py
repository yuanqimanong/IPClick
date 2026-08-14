"""gRPC 传输层加密与双向认证。

用临时生成的自签名 CA 现签证书，起真实的 gRPC 服务端，做真实握手——
不 mock。TLS 这层"看起来配上了、实际没生效"是最典型的失败方式，
只有真连一次才能证明它确实在起作用。
"""

from collections.abc import Iterator
from concurrent import futures
import datetime
import ipaddress
from pathlib import Path
from typing import Any

import grpc
import pytest

from ipclick.dto.proto import task_pb2, task_pb2_grpc
from ipclick.exceptions import ConfigError, TransportError
from ipclick.health import check_health
from ipclick.sdk import Downloader
from ipclick.services.task_service import TaskService
from ipclick.tls import TLSSettings, channel_credentials, describe, server_credentials, warn_if_insecure
from ipclick.utils.config_util import Settings
from tests.test_sdk_e2e import EchoAdapter, _free_port


cryptography = pytest.importorskip("cryptography", reason="生成测试证书需要 cryptography")

from cryptography import x509  # noqa: E402
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402
from cryptography.x509.oid import NameOID  # noqa: E402


# ---------------------------------------------------------------------- #
# 证书工具
# ---------------------------------------------------------------------- #

_ONE_DAY = datetime.timedelta(days=1)


def _key() -> rsa.RSAPrivateKey:
    # 2048 位：测试里每次都要现生成，4096 会让整个文件慢好几秒
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _write(directory: Path, name: str, key: rsa.RSAPrivateKey, cert: x509.Certificate) -> tuple[str, str]:
    key_path = directory / f"{name}.key"
    cert_path = directory / f"{name}.crt"
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return str(cert_path), str(key_path)


def _make_ca(directory: Path) -> tuple[rsa.RSAPrivateKey, x509.Certificate, str]:
    key = _key()
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "ipclick-test-ca")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - _ONE_DAY)
        .not_valid_after(now + _ONE_DAY)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    cert_path, _ = _write(directory, "ca", key, cert)
    return key, cert, cert_path


def _make_leaf(
    directory: Path,
    name: str,
    common_name: str,
    ca_key: rsa.RSAPrivateKey,
    ca_cert: x509.Certificate,
    *,
    server: bool,
) -> tuple[str, str]:
    key = _key()
    now = datetime.datetime.now(datetime.UTC)
    builder = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)]))
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - _ONE_DAY)
        .not_valid_after(now + _ONE_DAY)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
    )
    if server:
        # gRPC 校验的是 SAN，不是 CN——只写 CN 的证书在现代 TLS 栈里一律被拒
        builder = builder.add_extension(
            x509.SubjectAlternativeName([x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
            critical=False,
        )
    return _write(directory, name, key, builder.sign(ca_key, hashes.SHA256()))


@pytest.fixture(scope="module")
def pki(tmp_path_factory: pytest.TempPathFactory) -> dict[str, str]:
    """一套 CA + 服务端证书 + 客户端证书 + 一张"别家 CA 签的"客户端证书。"""
    directory = tmp_path_factory.mktemp("pki")
    ca_key, ca_cert, ca_path = _make_ca(directory)
    server_cert, server_key = _make_leaf(directory, "server", "localhost", ca_key, ca_cert, server=True)
    client_cert, client_key = _make_leaf(directory, "client", "ipclick-client", ca_key, ca_cert, server=False)

    # 另一套完全独立的 CA，用来验证"不被信任的客户端证书会被拒"
    rogue_dir = directory / "rogue"
    rogue_dir.mkdir()
    rogue_key, rogue_cert, _ = _make_ca(rogue_dir)
    rogue_client_cert, rogue_client_key = _make_leaf(
        rogue_dir, "client", "rogue-client", rogue_key, rogue_cert, server=False
    )

    return {
        "ca": ca_path,
        "server_cert": server_cert,
        "server_key": server_key,
        "client_cert": client_cert,
        "client_key": client_key,
        "rogue_client_cert": rogue_client_cert,
        "rogue_client_key": rogue_client_key,
    }


def _serve(tls: TLSSettings, monkeypatch: pytest.MonkeyPatch) -> Iterator[int]:
    """起一个真实 gRPC 服务端，适配器换成假的。"""
    adapter = EchoAdapter()
    monkeypatch.setattr("ipclick.services.task_service.get_default_adapter", lambda settings=None: adapter)
    monkeypatch.setattr(
        "ipclick.services.task_service.get_adapter",
        lambda name, settings=None, browser_settings=None: adapter,
    )
    service = TaskService(Settings({}))
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
    task_pb2_grpc.add_TaskServiceServicer_to_server(service, server)

    port = _free_port()
    if tls.enabled:
        assert server.add_secure_port(f"127.0.0.1:{port}", server_credentials(tls)) == port
    else:
        assert server.add_insecure_port(f"127.0.0.1:{port}") == port
    server.start()
    try:
        yield port
    finally:
        server.stop(grace=0).wait(timeout=5)
        service.cleanup()


# ---------------------------------------------------------------------- #
# 配置解析
# ---------------------------------------------------------------------- #


class TestSettings:
    def test_disabled_by_default(self):
        """默认关闭，保持与旧部署兼容。"""
        s = TLSSettings.from_config({})
        assert not s.enabled
        assert not s.require_client_cert

    def test_none_config(self):
        assert not TLSSettings.from_config(None).enabled

    def test_parses_all_keys(self):
        s = TLSSettings.from_config(
            {
                "tls": {
                    "enabled": True,
                    "cert_file": "/c.pem",
                    "key_file": "/k.pem",
                    "ca_file": "/ca.pem",
                    "require_client_cert": True,
                    "server_name_override": "ipclick.internal",
                }
            }
        )
        assert (s.enabled, s.require_client_cert) == (True, True)
        assert (s.cert_file, s.key_file, s.ca_file) == ("/c.pem", "/k.pem", "/ca.pem")
        assert s.server_name_override == "ipclick.internal"

    def test_empty_strings_become_none(self):
        """默认配置里这些是空串占位，不能被当成"配了个空路径"。"""
        s = TLSSettings.from_config({"tls": {"cert_file": "", "key_file": "  ", "ca_file": ""}})
        assert (s.cert_file, s.key_file, s.ca_file) == (None, None, None)

    def test_has_client_identity(self):
        assert not TLSSettings(cert_file="/c").has_client_identity
        assert TLSSettings(cert_file="/c", key_file="/k").has_client_identity

    def test_describe(self):
        assert "未启用" in describe(TLSSettings())
        assert "mTLS" in describe(TLSSettings(enabled=True, require_client_cert=True))
        assert "仅验证服务端" in describe(TLSSettings(enabled=True))


class TestConfigErrors:
    """配置不全必须立刻失败——带着半套 TLS 配置起来比起不来危险得多。"""

    def test_server_needs_cert_and_key(self):
        with pytest.raises(ConfigError, match="cert_file 与 key_file"):
            server_credentials(TLSSettings(enabled=True))

    def test_missing_file_reports_path(self, tmp_path: Path):
        missing = str(tmp_path / "nope.pem")
        with pytest.raises(ConfigError, match=r"nope\.pem"):
            server_credentials(TLSSettings(enabled=True, cert_file=missing, key_file=missing))

    def test_non_pem_file_rejected(self, tmp_path: Path):
        junk = tmp_path / "junk.pem"
        junk.write_text("not a certificate")
        with pytest.raises(ConfigError, match="PEM"):
            server_credentials(TLSSettings(enabled=True, cert_file=str(junk), key_file=str(junk)))

    def test_empty_file_rejected(self, tmp_path: Path):
        empty = tmp_path / "empty.pem"
        empty.write_text("")
        with pytest.raises(ConfigError, match="空文件"):
            server_credentials(TLSSettings(enabled=True, cert_file=str(empty), key_file=str(empty)))

    def test_mtls_without_ca_is_rejected(self, pki: dict[str, str]):
        """要求客户端出示证书却不给验证它的 CA，等于任何自签名证书都能过——
        mTLS 形同虚设。这必须是硬错误，不能默默降级。"""
        with pytest.raises(ConfigError, match="require_client_cert"):
            server_credentials(
                TLSSettings(
                    enabled=True,
                    cert_file=pki["server_cert"],
                    key_file=pki["server_key"],
                    require_client_cert=True,
                )
            )

    def test_client_cert_without_key_rejected(self, pki: dict[str, str]):
        with pytest.raises(ConfigError, match="成对出现"):
            channel_credentials(TLSSettings(enabled=True, cert_file=pki["client_cert"]))

    def test_client_key_without_cert_rejected(self, pki: dict[str, str]):
        with pytest.raises(ConfigError, match="成对出现"):
            channel_credentials(TLSSettings(enabled=True, key_file=pki["client_key"]))


class TestInsecureWarning:
    def test_warns_on_public_bind(self, caplog: pytest.LogCaptureFixture):
        with caplog.at_level("WARNING"):
            warn_if_insecure(TLSSettings(), "0.0.0.0")

    def test_quiet_on_loopback(self):
        """只监听本机时明文是可接受的，不必吵。"""
        warn_if_insecure(TLSSettings(), "127.0.0.1")

    def test_quiet_when_tls_on(self):
        warn_if_insecure(TLSSettings(enabled=True), "0.0.0.0")


# ---------------------------------------------------------------------- #
# 真实握手
# ---------------------------------------------------------------------- #


class TestServerAuthOnly:
    """只开 TLS：加密链路 + 客户端验证服务端身份。"""

    def test_round_trip(self, pki: dict[str, str], monkeypatch: pytest.MonkeyPatch):
        server_tls = TLSSettings(enabled=True, cert_file=pki["server_cert"], key_file=pki["server_key"])
        client_tls = TLSSettings(enabled=True, ca_file=pki["ca"])
        for port in _serve(server_tls, monkeypatch):
            with Downloader(host="127.0.0.1", port=port, tls=client_tls) as d:
                resp = d.get("http://example.com/x")
            assert resp.status_code == 200

    def test_plaintext_client_cannot_talk_to_tls_server(self, pki: dict[str, str], monkeypatch: pytest.MonkeyPatch):
        """回归：客户端漏配 TLS 时必须连不上，而不是悄悄降级成明文。"""
        server_tls = TLSSettings(enabled=True, cert_file=pki["server_cert"], key_file=pki["server_key"])
        for port in _serve(server_tls, monkeypatch):
            with Downloader(host="127.0.0.1", port=port) as d:
                resp = d.get("http://example.com/x", timeout=3, max_retries=0)
            assert resp.status_code == -1, "明文客户端居然连上了 TLS 服务端"

    def test_tls_client_cannot_talk_to_plaintext_server(self, pki: dict[str, str], monkeypatch: pytest.MonkeyPatch):
        client_tls = TLSSettings(enabled=True, ca_file=pki["ca"])
        for port in _serve(TLSSettings(), monkeypatch):
            with Downloader(host="127.0.0.1", port=port, tls=client_tls) as d:
                resp = d.get("http://example.com/x", timeout=3, max_retries=0)
            assert resp.status_code == -1

    def test_untrusted_ca_is_rejected(self, pki: dict[str, str], monkeypatch: pytest.MonkeyPatch):
        """客户端拿别家 CA 去验，必须验不过——否则等于没验证服务端身份。"""
        server_tls = TLSSettings(enabled=True, cert_file=pki["server_cert"], key_file=pki["server_key"])
        rogue_ca = str(Path(pki["rogue_client_cert"]).parent / "ca.crt")
        client_tls = TLSSettings(enabled=True, ca_file=rogue_ca)
        for port in _serve(server_tls, monkeypatch):
            with Downloader(host="127.0.0.1", port=port, tls=client_tls) as d:
                resp = d.get("http://example.com/x", timeout=3, max_retries=0)
            assert resp.status_code == -1, "陌生 CA 签的服务端证书被接受了"

    def test_server_name_override(self, pki: dict[str, str], monkeypatch: pytest.MonkeyPatch):
        """证书签给 localhost，连的是 127.0.0.1——靠 override 对上名字。"""
        server_tls = TLSSettings(enabled=True, cert_file=pki["server_cert"], key_file=pki["server_key"])
        client_tls = TLSSettings(enabled=True, ca_file=pki["ca"], server_name_override="localhost")
        for port in _serve(server_tls, monkeypatch):
            with Downloader(host="127.0.0.1", port=port, tls=client_tls) as d:
                assert d.get("http://example.com/x").status_code == 200


class TestMutualTLS:
    """开 mTLS：服务端反过来验证客户端证书。"""

    def _server_tls(self, pki: dict[str, str]) -> TLSSettings:
        return TLSSettings(
            enabled=True,
            cert_file=pki["server_cert"],
            key_file=pki["server_key"],
            ca_file=pki["ca"],
            require_client_cert=True,
        )

    def test_valid_client_cert_accepted(self, pki: dict[str, str], monkeypatch: pytest.MonkeyPatch):
        client_tls = TLSSettings(
            enabled=True, ca_file=pki["ca"], cert_file=pki["client_cert"], key_file=pki["client_key"]
        )
        for port in _serve(self._server_tls(pki), monkeypatch):
            with Downloader(host="127.0.0.1", port=port, tls=client_tls) as d:
                assert d.get("http://example.com/x").status_code == 200

    def test_client_without_cert_is_rejected(self, pki: dict[str, str], monkeypatch: pytest.MonkeyPatch):
        """这是 mTLS 的全部意义所在：没有证书就进不来。"""
        client_tls = TLSSettings(enabled=True, ca_file=pki["ca"])
        for port in _serve(self._server_tls(pki), monkeypatch):
            with Downloader(host="127.0.0.1", port=port, tls=client_tls) as d:
                resp = d.get("http://example.com/x", timeout=3, max_retries=0)
            assert resp.status_code == -1, "没带客户端证书居然通过了 mTLS"

    def test_client_cert_from_other_ca_is_rejected(self, pki: dict[str, str], monkeypatch: pytest.MonkeyPatch):
        """带了证书但不是我们 CA 签的，同样要拒——否则谁都能自签一张进来。"""
        client_tls = TLSSettings(
            enabled=True,
            ca_file=pki["ca"],
            cert_file=pki["rogue_client_cert"],
            key_file=pki["rogue_client_key"],
        )
        for port in _serve(self._server_tls(pki), monkeypatch):
            with Downloader(host="127.0.0.1", port=port, tls=client_tls) as d:
                resp = d.get("http://example.com/x", timeout=3, max_retries=0)
            assert resp.status_code == -1, "陌生 CA 签的客户端证书被接受了"

    def test_download_raises_transport_error(self, pki: dict[str, str], monkeypatch: pytest.MonkeyPatch):
        """低层 download() 仍然抛异常，握手失败属于传输失败。"""
        from ipclick.dto.models import DownloadTask

        client_tls = TLSSettings(enabled=True, ca_file=pki["ca"])
        for port in _serve(self._server_tls(pki), monkeypatch):
            with (
                Downloader(host="127.0.0.1", port=port, tls=client_tls) as d,
                pytest.raises(TransportError),
            ):
                d.download(DownloadTask(url="http://example.com/x", timeout=3, max_retries=0))


class TestHealthProbe:
    """探活也必须走 TLS——服务端开了 TLS 而探活还用明文，
    集群会把健康节点全判成挂了。"""

    def test_probe_over_tls(self, pki: dict[str, str], monkeypatch: pytest.MonkeyPatch):
        from ipclick.health import HealthReporter

        adapter = EchoAdapter()
        monkeypatch.setattr("ipclick.services.task_service.get_default_adapter", lambda settings=None: adapter)
        server_tls = TLSSettings(enabled=True, cert_file=pki["server_cert"], key_file=pki["server_key"])
        client_tls = TLSSettings(enabled=True, ca_file=pki["ca"])

        service = TaskService(Settings({}))
        server = grpc.server(futures.ThreadPoolExecutor(max_workers=2))
        task_pb2_grpc.add_TaskServiceServicer_to_server(service, server)
        reporter = HealthReporter()
        reporter.register(server)
        port = _free_port()
        server.add_secure_port(f"127.0.0.1:{port}", server_credentials(server_tls))
        server.start()
        reporter.set_serving()
        try:
            healthy, detail = check_health(f"127.0.0.1:{port}", timeout=5, tls=client_tls)
            assert healthy, f"TLS 探活失败: {detail}"

            # 回归：不带 TLS 去探一个 TLS 服务端，必须探不通
            plain_ok, _ = check_health(f"127.0.0.1:{port}", timeout=3)
            assert not plain_ok, "明文探活居然探通了 TLS 服务端"
        finally:
            server.stop(grace=0).wait(timeout=5)
            service.cleanup()


class TestConfigDrivenClient:
    """客户端不传 tls= 时应当从 [SECURITY.tls] 读——否则配置文件就是死的。"""

    def test_client_reads_config(self, pki: dict[str, str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        from ipclick.config_loader.loader import load_config

        cfg = tmp_path / "ipclick.toml"
        cfg.write_text(
            f'[SECURITY.tls]\nenabled = true\nca_file = "{pki["ca"]}"\n',
            encoding="utf-8",
        )
        server_tls = TLSSettings(enabled=True, cert_file=pki["server_cert"], key_file=pki["server_key"])
        for port in _serve(server_tls, monkeypatch):
            load_config.cache_clear()
            try:
                with Downloader(config_path=str(cfg), host="127.0.0.1", port=port) as d:
                    assert d.tls.enabled, "[SECURITY.tls] 没被读进来"
                    assert d.get("http://example.com/x").status_code == 200
            finally:
                load_config.cache_clear()


class TestAsyncClient:
    async def test_async_over_mtls(self, pki: dict[str, str], monkeypatch: pytest.MonkeyPatch):
        """异步客户端与同步版共用 ClientBase 的凭据构造，这里确认它真的用上了。"""
        from ipclick.aio import AsyncDownloader

        server_tls = TLSSettings(
            enabled=True,
            cert_file=pki["server_cert"],
            key_file=pki["server_key"],
            ca_file=pki["ca"],
            require_client_cert=True,
        )
        client_tls = TLSSettings(
            enabled=True, ca_file=pki["ca"], cert_file=pki["client_cert"], key_file=pki["client_key"]
        )
        for port in _serve(server_tls, monkeypatch):
            async with AsyncDownloader(host="127.0.0.1", port=port, tls=client_tls) as d:
                resp = await d.get("http://example.com/x")
            assert resp.status_code == 200

    async def test_async_without_cert_rejected(self, pki: dict[str, str], monkeypatch: pytest.MonkeyPatch):
        from ipclick.aio import AsyncDownloader

        server_tls = TLSSettings(
            enabled=True,
            cert_file=pki["server_cert"],
            key_file=pki["server_key"],
            ca_file=pki["ca"],
            require_client_cert=True,
        )
        client_tls = TLSSettings(enabled=True, ca_file=pki["ca"])
        for port in _serve(server_tls, monkeypatch):
            async with AsyncDownloader(host="127.0.0.1", port=port, tls=client_tls) as d:
                resp = await d.get("http://example.com/x", timeout=3, max_retries=0)
            assert resp.status_code == -1


class TestShippedConfig:
    """随包分发的默认配置里，[SECURITY.tls] 必须是 [SECURITY] 的子表。

    回归：把子表插在 [SECURITY] 的标量键中间，后面那些键（allowed_schemes、
    block_private_networks…）会全部落进子表——SSRF 防护被静默架空，
    而 TLS 看起来一切正常。TOML 的这个坑不写测试很难发现。
    """

    def _security(self) -> dict[str, object]:
        from ipclick.config_loader.loader import load_config

        load_config.cache_clear()
        try:
            return dict(load_config().get("SECURITY", {}))
        finally:
            load_config.cache_clear()

    def test_ssrf_keys_stay_at_top_level(self):
        security = self._security()
        for key in ("allowed_schemes", "block_metadata_endpoints", "block_private_networks", "allowlist"):
            assert key in security, f"{key} 掉进子表了，SSRF 配置会失效"

    def test_tls_subtable_present_and_off(self):
        security = self._security()
        assert isinstance(security.get("tls"), dict)
        assert TLSSettings.from_config(security).enabled is False


def test_no_plaintext_fallback_anywhere():
    """回归护栏：新增连接点时别忘了走 TLS。

    只允许已知位置出现 insecure：两个客户端、服务端绑定、健康探活、
    集群转发。每一处都必须是 if tls.enabled 的 else 分支——
    转发那处由 test_forwarding_uses_tls 正面验证。
    """
    src = Path(__file__).resolve().parent.parent / "src" / "ipclick"
    offenders: list[str] = []
    allowed = {"sdk.py", "aio.py", "server.py", "health.py", "tls.py", "forwarder.py"}
    for file in src.rglob("*.py"):
        if "_pb2" in file.name or file.name in allowed:
            continue
        text = file.read_text(encoding="utf-8")
        if "insecure_channel" in text or "add_insecure_port" in text:
            offenders.append(str(file.relative_to(src)))
    assert not offenders, f"这些文件绕过了 TLS 通路: {offenders}"


def test_forwarding_uses_tls(pki: dict[str, str], monkeypatch: pytest.MonkeyPatch):
    """集群转发这一跳必须真的走 TLS，而不是在允许清单里挂个名。

    正面证据：目标节点只接受 TLS。带 TLS 的转发器能成功转过去；
    不带 TLS 的转发器连不上（于是入口自己兜底执行，落点是本机）。
    """
    from ipclick.cluster.forwarder import ForwardingTaskService
    from ipclick.cluster.node import ClusterConfig

    server_tls = TLSSettings(
        enabled=True,
        cert_file=pki["server_cert"],
        key_file=pki["server_key"],
    )
    client_tls = TLSSettings(
        enabled=True,
        ca_file=pki["ca"],
        # 证书里的 SAN 是 localhost，而连的是 127.0.0.1
        server_name_override="localhost",
    )

    for port in _serve(server_tls, monkeypatch):
        section: dict[str, Any] = {
            "nodes": [{"id": "peer", "address": f"127.0.0.1:{port}"}],
            "forward": "on",
            # 本节点不在 nodes 里 -> 只会转发，不会自己抢活，落点因此可判定
            "self_id": "entry",
            "probe_interval": 3600,
            "max_failover": 0,
        }
        request = task_pb2.ReqTask(uuid="u1", url="http://example.com/x")

        with_tls = ForwardingTaskService(
            Settings({"CLUSTER": section}), ClusterConfig.from_config(section), tls=client_tls
        )
        try:
            ok = with_tls.Send(request, _FakeContext())
        finally:
            with_tls.cleanup()
        assert ok.status_code == 200, "带 TLS 应该能转发成功"
        assert ok.trace.forwarded is True

        plaintext = ForwardingTaskService(Settings({"CLUSTER": section}), ClusterConfig.from_config(section))
        try:
            failed = plaintext.Send(request, _FakeContext())
        finally:
            plaintext.cleanup()
        # 明文连不上只接受 TLS 的端口，转发失败后入口自己兜底 —— 而入口没有
        # 真适配器，所以状态码不会是 200。关键是它确实没能连上。
        assert failed.trace.forwarded is False, "明文不该能连上只接受 TLS 的节点"


class _FakeContext:
    """转发测试用的最小 ServicerContext。"""

    def set_code(self, _code: object) -> None: ...

    def set_details(self, _details: str) -> None: ...

    def is_active(self) -> bool:
        return True

    def invocation_metadata(self) -> tuple[tuple[str, str], ...]:
        return ()
