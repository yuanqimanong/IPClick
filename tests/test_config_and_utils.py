"""配置加载优先级、适配器注册表、工具函数。"""

from pathlib import Path
from typing import Any

import pytest

from ipclick.adapters.registry import ADAPTER_CLASSES, get_adapter, get_default_adapter, register_adapter
from ipclick.exceptions import AdapterError
from ipclick.utils.config_util import ConfigUtil, Settings
from ipclick.utils.path_util import PathUtil
from ipclick.utils.secure_util import SecureUtil


class TestConfigMerge:
    def test_later_file_overrides_earlier(self, tmp_path: Path):
        a = tmp_path / "a.toml"
        b = tmp_path / "b.toml"
        a.write_text('[SERVER]\nhost = "1.1.1.1"\nport = 1000\n', encoding="utf-8")
        b.write_text("[SERVER]\nport = 2000\n", encoding="utf-8")

        merged = ConfigUtil.load([a, b])
        assert merged["SERVER"]["port"] == 2000
        # 未被覆盖的键要保留
        assert merged["SERVER"]["host"] == "1.1.1.1"

    def test_missing_files_are_skipped(self, tmp_path: Path):
        real = tmp_path / "real.toml"
        real.write_text("[SERVER]\nport = 1234\n", encoding="utf-8")
        merged = ConfigUtil.load([tmp_path / "nope.toml", real])
        assert merged["SERVER"]["port"] == 1234

    def test_malformed_toml_does_not_crash(self, tmp_path: Path):
        bad = tmp_path / "bad.toml"
        bad.write_text("this is not = = toml [[[", encoding="utf-8")
        good = tmp_path / "good.toml"
        good.write_text("[SERVER]\nport = 9\n", encoding="utf-8")

        merged = ConfigUtil.load([bad, good])
        assert merged["SERVER"]["port"] == 9

    def test_empty_list_returns_empty_settings(self):
        assert ConfigUtil.merge([]) == Settings()

    def test_dot_access(self, tmp_path: Path):
        f = tmp_path / "c.toml"
        f.write_text('[SERVER]\nhost = "h"\n', encoding="utf-8")
        assert ConfigUtil.load(f).SERVER.host == "h"


class TestLoadConfigPrecedence:
    def test_env_vars_override_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        from ipclick.config_loader.loader import load_config

        cfg_file = tmp_path / "ipclick.toml"
        cfg_file.write_text('[SERVER]\nhost = "1.1.1.1"\nport = 1111\n', encoding="utf-8")

        monkeypatch.setenv("IPCLICK_HOST", "9.9.9.9")
        monkeypatch.setenv("IPCLICK_PORT", "9999")
        load_config.cache_clear()

        cfg = load_config(str(cfg_file))
        assert cfg["SERVER"]["host"] == "9.9.9.9"
        assert cfg["SERVER"]["port"] == 9999

    def test_defaults_present_without_user_config(self):
        from ipclick.config_loader.loader import load_config

        load_config.cache_clear()
        cfg = load_config()
        assert "SERVER" in cfg
        assert "SECURITY" in cfg  # 0.2.0 新增的安全配置节

    def test_shipped_proxy_default_is_empty(self):
        """回归：随包分发的默认配置里预置了 127.0.0.1:7890（作者本机的 Clash），
        会让所有用户的 proxy=True 都指向他们自己机器的该端口。"""
        from ipclick.config_loader.loader import load_config
        from ipclick.dto.models import ProxyConfig

        load_config.cache_clear()
        proxy = dict(load_config().get("PROXY", {}))
        assert not proxy.get("host"), f"默认配置不应预置代理地址，实际为 {proxy.get('host')!r}"
        assert ProxyConfig(**proxy).to_url() is None

    def test_security_defaults(self):
        from ipclick.config_loader.loader import load_config

        load_config.cache_clear()
        security = dict(load_config().get("SECURITY", {}))
        assert security["block_metadata_endpoints"] is True
        assert security["allowed_schemes"] == ["http", "https"]


