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

from agent.handoff import facts_to_claims
from agent.merge import merge_events, similarity
from agent.schemas import Event, Evidence, Fact, Investigation
from agent.trace import Tracer


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


if __name__ == "__main__":
    unittest.main()
