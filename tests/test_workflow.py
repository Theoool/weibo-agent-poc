"""工作流层单测：事件合并、Fact 兼容、handoff 抽取、trace 落盘。

这些规则被改坏时的症状很隐蔽，所以必须锁住：
- 合并阈值太严 → 同一事件被拆成两个（违反原始需求）；
- 合并阈值太松 → 不相关事件被并成一个（比不合并更糟）；
- Fact 兼容失效 → 旧产物直接加载失败；
- meta 未过滤 → "样本内 27 条"这种统计被当成待核查事实。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from agent.handoff import facts_to_claims
from agent.investigator import Investigator, LLMClient
from agent.merge import merge_events, similarity
from agent.schemas import Event, Evidence, Fact, Investigation, Metrics
from agent.trace import Tracer, load, summarize


class MergeTest(unittest.TestCase):
    def test_subset_names_merge(self) -> None:
        """「APEC」与「深圳APEC」是同一件事的两个标签，必须并成一个。"""
        merged, records = merge_events([Event(name="APEC"), Event(name="深圳APEC")])
        self.assertEqual(len(merged), 1)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].absorbed, "APEC")

    def test_different_events_stay_apart(self) -> None:
        events = [
            Event(name="影视飓风回应员工陪老板演白人及待遇争议"),
            Event(name="影视飓风发放iPhone Duo及全员放假玩GTA6"),
            Event(name="影视飓风创始人Tim开通个人账号及舆论反应"),
        ]
        merged, records = merge_events(events)
        self.assertEqual(len(merged), 3, [e.name for e in merged])
        self.assertEqual(records, [])

    def test_short_name_does_not_swallow_long_titles(self) -> None:
        """「APEC」不能把任何含 APEC 的长标题都吞掉。"""
        self.assertLess(similarity("APEC", "2026深圳APEC峰会工商领导人开幕式"), 0.75)

    def test_merge_keeps_evidence_and_heat(self) -> None:
        a = Event(
            name="APEC",
            heat="low",
            evidence=[Evidence(kind="weibo_post", mid="1", author="A", url="u1")],
        )
        b = Event(
            name="深圳APEC",
            heat="high",
            evidence=[Evidence(kind="weibo_post", mid="2", author="B", url="u2")],
        )
        merged, _ = merge_events([a, b])
        ev = merged[0]
        self.assertEqual(len(ev.evidence), 2)
        self.assertEqual(ev.heat, "high")
        self.assertEqual(ev.name, "深圳APEC")  # 取信息更全的长名
        self.assertIn("APEC", ev.merged_from)

    def test_merge_is_order_independent(self) -> None:
        """全局取最高相似度的一对，结果不应依赖输入顺序。"""
        a, b = Event(name="APEC"), Event(name="深圳APEC")
        first, _ = merge_events([a, b])
        second, _ = merge_events([b, a])
        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 1)


class FactCompatTest(unittest.TestCase):
    def test_old_string_facts_are_coerced(self) -> None:
        """旧产物是 list[str]，加载时必须自动升级成 Fact。"""
        ev = Event.model_validate({"name": "x", "facts": ["一句话事实"]})
        self.assertEqual(len(ev.facts), 1)
        self.assertEqual(ev.facts[0].text, "一句话事实")
        self.assertTrue(ev.facts[0].inferred_link)

    def test_new_dict_facts_keep_supporting(self) -> None:
        ev = Event.model_validate(
            {
                "name": "x",
                "facts": [
                    {
                        "text": "事实",
                        "supporting_mids": ["123"],
                        "source_kind": "weibo_post",
                    }
                ],
            }
        )
        self.assertEqual(ev.facts[0].supporting_mids, ["123"])
        self.assertFalse(ev.facts[0].inferred_link)
        self.assertEqual(ev.facts[0].source_kind, "weibo_post")

    def test_meta_fact_label_has_no_warning(self) -> None:
        self.assertEqual(Fact(text="样本统计", kind="meta").label(), "样本统计")
        self.assertIn("支撑自动关联", Fact(text="断言", inferred_link=True).label())


class HandoffTest(unittest.TestCase):
    def _inv(self) -> Investigation:
        return Investigation(
            topic="APEC",
            events=[
                Event(
                    name="事件一",
                    facts=[
                        Fact(
                            text="特朗普抵达深圳",
                            supporting_mids=["m1", "m2"],
                            source_kind="weibo_post",
                        ),
                        Fact(text="样本内 3 条帖子归入该聚类", kind="meta"),
                    ],
                    evidence=[
                        Evidence(
                            kind="weibo_post",
                            mid="m1",
                            author="新华社",
                            url="https://m.weibo.cn/detail/1",
                        ),
                        Evidence(
                            kind="weibo_post",
                            mid="m2",
                            author="新华社",
                            url="https://m.weibo.cn/detail/2",
                        ),
                    ],
                )
            ],
        )

    def test_meta_facts_are_skipped(self) -> None:
        payload = facts_to_claims(self._inv())
        self.assertEqual(len(payload["claims"]), 1)
        self.assertEqual(payload["handoff"]["skipped_meta"], 1)

    def test_same_author_is_one_independent_group(self) -> None:
        """同一个人发两条微博，不能算两个独立信源。"""
        payload = facts_to_claims(self._inv())
        groups = {s["independence_group"] for s in payload["sources"]}
        self.assertEqual(groups, {"weibo:新华社"})

    def test_claims_start_as_pending(self) -> None:
        payload = facts_to_claims(self._inv())
        self.assertTrue(all(c["verdict"] == "待核查" for c in payload["claims"]))

    def test_report_evidence_is_registered(self) -> None:
        payload = facts_to_claims(self._inv())
        claim = payload["claims"][0]
        self.assertEqual(len(claim["supporting"]), 2)
        self.assertEqual(payload["handoff"]["facts_without_evidence"], 0)


class TracerTest(unittest.TestCase):
    def test_writes_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.jsonl"
            t = Tracer(path)
            t.log("a", x=1)
            t.log("b", y=2)
            lines = path.read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(lines), 2)
            self.assertEqual(json.loads(lines[0])["step"], "a")
            self.assertEqual(t.counts(), {"a": 1, "b": 1})

    def test_memory_only_when_no_path(self) -> None:
        t = Tracer(None)
        t.log("x")
        self.assertEqual(len(t.records), 1)

    def test_load_skips_broken_lines(self) -> None:
        """trace 是用来排查问题的，最后一行被中断不该让整份读不出来。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.jsonl"
            path.write_text('{"step":"a"}\n\n{坏行\n{"step":"b"}\n', encoding="utf-8")
            self.assertEqual([r["step"] for r in load(path)], ["a", "b"])

    def test_summarize_separates_llm_calls(self) -> None:
        records = [
            {"step": "search", "keyword": "APEC"},
            {"step": "llm_call", "ok": True, "duration_ms": 100, "purpose": "event_build"},
            {
                "step": "llm_call",
                "ok": False,
                "duration_ms": 50,
                "purpose": "event_build",
                "error": "HTTP 402",
            },
        ]
        s = summarize(records)
        self.assertEqual(s["llm_calls"], 2)
        self.assertEqual(s["llm_ok"], 1)
        self.assertEqual(s["llm_failed"], 1)
        self.assertEqual(s["llm_ms"], 150)
        self.assertEqual(s["llm_purposes"], {"event_build": 2})
        self.assertEqual(len(s["errors"]), 1)


