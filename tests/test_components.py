"""可选组件清单、安装器、curl 解析。

这三样都是 0.4 新增的。共同点是它们都直接面对用户输入或用户环境，
所以测试的重点全在"边界与失败路径"上。
"""

from __future__ import annotations

from pathlib import Path
import sys
import tomllib
from typing import Any

import pytest

from ipclick.components import BY_EXTRA, COMPONENTS, adapter_choices, snapshot, status
from ipclick.web.curl_parser import parse_curl
from ipclick.web.installer import (
    InstallManager,
    Toolchain,
    browser_body_location,
    detect_toolchain,
    extra_requirements,
)


# --------------------------------------------------------------------------- #
# 清单
# --------------------------------------------------------------------------- #


class TestCatalog:
    def test_catalog_matches_pyproject_extras(self):
        """清单和 pyproject 的 extras 必须一一对应。

        0.3 的教训就是这两处会失步：``[niquests]`` 明明是个 extra，Web 端却完全
        没有它的展示位——因为那张表是照着"渲染引擎"手写的。
        """
        root = Path(__file__).resolve().parent.parent
        declared = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
        extras = set(declared["project"]["optional-dependencies"])
        # win / linux 是打包好的组合，不是独立组件
        extras -= {"win", "linux"}
        assert set(BY_EXTRA) == extras

    def test_probe_modules_are_top_level(self):
        """传子模块名会让 find_spec 去 import 父包，那就有副作用了。"""
        for component in COMPONENTS:
            assert all("." not in m for m in component.modules), component

    def test_browser_components_declare_a_body_command(self):
        """除了用系统 Chrome 的那个，其余浏览器组件都要说清楚本体怎么装。"""
        for component in COMPONENTS:
            if component.kind == "browser" and component.extra != "drissionpage":
                assert component.browser_command, component.name

    def test_every_component_explains_itself(self):
        """五个里要选一个，得知道差别在哪。"""
        assert all(c.summary for c in COMPONENTS)

    def test_status_reports_two_levels(self):
        for entry in snapshot():
            assert "package" in entry
            if entry["kind"] == "browser":
                assert "browser" in entry

    def test_missing_package_short_circuits_body_probe(self, monkeypatch: pytest.MonkeyPatch):
        """包都没装就别去扫浏览器目录了——那是白花的文件系统 IO。"""
        from ipclick.utils import module_probe

        monkeypatch.setattr(module_probe, "installed", lambda _n: False)
        called: list[str] = []
        monkeypatch.setattr(
            "ipclick.adapters.browser_engines.browser_ready",
            lambda *a, **k: (called.append("probed"), (None, ""))[1],
        )
        entry = status(BY_EXTRA["camoufox"])
        assert entry["package"] is False
        assert entry["browser"] is None
        assert not called


class TestAdapterChoices:
    def test_generic_browser_is_not_a_component(self):
        """``browser`` 是"引擎由服务端自动选"的占位值，不是第六个 extra。"""
        names = {c.name for c in COMPONENTS}
        assert "browser" not in names

    def test_unavailable_entries_stay_listed(self, monkeypatch: pytest.MonkeyPatch):
        from ipclick.utils import module_probe

        monkeypatch.setattr(module_probe, "installed", lambda _n: False)
        items = {i["value"]: i for g in adapter_choices() for i in g["items"]}
        for component in COMPONENTS:
            assert component.name in items
            assert items[component.name]["available"] is False


# --------------------------------------------------------------------------- #
# 安装器
# --------------------------------------------------------------------------- #


class TestToolchain:
    def test_uv_puts_python_after_the_verb(self):
        """回归：``uv pip --python X install pkg`` 是**错的**，uv 会直接报 usage。

        ``--python`` 是子命令的选项，必须写成 ``uv pip install --python X pkg``。
        第一版就是把它拼在 pip 后面，实测退出码 2。
        """
        uv = Toolchain(kind="uv", executable=("/bin/uv", "pip"))
        command = uv.command("install", "niquests")
        assert command == ("/bin/uv", "pip", "install", "--python", sys.executable, "niquests")

    def test_pip_needs_no_python_flag(self):
        """python -m pip 本来就跑在目标解释器里。"""
        pip = Toolchain(kind="pip", executable=(sys.executable, "-m", "pip"))
        assert pip.command("install", "niquests") == (sys.executable, "-m", "pip", "install", "niquests")

    def test_both_paths_bind_the_running_interpreter(self):
        """装到别的环境去比装不上更糟——装完页面还是看不到，人会以为功能坏了。"""
        for toolchain in (
            Toolchain(kind="uv", executable=("/bin/uv", "pip")),
            Toolchain(kind="pip", executable=(sys.executable, "-m", "pip")),
        ):
            assert sys.executable in toolchain.command("install", "x")

    def test_detects_something_in_this_environment(self):
        """本仓库用 uv 且 venv 里没有 pip —— 正是文档点名的那个组合。"""
        toolchain = detect_toolchain()
        assert toolchain is not None
        assert toolchain.kind in ("pip", "uv")


class TestRequirements:
    def test_requirements_come_from_our_own_metadata(self):
        """不装 ``ipclick[extra]``：那会把 ipclick 自己拖进解析，要么被升级覆盖掉，
        要么因为该版本不在索引上而直接失败（本地开发版必然如此）。
        """
        for component in COMPONENTS:
            reqs = extra_requirements(component.extra)
            assert reqs, component.extra
            assert all(
                component.distribution in r.lower().replace("-", "") or component.distribution in r for r in reqs
            ), (component.extra, reqs)

    def test_requirements_never_mention_ipclick(self):
        for component in COMPONENTS:
            assert not any(r.lower().startswith("ipclick") for r in extra_requirements(component.extra))


