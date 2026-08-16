""" "这个包装了没有" —— 不执行模块代码的探测。

为什么不直接 ``try: import X``
--------------------------------
那是最直觉的写法，也是 0.3 的做法，但它有三个问题，全都在"运行时装依赖"这个
场景下暴露出来：

1. **结论固化在进程启动那一刻。** 模块级的 try/except 只跑一次。之后在终端
   ``pip install "ipclick[camoufox]"``，Web 端刷新多少次都还是"未装"——页面只是
   重新渲染进程内存里的旧结论。
2. **卸载探测不出来。** 真正 import 过的模块留在 ``sys.modules`` 里，卸掉磁盘上
   的包也不会让它消失。于是"装"能看见、"卸"看不见。
3. **探测本身有副作用。** import 会执行模块顶层代码。camoufox 牵着 playwright，
   在一个正在服务请求的进程里重载它风险很高（``importlib.reload`` 更甚）。

:func:`importlib.util.find_spec` 三条全避开：它只做文件系统级查找，不执行任何
模块代码；配合 :func:`importlib.invalidate_caches` 能立刻看到新装的包；而因为
它查的是磁盘而不是 ``sys.modules``，卸载也能如实反映出来。

**真正的 import 仍然在执行路径上**（适配器构造时懒加载）。这个模块只负责
"状态展示"这一件事，两者职责分开。

只探测**顶层**模块名
--------------------
``find_spec("playwright.async_api")`` 会为了找到子模块而 import 父包
``playwright`` —— 那就又有副作用了。``find_spec("playwright")`` 不会。所以这里
的约定是：只传顶层名。
"""

from __future__ import annotations

import importlib
import importlib.metadata
import importlib.util
import threading


#: 探测结果缓存。``find_spec`` 每次都要走一遍 sys.path 上的 finder，
#: 而这些结论在一次"安装/卸载"之间是不变的——热路径上（适配器构造、
#: 每次渲染页面）重复付这个代价没有意义。
_installed_cache: dict[str, bool] = {}
_version_cache: dict[str, str | None] = {}
_lock = threading.Lock()


def installed(module: str) -> bool:
    """顶层模块 ``module`` 在磁盘上装了没。**不执行**它的代码。

    Args:
        module: **顶层**模块名（``"camoufox"``，不是 ``"camoufox.pkgman"``）。
    """
    cached = _installed_cache.get(module)
    if cached is not None:
        return cached

    with _lock:
        # 双检：拿锁期间可能已经有别的线程填好了
        if module in _installed_cache:
            return _installed_cache[module]
        _installed_cache[module] = _find(module)
        return _installed_cache[module]


def _find(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, AttributeError, ValueError):
        # ImportError/ModuleNotFoundError：父包缺失（本函数只收顶层名，理论上到不了）
        # ValueError：模块已在 sys.modules 里但 __spec__ 是 None
        # AttributeError：极少数自定义 finder 的残缺实现
        return False


def version(distribution: str) -> str | None:
    """已安装发行版的版本号；没装返回 None。

    与 :func:`installed` 分开是因为两者问的不是一件事：模块名（``DrissionPage``）
    和发行版名（``drissionpage``）经常对不上，而"装没装"只该由模块名决定。
    """
    if distribution in _version_cache:
        return _version_cache[distribution]
    with _lock:
        try:
            resolved: str | None = importlib.metadata.version(distribution)
        except Exception:
            # PackageNotFoundError 是常态；损坏的 dist-info 会抛别的，
            # 一个查版本号的调用不该把调用方带崩。
            resolved = None
        _version_cache[distribution] = resolved
        return resolved


def invalidate() -> None:
    """丢掉全部缓存，下次探测重新看磁盘。

    在**安装或卸载之后**调用。:func:`importlib.invalidate_caches` 是关键的一半：
    解释器会缓存每个目录的内容清单，不清掉的话刚装好的包在本进程里依然"不存在"。
    """
    importlib.invalidate_caches()
    with _lock:
        _installed_cache.clear()
        _version_cache.clear()


__all__ = ["installed", "invalidate", "version"]
