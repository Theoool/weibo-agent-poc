"""worker：队列消费者。**独立进程**，不跟 uvicorn 一起跑。

为什么必须分开（见 `docs/architecture.md` §4.4）：Playwright 是同步的，
一个专用 Chrome profile 同一时刻只能被一个进程占用；把它塞进 API 进程，
`--reload` 会杀掉任务，进程重启会丢任务，异步事件循环还会被同步浏览器阻塞。

这个进程只做四件事：领任务、建阶段记录、跑阶段、推进到下一阶段。
业务判断一行都没有——全在 `workflow/` 和 `agent/` 里。

启动：

    uv run python -m worker                 # 常驻，轮询队列
    uv run python -m worker --once          # 只处理一个任务就退出
    uv run python -m worker --drain         # 处理到队列为空再退出（适合定时任务）
"""

from __future__ import annotations

import argparse
import os
import socket
import sys
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv  # noqa: E402

from workflow import (  # noqa: E402
    OUTPUT_ROOT,
    RunContext,
    build_sinks,
    execute_factcheck,
    execute_handoff,
    execute_investigate,
    out_dir_for,
)

STAGE_LABEL = {"investigate": "调查", "handoff": "待核查清单", "factcheck": "事实核查"}


@dataclass
class StageResult:
    """一个阶段的产出摘要。worker 用它决定"下一阶段跑不跑、run 怎么收尾"。"""

    stop_reason: str = ""
    partial: bool = False
    outputs: dict = field(default_factory=dict)
    note: str = ""


Handler = Callable[[RunContext], StageResult]


