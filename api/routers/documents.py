"""证据文档：全文快照与抓取历史。

两个端点回答的是同一个问题的两半：
- `/documents/{id}`：这条帖子当时抓到的原文是什么（A 级数据）；
- `/documents/{id}/fetches`：它被抓过几次、内容改没改（同 URL 多版本）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from store import RunStore

from ..deps import get_store, require_run

router = APIRouter(tags=["documents"])


@router.get("/api/runs/{run_id}/documents", summary="某次运行抓到的证据文档（不含全文）")
def list_documents(
    run_id: str,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    store: RunStore = Depends(get_store),
) -> dict:
    require_run(store, run_id)
    page = store.list_documents(run_id, limit=limit, offset=offset)
    return {"run_id": run_id, **page}


@router.get("/api/documents/{doc_id}", summary="证据快照全文")
def get_document(doc_id: str, store: RunStore = Depends(get_store)) -> dict:
    doc = store.get_document(doc_id)
    if doc is None:
        raise HTTPException(status_code=404, detail=f"找不到文档：{doc_id}")
    return doc


@router.get(
    "/api/documents/{doc_id}/fetches",
    summary="同一 URL 的抓取历史与版本差异（内容变没变）",
)
def get_document_fetches(doc_id: str, store: RunStore = Depends(get_store)) -> dict:
    history = store.document_history(doc_id)
    if history is None:
        raise HTTPException(status_code=404, detail=f"找不到文档：{doc_id}")
    return history
