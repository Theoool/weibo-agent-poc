"""M2 的验收：轨迹与模型调用文献可回溯。

要证明的只有四件事，少一件这套设计就不成立：

1. `llm_call` 里存的是**全文**，不是预览。短的内联、长的进内容寻址目录，
   读回来的字节与送出去的一致。
2. 一次调用失败也要留证据（错误原因 + 模型实际返回的那段脏文本）。
3. 数据库写挂了不能拖垮调查：好 sink 照写，坏 sink 的错误被记下来告警。
4. 领域层不认识数据库——`LLMClient` 只按协议往上抛，JSONL 里仍然只有摘要。

全部用临时目录，不碰 data/ 和 artifacts/。
"""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from agent.investigator import LLMClient
from agent.trace import FanoutSink, LLMCall, TraceSink, Tracer
from store import RunStore, open_db
from store.db_sink import DbSink

LONG_PROMPT = "第 3 条帖子：深圳 APEC 期间部分路段临时交通管制，" * 1200


class CaptureSink:
    """记录一切的内存 sink，用来观察领域层到底往外抛了什么。"""

    def __init__(self) -> None:
        self.records: list[dict] = []
        self.calls: list[LLMCall] = []

    def log(self, step: str, **fields) -> dict:
        rec = {"step": step, **fields}
        self.records.append(rec)
        return rec

    def record_call(self, call: LLMCall) -> None:
        self.calls.append(call)


class BrokenSink:
    def log(self, step: str, **fields) -> dict:
        raise RuntimeError("database is locked")


class TraceDbTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.engine, Session, _ = open_db(Path(self.tmp) / "test.db")
        # dispose 必须早于临时目录清理，否则 Windows 删不掉还被占用的 .db
        self.addCleanup(self.engine.dispose)
        self.store = RunStore(Session, blob_dir=Path(self.tmp) / "artifacts")
        self.run_id = self.store.create_run("APEC")
        self.stage_id = self.store.begin_stage(self.run_id, "investigate")

    def call(self, **over) -> LLMCall:
        base = dict(
            purpose="event_build",
            model="gpt-4.1-mini",
            base_url="",
            attempt=1,
            ok=True,
            duration_ms=1234,
            system="你是舆情分析助手",
            user="以下是 60 条帖子……",
            content='{"events":[{"name":"交通管制"}]}',
            error="",
            prompt_digest="deadbeef1234",
            created_at="2026-11-18T23:05:00",
        )
        base.update(over)
        return LLMCall(**base)

    # ------------------------------------------------------------------ 轨迹
    def test_trace_events_keep_order_and_payload(self) -> None:
        sink = DbSink(self.store, self.run_id, self.stage_id)
        sink.log("run_start", topic="APEC", logged_in=True)
        sink.log("round_stop", round=2, reason="证据已足")
        sink.log("events_built", count=3, names=["交通管制", "会场周边"])

        rows = self.store.load_trace(self.run_id)
        self.assertEqual([r["step"] for r in rows], ["run_start", "round_stop", "events_built"])
        self.assertEqual(rows[0]["topic"], "APEC")
        self.assertEqual(rows[1]["reason"], "证据已足")
        self.assertEqual(rows[2]["names"], ["交通管制", "会场周边"])
        # 顺序靠 seq，不靠自增 id 或插入顺序
        self.assertEqual(sink.seq, 3)

    def test_trace_filter_by_step(self) -> None:
        sink = DbSink(self.store, self.run_id, self.stage_id)
        for i in range(5):
            sink.log("llm_call", purpose=f"p{i}")
            sink.log("gap_detected", kind="NEED_EVIDENCE")
        self.assertEqual(len(self.store.load_trace(self.run_id, step="llm_call")), 5)
        self.assertEqual(len(self.store.load_trace(self.run_id, step="gap_detected")), 5)

    def test_trace_payload_survives_non_json_types(self) -> None:
        """轨迹字段来自领域层的任意对象，不该因为一个 datetime 整条写不进去。"""
        sink = DbSink(self.store, self.run_id, self.stage_id)
        sink.log("run_end", at=object(), count=2)
        rows = self.store.load_trace(self.run_id)
        self.assertEqual(rows[0]["count"], 2)
        self.assertIn("object", rows[0]["at"])  # default=str 的结果

    # -------------------------------------------------------------- 模型全文
    def test_short_call_is_inlined(self) -> None:
        call_id = self.store.save_llm_call(self.run_id, self.stage_id, self.call())
        row = self.store.load_llm_call(call_id)
        self.assertEqual(row["prompt"], "你是舆情分析助手\n\n以下是 60 条帖子……")
        self.assertEqual(row["response"], '{"events":[{"name":"交通管制"}]}')
        self.assertEqual(row["prompt_ref"], "")
        self.assertEqual(row["response_ref"], "")
        self.assertEqual(row["prompt_digest"], "deadbeef1234")
        self.assertTrue(row["ok"])
        # 与 JSONL 轨迹同名同形，查看器才能共用一套渲染
        self.assertEqual(row["response_preview"], row["response"][:300])

    def test_long_prompt_goes_to_cas_and_reads_back_verbatim(self) -> None:
        call_id = self.store.save_llm_call(
            self.run_id, self.stage_id, self.call(user=LONG_PROMPT, content="x" * 5000)
        )
        row = self.store.load_llm_call(call_id)
        self.assertTrue(row["prompt_ref"], "≥4KB 的题面应当写进内容寻址目录")
        self.assertTrue(row["response_ref"])
        self.assertEqual(row["prompt"], "你是舆情分析助手\n\n" + LONG_PROMPT)
        self.assertEqual(row["response"], "x" * 5000)
        self.assertEqual(row["request_chars"], len("你是舆情分析助手") + len(LONG_PROMPT))

    def test_failed_call_keeps_reason_and_dirty_output(self) -> None:
        call_id = self.store.save_llm_call(
            self.run_id,
            self.stage_id,
            self.call(
                ok=False,
                content="抱歉，我无法按 JSON 输出",
                error="返回内容无法解析为 JSON（前 120 字：'抱歉…'）",
            ),
        )
        row = self.store.load_llm_call(call_id)
        self.assertFalse(row["ok"])
        self.assertIn("无法解析", row["error"])
        self.assertEqual(row["response"], "抱歉，我无法按 JSON 输出")

    def test_llm_calls_listed_in_call_order(self) -> None:
        for i in range(3):
            self.store.save_llm_call(
                self.run_id, self.stage_id, self.call(purpose=f"step{i}", created_at=f"2026-11-18T23:0{i}:00")
            )
        rows = self.store.load_llm_calls(self.run_id)
        self.assertEqual([r["purpose"] for r in rows], ["step0", "step1", "step2"])

    # ------------------------------------------------------- 领域层 → 持久层
    def test_client_writes_full_text_only_to_full_text_sinks(self) -> None:
        cap = CaptureSink()
        client = LLMClient(api_key="")
        client.attach_tracer(cap)
        client._record(
            "event_build", 1, True, time.perf_counter(), "SYS", "USER" * 2000, '{"a":1}', ""
        )
        self.assertEqual(len(cap.calls), 1)
        self.assertEqual(cap.calls[0].user, "USER" * 2000)
        self.assertTrue(cap.calls[0].request_text.startswith("SYS\n\n"))
        # 轨迹里仍然只有摘要：300 字预览、无全文
        rec = cap.records[0]
        self.assertEqual(rec["step"], "llm_call")
        self.assertNotIn("prompt", rec)
        self.assertEqual(rec["response_preview"], '{"a":1}')

    def test_plain_jsonl_tracer_has_no_full_text_hook(self) -> None:
        """保持向后兼容：只有 JSONL 时，_record 不该因为找不到 record_call 报错。"""
        tracer = Tracer(None)
        self.assertIsInstance(tracer, TraceSink)
        self.assertFalse(hasattr(tracer, "record_call"))
        client = LLMClient(api_key="")
        client.attach_tracer(tracer)
        client._record("ping", 1, True, time.perf_counter(), "", "只回复 ok", "ok", "")
        self.assertEqual(len(tracer.records), 1)

    def test_fanout_end_to_end(self) -> None:
        """一条链路：LLMClient → FanoutSink → 文件 + 数据库。"""
        jsonl = Tracer(Path(self.tmp) / "trace.jsonl")
        db = DbSink(self.store, self.run_id, self.stage_id)
        client = LLMClient(api_key="")
        client.attach_tracer(FanoutSink(jsonl, db))
        client._record("keyword_plan_r1", 1, True, time.perf_counter(), "S", LONG_PROMPT, '{"keywords":[]}', "")

        self.assertEqual(db.calls, 1)
        self.assertTrue(jsonl.path.exists())
        rows = self.store.load_llm_calls(self.run_id)
        self.assertEqual(rows[0]["purpose"], "keyword_plan_r1")
        self.assertEqual(rows[0]["prompt"], "S\n\n" + LONG_PROMPT)
        trace = self.store.load_trace(self.run_id)
        self.assertEqual([r["step"] for r in trace], ["llm_call"])

    # ------------------------------------------------------------ 失败隔离
    def test_broken_sink_does_not_stop_the_good_one(self) -> None:
        good = Tracer(None)
        fan = FanoutSink(good, BrokenSink())
        rec = fan.log("round_stop", reason="证据已足")
        self.assertEqual(good.records[0], rec)
        self.assertEqual(fan.records[0], rec)
        self.assertEqual(fan.failures, 1)
        self.assertIn("database is locked", fan.errors[0])
        # 同一个坏 sink 连续报错不该把告警列表刷爆
        for _ in range(10):
            fan.log("noop")
        self.assertEqual(len(fan.errors), 1)
        self.assertEqual(fan.failures, 11)

    def test_fanout_records_calls_to_each_full_text_sink(self) -> None:
        a, b = CaptureSink(), CaptureSink()
        fan = FanoutSink(a, b)
        call = self.call()
        fan.record_call(call)
        self.assertEqual(a.calls, [call])
        self.assertEqual(b.calls, [call])


if __name__ == "__main__":
    unittest.main()
