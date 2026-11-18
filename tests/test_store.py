"""持久层测试。

重点不在"能写进去"，而在两件事：
1. **读回来还能渲染出同一份报告** —— 这是 schema 够不够用的唯一硬标准。
2. **分级规则真的生效** —— 正文快照不被截断后的摘录覆盖、同一内容跨 run 只存一份、
   跳过机检时不留下假的"不可达"记录。

全部用临时目录，不碰 data/ 和 artifacts/。Windows 上临时目录要等 engine
dispose 之后才能删，所以统一走 setUp 里的 cleanup 顺序（后加先执行）。
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError

from agent.schemas import Event, Evidence, Fact, GapRecord, Investigation, Limitation, Metrics
from agent.schemas import SearchRecord as DomainSearch
from factcheck.schemas import ClaimCheck, Cite, FactCheckReport, Source
from store import RunStore, open_db
from store.db import create_db_engine, migrate
from store.models import Base, DocumentFetch, SourceCheck, SourceDocument

TRUNCATED = "某路段临时管制，附现场图"


class StoreTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.engine, Session, _ = open_db(Path(self.tmp) / "test.db")
        # dispose 必须早于临时目录清理，否则 Windows 删不掉还被占用的 .db
        self.addCleanup(self.engine.dispose)
        self.store = RunStore(Session, blob_dir=Path(self.tmp) / "artifacts")

    def posts(self) -> dict[str, dict]:
        full_text = (
            "深圳 APEC 期间部分路段实行临时交通管制，交警提醒提前规划路线。"
            "这是完整的帖子正文，长度明显超过报告里那段截断摘录，用来验证全文快照确实落库了。"
        )
        return {
            "m1": {
                "mid": "m1",
                "author": "深圳交警",
                "time": "2026-11-18 09:00",
                "text": full_text,
                "url": "https://m.weibo.cn/detail/1",
                "verified": True,
                "detail_verified": True,
            },
            "m2": {
                "mid": "m2",
                "author": "路人甲",
                "time": "2026-11-18 09:30",
                "text": "早上堵了一个小时，管制确实有。",
                "url": "https://m.weibo.cn/detail/2",
                "verified": False,
                "detail_verified": False,
            },
        }

    def investigation(self) -> Investigation:
        return Investigation(
            topic="APEC 微博舆情",
            generated_at=datetime(2026, 11, 18, 22, 48, 49),
            mode="llm",
            run_id="",
            events=[
                Event(
                    name="深圳APEC峰会交通管制",
                    summary="多条微博提到临时管制",
                    heat="high",
                    heat_basis="热搜第 3 位",
                    facts=[
                        Fact(
                            text="部分路段实行临时交通管制",
                            supporting_mids=["m1"],
                            source_kind="weibo_post",
                        ),
                        Fact(
                            text="样本内共读到 2 条相关帖子",
                            kind="meta",
                            supporting_mids=["m1", "m2"],
                        ),
                    ],
                    opinions=["有用户认为通知太晚"],
                    speculations=["后续可能扩大管制范围"],
                    evidence=[
                        Evidence(
                            kind="weibo_post",
                            mid="m1",
                            author="深圳交警",
                            time="2026-11-18 09:00",
                            content=TRUNCATED,
                            url="https://m.weibo.cn/detail/1",
                            verified=True,
                            note="详情页已复核（作者/时间/正文一致）",
                        )
                    ],
                    official_sources=[
                        Evidence(
                            kind="official_site",
                            mid="",
                            author="深圳交警官方",
                            time="2026-11-18",
                            content="通告原文",
                            url="https://example.com/notice?id=1",
                        )
                    ],
                    judgement="事实清楚，观点分歧不大",
                    keywords=["APEC", "交通管制"],
                    merged_from=["APEC交通管制"],
                )
            ],
            searches=[
                DomainSearch(
                    keyword="APEC", url="https://m.weibo.cn/search?q=APEC",
                    posts_read=18, new_posts=18, round=1,
                ),
                DomainSearch(keyword="深圳APEC", posts_read=12, new_posts=3, round=2),
            ],
            limitations=[Limitation(step="登录态", detail="以游客态运行")],
            gaps=[
                GapRecord(round=2, kind="NEED_EVIDENCE", target="深圳APEC峰会交通管制",
                          detail="证据 1 条，低于下限 3 条", action="APEC", resolved=False),
                GapRecord(round=2, kind="NEED_OFFICIAL", target="深圳APEC峰会交通管制",
                          detail="缺官方口径", action="", resolved=None),
            ],
            stop_reason="预算耗尽（已搜索 2 组）",
            metrics=Metrics(
                keywords_searched=2,
                posts_read=2,
                events_formed=1,
                evidence_items=2,
                rounds_used=2,
                gaps_detected=2,
                llm_calls=3,
                llm_ms=4200,
            ),
        )

    def save_sample(self) -> str:
        run_id = self.store.create_run("APEC")
        stage_id = self.store.begin_stage(run_id, "investigate")
        self.store.save_investigation(self.investigation(), run_id, stage_id, posts=self.posts())
        self.store.end_stage(stage_id)
        self.store.finish_run(run_id, stop_reason="预算耗尽（已搜索 2 组）")
        return run_id


class SchemaTest(StoreTestBase):
    def test_tables_match_models(self) -> None:
        """DDL 是权威，ORM 是映射。两边漂移必须在这里炸出来。"""
        self.engine2 = create_db_engine(Path(self.tmp) / "schema.db")
        self.addCleanup(self.engine2.dispose)
        migrate(self.engine2)
        insp = inspect(self.engine2)
        actual = set(insp.get_table_names()) - {"sqlite_sequence", "schema_version"}
        expected = set(Base.metadata.tables)
        self.assertEqual(actual, expected)
        for table in expected:
            db_cols = {c["name"] for c in insp.get_columns(table)}
            model_cols = {c.name for c in Base.metadata.tables[table].columns}
            self.assertEqual(db_cols, model_cols, f"{table} 列不一致")

    def test_migrate_is_idempotent(self) -> None:
        """第二次打开不能重放迁移。

        版本号原来靠 SQL 文件自己写 schema_version，ALTER 类迁移漏写就会
        每次打开都重放一遍，第二次直接 duplicate column 起不来。
        """
        path = Path(self.tmp) / "idem.db"
        first = create_db_engine(path)
        applied = migrate(first)
        self.assertTrue(applied, "首次应至少应用 0001")
        first.dispose()

        second = create_db_engine(path)
        self.addCleanup(second.dispose)
        self.assertEqual(migrate(second), [], "已应用的迁移被重放了")
        # 重放会炸在这里（report_at 已存在），所以上面那句通过等价于列没被重复加
        self.assertTrue(inspect(second).get_columns("workflow_run"))

    def test_foreign_keys_are_enforced(self) -> None:
        """SQLite 默认不检查外键，pragma 没生效的话这里会静默通过。"""
        from store.models import StageRun

        with self.store.Session() as s:
            s.add(StageRun(id="s1", run_id="不存在的run", stage="investigate", started_at="now"))
            with self.assertRaises(IntegrityError):
                s.commit()


class RunLifecycleTest(StoreTestBase):
    def test_create_begin_finish(self) -> None:
        run_id = self.store.create_run("APEC", config={"rounds": 3})
        stage_id = self.store.begin_stage(run_id, "investigate")
        self.store.end_stage(stage_id)
        self.store.finish_run(run_id, stop_reason="证据已达标")

        summary = self.store.run_summary(run_id)
        self.assertEqual(summary["status"], "succeeded")
        self.assertEqual(summary["stop_reason"], "证据已达标")
        self.assertEqual(len(summary["stages"]), 1)
        self.assertEqual(summary["stages"][0]["status"], "succeeded")

    def test_rerun_increments_attempt_and_reopens_run(self) -> None:
        run_id = self.store.create_run("APEC")
        first = self.store.begin_stage(run_id, "investigate")
        self.store.end_stage(first)
        self.store.finish_run(run_id)
        second = self.store.begin_stage(run_id, "investigate")

        summary = self.store.run_summary(run_id)
        attempts = sorted(x["attempt"] for x in summary["stages"])
        self.assertEqual(attempts, [1, 2])
        self.assertNotEqual(first, second)
        # 重跑期间 run 不该继续显示"已结束"
        self.assertEqual(summary["status"], "running")


class InvestigationRoundTripTest(StoreTestBase):
    def test_full_text_snapshot_is_kept(self) -> None:
        """报告里引的是截断摘录，库里必须留全文。这是这次改造的核心动机之一。"""
        self.save_sample()
        with self.engine.connect() as conn:
            rows = conn.execute(
                select(SourceDocument.mid, SourceDocument.text_inline).where(
                    SourceDocument.mid == "m1"
                )
            ).all()
        self.assertEqual(len(rows), 1)
        stored = rows[0][1]
        self.assertNotEqual(stored, TRUNCATED, "全文被截断摘录覆盖了")
        self.assertIn("完整", stored)
        self.assertGreater(len(stored), len(TRUNCATED))

    def test_readback_renders_same_report(self) -> None:
        """从库里重建的报告必须与原始报告逐字一致，包括生成时间。

        这条是 schema 够不够用的硬标准：少存一个字段，这里就会差一行。
        """
        run_id = self.save_sample()
        original = self.investigation()
        loaded = self.store.load_investigation(run_id)
        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(loaded.run_id, run_id)
        self.assertEqual(loaded.mode, "llm")
        self.assertEqual(len(loaded.events), 1)
        self.assertEqual(loaded.to_markdown(), original.to_markdown())

    def test_fact_support_and_meta_kind_survive(self) -> None:
        run_id = self.save_sample()
        loaded = self.store.load_investigation(run_id)
        assert loaded is not None
        facts = loaded.events[0].facts
        self.assertEqual(facts[0].supporting_mids, ["m1"])
        self.assertEqual(facts[0].kind, "claim")
        # meta 事实是"样本内统计"，handoff 时必须能被识别出来跳过
        self.assertEqual(facts[1].kind, "meta")

    def test_evidence_order_is_preserved(self) -> None:
        """证据顺序有意义（报告按它编号、详情复核只取前 N 条），不能靠主键顺序碰运气。"""
        run_id = self.store.create_run("APEC")
        stage_id = self.store.begin_stage(run_id, "investigate")
        inv = self.investigation()
        order = ["m3", "m1", "m2"]
        inv.events[0].evidence = [
            Evidence(
                kind="weibo_post",
                mid=mid,
                author=f"作者{mid}",
                content=f"内容{mid}",
                url=f"https://m.weibo.cn/detail/{mid}",
            )
            for mid in order
        ]
        posts = {
            mid: {"mid": mid, "author": f"作者{mid}", "text": f"内容{mid}",
                  "url": f"https://m.weibo.cn/detail/{mid}"}
            for mid in order
        }
        self.store.save_investigation(inv, run_id, stage_id, posts=posts)

        loaded = self.store.load_investigation(run_id)
        assert loaded is not None
        self.assertEqual([e.mid for e in loaded.events[0].evidence], order)

    def test_gap_resolved_tristate(self) -> None:
        """resolved=None 表示无法判定，不能读成 False。"""
        run_id = self.save_sample()
        loaded = self.store.load_investigation(run_id)
        assert loaded is not None
        self.assertEqual([g.resolved for g in loaded.gaps], [False, None])


class DocumentDedupTest(StoreTestBase):
    def test_same_content_across_runs_is_stored_once(self) -> None:
        self.save_sample()
        self.save_sample()
        with self.engine.connect() as conn:
            docs = conn.execute(select(DocumentFetch.document_id)).all()
        # 每条帖子 2 次运行 = 4 次抓取；官方来源 2 次 = 2 次抓取，合计 6 次
        self.assertEqual(len(docs), 6)
        # 但内容只有 3 份：m1、m2、官方通告
        self.assertEqual(len({d[0] for d in docs}), 3)

    def test_url_variants_with_same_mid_merge(self) -> None:
        from store.repositories import canonical_url

        a = canonical_url("https://m.weibo.cn/detail/123", "123")
        b = canonical_url("https://weibo.com/1/ABC", "123")
        self.assertEqual(a, b)
        self.assertNotEqual(
            canonical_url("https://example.com/a?id=1"),
            canonical_url("https://example.com/a?id=2"),
        )

    def test_long_text_goes_to_blob(self) -> None:
        run_id = self.store.create_run("APEC")
        stage_id = self.store.begin_stage(run_id, "investigate")
        long_post = dict(self.posts()["m1"])
        long_post["text"] = "长正文" * 3000  # 远超 INLINE_LIMIT
        self.store.save_investigation(
            self.investigation(), run_id, stage_id, posts={"m1": long_post}
        )
        with self.engine.connect() as conn:
            row = conn.execute(
                select(SourceDocument.text_ref, SourceDocument.text_inline).where(
                    SourceDocument.mid == "m1"
                )
            ).first()
        assert row is not None
        self.assertTrue(row[0], "长正文应写入内容寻址目录")
        self.assertIsNone(row[1])
        self.assertTrue(self.store.blobs.exists(row[0]))
        self.assertEqual(self.store.blobs.get_text(row[0]), long_post["text"])


class FactcheckStoreTest(StoreTestBase):
    def report(self) -> FactCheckReport:
        return FactCheckReport(
            title="APEC 核查",
            claims=[
                ClaimCheck(
                    id="C1",
                    claim="部分路段实行临时管制",
                    verdict="已证实",
                    confidence="高",
                    final_verdict="已证实",
                    independent_groups=1,
                    citation_check="supported",
                    supporting=[Cite(source_id="S1", point="官方通告", support="supports")],
                )
            ],
            sources=[
                Source(
                    id="S1",
                    kind="official_account",
                    publisher="深圳交警",
                    url="https://example.com/notice?id=1",
                    quote="临时交通管制",
                    independence_group="official",
                    http_status=200,
                    reachable=True,
                    quote_found=True,
                    page_title="通告",
                )
            ],
            summary={"independently_verified": 1, "unverifiable": 0},
        )

    def test_saves_claims_sources_and_checks(self) -> None:
        run_id = self.store.create_run("APEC")
        stage_id = self.store.begin_stage(run_id, "factcheck")
        stats = self.store.save_factcheck(self.report(), run_id, stage_id, checked=True)

        self.assertEqual(stats["claims"], 1)
        self.assertEqual(stats["sources"], 1)
        self.assertEqual(stats["checks"], 1)
        self.assertEqual(stats["citations"], 1)
        summary = self.store.run_summary(run_id)
        self.assertEqual(summary["counts"]["claims"], 1)
        self.assertEqual(summary["metrics"]["fc_independently_verified"], 1.0)

    def test_skip_checks_writes_no_fake_unreachable(self) -> None:
        """跳过机检时不能留下"不可达"记录，否则和真的校验失败无法区分。"""
        run_id = self.store.create_run("APEC")
        stage_id = self.store.begin_stage(run_id, "factcheck")
        stats = self.store.save_factcheck(self.report(), run_id, stage_id, checked=False)
        self.assertEqual(stats["checks"], 0)
        with self.engine.connect() as conn:
            self.assertEqual(len(conn.execute(select(SourceCheck.id)).all()), 0)


class ImportTest(StoreTestBase):
    def test_imported_run_is_marked_degraded(self) -> None:
        import json

        from store.importer import import_investigation

        path = Path(self.tmp) / "investigation_20260101_000000.json"
        path.write_text(
            json.dumps(self.investigation().model_dump(mode="json"), ensure_ascii=False),
            encoding="utf-8",
        )
        run_id = import_investigation(self.store, path)
        summary = self.store.run_summary(run_id)
        self.assertEqual(summary["quality"], "degraded")
        self.assertEqual(summary["trigger"], "import")
        self.assertEqual(summary["counts"]["events"], 1)

    def test_handoff_payload_is_not_mistaken_for_factcheck(self) -> None:
        """handoff 产物和核查报告都带 claims/sources，只能靠 checked_at 区分。

        分错的后果很具体：待核查清单会被当成"已经核过的结论"写进库。
        """
        import json

        from store.importer import import_file

        payload = {
            "title": "APEC 事实核查（由调查产物自动生成）",
            "source_run_id": "",
            "claims": [
                {
                    "id": "C1",
                    "claim": "部分路段实行临时管制",
                    "verdict": "待核查",
                    "confidence": "低",
                    "supporting": [{"source_id": "S1", "point": "报告标注的支撑证据"}],
                }
            ],
            "sources": [
                {
                    "id": "S1",
                    "kind": "social_post",
                    "publisher": "深圳交警",
                    "url": "https://m.weibo.cn/detail/1",
                }
            ],
            "handoff": {"events": 1, "facts": 1},
        }
        path = Path(self.tmp) / "claims_handoff.json"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

        run_id, kind = import_file(self.store, path)
        self.assertEqual(kind, "handoff")
        summary = self.store.run_summary(run_id)
        self.assertEqual([s["stage"] for s in summary["stages"]], ["handoff"])
        # 待核查清单从没被校验过，不能留下机检记录
        self.assertEqual(len(self._checks()), 0)

    def _checks(self) -> list:
        with self.engine.connect() as conn:
            return list(conn.execute(select(SourceCheck.id)).all())

    def test_import_dir_skips_raw_posts(self) -> None:
        import json

        from store.importer import import_dir

        folder = Path(self.tmp) / "output"
        folder.mkdir()
        (folder / "investigation.json").write_text(
            json.dumps(self.investigation().model_dump(mode="json"), ensure_ascii=False),
            encoding="utf-8",
        )
        (folder / "day1_APEC.json").write_text(
            json.dumps({"posts": []}, ensure_ascii=False), encoding="utf-8"
        )
        results = import_dir(self.store, folder, log=lambda *_: None)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0][1], "investigate")


if __name__ == "__main__":
    unittest.main()
