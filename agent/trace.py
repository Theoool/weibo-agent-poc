"""运行轨迹：把每一步决策留成可回放的记录。

为什么需要：工作流一旦从"固定管道"变成"多轮判断"，同一份输入跑两次
结果可能不同。没有 trace 就只能猜差异出在哪一轮。这也是唯一能证明
"Agent 真的做了什么"的东西——日志只给人看，trace 给程序和人都能看。

两个抽象：

- `TraceSink`：`log(step, **fields)` 的最小协议。JSONL 文件、数据库行、内存
  列表都实现它，领域层只认这个协议，所以 `investigator.py` 不需要知道数据库存在。
- `LLMCall`：一次模型调用的**全文**。轨迹里只留摘要（摘要够判断"输入变没变"），
  全文走单独的 `record_call`——因为只有全文才能回答"模型当时到底看到了什么、
  答了什么"，而摘要只能回答"还是不是同一次调用"。

由此得到的分工：JSONL 继续保持轻量（可提交、可 diff），数据库侧保留全文
（可复现、可审计）。两边记录的是同一件事，只是粒度不同。
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable


def _now() -> str:
    return datetime.now().isoformat(timespec="milliseconds")


# --------------------------------------------------------------------------- #
# 协议
# --------------------------------------------------------------------------- #
@runtime_checkable
class TraceSink(Protocol):
    """最小的轨迹写入协议。返回值是该条记录的字典形式（调用方可能要回读）。"""

    def log(self, step: str, **fields: Any) -> dict[str, Any]: ...


@runtime_checkable
class FullTextSink(Protocol):
    """愿意接收模型调用全文的 sink。

    单独一个协议，是因为不是所有 sink 都该拿到全文：JSONL 落全文会让轨迹文件
    从几十 KB 涨到几 MB，而它本来只用来回答"哪一轮、花了多久、成没成"。
    """

    def record_call(self, call: LLMCall) -> None: ...


# --------------------------------------------------------------------------- #
# 模型调用（全文）
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class LLMCall:
    """一次模型调用的完整现场：题面、原始输出、耗时、失败原因。

    `prompt_digest` 只证明输入没变，不能证明当时输出了什么；要复现必须存全文。
    """

    purpose: str
    model: str
    base_url: str
    attempt: int
    ok: bool
    duration_ms: int
    system: str
    user: str
    content: str
    error: str
    prompt_digest: str
    created_at: str = ""

    @property
    def request_text(self) -> str:
        """送出去的题面。system 与 user 分开存，读回时按同一个拼法还原。"""
        if not self.system:
            return self.user
        return f"{self.system}\n\n{self.user}"

    @property
    def request_chars(self) -> int:
        return len(self.system) + len(self.user)

    def to_record(self) -> dict[str, Any]:
        """轨迹里的摘要形态。字段与 `--trace` 查看器、现有 JSONL 完全一致。"""
        rec: dict[str, Any] = {
            "purpose": self.purpose or "unspecified",
            "model": self.model,
            "attempt": self.attempt,
            "ok": self.ok,
            "duration_ms": self.duration_ms,
            "request_chars": self.request_chars,
            "response_chars": len(self.content or ""),
            "prompt_digest": self.prompt_digest,
            "response_preview": (self.content or "")[:300],
        }
        if not self.ok:
            rec["error"] = self.error
            # 失败时把题面也留一份，否则无法复现"模型到底看到了什么"
            rec["prompt_preview"] = self.user[:800]
        return rec


# --------------------------------------------------------------------------- #
# sink 实现
# --------------------------------------------------------------------------- #
class Tracer:
    """极简 JSONL tracer。同时留内存副本，便于收尾时统计。

    不实现 `record_call`：轨迹文件刻意不存全文，理由见模块头。
    """

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else None
        self.records: list[dict[str, Any]] = []
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # 每次运行清空，避免多次运行的 trace 混在同一个文件里
            self.path.write_text("", encoding="utf-8")

    def log(self, step: str, **fields: Any) -> dict[str, Any]:
        rec: dict[str, Any] = {"ts": _now(), "step": step, **fields}
        self.records.append(rec)
        if self.path is not None:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        return rec

    def counts(self) -> dict[str, int]:
        return dict(Counter(r["step"] for r in self.records))


#: 语义化别名：`Tracer` 就是"写 JSONL 的那个 sink"，新代码用这个名字更清楚
JsonlSink = Tracer


class FanoutSink:
    """把一个事件分发给多个 sink。

    两条必须遵守的规则：
    1. **一个 sink 挂了不能中断调查。** trace 是观测手段，不是业务前提；
       数据库不可用时调查还得跑完，文件产物还在。
    2. **但也不能静默。** 错误收进 `errors`，由调用方在收尾时一次性告警。
       悄悄丢轨迹比没有轨迹更糟：事后会以为"这一段本来就没有记录"。

    `records` 由它自己维护，因为 `main.py` 用它报告"本次产出多少条轨迹"；
    JSONL sink 自己那份只是它自己的。
    """

    def __init__(self, *sinks: TraceSink | None) -> None:
        self.sinks: list[TraceSink] = [s for s in sinks if s is not None]
        self.records: list[dict[str, Any]] = []
        self.errors: list[str] = []
        self.failures = 0

    def log(self, step: str, **fields: Any) -> dict[str, Any]:
        rec: dict[str, Any] | None = None
        for sink in self.sinks:
            try:
                out = sink.log(step, **fields)
            except Exception as exc:  # noqa: BLE001
                self._fail(sink, exc)
                continue
            if rec is None and isinstance(out, dict):
                rec = out
        if rec is None:
            rec = {"ts": _now(), "step": step, **fields}
        self.records.append(rec)
        return rec

    def record_call(self, call: LLMCall) -> None:
        for sink in self.sinks:
            fn = getattr(sink, "record_call", None)
            if not callable(fn):
                continue
            try:
                fn(call)
            except Exception as exc:  # noqa: BLE001
                self._fail(sink, exc)

    def counts(self) -> dict[str, int]:
        return dict(Counter(r["step"] for r in self.records))

    def _fail(self, sink: TraceSink, exc: Exception) -> None:
        self.failures += 1
        msg = f"{type(sink).__name__}: {type(exc).__name__}: {exc}"
        # 同一个坏 sink 会在每次 log 时都炸，只记前 5 条不同原因即可
        if msg not in self.errors and len(self.errors) < 5:
            self.errors.append(msg)


# --------------------------------------------------------------------------- #
# 读回
# --------------------------------------------------------------------------- #
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
