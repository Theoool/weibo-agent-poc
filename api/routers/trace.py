"""轨迹与模型调用。

这两组数据的等级不同，接口也刻意分开：
- `/trace` 是 A 级原始记录，逐条、有序、可回放；
- `/llm-calls` 列表默认**不返回全文**（一次 event_build 的题面 17KB，20 条就是几百 KB），
  要全文就单独取 `/llm-calls/{id}`。列表里保留了 `prompt_digest` 与长度，
  足够判断"是不是同一次调用"。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from store import RunStore

from ..deps import get_store, require_run

router = APIRouter(prefix="/api", tags=["trace"])


@router.get("/runs/{run_id}/trace", summary="轨迹事件（按 seq 有序，可分页/按 step 过滤）")
def get_trace(
    run_id: str,
    step: str | None = Query(None, description="只看某一类事件，如 llm_call / gap_detected"),
    limit: int = Query(200, ge=1, le=5000),
    offset: int = Query(0, ge=0),
    store: RunStore = Depends(get_store),
) -> dict:
    require_run(store, run_id)
    records = store.load_trace(run_id, limit=limit, step=step, offset=offset)
    return {
        "run_id": run_id,
        "total": store.count_trace(run_id, step=step),
        "limit": limit,
        "offset": offset,
        "items": records,
    }


@router.get("/runs/{run_id}/llm-calls", summary="模型调用列表（默认不含全文）")
def list_llm_calls(
    run_id: str,
    include_text: bool = Query(False, description="是否返回题面与输出全文"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    store: RunStore = Depends(get_store),
) -> dict:
    require_run(store, run_id)
    calls = store.load_llm_calls(run_id, limit=limit, offset=offset, include_text=include_text)
    ok = sum(1 for c in calls if c["ok"])
    return {
        "run_id": run_id,
        "total": store.count_llm_calls(run_id),
        "limit": limit,
        "offset": offset,
        "ok": ok,
        "failed": len(calls) - ok,
        "items": calls,
    }


@router.get("/llm-calls/{call_id}", summary="单次调用：题面与输出全文")
def get_llm_call(call_id: str, store: RunStore = Depends(get_store)) -> dict:
    call = store.load_llm_call(call_id)
    if call is None:
        raise HTTPException(status_code=404, detail=f"找不到模型调用记录：{call_id}")
    return call
