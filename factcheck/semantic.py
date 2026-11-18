"""引文-说法对应性判定（核查阶段的受限语义层）。

为什么需要这一层：
前两道机检（链接可达、引文命中）只能证明"这句话确实出现在那一页"，
证明不了"这句话支持这个说法"。实测里就有一条：报告写"发了 PS5 Pro"，
引用的帖子通篇在讲放假安排——链接可达、引文也命中，但两者没关系。
这种错误人工读一遍能看出来，机检看不出来。

边界（必须守住，否则核查就变成了给原报告背书）：
1. 模型只标注"引文 ↔ 说法"的对应关系，**不产出核查结论**；
2. 结论由 runner.apply_checks 按标注聚合计算，且只有变弱的方向，永不升级；
3. 判断只允许使用给定引文，禁止引入模型自身知识——否则等于拿模型记忆当证据；
4. 引文为空或只有标题时，必须选 insufficient，不许猜。

依赖约定：llm 只需要提供 chat_json(system, user, purpose=...) 与 model/last_error，
不直接 import agent 包，保持核查层可以脱离调查层单独跑。
"""

from __future__ import annotations

import re

from .schemas import ClaimCheck, Cite, Source

SYSTEM_PROMPT = """你在做「引文-说法对应性」判定，不是事实核查。

你只回答一个问题：给出的这段原文，是否支持这条说法？
你不判断这条说法在现实中是真是假——那不是这一步的任务，也超出了你手上材料能回答的范围。

硬性约束：
1. 只能依据给出的引文和标题判断，不得使用你自己的知识补充任何事实。
2. 引文为空、或只有标题没有正文时，一律选 insufficient，不要靠标题推测。
3. source_id 只能填输入里出现过的 id，不得新增、不得改写。
4. reason 用一句话说明依据，不超过 40 字，要点出具体差异（例如"引文只提到放假，未提及主机"）。
5. 同一主体、同一事实给出不同数字/时间/地点时，才算矛盾；措辞不同不算。
6. 同一来源的两条引文之间不算矛盾。
"""

_SCHEMA_HINT = """{
  "citations": [
    {
      "source_id": "只能填输入里出现过的 id",
      "support": "supports | partial | unrelated | contradicts | insufficient",
      "reason": "一句话依据，不超过 40 字"
    }
  ],
  "cross_conflict": false,
  "cross_conflict_reason": "若 cross_conflict 为 true，写清是哪两条在哪个事实上冲突；否则留空字符串",
  "note": "一句话总评：这批引文整体能不能撑住这个说法"
}"""

_LEVELS = {"supports", "partial", "unrelated", "contradicts", "insufficient"}


def _clean(text: str, limit: int) -> str:
    return re.sub(r"\s+", " ", text or "").strip()[:limit]


def build_prompt(claim: ClaimCheck, pairs: list[tuple[Cite, Source]]) -> str:
    """把一条说法和它的引文压成模型能判断的最小输入。

    只给引文，不给 URL、不给抓取时间：避免模型去"推测"页面内容。
    """
    rows: list[str] = []
    for _, src in pairs:
        rows.append(
            f"[{src.id}] 发布方：{_clean(src.publisher, 40) or '未知'}"
            f"｜类型：{src.kind}\n"
            f"    标题：{_clean(src.title, 80) or '（无标题）'}\n"
            f"    引文：{_clean(src.quote, 300) or '（无引文）'}"
        )
    block = "\n".join(rows)
    line = _clean(claim.report_line, 120)
    return f"""待判断的说法：
{claim.claim}
{"（出自：" + line + "）" if line else ""}

报告为这条说法登记了 {len(pairs)} 条来源，逐条判断它们与说法的关系：

{block}

对每条引文选择一个 support 等级：
- supports：引文里有说法所需的关键信息，可以直接对上
- partial：只对上一部分，或缺关键限定（时间、主体、数量）
- unrelated：引文只是在讲同一主题，与这条说法没有支持关系
- contradicts：引文与说法相反（数字不同、时间不同、主体不同）
- insufficient：引文为空或只有标题，无法判断

再判断这些引文之间是否互相矛盾（cross_conflict）。

严格按此结构输出 JSON，不要输出 Markdown：
{_SCHEMA_HINT}
"""


