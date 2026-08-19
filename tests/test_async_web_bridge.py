"""异步服务端上，Web 管理端的「试一试」怎么把请求送进事件循环。

这条路径的特殊之处：Web 管理端跑在 HTTP 服务自己的工作线程里，而异步模式下
``TaskService.Send`` 是**协程**。直接调它拿到的是一个没人 await 的 coroutine
对象——Python 只会在垃圾回收时嘟囔一句 "coroutine was never awaited"，
页面上表现为静默失败。所以必须投递回服务端的事件循环。
"""

import asyncio
import threading
from typing import Any

import pytest

from ipclick.dto.proto import task_pb2
from ipclick.dto.response import Response
from ipclick.services.async_task_service import AsyncTaskService
from ipclick.utils.config_util import Settings


class _Ctx:
    def set_code(self, _code: object) -> None: ...
    def set_details(self, _details: str) -> None: ...
    def is_active(self) -> bool:
        return True
    def invocation_metadata(self) -> tuple[()]:
        return ()


def _service(monkeypatch: pytest.MonkeyPatch) -> AsyncTaskService:
    class Echo:
        adapter_name = "curl_cffi"
        supports_async = True

        async def adownload(self, url: str, **kwargs: Any) -> Response:
            await asyncio.sleep(0)
            return Response(url=url, status_code=200, content=b"ok")

    adapter = Echo()
    monkeypatch.setattr("ipclick.services.task_service.get_default_adapter", lambda settings=None: adapter)
    monkeypatch.setattr(
        "ipclick.services.task_service.get_adapter",
        lambda name, settings=None, browser_settings=None: adapter,
    )
    return AsyncTaskService(Settings({}))


class TestSendFromThread:
    def test_round_trip_from_a_worker_thread(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """在另一个线程里调用，应当拿到真正的响应而不是 coroutine 对象。"""
        service = _service(monkeypatch)
        result: dict[str, Any] = {}
        ready = threading.Event()

        def run_loop() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            service.bind_loop(loop)
            ready.set()
            loop.run_forever()

        thread = threading.Thread(target=run_loop, daemon=True)
        thread.start()
        ready.wait(timeout=5)
        try:
            request = task_pb2.ReqTask(uuid="u1", url="http://example.com/x")
            response = service.send_from_thread(request, _Ctx(), timeout=10)
            result["status"] = response.status_code
        finally:
            loop = service._loop
            if loop is not None:
                loop.call_soon_threadsafe(loop.stop)
            thread.join(timeout=5)

        assert result["status"] == 200
        assert not asyncio.iscoroutine(result["status"]), "拿到的是 coroutine 而不是结果"

    def test_fails_loudly_when_the_loop_is_not_bound(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """服务端还没起来时要明确报错，而不是静默失败或永久卡住。"""
        service = _service(monkeypatch)
        request = task_pb2.ReqTask(uuid="u2", url="http://example.com/x")
        with pytest.raises(RuntimeError, match="事件循环未绑定"):
            service.send_from_thread(request, _Ctx(), timeout=1)

    def test_timeout_does_not_hang_the_worker_thread(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """循环被卡住时必须超时返回。

        否则一个卡死的循环会把 HTTP 工作线程一个个占光，整个管理端跟着挂掉，
        而用户看到的只是页面转圈——最难排查的那种故障。
        """
        service = _service(monkeypatch)
        ready = threading.Event()

        def run_loop() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            service.bind_loop(loop)
            ready.set()
            # 用一个同步 sleep 把循环真正堵死，模拟"循环卡住"
            import time as _t

            _t.sleep(3)

        thread = threading.Thread(target=run_loop, daemon=True)
        thread.start()
        ready.wait(timeout=5)
        request = task_pb2.ReqTask(uuid="u3", url="http://example.com/x")
        with pytest.raises(Exception) as excinfo:
            service.send_from_thread(request, _Ctx(), timeout=0.3)
        assert "timeout" in type(excinfo.value).__name__.lower() or "Timeout" in str(excinfo.value)
        thread.join(timeout=5)


class TestWebPageDispatch:
    def test_pages_prefers_the_thread_safe_entry(self) -> None:
        """页面层应当优先用 send_from_thread —— 同步服务上没有这个方法，
        它会回退到直接调 Send，两种模式共用同一份页面代码。"""
        from pathlib import Path

        source = (Path(__file__).resolve().parent.parent / "src" / "ipclick" / "web" / "pages.py").read_text(
            encoding="utf-8"
        )
        assert "send_from_thread" in source, "页面层没有走跨线程入口，异步模式下「试一试」会静默失败"
