"""运行轨迹：把每一步决策落成 JSONL，事后可回放。

为什么需要：工作流一旦从"固定管道"变成"多轮判断"，同一份输入跑两次
结果可能不同。没有 trace 就只能猜差异出在哪一轮。这也是唯一能证明
"Agent 真的做了什么"的东西——日志只给人看，trace 给程序和人都能看。
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any


class Tracer:
    """极简 JSONL tracer。同时留内存副本，便于收尾时统计。"""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else None
        self.records: list[dict[str, Any]] = []
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # 每次运行清空，避免多次运行的 trace 混在同一个文件里
            self.path.write_text("", encoding="utf-8")

    def log(self, step: str, **fields: Any) -> dict[str, Any]:
        rec: dict[str, Any] = {
            "ts": datetime.now().isoformat(timespec="milliseconds"),
            "step": step,
            **fields,
        }
        self.records.append(rec)
        if self.path is not None:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return rec

    def counts(self) -> dict[str, int]:
        return dict(Counter(r["step"] for r in self.records))


def load(path: str | Path) -> list[dict[str, Any]]:
    """读回 JSONL。

    坏行直接跳过：trace 是用来排查问题的，不该因为最后一行被中断就整份读不出来。
    """
    p = Path(path)
    if not p.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    """把 trace 压成"这次跑得怎么样"。

    重点是把模型调用单独拎出来：失败的可重试、可复现，才是能动手修的信息。
    """
    calls = [r for r in records if r.get("step") == "llm_call"]
    return {
        "total": len(records),
        "counts": dict(Counter(r.get("step", "?") for r in records)),
        "llm_calls": len(calls),
        "llm_ok": sum(1 for r in calls if r.get("ok") is True),
        "llm_failed": sum(1 for r in calls if r.get("ok") is False),
        "llm_ms": sum(int(r.get("duration_ms") or 0) for r in calls),
        "llm_purposes": dict(Counter(r.get("purpose", "?") for r in calls)),
        "errors": [
            {
                "step": r.get("step"),
                "purpose": r.get("purpose", ""),
                "error": r.get("error", ""),
            }
            for r in records
            if r.get("ok") is False or r.get("error")
        ],
    }
