"""事实核查执行器：校验来源 → 汇总判定 → 渲染报告。

单独成模块而不是塞进 investigator，因为两者的核心纪律不同：
舆情调查要"发现"，事实核查要"证伪"。混在一起会让核查变成给原报告背书。
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit

from .schemas import ClaimCheck, FactCheckReport, Source
from .semantic import run_semantic
from .verify import check_source

KIND_LABEL = {
    "official_account": "当事人/公司官方账号",
    "official_api": "平台官方接口",
    "company_registry": "工商/社保登记信息",
    "media": "媒体报道（有采访或编辑流程）",
    "social_post": "社交平台用户发言",
    "aggregator": "聚合站/百科/AI 生成摘要",
}

SUPPORT_LABEL = {
    "supports": "支持",
    "partial": "部分支持",
    "unrelated": "不相关",
    "contradicts": "相反",
    "insufficient": "信息不足",
}


def load_claims(path: str | Path) -> FactCheckReport:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return FactCheckReport.model_validate(data)


def _group_of(source: Source) -> str:
    return source.independence_group or source.id


def check_all_sources(
    report: FactCheckReport, workers: int = 6, allow_browser: bool = True, log=print
) -> None:
    seen: set[str] = set()
    todo = []
    for src in report.sources:
        if src.id in seen:
            continue
        seen.add(src.id)
        todo.append(src)
    log(f"      校验 {len(todo)} 个来源（链接可达性 + 引文命中）……")
    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(check_source, todo))

    # 失败项用浏览器二次确认，避免把"我抓不到"记成"引文不存在"
    failed = [s for s in todo if not s.reachable or s.quote_found is False]
    if allow_browser and failed:
        log(
            f"      {len(failed)} 个来源 HTTP 校验未通过，改用浏览器复核："
            + "、".join(s.id for s in failed)
        )
        # 同一站点首次就失败（典型是限流 432 或要登录）时，其余同站来源不再逐个
        # 开浏览器——否则 15 条微博链接会白开 15 次 Chrome，还会加重限流。
        settled: dict[str, bool] = {}
        for src in failed:
            host = urlsplit(src.url).netloc
            if settled.get(host) is False:
                src.check_note += "（同站点首次即未通过，未重复尝试浏览器）"
                continue
            check_source(src, allow_browser=True)
            settled[host] = src.reachable

    ok = sum(1 for s in todo if s.reachable)
    hit = sum(1 for s in todo if s.quote_found)
    log(f"      可达 {ok}/{len(todo)}；引文命中 {hit}/{len(todo)}")


def _apply_semantic(claim: ClaimCheck) -> None:
    """把"引文是否支持说法"落到结论上。

    纪律：模型只能让结论变弱，不能变强。所以这里没有"升级"分支——
    「已证实」必须由独立信源数量支撑，不能因为模型说"看起来很支持"就给。
    """
    kind = claim.citation_check
    if kind == "not_run":
        return
    if claim.verdict == "待核查":
        # 待核查本来就没下结论，语义判定只作为展示信息，不改判定
        return
    if kind in ("unrelated", "conflicting"):
        claim.downgraded = True
        claim.final_verdict = "无法证实"
        claim.confidence = "低"
        reason = (
            "登记的支持来源与说法不对应，引文只是在讲同一主题"
            if kind == "unrelated"
            else "所引来源的原文与说法相反"
        )
        claim.analysis += f"（语义降级：{reason}，详见「引文与说法的关系」）"
        return
    if claim.cross_conflict and claim.independent_groups >= 2:
        # 两个以上互不相同的来源在同一事实上对不上，正是原报告「存在矛盾」的含义
        claim.final_verdict = "存在矛盾"
        claim.semantic_revised = True
        claim.analysis += "（语义判定：两条以上独立来源在同一事实上互相矛盾，详见「来源间矛盾」）"


def apply_checks(report: FactCheckReport, checked: bool = True) -> None:
    """把来源校验结果灌进结论：支持来源全挂 → 结论自动降级为无法证实。"""
    by_id = {s.id: s for s in report.sources}
    for claim in report.claims:
        ok_groups: set[str] = set()
        verified: list[str] = []
        failed: list[str] = []
        for cite in claim.supporting:
            src = by_id.get(cite.source_id)
            if src is None:
                failed.append(f"{cite.source_id}（未登记）")
                continue
            if not checked:
                # 没跑机检就不能判"未通过"，否则会把所有结论误降到"无法证实"
                continue
            if src.reachable and src.quote_found is not False:
                verified.append(src.id)
                ok_groups.add(_group_of(src))
            else:
                failed.append(f"{src.id}（{src.check_note or '未记录原因'}）")
        claim.verified_sources = verified
        claim.failed_sources = failed
        claim.independent_groups = len(ok_groups)

        # 待核查状态本来就没有结论，不存在"结论不成立"，不能降级
        claim.final_verdict = claim.verdict
        if claim.verdict == "待核查":
            continue

        if checked and claim.supporting and not verified:
            claim.downgraded = True
            claim.final_verdict = "无法证实"
            claim.confidence = "低"
            claim.analysis += (
                f"（机检降级：登记的支持来源全部未通过链接/引文校验，"
                f"原判定「{claim.verdict}」不成立）"
            )

        # 机检优先，语义判定在此基础上再收紧
        _apply_semantic(claim)

    # 跳过机检时不能声称"已机检"——报告里写做过的事必须真做过
    if checked:
        report.mode = (
            "机检 + 自动抽取（待人工复核）" if report.handoff else "机检 + 人工判定"
        )
    else:
        report.mode = "人工判定（未机检）"


def build_summary(report: FactCheckReport, notes: dict) -> dict:
    claims = report.claims
    independent = [
        c
        for c in claims
        if c.effective() in ("已证实", "部分证实") and c.independent_groups >= 2
    ]
    secondhand = [
        c
        for c in claims
        if c.independent_groups <= 1 and c.effective() not in ("无法证实", "待核查")
    ]
    unverifiable = [c for c in claims if c.effective() == "无法证实"]
    contradictory = [c for c in claims if c.effective() == "存在矛盾"]
    pending = [c for c in claims if c.effective() == "待核查"]
    return {
        "total": len(claims),
        "independently_verified": len(independent),
        "independently_verified_ids": [c.id for c in independent],
        "secondhand_only": len(secondhand),
        "secondhand_only_ids": [c.id for c in secondhand],
        "unverifiable": len(unverifiable),
        "unverifiable_ids": [c.id for c in unverifiable],
        "contradictory": len(contradictory),
        "contradictory_ids": [c.id for c in contradictory],
        "pending": len(pending),
        "pending_ids": [c.id for c in pending],
        "downgraded": len([c for c in claims if c.downgraded]),
        "downgraded_ids": [c.id for c in claims if c.downgraded],
        "sources_checked": len(report.sources),
        "sources_reachable": sum(1 for s in report.sources if s.reachable),
        "sources_quote_hit": sum(1 for s in report.sources if s.quote_found),
        # 语义层：引文与说法的对应关系，和"结论"是两件事，分开统计
        "semantic_checked": len([c for c in claims if c.citation_check != "not_run"]),
        "citation_supported": len(
            [c for c in claims if c.citation_check in ("supported", "partial")]
        ),
        "citation_unrelated": len([c for c in claims if c.citation_check == "unrelated"]),
        "citation_conflicting": len([c for c in claims if c.citation_check == "conflicting"]),
        "citation_insufficient": len(
            [c for c in claims if c.citation_check == "insufficient"]
        ),
        "semantic_revised": len([c for c in claims if c.semantic_revised]),
        **notes,
    }


def to_markdown(report: FactCheckReport) -> str:
    by_id = {s.id: s for s in report.sources}
    s = report.summary
    lines = [f"# {report.title}", ""]
    lines += [
        f"- 核查时间：{report.checked_at:%Y-%m-%d %H:%M:%S}",
        f"- 核查方式：{report.mode}"
        + ("" if "未机检" in report.mode else "（每个来源均已机检链接可达性与引文命中）"),
        f"- 被核查对象：{report.original_report or '（未注明）'}",
        (
            f"- 来源校验：登记 {s.get('sources_checked', 0)} 个｜可达 {s.get('sources_reachable', 0)} 个"
            f"｜引文命中 {s.get('sources_quote_hit', 0)} 个"
            if "未机检" not in report.mode
            else f"- 来源校验：登记 {s.get('sources_checked', 0)} 个（本次未机检）"
        ),
    ]
    if s.get("semantic_checked"):
        lines.append(
            f"- 语义判定：{s['semantic_checked']} 条说法已判定引文与说法的关系"
            f"（支持/部分支持 {s.get('citation_supported', 0)}｜不相关 {s.get('citation_unrelated', 0)}"
            f"｜相反 {s.get('citation_conflicting', 0)}｜信息不足 {s.get('citation_insufficient', 0)}）"
        )
    else:
        sem = report.semantic or {}
        reason = sem.get("reason") or "未执行"
        lines.append(f"- 语义判定：未执行（{reason}）——引文与说法是否对应只能人工看")
    lines += [
        "",
        "> 核查纪律：微博用户转述不作为事实依据；多家转载同一稿件只计一个信源；"
        "找不到原始证据一律标“无法证实”，不做善意推定。",
        "",
        "> 语义判定的边界：它只回答“这段引文有没有支持这句话”，不判断说法真假，"
        "也不会把结论升到「已证实」；它只能让结论变弱，判定权仍在证据规则手里。",
        "",
    ]
    if report.handoff:
        h = report.handoff
        lines += [
            f"**自动抽取**：来自调查产物 {h.get('events', 0)} 个事件、"
            f"{h.get('facts', 0)} 条事实；其中 {h.get('inferred_facts', 0)} 条为事件级自动关联"
            f"（非逐条标注），{h.get('facts_without_evidence', 0)} 条无任何支撑证据。"
            "这些来源是调查报告自己的说法，核查需要补充报告之外的独立信源。",
            "",
        ]

    blocked = [s_ for s_ in report.sources if not s_.reachable]
    if blocked:
        lines += [
            "**本次未能打开的来源**（结论已按“不可访问”处理，未凭摘要下判断）：",
            *[
                f"- {b.id}　{b.publisher}　{b.check_note or f'HTTP {b.http_status}'}"
                for b in blocked
            ],
            "",
        ]

    for claim in report.claims:
        lines += [f"## {claim.id}　{claim.claim}", ""]
        lines += [f"**待核查说法**：{claim.claim}", ""]
        if claim.report_line:
            lines += [f"> 原报告表述：{claim.report_line}", ""]
        flag = f"（机检降级，原判定「{claim.verdict}」）" if claim.downgraded else ""
        lines += [f"**核查结论**：{claim.effective()}{flag}", ""]

        cited = []
        for cite in claim.supporting + claim.counter:
            if cite.source_id in by_id and cite.source_id not in {x.id for x in cited}:
                cited.append(by_id[cite.source_id])
        lines += ["**原始来源**："]
        if cited:
            for src in cited:
                mark = "✓" if src.reachable else "✗"
                quote_mark = ""
                if src.quote_found is True:
                    quote_mark = "｜引文命中"
                elif src.quote_found is False:
                    quote_mark = "｜引文未命中"
                lines.append(
                    f"- [{src.id}] {src.publisher}《{src.title or '（无标题）'}》"
                    f"　HTTP {src.http_status} {mark}{quote_mark}　{src.url}"
                )
        else:
            lines.append("- （无可登记来源）")
        lines.append("")

        lines += ["**来源类型**："]
        for src in cited:
            lines.append(f"- [{src.id}] {KIND_LABEL.get(src.kind, src.kind)}")
        lines.append("")

        lines += ["**发布时间**："]
        for src in cited:
            lines.append(f"- [{src.id}] {src.published_at or '未标注'}")
        lines.append("")

        lines += ["**支持证据**："]
        if claim.supporting:
            for cite in claim.supporting:
                src = by_id.get(cite.source_id)
                if src is None:
                    lines.append(f"- {cite.source_id}（未登记）")
                    continue
                status = "已校验" if src.reachable and src.quote_found is not False else "校验未通过"
                lines.append(f"- [{src.id}]（{status}）{cite.point or src.title}")
                if src.quote:
                    lines.append(f"  - 原文：{src.quote}")
        else:
            lines.append("- （无）")
        lines.append("")

        marked = [c for c in claim.supporting if c.support != "unknown"]
        if marked:
            lines += ["**引文与说法的关系**（语义判定：只说明引文有没有支持这句话，不参与核查结论）："]
            for cite in marked:
                label = SUPPORT_LABEL.get(cite.support, cite.support)
                lines.append(f"- [{cite.source_id}] {label}：{cite.support_reason or '（未给依据）'}")
            if claim.semantic_note:
                lines.append(f"- 总评：{claim.semantic_note}")
            lines.append("")

        if claim.cross_conflict:
            lines += [
                "**来源间矛盾**：",
                claim.cross_conflict_reason or "（判定存在矛盾，但未说明是哪两条、在哪个事实上）",
                "",
            ]

        lines += ["**反向证据**："]
        if claim.counter:
            for cite in claim.counter:
                src = by_id.get(cite.source_id)
                if src is None:
                    lines.append(f"- {cite.source_id}（未登记）")
                    continue
                status = "已校验" if src.reachable and src.quote_found is not False else "校验未通过"
                lines.append(f"- [{src.id}]（{status}）{cite.point or src.title}")
                if src.quote:
                    lines.append(f"  - 原文：{src.quote}")
        else:
            lines.append("- （未发现反向证据；注意“未发现”不等于“不存在”）")
        lines.append("")

        lines += ["**核查说明**：", claim.analysis, ""]
        lines += [f"**可信度判断**：{claim.confidence}", ""]
        if claim.failed_sources:
            lines += ["**校验未通过项**："] + [f"- {x}" for x in claim.failed_sources] + [""]

    lines += ["## 最终总结", ""]
    lines += [
        f"1. **原报告中有多少条事实得到独立验证？**　{s.get('independently_verified', 0)} 条"
        f"（{('、'.join(s.get('independently_verified_ids', [])) or '无')}）。"
        "判定标准：至少两个不属于同一稿源的来源同时支持。",
        f"2. **有多少条只是二手转述？**　{s.get('secondhand_only', 0)} 条"
        f"（{('、'.join(s.get('secondhand_only_ids', [])) or '无')}）。"
        "这些说法只有单一信源，或全部来自同一条微博/同一篇通稿的转载"
        + ("；其中 "
           + "、".join(s.get("contradictory_ids", []))
           + " 同时计入“存在矛盾”，因为它不仅信源单一，还与可查数据冲突"
           if s.get("contradictory_ids") else "")
        + "。",
        f"3. **有多少条目前无法验证？**　{s.get('unverifiable', 0)} 条"
        f"（{('、'.join(s.get('unverifiable_ids', [])) or '无')}）。另有存在矛盾 "
        f"{s.get('contradictory', 0)} 条（{('、'.join(s.get('contradictory_ids', [])) or '无')}）。"
        + (
            f"待核查 {s.get('pending', 0)} 条"
            f"（{('、'.join(s.get('pending_ids', [])) or '无')}）——"
            "这些是由调查产物自动抽取、尚未独立核查的说法，不计入上述两类。"
            if s.get("pending")
            else ""
        ),
        "",
        f"4. **原报告是否存在把观点写成事实的问题？**　{s.get('opinion_as_fact', '')}",
        "",
        f"5. **哪些结论应该修改？**　{s.get('should_revise', '')}",
        "",
        "---",
        "",
        f"来源校验总表：{s.get('sources_checked', 0)} 条登记｜可达 {s.get('sources_reachable', 0)}"
        f"｜引文命中 {s.get('sources_quote_hit', 0)}。未通过的条目已在各条“校验未通过项”中逐条列出。",
    ]
    return "\n".join(lines).strip() + "\n"


def run_factcheck(
    input_path: str | Path,
    out_dir: str | Path,
    do_check: bool = True,
    log=print,
    llm=None,
    semantic: bool | None = None,
    semantic_limit: int = 20,
) -> tuple[FactCheckReport, Path, Path]:
    report = load_claims(input_path)
    log(f"[1/4] 载入 {len(report.claims)} 项待核查说法、{len(report.sources)} 个登记来源")
    if do_check:
        log("[2/4] 校验来源（链接 + 引文）")
        check_all_sources(report, log=log)
    else:
        report.mode = "人工判定（跳过机检）"

    # 语义判定：默认"有模型就跑"，显式 --no-semantic 才关。默认开是有意的——
    # 这一层正是用来发现"引文与说法无关"这种机检看不见的错误。
    if semantic is None:
        semantic = llm is not None and bool(getattr(llm, "available", False))
    if not semantic:
        report.semantic = {"enabled": False, "reason": "未启用"}
        log("[3/4] 跳过语义判定：引文与说法是否对应只能人工判断")
    elif llm is None or not getattr(llm, "available", False):
        report.semantic = {"enabled": False, "reason": "模型不可用"}
        log("[3/4] 跳过语义判定：需要可用的模型（检查 OPENAI_API_KEY / 余额）")
    else:
        log("[3/4] 语义判定：引文是否支持说法")
        report.semantic = run_semantic(report, llm, limit=semantic_limit, log=log)
        if report.semantic.get("reason"):
            log(f"      注意：{report.semantic['reason']}")

    apply_checks(report, checked=do_check)

    notes = {"semantic": report.semantic}
    raw = json.loads(Path(input_path).read_text(encoding="utf-8"))
    notes["opinion_as_fact"] = raw.get("opinion_as_fact", "")
    notes["should_revise"] = raw.get("should_revise", "")
    report.summary = build_summary(report, notes)

    log("[4/4] 生成报告")
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stem = Path(input_path).stem
    json_path = out / f"{stem}_factcheck.json"
    md_path = out / f"{stem}_factcheck.md"
    json_path.write_text(
        json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    md_path.write_text(to_markdown(report), encoding="utf-8")
    return report, json_path, md_path
