"""核查层的纯逻辑测试：引文比对、降级规则、信源独立性计数。

这三个是"核查"区别于"总结"的地方，改动它们等于改动结论本身，必须有测试兜住。
"""

from __future__ import annotations

import unittest

from factcheck.runner import apply_checks, build_summary
from factcheck.schemas import ClaimCheck, Cite, FactCheckReport, Source
from factcheck.semantic import aggregate, apply_result, build_prompt, run_semantic
from factcheck.verify import quote_present


class QuoteMatchTest(unittest.TestCase):
    def test_ignores_punctuation_and_spaces(self) -> None:
        page = "Tim 在通知里写道：“老规矩,每人一台iPhone Duo(也可以自选等价的其他型号)”"
        self.assertTrue(quote_present(page, "每人一台iPhone Duo（也可以自选等价的其他型号）"))

    def test_detects_missing_quote(self) -> None:
        page = "这篇报道只讲了放假安排，没有提到主机。"
        self.assertFalse(quote_present(page, "PS5 Pro"))

    def test_tolerates_one_char_edit_in_long_quote(self) -> None:
        page = "他说大家总共说了130万条难听话给我真的有点多了"
        self.assertTrue(quote_present(page, "大家总共说了130万条难听话给我"))

    def test_empty_inputs_are_false(self) -> None:
        self.assertFalse(quote_present("", "任意"))
        self.assertFalse(quote_present("正文", ""))


class DowngradeTest(unittest.TestCase):
    def _report(self, reachable: bool) -> FactCheckReport:
        src = Source(
            id="S1",
            kind="company_registry",
            publisher="某工商平台",
            url="https://example.com/a",
            quote="社保人数 122",
            independence_group="reg",
        )
        src.reachable = reachable
        src.http_status = 200 if reachable else 403
        src.quote_found = True if reachable else None
        claim = ClaimCheck(
            id="C1",
            claim="参保人数为 122 人",
            verdict="已证实",
            confidence="高",
            supporting=[Cite(source_id="S1", point="显示社保人数 122")],
        )
        return FactCheckReport(title="t", claims=[claim], sources=[src])

    def test_unreachable_support_forces_downgrade(self) -> None:
        report = self._report(reachable=False)
        apply_checks(report)
        # 原始判定保留，降级只体现在生效判定上，便于追溯"原本是怎么判的"
        self.assertEqual(report.claims[0].verdict, "已证实")
        self.assertEqual(report.claims[0].final_verdict, "无法证实")
        self.assertEqual(report.claims[0].effective(), "无法证实")
        self.assertTrue(report.claims[0].downgraded)

    def test_reachable_support_keeps_verdict(self) -> None:
        report = self._report(reachable=True)
        apply_checks(report)
        self.assertEqual(report.claims[0].verdict, "已证实")
        self.assertEqual(report.claims[0].effective(), "已证实")
        self.assertFalse(report.claims[0].downgraded)

    def test_pending_claim_is_not_downgraded(self) -> None:
        """待核查本来就没下结论，不能因为证据校验失败就给扣成"无法证实"。"""
        report = self._report(reachable=False)
        report.claims[0].verdict = "待核查"
        apply_checks(report)
        self.assertEqual(report.claims[0].effective(), "待核查")
        self.assertFalse(report.claims[0].downgraded)

    def test_skipped_check_does_not_downgrade(self) -> None:
        """跳过机检时不能把所有来源当成"未通过"，否则结论会被误降级。"""
        report = self._report(reachable=False)
        apply_checks(report, checked=False)
        claim = report.claims[0]
        self.assertEqual(claim.effective(), "已证实")
        self.assertFalse(claim.downgraded)
        self.assertEqual(claim.failed_sources, [])
        self.assertIn("未机检", report.mode)


