"""链路记录页面、实时 fragment 与 JSON 查询视图。"""

from __future__ import annotations

from typing import Any, final

from ipclick.trace import TraceRecord
from ipclick.web.pages.context import PageContext
from ipclick.web.templates import DEFAULT_LIVE_MS, LIVE_INTERVALS, render_trace, trace_live


TRACE_LIMIT_MAX = 1000


def _live_ms(query: dict[str, str]) -> int:
    if "_" not in query:
        return DEFAULT_LIVE_MS
    raw = query.get("live", "")
    if raw == "":
        return 0
    if raw == "1":
        return DEFAULT_LIVE_MS
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_LIVE_MS
    return value if any(value == ms for ms, _, _ in LIVE_INTERVALS) else DEFAULT_LIVE_MS


def _fragment_url(base: str, filters: dict[str, str]) -> str:
    from urllib.parse import urlencode

    query = {k: v for k, v in filters.items() if v}
    return f"{base}?{urlencode(query)}" if query else base


@final
class TracePage:
    """对链路查询参数设限，并输出 HTML 或 JSON 表示。"""

    def __init__(self, ctx: PageContext) -> None:
        self.ctx: PageContext = ctx

    def _query_records(self, query: dict[str, str]) -> tuple[list[TraceRecord], str, dict[str, str]]:
        filters = {
            "status": query.get("status", ""),
            "adapter": query.get("adapter", ""),
            "q": query.get("q", ""),
            "limit": query.get("limit", "100"),
        }
        try:
            limit = min(TRACE_LIMIT_MAX, max(1, int(filters["limit"] or 100)))
        except ValueError:
            limit = 100
        filters["limit"] = str(limit)
        records, source = self.ctx.recorder.query(
            limit=limit,
            status_class=filters["status"],
            adapter=filters["adapter"],
            keyword=filters["q"],
        )
        return records, source, filters

    def trace_page(self, query: dict[str, str], username: str, csrf: str) -> str:
        """渲染带筛选条件和可选实时刷新的完整链路页。"""
        records, source, filters = self._query_records(query)
        return render_trace(
            records,
            self.ctx.recorder.stats(),
            filters,
            username,
            csrf,
            source=source,
            live_ms=_live_ms(query),
            fragment_url=_fragment_url("/fragment/trace", filters),
        )

    def trace_fragment(self, query: dict[str, str]) -> str:
        """渲染实时轮询替换的链路表格 fragment。"""
        records, source, _ = self._query_records(query)
        return trace_live(records, self.ctx.recorder.stats(), source=source)

    def trace_json(self, query: dict[str, str]) -> dict[str, Any]:
        """返回受数量上限约束的链路记录 JSON 数据。"""
        records, source, filters = self._query_records(query)
        return {
            "source": source,
            "filters": filters,
            "stats": self.ctx.recorder.stats(),
            "records": [
                {
                    "ts": r.ts,
                    "when": r.when,
                    "uuid": r.uuid,
                    "node_id": r.node_id,
                    "adapter": r.adapter,
                    "method": r.method,
                    "url": r.url,
                    "status_code": r.status_code,
                    "duration_ms": r.duration_ms,
                    "size": r.size,
                    "attempts": r.attempts,
                    "forwarded": r.forwarded,
                    "queued_ms": r.queued_ms,
                    "stream": r.stream,
                    "error": r.error,
                }
                for r in records
            ],
        }
