"""持久层入口。

用法：

    from store import open_store
    store, applied = open_store("data/workflow.db")
    run_id = store.create_run("APEC", config={...})

`open_store` 会顺手跑迁移，所以调用方不需要关心建表。
"""

from __future__ import annotations

from pathlib import Path

from .db import DEFAULT_DB_PATH, create_db_engine, create_session_factory, migrate, open_db
from .repositories import RunStore, app_version, canonical_url, name_key, now_iso

__all__ = [
    "DEFAULT_DB_PATH",
    "RunStore",
    "app_version",
    "canonical_url",
    "create_db_engine",
    "create_session_factory",
    "migrate",
    "name_key",
    "now_iso",
    "open_db",
    "open_store",
]


def open_store(
    db_path: str | Path | None = None, blob_dir: str | Path | None = None
) -> tuple[RunStore, list[int]]:
    """建库并返回 (RunStore, 本次应用的迁移版本)。

    `blob_dir` 可指定内容寻址目录，测试与 API 用它把数据留在临时目录里。
    """
    engine, session_factory, applied = open_db(db_path or DEFAULT_DB_PATH)
    return RunStore(session_factory, blob_dir=blob_dir), applied