class SummaryTest(unittest.TestCase):
    def test_reprints_count_as_one_source(self) -> None:
        """同一稿件的两家转载，不能算两个独立信源。"""
        a = Source(id="S1", kind="media", publisher="A", url="u1", independence_group="same")
        b = Source(id="S2", kind="media", publisher="B", url="u2", independence_group="same")
        for s in (a, b):
            s.reachable = True
            s.quote_found = True
        claim = ClaimCheck(
            id="C1",
            claim="x",
            verdict="部分证实",
            supporting=[Cite(source_id="S1"), Cite(source_id="S2")],
        )
        report = FactCheckReport(title="t", claims=[claim], sources=[a, b])
        apply_checks(report)
        self.assertEqual(claim.independent_groups, 1)
        summary = build_summary(report, {})
        self.assertEqual(summary["independently_verified"], 0)
        self.assertEqual(summary["secondhand_only"], 1)

    def test_distinct_groups_count_as_independent(self) -> None:
        a = Source(id="S1", kind="media", publisher="A", url="u1", independence_group="g1")
        b = Source(id="S2", kind="media", publisher="B", url="u2", independence_group="g2")
        for s in (a, b):
            s.reachable = True
            s.quote_found = True
        claim = ClaimCheck(
            id="C1",
            claim="x",
            verdict="已证实",
            supporting=[Cite(source_id="S1"), Cite(source_id="S2")],
        )
        report = FactCheckReport(title="t", claims=[claim], sources=[a, b])
        apply_checks(report)
        self.assertEqual(claim.independent_groups, 2)
        self.assertEqual(build_summary(report, {})["independently_verified"], 1)


class SemanticAggregateTest(unittest.TestCase):
    """逐条标注怎么聚合成"这条说法的引文成色"。结论直接依赖它，规则不能含糊。"""

    def _claim(self, *levels: str) -> ClaimCheck:
        return ClaimCheck(
            id="C1",
            claim="x",
            verdict="已证实",
            supporting=[
                Cite(source_id=f"S{i}", support=lvl) for i, lvl in enumerate(levels, 1)
            ],
        )

    def test_any_contradiction_wins(self) -> None:
        self.assertEqual(aggregate(self._claim("supports", "contradicts")), "conflicting")

    def test_all_unrelated(self) -> None:
        self.assertEqual(aggregate(self._claim("unrelated", "unrelated")), "unrelated")

    def test_all_insufficient(self) -> None:
        self.assertEqual(aggregate(self._claim("insufficient")), "insufficient")

    def test_pure_support(self) -> None:
        self.assertEqual(aggregate(self._claim("supports", "partial")), "supported")

    def test_mixed_support_and_unrelated_is_partial(self) -> None:
        self.assertEqual(aggregate(self._claim("supports", "unrelated")), "partial")

    def test_no_marks_means_not_run(self) -> None:
        self.assertEqual(aggregate(self._claim("unknown")), "not_run")


class SemanticApplyTest(unittest.TestCase):
    """模型的标注只能让结论变弱，永不变强。"""

    def _report(self, verdict: str = "已证实") -> FactCheckReport:
        a = Source(id="S1", kind="media", publisher="A", url="u1", independence_group="g1")
        b = Source(id="S2", kind="media", publisher="B", url="u2", independence_group="g2")
        for s in (a, b):
            s.reachable = True
            s.quote_found = True
        claim = ClaimCheck(
            id="C1",
            claim="发放了 PS5 Pro",
            verdict=verdict,  # type: ignore[arg-type]
            supporting=[Cite(source_id="S1"), Cite(source_id="S2")],
        )
        return FactCheckReport(title="t", claims=[claim], sources=[a, b])

    def test_unrelated_citations_downgrade(self) -> None:
        report = self._report()
        for cite in report.claims[0].supporting:
            cite.support = "unrelated"
        report.claims[0].citation_check = "unrelated"
        apply_checks(report)
        claim = report.claims[0]
        self.assertEqual(claim.verdict, "已证实")  # 原判定保留
        self.assertEqual(claim.effective(), "无法证实")
        self.assertTrue(claim.downgraded)
        self.assertIn("语义降级", claim.analysis)

    def test_never_upgrades(self) -> None:
        """模型说"很支持"也不能把"无法证实"抬成"已证实"。"""
        report = self._report(verdict="无法证实")
        for cite in report.claims[0].supporting:
            cite.support = "supports"
        report.claims[0].citation_check = "supported"
        apply_checks(report)
        claim = report.claims[0]
        self.assertEqual(claim.effective(), "无法证实")
        self.assertFalse(claim.downgraded)

    def test_cross_conflict_needs_two_independent_groups(self) -> None:
        report = self._report()
        for cite in report.claims[0].supporting:
            cite.support = "supports"
        claim = report.claims[0]
        claim.citation_check = "supported"
        claim.cross_conflict = True
        claim.cross_conflict_reason = "粉丝数一条写 300 万、一条写 500 万"
        apply_checks(report)
        self.assertEqual(claim.independent_groups, 2)
        self.assertEqual(claim.effective(), "存在矛盾")
        self.assertTrue(claim.semantic_revised)

    def test_cross_conflict_ignored_when_same_source(self) -> None:
        """两条引文来自同一个稿源时，"矛盾"可能只是表述差异，不足以改判。"""
        report = self._report()
        report.sources[1].independence_group = "g1"
        for cite in report.claims[0].supporting:
            cite.support = "supports"
        claim = report.claims[0]
        claim.citation_check = "supported"
        claim.cross_conflict = True
        apply_checks(report)
        self.assertEqual(claim.independent_groups, 1)
        self.assertEqual(claim.effective(), "已证实")

    def test_pending_claim_keeps_verdict(self) -> None:
        report = self._report(verdict="待核查")
        for cite in report.claims[0].supporting:
            cite.support = "unrelated"
        report.claims[0].citation_check = "unrelated"
        apply_checks(report)
        claim = report.claims[0]
        self.assertEqual(claim.effective(), "待核查")
        self.assertFalse(claim.downgraded)


