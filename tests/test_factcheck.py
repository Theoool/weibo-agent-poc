"""核查层的纯逻辑测试：引文比对、降级规则、信源独立性计数。

这三个是"核查"区别于"总结"的地方，改动它们等于改动结论本身，必须有测试兜住。
"""

from __future__ import annotations

import unittest

from factcheck.runner import apply_checks, build_summary
from factcheck.schemas import ClaimCheck, Cite, FactCheckReport, Source
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


if __name__ == "__main__":
    unittest.main()
