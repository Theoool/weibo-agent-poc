"""队列状态。

没有这个端点，"任务一直 queued" 只能靠人去猜：是前面堵着、还是 worker 没起、
还是 worker 起来就崩了。`worker_alive` 与 `oldest_queued_at` 合起来能直接区分。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from store import RunStore

from ..deps import get_store

router = APIRouter(prefix="/api/queue", tags=["queue"])

WORKER_HINT = "没有检测到在线的 worker（90 秒内有心跳）。启动：uv run python -m worker"


@router.get("", summary="队列概览：积压、在线 worker、最近任务")
def get_queue(
    store: RunStore = Depends(get_store),
    limit: int = Query(10, ge=1, le=50, description="返回最近多少个任务"),
) -> dict:
    overview = store.queue_overview()
    overview["recent"] = overview["recent"][:limit]
    overview["hint"] = "" if overview["worker_alive"] else WORKER_HINT
    return overview
