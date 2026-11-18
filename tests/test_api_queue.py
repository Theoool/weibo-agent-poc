"""M4 验收：一条 curl 起一次调查，轮询到完成。

这里不真跑浏览器（worker 的处理器可注入，已在 `test_queue.py` 覆盖）。
API 层要验的是四件事：

1. `POST /runs` 只入队、立刻返回 202，不占着连接等 3 分钟；
2. 参数越界要当场拒绝，而不是让 worker 在微博上翻两小时；
3. 排队中的取消立刻生效，运行中的取消只打标记；
4. 重跑要沿用上一次的预算，否则两次结果没法对比。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from store import open_store


class QueueApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.db = Path(self.tmp) / "api-queue.db"
        self.artifacts = Path(self.tmp) / "artifacts"
        store, _ = open_store(self.db, self.artifacts)
        self.addCleanup(store.close)
        self.store = store

    def client(self) -> TestClient:
        from api.app import create_app

        return TestClient(create_app(self.db, self.artifacts))

    def create(self, client, **overrides) -> dict:
        body = {"topic": "APEC", **overrides}
        r = client.post("/api/runs", json=body)
        self.assertEqual(r.status_code, 202, r.text)
        return r.json()

    def fail_stage(self, run_id: str, stage: str = "investigate") -> None:
        """把任务领走并判失败，模拟 worker 跑挂了。"""
        job = self.store.claim_job("wk_test", lease_sec=60)
        stage_id = self.store.begin_stage(run_id, stage)
        self.store.bind_job_stage(job["id"], stage_id)
        self.store.end_stage(stage_id, "failed", "浏览器启动失败")
        self.store.finish_job(job["id"], "failed", "浏览器启动失败")
        self.store.finish_run(run_id, "failed", error="浏览器启动失败")

    # ------------------------------------------------------------------ 创建
    def test_create_enqueues_and_returns_202(self) -> None:
        with self.client() as c:
            body = self.create(c)
            self.assertEqual(body["status"], "queued")
            self.assertEqual(body["stages"], ["investigate", "handoff"])
            self.assertEqual(body["queue"]["position"], 1)
            # 没有 worker 时必须说出来，否则客户端会一直等一个不会开始的任务
            self.assertFalse(body["queue"]["worker_alive"])
            self.assertIn("worker", body["note"])

            run = c.get(f"/api/runs/{body['run_id']}").json()
            self.assertEqual(run["status"], "queued")
            self.assertEqual(run["stages"], [], "还在排队，不该有阶段记录")
            self.assertEqual(run["queue"]["id"], body["job_id"])
            self.assertEqual(run["config"]["topic"], "APEC")

    def test_create_with_worker_alive_has_no_warning(self) -> None:
        self.store.heartbeat("wk_live", host="dev", pid=9)
        with self.client() as c:
            body = self.create(c)
            self.assertTrue(body["queue"]["worker_alive"])
            self.assertEqual(body["note"], "")

    def test_create_rejects_bad_requests(self) -> None:
        with self.client() as c:
            cases = [
                ({"topic": "   "}, "不能为空"),
                ({"topic": "APEC", "stages": []}, "stages"),
                ({"topic": "APEC", "stages": ["factcheck"]}, "handoff"),
                ({"topic": "APEC", "stages": ["handoff", "investigate"]}, "顺序"),
                ({"topic": "APEC", "stages": ["investigate", "investigate"]}, "重复"),
                ({"topic": "APEC", "options": {"rounds": 99}}, "rounds"),
                ({"topic": "APEC", "options": {"max_pages": 5000}}, "max_pages"),
                ({"topic": "APEC", "seed_keywords": ["a"] * 20}, "seed_keywords"),
            ]
            for payload, hint in cases:
                r = c.post("/api/runs", json=payload)
                self.assertEqual(r.status_code, 422, f"{payload} 应该被拒绝")
                self.assertIn(hint, r.text, f"{payload} 的报错应提到 {hint}")

    def test_create_keeps_options_and_seed_keywords(self) -> None:
        with self.client() as c:
            body = self.create(
                c,
                seed_keywords=["深圳APEC", "  "],
                options={"rounds": 2, "max_searches": 4, "min_evidence": 2, "semantic": False},
            )
            run = c.get(f"/api/runs/{body['run_id']}").json()
            self.assertEqual(run["config"]["seed_keywords"], ["深圳APEC"])
            self.assertEqual(run["config"]["options"]["rounds"], 2)
            job = self.store.job_of_run(body["run_id"])
            self.assertEqual(job["payload"]["options"]["max_searches"], 4)
            self.assertEqual(job["payload"]["topic"], "APEC")

    # ------------------------------------------------------------------ 取消
    def test_cancel_queued_run(self) -> None:
        with self.client() as c:
            body = self.create(c)
            r = c.post(f"/api/runs/{body['run_id']}/cancel")
            self.assertEqual(r.status_code, 202)
            self.assertIn("尚未开始", r.json()["state"])
            self.assertEqual(c.get(f"/api/runs/{body['run_id']}").json()["status"], "cancelled")

    def test_cancel_running_run_reports_boundary(self) -> None:
        with self.client() as c:
            body = self.create(c)
            self.store.claim_job("wk_test", lease_sec=60)
            r = c.post(f"/api/runs/{body['run_id']}/cancel")
            self.assertEqual(r.status_code, 202)
            self.assertIn("轮次", r.json()["state"])
            # 状态仍是 running：正在跑的进程要自己收尾，不能由 API 单方面改
            self.assertEqual(c.get(f"/api/runs/{body['run_id']}").json()["status"], "running")

    def test_cancel_finished_run_409(self) -> None:
        with self.client() as c:
            body = self.create(c)
            self.fail_stage(body["run_id"])
            r = c.post(f"/api/runs/{body['run_id']}/cancel")
            self.assertEqual(r.status_code, 409)

    # ------------------------------------------------------------------ 重跑
    def test_retry_while_queued_is_rejected(self) -> None:
        with self.client() as c:
            body = self.create(c)
            r = c.post(f"/api/runs/{body['run_id']}/retry")
            self.assertEqual(r.status_code, 409)
            self.assertIn("排队", r.json()["detail"])

    def test_retry_after_failure_requeues_same_stage(self) -> None:
        with self.client() as c:
            body = self.create(
                c, options={"rounds": 2, "max_searches": 5, "min_evidence": 2}
            )
            self.fail_stage(body["run_id"])

            r = c.post(f"/api/runs/{body['run_id']}/retry")
            self.assertEqual(r.status_code, 202, r.text)
            retried = r.json()
            self.assertEqual(retried["stage"], "investigate")
            self.assertEqual(retried["status"], "queued")

            run = c.get(f"/api/runs/{body['run_id']}").json()
            self.assertEqual(run["status"], "queued")
            # 旧的那次尝试必须留着：重跑是新增一次 attempt，不是覆盖历史
            self.assertEqual([s["status"] for s in run["stages"]], ["failed"])

            job = self.store.job_of_run(body["run_id"])
            self.assertEqual(job["payload"]["plan"], ["investigate"])
            self.assertEqual(job["payload"]["options"]["rounds"], 2, "重跑要沿用上次预算")
            self.assertGreater(job["priority"], 0, "人工重跑应该插队")

    def test_retry_all_succeeded_needs_explicit_stage(self) -> None:
        with self.client() as c:
            body = self.create(c)
            job = self.store.claim_job("wk_test", lease_sec=60)
            stage_id = self.store.begin_stage(body["run_id"], "investigate")
            self.store.bind_job_stage(job["id"], stage_id)
            self.store.end_stage(stage_id, "succeeded")
            self.store.finish_job(job["id"], "succeeded")
            self.store.finish_run(body["run_id"], "succeeded")

            r = c.post(f"/api/runs/{body['run_id']}/retry")
            self.assertEqual(r.status_code, 409)
            self.assertIn("显式指定", r.json()["detail"])

            ok = c.post(f"/api/runs/{body['run_id']}/retry", json={"stage": "handoff"})
            self.assertEqual(ok.status_code, 202, ok.text)
            self.assertEqual(ok.json()["stage"], "handoff")

    def test_unknown_run_404(self) -> None:
        with self.client() as c:
            for method, path in (
                ("post", "/api/runs/run_nope/retry"),
                ("post", "/api/runs/run_nope/cancel"),
            ):
                self.assertEqual(getattr(c, method)(path).status_code, 404)

    # ------------------------------------------------------------------ 队列视图
    def test_queue_overview(self) -> None:
        with self.client() as c:
            self.create(c)
            self.create(c, topic="另一件事")
            body = c.get("/api/queue").json()
            self.assertEqual(body["counts"]["queued"], 2)
            self.assertFalse(body["worker_alive"])
            self.assertIn("worker", body["hint"])
            self.assertEqual(len(body["recent"]), 2)
            # recent 最新在前：后建的任务 ahead=1，先建的 ahead=0
            self.assertEqual([j["ahead"] for j in body["recent"]], [1, 0])

    def test_ready_reports_queue_backlog_without_worker(self) -> None:
        with self.client() as c:
            self.create(c)
            ready = c.get("/api/ready").json()
            self.assertIn("worker", ready["checks"])
            self.assertFalse(ready["checks"]["worker"]["ok"])
            # 但没有 worker 不该让整个服务 not ready：查历史完全不受影响
            self.assertNotIn("worker", ready["blocking"])


if __name__ == "__main__":
    unittest.main()
