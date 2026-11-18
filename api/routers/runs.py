"""run 维度的查询：列表、概览、阶段、缺口、结论。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from store import RunStore

from ..deps import get_store, require_run

router = APIRouter(prefix="/api/runs", tags=["runs"])


@router.get("", summary="run 列表")
def list_runs(
    limit: int = Query(20, ge=1, le=200),
    offset: int = Query(0, ge=0),
    topic: str | None = Query(None, description="按主题模糊过滤"),
    store: RunStore = Depends(get_store),
) -> dict:
    items = store.list_runs(limit=limit, offset=offset, topic=topic)
    return {"count": len(items), "limit": limit, "offset": offset, "items": items}


@router.get("/{run_id}", summary="run 概览 + 来源（这份核查核的是哪次调查）")
def get_run(run_id: str, store: RunStore = Depends(get_store)) -> dict:
    summary = require_run(store, run_id)
    summary["provenance"] = store.run_provenance(run_id)
    return summary


@router.get("/{run_id}/stages", summary="阶段状态与耗时")
def get_stages(run_id: str, store: RunStore = Depends(get_store)) -> dict:
    summary = require_run(store, run_id)
    return {"run_id": run_id, "stages": summary["stages"]}


@router.get("/{run_id}/gaps", summary="缺口决策链（为什么继续搜、为什么停）")
def get_gaps(run_id: str, store: RunStore = Depends(get_store)) -> dict:
    require_run(store, run_id)
    gaps = store.run_gaps(run_id)
    counts = {"resolved": 0, "unresolved": 0, "unknown": 0}
    for g in gaps:
        key = "unknown" if g["resolved"] is None else ("resolved" if g["resolved"] else "unresolved")
        counts[key] += 1
    return {"run_id": run_id, "count": len(gaps), "summary": counts, "items": gaps}


@router.get("/{run_id}/claims", summary="核查结论及其引用的来源（含原文快照 id）")
def get_claims(run_id: str, store: RunStore = Depends(get_store)) -> dict:
    require_run(store, run_id)
    claims = store.run_claims(run_id)
    if not claims:
        raise HTTPException(
            status_code=404,
            detail="该 run 没有核查结论（可能只跑了调查阶段，或 handoff 还没转成核查）",
        )
    linked = sum(1 for c in claims for s in c["sources"] if s.get("document_id"))
    return {
        "run_id": run_id,
        "count": len(claims),
        "linked_sources": linked,
        "note": (
            ""
            if linked
            else "这批结论没有任何一条能链到原文快照（历史导入的核查没有存文档），"
            "只能看到当时引用的 quote 片段。"
        ),
        "items": claims,
    }
