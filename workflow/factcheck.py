"""核查阶段的执行器。

输入优先取 `claims_json` 这个 artifact：队列里跑的时候没有"某个固定文件路径"
可以依赖，而 artifact 是 handoff 阶段刚刚写下的那一份，不会串档。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from factcheck.runner import run_factcheck

from .context import RunContext


@dataclass
class FactcheckOutcome:
    report: object
    json_path: Path
    md_path: Path
    stats: dict = field(default_factory=dict)


def execute_factcheck(
    ctx: RunContext,
    *,
    claims_path: str | Path | None = None,
    do_check: bool = True,
    llm=None,
    semantic: bool | None = None,
    semantic_limit: int = 20,
    update_topic: bool = False,
) -> FactcheckOutcome:
    out_dir = ctx.mkdir()
    if claims_path is None:
        if ctx.store is None:
            raise RuntimeError("没有数据库时必须用 --factcheck 指定一份核查清单")
        raw = ctx.store.read_artifact(ctx.run_id, "claims_json")
        if not raw:
            raise RuntimeError(
                "这个 run 没有核查清单（claims_json）：先把 handoff 阶段跑成功，"
                "或直接用 --factcheck 指定一份清单"
            )
        claims_path = out_dir / "claims_handoff.json"
        claims_path.write_text(raw, encoding="utf-8")

    report, json_path, md_path = run_factcheck(
        claims_path,
        out_dir,
        do_check=do_check,
        log=ctx.log,
        llm=llm,
        semantic=semantic,
        semantic_limit=semantic_limit,
    )

    stats: dict = {}
    if ctx.store is not None and ctx.run_id:
        stats = ctx.store.save_factcheck(
            report,
            ctx.run_id,
            ctx.stage_id,
            checked=do_check,
            artifacts={"factcheck_md": Path(md_path).read_text(encoding="utf-8")},
        )
        # 只有"为一个独立核查清单新建的 run"才该用报告标题覆盖主题：
        # 挂在调查 run 上时，主题是调查主题，比"XX 事实核查"更有信息量
        if update_topic:
            ctx.store.set_run_topic(ctx.run_id, report.title or ctx.store.run_summary(ctx.run_id)["topic"])
    return FactcheckOutcome(
        report=report, json_path=Path(json_path), md_path=Path(md_path), stats=stats
    )
