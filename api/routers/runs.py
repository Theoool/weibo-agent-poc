"""run 维度的查询与写操作：列表、概览、阶段、缺口、结论、创建/重跑/取消。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status

from store import RunStore

from ..deps import get_store, require_run
from ..schemas import RetryRequest, RunCreate

router = APIRouter(prefix="/api/runs", tags=["runs"])

WORKER_HINT = "没有检测到在线的 worker，任务会一直排队。启动：uv run python -m worker"


def _worker_note(overview: dict) -> str:
    return "" if overview.get("worker_alive") else WORKER_HINT


def _latest_failure(summary: dict) -> str | None:
    """最近一个失败或取消的阶段。重跑默认就补这一步，不用客户端自己判断。"""
    for st in reversed(summary.get("stages") or []):
        if st["status"] in ("failed", "cancelled"):
            return st["stage"]
    return None


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
    summary["queue"] = store.run_queue(run_id)
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


# --------------------------------------------------------------------------- #
# 写：创建 / 重跑 / 取消
# --------------------------------------------------------------------------- #
@router.post(
    "",
    status_code=status.HTTP_202_ACCEPTED,
    summary="创建并排队一次工作流（立即返回，用 GET 轮询）",
)
def create_run(payload: RunCreate, store: RunStore = Depends(get_store)) -> dict:
    """建 run + 入队，立刻返回 202。

    不在这里跑调查是有原因的：一次调查 3–6 分钟，同步 HTTP 早超时了，
    客户端一重试就会造出第二个 run。这里只做两件原子性够用的事——
    建 run、入队，然后让客户端拿 `links.self` 去轮询。
    """
    stages = list(payload.stages)
    run_id = store.create_run(
        payload.topic,
        trigger="api",
        status="queued",
        config={
            "topic": payload.topic,
            "seed_keywords": payload.seed_keywords,
            "stages": stages,
            "options": payload.options.model_dump(),
            "requested_via": "POST /api/runs",
        },
    )
    job = store.enqueue(
        run_id,
        stages[0],
        priority=payload.priority,
        payload={
            "topic": payload.topic,
            "seed_keywords": payload.seed_keywords,
            "plan": stages,
            "index": 0,
            "options": payload.options.model_dump(),
        },
    )
    overview = store.queue_overview()
    return {
        "run_id": run_id,
        "job_id": job["id"],
        "status": "queued",
        "stages": stages,
        "queue": {
            "position": job["ahead"] + 1,
            "ahead": job["ahead"],
            "worker_alive": overview["worker_alive"],
        },
        "links": {
            "self": f"/api/runs/{run_id}",
            "stages": f"/api/runs/{run_id}/stages",
            "report": f"/api/runs/{run_id}/report?format=md",
            "queue": "/api/queue",
        },
        "note": _worker_note(overview),
    }


@router.post(
    "/{run_id}/retry",
    status_code=status.HTTP_202_ACCEPTED,
    summary="从失败阶段重跑（新开一次 attempt，旧的那次保留）",
)
def retry_run(
    run_id: str,
    body: RetryRequest | None = None,
    store: RunStore = Depends(get_store),
) -> dict:
    summary = require_run(store, run_id)
    if summary["status"] in ("queued", "running"):
        raise HTTPException(
            status_code=409,
            detail=f"这次运行还在{('排队' if summary['status'] == 'queued' else '执行')}中，不能重跑",
        )

    body = body or RetryRequest()
    stage = body.stage or _latest_failure(summary)
    if stage is None:
        raise HTTPException(
            status_code=409,
            detail="没有失败或取消的阶段可重跑。要整段重来请在请求体里显式指定 stage。",
        )

    # 沿用上一次任务的参数（预算、topic、seed_keywords），否则重跑会用默认值，
    # 与第一次跑的预算不一致，结果也没法对比
    last = store.job_of_run(run_id) or {}
    base = dict(last.get("payload") or {})
    config = summary.get("config") or {}
    base.setdefault("topic", summary["topic"])
    base.setdefault("seed_keywords", config.get("seed_keywords") or [])
    base.setdefault("options", config.get("options") or {})
    payload = {**base, "plan": [stage], "index": 0}

    job = store.enqueue(run_id, stage, priority=body.priority, payload=payload)
    overview = store.queue_overview()
    return {
        "run_id": run_id,
        "job_id": job["id"],
        "stage": stage,
        "status": "queued",
        "attempts_so_far": job["attempts"],
        "queue": {"position": job["ahead"] + 1, "ahead": job["ahead"],
                  "worker_alive": overview["worker_alive"]},
        "note": _worker_note(overview),
    }


@router.post(
    "/{run_id}/cancel",
    status_code=status.HTTP_202_ACCEPTED,
    summary="取消：排队中的立即作废，运行中的在轮次边界停",
)
def cancel_run(run_id: str, store: RunStore = Depends(get_store)) -> dict:
    require_run(store, run_id)
    result = store.request_cancel(run_id)
    if not result["cancelled"]:
        raise HTTPException(status_code=409, detail=result["state"])
    return {"run_id": run_id, **result}
