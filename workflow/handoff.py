"""handoff 阶段：把调查产物转成核查待办。

在队列里，这一步的输入**从数据库读**（`load_investigation`），不读文件。
文件是导出视图，可能被下一次运行覆盖、也可能被人手改；库里那份带 stage_run_id，
才是"这一次调查"唯一的权威来源。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from agent.handoff import facts_to_claims
from agent.schemas import Investigation

from .context import RunContext


@dataclass
class HandoffOutcome:
    payload: dict
    path: Path
    source: str  # db | file


def execute_handoff(
    ctx: RunContext,
    *,
    investigation: Investigation | None = None,
    only_events: list[int] | None = None,
) -> HandoffOutcome:
    from_db = investigation is None
    if investigation is None:
        if ctx.store is None:
            raise RuntimeError("没有数据库时必须在参数里直接传入调查产物")
        investigation = ctx.store.load_investigation(ctx.run_id)
    if investigation is None:
        raise RuntimeError(
            "这个 run 在库里没有调查产物，无法生成核查清单（调查阶段可能没成功）"
        )

    out_dir = ctx.mkdir()
    payload = facts_to_claims(investigation, only_events=only_events)
    path = out_dir / "claims_handoff.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    if ctx.store is not None and ctx.run_id:
        ctx.store.save_artifact(
            ctx.run_id, ctx.stage_id, "claims_json", path.read_text(encoding="utf-8")
        )
    return HandoffOutcome(payload=payload, path=path, source="db" if from_db else "file")
