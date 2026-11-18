"""把调查产物转成核查待办（handoff）。

这是原先那个"手工断层"的补丁：上一轮核查报告里的 8 条待核查说法，是我
人手读 investigation.md 抄出来的。有了 Fact.supporting_mids，这一步可以
由代码完成。

关键设计：调查报告自己声称的支撑帖子会被**登记为来源**并带进核查。
于是核查的起点不是"什么都没有"，而是"报告说有这几条证据"——核查要补的是
调查报告之外、且互相独立的来源。这恰好是 NEED_INDEPENDENT 缺口的判定依据。
"""

from __future__ import annotations

import re

from .schemas import Evidence, Fact, Investigation

# 调查证据类型 -> 核查来源类型
_KIND_MAP = {
    "weibo_post": "social_post",
    "official_site": "official_account",
    "media_report": "media",
    "hot_board": "aggregator",
    "other": "aggregator",
}

# 同一作者的多条微博只算一个独立信源，所以按作者分组
def _author_group(author: str) -> str:
    return f"weibo:{author.strip()}" if author.strip() else "weibo:unknown"


def _source_of(event_ev: Evidence) -> dict:
    # 用正文前缀当机检引文：核查时要验证"这条帖子确实这么说"，
    # 而不是只看链接有没有 200。截 60 字是因为详情页长文会被折叠。
    quote = re.sub(r"\s+", " ", event_ev.content or "").strip()[:60]
    return {
        "id": "",
        "kind": _KIND_MAP.get(event_ev.kind, "aggregator"),
        "publisher": event_ev.author or "未知",
        "title": (event_ev.content or "")[:60],
        "url": event_ev.url,
        "published_at": event_ev.time,
        "quote": quote,
        "independence_group": _author_group(event_ev.author),
        "note": "来自调查报告自身的证据"
        + ("（已详情页复核）" if event_ev.verified else "（未复核）"),
    }


def facts_to_claims(
    inv: Investigation, only_events: list[int] | None = None
) -> dict:
    """生成可直接喂给 `--factcheck` 的 dict。

    only_events 用 1 起的序号选择事件，不填则全部。
    """
    sources: list[dict] = []
    index: dict[str, str] = {}  # mid/url -> source id

    def register(item: Evidence) -> str:
        # kind="other" 是"防编造校验"留下的说明性条目，不是真来源
        if item.kind == "other":
            return ""
        key = item.mid or item.url or item.content[:40]
        if not key:
            return ""
        if key in index:
            return index[key]
        src = _source_of(item)
        src["id"] = f"S{len(sources) + 1}"
        index[key] = src["id"]
        sources.append(src)
        return src["id"]

    events = list(inv.events)
    if only_events:
        events = [ev for i, ev in enumerate(events, 1) if i in set(only_events)]

    claims: list[dict] = []
    inferred_facts = 0
    facts_without_evidence = 0
    skipped_meta = 0

    for e_idx, event in enumerate(events, 1):
        for f_idx, fact in enumerate(event.facts, 1):
            assert isinstance(fact, Fact)
            if fact.kind == "meta":
                # 「样本内 N 条帖子」这类统计描述的是本次抓取，不是事件本身，
                # 没有外部来源可核，送进核查只会产出无意义的「无法证实」。
                skipped_meta += 1
                continue
            cites: list[dict] = []
            for mid in fact.supporting_mids:
                match = next((x for x in event.evidence if x.mid == mid), None)
                if match is not None:
                    sid = register(match)
                    if sid:
                        cites.append({"source_id": sid, "point": "报告标注的支撑证据"})

            note = ""
            if not cites:
                # 逐条标注缺失时退到事件级证据，但必须如实说明这是自动关联
                for item in event.evidence:
                    sid = register(item)
                    if sid:
                        cites.append({"source_id": sid, "point": "事件级自动关联"})
                if cites:
                    note = "（支撑证据为事件级自动关联，非逐条标注）"
                    inferred_facts += 1

            if not cites:
                facts_without_evidence += 1

            claims.append(
                {
                    "id": f"C{len(claims) + 1}",
                    "claim": fact.text,
                    "report_line": f"事件 {e_idx}「{event.name}」事实第 {f_idx} 条",
                    "verdict": "待核查",
                    "confidence": "低",
                    "analysis": (
                        "由调查产物自动抽取，尚未独立核查。"
                        + ("原始事实未标注支撑证据。" if fact.inferred_link or not cites else "")
                        + note
                    ).strip(),
                    "supporting": cites,
                    "counter": [],
                }
            )

    return {
        "title": f"{inv.topic} 事实核查（由调查产物自动生成）",
        "original_report": (
            f"{inv.topic}，生成于 {inv.generated_at:%Y-%m-%d %H:%M}，"
            f"运行模式 {inv.mode}"
        ),
        "claims": claims,
        "sources": sources,
        "handoff": {
            "events": len(events),
            "facts": len(claims),
            "report_evidence": len(sources),
            "inferred_facts": inferred_facts,
            "facts_without_evidence": facts_without_evidence,
            "skipped_meta": skipped_meta,
        },
    }
