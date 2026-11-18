"""调查阶段的执行器：命令行与 worker 共用这一份。

这里做的事只有"组装 + 落库"，判断逻辑全在 `agent/`。之所以单独一层，
是因为 `agent/` 不许 import `store/`——领域层一旦认识数据库，单测就得起库，
"读回能逐字渲染同一份报告"这条硬标准也就没法在纯内存里验。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from agent.investigator import Budget, Investigator, write_outputs
from agent.schemas import Investigation

from .context import RunContext
from .sinks import build_sinks


@dataclass
class InvestigateOutcome:
    investigation: Investigation
    json_path: Path
    md_path: Path
    trace_path: Path | None
    store_stats: dict = field(default_factory=dict)
    llm_stats: dict = field(default_factory=dict)
    tracer_errors: list[str] = field(default_factory=list)
    trace_events: int = 0

    @property
    def llm_calls(self) -> int:
        return int(self.llm_stats.get("llm_calls", 0))

    @property
    def llm_failures(self) -> int:
        return int(self.llm_stats.get("llm_failures", 0))

    @property
    def llm_ms(self) -> int:
        return int(self.llm_stats.get("llm_ms", 0))


def execute_investigate(
    ctx: RunContext,
    session,
    *,
    topic: str,
    seed_keywords: list[str] | None = None,
    budget: Budget | None = None,
    llm=None,
    logged_in: bool = False,
) -> InvestigateOutcome:
    """跑一次调查，写文件产物，并把语义层整份存进数据库。

    `session` 是 Playwright 会话。由调用方持有并负责关闭——worker 在同一个
    浏览器里可以连续跑几条任务，这里关掉会把登录态之外的缓存也一起丢掉。
    """
    out_dir = ctx.mkdir()
    trace_path = out_dir / f"trace_{datetime.now():%Y%m%d_%H%M%S}.jsonl"
    tracer, db_sink = build_sinks(trace_path, ctx.store, ctx.run_id, ctx.stage_id)

    investigator = Investigator(
        session=session,
        topic=topic,
        seed_keywords=seed_keywords or [],
        budget=budget or Budget(),
        llm=llm,
        logged_in=logged_in,
        tracer=tracer,
        log=ctx.log,
        should_stop=ctx.should_stop,
    )
    inv = investigator.run()
    # run_id 要进产物：下游 handoff / factcheck 靠它挂回同一条链
    inv.run_id = ctx.run_id

    json_path, md_path = write_outputs(inv, out_dir)

    stats: dict = {}
    if ctx.store is not None and ctx.run_id:
        # posts 必须一起传：Investigation 里的证据正文是截断摘录，
        # 只靠它入库会把最该留住的全文快照永久丢掉
        stats = ctx.store.save_investigation(
            inv, ctx.run_id, ctx.stage_id, posts=investigator.posts
        )
        ctx.store.save_artifact(ctx.run_id, ctx.stage_id, "report_md", inv.to_markdown())

    return InvestigateOutcome(
        investigation=inv,
        json_path=json_path,
        md_path=md_path,
        trace_path=trace_path if trace_path.exists() else None,
        store_stats=stats,
        llm_stats=investigator.llm.stats(),
        tracer_errors=list(getattr(tracer, "errors", []) or []),
        trace_events=len(getattr(tracer, "records", []) or []),
    )