class _FakeLLM:
    model = "fake"
    last_error = ""

    def __init__(self, payloads) -> None:
        self.payloads = list(payloads)
        self.calls = 0
        self.purposes: list[str] = []

    def chat_json(self, system: str, user: str, purpose: str = ""):
        self.purposes.append(purpose)
        payload = self.payloads[min(self.calls, len(self.payloads) - 1)]
        self.calls += 1
        if payload is None:
            self.last_error = "HTTP 402 Insufficient Balance"
        return payload


class SemanticRunTest(unittest.TestCase):
    def _report(self) -> FactCheckReport:
        src = Source(
            id="S1",
            kind="social_post",
            publisher="某人",
            url="https://example.com/a",
            quote="今天全员放假玩 GTA6",
            title="放假通知",
            independence_group="weibo:某人",
        )
        src.reachable = True
        src.quote_found = True
        claim = ClaimCheck(
            id="C1",
            claim="每人发了一台 PS5 Pro",
            verdict="待核查",
            supporting=[Cite(source_id="S1")],
        )
        return FactCheckReport(title="t", claims=[claim], sources=[src])

    def test_marks_cite_and_aggregates(self) -> None:
        report = self._report()
        llm = _FakeLLM(
            [
                {
                    "citations": [
                        {"source_id": "S1", "support": "unrelated", "reason": "只提到放假，未提主机"}
                    ],
                    "cross_conflict": False,
                    "note": "引文与说法无关",
                }
            ]
        )
        info = run_semantic(report, llm, log=lambda *_: None)
        claim = report.claims[0]
        self.assertEqual(info["judged"], 1)
        self.assertEqual(info["cites_marked"], 1)
        self.assertEqual(claim.citation_check, "unrelated")
        self.assertEqual(claim.supporting[0].support_reason, "只提到放假，未提主机")
        self.assertEqual(llm.purposes, ["claim_citation_check"])

    def test_bogus_source_id_is_dropped(self) -> None:
        """模型编出不存在的来源 id 时必须丢弃，不能写进报告。"""
        report = self._report()
        llm = _FakeLLM(
            [{"citations": [{"source_id": "S99", "support": "supports", "reason": "编的"}]}]
        )
        info = run_semantic(report, llm, log=lambda *_: None)
        self.assertEqual(info["cites_marked"], 0)
        self.assertEqual(report.claims[0].supporting[0].support, "unknown")

    def test_failure_does_not_break_report(self) -> None:
        report = self._report()
        info = run_semantic(report, _FakeLLM([None]), log=lambda *_: None)
        self.assertEqual(info["judged"], 0)
        self.assertEqual(info["failed"], 1)
        self.assertEqual(report.claims[0].citation_check, "not_run")

    def test_prompt_has_no_url(self) -> None:
        """只给引文，不给链接：避免模型去"推测"页面内容。"""
        report = self._report()
        claim = report.claims[0]
        prompt = build_prompt(claim, [(claim.supporting[0], report.sources[0])])
        self.assertIn("每人发了一台 PS5 Pro", prompt)
        self.assertIn("今天全员放假玩 GTA6", prompt)
        self.assertNotIn("https://", prompt)


if __name__ == "__main__":
    unittest.main()