class _FakeCompletions:
    """按次序返回预设内容，或始终抛同一个异常。"""

    def __init__(self, contents=None, exc=None) -> None:
        self.contents = list(contents or [])
        self.exc = exc
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        if self.exc is not None:
            raise self.exc
        idx = min(self.calls - 1, len(self.contents) - 1)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=self.contents[idx]))]
        )


def _stub_client(contents=None, exc=None) -> LLMClient:
    client = LLMClient(api_key="test-key")
    client.available = True
    client._client = SimpleNamespace(
        chat=SimpleNamespace(completions=_FakeCompletions(contents, exc))
    )
    return client


class LLMTraceTest(unittest.TestCase):
    """模型调用必须留痕：否则只能看到"失败了"，看不到失败在第几次、返回了什么。"""

    def test_success_is_recorded_with_purpose(self) -> None:
        tracer = Tracer(None)
        client = _stub_client(['{"keywords": ["A"]}'])
        client.attach_tracer(tracer)
        out = client.chat_json("sys", "user", purpose="keyword_plan_r1")
        self.assertEqual(out, {"keywords": ["A"]})
        rec = [r for r in tracer.records if r["step"] == "llm_call"][0]
        self.assertTrue(rec["ok"])
        self.assertEqual(rec["purpose"], "keyword_plan_r1")
        self.assertEqual(rec["attempt"], 1)
        self.assertGreater(rec["response_chars"], 0)
        self.assertEqual(rec["request_chars"], len("sys") + len("user"))
        self.assertEqual(client.stats()["llm_calls"], 1)

    def test_fatal_error_is_not_retried(self) -> None:
        """402 换参数重试没有意义，只该记一次。"""

        class _Err(Exception):
            status_code = 402

        tracer = Tracer(None)
        client = _stub_client(exc=_Err("Insufficient Balance"))
        client.attach_tracer(tracer)
        self.assertIsNone(client.chat_json("s", "u", purpose="event_build"))
        calls = [r for r in tracer.records if r["step"] == "llm_call"]
        self.assertEqual(len(calls), 1)
        self.assertFalse(calls[0]["ok"])
        self.assertIn("402", calls[0]["error"])
        self.assertIn("余额", calls[0]["error"])
        # 失败时留下题面，否则无法复现"模型到底看到了什么"
        self.assertIn("prompt_preview", calls[0])

    def test_soft_error_retries_twice(self) -> None:
        class _Err(Exception):
            status_code = 500

        tracer = Tracer(None)
        client = _stub_client(exc=_Err("server error"))
        client.attach_tracer(tracer)
        self.assertIsNone(client.chat_json("s", "u", purpose="event_build"))
        calls = [r for r in tracer.records if r["step"] == "llm_call"]
        self.assertEqual([r["attempt"] for r in calls], [1, 2])
        self.assertEqual(client.stats()["llm_failures"], 2)

    def test_unparsable_then_fallback_succeeds(self) -> None:
        tracer = Tracer(None)
        client = _stub_client(["这不是 JSON", '{"events": []}'])
        client.attach_tracer(tracer)
        out = client.chat_json("s", "u", purpose="event_build")
        self.assertEqual(out, {"events": []})
        calls = [r for r in tracer.records if r["step"] == "llm_call"]
        self.assertEqual([r["ok"] for r in calls], [False, True])
        # 解析失败时要把模型原始输出留下来，否则不知道它到底返回了什么
        self.assertIn("这不是 JSON", calls[0]["response_preview"])
        stats = client.stats()
        self.assertEqual((stats["llm_calls"], stats["llm_failures"]), (1, 1))

    def test_no_tracer_still_counts(self) -> None:
        """不给 tracer 也不能崩，统计照旧。"""
        client = _stub_client(['{"a": 1}'])
        self.assertEqual(client.chat_json("s", "u"), {"a": 1})
        self.assertEqual(client.stats()["llm_calls"], 1)

    def test_investigator_wires_tracer_into_llm(self) -> None:
        """接线本身要锁住：Investigator 忘了 attach，trace 里就一条模型记录都没有。"""
        client = _stub_client(['{"keywords": ["x"]}'])
        inv = Investigator(session=None, llm=client, tracer=Tracer(None))
        self.assertIs(client.tracer, inv.tracer)
        client.chat_json("s", "u", purpose="keyword_plan_r1")
        m = inv._metrics(Investigation(topic="t"))
        self.assertEqual((m.llm_calls, m.llm_failures), (1, 0))

    def test_markdown_shows_model_calls(self) -> None:
        """报告里要能看出这次到底调了几次模型，否则只能靠猜。"""
        inv = Investigation(topic="t", metrics=Metrics(llm_calls=3, llm_failures=1, llm_ms=2500))
        md = inv.to_markdown()
        self.assertIn("模型调用：成功 3 次｜失败尝试 1 次｜累计 2.5 秒", md)


if __name__ == "__main__":
    unittest.main()
