"""FastAPI 只读层（M3）。

设计约束来自 `docs/architecture.md`：

- **只读**。这一层不改任何状态；创建 run 与执行阶段是 worker 的事（M4）。
- **现渲染，不读文件**。报告从数据库（B 级）重新算出来，`output/` 只是导出视图。
  这样"API 说 A、文件说 B"这类问题不可能出现。
- **异步只体现在客户端**。查询都是短事务，同步 SQLAlchemy 足够；真正的长活
  （浏览器采集）不在这个进程里。

启动：

    uv run python -m api --port 8000

或

    uv run uvicorn api.app:app --port 8000
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from store import open_store

from .routers import documents, health, reports, runs, trace

DESCRIPTION = """
微博舆情调查 Agent 的只读查询接口。

能回答的问题：
- 这次运行跑了哪些阶段、为什么停（`/runs/{id}`）
- 这份核查报告核的是哪一次调查（`/runs/{id}` 的 `provenance`）
- 每条结论引了哪几个来源、对应哪条帖子原文（`/runs/{id}/claims`）
- 某条帖子被抓过几次、内容改没改（`/documents/{id}/fetches`）
- 模型每次调用看到了什么、答了什么（`/runs/{id}/llm-calls`、`/llm-calls/{id}`）
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
        version="0.3.0",
        description=DESCRIPTION,
        lifespan=lifespan,
    )
    app.state.db_path = db_path or os.getenv("WEIBO_DB_PATH") or None
    app.state.blob_dir = blob_dir or os.getenv("WEIBO_ARTIFACTS_DIR") or None
    app.state.migrations = []
    for module in (health, runs, documents, reports, trace):
        app.include_router(module.router)
    return app


app = create_app()