class TestWhitelist:
    """包名绝不能拼接用户输入——那就是命令注入。"""

    @pytest.mark.parametrize(
        "hostile",
        [
            "niquests; rm -rf /",
            "../../etc/passwd",
            "niquests --index-url http://evil.example.com",
            "-e /tmp/evil",
            "",
            "NIQUESTS",
        ],
    )
    def test_hostile_extras_are_rejected(self, hostile: str):
        manager = InstallManager()
        for op in (manager.install, manager.uninstall, manager.fetch_browser):
            ok, message = op(hostile)
            assert ok is False
            assert "未知的组件" in message

    def test_only_one_job_at_a_time(self, monkeypatch: pytest.MonkeyPatch):
        """pip 往同一个 site-packages 里写，两个并发的安装能互相覆盖到一半。"""
        manager = InstallManager()
        started: list[tuple[str, ...]] = []
        monkeypatch.setattr(manager, "_run", lambda job: started.append(job.command))

        assert manager.install("niquests")[0] is True
        ok, message = manager.install("camoufox")
        assert ok is False
        assert "已有任务在执行" in message

    def test_fetch_requires_the_package_first(self):
        """先装包再下本体，顺序反了必然失败——不如直接说清楚。"""
        manager = InstallManager()
        ok, message = manager.fetch_browser("drissionpage")
        assert ok is False
        assert "本机已装的 Chrome" in message


class TestUninstallSemantics:
    def test_browser_body_is_reported_not_deleted(self):
        """卸载只卸 Python 包。那 1 GB 还在磁盘上，界面必须把路径和体积摆出来——
        不说的话用户会以为空间释放了。
        """
        location, size = browser_body_location(BY_EXTRA["camoufox"])
        # 本机装没装 camoufox 都可以，但接口形状必须是 (路径, 字节数)
        assert isinstance(location, str)
        assert isinstance(size, int)
        if location:
            assert size >= 0


# --------------------------------------------------------------------------- #
# curl 解析
# --------------------------------------------------------------------------- #


class TestCurlParser:
    def test_devtools_style_command(self):
        """DevTools「复制为 cURL」的典型输出：多行续行 + 单引号里嵌 JSON。"""
        parsed = parse_curl(
            "curl 'https://api.example.com/v1/items?page=2' \\\n"
            "  -H 'accept: application/json' \\\n"
            "  -H 'cookie: sid=abc' \\\n"
            "  --data-raw '{\"qty\":3}' \\\n"
            "  --compressed"
        )
        assert parsed.ok
        form = parsed.as_form()
        assert form["url"] == "https://api.example.com/v1/items?page=2"
        # 有 body 又没写 -X，curl 的语义就是 POST
        assert form["method"] == "POST"
        assert "accept: application/json" in form["headers"]
        assert form["body"] == '{"qty":3}'
        assert not parsed.notes, "--compressed 是已知开关，不该被报成未识别的参数"

    def test_shell_escaped_single_quote(self):
        """``'\"'\"'`` 是 DevTools 转义单引号的写法，手写状态机基本都会挂在这。"""
        parsed = parse_curl("""curl 'https://x.io' --data-raw '{"n":"O'"'"'Brien"}'""")
        assert parsed.body == '{"n":"O\'Brien"}'

    def test_multiple_data_flags_join_with_ampersand(self):
        assert parse_curl("curl -X POST https://x.io -d a=1 -d b=2").body == "a=1&b=2"

    def test_scheme_is_completed(self):
        parsed = parse_curl("curl example.com")
        assert parsed.url == "https://example.com"
        assert any("https://" in n for n in parsed.notes)

    def test_bundled_short_flags(self):
        """``-sS`` 这种粘在一起的写法不能被报成"未识别的参数"。"""
        parsed = parse_curl("curl -sS https://x.io")
        assert parsed.ok
        assert not parsed.notes

    @pytest.mark.parametrize(
        ("command", "fragment"),
        [
            ("curl -k https://x.io", "--insecure"),
            ("curl -F file=@a.png https://x.io", "文件上传"),
            ("curl -u user:pw https://x.io", "Basic"),
            ("curl -x http://proxy:8080 https://x.io", "代理"),
        ],
    )
    def test_unsupported_flags_are_surfaced(self, command: str, fragment: str):
        """静默丢掉一个参数比不支持它更糟——用户会以为已经导入了。"""
        parsed = parse_curl(command)
        assert any(fragment in n for n in parsed.notes), parsed.notes

    def test_headers_and_ua_and_referer(self):
        parsed = parse_curl('curl --url https://x.io -A "Bot/1.0" -e https://ref.io -m 12')
        assert parsed.headers["User-Agent"] == "Bot/1.0"
        assert parsed.headers["Referer"] == "https://ref.io"
        assert parsed.timeout == "12"

    @pytest.mark.parametrize(
        ("command", "fragment"),
        [
            ("", "请先粘贴"),
            ("wget https://x.io", "不像一条 curl"),
            ("curl 'https://unclosed.io", "引号"),
            ("curl -X GET", "没找到网址"),
        ],
    )
    def test_bad_input_gives_a_readable_error(self, command: str, fragment: str):
        parsed = parse_curl(command)
        assert not parsed.ok
        assert fragment in parsed.error

    def test_never_raises(self):
        """粘错东西是常态，这个入口不该抛。"""
        for junk in ("curl " + "'" * 50, "curl -H", "curl --data-raw", "curl -X", "\x00curl x"):
            _ = parse_curl(junk)  # 不抛就算过

    def test_form_shape_matches_the_test_page(self):
        """解析结果要能直接喂给「试一试」的表单字段。"""
        form: dict[str, Any] = parse_curl("curl https://x.io").as_form()
        assert set(form) == {"url", "method", "headers", "body", "timeout"}
