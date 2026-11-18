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

import json
import os
import re
import time
from collections import Counter, defaultdict
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
    keyword_plan_prompt,
)
from .merge import merge_events
from .schemas import (
    Event,
    Evidence,
    Fact,
    Investigation,
    Limitation,
    Metrics,
    SearchRecord,
)
from .trace import Tracer

_VALID_SOURCE_KINDS = {
    "weibo_post",
    "official_site",
    "media_report",
    "hot_board",
    "unknown",
}


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
    """

    def __init__(self, api_key: str | None = None, model: str | None = None) -> None:
        self.api_key = api_key or os.getenv("OPENAI_API_KEY") or ""
        self.model = model or os.getenv("OPENAI_MODEL") or "gpt-4.1-mini"
        self.base_url = os.getenv("OPENAI_BASE_URL") or None
        self.available = bool(self.api_key)
        self.last_error: str = ""
        self._client = None
        if self.available:
            try:
                from openai import OpenAI

                self._client = OpenAI(api_key=self.api_key, base_url=self.base_url)
            except Exception as exc:  # noqa: BLE001
                self.available = False
                self.last_error = f"客户端初始化失败：{exc}"

    def ping(self) -> tuple[bool, str]:
        """最小连通性探针：只发一句 hello，用于 --doctor 与启动前自检。"""
        if not self.available or self._client is None:
            return False, self.last_error or "未配置 OPENAI_API_KEY"
        try:
            resp = self._client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": "只回复 ok"}],
                max_tokens=5,
            )
            return True, (resp.choices[0].message.content or "").strip()[:40]
        except Exception as exc:  # noqa: BLE001
            reason = _describe_api_error(exc)
            self.last_error = reason
            return False, reason

    def chat_json(self, system: str, user: str) -> dict | None:
        if not self.available or self._client is None:
            self.last_error = self.last_error or "未配置 OPENAI_API_KEY"
            return None
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        errors: list[str] = []
        for kwargs in ({"response_format": {"type": "json_object"}}, {}):
            try:
                resp = self._client.chat.completions.create(
                    model=self.model, messages=messages, temperature=0.2, **kwargs
                )
            except Exception as exc:  # noqa: BLE001
                reason = _describe_api_error(exc)
                errors.append(reason)
                # 认证/余额/模型不存在这类错误，换参数重试没有意义
                if _is_fatal_api_error(exc):
                    break
                continue
            content = resp.choices[0].message.content or ""
            payload = _loads_loose(content)
            if payload is not None:
                self.last_error = ""
                return payload
            errors.append(f"返回内容无法解析为 JSON（前 120 字：{content[:120]!r}）")
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
        tracer: Tracer | None = None,
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

        self.posts: dict[str, dict] = {}
        self.searches: list[SearchRecord] = []
        self.limitations: list[Limitation] = []
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

        self.log(f"[1/5] 读取公开热搜榜单（无需登录）……")
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

        self.log("[3/5] 关键词扩展（自主扩展轮）……")
        round2 = self._plan_expansion()
        if round2:
            self._search_many(round2, round_no=2)
        else:
            self.limitations.append(
                Limitation(step="关键词扩展", detail="未从已读帖子中提取到可用的扩展关键词")
            )

        self.log(f"[4/5] 归纳事件（共读到 {len(self.posts)} 条去重帖子）……")
        events = self._build_events()
        self.tracer.log("events_built", count=len(events), names=[e.name for e in events])
        events = self._dedupe_events(events)

        self.log("[5/5] 证据回填与详情页复核……")
        events = [self._attach_evidence(ev) for ev in events]
        events = [ev for ev in events if ev.evidence]

        inv = Investigation(
            topic=f"{self.topic} 微博舆情",
            mode="llm" if self._llm_active() else "heuristic",
            events=events,
            searches=self.searches,
            limitations=self.limitations,
        )
        inv.metrics = self._metrics(inv)
        self.tracer.log("run_end", **inv.metrics.model_dump())
        return inv

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

    def _plan_expansion(self) -> list[str]:
        """从已读帖子的话题词里挑扩展关键词（保证至少扩展一次）。"""
        counter: Counter[str] = Counter()
        for post in self.posts.values():
            for tag in post.get("hashtags") or []:
                counter[tag] += 1

        searched = {s.keyword for s in self.searches}
        candidates = [
            tag for tag, count in counter.most_common(20) if count >= 2 and tag not in searched
        ]

        if self._llm_active() and candidates:
            payload = self.llm.chat_json(
                SYSTEM_PROMPT,
                keyword_plan_prompt(
                    self.topic, [f"#{c}#" for c in candidates] + self.hot_items,
                    list(searched), 2,
                ),
            )
            planned = [str(k).strip().strip("#") for k in (payload or {}).get("keywords") or []]
            planned = [k for k in planned if k and k not in searched]
            if planned:
                return planned[:3]

        if candidates:
            return candidates[:2]
        # 退路：榜单里与主题相关但还没搜过的条目
        fallback = [
            h.strip("#")
            for h in self.hot_items
            if _topic_match(h, self.topic) and h.strip("#") not in searched
        ]
        return fallback[:2]

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
            payload = self.llm.chat_json(
                SYSTEM_PROMPT,
                event_build_prompt(self.topic, digest_posts(posts), len(posts), self.hot_items),
            )
            events = self._events_from_llm(payload)
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

    def _coerce_llm_facts(self, raw, fallback_mids: list[str]) -> list[Fact]:
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

            claimed = [
                str(m) for m in (item.get("supporting_mids") or item.get("mids") or [])
            ]
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

    def _events_from_llm(self, payload: dict | None) -> list[Event]:
        if not payload:
            return []
        raw_events = payload.get("events")
        if not isinstance(raw_events, list):
            return []
        out: list[Event] = []
        for item in raw_events[: self.max_events]:
            if not isinstance(item, dict) or not item.get("name"):
                continue
            mids = [str(m) for m in item.get("evidence_mids") or []]
            ev = Event(
                name=str(item.get("name")).strip(),
                summary=str(item.get("summary") or ""),
                heat=_norm_heat(item.get("heat")),
                heat_basis=str(item.get("heat_basis") or ""),
                facts=self._coerce_llm_facts(item.get("facts"), mids),
                opinions=[str(x) for x in item.get("opinions") or []],
                speculations=[str(x) for x in item.get("speculations") or []],
                judgement=str(item.get("judgement") or ""),
                keywords=[str(x) for x in item.get("keywords") or []],
            )
            if item.get("controversy"):
                ev.judgement = (ev.judgement + "\n争议：" + str(item["controversy"])).strip()
            ev.evidence = self._evidence_from_mids(mids)
            out.append(ev)
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
                    content="模型引用了未在抓取结果中出现的 mid，已丢弃：" + ", ".join(missing[:5]),
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
            evidence_items=sum(len(e.evidence) for e in inv.events),
            speculations=sum(len(e.speculations) for e in inv.events),
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
