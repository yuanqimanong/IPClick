from __future__ import annotations

from ipclick.utils.secure_util import SecureUtil


def test_md5_keeps_list_element_boundaries() -> None:
    assert SecureUtil.md5(["ab", "c"]) != SecureUtil.md5(["a", "bc"])
