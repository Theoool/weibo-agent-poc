"""结构化输出模型。先落 JSON，再渲染 Markdown，避免"Agent 直接写长文"。"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

Heat = Literal["high", "medium", "low", "unknown"]
EvidenceKind = Literal["weibo_post", "hot_board", "official_site", "media_report", "other"]
FactSourceKind = Literal["weibo_post", "official_site", "media_report", "hot_board", "unknown"]


class Evidence(BaseModel):
    """一条证据。kind 必须如实标注，不要把转载当成原始来源。"""

    kind: EvidenceKind = "weibo_post"
    # 微博帖子的 mid。必须落盘，否则"事实"无法回溯到"支撑它的帖子"
    mid: str = ""
    author: str = ""
    time: str = ""
    content: str = ""
    url: str = ""
    # 帖子是否已通过详情页复核（作者/时间/正文一致）
    verified: bool = False
    note: str = ""


class Fact(BaseModel):
    """一条"事实"。

    关键改动：事实不再是一句裸字符串，而要说明它由哪几条帖子支撑。
    没有这个字段，"调查 → 核查"就只能靠人工读报告转写。
    """

    text: str
    # claim = 关于事件的断言（可外部核查）；meta = 关于本次样本的统计（不可外部核查）
    kind: Literal["claim", "meta"] = "claim"
    supporting_mids: list[str] = Field(default_factory=list)
    source_kind: FactSourceKind = "unknown"
    # 支撑关系由代码推断而非模型标注时为 True。推断出来的关联只能当线索，
    # 不能当核查依据，所以要在报告里显式标出。
    inferred_link: bool = False

    def label(self) -> str:
        if self.kind == "meta":
            return self.text
        return self.text + ("　〔支撑自动关联〕" if self.inferred_link else "")


class SearchRecord(BaseModel):
    keyword: str
    url: str = ""
    posts_read: int = 0
    new_posts: int = 0
    round: int = 1
    limitation: str = ""


class Limitation(BaseModel):
    step: str
    detail: str


class Event(BaseModel):
    name: str
    summary: str = ""
    heat: Heat = "unknown"
    # 热度依据：位次、条目数等可核对的信息，避免"凭感觉给 high"
    heat_basis: str = ""
    facts: list[Fact] = Field(default_factory=list)
    opinions: list[str] = Field(default_factory=list)
    speculations: list[str] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    official_sources: list[Evidence] = Field(default_factory=list)
    judgement: str = ""
    keywords: list[str] = Field(default_factory=list)
    # 由哪些事件合并而来（合并留痕，便于追溯"同一事件没有被拆开"）
    merged_from: list[str] = Field(default_factory=list)

    @field_validator("facts", mode="before")
    @classmethod
    def _coerce_facts(cls, value):
        """兼容旧产物：facts 曾是 list[str]，现在允许 str / dict / Fact 混写。"""
        if not value:
            return []
        out = []
        for item in value:
            if isinstance(item, Fact):
                out.append(item)
            elif isinstance(item, dict):
                out.append(item)
            elif isinstance(item, str):
                if item.strip():
                    out.append({"text": item.strip(), "inferred_link": True})
        return out


class Metrics(BaseModel):
    keywords_searched: int = 0
    posts_read: int = 0
    posts_with_verified_link: int = 0
    posts_detail_verified: int = 0
    events_formed: int = 0
    evidence_items: int = 0
    speculations: int = 0
    # 模型调用：calls 是成功次数，failures 是**尝试**失败次数（含重试前的失败）
    llm_calls: int = 0
    llm_failures: int = 0
    llm_ms: int = 0


class Investigation(BaseModel):
    topic: str
    generated_at: datetime = Field(default_factory=datetime.now)
    mode: str = "heuristic"
    events: list[Event] = Field(default_factory=list)
    searches: list[SearchRecord] = Field(default_factory=list)
    limitations: list[Limitation] = Field(default_factory=list)
    metrics: Metrics = Field(default_factory=Metrics)

    def to_markdown(self) -> str:
        m = self.metrics
        lines: list[str] = [f"# {self.topic}调查（POC）", ""]
        lines += [
            f"- 生成时间：{self.generated_at:%Y-%m-%d %H:%M:%S}",
            f"- 运行模式：{self.mode}",
            (
                f"- 关键词 {m.keywords_searched} 组｜读取帖子 {m.posts_read} 条｜"
                f"详情页复核 {m.posts_detail_verified} 条｜事件 {m.events_formed} 个"
            ),
        ]
        if m.llm_calls or m.llm_failures:
            lines.append(
                f"- 模型调用：成功 {m.llm_calls} 次｜失败尝试 {m.llm_failures} 次"
                f"｜累计 {m.llm_ms / 1000:.1f} 秒"
            )
        lines += [""]

        if self.limitations:
            lines += ["## 访问限制记录", ""]
            lines += [f"- **{x.step}**：{x.detail}" for x in self.limitations]
            lines.append("")

        if any(f.inferred_link for ev in self.events for f in ev.facts):
            lines += [
                "> 「〔支撑自动关联〕」表示该条事实与支撑帖子的对应关系由代码推断（模型未逐条标注），"
                "只能作为核查线索，不能直接作为引用依据。",
                "",
            ]

        for idx, ev in enumerate(self.events, 1):
            lines += [f"## {idx}. {ev.name}", ""]
            if ev.merged_from:
                lines += [f"> 合并来源：{'、'.join(ev.merged_from)}", ""]
            lines += [
                f"- 热度判断：**{ev.heat}**" + (f"（{ev.heat_basis}）" if ev.heat_basis else ""),
                f"- 摘要：{ev.summary}",
                "",
                "### 事实",
            ]
            lines += [f"- {x.label()}" for x in ev.facts] or ["- （无）"]
            lines += ["", "### 主要观点"]
            lines += [f"- {x}" for x in ev.opinions] or ["- （无）"]
            lines += ["", "### 推测"]
            lines += [f"- {x}" for x in ev.speculations] or ["- （无）"]
            lines += ["", "### 证据"]
            if ev.evidence:
                for i, item in enumerate(ev.evidence, 1):
                    flag = "已复核" if item.verified else "未复核"
                    link = item.url or "无可验证直链"
                    lines.append(
                        f"{i}. 作者：{item.author or '未知'}｜时间：{item.time or '未知'}"
                        f"｜来源：{item.kind}｜{flag}｜链接：{link}"
                    )
                    if item.content:
                        lines.append(f"   - 内容：{item.content}")
                    if item.note:
                        lines.append(f"   - 备注：{item.note}")
            else:
                lines.append("- （无）")
            if ev.official_sources:
                lines += ["", "### 官方/权威信息"]
                for item in ev.official_sources:
                    lines.append(
                        f"- {item.author or '未知来源'}｜{item.time or '时间未知'}"
                        f"｜{item.content}｜{item.url or '无可验证直链'}"
                    )
            if ev.judgement:
                lines += ["", "### 判断", ev.judgement]
            lines.append("")

        lines += ["## 搜索记录", ""]
        for rec in self.searches:
            lines.append(
                f"- 第 {rec.round} 轮｜`{rec.keyword}`｜读取 {rec.posts_read} 条"
                f"（新增 {rec.new_posts}）｜{rec.limitation or 'ok'}"
            )
        return "\n".join(lines).strip() + "\n"
