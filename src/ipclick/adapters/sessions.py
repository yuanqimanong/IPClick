"""按连接属性复用同步及事件循环绑定的异步 session。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Hashable
import threading
from typing import Any, Generic, TypeVar, final

from ipclick.utils.log_util import log


_K = TypeVar("_K", bound=Hashable)

ASYNC_CLOSE_TIMEOUT = 2.0


@final
class SessionCache(Generic[_K]):
    """线程安全地按键惰性创建并复用同步 session。"""

    def __init__(self, label: str, factory: Callable[[_K], Any]) -> None:
        """保存诊断标签和 session 工厂。"""
        self._label: str = label
        self._factory: Callable[[_K], Any] = factory
        self._sessions: dict[_K, Any] = {}
        self._lock: threading.Lock = threading.Lock()

    def get(self, key: _K) -> Any:
        """返回缓存 session，不存在时只创建一次。"""
        session = self._sessions.get(key)
        if session is not None:
            return session
        with self._lock:
            # 工厂调用也在锁内，避免相同键产生一个未被关闭的竞态实例。
            if key not in self._sessions:
                self._sessions[key] = self._factory(key)
            return self._sessions[key]

    def close(self) -> None:
        """从缓存移除并尽力关闭全部同步 session。"""
        for session in self._drain():
            try:
                session.close()
            except Exception as e:
                log.debug(f"关闭 {self._label} session 失败: {e}")

    def _drain(self) -> list[Any]:
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        return sessions


@final
class AsyncSessionCache(Generic[_K]):
    """按事件循环和连接键复用异步 session。"""

    def __init__(self, label: str, factory: Callable[[_K], Any]) -> None:
        """初始化跨线程可管理的异步 session 表。"""
        self._label: str = label
        self._factory: Callable[[_K], Any] = factory
        self._sessions: dict[tuple[int, _K], tuple[asyncio.AbstractEventLoop, Any]] = {}
        self._lock: threading.Lock = threading.Lock()

    def get(self, key: _K) -> Any:
        """返回当前事件循环专属的缓存 session。"""
        loop = asyncio.get_running_loop()
        composite = (id(loop), key)
        existing = self._sessions.get(composite)
        if existing is not None:
            return existing[1]
        with self._lock:
            if composite not in self._sessions:
                self._sessions[composite] = (loop, self._factory(key))
            return self._sessions[composite][1]

    async def aclose(self) -> None:
        """在所属事件循环中关闭全部 session。"""
        for loop, session in self._drain():
            if loop is asyncio.get_running_loop():
                await self._close_one(session)
            else:
                self._close_from_other_loop(loop, session)

    def close(self) -> None:
        """从同步上下文调度关闭全部异步 session。"""
        for loop, session in self._drain():
            self._close_from_other_loop(loop, session)

    def _drain(self) -> list[tuple[asyncio.AbstractEventLoop, Any]]:
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        return sessions

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


__all__ = ["ASYNC_CLOSE_TIMEOUT", "AsyncSessionCache", "SessionCache"]
