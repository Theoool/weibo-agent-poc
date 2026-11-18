"""FastAPI 层：查询（M3）+ 创建/重跑/取消（M4）。

设计约束来自 `docs/architecture.md`：

- **进程内不跑长任务。** 采集要 Playwright 与专用 Chrome profile，放进这个进程会让
  `--reload` 杀掉任务、进程重启丢任务，还会阻塞事件循环。写接口只做"建 run + 入队"。
- **现渲染，不读文件。** 报告从数据库（B 级）重新算出来，`output/` 只是导出视图。
- **路由拿不到 session。** 只能通过 `RunStore` 改状态，写路径收敛在一个地方。

启动：

    uv run python -m api --port 8000      # 查询与写接口
    uv run python -m worker               # 另起一个进程消费队列
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from store import open_store

from .routers import documents, health, queue, reports, runs, trace

DESCRIPTION = """
微博舆情调查 Agent 的 HTTP 接口。

查询能回答：
- 这次运行跑了哪些阶段、为什么停（`/runs/{id}`）
- 这份核查报告核的是哪一次调查（`/runs/{id}` 的 `provenance`）
- 每条结论引了哪几个来源、对应哪条帖子原文（`/runs/{id}/claims`）
- 某条帖子被抓过几次、内容改没改（`/documents/{id}/fetches`）
- 模型每次调用看到了什么、答了什么（`/runs/{id}/llm-calls`、`/llm-calls/{id}`）

写操作（M4，202 + 轮询）：
- `POST /runs` 创建并排队，`GET /runs/{id}` 轮询到 `succeeded`
- `POST /runs/{id}/retry` 从失败阶段重跑，`POST /runs/{id}/cancel` 取消
- `GET /queue` 看积压、在线的 worker、最近任务

**写接口不会自己跑调查**：需要另起 `uv run python -m worker` 消费队列。
""".strip()


@asynccontextmanager
async def lifespan(app: FastAPI):
    store, applied = open_store(app.state.db_path, app.state.blob_dir)
    app.state.store = store
    app.state.migrations = applied
    try:
        yield
    finally:
        # 不 dispose 的话，Windows 上进程退出后 .db 还被占着，测试也删不掉临时目录
        store.close()


def create_app(db_path: str | Path | None = None, blob_dir: str | Path | None = None) -> FastAPI:
    """app 工厂。测试用 `create_app(tmp_path)` 就能指向临时库，不碰 data/。

    也认环境变量 `WEIBO_DB_PATH`：`uvicorn --reload` 下没法传 app 实例，
    只能靠环境变量把库路径带进子进程。
    """
    app = FastAPI(
        title="微博舆情调查 Agent API",
        version="0.4.0",
        description=DESCRIPTION,
        lifespan=lifespan,
    )
    app.state.db_path = db_path or os.getenv("WEIBO_DB_PATH") or None
    app.state.blob_dir = blob_dir or os.getenv("WEIBO_ARTIFACTS_DIR") or None
    app.state.migrations = []
    for module in (health, runs, queue, documents, reports, trace):
        app.include_router(module.router)
    return app


app = create_app()
