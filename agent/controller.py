"""按缺口驱动的调查循环（Controller）。

原实现固定两轮：种子词 → 话题词扩展。问题是第二轮搜什么与"当前证据缺什么"无关，
已经攒了 8 条证据的事件和只有 1 条的事件被同等对待，预算花在已经够了的地方，
真正薄的事件反而没人管。

这一层把"要不要再搜、搜什么"变成显式判定：

1. 每轮结束后由**代码**算缺口，不交给模型判断"够不够"——让模型判"证据是否充分"，
   它永远会说"基本充分"，因为那是它自己刚归纳出来的东西；
2. 缺口映射到可搜关键词，候选只能来自已抓到的数据（正文话题词、榜单条目），
   不允许凭空构造"XX 官方回应"这类事件名；
3. 模型只能在这批候选里挑，挑到候选之外的直接丢弃——和 mid 白名单同一个道理；
4. 没有可行动缺口 / 补搜没有新数据 / 预算或轮次用尽 → 停，并写明停止原因。

停止条件是这一层真正值钱的部分：一个不会停的 Agent 会把预算均匀烧光，
最后还是拿着同一批证据写报告。
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

NEED_EVIDENCE = "NEED_EVIDENCE"
NEED_INDEPENDENT = "NEED_INDEPENDENT"
NEED_OFFICIAL = "NEED_OFFICIAL"
THIN_SAMPLE = "THIN_SAMPLE"
FALLBACK = "FALLBACK"

#: 缺口针对的是整体样本而不是某个事件时，target 用这个占位
SAMPLE = "整体样本"

KIND_LABEL = {
    NEED_EVIDENCE: "证据不足",
    NEED_INDEPENDENT: "信源不独立",
    NEED_OFFICIAL: "缺官方口径",
    THIN_SAMPLE: "样本过薄",
    FALLBACK: "保底扩展",
}


@dataclass
class Gap:
    kind: str
    target: str
    detail: str
    priority: int
    candidates: list[str] = field(default_factory=list)

    @property
    def actionable(self) -> bool:
        """有没有可执行的搜索动作。没有候选就只是记录，不该假装能补。"""
        return bool(self.candidates)

    def line(self) -> str:
        return f"[{KIND_LABEL.get(self.kind, self.kind)}] {self.target}：{self.detail}"

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "target": self.target,
            "detail": self.detail,
            "candidates": list(self.candidates),
            "actionable": self.actionable,
        }


@dataclass
class RoundPlan:
    """一轮补搜的计划：搜哪些词，以及每个词是为哪个缺口服务的。"""

    keywords: list[str] = field(default_factory=list)
    assignments: list[tuple[Gap, list[str]]] = field(default_factory=list)

    def reason_for(self, keyword: str) -> str:
        for gap, kws in self.assignments:
            if keyword in kws:
                return gap.line()
        return ""


def candidate_pool(
    posts: dict[str, dict], searched: set[str], limit: int = 8, min_count: int = 2
) -> list[str]:
    """候选词只能来自已抓到的帖子正文话题词，按出现频次排序。

    `min_count=2` 是噪声过滤：微博搜索结果里总混着几条不相干的帖子，
    它们的话题标签只出现一次（实测出现过「全妆出街」这种）。拿这种词去补搜，
    只会把更多不相干的帖子拉进样本，还得再花轮次清理。
    """
    counter: Counter[str] = Counter()
    for post in posts.values():
        for tag in post.get("hashtags") or []:
            tag = str(tag or "").strip().strip("#")
            if tag:
                counter[tag] += 1
    out: list[str] = []
    for tag, count in counter.most_common(limit * 3):
        if count < min_count:
            continue
        if tag not in searched and tag not in out:
            out.append(tag)
        if len(out) >= limit:
            break
    return out


def hot_candidates(hot_items: list[str], topic: str, searched: set[str], limit: int = 3) -> list[str]:
    """榜单里与主题相关、且尚未搜过的条目。"""
    out: list[str] = []
    for item in hot_items:
        kw = str(item or "").strip().strip("#")
        if not kw or kw in searched or kw in out:
            continue
        if topic.lower() in kw.lower():
            out.append(kw)
        if len(out) >= limit:
            break
    return out


def _for_event(event, searched: set[str], pool: list[str], limit: int = 3) -> list[str]:
    """为一个事件挑候选词：先事件自身关键词，再退到样本话题词池。"""
    out: list[str] = []
    for kw in [*(event.keywords or []), *pool]:
        kw = str(kw or "").strip().strip("#")
        if kw and kw not in searched and kw not in out:
            out.append(kw)
        if len(out) >= limit:
            break
    return out


def detect_gaps(
    events,
    posts: dict[str, dict],
    searched: list[str] | set[str],
    hot_items: list[str] | None = None,
    topic: str = "",
    min_evidence: int = 3,
    min_authors: int = 2,
    min_posts: int = 10,
) -> list[Gap]:
    """算出这一轮结束时的证据缺口。

    判定顺序有意义：证据条数不够时，先补数量；数量够了才谈信源独立性；
    独立性够了才谈有没有官方口径。否则一个只有 1 条证据的事件会同时被要求
    "补证据""补独立信源""补官方"，三份预算都花在同一件事上。
    """
    searched_set = {str(s) for s in searched}
    pool = candidate_pool(posts, searched_set)
    gaps: list[Gap] = []

    for ev in events:
        mids = [e.mid for e in ev.evidence if e.kind == "weibo_post" and e.mid in posts]
        known = [posts[m] for m in mids if posts[m]]
        n = len(known)
        if n < min_evidence:
            gaps.append(
                Gap(
                    NEED_EVIDENCE,
                    ev.name,
                    f"证据 {n} 条，低于下限 {min_evidence} 条",
                    priority=1,
                    candidates=_for_event(ev, searched_set, pool),
                )
            )
            continue

        authors = {p.get("author") for p in known if p.get("author")}
        if len(authors) < min_authors:
            who = "、".join(sorted(authors)) or "未知"
            gaps.append(
                Gap(
                    NEED_INDEPENDENT,
                    ev.name,
                    f"{n} 条证据只来自 {len(authors)} 个账号（{who}）",
                    priority=2,
                    candidates=_for_event(ev, searched_set, pool),
                )
            )
            continue

        if not any(p.get("verified") for p in known):
            gaps.append(
                Gap(
                    NEED_OFFICIAL,
                    ev.name,
                    "证据均来自非认证账号，缺官方/权威口径",
                    priority=3,
                    candidates=_for_event(ev, searched_set, pool),
                )
            )

    if len(posts) < min_posts:
        gaps.append(
            Gap(
                THIN_SAMPLE,
                SAMPLE,
                f"已读 {len(posts)} 条，低于 {min_posts} 条",
                priority=0,
                # 样本薄时优先用榜单里与主题相关的条目：它们比单个帖子的话题词更"宽"
                candidates=hot_candidates(hot_items or [], topic, searched_set) or pool,
            )
        )
    return gaps


def plan_round(
    gaps: list[Gap],
    pool: list[str],
    hot: list[str],
    searched: list[str] | set[str],
    limit: int = 3,
    allow_fallback: bool = False,
) -> RoundPlan:
    """按优先级把缺口摊成这一轮的关键词。

    优先级高的缺口先拿词，拿满 limit 就停——一轮搜太多词既触发风控，
    也让"这一轮到底想补什么"变得不可解释。

    `allow_fallback` 只在"还没做过任何扩展"时为真：它的作用是保证首次扩展发生，
    而不是在没有缺口时继续搜。缺口清零后再搜，就是纯粹烧配额。
    """
    searched_set = {str(s) for s in searched}
    plan = RoundPlan()
    for gap in sorted([g for g in gaps if g.actionable], key=lambda g: g.priority):
        picked: list[str] = []
        for kw in gap.candidates:
            if kw in searched_set or kw in plan.keywords:
                continue
            plan.keywords.append(kw)
            picked.append(kw)
            if len(plan.keywords) >= limit:
                break
        if picked:
            plan.assignments.append((gap, picked))
        if len(plan.keywords) >= limit:
            break

    if not plan.keywords and allow_fallback:
        # 一个缺口都没有，但还没做过任何扩展：保底搜一次，
        # 避免"只搜一轮就收工"和验收项「自主扩展 ≥1 次」永远为空
        fallback = [kw for kw in [*pool, *hot] if kw not in searched_set][: min(2, limit)]
        if fallback:
            plan.keywords = fallback
            plan.assignments.append(
                (
                    Gap(FALLBACK, SAMPLE, "无显式缺口，按话题词覆盖度保底扩展", 9, fallback),
                    fallback,
                )
            )
    return plan


def stop_reason(gaps: list[Gap], plan: RoundPlan) -> str:
    """没有可执行的下一步时，用一句人能读懂的话交代为什么停。"""
    if plan.keywords:
        return ""
    actionable = [g for g in gaps if g.actionable]
    if gaps and not actionable:
        return (
            f"检测到 {len(gaps)} 个缺口，但已无未搜过的候选词，"
            "继续搜只会重复同一批帖子（缺口已记入报告）"
        )
    if not gaps:
        return "各事件证据已达标，无缺口需要补搜"
    return "无可执行的关键词，提前结束"
