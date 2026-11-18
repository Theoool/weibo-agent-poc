"""依赖注入：HTTP 层拿到的永远是 `RunStore`，拿不到 session。

不暴露 session 是有意的：一旦路由能拿到 session，就会有人在那里写
`session.add(...)`，只读层就名存实亡了。
"""

from __future__ import annotations

from fastapi import HTTPException, Request

from store import RunStore


def get_store(request: Request) -> RunStore:
    store = getattr(request.app.state, "store", None)
    if store is None:
        # lifespan 没跑（例如忘了 with TestClient(app)）时给一个能看懂的错
        raise HTTPException(status_code=503, detail="数据库未初始化（应用尚未启动）")
    return store


def require_run(store: RunStore, run_id: str) -> dict:
    summary = store.run_summary(run_id)
    if summary is None:
        raise HTTPException(status_code=404, detail=f"找不到 run：{run_id}")
    return summary
