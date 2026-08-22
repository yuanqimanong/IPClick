"""机密探测与配置文件告警的回归测试。"""

from __future__ import annotations

from typing import Any

import pytest

from ipclick.secrets import (
    SECRETS,
    SUPPRESS_KEY,
    audit,
    config_value,
    describe_source,
    proxy_config,
    warn_secrets_in_config,
)


@pytest.mark.parametrize("bad_section", ["oops", ["a", "b"], 5, 1.5, True, None])
def test_a_mistyped_config_section_does_not_crash_the_startup_path(bad_section: Any) -> None:
    """把某个节写成标量/数组是合法 TOML，但不能让服务在启动时崩掉。

    ``_dig`` 原来对每一层都 ``dict(node or {})``，于是 ``SECURITY = "oops"`` 这种手误
    会抛出 stdlib 的 ``ValueError: dictionary update sequence element #0 has length 1``。
    ``warn_secrets_in_config`` 在 server.py 的启动路径上——服务带着一个看不懂的
    ValueError 直接死掉，还不说是哪个节写错了。而同仓库的 utils.config_util.section
    对同样的输入早就是容错的，两处口径必须一致。
    """
    config: dict[str, Any] = {"SECURITY": bad_section, "SERVER": {"port": 9528}}

    # 公开入口全都不能抛
    assert warn_secrets_in_config(config) == []
    assert len(audit(config)) == len(SECRETS)
    assert proxy_config(config) == {}
    for spec in SECRETS:
        _ = describe_source(config, spec)
        assert config_value(config, spec) is None


def test_a_quoted_false_does_not_silently_suppress_the_secret_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    """``allow_secrets_in_config = "false"`` 不能被当成 true。

    原来用 bool() 判断，带引号的 "false"（TOML 里很容易手滑写成字符串）是真值，
    于是拿到的结果与所求完全相反：机密照样进 git，而那条提醒被自己关掉了。

    注意断言的是**有没有告警**而不是返回值：两条分支都返回 found，只有日志不同，
    只断返回值的话这条用例根本测不到抑制逻辑。
    """
    from ipclick.utils.log_util import log

    warnings: list[str] = []
    monkeypatch.setattr(log, "warning", lambda message, *a, **k: warnings.append(str(message)))

    def warned(suppress: object) -> bool:
        warnings.clear()
        _ = warn_secrets_in_config({"SECURITY": {"auth_token": "s3cr3t", SUPPRESS_KEY: suppress}})
        return bool(warnings)

    assert warned("false") is True, '"false" 被当成了真值，提醒被自己关掉了'
    assert warned("no") is True
    assert warned("0") is True
    assert warned(False) is True
    # 真正的开启写法仍然抑制
    assert warned(True) is False
    assert warned("true") is False
