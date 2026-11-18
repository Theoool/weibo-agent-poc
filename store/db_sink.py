"""把轨迹写进数据库的 sink。

存在意义来自一条硬要求：**报告发出去之后，必须能回答"当时模型看到了什么"**。
JSONL 轨迹只留摘要（purpose / digest / 耗时 / 300 字预览），够判断"是不是同一次
调用"，不够复现一次失败或一次误判。全文必须另有去处。

实现上有两个刻意的选择：

1. **逐条提交，不攒批。** A 级数据要求"发生即写"。攒到阶段结束再写看起来更快，
   但一旦进程被杀（Chrome 崩、Ctrl-C），攒着的那批就永久丢了——而丢的恰好是
   出事前最后几步，正是最想看的几步。
2. **自己不吞异常。** 数据库写失败时直接抛给 `FanoutSink`，由它计数并告警。
   如果这里静默 `pass`，事后会出现"这段本来就没有记录"的错觉。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from agent.trace import LLMCall as LLMCallRecord

from .repositories import RunStore


class DbSink:
    """`TraceSink` + `FullTextSink` 的数据库实现。

    `run_id` / `stage_run_id` 在构造时绑定：一条轨迹属于哪次运行、哪个阶段，
    是它最重要的属性，不该由每条记录自己声明（会写错，而且重复）。
    """

    def __init__(self, store: RunStore, run_id: str, stage_run_id: str | None = None) -> None:
        self.store = store
        self.run_id = run_id
        self.stage_run_id = stage_run_id
        self.seq = 0
        self.calls = 0

    def log(self, step: str, **fields: Any) -> dict[str, Any]:
        self.seq += 1
        ts = datetime.now().isoformat(timespec="milliseconds")
        self.store.save_trace_event(
            self.run_id,
            self.stage_run_id,
            seq=self.seq,
            ts=ts,
            step=step,
            payload=fields,
        )
        return {"ts": ts, "step": step, **fields}

    def record_call(self, call: LLMCallRecord) -> None:
        self.calls += 1
        self.store.save_llm_call(self.run_id, self.stage_run_id, call)

    def stats(self) -> dict[str, int]:
        return {"trace_events": self.seq, "llm_calls": self.calls}
