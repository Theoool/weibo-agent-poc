"""队列与 worker 的测试。

重点不在"能领到任务"，而在四个容易静默错的地方：

1. **抢占是原子的** —— 两个 worker 读到同一行是正常的，重复执行不是；
2. **租约过期能回收** —— worker 被杀之后任务不能永远卡在 running；
3. **失败不自动重跑** —— 崩溃（租约过期）才重试，业务异常要人来判断，
   否则一个必然失败的配置会被无限重跑；
4. **取消只在边界生效** —— 运行中取消不能留下半截记录。

全部用临时库，不需要浏览器：worker 接受注入的 handler。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from sqlalchemy import select

from store import RunStore, open_db
from store.models import StageRun, WorkerHeartbeat
from store.repositories import iso_after, now_iso

import worker as worker_module
from worker import StageResult, Worker


class QueueStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.engine, Session, _ = open_db(Path(self.tmp) / "queue.db")
        self.addCleanup(self.engine.dispose)
        self.store = RunStore(Session, blob_dir=Path(self.tmp) / "artifacts")

    def queued_run(self, topic: str = "APEC") -> tuple[str, dict]:
        run_id = self.store.create_run(topic, trigger="api", status="queued")
        job = self.store.enqueue(run_id, "investigate", payload={"topic": topic, "plan": ["investigate"]})
        return run_id, job

    # ---------------------------------------------------------------- 入队/领取
    def test_enqueue_sets_run_queued_and_is_idempotent(self) -> None:
        run_id, job = self.queued_run()
        self.assertEqual(job["status"], "queued")
        self.assertEqual(self.store.run_summary(run_id)["status"], "queued")

        again = self.store.enqueue(run_id, "investigate", payload={"topic": "别的"})
        self.assertEqual(again["id"], job["id"], "重复入队不该造出第二个任务")

    def test_claim_is_exclusive(self) -> None:
        run_id, job = self.queued_run()
        claimed = self.store.claim_job("wk_a", lease_sec=60)
        self.assertEqual(claimed["id"], job["id"])
        self.assertEqual(claimed["attempts"], 1)
        self.assertIsNone(self.store.claim_job("wk_b", lease_sec=60), "同一个任务不能被领两次")
        self.assertEqual(self.store.run_summary(run_id)["status"], "running")

    def test_claim_prefers_priority_then_fifo(self) -> None:
        low = self.store.create_run("低", trigger="api", status="queued")
        self.store.enqueue(low, "investigate", priority=0)
        high = self.store.create_run("高", trigger="api", status="queued")
        self.store.enqueue(high, "investigate", priority=5)
        self.assertEqual(self.store.claim_job("wk")["run_id"], high)
        self.assertEqual(self.store.claim_job("wk")["run_id"], low)

    def test_claim_respects_available_at(self) -> None:
        run_id = self.store.create_run("延迟", trigger="api", status="queued")
        self.store.enqueue(run_id, "investigate", delay_sec=60)
        self.assertIsNone(self.store.claim_job("wk"), "退避中的任务不该被领走")

    def test_stage_filter(self) -> None:
        run_id, _ = self.queued_run()
        self.assertIsNone(self.store.claim_job("wk", stages=["factcheck"]))
        self.assertIsNotNone(self.store.claim_job("wk", stages=["investigate"]))

    # ---------------------------------------------------------------- 租约
    def test_expired_lease_is_requeued(self) -> None:
        run_id, job = self.queued_run()
        claimed = self.store.claim_job("wk_a", lease_sec=-1)  # 领到即过期
        stage_id = self.store.begin_stage(run_id, "investigate")
        self.store.bind_job_stage(claimed["id"], stage_id)

        result = self.store.reclaim_expired()
        self.assertEqual(result["requeued"], [claimed["id"]])
        # 那次尝试必须被标失败，否则会留下一条永远 running 的阶段
        stages = self.store.run_summary(run_id)["stages"]
        self.assertEqual(stages[0]["status"], "failed")
        self.assertIn("租约", stages[0]["error"])

        again = self.store.claim_job("wk_b", lease_sec=60)
        self.assertEqual(again["attempts"], 2, "回收后重领要沿用尝试次数，不能清零")

    def test_retry_give_up_after_max_attempts(self) -> None:
        run_id = self.store.create_run("崩溃", trigger="api", status="queued")
        self.store.enqueue(run_id, "investigate", max_attempts=1)
        self.store.claim_job("wk_a", lease_sec=-1)

        result = self.store.reclaim_expired()
        self.assertEqual(len(result["failed"]), 1)
        job = self.store.job_of_run(run_id)
        self.assertEqual(job["status"], "failed")
        self.assertEqual(self.store.run_summary(run_id)["status"], "failed")
        self.assertIsNone(self.store.claim_job("wk_b"), "放弃之后不能再被领走")

    # ---------------------------------------------------------------- 取消
    def test_cancel_queued_job_never_runs(self) -> None:
        run_id, job = self.queued_run()
        result = self.store.request_cancel(run_id)
        self.assertTrue(result["cancelled"])
        self.assertIn("尚未开始", result["state"])
        self.assertIsNone(self.store.claim_job("wk"))
        self.assertEqual(self.store.run_summary(run_id)["status"], "cancelled")

    def test_cancel_running_job_only_sets_flag(self) -> None:
        run_id, job = self.queued_run()
        self.store.claim_job("wk")
        result = self.store.request_cancel(run_id)
        self.assertTrue(result["cancelled"])
        self.assertIn("轮次", result["state"])
        self.assertTrue(self.store.cancel_requested(job["id"]))
        # 状态还是 running：正在跑的进程要自己收尾
        self.assertEqual(self.store.run_summary(run_id)["status"], "running")

    def test_cancel_finished_run_is_rejected(self) -> None:
        run_id = self.store.create_run("完成", trigger="cli")
        self.store.begin_stage(run_id, "investigate")
        self.store.finish_run(run_id, "succeeded")
        self.assertFalse(self.store.request_cancel(run_id)["cancelled"])

    # ---------------------------------------------------------------- 心跳/概览
    def test_heartbeat_marks_worker_alive(self) -> None:
        self.assertFalse(self.store.queue_overview()["worker_alive"])
        self.store.heartbeat("wk_a", host="dev", pid=123)
        overview = self.store.queue_overview()
        self.assertTrue(overview["worker_alive"])
        self.assertEqual(overview["workers"][0]["worker_id"], "wk_a")

        # 心跳过期就不算在线：否则"进程早就没了"会一直显示成"在跑"
        self.store.heartbeat("wk_stale", host="dev", pid=1)
        with self.store.Session() as s:
            s.get(WorkerHeartbeat, "wk_stale").last_seen_at = iso_after(-600)
            s.commit()
        alive = [w["worker_id"] for w in self.store.queue_overview()["workers"]]
        self.assertEqual(alive, ["wk_a"])

    def test_queue_position(self) -> None:
        first = self.store.create_run("一", trigger="api", status="queued")
        self.store.enqueue(first, "investigate")
        second = self.store.create_run("二", trigger="api", status="queued")
        job = self.store.enqueue(second, "investigate")
        self.assertEqual(job["ahead"], 1)
        self.assertEqual(self.store.queue_overview()["counts"]["queued"], 2)


class WorkerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.engine, Session, _ = open_db(Path(self.tmp) / "worker.db")
        self.addCleanup(self.engine.dispose)
        self.store = RunStore(Session, blob_dir=Path(self.tmp) / "artifacts")
        self.logs: list[str] = []

    def make_worker(self, handlers: dict) -> Worker:
        return Worker(
            self.store,
            handlers,
            worker_id="wk_test",
            lease_sec=60,
            out_root=Path(self.tmp) / "out",
            log=self.logs.append,
        )

    def enqueue_chain(self, plan: list[str]) -> str:
        run_id = self.store.create_run("APEC", trigger="api", status="queued")
        self.store.enqueue(
            run_id,
            plan[0],
            payload={"topic": "APEC", "plan": plan, "index": 0, "options": {}},
        )
        return run_id

    def test_chain_advances_then_finishes(self) -> None:
        run_id = self.enqueue_chain(["investigate", "handoff"])
        order: list[str] = []

        def investigate(ctx):
            order.append("investigate")
            self.assertEqual(ctx.run_id, run_id)
            return StageResult(stop_reason="证据已达标", outputs={"events": 2})

        def handoff(ctx):
            order.append("handoff")
            # 上一阶段的产出要传到 payload 里，否则下游只能靠猜
            self.assertEqual(ctx.payload["outputs"]["events"], 2)
            return StageResult()

        worker = self.make_worker({"investigate": investigate, "handoff": handoff})

        self.assertIsNotNone(worker.run_once())
        mid = self.store.run_summary(run_id)
        self.assertEqual(mid["status"], "queued", "中间阶段结束后应等下一阶段，而不是直接成功")
        self.assertEqual([s["stage"] for s in mid["stages"]], ["investigate"])

        self.assertIsNotNone(worker.run_once())
        done = self.store.run_summary(run_id)
        self.assertEqual(done["status"], "succeeded")
        self.assertEqual(done["stop_reason"], "证据已达标")
        self.assertEqual([s["status"] for s in done["stages"]], ["succeeded", "succeeded"])
        self.assertEqual(order, ["investigate", "handoff"])
        self.assertIsNone(worker.run_once(), "队列空了应该返回 None")

    def test_input_stage_id_links_previous_stage(self) -> None:
        run_id = self.enqueue_chain(["investigate", "handoff"])
        worker = self.make_worker(
            {"investigate": lambda ctx: StageResult(), "handoff": lambda ctx: StageResult()}
        )
        worker.run_once()
        inv_stage = self.store.latest_stage(run_id, "investigate")
        worker.run_once()

        with self.store.Session() as s:
            handoff = s.execute(
                select(StageRun).where(StageRun.run_id == run_id, StageRun.stage == "handoff")
            ).scalars().first()
        self.assertEqual(handoff.input_stage_id, inv_stage, "阶段链断了，溯源就断了")

    def test_handler_failure_marks_run_failed(self) -> None:
        run_id = self.enqueue_chain(["investigate"])

        def boom(ctx):
            raise RuntimeError("浏览器启动失败：profile 被占用")

        worker = self.make_worker({"investigate": boom})
        worker.run_once()

        summary = self.store.run_summary(run_id)
        self.assertEqual(summary["status"], "failed")
        self.assertIn("profile 被占用", summary["error"])
        self.assertFalse(summary["partial"], "第一阶段就失败，没有任何可用的产物")
        self.assertEqual(summary["stages"][0]["status"], "failed")
        self.assertEqual(self.store.job_of_run(run_id)["status"], "failed")
        self.assertEqual(worker.jobs_failed, 1)

    def test_late_stage_failure_is_partial(self) -> None:
        run_id = self.store.create_run("APEC", trigger="api", status="queued")
        self.store.enqueue(
            run_id, "factcheck", payload={"plan": ["investigate", "factcheck"], "index": 1}
        )
        worker = self.make_worker(
            {"factcheck": lambda ctx: (_ for _ in ()).throw(RuntimeError("清单不存在"))}
        )
        worker.run_once()

        summary = self.store.run_summary(run_id)
        self.assertEqual(summary["status"], "failed")
        # 前序阶段（调查）的产物仍然有效，"核查挂了"和"调查挂了"必须能区分
        self.assertTrue(summary["partial"])

    def test_unknown_stage_fails_cleanly(self) -> None:
        run_id = self.store.create_run("APEC", trigger="api", status="queued")
        self.store.enqueue(run_id, "collect")
        worker = self.make_worker({})
        worker.run_once()
        summary = self.store.run_summary(run_id)
        self.assertEqual(summary["status"], "failed")
        self.assertIn("没有注册", summary["error"])

    def test_cancel_during_run_stops_at_boundary(self) -> None:
        run_id = self.enqueue_chain(["investigate", "handoff"])

        def investigate(ctx):
            # 模拟前端在采集过程中点了取消
            self.store.request_cancel(run_id)
            self.assertIsNotNone(ctx.should_stop(), "运行中的任务要能看到取消标记")
            return StageResult(stop_reason="证据已达标")

        worker = self.make_worker(
            {"investigate": investigate, "handoff": lambda ctx: StageResult()}
        )
        worker.run_once()

        summary = self.store.run_summary(run_id)
        self.assertEqual(summary["status"], "cancelled", "取消后不该再排下一阶段")
        self.assertEqual(summary["stages"][0]["status"], "cancelled")
        self.assertEqual(self.store.queue_overview()["counts"]["queued"], 0)

    def test_worker_records_heartbeat(self) -> None:
        self.make_worker({}).run_once()
        overview = self.store.queue_overview()
        self.assertTrue(overview["worker_alive"])
        self.assertEqual(overview["workers"][0]["worker_id"], "wk_test")

    def test_drain_stops_on_empty_queue(self) -> None:
        run_id = self.enqueue_chain(["investigate"])
        worker = self.make_worker({"investigate": lambda ctx: StageResult()})
        code = worker.run_forever(poll_sec=0, stop_when_empty=True)
        self.assertEqual(code, 0)
        self.assertEqual(self.store.run_summary(run_id)["status"], "succeeded")

    def test_factcheck_handler_reads_claims_from_artifact(self) -> None:
        """真实 handler：核查阶段的输入必须来自 handoff 写下的 artifact。

        这条不联网、不调模型（check=False / semantic=False），验的是"队列里
        没有固定文件路径可依赖"时，输入能不能拿对。
        """
        from agent.handoff import facts_to_claims
        from agent.schemas import Event, Evidence, Fact, Investigation
        from worker import build_handlers

        run_id = self.store.create_run("APEC", trigger="api", status="queued")
        inv_stage = self.store.begin_stage(run_id, "investigate")
        inv = Investigation(
            topic="APEC 微博舆情",
            mode="heuristic",
            run_id=run_id,
            events=[
                Event(
                    name="交通管制",
                    summary="多条微博提到临时管制",
                    facts=[Fact(text="部分路段实行临时交通管制")],
                    evidence=[
                        Evidence(
                            mid="5366736759032400",
                            url="https://m.weibo.cn/detail/5366736759032400",
                            author="深圳交警",
                            content="部分路段实行临时交通管制",
                        )
                    ],
                )
            ],
        )
        self.store.save_investigation(inv, run_id, inv_stage, posts={})
        self.store.end_stage(inv_stage)

        claims_path = Path(self.tmp) / "claims_handoff.json"
        claims_path.write_text(
            json.dumps(facts_to_claims(inv), ensure_ascii=False), encoding="utf-8"
        )
        self.store.save_artifact(run_id, inv_stage, "claims_json", claims_path.read_text("utf-8"))

        self.store.enqueue(
            run_id,
            "factcheck",
            payload={"plan": ["factcheck"], "index": 0,
                     "options": {"check": False, "semantic": False}},
        )

        class FakeLLM:
            available = False
            model = "fake"

            def attach_tracer(self, tracer) -> None:
                self.tracer = tracer

        worker = Worker(
            self.store,
            build_handlers(llm_factory=FakeLLM),
            worker_id="wk_test",
            out_root=Path(self.tmp) / "out",
            log=self.logs.append,
        )
        worker.run_once()

        summary = self.store.run_summary(run_id)
        self.assertEqual(summary["status"], "succeeded", summary["error"])
        self.assertEqual(summary["stages"][-1]["stage"], "factcheck")
        self.assertEqual(summary["counts"]["claims"], 1, "结论没有落库")


class StageResultTest(unittest.TestCase):
    def test_stage_result_defaults(self) -> None:
        r = StageResult()
        self.assertEqual(r.stop_reason, "")
        self.assertEqual(r.outputs, {})
        self.assertEqual(worker_module.STAGE_LABEL["factcheck"], "事实核查")
        self.assertTrue(now_iso())
