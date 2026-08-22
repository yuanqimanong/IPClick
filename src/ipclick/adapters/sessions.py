"""按连接属性复用同步及事件循环绑定的异步 session。"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Callable, Generator, Hashable
from contextlib import contextmanager
from dataclasses import dataclass
import threading
from typing import Any, Generic, TypeVar, final

from ipclick.utils.log_util import log


_K = TypeVar("_K", bound=Hashable)

ASYNC_CLOSE_TIMEOUT = 2.0

# 缓存上限。key 里带着**每请求可变**的 proxy 串，而粘性会话代理的常规用法就是每个
# 请求一个会话 ID（http://user-Csess<随机>:pw@gw:8000）——不设上限的话每个请求都会
# 留下一个 session，每个各自持有连接池与 keep-alive 连接，实测约 130 KB/个，
# 一万次请求就是 1.3 GB 常驻加上一堆 fd，进程活着就回收不掉。
#
# 刻意不做"把会话凭据从 proxy 里归一化掉"：不同会话凭据对应不同出口 IP，
# 共用一个 session 会串连接、把粘性会话语义破坏掉。
DEFAULT_MAX_SESSIONS = 64


def reset_cookies(session: Any) -> None:
    """清空复用 session 上残留的 cookie jar。

    session 按 (proxy, verify, impersonate) 复用，key 里没有任何调用方身份，
    而 session 自带的 cookie jar 从不清空。于是 A 调用方在目标站点拿到的
    ``Set-Cookie`` 会被自动带到 B 之后发往同一 host 的请求上——对一个多租户的
    共享服务端来说，这是跨调用方的会话串号。

    清空放在**发请求之前**：单次请求内部（含重定向链）的 cookie 传递仍由底层库照常处理，
    变的只是"两次请求之间不再自动保持会话"。服务端本来就没有调用方会话的概念，
    要保持会话请在请求里显式传 cookies。
    """
    jar = getattr(session, "cookies", None)
    clear = getattr(jar, "clear", None)
    if not callable(clear):
        return
    try:
        clear()
    except Exception as e:  # 不同实现的 jar 语义不一，清不掉也不该让请求失败
        log.debug(f"清空复用 session 的 cookie jar 失败：{type(e).__name__}: {e}")


@final
@dataclass
class _SessionEntry:
    """一个缓存 session 及其在途使用者计数。

    LRU 淘汰会 close 掉 session，而在途请求可能正拿着它——流式下载尤其明显：整条流的
    生命周期里都持着同一个 session，而它的"最近使用时间"停在请求开始那一刻，很容易先
    变成 LRU。在别人用着的时候关掉，传输就在中途断了，而调用方看到的是一个莫名的
    连接错误。所以 close 必须推迟到最后一个使用者放手之后——与 cluster 的
    ``_ChannelEntry`` 同一套口径。
    """

    session: Any
    users: int = 0
    retired: bool = False
    closed: bool = False


@final
class SessionCache(Generic[_K]):
    """线程安全地按键惰性创建并复用同步 session，带 LRU 上限。

    上限是必须的：key 里含每请求可变的代理串，粘性会话代理的常规用法就是每个请求一个
    会话 ID，不淘汰的话每个请求都会留下一个持有连接池的 session。
    """

    def __init__(self, label: str, factory: Callable[[_K], Any], *, max_sessions: int = DEFAULT_MAX_SESSIONS) -> None:
        """保存诊断标签、session 工厂与缓存上限。"""
        self._label: str = label
        self._factory: Callable[[_K], Any] = factory
        self._sessions: OrderedDict[_K, _SessionEntry] = OrderedDict()
        self._max_sessions: int = max(1, max_sessions)
        self._lock: threading.Lock = threading.Lock()
        self._evicted: int = 0

    @contextmanager
    def lease(self, key: _K) -> Generator[Any]:
        """租借一个 session，并在使用期间阻止它被 LRU 淘汰关闭。

        请求期间持有 session 的调用方必须走这里，不能用 ``get()``：LRU 会在别的线程
        淘汰并 close 掉正在用的那一个（见 ``_SessionEntry``）。
        """
        with self._lock:
            entry = self._sessions.get(key)
            if entry is None:
                # 工厂调用也在锁内，避免相同键产生一个未被关闭的竞态实例。
                entry = _SessionEntry(self._factory(key))
                self._sessions[key] = entry
            self._sessions.move_to_end(key)
            stale = self._evict_locked()
            # 计数放在锁块最后一句：它后面再有任何可能抛异常的语句，异常都会从锁块
            # 逃出而不进 try，finally 不执行、users 永久 +1，这个 session 就再也关不掉。
            entry.users += 1
        self._close_all(stale)
        try:
            yield entry.session
        finally:
            with self._lock:
                entry.users -= 1
                victim = entry if entry.retired and entry.users == 0 and not entry.closed else None
                if victim is not None:
                    victim.closed = True
            if victim is not None:
                self._close_one(victim.session)

    def get(self, key: _K) -> Any:
        """返回缓存 session，不存在时只创建一次；超过上限按 LRU 淘汰。

        **不保证返回后它还活着**——调用方一放手它就可能被淘汰关闭。请求期间要持有
        请用 ``lease()``。
        """
        with self.lease(key) as session:
            return session

    def _evict_locked(self) -> list[_SessionEntry]:
        """摘掉超出上限的最久未用 session；只把没人用的交给调用方关闭。"""
        stale: list[_SessionEntry] = []
        while len(self._sessions) > self._max_sessions:
            _, victim = self._sessions.popitem(last=False)
            victim.retired = True
            self._evicted += 1
            # 还有在途使用者的只做标记，由最后一个使用者退出时收尾。
            if victim.users == 0 and not victim.closed:
                victim.closed = True
                stale.append(victim)
        if stale:
            log.debug(
                f"{self._label} session 缓存超过上限 {self._max_sessions}，"
                f"已淘汰 {len(stale)} 个（累计 {self._evicted}）"
            )
        return stale

    def _close_one(self, session: Any) -> None:
        try:
            session.close()
        except Exception as e:
            log.debug(f"关闭 {self._label} session 失败: {e}")

    def _close_all(self, entries: list[_SessionEntry]) -> None:
        # 关闭放在锁外：session.close() 会做网络 IO，占着锁会把其他请求全堵住。
        for entry in entries:
            self._close_one(entry.session)

    def close(self) -> None:
        """从缓存移除并尽力关闭全部同步 session。

        这是停机路径，优雅停机的 grace 期已经过了，所以在途的也一并关掉；打上
        ``closed`` 标记，避免租借退出时重复 close。
        """
        self._close_all(self._drain())

    def _drain(self) -> list[_SessionEntry]:
        with self._lock:
            entries = [entry for entry in self._sessions.values() if not entry.closed]
            for entry in entries:
                entry.retired = True
                entry.closed = True
            self._sessions.clear()
        return entries


@final
@dataclass
class _AsyncSessionEntry:
    """异步版的缓存条目，额外记住 session 所属的事件循环。"""

    session: Any
    loop: asyncio.AbstractEventLoop
    users: int = 0
    retired: bool = False
    closed: bool = False


@final
class AsyncSessionCache(Generic[_K]):
    """按事件循环和连接键复用异步 session，带 LRU 上限（理由同 ``SessionCache``）。"""

    def __init__(self, label: str, factory: Callable[[_K], Any], *, max_sessions: int = DEFAULT_MAX_SESSIONS) -> None:
        """初始化跨线程可管理的异步 session 表。"""
        self._label: str = label
        self._factory: Callable[[_K], Any] = factory
        self._sessions: OrderedDict[tuple[int, _K], _AsyncSessionEntry] = OrderedDict()
        self._max_sessions: int = max(1, max_sessions)
        self._lock: threading.Lock = threading.Lock()

    @contextmanager
    def lease(self, key: _K) -> Generator[Any]:
        """租借当前事件循环专属的 session，使用期间不会被淘汰关闭。

        用同步 contextmanager 而不是 ``asynccontextmanager``：整段没有一个 await，
        换成异步版只会逼调用点多一个 ``async with``，没有任何好处。
        """
        loop = asyncio.get_running_loop()
        composite = (id(loop), key)
        with self._lock:
            entry = self._sessions.get(composite)
            if entry is None:
                entry = _AsyncSessionEntry(self._factory(key), loop)
                self._sessions[composite] = entry
            self._sessions.move_to_end(composite)
            stale = self._evict_locked()
            entry.users += 1  # 与同步版同理，必须是锁块最后一句
        self._close_all(stale)
        try:
            yield entry.session
        finally:
            with self._lock:
                entry.users -= 1
                victim = entry if entry.retired and entry.users == 0 and not entry.closed else None
                if victim is not None:
                    victim.closed = True
            if victim is not None:
                self._close_from_other_loop(victim.loop, victim.session)

    def get(self, key: _K) -> Any:
        """返回当前事件循环专属的缓存 session。

        **不保证返回后它还活着**——请求期间要持有请用 ``lease()``。
        """
        with self.lease(key) as session:
            return session

    def _evict_locked(self) -> list[_AsyncSessionEntry]:
        """摘掉超出上限的最久未用 session；只把没人用的交给调用方关闭。

        上限与同步版一致：proxy 每请求可变时不淘汰就会无界增长。
        """
        stale: list[_AsyncSessionEntry] = []
        while len(self._sessions) > self._max_sessions:
            _, victim = self._sessions.popitem(last=False)
            victim.retired = True
            if victim.users == 0 and not victim.closed:
                victim.closed = True
                stale.append(victim)
        if stale:
            log.debug(f"{self._label} 协程 session 缓存超过上限 {self._max_sessions}，已淘汰 {len(stale)} 个")
        return stale

    def _close_all(self, entries: list[_AsyncSessionEntry]) -> None:
        for entry in entries:
            self._close_from_other_loop(entry.loop, entry.session)

    async def aclose(self) -> None:
        """在所属事件循环中关闭全部 session。"""
        for entry in self._drain():
            if entry.loop is asyncio.get_running_loop():
                await self._close_one(entry.session)
            else:
                self._close_from_other_loop(entry.loop, entry.session)

    def close(self) -> None:
        """从同步上下文调度关闭全部异步 session。"""
        for entry in self._drain():
            self._close_from_other_loop(entry.loop, entry.session)

    def _drain(self) -> list[_AsyncSessionEntry]:
        with self._lock:
            entries = [entry for entry in self._sessions.values() if not entry.closed]
            for entry in entries:
                entry.retired = True
                entry.closed = True
            self._sessions.clear()
        return entries

    async def _close_one(self, session: Any) -> None:
        try:
            await session.close()
        except Exception as e:
            log.debug(f"关闭 {self._label} 协程 session 失败: {e}")

    def _close_from_other_loop(self, loop: asyncio.AbstractEventLoop, session: Any) -> None:
        if loop.is_closed():
            log.debug(f"{self._label} 协程 session 所属事件循环已关闭，跳过显式关闭")
            return
        try:
            try:
                current = asyncio.get_running_loop()
            except RuntimeError:
                current = None
            if current is loop:
                # 在事件循环自己的线程里同步等待会造成死锁；交给当前循环异步收尾。
                _ = loop.create_task(self._close_one(session))
                return
            if loop.is_running():
                # session 必须回到创建它的事件循环关闭。
                future = asyncio.run_coroutine_threadsafe(session.close(), loop)
                _ = future.result(timeout=ASYNC_CLOSE_TIMEOUT)
            else:
                _ = loop.run_until_complete(session.close())
        except Exception as e:
            log.debug(f"关闭 {self._label} 协程 session 失败: {e}")


__all__ = [
    "ASYNC_CLOSE_TIMEOUT",
    "AsyncSessionCache",
    "SessionCache",
    "reset_cookies",
]
