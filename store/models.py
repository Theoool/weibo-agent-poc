"""ORM 映射。与 `migrations/*.sql` 一一对应。

约定：**DDL 是权威**，这里是映射层。改 schema 一律先写迁移文件再改这里，
`tests/test_store.py` 里有一条用例逐表比对列名，防止两边漂移。

字段一律用 TEXT 存时间（ISO 字符串），与现有产物 JSON 的表示一致，
避免 SQLite 的 datetime 在读写过程中被悄悄转成不带时区的本地时间。
"""

from __future__ import annotations

from sqlalchemy import Boolean, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


# --------------------------------------------------------------------------- #
# 编排层
# --------------------------------------------------------------------------- #
class WorkflowRun(Base):
    __tablename__ = "workflow_run"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    topic: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="running")
    # cli | api | import
    trigger: Mapped[str] = mapped_column(String, nullable=False, default="cli")
    # full | degraded（历史产物导入，没有全文快照）
    quality: Mapped[str] = mapped_column(String, nullable=False, default="full")
    # llm | heuristic（降级后的运行模式，报告头要显示）
    mode: Mapped[str] = mapped_column(String, nullable=False, default="")
    config_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    app_version: Mapped[str] = mapped_column(String, nullable=False, default="")
    stop_reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # 报告自身的生成时刻（不是 run 的开始/结束时间），报告头显示的就是它
    report_at: Mapped[str | None] = mapped_column(String, nullable=True)
    parent_run_id: Mapped[str | None] = mapped_column(String, nullable=True)
    started_at: Mapped[str] = mapped_column(String, nullable=False)
    finished_at: Mapped[str | None] = mapped_column(String, nullable=True)
    error: Mapped[str] = mapped_column(Text, nullable=False, default="")
    partial: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class StageRun(Base):
    __tablename__ = "stage_run"
    __table_args__ = (UniqueConstraint("run_id", "stage", "attempt"),)

    id: Mapped[str] = mapped_column(String, primary_key=True)
    run_id: Mapped[str] = mapped_column(
        String, ForeignKey("workflow_run.id", ondelete="CASCADE"), nullable=False
    )
    # collect | investigate | handoff | factcheck
    stage: Mapped[str] = mapped_column(String, nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # queued | running | succeeded | failed | skipped
    status: Mapped[str] = mapped_column(String, nullable=False, default="running")
    input_stage_id: Mapped[str | None] = mapped_column(String, nullable=True)
    started_at: Mapped[str] = mapped_column(String, nullable=False)
    finished_at: Mapped[str | None] = mapped_column(String, nullable=True)
    error: Mapped[str] = mapped_column(Text, nullable=False, default="")


class Artifact(Base):
    __tablename__ = "artifact"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    run_id: Mapped[str] = mapped_column(
        String, ForeignKey("workflow_run.id", ondelete="CASCADE"), nullable=False
    )
    stage_run_id: Mapped[str | None] = mapped_column(String, nullable=True)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    content_ref: Mapped[str] = mapped_column(String, nullable=False, default="")
    sha256: Mapped[str] = mapped_column(String, nullable=False, default="")
    bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[str] = mapped_column(String, nullable=False)


# --------------------------------------------------------------------------- #
# 证据层
# --------------------------------------------------------------------------- #
class SourceDocument(Base):
    """一条内容快照。按 (url_canonical, content_hash) 去重，正文永不 UPDATE。"""

    __tablename__ = "source_document"
    __table_args__ = (UniqueConstraint("url_canonical", "content_hash"),)

    id: Mapped[str] = mapped_column(String, primary_key=True)
    url: Mapped[str] = mapped_column(String, nullable=False, default="")
    url_canonical: Mapped[str] = mapped_column(String, nullable=False)
    mid: Mapped[str] = mapped_column(String, nullable=False, default="")
    host: Mapped[str] = mapped_column(String, nullable=False, default="")
    kind: Mapped[str] = mapped_column(String, nullable=False, default="weibo_post")
    author: Mapped[str] = mapped_column(String, nullable=False, default="")
    published_at: Mapped[str] = mapped_column(String, nullable=False, default="")
    content_hash: Mapped[str] = mapped_column(String, nullable=False)
    bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    text_inline: Mapped[str | None] = mapped_column(Text, nullable=True)
    text_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    is_official: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    detail_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    first_seen_at: Mapped[str] = mapped_column(String, nullable=False)


class DocumentFetch(Base):
    """一次抓取事件。同一 URL 内容变了就会出现第二行 source_document。"""

    __tablename__ = "document_fetch"
    __table_args__ = (UniqueConstraint("run_id", "document_id"),)

    id: Mapped[str] = mapped_column(String, primary_key=True)
    run_id: Mapped[str] = mapped_column(
        String, ForeignKey("workflow_run.id", ondelete="CASCADE"), nullable=False
    )
    stage_run_id: Mapped[str | None] = mapped_column(String, nullable=True)
    document_id: Mapped[str] = mapped_column(
        String, ForeignKey("source_document.id"), nullable=False
    )
    fetched_at: Mapped[str] = mapped_column(String, nullable=False)
    http_status: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    source_url: Mapped[str] = mapped_column(String, nullable=False, default="")


# --------------------------------------------------------------------------- #
# 语义层
# --------------------------------------------------------------------------- #
class Event(Base):
    __tablename__ = "event"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    stage_run_id: Mapped[str] = mapped_column(
        String, ForeignKey("stage_run.id", ondelete="CASCADE"), nullable=False
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 归一化事件名。事件改名后仍能认出"还是同一个事件"
    event_key: Mapped[str] = mapped_column(String, nullable=False, default="")
    name: Mapped[str] = mapped_column(String, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    heat: Mapped[str] = mapped_column(String, nullable=False, default="unknown")
    heat_basis: Mapped[str] = mapped_column(Text, nullable=False, default="")
    judgement: Mapped[str] = mapped_column(Text, nullable=False, default="")
    keywords_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    merged_from_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")


class EventEvidence(Base):
    __tablename__ = "event_evidence"

    event_id: Mapped[str] = mapped_column(
        String, ForeignKey("event.id", ondelete="CASCADE"), primary_key=True
    )
    document_id: Mapped[str] = mapped_column(
        String, ForeignKey("source_document.id"), primary_key=True
    )
    # evidence | official
    role: Mapped[str] = mapped_column(String, primary_key=True, default="evidence")
    # 事件内的显示/复核顺序，报告编号和"复核前 N 条"都依赖它
    seq: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # 报告里实际引用的那一段（全文在 source_document 里）
    content_excerpt: Mapped[str] = mapped_column(Text, nullable=False, default="")


class Fact(Base):
    __tablename__ = "fact"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    event_id: Mapped[str] = mapped_column(
        String, ForeignKey("event.id", ondelete="CASCADE"), nullable=False
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    # claim | meta
    kind: Mapped[str] = mapped_column(String, nullable=False, default="claim")
    source_kind: Mapped[str] = mapped_column(String, nullable=False, default="unknown")
    inferred_link: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class FactSupport(Base):
    __tablename__ = "fact_support"

    fact_id: Mapped[str] = mapped_column(
        String, ForeignKey("fact.id", ondelete="CASCADE"), primary_key=True
    )
    document_id: Mapped[str] = mapped_column(
        String, ForeignKey("source_document.id"), primary_key=True
    )
    # annotated=模型逐条标注；inferred=代码按事件级推断
    link_kind: Mapped[str] = mapped_column(String, primary_key=True, default="annotated")


class EventStatement(Base):
    """观点与推测。两者结构相同、渲染位置不同，用 kind 区分。"""

    __tablename__ = "event_statement"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    event_id: Mapped[str] = mapped_column(
        String, ForeignKey("event.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String, nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    text: Mapped[str] = mapped_column(Text, nullable=False)


class GapRecord(Base):
    __tablename__ = "gap_record"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    stage_run_id: Mapped[str] = mapped_column(
        String, ForeignKey("stage_run.id", ondelete="CASCADE"), nullable=False
    )
    round: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    kind: Mapped[str] = mapped_column(String, nullable=False, default="")
    target: Mapped[str] = mapped_column(String, nullable=False, default="")
    detail: Mapped[str] = mapped_column(Text, nullable=False, default="")
    action: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # True 已补齐 / False 仍缺 / None 无法判定（NULL 有意义，不能默认成 False）
    resolved: Mapped[bool | None] = mapped_column(Boolean, nullable=True)


class Limitation(Base):
    __tablename__ = "limitation"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    stage_run_id: Mapped[str] = mapped_column(
        String, ForeignKey("stage_run.id", ondelete="CASCADE"), nullable=False
    )
    step: Mapped[str] = mapped_column(String, nullable=False)
    detail: Mapped[str] = mapped_column(Text, nullable=False)
    hit_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class SearchRecord(Base):
    __tablename__ = "search_record"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    stage_run_id: Mapped[str] = mapped_column(
        String, ForeignKey("stage_run.id", ondelete="CASCADE"), nullable=False
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    round: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    keyword: Mapped[str] = mapped_column(String, nullable=False)
    url: Mapped[str] = mapped_column(String, nullable=False, default="")
    posts_read: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    new_posts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    limitation: Mapped[str] = mapped_column(Text, nullable=False, default="")


# --------------------------------------------------------------------------- #
# 核查层
# --------------------------------------------------------------------------- #
class ClaimCheck(Base):
    __tablename__ = "claim_check"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    stage_run_id: Mapped[str] = mapped_column(
        String, ForeignKey("stage_run.id", ondelete="CASCADE"), nullable=False
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    claim_key: Mapped[str] = mapped_column(String, nullable=False, default="")
    claim: Mapped[str] = mapped_column(Text, nullable=False)
    report_line: Mapped[str] = mapped_column(Text, nullable=False, default="")
    verdict: Mapped[str] = mapped_column(String, nullable=False, default="待核查")
    final_verdict: Mapped[str | None] = mapped_column(String, nullable=True)
    confidence: Mapped[str] = mapped_column(String, nullable=False, default="中")
    analysis: Mapped[str] = mapped_column(Text, nullable=False, default="")
    citation_check: Mapped[str] = mapped_column(String, nullable=False, default="not_run")
    semantic_note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    cross_conflict: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    cross_conflict_reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    independent_groups: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    downgraded: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    semantic_revised: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    handoff_fact_id: Mapped[str | None] = mapped_column(String, nullable=True)


class FactcheckSource(Base):
    __tablename__ = "factcheck_source"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    stage_run_id: Mapped[str] = mapped_column(
        String, ForeignKey("stage_run.id", ondelete="CASCADE"), nullable=False
    )
    # 原来的 S1 / S2 序号，保留是为了让报告里的引用还能对上
    source_key: Mapped[str] = mapped_column(String, nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    publisher: Mapped[str] = mapped_column(String, nullable=False, default="")
    title: Mapped[str] = mapped_column(Text, nullable=False, default="")
    url: Mapped[str] = mapped_column(String, nullable=False, default="")
    published_at: Mapped[str] = mapped_column(String, nullable=False, default="")
    quote: Mapped[str] = mapped_column(Text, nullable=False, default="")
    independence_group: Mapped[str] = mapped_column(String, nullable=False, default="")
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    document_id: Mapped[str | None] = mapped_column(String, nullable=True)


class SourceCheck(Base):
    __tablename__ = "source_check"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    source_id: Mapped[str] = mapped_column(
        String, ForeignKey("factcheck_source.id", ondelete="CASCADE"), nullable=False
    )
    checked_at: Mapped[str] = mapped_column(String, nullable=False)
    http_status: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    final_url: Mapped[str] = mapped_column(String, nullable=False, default="")
    reachable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # None = 没有引文可校验（不是"校验失败"）
    quote_found: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    page_title: Mapped[str] = mapped_column(Text, nullable=False, default="")
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")


class Citation(Base):
    __tablename__ = "citation"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    claim_id: Mapped[str] = mapped_column(
        String, ForeignKey("claim_check.id", ondelete="CASCADE"), nullable=False
    )
    source_id: Mapped[str] = mapped_column(
        String, ForeignKey("factcheck_source.id"), nullable=False
    )
    # supporting | counter
    side: Mapped[str] = mapped_column(String, nullable=False, default="supporting")
    point: Mapped[str] = mapped_column(Text, nullable=False, default="")
    support: Mapped[str] = mapped_column(String, nullable=False, default="unknown")
    support_reason: Mapped[str] = mapped_column(Text, nullable=False, default="")


# --------------------------------------------------------------------------- #
# 留痕层
# --------------------------------------------------------------------------- #
class LLMCall(Base):
    __tablename__ = "llm_call"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    run_id: Mapped[str | None] = mapped_column(String, nullable=True)
    stage_run_id: Mapped[str | None] = mapped_column(String, nullable=True)
    purpose: Mapped[str] = mapped_column(String, nullable=False, default="")
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    ok: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    model: Mapped[str] = mapped_column(String, nullable=False, default="")
    base_url: Mapped[str] = mapped_column(String, nullable=False, default="")
    prompt_digest: Mapped[str] = mapped_column(String, nullable=False, default="")
    request_chars: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    response_chars: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # 全文，走内容寻址；ref 为空表示不足 4KB、内容内联在下面的 *_inline 列里
    prompt_ref: Mapped[str] = mapped_column(String, nullable=False, default="")
    response_ref: Mapped[str] = mapped_column(String, nullable=False, default="")
    prompt_inline: Mapped[str | None] = mapped_column(Text, nullable=True)
    response_inline: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String, nullable=False)


class TraceEvent(Base):
    __tablename__ = "trace_event"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str | None] = mapped_column(String, nullable=True)
    stage_run_id: Mapped[str | None] = mapped_column(String, nullable=True)
    seq: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ts: Mapped[str] = mapped_column(String, nullable=False)
    step: Mapped[str] = mapped_column(String, nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")


# --------------------------------------------------------------------------- #
# 度量
# --------------------------------------------------------------------------- #
class RunMetric(Base):
    __tablename__ = "run_metric"

    run_id: Mapped[str] = mapped_column(
        String, ForeignKey("workflow_run.id", ondelete="CASCADE"), primary_key=True
    )
    name: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