def _pairs_of(claim: ClaimCheck, by_id: dict[str, Source]) -> list[tuple[Cite, Source]]:
    out: list[tuple[Cite, Source]] = []
    for cite in claim.supporting:
        src = by_id.get(cite.source_id)
        if src is not None:
            out.append((cite, src))
    return out


def apply_result(
    claim: ClaimCheck, payload: dict | None, by_id: dict[str, Source]
) -> int:
    """把模型标注写回 Cite。返回实际采纳的条数。

    模型编出不存在的 source_id、或给出非法等级时一律丢弃——和调查层对 mid
    的白名单校验同一个道理：宁可少标，不能把编造的来源写进报告。
    """
    if not isinstance(payload, dict):
        return 0
    raw = payload.get("citations")
    if not isinstance(raw, list):
        return 0

    index = {c.source_id: c for c in claim.supporting}
    applied = 0
    for item in raw:
        if not isinstance(item, dict):
            continue
        sid = str(item.get("source_id") or "").strip()
        cite = index.get(sid)
        if cite is None or sid not in by_id:
            continue
        level = str(item.get("support") or "").strip().lower()
        if level not in _LEVELS:
            continue
        cite.support = level  # type: ignore[assignment]
        cite.support_reason = _clean(str(item.get("reason") or ""), 80)
        applied += 1

    claim.cross_conflict = bool(payload.get("cross_conflict"))
    claim.cross_conflict_reason = _clean(str(payload.get("cross_conflict_reason") or ""), 200)
    claim.semantic_note = _clean(str(payload.get("note") or ""), 200)
    return applied


def aggregate(claim: ClaimCheck) -> str:
    """把逐条标注聚合成"这条说法的引文成色"。

    规则写得直白，因为结论直接依赖它：
    - 有一条相反 → conflicting（不管其他几条多支持）
    - 全部无关 → unrelated
    - 全部信息不足 → insufficient
    - 有支持且无无关/相反 → supported；其余混杂 → partial
    """
    levels = [c.support for c in claim.supporting if c.support != "unknown"]
    if not levels:
        return "not_run"
    if "contradicts" in levels:
        return "conflicting"
    if all(x == "unrelated" for x in levels):
        return "unrelated"
    if all(x == "insufficient" for x in levels):
        return "insufficient"
    if all(x in ("supports", "partial") for x in levels):
        return "supported" if "supports" in levels else "partial"
    return "partial"


_FATAL_HINTS = ("HTTP 401", "HTTP 402", "HTTP 403", "HTTP 404")


def run_semantic(report, llm, limit: int = 20, log=print) -> dict:
    """对报告里的说法逐条跑引文判定。失败可降级，不影响机检结论。"""
    by_id = {s.id: s for s in report.sources}
    targets = [c for c in report.claims if c.supporting][:limit]
    info = {
        "enabled": True,
        "model": getattr(llm, "model", ""),
        "claimed_total": len(report.claims),
        "attempted": 0,
        "judged": 0,
        "cites_marked": 0,
        "failed": 0,
        "skipped": len(report.claims) - len(targets),
        "reason": "",
    }

    log(f"      语义判定 {len(targets)} 条说法（模型只标引文与说法的关系，不下结论）……")
    for claim in targets:
        pairs = _pairs_of(claim, by_id)
        if not pairs:
            continue
        info["attempted"] += 1
        payload = llm.chat_json(
            SYSTEM_PROMPT, build_prompt(claim, pairs), purpose="claim_citation_check"
        )
        if payload is None:
            info["failed"] += 1
            err = (getattr(llm, "last_error", "") or "未知原因")[:120]
            log(f"      {claim.id} 判定失败：{err}")
            # 鉴权/余额类错误不会自己好，继续跑只会把日志刷满
            if any(h in err for h in _FATAL_HINTS) or info["failed"] >= 3:
                info["reason"] = err
                break
            continue

        applied = apply_result(claim, payload, by_id)
        claim.citation_check = aggregate(claim)  # type: ignore[assignment]
        info["judged"] += 1
        info["cites_marked"] += applied
        log(f"      {claim.id} → {claim.citation_check}（采纳 {applied} 条标注）")

    if info["failed"] and not info["reason"]:
        info["reason"] = f"{info['failed']} 条判定失败，其余照常"
    return info