# --------------------------------------------------------------------------- #
# 租约续期
# --------------------------------------------------------------------------- #
class _LeaseKeeper(threading.Thread):
    """后台续租 + 心跳。

    一次调查可能跑十几分钟，而租约默认 30 分钟——看着够，但补搜轮次多的时候
    会超。没有续租，另一个 worker（或同一进程重启后）会把还在跑的任务判成
    "失联"再跑一遍，同一份报告写两遍。

    心跳也在这里发：只在 `run_once` 开头打一次的话，一条跑 3 分钟的任务会让
    `/api/ready` 在第 91 秒起显示"没有在线 worker"——明明正在跑。
    """

    def __init__(self, store, job_id: str, lease_sec: int, heartbeat=None) -> None:
        super().__init__(daemon=True, name=f"lease-{job_id}")
        self.store = store
        self.job_id = job_id
        self.lease_sec = lease_sec
        self.heartbeat = heartbeat
        self._stop = threading.Event()

    def run(self) -> None:  # pragma: no cover - 线程体，靠 stop 行为验证
        # 心跳窗口是 90 秒，间隔必须明显小于它
        interval = max(5, min(60, self.lease_sec // 3))
        while not self._stop.wait(interval):
            try:
                self.store.renew_lease(self.job_id, self.lease_sec)
            except Exception:  # noqa: BLE001
                # 续租失败不致命：租约还没过期，下一次再试
                pass
            if self.heartbeat is not None:
                try:
                    self.heartbeat()
                except Exception:  # noqa: BLE001
                    pass

    def stop(self) -> None:
        self._stop.set()


# --------------------------------------------------------------------------- #
# Worker
# --------------------------------------------------------------------------- #
class Worker:
    def __init__(
        self,
        store,
        handlers: dict[str, Handler] | None = None,
        *,
        worker_id: str = "",
        lease_sec: int = 1800,
        out_root: str | Path | None = None,
        log: Callable[[str], None] = print,
    ) -> None:
        self.store = store
        self.handlers = handlers or build_handlers()
        self.worker_id = worker_id or (
            f"wk_{socket.gethostname().split('.')[0][:12]}_{os.getpid()}"
        )
        self.lease_sec = lease_sec
        self.out_root = Path(out_root) if out_root else OUTPUT_ROOT
        self.log = log
        self.jobs_done = 0
        self.jobs_failed = 0

    # ------------------------------------------------------------------ 主循环
    def _heartbeat(self, last_error: str = "") -> None:
        self.store.heartbeat(
            self.worker_id,
            host=socket.gethostname(),
            pid=os.getpid(),
            jobs_done=self.jobs_done,
            last_error=last_error,
        )

    def run_once(self) -> str | None:
        """领一个任务并执行。没有任务返回 None。"""
        self._heartbeat()

        reclaimed = self.store.reclaim_expired()
        if reclaimed["requeued"]:
            self.log(f"[queue] 回收失联任务 {len(reclaimed['requeued'])} 个，重新排队")
        if reclaimed["failed"]:
            self.log(f"[queue] {len(reclaimed['failed'])} 个任务重试次数用尽，标记失败")

        job = self.store.claim_job(self.worker_id, lease_sec=self.lease_sec)
        if job is None:
            return None
        self._execute(job)
        return job["id"]

    def run_forever(self, poll_sec: float = 2.0, max_jobs: int | None = None,
                    stop_when_empty: bool = False) -> int:
        idle_logged = False
        while True:
            job_id = self.run_once()
            if job_id is None:
                if stop_when_empty:
                    self.log("[queue] 队列已空，退出")
                    break
                if not idle_logged:
                    self.log(f"[queue] 暂无任务，每 {poll_sec:g} 秒轮询一次（Ctrl-C 退出）")
                    idle_logged = True
                time.sleep(poll_sec)
                continue
            idle_logged = False
            if max_jobs is not None and self.jobs_done + self.jobs_failed >= max_jobs:
                self.log(f"[queue] 已达到 --max-jobs {max_jobs}，退出")
                break
        return 0 if self.jobs_failed == 0 else 1

    # ------------------------------------------------------------------ 执行
    def _execute(self, job: dict) -> None:
        run_id, stage = job["run_id"], job["stage"]
        payload = job.get("payload") or {}
        plan = list(payload.get("plan") or [stage])
        index = int(payload.get("index") or 0)

        if job.get("cancel_requested"):
            self.log(f"[job] {job['id']} 在开始前已被取消")
            self.store.finish_job(job["id"], "cancelled", "开始执行前被取消")
            self.store.finish_run(run_id, "cancelled", stop_reason="用户请求取消")
            return

        prev_stage = plan[index - 1] if index > 0 else None
        prev_id = self.store.latest_stage(run_id, prev_stage) if prev_stage else None
        stage_id = self.store.begin_stage(run_id, stage, input_stage_id=prev_id)
        self.store.bind_job_stage(job["id"], stage_id)

        self.log(
            f"[job] {job['id']}｜run {run_id}｜{STAGE_LABEL.get(stage, stage)}"
            f"（第 {index + 1}/{len(plan)} 阶段，第 {job['attempts']} 次尝试）"
        )

        ctx = RunContext(
            store=self.store,
            run_id=run_id,
            stage_id=stage_id,
            job_id=job["id"],
            out_dir=out_dir_for(run_id, self.out_root),
            log=lambda msg: self.log(f"      {msg}"),
            should_stop=self._cancel_probe(job["id"]),
            payload=payload,
        )
        handler = self.handlers.get(stage)
        if handler is None:
            self._fail(job, stage_id, RuntimeError(f"没有注册 {stage} 阶段的处理器"), index)
            return

        keeper = _LeaseKeeper(self.store, job["id"], self.lease_sec, self._heartbeat)
        keeper.start()
        started = time.time()
        try:
            result = handler(ctx)
        except Exception as exc:  # noqa: BLE001
            self.log(traceback.format_exc().rstrip())
            self._fail(job, stage_id, exc, index)
            return
        finally:
            keeper.stop()

        elapsed = time.time() - started
        if self.store.cancel_requested(job["id"]):
            self.store.end_stage(stage_id, "cancelled", "用户请求取消")
            self.store.finish_job(job["id"], "cancelled", "用户请求取消")
            self.store.finish_run(run_id, "cancelled", stop_reason="用户请求取消")
            self.log(f"[job] {job['id']} 已在 {elapsed:.0f}s 内取消")
            return

        self.store.end_stage(stage_id, "succeeded")
        self.jobs_done += 1
        self.log(f"[job] {job['id']} 完成（{elapsed:.0f}s）{result.note}".rstrip())
        self._advance(job, result, plan, index)

    def _advance(self, job: dict, result: StageResult, plan: list[str], index: int) -> None:
        run_id = job["run_id"]
        payload_in = job.get("payload") or {}
        # 停止原因是"整条链"的属性，不是最后一个阶段的：调查说了"证据已达标"，
        # handoff 自己没话说，收尾时不继承就会把最有信息量的那句话丢掉
        stop_reason = result.stop_reason or payload_in.get("stop_reason") or ""
        nxt = index + 1
        if nxt < len(plan):
            next_stage = plan[nxt]
            payload = {
                **payload_in,
                "index": nxt,
                "stop_reason": stop_reason,
                "outputs": {**payload_in.get("outputs", {}), **result.outputs},
            }
            nxt_job = self.store.enqueue(
                run_id, next_stage, payload=payload, priority=job.get("priority") or 0
            )
            self.store.finish_job(job["id"], "succeeded")
            self.log(
                f"[queue] 已排下一阶段 {STAGE_LABEL.get(next_stage, next_stage)}"
                f"（{nxt_job['id']}）"
            )
            return

        self.store.finish_job(job["id"], "succeeded")
        self.store.finish_run(run_id, "succeeded", stop_reason=stop_reason, partial=result.partial)
        self.log(f"[queue] run {run_id} 已完成｜停止原因：{stop_reason or '（未记录）'}")

    def _fail(self, job: dict, stage_id: str, exc: BaseException, index: int) -> None:
        message = f"{type(exc).__name__}: {exc}"
        self.store.end_stage(stage_id, "failed", message)
        self.store.finish_job(job["id"], "failed", message)
        # partial：前序阶段（通常是调查）的产物仍然有效，只是这一链没跑完。
        # 不同 partial 的话，"核查挂了"和"调查挂了"在列表里长得一样。
        self.store.finish_run(
            job["run_id"], "failed", error=message, partial=index > 0
        )
        self.jobs_failed += 1
        self._heartbeat(last_error=message)
        self.log(f"[job] {job['id']} 失败：{message}")

    def _cancel_probe(self, job_id: str):
        def probe() -> str | None:
            if self.store.cancel_requested(job_id):
                return "用户请求取消（在轮次边界停止）"
            return None

        return probe


# --------------------------------------------------------------------------- #
# 阶段处理器
# --------------------------------------------------------------------------- #
def build_handlers(
    *,
    session_factory=None,
    llm_factory=None,
) -> dict[str, Handler]:
    """装配真实处理器。测试传自己的 handler，不经过这里，因此不需要浏览器。"""
    if session_factory is None:
        from browser.chrome import start_browser

        session_factory = start_browser
    if llm_factory is None:
        from agent.investigator import LLMClient

        llm_factory = LLMClient

    def _budget(options: dict):
        from agent.investigator import Budget

        return Budget(
            max_searches=int(options.get("max_searches", 8)),
            max_pages=int(options.get("max_pages", 30)),
            max_minutes=float(options.get("max_minutes", 10.0)),
            per_keyword_posts=int(options.get("max_posts", 30)),
            detail_checks_per_event=int(options.get("detail_checks", 2)),
            max_rounds=int(options.get("rounds", 3)),
            min_evidence=int(options.get("min_evidence", 3)),
        )

    def investigate(ctx: RunContext) -> StageResult:
        from browser.weibo import is_logged_in

        options = ctx.payload.get("options") or {}
        topic = ctx.payload.get("topic") or ""
        client = llm_factory()
        session = session_factory(
            headless=bool(options.get("headless", True)),
            profile_dir=options.get("profile_dir") or None,
        )
        try:
            logged_in = is_logged_in(session.context)
            if not logged_in:
                ctx.log("未登录微博，按游客态继续（翻页与详情页会受限，会写进 limitations）")
            outcome = execute_investigate(
                ctx,
                session,
                topic=topic,
                seed_keywords=ctx.payload.get("seed_keywords") or [],
                budget=_budget(options),
                llm=client,
                logged_in=logged_in,
            )
        finally:
            session.close()

        inv = outcome.investigation
        for err in outcome.tracer_errors:
            ctx.log(f"轨迹写库降级：{err}")
        return StageResult(
            stop_reason=inv.stop_reason,
            outputs={
                "events": len(inv.events),
                "evidence": inv.metrics.evidence_items,
                "llm_calls": int(outcome.llm_stats.get("llm_calls", 0)),
                "trace_path": str(outcome.trace_path or ""),
            },
            note=(
                f"事件 {len(inv.events)}｜证据 {inv.metrics.evidence_items}"
                f"｜帖子 {inv.metrics.posts_read}"
                f"｜模型调用 {outcome.llm_stats.get('llm_calls', 0)} 次"
            ),
        )

    def handoff(ctx: RunContext) -> StageResult:
        options = ctx.payload.get("options") or {}
        picked = options.get("only_events") or None
        outcome = execute_handoff(ctx, only_events=picked)
        h = outcome.payload["handoff"]
        return StageResult(
            outputs={"claims": len(outcome.payload["claims"]), "claims_path": str(outcome.path)},
            note=(
                f"事件 {h['events']}｜事实 {h['facts']}｜来源 {h['report_evidence']}"
                f"｜待核查 {len(outcome.payload['claims'])}"
            ),
        )

    def factcheck(ctx: RunContext) -> StageResult:
        from datetime import datetime

        options = ctx.payload.get("options") or {}
        client = llm_factory()
        out_dir = ctx.mkdir()
        tracer, _ = build_sinks(
            out_dir / f"trace_factcheck_{datetime.now():%Y%m%d_%H%M%S}.jsonl",
            ctx.store,
            ctx.run_id,
            ctx.stage_id,
        )
        client.attach_tracer(tracer)
        outcome = execute_factcheck(
            ctx,
            do_check=bool(options.get("check", True)),
            llm=client,
            semantic=options.get("semantic"),
            semantic_limit=int(options.get("semantic_limit", 20)),
        )
        summary = outcome.report.summary
        return StageResult(
            outputs={"claims": summary.get("claims", 0), "sources": summary.get("sources_checked", 0)},
            note=(
                f"说法 {summary.get('claims', 0)}｜独立验证 {summary.get('independently_verified', 0)}"
                f"｜无法证实 {summary.get('unverifiable', 0)}"
            ),
        )

    return {"investigate": investigate, "handoff": handoff, "factcheck": factcheck}


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="微博舆情调查 Agent 队列消费者")
    parser.add_argument("--db", default=None, help="SQLite 路径（默认 data/workflow.db）")
    parser.add_argument("--poll", type=float, default=2.0, help="队列空时的轮询间隔（秒）")
    parser.add_argument("--once", action="store_true", help="只处理一个任务就退出")
    parser.add_argument("--drain", action="store_true", help="处理到队列为空再退出")
    parser.add_argument("--max-jobs", type=int, default=None, help="处理 N 个任务后退出")
    parser.add_argument("--lease", type=int, default=1800, help="任务租约（秒）")
    parser.add_argument("--out", default=None, help="产物根目录（默认 output/）")
    parser.add_argument("--id", default=None, help="worker 标识（默认 主机名_PID）")
    args = parser.parse_args(argv)

    load_dotenv(PROJECT_ROOT / ".env")

    from store import open_store

    store, applied = open_store(args.db)
    if applied:
        print(f"[DB] 已应用迁移 v{', v'.join(str(v) for v in applied)}")

    worker = Worker(
        store,
        worker_id=args.id or "",
        lease_sec=args.lease,
        out_root=args.out,
    )

    from agent.investigator import LLMClient

    client = LLMClient()
    overview = store.queue_overview()
    print(f"worker：{worker.worker_id}（PID {os.getpid()}）")
    print(
        "模型  ："
        + (f"{client.model}（{client.base_url or 'api.openai.com'}）" if client.available else "未配置，将走 heuristic")
    )
    print(
        f"队列  ：排队 {overview['counts']['queued']}｜运行 {overview['counts']['running']}"
        f"｜已完成 {overview['counts']['succeeded']}｜失败 {overview['counts']['failed']}"
    )
    print(f"产物  ：{Path(args.out) if args.out else OUTPUT_ROOT}/runs/<run_id>/")
    print("-" * 78)

    try:
        if args.once:
            return 0 if worker.run_once() else 0
        return worker.run_forever(
            poll_sec=args.poll,
            max_jobs=args.max_jobs,
            stop_when_empty=args.drain,
        )
    except KeyboardInterrupt:
        print("\n已停止。正在跑的任务会在租约过期后被其他 worker 接管（或重跑）。")
        return 130
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
