"""报告产物：从数据库现渲染，不读 `output/`。

这是 M3 的一条硬约束：如果 API 直接吐文件，文件与库不一致时没人能发现。
现渲染的代价是要把 B 级数据拼回 `Investigation`，好处是每次请求都校验了一遍
"库里存的字段够不够渲染出一份完整报告"——不够就会在渲染时暴露，而不是等审计。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse, JSONResponse

from store import RunStore

from ..deps import get_store, require_run

router = APIRouter(prefix="/api/runs", tags=["reports"])


@router.get("/{run_id}/report", summary="报告产物（md 现渲染 / json 结构化）")
def get_report(
    run_id: str,
    format: str = Query("md", pattern="^(md|json)$"),
    store: RunStore = Depends(get_store),
):
    require_run(store, run_id)

    inv = store.load_investigation(run_id)
    if format == "json":
        if inv is None:
            raise HTTPException(
                status_code=404,
                detail="该 run 没有调查产物；核查结论请用 /api/runs/{run_id}/claims",
            )
        payload = inv.model_dump(mode="json")
        payload["_source"] = "rendered:database"
        return JSONResponse(payload)

    if inv is not None and inv.events:
        return PlainTextResponse(
            inv.to_markdown(),
            media_type="text/markdown; charset=utf-8",
            headers={"X-Report-Source": "rendered:investigation"},
        )

    # 只跑了核查的 run：md 是阶段结束时的渲染产物（B 级，按 run 存）
    md = store.read_artifact(run_id, "factcheck_md")
    if md:
        return PlainTextResponse(
            md,
            media_type="text/markdown; charset=utf-8",
            headers={"X-Report-Source": "artifact:factcheck_md"},
        )

    raise HTTPException(status_code=404, detail="该 run 没有可渲染的报告产物")
