"""进程内的请求计数：总量、状态分布、按适配器统计。

和链路记录是两件事——它不落盘、不保留单条记录，只是本进程启动以来的累计量。
放在同一个模块里过，但两者没有共同的状态或生命周期。
"""

from __future__ import annotations

from dataclasses import dataclass
import threading
import time
from typing import Any, final

from ipclick.trace.records import TraceRecord


@final
@dataclass
class _AdapterStat:
    """进程内单个适配器的累积计数。"""

    total: int = 0
    ok: int = 0
    duration_ms: int = 0
    bytes: int = 0

    def snapshot(self) -> dict[str, Any]:
        """生成不暴露可变内部状态的统计快照。"""
        return {
            "total": self.total,
            "ok": self.ok,
            "failed": self.total - self.ok,
            "avg_ms": round(self.duration_ms / self.total, 1) if self.total else 0.0,
            "bytes": self.bytes,
        }


@final
class Counters:
    """线程安全地累计当前进程的请求、重试与拒绝指标。"""

    def __init__(self) -> None:
        """初始化空计数器并记录进程统计起点。"""
        self._lock: threading.Lock = threading.Lock()
        self.started_at: float = time.time()
        self.total: int = 0
        self.ok: int = 0
        self.in_flight: int = 0
        self.peak_in_flight: int = 0
        self.duration_ms: int = 0
        self.bytes: int = 0
        self.by_status: dict[str, int] = {}
        self.by_adapter: dict[str, _AdapterStat] = {}
        self.retries: dict[str, int] = {}
        self.rejected: dict[str, int] = {}

    def enter(self) -> None:
        """标记一个请求进入执行阶段。"""
        with self._lock:
            self.in_flight += 1
            self.peak_in_flight = max(self.peak_in_flight, self.in_flight)

    def leave(self, record: TraceRecord) -> None:
        """标记请求完成并归入状态及适配器统计。"""
        with self._lock:
            self.in_flight = max(0, self.in_flight - 1)
            self.total += 1
            self.duration_ms += record.duration_ms
            self.bytes += record.size
            cls_ = record.status_class
            self.by_status[cls_] = self.by_status.get(cls_, 0) + 1
            stat = self.by_adapter.get(record.adapter)
            if stat is None:
                stat = _AdapterStat()
                self.by_adapter[record.adapter] = stat
            stat.total += 1
            stat.duration_ms += record.duration_ms
            stat.bytes += record.size
            if record.ok:
                self.ok += 1
                stat.ok += 1

    def record_retry(self, reason: str) -> None:
        """按规范化原因累计一次重试。"""
        with self._lock:
            self.retries[reason] = self.retries.get(reason, 0) + 1

    def record_rejected(self, reason: str) -> None:
        """按原因累计一次准入拒绝。"""
        with self._lock:
            self.rejected[reason] = self.rejected.get(reason, 0) + 1

    def snapshot(self) -> dict[str, Any]:
        """在同一把锁内生成一致的进程指标快照。"""
        with self._lock:
            total = self.total
            return {
                "started_at": self.started_at,
                "uptime_seconds": int(time.time() - self.started_at),
                "total": total,
                "ok": self.ok,
                "failed": total - self.ok,
                "success_rate": round(self.ok / total * 100, 1) if total else 0.0,
                "avg_ms": round(self.duration_ms / total, 1) if total else 0.0,
                "bytes": self.bytes,
                "in_flight": self.in_flight,
                "peak_in_flight": self.peak_in_flight,
                "by_status": dict(sorted(self.by_status.items())),
                "by_adapter": {k: v.snapshot() for k, v in sorted(self.by_adapter.items())},
                "retries": dict(sorted(self.retries.items())),
                "rejected": dict(sorted(self.rejected.items())),
            }
