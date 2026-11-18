"""M3 验收：只读 API 能不能回答产品问题。

不是"接口返回 200"，而是三个具体问题：
1. 这份核查报告核的是哪一次调查？（`provenance.chain` / `investigate_stage_id`）
2. 每条结论引的原文是哪条帖子？（`claims → sources → document_id → text`）
3. 某条帖子被抓过几次、内容变没变？（`fetches` / `versions` / `content_changed`）

另外验证两条纪律：
- **报告现渲染**，不读 `output/`；
- 历史导入的 degraded run 拿不到原文时，接口说"没有"，而不是编一条。
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from fastapi.testclient import TestClient

from agent.schemas import Event, Evidence, Fact, GapRecord, Investigation, Metrics
from agent.trace import LLMCall
from factcheck.schemas import ClaimCheck, Cite, FactCheckReport, Source
from store import open_store
from store.db_sink import DbSink

MID = "5366736759032400"
POST_URL = f"https://m.weibo.cn/detail/{MID}"
OTHER_URL = "https://example.com/notice?id=1"
FULL_TEXT = (
    "深圳 APEC 期间部分路段实行临时交通管制，交警提醒提前规划路线。"
    "这是完整的帖子正文，明显长于报告里的截断摘录，用来验证 API 读到的是全文。"
)


def posts(text: str = FULL_TEXT) -> dict[str, dict]:
    return {
        MID: {
            "mid": MID,
            "author": "深圳交警",
            "time": "2026-11-18 09:00",
            "text": text,
            "url": POST_URL,
            "verified": True,
            "detail_verified": True,
        }
    }


def investigation() -> Investigation:
    return Investigation(
        topic="APEC 微博舆情",
        generated_at=datetime(2026, 11, 18, 23, 30, 0),
        mode="llm",
        events=[
            Event(
                name="深圳APEC峰会交通管制",
                summary="多条微博提到临时管制",
                heat="high",
                heat_basis="热搜第 3 位",
                facts=[
                    Fact(
                        text="部分路段实行临时交通管制",
                        supporting_mids=[MID],
                        source_kind="weibo_post",
                    )
                ],
                opinions=["有用户认为通知太晚"],
                evidence=[
                    Evidence(
                        kind="weibo_post",
                        mid=MID,
                        author="深圳交警",
                        time="2026-11-18 09:00",
                        content="某路段临时管制，附现场图",
                        url=POST_URL,
                        verified=True,
                        note="详情页已复核",
                    )
                ],
                judgement="事实清楚",
                keywords=["APEC"],
            )
        ],
        limitations=[],
        gaps=[
            GapRecord(
                round=2,
                kind="NEED_EVIDENCE",
                target="深圳APEC峰会交通管制",
                detail="证据 1 条，低于下限 3 条",
                action="深圳APEC",
                resolved=False,
            ),
            GapRecord(
                round=2,
                kind="NEED_OFFICIAL",
                target="深圳APEC峰会交通管制",
                detail="缺官方口径",
                action="",
                resolved=None,
            ),
        ],
        stop_reason="预算耗尽（已搜索 2 组）",
        metrics=Metrics(posts_read=2, events_formed=1, evidence_items=1, llm_calls=1),
    )


def factcheck() -> FactCheckReport:
    return FactCheckReport(
        title="APEC 微博舆情 事实核查",
        checked_at=datetime(2026, 11, 18, 23, 40, 0),
        claims=[
            ClaimCheck(
                id="C1",
                claim="部分路段实行临时交通管制",
                verdict="部分证实",
                final_verdict="部分证实",
                confidence="中",
                analysis="有本地交警账号发布，但未见统一通告",
                supporting=[Cite(source_id="S1", point="交警提示绕行", support="supports")],
                counter=[Cite(source_id="S2", point="官网通告未提具体路段", support="unknown")],
                citation_check="supported",
                independent_groups=1,
            )
        ],
        sources=[
            Source(
                id="S1",
                kind="social_post",
                publisher="深圳交警",
                url=POST_URL,
                quote="某路段临时管制",
                http_status=200,
                reachable=True,
                quote_found=True,
            ),
            Source(
                id="S2",
                kind="official_account",
                publisher="交警官网",
                url=OTHER_URL,
                quote="通告原文",
                http_status=200,
                reachable=True,
                quote_found=True,
            ),
        ],
    )


class ApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.db = Path(self.tmp) / "workflow.db"
        self.artifacts = Path(self.tmp) / "artifacts"
        store, _ = open_store(self.db, self.artifacts)
        self.addCleanup(store.close)
        self.store = store
        self.run_id = self.seed(store)

    def seed(self, store) -> str:
        run_id = store.create_run("APEC 微博舆情", trigger="cli", config={"rounds": 3})
        inv_stage = store.begin_stage(run_id, "investigate")
        inv = investigation()
        store.save_investigation(inv, run_id, inv_stage, posts=posts())
        store.save_artifact(run_id, inv_stage, "report_md", inv.to_markdown())
        store.end_stage(inv_stage)

        handoff = store.begin_stage(run_id, "handoff", input_stage_id=inv_stage)
        store.end_stage(handoff)

        fc_stage = store.begin_stage(run_id, "factcheck", input_stage_id=handoff)
        store.save_factcheck(
            factcheck(),
            run_id,
            fc_stage,
            checked=True,
            artifacts={"factcheck_md": "# APEC 微博舆情 事实核查\n\nC1 部分证实\n"},
        )
        store.end_stage(fc_stage)
        store.finish_run(run_id, "succeeded", stop_reason=inv.stop_reason)

        sink = DbSink(store, run_id, inv_stage)
        sink.log("run_start", topic="APEC")
        sink.log("llm_call", purpose="event_build", ok=True, duration_ms=1200)
        sink.record_call(
            LLMCall(
                purpose="event_build",
                model="gpt-4.1-mini",
                base_url="",
                attempt=1,
                ok=True,
                duration_ms=1200,
                system="SYS",
                user="PROMPT " * 500,
                content='{"events":[]}',
                error="",
                prompt_digest="abc123",
                created_at="2026-11-18T23:31:00",
            )
        )
        sink.log("round_stop", reason="证据已足")
        return run_id

    def client(self) -> TestClient:
        from api.app import create_app

        return TestClient(create_app(self.db, self.artifacts))

    # ------------------------------------------------------------------ 基础
    def test_health_and_ready(self) -> None:
        with self.client() as c:
            self.assertEqual(c.get("/api/health").json(), {"status": "ok"})
            ready = c.get("/api/ready").json()
            self.assertTrue(ready["checks"]["database"]["ok"])
            self.assertTrue(ready["checks"]["artifacts"]["ok"])
            self.assertEqual(ready["blocking"], [])

    def test_runs_list_and_detail(self) -> None:
        with self.client() as c:
            listing = c.get("/api/runs").json()
            self.assertEqual(listing["count"], 1)
            self.assertEqual(listing["items"][0]["id"], self.run_id)

            detail = c.get(f"/api/runs/{self.run_id}").json()
            self.assertEqual(detail["topic"], "APEC 微博舆情")
            self.assertEqual(detail["status"], "succeeded")
            # 三阶段串成链，且顺序是 调查 → handoff → 核查
            chain = detail["provenance"]["chain"]
            self.assertEqual([x["stage"] for x in chain], ["investigate", "handoff", "factcheck"])
            self.assertTrue(detail["provenance"]["investigate_stage_id"])

    def test_unknown_run_is_404(self) -> None:
        with self.client() as c:
            self.assertEqual(c.get("/api/runs/run_nope").status_code, 404)

    def test_stages(self) -> None:
        with self.client() as c:
            stages = c.get(f"/api/runs/{self.run_id}/stages").json()["stages"]
            self.assertEqual([s["stage"] for s in stages], ["investigate", "handoff", "factcheck"])
            self.assertTrue(all(s["status"] == "succeeded" for s in stages))

    def test_gaps_keep_tri_state(self) -> None:
        with self.client() as c:
            data = c.get(f"/api/runs/{self.run_id}/gaps").json()
            self.assertEqual(data["count"], 2)
            # False=仍缺、None=无法判定，不能被压成同一个假
            self.assertEqual(data["summary"], {"resolved": 0, "unresolved": 1, "unknown": 1})
            by_kind = {g["kind"]: g for g in data["items"]}
            self.assertIs(by_kind["NEED_EVIDENCE"]["resolved"], False)
            self.assertIsNone(by_kind["NEED_OFFICIAL"]["resolved"])

    # ------------------------------------------------------------- 结论→原文
    def test_claims_link_to_source_document(self) -> None:
        with self.client() as c:
            data = c.get(f"/api/runs/{self.run_id}/claims").json()
            self.assertEqual(data["count"], 1)
            claim = data["items"][0]
            self.assertEqual(claim["effective"], "部分证实")
            by_key = {s["source_key"]: s for s in claim["sources"]}
            # S1 的 URL 是微博详情页：必须能靠 mid 归一链到原文快照
            self.assertTrue(by_key["S1"]["document_available"])
            doc_id = by_key["S1"]["document_id"]
            self.assertTrue(doc_id.startswith("doc_"))
            # S2 是外部官网，本次没抓过，如实说没有
            self.assertFalse(by_key["S2"]["document_available"])
            self.assertEqual(data["linked_sources"], 1)

            doc = c.get(f"/api/documents/{doc_id}").json()
            self.assertEqual(doc["text"], FULL_TEXT)
            self.assertEqual(doc["storage"], "inline")
            self.assertFalse(doc["excerpt_only"])
            self.assertEqual(doc["mid"], MID)

    def test_claims_404_for_run_without_claims(self) -> None:
        store, _ = open_store(Path(self.tmp) / "only_inv.db", self.artifacts)
        self.addCleanup(store.close)
        run_id = store.create_run("只有调查")
        stage = store.begin_stage(run_id, "investigate")
        store.save_investigation(investigation(), run_id, stage, posts=posts())
        store.end_stage(stage)
        from api.app import create_app

        with TestClient(create_app(Path(self.tmp) / "only_inv.db", self.artifacts)) as c:
            r = c.get(f"/api/runs/{run_id}/claims")
            self.assertEqual(r.status_code, 404)
            self.assertIn("没有核查结论", r.json()["detail"])

    # --------------------------------------------------------- 抓取与版本
    def test_document_history_counts_fetches_and_detects_change(self) -> None:
        with self.client() as c:
            doc_id = c.get(f"/api/runs/{self.run_id}/documents").json()["items"][0]["id"]

            first = c.get(f"/api/documents/{doc_id}/fetches").json()
            self.assertEqual(first["fetch_count"], 1)
            self.assertEqual(first["version_count"], 1)
            self.assertFalse(first["content_changed"])

        # 第二次运行抓到同样的正文：次数 +1，版本不变
        store, _ = open_store(self.db, self.artifacts)
        self.addCleanup(store.close)
        run2 = store.create_run("APEC 微博舆情")
        st2 = store.begin_stage(run2, "investigate")
        store.save_investigation(investigation(), run2, st2, posts=posts())
        store.end_stage(st2)
        with self.client() as c:
            second = c.get(f"/api/documents/{doc_id}/fetches").json()
            self.assertEqual(second["fetch_count"], 2)
            self.assertEqual(second["version_count"], 1)
            self.assertFalse(second["content_changed"])

        # 第三次正文被改：同 URL 出现第二个 hash，内容变过
        run3 = store.create_run("APEC 微博舆情")
        st3 = store.begin_stage(run3, "investigate")
        store.save_investigation(
            investigation(), run3, st3, posts=posts(FULL_TEXT + "（编辑补充：管制已解除）")
        )
        store.end_stage(st3)
        with self.client() as c:
            third = c.get(f"/api/documents/{doc_id}/fetches").json()
            self.assertEqual(third["version_count"], 2)
            self.assertTrue(third["content_changed"])
            self.assertEqual(sum(1 for v in third["versions"] if v["is_current"]), 1)

    def test_documents_list_flags_degraded_without_full_text(self) -> None:
        """历史导入的文档只有摘录，接口要标出来，不能让人以为是全文。"""
        store, _ = open_store(Path(self.tmp) / "degraded.db", self.artifacts)
        self.addCleanup(store.close)
        from store.importer import import_investigation

        src = Path(self.tmp) / "investigation_20260101.json"
        src.write_text(investigation().model_dump_json(), encoding="utf-8")
        run_id = import_investigation(store, src)
        from api.app import create_app

        with TestClient(create_app(Path(self.tmp) / "degraded.db", self.artifacts)) as c:
            page = c.get(f"/api/runs/{run_id}/documents").json()
            doc = page["items"][0]
            self.assertEqual(page["quality"], "degraded")
            # 有可读正文，但那只是当年的截断摘录，不能当全文审计
            self.assertTrue(doc["has_full_text"])
            self.assertTrue(doc["excerpt_only"])
            self.assertEqual(c.get(f"/api/runs/{run_id}").json()["quality"], "degraded")

    # --------------------------------------------------------------- 报告
    def test_report_renders_from_database(self) -> None:
        with self.client() as c:
            md = c.get(f"/api/runs/{self.run_id}/report?format=md")
            self.assertEqual(md.status_code, 200)
            self.assertEqual(md.headers["content-type"].split(";")[0], "text/markdown")
            # 调查阶段存在时优先现渲染，而不是回头看 output/ 或 artifact
            self.assertEqual(md.headers["x-report-source"], "rendered:investigation")
            self.assertIn("深圳APEC峰会交通管制", md.text)

            data = c.get(f"/api/runs/{self.run_id}/report?format=json").json()
            self.assertEqual(data["events"][0]["name"], "深圳APEC峰会交通管制")
            self.assertEqual(data["_source"], "rendered:database")

    def test_report_bad_format_rejected(self) -> None:
        with self.client() as c:
            self.assertEqual(c.get(f"/api/runs/{self.run_id}/report?format=pdf").status_code, 422)

    # --------------------------------------------------------------- 留痕
    def test_trace_is_ordered_and_filterable(self) -> None:
        with self.client() as c:
            data = c.get(f"/api/runs/{self.run_id}/trace").json()
            self.assertEqual(data["total"], 3)
            self.assertEqual([x["step"] for x in data["items"]], ["run_start", "llm_call", "round_stop"])

            only_llm = c.get(f"/api/runs/{self.run_id}/trace?step=llm_call").json()
            self.assertEqual(only_llm["total"], 1)
            self.assertEqual(only_llm["items"][0]["purpose"], "event_build")

            paged = c.get(f"/api/runs/{self.run_id}/trace?limit=1&offset=1").json()
            self.assertEqual(len(paged["items"]), 1)
            self.assertEqual(paged["items"][0]["step"], "llm_call")

    def test_llm_calls_default_to_summary_then_full_text_on_demand(self) -> None:
        with self.client() as c:
            listing = c.get(f"/api/runs/{self.run_id}/llm-calls").json()
            self.assertEqual(listing["total"], 1)
            item = listing["items"][0]
            # 列表不带全文：一次 event_build 就 17KB，20 条会拖死响应
            self.assertEqual(item["prompt"], "")
            self.assertEqual(item["request_chars"], len("SYS") + len("PROMPT " * 500))
            self.assertEqual(item["prompt_digest"], "abc123")

            full = c.get(f"/api/runs/{self.run_id}/llm-calls?include_text=true").json()
            self.assertIn("PROMPT", full["items"][0]["prompt"])

            one = c.get(f"/api/llm-calls/{item['id']}").json()
            self.assertTrue(one["prompt"].startswith("SYS\n\nPROMPT"))
            self.assertEqual(one["response"], '{"events":[]}')

    def test_llm_call_404(self) -> None:
        with self.client() as c:
            self.assertEqual(c.get("/api/llm-calls/llm_nope").status_code, 404)


if __name__ == "__main__":
    unittest.main()
