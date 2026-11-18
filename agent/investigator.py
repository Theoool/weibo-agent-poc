"""最小 Agent 调度层。

做两件事（对应 POC 的验收目标）：
1. 持续操作浏览器：按计划搜索 → 读取帖子 → 详情页复核。
2. 把结果结构化落盘：investigation.json / investigation.md。

两种运行模式：
- mode="llm"      ：有 OPENAI_API_KEY 时，用模型做关键词规划与事件归纳。
- mode="heuristic"：无 Key 时，用话题词/关键词频率做聚类，仍产出同一份 JSON 结构。
两模式共用同一套预算、去重、证据回填与限制记录逻辑。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections import Counter, defaultdict
from datetime import datetime
from dataclasses import dataclass
from pathlib import Path

from browser.weibo import (
    collect_search,
    extract_hashtags,
    merge_posts,
    read_hot_board,
    verify_detail,
)

from .prompts import (
    SYSTEM_PROMPT,
    digest_posts,
    event_build_prompt,
    gap_plan_prompt,
    keyword_plan_prompt,
)
from .controller import (
    SAMPLE,
    Gap,
    candidate_pool,
    detect_gaps,
    hot_candidates,
    plan_round,
    stop_reason as gap_stop_reason,
)
from .merge import merge_events, similarity
from .schemas import (
    Event,
    Evidence,
    Fact,
    GapRecord,
    Investigation,
    Limitation,
    Metrics,
    SearchRecord,
    dedupe_limitations,
)
from .trace import LLMCall, TraceSink, Tracer

_VALID_SOURCE_KINDS = {
    "weibo_post",
    "official_site",
    "media_report",
    "hot_board",
    "unknown",
}

#: 喂给模型的帖子条数上限，必须与 prompts.digest_posts 的默认 limit 一致，
#: 否则“第 N 条”的映射会和模型实际看到的内容错位。
DIGEST_LIMIT = 60

#: 真正算“证据”的 kind。other 是校验留痕（如“引用了不存在的 mid”），不能计入证据条数。
REAL_EVIDENCE_KINDS = {"weibo_post", "official_site", "media_report"}


def _norm_source_kind(value) -> str:
    """模型可能给出任意字符串，非法值统一落回 unknown。"""
    v = str(value or "").strip()
    return v if v in _VALID_SOURCE_KINDS else "unknown"


@dataclass
class Budget:
    max_searches: int = 10
    max_pages: int = 30
    max_minutes: float = 10.0
    per_keyword_posts: int = 30
    detail_checks_per_event: int = 2
    # 总轮次上限（含第一轮）。轮次是比搜索次数更硬的刹车：
    # 没有它，缺口一直判不出来时就会一直搜下去。
    max_rounds: int = 3
    # 单个事件低于这个证据条数就算"证据不足"
    min_evidence: int = 3
    # 整体样本低于这个条数就算"样本过薄"
    min_posts: int = 10

    def __post_init__(self) -> None:
        self.started = time.time()

    def minutes_left(self) -> float:
        return self.max_minutes - (time.time() - self.started) / 60.0

    def exhausted(self, searches: int, pages: int) -> bool:
        return searches >= self.max_searches or pages >= self.max_pages or self.minutes_left() <= 0

    def page_budget_left(self, pages_read: int) -> bool:
        """页面/时间预算（不含搜索次数），供详情页复核使用。"""
        return pages_read < self.max_pages and self.minutes_left() > 0


class LLMClient:
    """OpenAI 兼容客户端。没有 Key 时 available=False，调用方自动降级。

    重要：调用失败时**不再静默返回 None**，而是把真实原因存进 last_error，
    由调用方写进 limitations。否则 402/401/404 都会显示成"输出不可解析"，
    把"账户没钱"误诊成"模型不会输出 JSON"。

    每次尝试都会往 tracer 写一条 llm_call：模型输出一旦只留在日志里，
    事后就只能靠猜"是模型判错，还是喂进去的数据本来就不够"。
    """

    def __init__(self, api_key: str | None = None, model: str | None = None) -> None:
        self.api_key = api_key or os.getenv("OPENAI_API_KEY") or ""
        self.model = model or os.getenv("OPENAI_MODEL") or "gpt-4.1-mini"
        self.base_url = os.getenv("OPENAI_BASE_URL") or None
        self.available = bool(self.api_key)
        self.last_error: str = ""
        self._client = None
        # 统计：calls/failures 都是**尝试**计数，一次调用可能尝试两次
        self.calls = 0
        self.failures = 0
        self.total_ms = 0.0
        self.tracer: TraceSink | None = None
        if self.available:
            try:
                from openai import OpenAI

                self._client = OpenAI(api_key=self.api_key, base_url=self.base_url)
            except Exception as exc:  # noqa: BLE001
                self.available = False
                self.last_error = f"客户端初始化失败：{exc}"

    def attach_tracer(self, tracer: TraceSink | None) -> None:
        self.tracer = tracer

    def stats(self) -> dict[str, int]:
        return {
            "llm_calls": self.calls,
            "llm_failures": self.failures,
            "llm_ms": int(self.total_ms),
        }

    def _record(
        self,
        purpose: str,
        attempt: int,
        ok: bool,
        started: float,
        system: str,
        user: str,
        content: str,
        error: str,
    ) -> None:
        duration_ms = int((time.perf_counter() - started) * 1000)
        self.total_ms += duration_ms
        if self.tracer is None:
            return
        call = LLMCall(
            purpose=purpose or "unspecified",
            model=self.model,
            base_url=self.base_url or "",
            attempt=attempt,
            ok=ok,
            duration_ms=duration_ms,
            system=system or "",
            user=user or "",
            content=content or "",
            error=error or "",
            prompt_digest=hashlib.sha1(f"{system}\n{user}".encode()).hexdigest()[:12],
            created_at=datetime.now().isoformat(timespec="milliseconds"),
        )
        # 摘要进轨迹（人能一眼看懂的粒度）
        self.tracer.log("llm_call", **call.to_record())
        # 全文只给愿意接的 sink：数据库把 prompt / response 落内容寻址目录，
        # 这样"模型当时看到了什么、答了什么"可以逐字复现，而不是靠 120 字预览去猜。
        sink = getattr(self.tracer, "record_call", None)
        if callable(sink):
            sink(call)

    def ping(self) -> tuple[bool, str]:
        """最小连通性探针：只发一句 hello，用于 --doctor 与启动前自检。"""
        if not self.available or self._client is None:
            return False, self.last_error or "未配置 OPENAI_API_KEY"
        started = time.perf_counter()
        try:
            resp = self._client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": "只回复 ok"}],
                max_tokens=5,
            )
            detail = (resp.choices[0].message.content or "").strip()[:40]
            self._record("ping", 1, True, started, "只回复 ok", "", detail, "")
            return True, detail
        except Exception as exc:  # noqa: BLE001
            reason = _describe_api_error(exc)
            self.last_error = reason
            self._record("ping", 1, False, started, "只回复 ok", "", "", reason)
            return False, reason

    def chat_json(self, system: str, user: str, purpose: str = "") -> dict | None:
        if not self.available or self._client is None:
            self.last_error = self.last_error or "未配置 OPENAI_API_KEY"
            return None
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        errors: list[str] = []
        for attempt, kwargs in enumerate(
            ({"response_format": {"type": "json_object"}}, {}), 1
        ):
            started = time.perf_counter()
            try:
                resp = self._client.chat.completions.create(
                    model=self.model, messages=messages, temperature=0.2, **kwargs
                )
            except Exception as exc:  # noqa: BLE001
                reason = _describe_api_error(exc)
                errors.append(reason)
                self.failures += 1
                self._record(purpose, attempt, False, started, system, user, "", reason)
                # 认证/余额/模型不存在这类错误，换参数重试没有意义
                if _is_fatal_api_error(exc):
                    break
                continue
            content = resp.choices[0].message.content or ""
            payload = _loads_loose(content)
            if payload is not None:
                self.last_error = ""
                self.calls += 1
                self._record(purpose, attempt, True, started, system, user, content, "")
                return payload
            reason = f"返回内容无法解析为 JSON（前 120 字：{content[:120]!r}）"
            errors.append(reason)
            self.failures += 1
            self._record(purpose, attempt, False, started, system, user, content, reason)
        self.last_error = "；".join(dict.fromkeys(errors))[:600]
        return None


def _describe_api_error(exc: Exception) -> str:
    """把 SDK 异常翻译成能直接动手修的一句话。"""
    name = type(exc).__name__
    status = getattr(exc, "status_code", None)
    body = str(exc)
    hints = {
        401: "API Key 无效或已过期，检查 OPENAI_API_KEY",
        402: "账户余额不足，需要充值后再跑（这不是代码问题）",
        403: "无权访问该模型，或该地区被限制",
        404: "模型名不存在或 base_url 路径写错（OpenAI 兼容网关常需以 /v1 结尾）",
        429: "触发限流或配额用尽，降低并发或稍后重试",
    }
    hint = hints.get(status or 0, "")
    if status in (400, 422):
        hint = "请求参数被拒，常见原因：模型名不对，或该网关不支持 response_format=json_object"
    if "Connection" in name or "Timeout" in name or "APIConnection" in name:
        hint = "网络不可达，检查 OPENAI_BASE_URL 与本机代理"
    head = f"HTTP {status} {name}" if status else f"{name}"
    detail = body.split("\n")[0][:200]
    return f"{head}：{detail}" + (f"（{hint}）" if hint else "")


def _is_fatal_api_error(exc: Exception) -> bool:
    """鉴权/余额/权限/模型不存在 —— 重试或换参数都不会成功。"""
    status = getattr(exc, "status_code", None)
    return status in {401, 402, 403, 404}


def _loads_loose(text: str) -> dict | None:
    text = (text or "").strip()
    if not text:
        return None
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{[\s\S]*\}", text)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        return None


def _topic_match(item: str, topic: str) -> bool:
    return topic.lower() in item.lower()


class Investigator:
    def __init__(
        self,
        session,
        topic: str = "APEC",
        seed_keywords: list[str] | None = None,
        budget: Budget | None = None,
        llm: LLMClient | None = None,
        max_events: int = 5,
        logged_in: bool = True,
        search_delay: float = 2.5,
        tracer: TraceSink | None = None,
        log=print,
    ) -> None:
        self.session = session
        self.topic = topic
        self.seed_keywords = seed_keywords or []
        self.budget = budget or Budget()
        self.llm = llm or LLMClient()
        self.max_events = max_events
        self.logged_in = logged_in
        self.search_delay = search_delay
        # 不传就是纯内存 trace（不落盘），便于测试时不产生文件
        self.tracer = tracer or Tracer(None)
        self.log = log
        self.llm_disabled = False
        # 让每次模型调用都进同一条 trace，否则只能从 limitations 里看到"失败了"，
        # 看不到失败在第几次尝试、消耗多少、模型实际返回了什么。
        self.llm.attach_tracer(self.tracer)

        self.posts: dict[str, dict] = {}
        self.searches: list[SearchRecord] = []
        self.limitations: list[Limitation] = []
        self.gap_log: list[GapRecord] = []
        self.stop_reason: str = ""
        self.rounds_used: int = 0
        self.hot_items: list[str] = []
        self.pages_read = 0
        self.empty_streak = 0

    # ------------------------------------------------------------------ #
    # 入口
    # ------------------------------------------------------------------ #
    def _llm_active(self) -> bool:
        """真能用模型吗？配了 Key 但连通性自检失败时，这里返回 False。"""
        return self.llm.available and not self.llm_disabled

    def run(self) -> Investigation:
        self.tracer.log(
            "run_start",
            topic=self.topic,
            logged_in=self.logged_in,
            llm_configured=self.llm.available,
            max_searches=self.budget.max_searches,
            max_pages=self.budget.max_pages,
            max_minutes=self.budget.max_minutes,
        )
        if not self.logged_in:
            self.limitations.append(
                Limitation(
                    step="登录态",
                    detail="未登录微博。搜索页通常只能读到首屏内容，翻页与详情页复核会失败或被重定向；"
                    "本轮结果为游客可见范围内的样本。",
                )
            )

        # 模型连通性自检：让 401/402/404 在第一步就暴露，而不是等到事件归纳时才说"不可解析"
        if self.llm.available:
            ok, detail = self.llm.ping()
            if ok:
                self.log(f"[0/5] 模型连通性自检通过（{self.llm.model} 回复：{detail}）")
            else:
                self.log(f"[0/5] 模型不可用：{detail}")
                self.log("      将降级为 heuristic 模式（规则聚类），关键词规划不再由模型完成")
                self.llm_disabled = True
                self.limitations.append(
                    Limitation(step="模型调用", detail=f"连通性自检失败，全程降级 heuristic：{detail}")
                )

        self.tracer.log(
            "llm_mode", active=self._llm_active(), model=self.llm.model, error=self.llm.last_error
        )

        self.log("[1/5] 读取公开热搜榜单（无需登录）……")
        self.hot_items = read_hot_board(self.session)
        if not self.hot_items:
            self.limitations.append(
                Limitation(step="读取热搜榜单", detail="weibo.cn/pub 未返回可解析条目")
            )
        else:
            self.log(f"      拿到 {len(self.hot_items)} 条榜单条目")

        self.log("[2/5] 第一轮搜索……")
        round1 = self._plan_round1()
        self._search_many(round1, round_no=1)
        self.rounds_used = 1

        self.log(f"[3/5] 归纳事件（共读到 {len(self.posts)} 条去重帖子）……")
        events = self._dedupe_events(self._build_events())
        self.tracer.log("events_built", count=len(events), names=[e.name for e in events], round=1)

        self.log("[4/5] 缺口判定与补搜……")
        events = self._run_gap_rounds(events)

        self.log("[5/5] 证据回填与详情页复核……")
        events = [self._attach_evidence(ev) for ev in events]
        # 只剩“防编造校验”提示的事件不算有证据，留着会让报告看起来查到了东西
        events = [ev for ev in events if any(e.kind in REAL_EVIDENCE_KINDS for e in ev.evidence)]

        inv = Investigation(
            topic=f"{self.topic} 微博舆情",
            mode="llm" if self._llm_active() else "heuristic",
            events=events,
            searches=self.searches,
            limitations=dedupe_limitations(self.limitations),
            gaps=self.gap_log,
            stop_reason=self.stop_reason,
        )
        inv.metrics = self._metrics(inv)
        self.tracer.log("run_end", **inv.metrics.model_dump())
        return inv

    # ------------------------------------------------------------------ #
    # 缺口驱动的补搜循环
    # ------------------------------------------------------------------ #
    def _run_gap_rounds(self, events: list[Event]) -> list[Event]:
        """反复「算缺口 → 挑词 → 搜 → 重新归纳」，直到没得补或预算用尽。

        每一轮都必须满足两个条件才继续：有可执行的缺口，且上一轮真的带来了新帖子。
        第二个条件是对微博这类数据源的现实妥协——同一个话题第二轮往往返回同一批帖子，
        再搜只是重复消耗配额和触发风控。
        """
        pending: list[tuple[str, str, GapRecord]] = []
        round_no = 2
        while round_no <= self.budget.max_rounds:
            gaps = detect_gaps(
                events,
                self.posts,
                [s.keyword for s in self.searches],
                self.hot_items,
                self.topic,
                min_evidence=self.budget.min_evidence,
                min_posts=self.budget.min_posts,
            )
            self._resolve_pending(pending, gaps, events)
            self.tracer.log(
                "gaps",
                round=round_no,
                count=len(gaps),
                gaps=[g.as_dict() for g in gaps],
            )
            if gaps:
                self.log(
                    f"      第 {round_no - 1} 轮后缺口 {len(gaps)} 个："
                    + "；".join(g.line() for g in gaps[:3])
                    + ("…" if len(gaps) > 3 else "")
                )

            if self.budget.exhausted(len(self.searches), self.pages_read):
                # 说清楚是哪一项用尽，否则"预算耗尽"会让人误以为是时间不够
                hits: list[str] = []
                if len(self.searches) >= self.budget.max_searches:
                    hits.append(f"搜索次数达上限 {self.budget.max_searches} 组")
                if self.pages_read >= self.budget.max_pages:
                    hits.append(f"页面数达上限 {self.budget.max_pages}")
                if self.budget.minutes_left() <= 0:
                    hits.append(f"时间用尽（上限 {self.budget.max_minutes:g} 分钟）")
                self.stop_reason = "预算耗尽：" + "；".join(hits or ["未知"])
                self.tracer.log("round_stop", round=round_no, reason=self.stop_reason)
                break

            plan = self._plan_from_gaps(gaps, round_no)
            if not plan.keywords:
                self.stop_reason = gap_stop_reason(gaps, plan)
                self.tracer.log("round_stop", round=round_no, reason=self.stop_reason)
                self.log(f"      停止：{self.stop_reason}")
                break

            self.log(
                f"      第 {round_no} 轮补搜：" + "、".join(plan.keywords)
                + f"（依据：{plan.reason_for(plan.keywords[0]).split('：')[0]}）"
            )
            self.tracer.log(
                "round_plan",
                round=round_no,
                keywords=plan.keywords,
                reasons={g.line(): kws for g, kws in plan.assignments},
            )

            before = len(self.posts)
            # 每个判出来的缺口都记一笔，包括这一轮没轮到搜的：
            # 只记"搜了的"会让报告看起来缺口全被处理过，实际有的从头到尾没管。
            for gap in gaps:
                assigned = [k for k in plan.keywords if k in gap.candidates]
                rec = GapRecord(
                    round=round_no,
                    kind=gap.kind,
                    target=gap.target,
                    detail=gap.detail,
                    action="、".join(assigned),
                )
                self.gap_log.append(rec)
                pending.append((gap.kind, gap.target, rec))

            self._search_many(plan.keywords, round_no=round_no)
            self.rounds_used = round_no

            if len(self.posts) == before:
                self.stop_reason = (
                    f"第 {round_no} 轮补搜未带来新帖子，继续搜索只会重复同一批内容"
                )
                self.tracer.log("round_stop", round=round_no, reason=self.stop_reason)
                self.log(f"      停止：{self.stop_reason}")
                break

            events = self._dedupe_events(self._build_events())
            self.tracer.log(
                "events_built",
                count=len(events),
                names=[e.name for e in events],
                round=round_no,
            )
            round_no += 1
        else:
            if not self.stop_reason:
                self.stop_reason = f"达到轮次上限 {self.budget.max_rounds} 轮"

        # 循环结束后还挂着的缺口，说明补了但没补上，也要留痕
        self._resolve_pending(pending, detect_gaps(
            events,
            self.posts,
            [s.keyword for s in self.searches],
            self.hot_items,
            self.topic,
            min_evidence=self.budget.min_evidence,
            min_posts=self.budget.min_posts,
        ), events)
        if not self.stop_reason:
            self.stop_reason = "各事件证据已达标，无缺口需要补搜"
        return events

    def _resolve_pending(
        self,
        pending: list[tuple[str, str, GapRecord]],
        gaps,
        events: list[Event],
    ) -> None:
        """回看上一轮的缺口在这轮是否消失。

        事件名每轮都可能被模型改写（实测同一事件三轮三个名字），所以不能按名字精确匹配，
        否则所有缺口都会变成"无法判定"。这里按相似度找最接近的事件：
        对上了就看该事件是否还有同类缺口；对不上才记 None。
        """
        current = {(g.kind, g.target) for g in gaps}
        by_name: dict[str, set[str]] = {}
        for g in gaps:
            by_name.setdefault(g.target, set()).add(g.kind)
        names = [e.name for e in events]

        for kind, target, rec in pending:
            if target == SAMPLE:
                rec.resolved = (kind, SAMPLE) not in current
                continue
            best, score = "", 0.0
            for name in names:
                s = similarity(target, name)
                if s > score:
                    best, score = name, s
            # 阈值取 0.55：低于此值说明事件被视为换了一件，判定无意义
            rec.resolved = kind not in by_name.get(best, set()) if score >= 0.55 else None
        pending.clear()

    def _plan_from_gaps(self, gaps, round_no: int):
        """先由代码按优先级排好，再让模型在候选里挑；模型挑不中就用代码的结果。"""
        searched = [s.keyword for s in self.searches]
        searched_set = set(searched)
        pool = candidate_pool(self.posts, searched_set)
        hot = hot_candidates(self.hot_items, self.topic, searched_set)
        # 只有"还没扩展过任何关键词"时才允许保底搜索；缺口清零后再搜就是白烧配额
        first_expansion = round_no == 2 and len(self.searches) < 2
        plan = plan_round(gaps, pool, hot, searched, allow_fallback=first_expansion)

        if not self._llm_active() or not plan.keywords:
            return plan

        # 模型只能在候选里挑，候选之外的一律丢弃
        allowed: dict[str, str] = {}
        for gap in gaps:
            for kw in gap.candidates:
                allowed.setdefault(kw, gap.line())
        for kw in plan.keywords:
            allowed.setdefault(kw, "代码按优先级选出的候选")

        payload = self.llm.chat_json(
            SYSTEM_PROMPT,
            gap_plan_prompt(
                self.topic,
                [g.line() for g in gaps],
                list(allowed)[:12],
                searched,
                round_no,
            ),
            purpose=f"gap_plan_r{round_no}",
        )
        planned = [
            str(k).strip().strip("#") for k in (payload or {}).get("keywords") or []
        ]
        valid = [k for k in planned if k in allowed and k not in searched_set]
        if not valid:
            if payload:
                self.tracer.log(
                    "gap_plan_rejected",
                    round=round_no,
                    planned=planned,
                    reason="模型给出的词不在候选内，已丢弃并改用代码排序",
                )
            return plan

        # 把模型选中的词还回它对应的缺口，保证补搜记录能对上
        plan.keywords = valid[:3]
        plan.assignments = []
        for gap in gaps:
            picked = [k for k in plan.keywords if k in gap.candidates]
            if picked:
                plan.assignments.append((gap, picked))
        assigned = {k for _, kws in plan.assignments for k in kws}
        rest = [k for k in plan.keywords if k not in assigned]
        if rest:
            plan.assignments.append(
                (Gap("FALLBACK", SAMPLE, "模型补充的候选词", 9, rest), rest)
            )
        return plan

    # ------------------------------------------------------------------ #
    # 关键词规划
    # ------------------------------------------------------------------ #
    def _plan_round1(self) -> list[str]:
        keywords: list[str] = []
        for kw in [self.topic, *self.seed_keywords]:
            kw = (kw or "").strip().strip("#")
            if kw and kw not in keywords:
                keywords.append(kw)

        related = [h.strip("#") for h in self.hot_items if _topic_match(h, self.topic)]
        if self._llm_active():
            payload = self.llm.chat_json(
                SYSTEM_PROMPT,
                keyword_plan_prompt(self.topic, self.hot_items, keywords, 1),
                purpose="keyword_plan_r1",
            )
            planned = (payload or {}).get("keywords") or []
            for kw in planned:
                kw = str(kw).strip().strip("#")
                if kw and kw not in keywords:
                    keywords.append(kw)
        for kw in related[:3]:
            if kw not in keywords:
                keywords.append(kw)
        return keywords[:5]

    # ------------------------------------------------------------------ #
    # 搜索与读取
    # ------------------------------------------------------------------ #
    def _search_many(self, keywords: list[str], round_no: int) -> None:
        for kw in keywords:
            if self.budget.exhausted(len(self.searches), self.pages_read):
                self.limitations.append(
                    Limitation(step=f"第 {round_no} 轮搜索", detail=f"预算耗尽，跳过「{kw}」")
                )
                break
            before = len(self.posts)
            if self.searches and self.search_delay > 0:
                time.sleep(self.search_delay)  # 关键词之间主动节流
            result = collect_search(
                self.session, kw, max_posts=self.budget.per_keyword_posts, api_pages=2
            )
            self.pages_read += 2
            for post in result.posts:
                key = post.get("mid") or post.get("url") or (post.get("text") or "")[:60]
                if key and key not in self.posts:
                    self.posts[key] = post
            new_posts = len(self.posts) - before
            self.searches.append(
                SearchRecord(
                    keyword=kw,
                    url=result.url,
                    posts_read=len(result.posts),
                    new_posts=new_posts,
                    round=round_no,
                    limitation=("；".join(result.limitations))[:260],
                )
            )
            reason = "；".join(result.limitations)
            self.tracer.log(
                "search",
                round=round_no,
                keyword=kw,
                posts_read=len(result.posts),
                new_posts=new_posts,
                limitations=result.limitations,
            )
            self.log(
                f"      「{kw}」读取 {len(result.posts)} 条，新增 {new_posts} 条"
                + (f"（限制：{reason[:60]}…）" if reason else "")
            )

            if result.posts:
                self.empty_streak = 0
                continue

            # 空结果：多半是游客态登录墙或限流，拉长冷却再走下一个关键词
            self.empty_streak += 1
            cooldown = 10 if ("登录" in reason or "passport" in reason) else 5
            if self.empty_streak >= 3:
                self.limitations.append(
                    Limitation(
                        step="搜索",
                        detail=f"连续 {self.empty_streak} 个关键词未读到帖子，提前结束本轮以避免继续触发风控",
                    )
                )
                break
            self.log(f"      空结果，冷却 {cooldown}s 后继续")
            time.sleep(cooldown)

    # ------------------------------------------------------------------ #
    # 事件归纳
    # ------------------------------------------------------------------ #
    def _build_events(self) -> list[Event]:
        posts = list(self.posts.values())
        if not posts:
            self.limitations.append(Limitation(step="事件归纳", detail="没有读到任何帖子，无法归纳事件"))
            return []

        if self._llm_active():
            shown = posts[:DIGEST_LIMIT]
            payload = self.llm.chat_json(
                SYSTEM_PROMPT,
                event_build_prompt(self.topic, digest_posts(posts), len(posts), self.hot_items),
                purpose="event_build",
            )
            # 序号映射必须用**给模型看的那一批**帖子，不能是全部：
            # 输入被截断时，第 70 条并不在模型眼前，按序号映射会张冠李戴。
            index_map = {
                i: str(p.get("mid")) for i, p in enumerate(shown, 1) if p.get("mid")
            }
            events = self._events_from_llm(payload, index_map)
            if events:
                return events
            # 区分"模型没返回"和"返回了但结构不对"，两者修法完全不同
            reason = self.llm.last_error or "模型返回的 JSON 里没有可用的 events 数组"
            self.log(f"      模型归纳失败：{reason}")
            self.limitations.append(
                Limitation(step="事件归纳", detail=f"模型归纳失败，降级为频率聚类；真实原因：{reason}")
            )

        return self._events_heuristic()

    def _dedupe_events(self, events: list[Event]) -> list[Event]:
        """合并被同一个话题标签拆开的事件。

        这一步必须在详情页复核之前：合并后同一条帖子只复核一次，省预算。
        """
        merged, records = merge_events(events)
        for rec in records:
            self.tracer.log(
                "merge",
                kept=rec.kept,
                absorbed=rec.absorbed,
                similarity=rec.similarity,
            )
        if records:
            self.log(
                f"      合并 {len(records)} 组重复事件："
                + "；".join(f"「{r.absorbed}」并入「{r.kept}」" for r in records)
            )
        return merged

    def _resolve_mids(self, values, index_map: dict[int, str]) -> tuple[list[str], int]:
        """把模型给的引用解析成 mid。

        实测里模型会把摘要的“第 N 条”当成 mid 填回来——它看到的每条记录都有序号，
        而 mid 是一串长数字，模型倾向于挑更短的那个。这是引用格式问题，不是编造，
        所以按序号做**确定性映射**：第 N 条就是输入里的第 N 条，可逐条核对。
        映射了几条要记进 limitations，不能悄悄发生。
        """
        out: list[str] = []
        mapped = 0
        for value in values or []:
            text = str(value).strip().strip("[]")
            if not text:
                continue
            if text in self.posts:
                mid = text
            elif text.isdigit() and int(text) in index_map:
                mid = index_map[int(text)]
                mapped += 1
            else:
                mid = text  # 保留原值，交给白名单过滤时丢弃并留痕
            if mid not in out:
                out.append(mid)
        return out, mapped

    def _coerce_llm_facts(
        self,
        raw,
        fallback_mids: list[str],
        index_map: dict[int, str] | None = None,
    ) -> list[Fact]:
        """模型可能给字符串数组，也可能给带 supporting_mids 的对象数组，两种都收。

        三种情况必须区分开：
        - 模型没标支撑 → 退到事件级证据，标 inferred_link（可当线索，不可当依据）；
        - 模型标了且 mid 有效 → 保留，inferred_link=False；
        - 模型标了但 mid 全无效 → 清空支撑，标 inferred_link，绝不用 fallback 掩盖编造。
        """
        valid_fallback = [m for m in fallback_mids if m in self.posts]
        out: list[Fact] = []
        for item in raw or []:
            if isinstance(item, str):
                text = item.strip()
                if text:
                    out.append(
                        Fact(text=text, supporting_mids=list(valid_fallback), inferred_link=True)
                    )
                continue
            if not isinstance(item, dict):
                continue

            text = str(item.get("text") or item.get("fact") or "").strip()
            if not text:
                continue

            claimed, _ = self._resolve_mids(
                item.get("supporting_mids") or item.get("mids") or [], index_map or {}
            )
            valid = [m for m in claimed if m in self.posts]
            if claimed and not valid:
                supporting: list[str] = []
            else:
                supporting = valid or list(valid_fallback)

            out.append(
                Fact(
                    text=text,
                    kind="meta" if str(item.get("kind") or "").lower() == "meta" else "claim",
                    supporting_mids=supporting,
                    source_kind=_norm_source_kind(item.get("source_kind")),
                    inferred_link=not valid,
                )
            )
        return out

    def _events_from_llm(
        self, payload: dict | None, index_map: dict[int, str] | None = None
    ) -> list[Event]:
        if not payload:
            return []
        raw_events = payload.get("events")
        if not isinstance(raw_events, list):
            return []
        index_map = index_map or {}
        out: list[Event] = []
        mapped_total = 0
        for item in raw_events[: self.max_events]:
            if not isinstance(item, dict) or not item.get("name"):
                continue
            mids, mapped = self._resolve_mids(item.get("evidence_mids") or [], index_map)
            mapped_total += mapped
            ev = Event(
                name=str(item.get("name")).strip(),
                summary=str(item.get("summary") or ""),
                heat=_norm_heat(item.get("heat")),
                heat_basis=str(item.get("heat_basis") or ""),
                facts=self._coerce_llm_facts(item.get("facts"), mids, index_map),
                opinions=[str(x) for x in item.get("opinions") or []],
                speculations=[str(x) for x in item.get("speculations") or []],
                judgement=str(item.get("judgement") or ""),
                keywords=[str(x) for x in item.get("keywords") or []],
            )
            if item.get("controversy"):
                ev.judgement = (ev.judgement + "\n争议：" + str(item["controversy"])).strip()
            ev.evidence = self._evidence_from_mids(mids)
            out.append(ev)

        if mapped_total:
            detail = (
                f"模型用摘要里的「第 N 条」序号引用证据 {mapped_total} 处，"
                "已按输入顺序映射为对应 mid（可逐条核对，非补全）"
            )
            self.limitations.append(Limitation(step="证据引用", detail=detail))
            self.tracer.log("mids_mapped", count=mapped_total, detail=detail)
        return out

    def _events_heuristic(self) -> list[Event]:
        """无模型时的降级方案：先按正文话题词聚类，剩下的按搜索关键词兜底。"""
        posts = list(self.posts.values())
        tagged: dict[str, list[dict]] = defaultdict(list)
        residual: dict[str, list[dict]] = defaultdict(list)
        for post in posts:
            tags = post.get("hashtags") or []
            if tags:
                tagged[tags[0]].append(post)
            else:
                residual[post.get("keyword") or "未分类"].append(post)

        # 话题词聚类优先，关键词兜底组排在后面
        candidates: list[tuple[str, list[dict], bool]] = [
            (k, v, False) for k, v in tagged.items()
        ] + [(k, v, True) for k, v in residual.items()]
        candidates.sort(key=lambda item: (item[2], -len(item[1])))

        total = max(1, len(posts))
        events: list[Event] = []
        for key, items, is_residual in candidates:
            if len(events) >= self.max_events:
                break
            # 兜底组只在事件数不足时才用，避免出现跟主题同名的空泛事件
            if is_residual and len(events) >= 3:
                continue
            if len(items) < 2 and len(events) >= 3:
                continue
            events.append(self._make_heuristic_event(key, items, is_residual, total))
        return events

    def _make_heuristic_event(
        self, key: str, items: list[dict], is_residual: bool, total: int
    ) -> Event:
        ratio = len(items) / total
        heat = "high" if ratio >= 0.3 else ("medium" if ratio >= 0.12 else "low")
        authors = Counter(i.get("author") for i in items if i.get("author"))
        verified_n = sum(1 for i in items if i.get("verified"))
        with_link = sum(1 for i in items if i.get("url"))
        times = sorted({i.get("time") for i in items if i.get("time")})
        top_authors = "、".join(a for a, _ in authors.most_common(3)) or "未知"
        sample = (items[0].get("text") or "")[:120].replace("\n", " ")
        name = f"{key}（未带话题词的帖子）" if is_residual else key

        # 只用"我们自己抓到的样本可核对的量"作为事实，不做语义推断。
        # 全部标 meta：这些是对本次样本的统计，不是关于事件本身的断言，
        # 送去事实核查没有意义，handoff 会跳过。
        facts: list[Fact] = [
            Fact(
                text=f"样本内 {len(items)} 条帖子归入该聚类，占已读 {total} 条的 {ratio:.0%}。",
                kind="meta",
            ),
            Fact(
                text=f"其中 {verified_n} 条来自微博认证账号，{with_link} 条具备可验证直链。",
                kind="meta",
            ),
            Fact(text=f"出现频次最高的作者：{top_authors}。", kind="meta"),
        ]
        if times:
            facts.append(
                Fact(
                    text=f"样本中可见的发布时间共 {len(times)} 种，"
                    f"最早 / 最晚为 {times[0]} / {times[-1]}。",
                    kind="meta",
                )
            )

        ev = Event(
            name=name,
            summary=f"按{'正文话题词' if not is_residual else '搜索关键词'}「{key}」聚类，"
            f"样本 {len(items)} 条。代表文本：{sample}",
            heat=heat,
            heat_basis=f"该聚类在已读 {total} 条中占 {len(items)} 条（{ratio:.0%}）",
            keywords=[key],
            facts=facts,
            opinions=[],
            speculations=[
                "本事件由关键词频率聚成，尚未接入模型做语义级的事实/观点拆分；"
                "上文'事实'仅指样本层面可核对的信息，不代表事件本身已被权威来源确认。"
            ],
            judgement=(
                "启发式聚类结果，只能作为事件候选。要判断事件真伪、各方观点与争议点，"
                "需要配置 OPENAI_API_KEY 走 llm 模式，或人工复核。"
            ),
        )
        ev.evidence = self._evidence_from_posts(items)
        return ev

    # ------------------------------------------------------------------ #
    # 证据
    # ------------------------------------------------------------------ #
    def _evidence_from_posts(self, posts: list[dict], limit: int = 5) -> list[Evidence]:
        out: list[Evidence] = []
        for post in posts[:limit]:
            out.append(
                Evidence(
                    kind="weibo_post",
                    mid=str(post.get("mid") or ""),
                    author=post.get("author") or "",
                    time=post.get("time") or "",
                    content=(post.get("text") or "")[:300],
                    url=post.get("url") or "",
                    verified=bool(post.get("detail_verified")),
                    note="来源：" + (post.get("evidence_source") or "unknown"),
                )
            )
        return out

    def _evidence_from_mids(self, mids: list[str]) -> list[Evidence]:
        picked = [self.posts[m] for m in mids if m in self.posts]
        missing = [m for m in mids if m not in self.posts]
        out = self._evidence_from_posts(picked)
        if missing:
            out.append(
                Evidence(
                    kind="other",
                    content="（校验留痕，非证据）模型引用了未在抓取结果中出现的 mid，已丢弃："
                    + ", ".join(missing[:5]),
                    note="防编造校验",
                )
            )
        return out

    def _attach_evidence(self, ev: Event) -> Event:
        """对每个事件的证据做详情页复核（默认 2 条），并补齐官方来源字段。"""
        if not ev.evidence:
            return ev
        checked = 0
        for item in ev.evidence:
            if item.kind != "weibo_post" or not item.url:
                continue
            if checked >= self.budget.detail_checks_per_event:
                break
            if not self.budget.page_budget_left(self.pages_read):
                self.limitations.append(
                    Limitation(step="详情页复核", detail="页面/时间预算耗尽，剩余证据未复核")
                )
                break
            mid = item.mid or item.url.rstrip("/").split("/")[-1]
            info = verify_detail(self.session, mid, item.author, ev.keywords[0] if ev.keywords else "")
            self.pages_read += 1
            checked += 1
            if info.get("ok") and info.get("post"):
                fresh = info["post"]
                item.author = fresh.get("author") or item.author
                item.time = fresh.get("time") or item.time
                item.content = (fresh.get("text") or item.content)[:300]
                item.url = fresh.get("url") or item.url
                item.verified = True
                item.note = "详情页已复核（作者/时间/正文一致）"
                # 回写到原始样本，便于统计"详情页复核通过"的数量
                if mid in self.posts:
                    self.posts[mid].update(
                        {
                            "author": item.author,
                            "time": item.time,
                            "text": fresh.get("text") or self.posts[mid].get("text", ""),
                            "detail_verified": True,
                        }
                    )
            else:
                item.note = (item.note + "；" if item.note else "") + (
                    "详情页复核未通过：" + (info.get("reason") or "未知原因")
                )
                self.limitations.append(
                    Limitation(step="详情页复核", detail=f"{item.url} -> {info.get('reason')}")
                )
        return ev

    # ------------------------------------------------------------------ #
    # 指标
    # ------------------------------------------------------------------ #
    def _metrics(self, inv: Investigation) -> Metrics:
        posts = list(self.posts.values())
        return Metrics(
            keywords_searched=len(self.searches),
            posts_read=len(posts),
            posts_with_verified_link=sum(1 for p in posts if p.get("url")),
            posts_detail_verified=sum(1 for p in posts if p.get("detail_verified")),
            events_formed=len(inv.events),
            evidence_items=sum(
                1
                for e in inv.events
                for item in e.evidence
                if item.kind in REAL_EVIDENCE_KINDS
            ),
            speculations=sum(len(e.speculations) for e in inv.events),
            rounds_used=self.rounds_used,
            gaps_detected=len(self.gap_log),
            gaps_resolved=sum(1 for g in self.gap_log if g.resolved),
            **self.llm.stats(),
        )


def _norm_heat(value) -> str:
    text = str(value or "").strip().lower()
    if text in {"high", "medium", "low"}:
        return text
    if text in {"高", "很高", "极高"}:
        return "high"
    if text in {"中", "中等"}:
        return "medium"
    if text in {"低", "较低"}:
        return "low"
    return "unknown"


def write_outputs(inv: Investigation, out_dir: str | Path) -> tuple[Path, Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stamp = inv.generated_at.strftime("%Y%m%d_%H%M%S")
    json_path = out / "investigation.json"
    md_path = out / "investigation.md"
    json_path.write_text(
        json.dumps(inv.model_dump(mode="json"), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    md_path.write_text(inv.to_markdown(), encoding="utf-8")
    (out / f"investigation_{stamp}.json").write_text(
        json_path.read_text(encoding="utf-8"), encoding="utf-8"
    )
    return json_path, md_path


def save_raw_posts(posts: list[dict], out_dir: str | Path, name: str = "raw_posts.json") -> Path:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / name
    path.write_text(json.dumps(posts, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
