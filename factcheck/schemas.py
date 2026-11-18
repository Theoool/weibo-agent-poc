"""事实核查的结构化模型。

设计要点：核查结论不允许"凭空写"。
每条结论必须挂到已登记来源上，来源还要通过两道机检：
1) 链接可达（HTTP 200）；
2) 引文命中（quote 真的出现在页面正文里）。
任一条不通过，结论就会被自动降级。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

Verdict = Literal["已证实", "部分证实", "无法证实", "存在矛盾", "待核查"]

# 引文与说法的关系。由模型标注，但只描述"这段文字说了什么"，不判断说法真假。
SupportLevel = Literal[
    "supports",      # 直接支持，引文里有说法所需的关键信息
    "partial",       # 只支持一部分，缺关键限定（时间/主体/数量）
    "unrelated",     # 只是同一主题，没有支持关系
    "contradicts",   # 与说法相反（数字/时间/主体对不上）
    "insufficient",  # 引文为空或只有标题
    "unknown",       # 尚未判定
]

# 一条说法的引文整体成色，由上面逐条标注聚合而来
CitationCheck = Literal[
    "not_run",      # 没跑语义判定
    "supported",    # 至少一条直接支持，且没有不相关/相反
    "partial",      # 支持与不支持混在一起
    "unrelated",    # 全部与说法无关
    "conflicting",  # 至少一条与说法相反
    "insufficient", # 全部信息不足
]

# 来源类型：决定它在证据链里的权重
SourceKind = Literal[
    "official_account",   # 当事人/公司官方账号
    "official_api",       # 平台官方接口（如 B 站粉丝数）
    "company_registry",   # 工商/社保等登记信息
    "media",              # 有采访或编辑流程的媒体报道
    "social_post",        # 微博/小红书等用户发言
    "aggregator",         # 聚合站、百科、AI 生成摘要
]


class Source(BaseModel):
    id: str
    kind: SourceKind
    publisher: str
    title: str = ""
    url: str
    published_at: str = ""
    # 用于机器校验的原文片段；为空则跳过引文校验
    quote: str = ""
    # 同一稿件被多家转载时填同一个组名，避免"转载当多信源"
    independence_group: str = ""
    note: str = ""

    # 运行时由校验器填写
    http_status: int = 0
    reachable: bool = False
    quote_found: bool | None = None
    page_title: str = ""
    check_note: str = ""


class Cite(BaseModel):
    source_id: str
    point: str = ""
    # 语义判定结果：这条引文到底支不支持这个说法。只影响证据成色，不直接产出结论。
    support: SupportLevel = "unknown"
    support_reason: str = ""


class ClaimCheck(BaseModel):
    id: str
    claim: str
    report_line: str = ""
    # 人工（或抽取器）给出的原始判定。机检不会覆盖它，只在 final_verdict 里生效。
    verdict: Verdict
    confidence: Literal["高", "中", "低"] = "中"
    analysis: str = ""
    supporting: list[Cite] = Field(default_factory=list)
    counter: list[Cite] = Field(default_factory=list)

    # 运行时填写
    # 机检之后的生效判定。verdict 与它不同即说明发生了降级，降级原因可查。
    final_verdict: Verdict | None = None
    verified_sources: list[str] = Field(default_factory=list)
    failed_sources: list[str] = Field(default_factory=list)
    # 通过校验的支持来源里，有多少个互不相同的稿源（转载只算一个）
    independent_groups: int = 0
    downgraded: bool = False

    # 语义层（模型标注 + 代码聚合）。注意：这部分只描述证据成色，
    # 结论仍由规则计算，且只有"变弱"这一个方向。
    citation_check: CitationCheck = "not_run"
    semantic_note: str = ""
    # 引文之间是否互相矛盾（同一事实给出不同数字/时间/主体）
    cross_conflict: bool = False
    cross_conflict_reason: str = ""
    # 因"来源互相矛盾"被改写为「存在矛盾」，与机检降级分开记
    semantic_revised: bool = False

    def effective(self) -> str:
        """对外展示和统计都用这个，避免"字段说 A、正文说 B"。"""
        return self.final_verdict or self.verdict


class FactCheckReport(BaseModel):
    title: str
    original_report: str = ""
    checked_at: datetime = Field(default_factory=datetime.now)
    mode: str = "analyst"
    claims: list[ClaimCheck] = Field(default_factory=list)
    sources: list[Source] = Field(default_factory=list)
    summary: dict = Field(default_factory=dict)
    # 由 handoff 自动生成时的抽取统计（事件数、事实数、无证据事实数）
    handoff: dict = Field(default_factory=dict)
    # 语义判定的执行情况（是否启用、模型、判定条数、失败原因）
    semantic: dict = Field(default_factory=dict)
