"""轨迹出口的组装。

`TraceSink` 有两个实现：JSONL（人看、可 diff、可提交）和数据库（SQL 可查）。
两条都写是有意的——文件负责"这次运行发生了什么"的线性叙事，数据库负责
"把某次调用的题面调出来"的定点查询。少一个都做不到。
"""

from __future__ import annotations

from pathlib import Path

from agent.trace import FanoutSink, TraceSink, Tracer
from store.db_sink import DbSink
from store.repositories import RunStore


def build_sinks(
    trace_path: str | Path | None, store: RunStore | None, run_id: str, stage_id: str
) -> tuple[TraceSink, DbSink | None]:
    """返回 (tracer, db_sink)。没有数据库时 db_sink 为 None。"""
    jsonl = Tracer(trace_path)
    if store is None or not run_id:
        return jsonl, None
    db_sink = DbSink(store, run_id, stage_id)
    return FanoutSink(jsonl, db_sink), db_sink
