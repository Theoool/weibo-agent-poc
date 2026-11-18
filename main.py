"""微博舆情调查 Agent POC —— 入口。

常用用法：

1) 环境自检
   uv run python main.py --doctor

2) 第一阶段：只验证浏览器链路（不接 AI）
   uv run python main.py --keyword APEC
   → Python → Playwright → 专用 Chrome → 手动登录 → 搜索 → 读取帖子 → 落盘

3) 第二阶段：接入 Agent，自主扩展关键词并形成事件
   uv run python main.py --agent --keyword APEC

4) 查看运行轨迹（含每次模型调用，不需要浏览器）
   uv run python main.py --trace output/trace_20261118_212101.jsonl

5) 把调查产物转成核查待办（POC 1 → POC 2 的衔接）
   uv run python main.py --handoff output/investigation.json

6) 事实核查
   uv run python main.py --factcheck output/claims_handoff.json

7) 查看工作流留存（不需要浏览器）
   uv run python main.py --runs
   uv run python main.py --show <run_id> --report
   uv run python main.py --trace <run_id>          # 轨迹与模型调用
   uv run python main.py --llm <call_id>           # 某次调用的题面与输出全文

8) 把改造前的历史产物导入数据库（标记 degraded）
   uv run python main.py --import-output output

HTTP 与队列（两个独立进程）：
   uv run python -m api --port 8000   # 查询 + 创建/重跑/取消（POST /api/runs）
   uv run python -m worker            # 消费队列：采集必须在这里跑，API 进程不碰浏览器

默认会写数据库（data/workflow.db）；加 --no-db 可只产出文件。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import unicodedata
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from browser.chrome import DEFAULT_PROFILE_DIR, BrowserSetupError, start_browser  # noqa: E402
from browser.weibo import (  # noqa: E402
    collect_search,
    is_logged_in,
    search_url,
    verify_detail,
    wait_for_login,
)

OUTPUT_DIR = PROJECT_ROOT / "output"


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def _clip(text: str, width: int) -> str:
    text = (text or "").replace("\n", " ").strip()
    return text if len(text) <= width else text[: width - 1] + "…"


def _topic_hint(path: str | Path) -> str:
    """核查 run 的临时主题：优先读清单里的 title/topic，读不到退化为文件名。

    报告标题要等核查跑完才有，跑完会用 `set_run_topic` 覆盖这里。
    先占位是为了让这次运行从第一秒起就有个能认出来的名字。
    """
    p = Path(path)
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        for key in ("title", "topic"):
            value = str(raw.get(key) or "").strip()
            if value:
                return _clip(value, 80)
    except Exception:  # noqa: BLE001
        pass
    return _clip(p.stem or "事实核查", 80)


def _line(char: str = "-", n: int = 78) -> str:
    return char * n


def _pad(text: str, width: int) -> str:
    """按显示宽度补齐。中日韩字符占两格，直接用 ljust 会让整列错位。"""
    text = _clip(text, width)
    used = sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)
    return text + " " * max(0, width - used)


def print_header(profile_dir: str | Path | None = None) -> None:
    print(_line("="))
    print("微博舆情调查 Agent POC")
    print(f"工作目录：{PROJECT_ROOT}")
    print(f"专用 Profile：{Path(profile_dir) if profile_dir else DEFAULT_PROFILE_DIR}")
    print(_line("="))


def doctor() -> int:
    print_header(None)
    ok = True

    print("[环境]")
    print(f"  Python      : {sys.version.split()[0]}")
    print(f"  OpenAI Key  : {'已配置' if os.getenv('OPENAI_API_KEY') else '未配置（Agent 将走 heuristic 模式）'}")
    print(f"  OpenAI Model: {os.getenv('OPENAI_MODEL') or '(默认 gpt-4.1-mini)'}")

    print("[依赖]")
    for mod in ("playwright", "openai", "pydantic", "dotenv"):
        try:
            __import__(mod)
            print(f"  {mod:<12}: ok")
        except Exception as exc:  # noqa: BLE001
            ok = False
            print(f"  {mod:<12}: 缺失（{exc}）")

    print("[模型]")
    from agent.investigator import LLMClient

    client = LLMClient()
    if not client.available:
        print("  未配置 OPENAI_API_KEY：Agent 将走 heuristic 模式（规则聚类，不调用模型）")
    else:
        print(f"  model    : {client.model}")
        print(f"  base_url : {client.base_url or '(默认 https://api.openai.com/v1)'}")
        ping_ok, detail = client.ping()
        if ping_ok:
            print(f"  连通性    : ok（模型回复 {detail!r}）")
        else:
            ok = False
            print(f"  连通性    : 失败 -> {detail}")
            print("             修好 Key / 余额 / 模型名 / base_url 后重试，否则事件归纳会降级为频率聚类")

    print("[浏览器]")
    try:
        session = start_browser(headless=True)
        try:
            session.page.goto("https://m.weibo.cn/", wait_until="domcontentloaded", timeout=30000)
            print(f"  Chrome 启动 : ok")
            print(f"  profile     : {session.profile_dir}")
            print(f"  当前登录态   : {'已登录' if is_logged_in(session.context) else '未登录（首次运行需手动登录一次）'}")
        finally:
            session.close()
    except BrowserSetupError as exc:
        ok = False
        print(f"  Chrome 启动 : 失败\n{exc}")
    except Exception as exc:  # noqa: BLE001
        ok = False
        print(f"  Chrome 启动 : 失败（{exc}）")

    print()
    print("自检结果：" + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


# --------------------------------------------------------------------------- #
# 登录（两个阶段共用）
# --------------------------------------------------------------------------- #
def ensure_login(args: argparse.Namespace, session, required: bool = True) -> bool:
    """检查登录态；未登录且非无头模式下等待人工登录。"""
    if is_logged_in(session.context):
        print("      已登录（复用 profile 中的登录态）")
        return True

    if args.headless:
        print("      未登录，且当前为 --headless（无法人工登录）。将以游客态继续，可能只能读到首页内容。")
        return False

    print("      未登录。请在弹出的 Chrome 窗口里手动登录微博。")
    print("      （本程序不代填账号密码，也不处理验证码；登录态会写入 profile，下次免登录）")
    if wait_for_login(session, timeout_sec=args.login_timeout):
        print("      登录成功")
        return True

    print("      登录超时。可能原因：未完成登录 / 需要验证码 / 网络问题。")
    if required:
        return False
    print("      将继续以游客态运行，限制会写入 limitations。")
    return False


# --------------------------------------------------------------------------- #
# 第一阶段：只验证浏览器链路
# --------------------------------------------------------------------------- #
def stage1(args: argparse.Namespace, session) -> int:
    keyword = args.keyword[0]
    print()
    print("[1/4] 检查登录态")
    logged_in = ensure_login(args, session, required=not args.allow_guest)
    if not logged_in and not args.allow_guest:
        print("      第一阶段要求登录态，终止。若只想先看读取链路，可加 --allow-guest。")
        return 2
    if not logged_in:
        print("      [!] --allow-guest：继续以游客态读取，翻页与详情页会受限。")

    print(f"[2/4] 打开搜索页：{search_url(keyword)}")
    result = collect_search(
        session,
        keyword,
        max_posts=args.max_posts,
        scrolls=args.scrolls,
        api_pages=args.api_pages,
    )
    if result.limitations:
        for item in result.limitations:
            print(f"      [限制] {item}")
    if not result.posts:
        print("      未读取到帖子，终止。")
        return 3

    print(f"[3/4] 读取到 {len(result.posts)} 条帖子，前 {min(10, len(result.posts))} 条：")
    print(_line())
    for i, post in enumerate(result.posts[:10], 1):
        print(
            f"{i:>2}. {_clip(post.get('author') or '未知作者', 16):<16} "
            f"| {_clip(post.get('time') or '时间未知', 14):<14} "
            f"| 赞{post.get('likes') if post.get('likes') is not None else '-'}"
        )
        print(f"    正文：{_clip(post.get('text') or '', 90)}")
        print(f"    链接：{post.get('url') or '无可验证直链'}  [{post.get('evidence_source')}]")
    print(_line())

    print(f"[4/4] 落盘")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    raw_path = OUTPUT_DIR / f"day1_{keyword.replace(' ', '_')}.json"
    payload = {
        "keyword": keyword,
        "captured_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "search_url": result.url,
        "count": len(result.posts),
        "limitations": result.limitations,
        "posts": result.posts,
    }
    raw_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"      已保存：{raw_path}")

    if args.verify_detail and result.posts:
        n = min(args.verify_detail, len(result.posts))
        print()
        print(f"[附加] 详情页复核前 {n} 条")
        passed = 0
        for post in result.posts[:n]:
            mid = (post.get("url") or "").rstrip("/").split("/")[-1]
            info = verify_detail(session, mid, post.get("author") or "", keyword)
            status = "PASS" if info.get("ok") else "FAIL"
            if info.get("ok"):
                passed += 1
                fresh = info["post"]
                print(
                    f"  {status} {fresh.get('author')} | {fresh.get('time')} | "
                    f"{_clip(fresh.get('text') or '', 50)}"
                )
            else:
                print(f"  {status} {post.get('url')} -> {info.get('reason')}")
        print(f"      详情页复核：{passed}/{n} 通过")

    # 验收
    print()
    print("Day-1 验收清单")
    print(_line())
    checks = [
        ("登录态浏览", logged_in, "已登录" if logged_in else "游客态（--allow-guest）"),
        ("搜索微博", True, result.url),
        ("读取微博 ≥10 条", len(result.posts) >= 10, f"实际 {len(result.posts)} 条"),
    ]
    all_pass = True
    for name, cond, detail in checks:
        all_pass = all_pass and cond
        print(f"  [{'PASS' if cond else 'FAIL'}] {name:<16} {detail}")
    print(_line())
    print("结论：" + ("Day-1 链路已跑通，可以进入 Agent 阶段。" if all_pass else "存在未达标项，先修链路。"))
    return 0 if all_pass else 4


# --------------------------------------------------------------------------- #
# 第二阶段：Agent 自主调查
# --------------------------------------------------------------------------- #
def stage2(args: argparse.Namespace, session) -> int:
    from agent.controller import KIND_LABEL as GAP_LABEL
    from agent.investigator import Budget, LLMClient, REAL_EVIDENCE_KINDS
    from workflow import RunContext, execute_investigate

    print()
    print("[0/5] 检查登录态")
    logged_in = ensure_login(args, session, required=False)

    client = LLMClient()
    if client.available:
        print(
            f"[模型] 已配置 {client.model}"
            f"（{client.base_url or 'api.openai.com'}），启动时会做连通性自检"
        )
    else:
        print("[模式] heuristic（未配置 OPENAI_API_KEY，使用频率聚类降级）")

    # 先把 run 建出来：它的 id 要写进产物，下游 handoff / factcheck 才能挂到同一条链上
    store = open_persistence(args)
    run_id = ""
    stage_id = ""
    if store is not None:
        run_id = store.create_run(
            args.keyword[0],
            trigger="cli",
            mode="llm" if client.available else "heuristic",
            config={
                "keywords": args.keyword,
                "rounds": args.rounds,
                "max_searches": args.max_searches,
                "max_pages": args.max_pages,
                "max_minutes": args.max_minutes,
                "max_posts": args.max_posts,
                "min_evidence": args.min_evidence,
                "detail_checks": args.detail_checks,
                "model": client.model if client.available else "",
                "base_url": (client.base_url or "") if client.available else "",
                "logged_in": logged_in,
            },
        )
        stage_id = store.begin_stage(run_id, "investigate")
        print(f"[DB] run {run_id}｜investigate 已开始")

    # 采集、判断、落库全在 workflow/investigate.py 里：worker 走的是同一份代码，
    # 否则"命令行跑出来的"和"API 跑出来的"迟早会分岔
    ctx = RunContext(
        store=store, run_id=run_id, stage_id=stage_id, out_dir=OUTPUT_DIR, log=print
    )
    try:
        outcome = execute_investigate(
            ctx,
            session,
            topic=args.keyword[0],
            seed_keywords=args.keyword[1:],
            budget=Budget(
                max_searches=args.max_searches,
                max_pages=args.max_pages,
                max_minutes=args.max_minutes,
                per_keyword_posts=args.max_posts,
                detail_checks_per_event=args.detail_checks,
                max_rounds=args.rounds,
                min_evidence=args.min_evidence,
            ),
            llm=client,
            logged_in=logged_in,
        )
    except Exception as exc:  # noqa: BLE001
        if store is not None and run_id:
            store.end_stage(stage_id, "failed", str(exc))
            store.finish_run(run_id, "failed", error=str(exc))
        print(f"[调查失败] {type(exc).__name__}: {exc}")
        return 1

    inv = outcome.investigation
    json_path, md_path, trace_path = outcome.json_path, outcome.md_path, outcome.trace_path
    print_tracer_errors(outcome.tracer_errors)

    if store is not None and run_id:
        try:
            store.end_stage(stage_id, "succeeded")
            store.finish_run(run_id, "succeeded", stop_reason=inv.stop_reason)
            stats = outcome.store_stats
            print(
                f"[DB] 已留存：文档 {stats.get('documents', 0)}｜事件 {stats.get('events', 0)}"
                f"｜事实 {stats.get('facts', 0)}｜缺口 {stats.get('gaps', 0)}"
                f"｜搜索 {stats.get('searches', 0)}"
            )
            if outcome.trace_events:
                print(
                    f"[DB] 轨迹 {outcome.trace_events} 条｜模型调用 {outcome.llm_calls} 次"
                    f"（题面与输出全文已留存，可逐字复现）"
                )
        except Exception as exc:  # noqa: BLE001
            store.end_stage(stage_id, "failed", str(exc))
            store.finish_run(run_id, "failed", error=str(exc))
            print(f"[DB] 收尾写入失败：{exc}")

    print()
    print("决策链（缺口驱动）")
    print(_line())
    m = inv.metrics
    print(
        f"  轮次 {m.rounds_used}｜判出缺口 {m.gaps_detected} 个｜补上 {m.gaps_resolved} 个"
        f"｜停止原因：{inv.stop_reason or '（未记录）'}"
    )
    for rec in inv.gaps:
        mark = "✓" if rec.resolved else ("✗" if rec.resolved is False else "?")
        label = GAP_LABEL.get(rec.kind, rec.kind)
        print(f"  {mark} 第 {rec.round} 轮 [{label}] {_clip(rec.target, 24)}：{_clip(rec.detail, 34)}")
        print(f"      补搜：{rec.action or '（未搜）'}")

    print()
    print(inv.to_markdown())
    print(_line("="))
    print(f"JSON：{json_path}")
    print(f"Markdown：{md_path}")
    if trace_path:
        print(f"运行轨迹：{trace_path}（{outcome.trace_events} 条事件）")
    if run_id:
        print(f"数据库  ：{run_id}")
        print(f"          查看留存：uv run python main.py --show {run_id}")
    if outcome.llm_calls or outcome.llm_failures:
        print(
            f"模型调用：成功 {outcome.llm_calls} 次｜失败尝试 {outcome.llm_failures} 次"
            f"｜累计 {outcome.llm_ms / 1000:.1f} 秒"
        )
        if trace_path:
            print(f"         明细：uv run python main.py --trace {trace_path}")

    m = inv.metrics
    from agent.investigator import REAL_EVIDENCE_KINDS

    print()
    print("POC 验收清单")
    print(_line())
    checks = [
        ("读取微博 ≥10 条", m.posts_read >= 10, f"实际 {m.posts_read} 条"),
        ("自主扩展关键词 ≥1 次", m.keywords_searched >= 2, f"共搜索 {m.keywords_searched} 组"),
        ("形成事件 ≥3 个", m.events_formed >= 3, f"实际 {m.events_formed} 个"),
        (
            "每事件证据 ≥2 条",
            bool(inv.events)
            and all(
                sum(1 for x in e.evidence if x.kind in REAL_EVIDENCE_KINDS) >= 2
                for e in inv.events
            ),
            f"共 {m.evidence_items} 条",
        ),
        ("停止原因已记录", bool(inv.stop_reason), inv.stop_reason or "未记录"),
    ]
    all_pass = True
    for name, cond, detail in checks:
        all_pass = all_pass and cond
        print(f"  [{'PASS' if cond else 'FAIL'}] {name:<22} {detail}")
    print(_line())
    print("结论：" + ("POC 成功。" if all_pass else "未全部达标，需看 limitations 定位原因。"))
    return 0 if all_pass else 4


# --------------------------------------------------------------------------- #
# POC 2：事实核查
# --------------------------------------------------------------------------- #
def run_factcheck_stage(args: argparse.Namespace) -> int:
    from datetime import datetime

    from agent.investigator import LLMClient
    from workflow import RunContext, build_sinks, execute_factcheck

    print_header(None)

    client = LLMClient()

    store = open_persistence(args)
    source_run_id = ""
    try:
        source_run_id = str(
            json.loads(Path(args.factcheck).read_text(encoding="utf-8")).get("source_run_id") or ""
        )
    except Exception:  # noqa: BLE001
        source_run_id = ""

    # 先把 run 与 stage 建好，再跑核查：模型调用发生在核查过程中，
    # 那时候必须已经有地方可写。反过来的话，语义判定的全文只能事后补，补不回来。
    run_id = ""
    stage_id = ""
    created_run = False
    if store is not None:
        if source_run_id and store.run_summary(source_run_id):
            run_id = source_run_id
        else:
            # 没有上游 run（例如手工维护的核查清单）：单独起一条，别硬塞进别人的 run
            run_id = store.create_run(
                _topic_hint(args.factcheck),
                trigger="cli",
                config={
                    "claims_file": str(args.factcheck),
                    "source_run_id": source_run_id,
                    "semantic": bool(args.semantic) if args.semantic is not None else "auto",
                    "checked": not args.no_check,
                },
            )
            created_run = True
        prev = store.latest_stage(run_id, "handoff")
        stage_id = store.begin_stage(run_id, "factcheck", input_stage_id=prev)

    trace_path = None
    tracer = None
    if client.available and args.semantic is not False:
        trace_path = OUTPUT_DIR / f"trace_factcheck_{datetime.now():%Y%m%d_%H%M%S}.jsonl"
        tracer, _ = build_sinks(trace_path, store, run_id, stage_id)
        client.attach_tracer(tracer)
        print(f"[模型] {client.model}（语义判定：引文是否支持说法）")
    elif args.semantic is True:
        print("[模型] 未配置 OPENAI_API_KEY，语义判定将跳过")

    ctx = RunContext(
        store=store, run_id=run_id, stage_id=stage_id, out_dir=OUTPUT_DIR, log=print
    )
    try:
        outcome = execute_factcheck(
            ctx,
            claims_path=args.factcheck,
            do_check=not args.no_check,
            llm=client,
            semantic=args.semantic,
            semantic_limit=args.semantic_limit,
            update_topic=created_run,
        )
    except Exception as exc:  # noqa: BLE001
        if store is not None and run_id:
            store.end_stage(stage_id, "failed", str(exc))
            store.finish_run(run_id, "failed", error=str(exc))
        print(f"[核查失败] {type(exc).__name__}: {exc}")
        return 1

    report = outcome.report
    json_path, md_path = outcome.json_path, outcome.md_path
    print_tracer_errors(getattr(tracer, "errors", None) or [])

    if store is not None and run_id:
        try:
            store.end_stage(stage_id, "succeeded")
            store.finish_run(run_id, "succeeded")
            stats = outcome.stats
            print(
                f"[DB] 已留存：来源 {stats.get('sources', 0)}｜机检 {stats.get('checks', 0)}"
                f"｜说法 {stats.get('claims', 0)}｜引用 {stats.get('citations', 0)}"
                f"（run {run_id}）"
            )
        except Exception as exc:  # noqa: BLE001
            store.end_stage(stage_id, "failed", str(exc))
            store.finish_run(run_id, "failed", error=str(exc))
            print(f"[DB] 收尾写入失败：{exc}")

    s = report.summary
    print()
    print("核查结果（* 表示因来源校验失败被自动降级）")
    print(_line())
    for claim in report.claims:
        flag = "*" if claim.downgraded else " "
        print(
            f"  {claim.id}{flag} {claim.effective():<5} 独立信源 {claim.independent_groups} "
            f"| {claim.claim[:40]}"
        )
    print(_line())
    counts: dict[str, int] = {}
    for claim in report.claims:
        key = claim.effective()
        counts[key] = counts.get(key, 0) + 1
    print("  " + "｜".join(f"{k} {v}" for k, v in counts.items()))
    print(
        f"  独立验证 {s['independently_verified']} 条｜仅二手转述 {s['secondhand_only']} 条"
        f"｜无法证实 {s['unverifiable']} 条｜存在矛盾 {s['contradictory']} 条"
        + (f"｜待核查 {s['pending']} 条" if s.get("pending") else "")
    )
    if args.no_check:
        print(f"  来源：登记 {s['sources_checked']}（本次未机检）")
    else:
        print(f"  来源：登记 {s['sources_checked']}｜可达 {s['sources_reachable']}"
              f"｜引文命中 {s['sources_quote_hit']}")
    if s.get("semantic_checked"):
        print(
            f"  语义：{s['semantic_checked']} 条已判定引文与说法关系"
            f"｜支持/部分 {s.get('citation_supported', 0)}"
            f"｜不相关 {s.get('citation_unrelated', 0)}"
            f"｜相反 {s.get('citation_conflicting', 0)}"
            f"｜信息不足 {s.get('citation_insufficient', 0)}"
        )
    else:
        print(f"  语义：未执行（{(report.semantic or {}).get('reason') or '未启用'}）")
    print(_line("="))
    print(f"JSON：{json_path}")
    print(f"Markdown：{md_path}")
    if trace_path is not None and trace_path.exists():
        print(f"语义轨迹：{trace_path}")
        print(f"          uv run python main.py --trace {trace_path}")
    return 0


# --------------------------------------------------------------------------- #
# 衔接：把调查产物转成核查待办
# --------------------------------------------------------------------------- #
def run_handoff_stage(args: argparse.Namespace) -> int:
    from agent.schemas import Investigation
    from workflow import RunContext, execute_handoff

    print_header(None)
    src = Path(args.handoff)
    if not src.exists():
        print(f"找不到调查产物：{src}")
        return 2

    raw = json.loads(src.read_text(encoding="utf-8"))
    inv = Investigation.model_validate(raw)

    print(f"[1/2] 读取调查产物：{src}")
    print(f"      主题「{inv.topic}」，{len(inv.events)} 个事件，模式 {inv.mode}")

    store = open_persistence(args)
    run_id = inv.run_id
    if store is not None and run_id and store.run_summary(run_id) is None:
        run_id = ""
    stage_id = ""
    if store is not None and run_id:
        prev = store.latest_stage(run_id, "investigate")
        stage_id = store.begin_stage(run_id, "handoff", input_stage_id=prev)

    ctx = RunContext(
        store=store, run_id=run_id, stage_id=stage_id, out_dir=OUTPUT_DIR, log=print
    )
    try:
        outcome = execute_handoff(ctx, investigation=inv, only_events=args.events)
    except Exception as exc:  # noqa: BLE001
        if stage_id:
            store.end_stage(stage_id, "failed", str(exc))
            store.finish_run(run_id, "failed", error=str(exc))
        print(f"[handoff 失败] {type(exc).__name__}: {exc}")
        return 1

    payload = outcome.payload
    h = payload["handoff"]
    out_path = outcome.path

    if store is None:
        print("      [DB] 未写数据库（--no-db 或库不可用）")
    elif stage_id:
        store.end_stage(stage_id, "succeeded")
        store.finish_run(run_id, "succeeded", stop_reason=inv.stop_reason)
        print(f"      [DB] 已挂到 run {run_id}（handoff 阶段）")
    else:
        print("      [DB] 这份产物没带 run_id（旧产物或来自别处），未挂到工作流上")

    print("[2/2] 抽取待核查说法")
    print(_line())
    print(f"  事件 {h['events']} 个｜事实 {h['facts']} 条｜来源 {h['report_evidence']} 个")
    print(f"  跳过样本统计 {h['skipped_meta']} 条（不可外部核查）")
    print(
        f"  其中事件级自动关联 {h['inferred_facts']} 条，"
        f"无任何支撑证据 {h['facts_without_evidence']} 条"
    )
    print(_line())
    for claim in payload["claims"]:
        print(f"  {claim['id']}  [{len(claim['supporting'])} 条来源] {claim['claim'][:46]}")
    print(_line("="))
    print(f"已写出：{out_path}")
    print()
    print("下一步（补齐报告之外的独立来源后，再跑一次核查）：")
    print(f"  uv run python main.py --factcheck {out_path}")
    return 0


# --------------------------------------------------------------------------- #
# trace 查看：把一次运行的决策链读回来
# --------------------------------------------------------------------------- #
def run_trace_stage(args: argparse.Namespace) -> int:
    from agent.trace import load, summarize

    print_header(None)
    path = Path(args.trace)
    from_db = False
    calls: list[dict] | None = None

    if path.exists():
        records = load(path)
        label = path.name
    else:
        # 不当成路径时按 run_id 处理：M2 之后轨迹同时留在库里，
        # "看这次运行到底发生了什么"不该依赖 output/ 下的某个文件还在不在
        store = open_persistence(args)
        if store is None:
            print(f"找不到 trace 文件：{path}")
            return 2
        records = store.load_trace(args.trace)
        if not records:
            if store.run_summary(args.trace) is None:
                print(f"{args.trace} 既不是存在的文件，也不是库里的 run_id。")
                print("查看所有 run：uv run python main.py --runs")
            else:
                # 老 run 别让人以为"跑得很干净"，而是当时根本没往库里写
                print(f"{args.trace} 是早于 M2 的 run：那次轨迹只写进了 output/trace_*.jsonl。")
                print("新跑的运行会同时写库；旧运行请传 JSONL 路径查看。")
            return 2
        calls = store.load_llm_calls(args.trace)
        label = args.trace
        from_db = True

    if not records:
        print(f"{label} 中没有可解析记录")
        return 2

    s = summarize(records)
    print(f"[1/2] {label}：{s['total']} 条记录" + ("（来自数据库）" if from_db else ""))
    print(_line())
    for step, n in sorted(s["counts"].items(), key=lambda kv: -kv[1]):
        print(f"  {step:<20}{n}")

    if calls is None:
        calls = [r for r in records if r.get("step") == "llm_call"]
    print()
    print(
        f"[2/2] 模型调用 {len(calls)} 次（成功 {s['llm_ok']}｜失败 {s['llm_failed']}）"
        f"｜累计 {s['llm_ms'] / 1000:.1f} 秒"
    )
    print(_line())
    if calls:
        for i, rec in enumerate(calls, 1):
            flag = "ok  " if rec.get("ok") else "FAIL"
            print(
                f"  {i:>2}. {flag} {str(rec.get('purpose') or '-'):<18} "
                f"{rec.get('duration_ms', 0) / 1000:>5.1f}s  "
                f"{rec.get('request_chars', 0)} → {rec.get('response_chars', 0)} 字  "
                f"digest {rec.get('prompt_digest', '')}  第 {rec.get('attempt', 1)} 次"
            )
            if not rec.get("ok"):
                error = str(rec.get("error") or "未知")
                print(f"      原因：{_clip(error, 160)}")
                preview = str(rec.get("response_preview") or "")
                # 解析失败时 error 里已经带了返回内容，别重复打一遍
                if preview and preview[:30] not in error:
                    print(f"      实际返回：{_clip(preview, 120)}")
            if from_db and rec.get("id"):
                print(f"      全文：uv run python main.py --llm {rec['id']}")
    else:
        print("  本次没有模型调用（heuristic 模式，或全部走了规则分支）")

    if s["errors"]:
        print()
        print("全部错误：")
        for err in s["errors"]:
            print(f"  [{err['step']}] {err['purpose'] or '-'}：{_clip(err['error'], 160)}")

    print(_line("="))
    if from_db:
        print("读法：同一 purpose 的 prompt_digest 相同，说明输入完全一致；")
        print("      此时输出不同才是模型随机性，输出相同但结论不同则是后处理的问题。")
        print("      库里同时留了题面与输出全文，用 --llm <调用 id> 逐字看。")
    else:
        print("读法：同一 purpose 的 prompt_digest 相同，说明输入完全一致；")
        print("      此时输出不同才是模型随机性，输出相同但结论不同则是后处理的问题。")
        print("      文件里只有摘要；全文在库里（--trace <run_id> 可看到调用 id）。")
    return 0


def run_llm_stage(args: argparse.Namespace) -> int:
    """打印一次模型调用的题面与输出全文。

    M2 的核心验收点就在这条命令上：报告里的每个判断都能追回"模型当时
    实际收到了什么、答了什么"，而不是只能看到一个 300 字预览。
    """
    print_header(None)
    store = open_persistence(args)
    if store is None:
        return 2
    call = store.load_llm_call(args.llm)
    if call is None:
        print(f"找不到模型调用记录：{args.llm}")
        print("先跑一次带模型的阶段，再用 --trace <run_id> 拿到调用 id。")
        return 2

    mark = "成功" if call["ok"] else "失败"
    print(f"{call['id']} ｜{call['purpose']}｜第 {call['attempt']} 次｜{mark}")
    print(_line("="))
    print(f"模型    ：{call['model']}（{call['base_url'] or '默认端点'}）")
    print(f"时间    ：{call['created_at']}｜耗时 {call['duration_ms'] / 1000:.1f}s")
    print(
        f"长度    ：题面 {call['request_chars']} 字 → 输出 {call['response_chars']} 字"
        f"｜digest {call['prompt_digest']}"
    )
    print(
        "存储    ：题面 "
        + ("外部文件（≥4KB）" if call["prompt_ref"] else "内联（<4KB）")
        + "｜输出 "
        + ("外部文件（≥4KB）" if call["response_ref"] else "内联（<4KB）")
    )
    if call["error"]:
        print(f"错误    ：{call['error']}")

    print()
    print("题面全文")
    print(_line())
    print(call["prompt"] or "（未留存：这次调用在发出请求前就失败了）")
    print()
    print("模型输出全文")
    print(_line())
    print(call["response"] or "（空：模型没有返回内容）")
    print(_line("="))
    return 0


# --------------------------------------------------------------------------- #
# 持久层：只在阶段边界出现，领域层不 import 它
# --------------------------------------------------------------------------- #
def print_tracer_errors(errors: list[str]) -> None:
    """轨迹写库失败时明确告警。

    不中断调查（文件产物还在），但必须说出来——否则事后会以为"这一段本来就没记录"。
    """
    if not errors:
        return
    print(f"[DB] 轨迹写入降级（{len(errors)} 类错误），文件轨迹不受影响：")
    for err in errors:
        print(f"      {_clip(err, 140)}")


def open_persistence(args: argparse.Namespace):
    """打开数据库。失败时不中断主流程——文件产物仍然照写。

    A 级原则要求"发生即写"，但让调查因为数据库不可用而整个失败更糟：
    文件产物还在，事后可以补导。所以这里降级并**明确告警**，不静默。
    """
    if getattr(args, "no_db", False):
        print("[DB] --no-db：本次只写文件产物")
        return None
    try:
        from store import open_store
    except Exception as exc:  # noqa: BLE001
        print(f"[DB] 持久层不可用（{exc}），本次只写文件产物")
        return None
    try:
        store, applied = open_store(getattr(args, "db", None))
    except Exception as exc:  # noqa: BLE001
        print(f"[DB] 初始化失败（{exc}），本次只写文件产物")
        return None
    if applied:
        print(f"[DB] 已应用迁移 v{', v'.join(str(v) for v in applied)}")
    return store


def run_import_stage(args: argparse.Namespace) -> int:
    from store.importer import import_dir

    print_header(None)
    store = open_persistence(args)
    if store is None:
        return 2
    src = Path(args.import_output)
    if not src.exists():
        print(f"找不到目录：{src}")
        return 2

    print(f"[1/2] 导入历史产物：{src}")
    results = import_dir(store, src, log=print)
    print(f"[2/2] 完成：导入 {len(results)} 个 run")
    if results:
        print(_line("="))
        print("注意：导入的 run 标记为 quality=degraded —— 历史产物里没有帖子全文快照，")
        print("      证据正文是当时截断后的摘录，不能当审计依据。")
    return 0


def run_runs_stage(args: argparse.Namespace) -> int:
    print_header(None)
    store = open_persistence(args)
    if store is None:
        return 2
    rows = store.list_runs(limit=args.limit)
    if not rows:
        print("数据库里还没有 run。先跑一次 --agent，或用 --import-output 导入历史产物。")
        return 0

    print(f"工作流记录（最近 {len(rows)} 条）")
    print(_line())
    print(f"{_pad('run_id', 30)} {_pad('主题', 22)} {_pad('状态', 10)} {_pad('来源', 8)} {_pad('质量', 9)} 开始时间")
    for row in rows:
        print(
            f"{_pad(row['id'], 30)} {_pad(row['topic'], 22)} {_pad(row['status'], 10)} "
            f"{_pad(row['trigger'], 8)} {_pad(row['quality'], 9)} {row['started_at']}"
        )
    print(_line("="))
    print("查看详情：uv run python main.py --show <run_id>")
    return 0


def run_show_stage(args: argparse.Namespace) -> int:
    print_header(None)
    store = open_persistence(args)
    if store is None:
        return 2
    summary = store.run_summary(args.show)
    if summary is None:
        print(f"找不到 run：{args.show}")
        return 2

    print(f"{summary['id']}")
    print(_line("="))
    print(f"主题      ：{summary['topic']}")
    print(f"状态      ：{summary['status']}"
          + ("（部分完成）" if summary.get("partial") else ""))
    print(f"触发方式  ：{summary['trigger']}")
    print(f"数据质量  ：{summary['quality']}"
          + ("（历史导入，无全文快照）" if summary["quality"] == "degraded" else ""))
    print(f"运行模式  ：{summary['mode'] or '未记录'}")
    print(f"时间      ：{summary['started_at']} → {summary['finished_at'] or '进行中'}")
    if summary["stop_reason"]:
        print(f"停止原因  ：{summary['stop_reason']}")
    if summary["parent_run_id"]:
        print(f"上游 run  ：{summary['parent_run_id']}")
    if summary["error"]:
        print(f"错误      ：{summary['error']}")

    print()
    print("阶段")
    print(_line())
    for st in summary["stages"]:
        span = f"{st['started_at'][11:]}→{(st['finished_at'] or '')[:19][11:] or '进行中'}"
        print(f"  {st['stage']:<12} 第 {st['attempt']} 次  {st['status']:<10} {span}")
        if st["error"]:
            print(f"      错误：{_clip(st['error'], 70)}")

    # 队列信息与阶段是两回事：阶段说"跑过什么"，队列说"还在等什么/为什么没跑"
    job = summary.get("queue") or store.run_queue(args.show)
    if job:
        print()
        print("队列（最近一次任务）")
        print(_line())
        print(
            f"  {job['stage']:<12} {job['status']:<10} 第 {job['attempts']} 次尝试"
            f"｜worker {job['worker_id'] or '（未领取）'}"
        )
        if job["status"] == "queued":
            print(f"  前面还有 {job['ahead']} 个任务；消费需要 worker 在跑：uv run python -m worker")
        if job["error"]:
            print(f"  错误：{_clip(job['error'], 70)}")

    c = summary["counts"]
    print()
    print("留存内容")
    print(_line())
    print(f"  证据文档 {c['documents']} 个｜事件 {c['events']} 个｜事实 {c['facts']} 条")
    print(f"  待核查说法 {c['claims']} 条｜模型调用 {c['llm_calls']} 次｜轨迹 {c['trace_events']} 条")
    if c["llm_calls"] or c["trace_events"]:
        print(f"  轨迹查看：uv run python main.py --trace {summary['id']}")
    elif int(summary["metrics"].get("llm_calls") or 0) > 0:
        # 指标说调过模型、表里却没有：这不是数据丢了，是那次运行早于轨迹入库
        print("  注：该 run 早于 M2，模型调用只留在 JSONL 轨迹里，库里没有题面全文")
    metrics = summary["metrics"]
    if metrics:
        head = "｜".join(f"{k} {int(v)}" for k, v in sorted(metrics.items())[:8])
        print(f"  指标：{head}")

    inv = store.load_investigation(args.show)
    if inv is not None and inv.events:
        print()
        print("事件（从数据库读回）")
        print(_line())
        for i, ev in enumerate(inv.events, 1):
            print(
                f"  {i}. {_clip(ev.name, 30)}｜证据 {len(ev.evidence)} 条"
                f"｜事实 {len(ev.facts)} 条｜热度 {ev.heat}"
            )

    if args.report:
        if inv is None:
            print()
            print("没有可渲染的调查产物（该 run 可能只跑了核查阶段）。")
        else:
            print()
            print(_line("="))
            print(inv.to_markdown())

    print(_line("="))
    print("说明：报告是现渲染的，数据来自数据库，不是读 output/ 下的文件。")
    return 0


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="微博舆情调查 Agent POC",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--doctor", action="store_true", help="只做环境自检，不打开微博")
    p.add_argument(
        "--factcheck",
        default=None,
        metavar="CLAIMS_JSON",
        help="对指定的待核查清单执行事实核查（不需要浏览器）",
    )
    p.add_argument("--agent", action="store_true", help="进入 Agent 阶段（自主扩展关键词 + 事件归纳）")
    p.add_argument("--keyword", action="append", nargs="?", help="调查主题/关键词，可重复")
    p.add_argument("--max-posts", type=int, default=30, help="每个关键词最多保留帖子数")
    p.add_argument("--scrolls", type=int, default=3, help="搜索页滚动次数（触发懒加载）")
    p.add_argument("--api-pages", type=int, default=2, help="补充读取的 container 分页数")
    p.add_argument("--verify-detail", type=int, default=0, help="第一阶段额外复核前 N 条详情页")
    p.add_argument(
        "--allow-guest",
        action="store_true",
        help="未登录也继续（游客态只能读首屏，用于先验证读取链路）",
    )
    p.add_argument("--login-timeout", type=int, default=300, help="等待手动登录的秒数")
    p.add_argument("--headless", action="store_true", help="无头模式（无法手动登录，仅调试用）")
    p.add_argument("--no-real-chrome", action="store_true", help="改用 Playwright 自带 Chromium")
    p.add_argument("--profile-dir", default=None, help="专用 Chrome Profile 目录")
    p.add_argument("--max-searches", type=int, default=10, help="Agent 阶段最多搜索次数")
    p.add_argument("--max-pages", type=int, default=30, help="Agent 阶段最多页面数")
    p.add_argument("--max-minutes", type=float, default=10.0, help="Agent 阶段时间上限（分钟）")
    p.add_argument("--detail-checks", type=int, default=2, help="每个事件详情页复核条目数")
    p.add_argument(
        "--rounds",
        type=int,
        default=3,
        help="搜索总轮次上限（1 轮种子 + 缺口驱动补搜），含第一轮",
    )
    p.add_argument(
        "--min-evidence",
        type=int,
        default=3,
        help="单个事件低于该证据条数即判为「证据不足」缺口",
    )
    p.add_argument(
        "--trace",
        default=None,
        metavar="TRACE_JSONL | RUN_ID",
        help="查看一次运行的轨迹（含每次模型调用）：可传 JSONL 文件，也可直接传 run_id，不需要浏览器",
    )
    p.add_argument(
        "--llm",
        default=None,
        metavar="CALL_ID",
        help="打印一次模型调用的题面与输出全文（id 从 --trace 里取）",
    )
    p.add_argument(
        "--no-check",
        action="store_true",
        help="跳过链接可达性与引文机检（只跑语义判定，或用于快速重渲报告）",
    )
    p.add_argument(
        "--semantic",
        dest="semantic",
        action="store_true",
        default=None,
        help="核查时对每条说法执行引文-说法语义判定（默认：有 Key 就跑）",
    )
    p.add_argument(
        "--no-semantic",
        dest="semantic",
        action="store_false",
        default=None,
        help="跳过语义判定，只做链接可达性与引文命中机检",
    )
    p.add_argument(
        "--semantic-limit",
        type=int,
        default=20,
        help="语义判定最多处理多少条说法",
    )
    p.add_argument(
        "--db",
        default=None,
        metavar="PATH",
        help="SQLite 路径（默认 data/workflow.db）",
    )
    p.add_argument(
        "--no-db",
        action="store_true",
        help="本次不写数据库，只产出文件",
    )
    p.add_argument(
        "--import-output",
        default=None,
        metavar="DIR",
        help="把目录下的历史产物导入数据库（标记为 degraded）",
    )
    p.add_argument("--runs", action="store_true", help="列出工作流记录")
    p.add_argument("--show", default=None, metavar="RUN_ID", help="查看一条工作流的留存内容")
    p.add_argument(
        "--report",
        action="store_true",
        help="配合 --show：从数据库重新渲染报告",
    )
    p.add_argument("--limit", type=int, default=20, help="配合 --runs：最多列出多少条")
    p.add_argument(
        "--handoff",
        default=None,
        help="把调查产物转成核查待办：传 investigation.json 路径",
    )
    p.add_argument(
        "--events",
        default=None,
        help="配合 --handoff，只抽取指定事件（1 起，逗号分隔，如 1,2）",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    load_dotenv(PROJECT_ROOT / ".env")
    parser = build_parser()
    args = parser.parse_args(argv)
    args.keyword = args.keyword or ["APEC"]

    if args.doctor:
        return doctor()

    if args.trace:
        return run_trace_stage(args)

    if args.llm:
        return run_llm_stage(args)

    if args.runs:
        return run_runs_stage(args)

    if args.show:
        return run_show_stage(args)

    if args.import_output:
        return run_import_stage(args)

    if args.handoff:
        picked = None
        if args.events:
            picked = [
                int(x)
                for x in str(args.events).replace("，", ",").split(",")
                if x.strip().isdigit()
            ]
        args.events = picked
        return run_handoff_stage(args)

    if args.factcheck:
        return run_factcheck_stage(args)

    print_header(args.profile_dir)
    session = None
    try:
        session = start_browser(
            profile_dir=args.profile_dir,
            headless=args.headless,
            use_real_chrome=not args.no_real_chrome,
        )
        if args.agent:
            return stage2(args, session)
        return stage1(args, session)
    except BrowserSetupError as exc:
        print(f"[fatal] {exc}")
        return 1
    except KeyboardInterrupt:
        print("\n已中断。")
        return 130
    finally:
        if session is not None:
            session.close()


if __name__ == "__main__":
    raise SystemExit(main())
