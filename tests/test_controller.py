"""Controller 单测：缺口判定与补搜循环。

这一层被改坏时，报告不会报错，只会"看起来正常"：
- 缺口阈值判错 → 每个事件都被要求补证据，预算被平均摊薄；
- 候选词放开了 → 模型开始造"XX 官方回应"这类事件名，搜回来的全是噪声；
- 循环不会停 → 同一批帖子被反复搜，最后配额耗尽还写着"查够了"。

所以这里的每一条都对着一个具体的坏结果。
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from agent.controller import (
    FALLBACK,
    NEED_EVIDENCE,
    NEED_INDEPENDENT,
    NEED_OFFICIAL,
    SAMPLE,
    THIN_SAMPLE,
    candidate_pool,
    detect_gaps,
    plan_round,
    stop_reason,
)
from agent.investigator import Budget, Investigator, LLMClient
from agent.schemas import Event, Evidence, GapRecord, Investigation
from agent.trace import Tracer


def _post(mid: str, author: str, tags: list[str] | None = None, verified: bool = False) -> dict:
    return {
        "mid": mid,
        "author": author,
        "hashtags": tags or [],
        "verified": verified,
        "text": f"正文 {mid}",
        "url": f"https://m.weibo.cn/detail/{mid}",
    }


def _event(name: str, mids: list[str], keywords: list[str] | None = None) -> Event:
    return Event(
        name=name,
        keywords=keywords if keywords is not None else [name],
        evidence=[Evidence(kind="weibo_post", mid=m) for m in mids],
    )


class GapDetectTest(unittest.TestCase):
    def setUp(self) -> None:
        self.posts = {
            "m1": _post("m1", "甲", ["#APEC"]),
            "m2": _post("m2", "甲", ["#APEC"]),
            "m3": _post("m3", "乙", ["#APEC"]),
            "m4": _post("m4", "乙", ["#APEC"], verified=True),
            "m5": _post("m5", "丙", ["#APEC"]),
            "m6": _post("m6", "丁", ["#APEC"]),
            "m7": _post("m7", "戊", ["#APEC"]),
            "m8": _post("m8", "己", ["#APEC"]),
            "m9": _post("m9", "庚", ["#APEC"]),
            "m10": _post("m10", "辛", ["#APEC"]),
        }

    def test_thin_evidence_is_reported_first(self) -> None:
        gaps = detect_gaps([_event("事件A", ["m1"])], self.posts, [], min_evidence=3)
        kinds = [g.kind for g in gaps]
        self.assertEqual(kinds[0], NEED_EVIDENCE)
        # 证据不足时不再同时要求"独立信源"和"官方口径"，否则三份预算打同一件事
        self.assertNotIn(NEED_INDEPENDENT, kinds)
        self.assertNotIn(NEED_OFFICIAL, kinds)

    def test_single_author_is_not_independent(self) -> None:
        gaps = detect_gaps([_event("事件A", ["m1", "m2"])], self.posts, [], min_evidence=2)
        self.assertEqual(gaps[0].kind, NEED_INDEPENDENT)
        self.assertIn("甲", gaps[0].detail)

    def test_missing_official_source(self) -> None:
        gaps = detect_gaps([_event("事件A", ["m1", "m3"])], self.posts, [], min_evidence=2)
        self.assertEqual(gaps[0].kind, NEED_OFFICIAL)

    def test_verified_author_closes_official_gap(self) -> None:
        gaps = detect_gaps([_event("事件A", ["m3", "m4"])], self.posts, [], min_evidence=2)
        self.assertNotIn(NEED_OFFICIAL, [g.kind for g in gaps])

    def test_thin_sample_has_top_priority(self) -> None:
        gaps = detect_gaps([_event("事件A", ["m1"])], self.posts, [], min_evidence=3, min_posts=50)
        thin = [g for g in gaps if g.kind == THIN_SAMPLE]
        self.assertEqual(len(thin), 1)
        self.assertEqual(thin[0].target, SAMPLE)
        self.assertEqual(thin[0].priority, 0)

    def test_searched_keywords_are_not_candidates(self) -> None:
        gaps = detect_gaps(
            [_event("事件A", ["m1"], keywords=["APEC"])],
            self.posts,
            ["APEC"],
            min_evidence=3,
        )
        self.assertNotIn("APEC", gaps[0].candidates)

    def test_candidates_only_come_from_scraped_data(self) -> None:
        """候选词必须能在已抓到的帖子话题词或榜单里找到，不能凭空造。"""
        posts = {"m1": _post("m1", "甲", ["#话题甲#"]), "m2": _post("m2", "乙", ["#话题甲#"])}
        gaps = detect_gaps([_event("事件A", ["m1"], keywords=[])], posts, [], min_evidence=3)
        self.assertEqual(gaps[0].candidates, ["话题甲"])

    def test_single_occurrence_tag_is_noise(self) -> None:
        """只出现一次的话题标签多半是搜索噪声，不能拿它去补搜。"""
        posts = {"m1": _post("m1", "甲", ["#全妆出街#"]), "m2": _post("m2", "乙", ["#APEC#"])}
        gaps = detect_gaps([_event("事件A", ["m1"], keywords=[])], posts, [], min_evidence=3)
        self.assertEqual(gaps[0].candidates, [])


class PlanRoundTest(unittest.TestCase):
    def test_priority_wins_and_limit_holds(self) -> None:
        gaps = [
            detect_gaps([_event("低优先", ["m1", "m2"])], {"m1": _post("m1", "甲"), "m2": _post("m2", "乙")}, [], min_evidence=2, min_posts=1)[0]
        ]
        plan = plan_round(gaps, ["话题A", "话题B", "话题C"], [], [], limit=2)
        self.assertLessEqual(len(plan.keywords), 2)
        self.assertTrue(plan.keywords)

    def test_no_duplicate_keywords(self) -> None:
        pool = ["话题A", "话题B"]
        gaps = detect_gaps(
            [_event("事件A", ["m1"], keywords=["话题A"])],
            {"m1": _post("m1", "甲", ["#话题A#"])},
            [],
            min_evidence=3,
            min_posts=1,
        )
        plan = plan_round(gaps, pool, [], [], limit=3)
        self.assertEqual(len(plan.keywords), len(set(plan.keywords)))

    def test_fallback_when_no_gaps(self) -> None:
        """首轮扩展允许保底：否则“自主扩展 ≥1 次”这项验收永远是空的。"""
        plan = plan_round([], ["话题A"], [], [], limit=3, allow_fallback=True)
        self.assertEqual(plan.keywords, ["话题A"])
        self.assertEqual(plan.assignments[0][0].kind, FALLBACK)

    def test_no_fallback_after_first_expansion(self) -> None:
        """缺口清零之后不能再保底搜——那已经是白烧配额。"""
        plan = plan_round([], ["话题A"], [], [], limit=3, allow_fallback=False)
        self.assertEqual(plan.keywords, [])

    def test_fallback_never_repeats_searched(self) -> None:
        plan = plan_round([], ["话题A"], [], ["话题A"], limit=3, allow_fallback=True)
        self.assertEqual(plan.keywords, [])

    def test_empty_when_nothing_to_search(self) -> None:
        plan = plan_round([], [], [], [], limit=3)
        self.assertEqual(plan.keywords, [])
        self.assertTrue(stop_reason([], plan))

    def test_stop_reason_names_the_blocker(self) -> None:
        gaps = detect_gaps(
            [_event("事件A", ["m1"], keywords=["已搜词"])],
            {"m1": _post("m1", "甲")},
            ["已搜词"],
            min_evidence=3,
            min_posts=1,
        )
        plan = plan_round(gaps, [], [], ["已搜词"], limit=3)
        self.assertEqual(plan.keywords, [])
        self.assertIn("缺口", stop_reason(gaps, plan))


class _FakeCompletions:
    def __init__(self, contents) -> None:
        self.contents = list(contents)
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        idx = min(self.calls - 1, len(self.contents) - 1)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=self.contents[idx]))]
        )


def _stub_client(contents) -> LLMClient:
    client = LLMClient(api_key="test-key")
    client.available = True
    client._client = SimpleNamespace(
        chat=SimpleNamespace(completions=_FakeCompletions(contents))
    )
    return client


class CandidateWhitelistTest(unittest.TestCase):
    """模型只能在候选里挑，这是和 mid 白名单同一个道理。"""

    def _investigator(self, reply: str) -> Investigator:
        tracer = Tracer(None)
        inv = Investigator(
            session=None,
            llm=_stub_client([reply]),
            tracer=tracer,
            search_delay=0,
            log=lambda *_: None,
        )
        inv.posts = {"m1": _post("m1", "甲", ["#话题甲#"])}
        return inv

    def _gaps(self, inv):
        return detect_gaps(
            [_event("事件A", ["m1"], keywords=["话题甲"])],
            inv.posts,
            [],
            min_evidence=3,
            min_posts=1,
        )

    def test_out_of_candidate_keyword_is_discarded(self) -> None:
        inv = self._investigator('{"keywords": ["话题甲 官方回应"]}')
        plan = inv._plan_from_gaps(self._gaps(inv), round_no=2)
        self.assertNotIn("话题甲 官方回应", plan.keywords)
        self.assertTrue(plan.keywords)
        rejected = [r for r in inv.tracer.records if r["step"] == "gap_plan_rejected"]
        self.assertEqual(len(rejected), 1)

    def test_valid_candidate_is_used(self) -> None:
        inv = self._investigator('{"keywords": ["话题甲"]}')
        plan = inv._plan_from_gaps(self._gaps(inv), round_no=2)
        self.assertEqual(plan.keywords, ["话题甲"])
        self.assertEqual([g.kind for g, _ in plan.assignments], [NEED_EVIDENCE])

    def test_empty_model_output_falls_back_to_code_order(self) -> None:
        inv = self._investigator('{"keywords": []}')
        plan = inv._plan_from_gaps(self._gaps(inv), round_no=2)
        self.assertEqual(plan.keywords, ["话题甲"])


class ResolveTest(unittest.TestCase):
    def _inv(self) -> Investigator:
        inv = Investigator(session=None, tracer=Tracer(None), log=lambda *_: None)
        return inv

    def test_gap_that_disappears_is_resolved(self) -> None:
        inv = self._inv()
        rec = GapRecord(round=2, kind=NEED_EVIDENCE, target="事件A", action="话题甲")
        inv._resolve_pending([(NEED_EVIDENCE, "事件A", rec)], [], [Event(name="事件A")])
        self.assertTrue(rec.resolved)

    def test_gap_that_remains_is_not_resolved(self) -> None:
        inv = self._inv()
        rec = GapRecord(round=2, kind=NEED_EVIDENCE, target="事件A")
        gaps = detect_gaps(
            [_event("事件A", ["m1"])], {"m1": _post("m1", "甲")}, [], min_evidence=3, min_posts=1
        )
        inv._resolve_pending([(NEED_EVIDENCE, "事件A", rec)], gaps, [Event(name="事件A")])
        self.assertFalse(rec.resolved)

    def test_renamed_event_is_unjudgeable(self) -> None:
        """事件被重新归纳后改了名，缺口的"消失"与补搜无关，不能冒充已补齐。"""
        inv = self._inv()
        rec = GapRecord(round=2, kind=NEED_EVIDENCE, target="旧名字")
        inv._resolve_pending([(NEED_EVIDENCE, "旧名字", rec)], [], [Event(name="新名字")])
        self.assertIsNone(rec.resolved)

    def test_similar_new_name_is_still_matched(self) -> None:
        """模型改名是常态（实测同一事件三轮三个名字），改名后仍要能判断缺口是否补上。"""
        inv = self._inv()
        rec = GapRecord(round=2, kind=NEED_EVIDENCE, target="深圳APEC峰会交通管制")
        inv._resolve_pending(
            [(NEED_EVIDENCE, "深圳APEC峰会交通管制", rec)],
            [],
            [Event(name="深圳APEC峰会交通管制措施")],
        )
        self.assertTrue(rec.resolved)

    def test_similar_name_with_same_gap_is_not_resolved(self) -> None:
        inv = self._inv()
        rec = GapRecord(round=2, kind=NEED_EVIDENCE, target="深圳APEC峰会交通管制")
        gaps = detect_gaps(
            [_event("深圳APEC峰会交通管制", ["m1"])],
            {"m1": _post("m1", "甲")},
            [],
            min_evidence=3,
            min_posts=1,
        )
        inv._resolve_pending(
            [(NEED_EVIDENCE, "深圳APEC峰会交通管制", rec)],
            gaps,
            [Event(name="深圳APEC峰会交通管制")],
        )
        self.assertFalse(rec.resolved)


class GapLoopTest(unittest.TestCase):
    """补搜循环的停止条件：不会停的 Agent 会把预算均匀烧光。"""

    def _inv(self, results: list) -> Investigator:
        inv = Investigator(
            session=None,
            budget=Budget(max_rounds=3, min_evidence=3, max_searches=6, max_pages=20),
            tracer=Tracer(None),
            search_delay=0,
            log=lambda *_: None,
        )
        # 第一轮的结果直接灌进去，避免依赖浏览器
        inv.posts = {"m1": _post("m1", "甲", ["#话题甲#"]), "m2": _post("m2", "甲", ["#话题甲#"])}
        inv.searches = []
        inv.hot_items = []
        inv._fake = results
        return inv

    def test_stops_when_no_new_posts(self) -> None:
        inv = self._inv([])
        with patch("agent.investigator.collect_search", return_value=SimpleNamespace(
            keyword="x", url="u", posts=[], limitations=[]
        )), patch("agent.investigator.time.sleep"):
            events = inv._run_gap_rounds([_event("话题甲", ["m1", "m2"])])
        self.assertIn("未带来新帖子", inv.stop_reason)
        self.assertEqual(inv.rounds_used, 2)
        self.assertTrue(events)

    def test_stops_when_rounds_exhausted(self) -> None:
        """每轮都真有新数据时，刹车只能是轮次上限，不能一直搜下去。"""
        inv = self._inv([])
        counter = {"n": 0}

        def _fresh(*args, **kwargs):
            counter["n"] += 1
            # 每轮都带来一条全新话题的帖子，保证候选不会枯竭，
            # 这样唯一的刹车就只能是轮次上限
            return SimpleNamespace(
                keyword=args[1] if len(args) > 1 else kwargs.get("keyword", ""),
                url="u",
                posts=[_post(f"n{counter['n']}", f"作者{counter['n']}", [f"#话题{counter['n']}#"])],
                limitations=[],
            )

        with patch("agent.investigator.collect_search", side_effect=_fresh), patch(
            "agent.investigator.time.sleep"
        ):
            inv._run_gap_rounds([_event("话题甲", ["m1", "m2"])])
        self.assertEqual(inv.rounds_used, inv.budget.max_rounds)
        self.assertIn("轮次上限", inv.stop_reason)

    def test_stops_on_budget(self) -> None:
        inv = self._inv([])
        inv.budget.max_searches = 0
        with patch("agent.investigator.collect_search") as fake:
            inv._run_gap_rounds([_event("话题甲", ["m1", "m2"])])
            fake.assert_not_called()
        self.assertIn("预算耗尽", inv.stop_reason)

    def test_gap_records_are_written(self) -> None:
        inv = self._inv([])
        # 调低下限，让事件跨过"证据不足"进入"信源不独立"这一档
        inv.budget.min_evidence = 2
        with patch("agent.investigator.collect_search", return_value=SimpleNamespace(
            keyword="x", url="u", posts=[], limitations=[]
        )), patch("agent.investigator.time.sleep"):
            inv._run_gap_rounds([_event("话题甲", ["m1", "m2"])])
        kinds = [r.kind for r in inv.gap_log]
        self.assertTrue(inv.gap_log)
        # 薄样本与信源不独立都要记下来，即使这一轮没轮到搜后者
        self.assertIn(THIN_SAMPLE, kinds)
        self.assertIn(NEED_INDEPENDENT, kinds)
        for rec in inv.gap_log:
            self.assertTrue(rec.target and rec.detail)
            self.assertFalse(rec.resolved)  # 补搜没带回新帖子，缺口仍在
        self.assertTrue(any(r.action for r in inv.gap_log))


class RunIntegrationTest(unittest.TestCase):
    """整条 run() 跑一遍（浏览器全部打桩）：确认循环接管了原来的固定两轮。"""

    def test_run_records_gaps_and_stop_reason(self) -> None:
        seed_posts = [
            _post("a1", "甲", ["#APEC交通管制#"], verified=False),
            _post("a2", "甲", ["#APEC交通管制#"], verified=False),
            _post("b1", "乙", ["#APEC峰会#"]),
            _post("b2", "丙", ["#APEC峰会#"]),
        ]

        def _search(session, keyword, **kwargs):
            return SimpleNamespace(keyword=keyword, url=f"https://s.weibo.com/{keyword}",
                                   posts=list(seed_posts), limitations=[])

        client = LLMClient(api_key="x")
        client.available = False  # 强制 heuristic，避免测试依赖网络

        inv = Investigator(
            session=None,
            topic="APEC",
            budget=Budget(max_rounds=2, min_evidence=2, max_searches=6, max_pages=20),
            llm=client,
            tracer=Tracer(None),
            search_delay=0,
            log=lambda *_: None,
        )
        with patch("agent.investigator.read_hot_board", return_value=["APEC 交通管制"]), patch(
            "agent.investigator.collect_search", side_effect=_search
        ), patch("agent.investigator.verify_detail", return_value={"ok": False, "reason": "test"}):
            result = inv.run()

        self.assertTrue(result.events, "打桩数据应能聚出事件")
        self.assertTrue(result.stop_reason, "必须交代为什么停")
        self.assertTrue(result.gaps, "必须留下缺口记录")
        self.assertEqual(result.metrics.rounds_used, inv.rounds_used)
        # 同一批帖子重复出现 → 第二轮就该停，不该把预算烧完
        self.assertIn("未带来新帖子", result.stop_reason)
        self.assertEqual(result.metrics.gaps_detected, len(result.gaps))


    def test_stops_when_evidence_is_enough(self) -> None:
        """缺口清零后就该停，不能靠"保底扩展"把轮次一直跑满。"""
        posts = [
            _post("a1", "甲", ["#APEC交通管制#"], verified=True),
            _post("a2", "乙", ["#APEC交通管制#"]),
            _post("b1", "丙", ["#APEC峰会#"], verified=True),
            _post("b2", "丁", ["#APEC峰会#"]),
        ]

        def _search(session, keyword, **kwargs):
            return SimpleNamespace(keyword=keyword, url="u", posts=list(posts), limitations=[])

        client = LLMClient(api_key="x")
        client.available = False
        inv = Investigator(
            session=None,
            topic="APEC",
            budget=Budget(max_rounds=4, min_evidence=2, min_posts=2, max_searches=6, max_pages=20),
            llm=client,
            tracer=Tracer(None),
            search_delay=0,
            log=lambda *_: None,
        )
        with patch("agent.investigator.read_hot_board", return_value=["APEC 交通管制"]), patch(
            "agent.investigator.collect_search", side_effect=_search
        ), patch("agent.investigator.verify_detail", return_value={"ok": False, "reason": "t"}):
            result = inv.run()

        self.assertIn("证据已达标", result.stop_reason)
        self.assertLess(result.metrics.rounds_used, 4)
        # 只剩首轮扩展一次的机会，不该继续搜
        self.assertEqual(result.metrics.keywords_searched, 2)


class LimitationDedupeTest(unittest.TestCase):
    """同一限制重复十遍会把真正不同的限制冲掉。"""

    def test_repeated_limit_is_collapsed(self) -> None:
        from agent.schemas import Limitation, dedupe_limitations

        items = [
            Limitation(step="详情页复核", detail="预算耗尽"),
            Limitation(step="详情页复核", detail="预算耗尽"),
            Limitation(step="登录态", detail="未登录"),
        ]
        out = dedupe_limitations(items)
        self.assertEqual(len(out), 2)
        self.assertIn("同类共 2 次", out[0].detail)
        self.assertEqual(out[1].step, "登录态")

    def test_markdown_also_dedupes_old_artifacts(self) -> None:
        """旧产物里已经存了重复项，渲染时要收敛，不能原样打印四遍。"""
        from agent.schemas import Limitation

        inv = Investigation(
            topic="t",
            limitations=[
                Limitation(step="详情页复核", detail="预算耗尽"),
                Limitation(step="详情页复核", detail="预算耗尽"),
            ],
        )
        md = inv.to_markdown()
        self.assertEqual(md.count("预算耗尽"), 1)


if __name__ == "__main__":
    unittest.main()