class TestRegistry:
    def test_known_adapters_resolve(self):
        assert get_adapter("curl_cffi").adapter_name == "curl_cffi"
        assert get_adapter("httpx").adapter_name == "httpx"

    def test_default_adapter_is_curl_cffi(self):
        assert get_default_adapter().adapter_name == "curl_cffi"

    def test_unimplemented_adapter_raises_adapter_error(self):
        """枚举里宣称支持 6 种适配器，实际只实现了 2 种，其余要给出清楚的报错。"""
        with pytest.raises(AdapterError, match="尚未支持"):
            get_adapter("playwright")

    def test_error_lists_available_adapters(self):
        with pytest.raises(AdapterError, match="curl_cffi"):
            get_adapter("nope")

    def test_register_custom_adapter(self):
        from ipclick.adapters.base import DownloaderAdapter
        from ipclick.dto.response import Response

        class MyAdapter(DownloaderAdapter):
            adapter_name = "my_test_adapter"

            def download(self, url: str, **kwargs: Any) -> Response:  # type: ignore[override]
                return Response(url=url, status_code=200)

        try:
            register_adapter(MyAdapter)
            assert get_adapter("my_test_adapter").adapter_name == "my_test_adapter"
        finally:
            ADAPTER_CLASSES.pop("my_test_adapter", None)


class TestSecureUtil:
    def test_stable_hash(self):
        assert SecureUtil.md5("hello world") == "5eb63bbbe01eeed093cb22bb8f5acdc3"

    def test_short_form_is_16_chars(self):
        assert len(SecureUtil.md5("x", short=True)) == 16

    def test_dict_key_order_does_not_matter(self):
        assert SecureUtil.md5({"a": 1, "b": 2}) == SecureUtil.md5({"b": 2, "a": 1})

    def test_dicts_inside_a_list_are_canonicalised(self):
        """回归：循环里误用 isinstance(data, ...) 而非 isinstance(_d, ...)，
        列表中的 dict 会走 str() 分支，键序不同就得到不同哈希。"""
        assert SecureUtil.md5([{"a": 1, "b": 2}]) == SecureUtil.md5([{"b": 2, "a": 1}])

    def test_none_components_are_handled(self):
        assert SecureUtil.md5([None, None, None])

    def test_different_inputs_differ(self):
        assert SecureUtil.md5([None, "h", 1]) != SecureUtil.md5([None, "h", 2])


class TestPathUtil:
    def test_absolute_path_unchanged(self, tmp_path: Path):
        assert PathUtil.resolve_path(tmp_path) == tmp_path

    def test_relative_resolved_against_base(self, tmp_path: Path):
        assert PathUtil.resolve_path("a/b.log", tmp_path) == tmp_path / "a/b.log"

    def test_ensure_parent_dir_creates(self, tmp_path: Path):
        target = tmp_path / "x" / "y" / "z.log"
        PathUtil.ensure_parent_dir(target)
        assert target.parent.is_dir()


class TestJsonHelpers:
    def test_datetime_serializer(self):
        from datetime import datetime

        from ipclick.utils import json_serializer

        assert json_serializer(datetime(2026, 1, 2, 3, 4, 5)) == "2026-01-02T03:04:05"

    def test_serializer_rejects_unknown_type(self):
        from ipclick.utils import json_serializer

        with pytest.raises(TypeError):
            json_serializer(object())

    def test_json_hook_revives_datetimes(self):
        from datetime import datetime

        from ipclick.utils import json_hook

        assert json_hook({"t": "2026-01-02T03:04:05"})["t"] == datetime(2026, 1, 2, 3, 4, 5)

    def test_json_hook_leaves_plain_strings(self):
        from ipclick.utils import json_hook

        assert json_hook({"s": "hello"})["s"] == "hello"
