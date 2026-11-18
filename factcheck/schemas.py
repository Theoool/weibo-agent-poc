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
