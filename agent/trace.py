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
