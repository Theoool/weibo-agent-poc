"""存活与依赖检查。

`/health` 只回答"进程还在"，供容器探针用；`/ready` 才检查依赖。
两者分开是因为依赖挂掉时**不该重启进程**——重启修不好没有 Key 的模型账户，
但会让能用的查询也一起不可用。
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter, Depends
from sqlalchemy import text
from store import RunStore
from store.db import PROJECT_ROOT

from ..deps import get_store

router = APIRouter(prefix="/api", tags=["health"])

WORKER_HINT = "uv run python -m worker"

#: 采集用的专用 Chrome profile（`browser/chrome.py` 的默认值，这里不 import 它，
#: 免得为了一个健康检查把 playwright 拉进 API 进程）
CHROME_PROFILE = PROJECT_ROOT / ".chrome-profile"


@router.get("/health", summary="进程存活")
def health() -> dict:
    return {"status": "ok"}


@router.get("/ready", summary="依赖检查（数据库 / 存储 / 模型 Key / 登录态目录）")
def ready(store: RunStore = Depends(get_store)) -> dict:
    checks: dict[str, dict] = {}

    try:
        with store.Session() as s:
            s.execute(text("SELECT 1"))
        checks["database"] = {"ok": True, "detail": str(store.engine.url)}
    except Exception as exc:  # noqa: BLE001
        checks["database"] = {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}

    try:
        root: Path = store.blobs.root
        root.mkdir(parents=True, exist_ok=True)
        stats = store.blobs.stats()
        checks["artifacts"] = {
            "ok": True,
            "detail": f"{stats['blobs']} 个文件 / {stats['bytes']} 字节｜{root}",
        }
    except Exception as exc:  # noqa: BLE001
        checks["artifacts"] = {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}

    key = os.getenv("OPENAI_API_KEY") or ""
    checks["model"] = {
        "ok": bool(key),
        # 只报有没有，不回显 Key 本身
        "detail": f"{os.getenv('OPENAI_MODEL') or 'gpt-4.1-mini'}"
        + ("" if key else "｜未配置 OPENAI_API_KEY，只能查历史数据"),
    }

    checks["chrome_profile"] = {
        "ok": CHROME_PROFILE.exists(),
        "detail": str(CHROME_PROFILE)
        + ("" if CHROME_PROFILE.exists() else "｜不存在，采集阶段需先登录一次"),
    }

    # worker 不在线不算 not ready：查询历史数据完全不受影响，
    # 但新任务会一直排队，所以必须单独报出来
    queue = store.queue_overview()
    checks["worker"] = {
        "ok": queue["worker_alive"] or queue["counts"]["queued"] == 0,
        "detail": (
            f"{len(queue['workers'])} 个在线｜排队 {queue['counts']['queued']}"
            f"｜运行 {queue['counts']['running']}"
            + (
                ""
                if queue["worker_alive"] or queue["counts"]["queued"] == 0
                else f"｜有任务在排队但没有 worker：{WORKER_HINT}"
            )
        ),
    }

    # 模型 Key 与 Chrome 属于"采集能力"，缺了不影响查历史，所以不算 not ready
    blocking = [k for k in ("database", "artifacts") if not checks[k]["ok"]]
    return {
        "status": "ok" if not blocking else "degraded",
        "blocking": blocking,
        "checks": checks,
    }
