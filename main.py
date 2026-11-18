"""微博舆情调查 Agent POC —— 入口。

六种用法：

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
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
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


def _line(char: str = "-", n: int = 78) -> str:
    return char * n


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
    from datetime import datetime

    from agent.investigator import Budget, Investigator, LLMClient, write_outputs
    from agent.trace import Tracer

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

    trace_path = OUTPUT_DIR / f"trace_{datetime.now():%Y%m%d_%H%M%S}.jsonl"

    investigator = Investigator(
        session=session,
        topic=args.keyword[0],
        seed_keywords=args.keyword[1:],
        budget=Budget(
            max_searches=args.max_searches,
            max_pages=args.max_pages,
            max_minutes=args.max_minutes,
            per_keyword_posts=args.max_posts,
            detail_checks_per_event=args.detail_checks,
        ),
        llm=client,
        logged_in=logged_in,
        tracer=Tracer(trace_path),
    )
    inv = investigator.run()
    json_path, md_path = write_outputs(inv, OUTPUT_DIR)

    print()
    print(inv.to_markdown())
    print(_line("="))
    print(f"JSON：{json_path}")
    print(f"Markdown：{md_path}")
    print(f"运行轨迹：{trace_path}（{len(investigator.tracer.records)} 条事件）")
    stats = investigator.llm.stats()
    if stats["llm_calls"] or stats["llm_failures"]:
        print(
            f"模型调用：成功 {stats['llm_calls']} 次｜失败尝试 {stats['llm_failures']} 次"
            f"｜累计 {stats['llm_ms'] / 1000:.1f} 秒"
        )
        print(f"         明细：uv run python main.py --trace {trace_path}")

    m = inv.metrics
    print()
    print("POC 验收清单")
    print(_line())
    checks = [
        ("读取微博 ≥10 条", m.posts_read >= 10, f"实际 {m.posts_read} 条"),
        ("自主扩展关键词 ≥1 次", m.keywords_searched >= 2, f"共搜索 {m.keywords_searched} 组"),
        ("形成事件 ≥3 个", m.events_formed >= 3, f"实际 {m.events_formed} 个"),
        (
            "每事件证据 ≥2 条",
            bool(inv.events) and all(len(e.evidence) >= 2 for e in inv.events),
            f"共 {m.evidence_items} 条",
        ),
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
    from factcheck.runner import run_factcheck

    print_header(None)
    report, json_path, md_path = run_factcheck(args.factcheck, OUTPUT_DIR, log=print)

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
    print(f"  来源：登记 {s['sources_checked']}｜可达 {s['sources_reachable']}"
          f"｜引文命中 {s['sources_quote_hit']}")
    print(_line("="))
    print(f"JSON：{json_path}")
    print(f"Markdown：{md_path}")
    return 0


# --------------------------------------------------------------------------- #
# 衔接：把调查产物转成核查待办
# --------------------------------------------------------------------------- #
def run_handoff_stage(args: argparse.Namespace) -> int:
    from agent.handoff import facts_to_claims
    from agent.schemas import Investigation

    print_header(None)
    src = Path(args.handoff)
    if not src.exists():
        print(f"找不到调查产物：{src}")
        return 2

    raw = json.loads(src.read_text(encoding="utf-8"))
    inv = Investigation.model_validate(raw)

    print(f"[1/2] 读取调查产物：{src}")
    print(f"      主题「{inv.topic}」，{len(inv.events)} 个事件，模式 {inv.mode}")

    payload = facts_to_claims(inv, only_events=args.events)
    h = payload["handoff"]

    out_path = OUTPUT_DIR / "claims_handoff.json"
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

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

    path = Path(args.trace)
    print_header(None)
    if not path.exists():
        print(f"找不到 trace 文件：{path}")
        return 2
    records = load(path)
    if not records:
        print(f"{path} 中没有可解析记录")
        return 2

    s = summarize(records)
    print(f"[1/2] {path.name}：{s['total']} 条记录")
    print(_line())
    for step, n in sorted(s["counts"].items(), key=lambda kv: -kv[1]):
        print(f"  {step:<20}{n}")

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
    else:
        print("  本次没有模型调用（heuristic 模式，或全部走了规则分支）")

    if s["errors"]:
        print()
        print("全部错误：")
        for err in s["errors"]:
            print(f"  [{err['step']}] {err['purpose'] or '-'}：{_clip(err['error'], 160)}")

    print(_line("="))
    print("读法：同一 purpose 的 prompt_digest 相同，说明输入完全一致；")
    print("      此时输出不同才是模型随机性，输出相同但结论不同则是后处理的问题。")
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
        "--trace",
        default=None,
        metavar="TRACE_JSONL",
        help="查看一次运行的轨迹（含每次模型调用），不需要浏览器",
    )
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
