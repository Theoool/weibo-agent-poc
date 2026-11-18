"""仓储层：领域对象（Pydantic）与数据库（ORM）之间唯一的转换点。

设计约束（见 docs/architecture.md）：
- 领域层不 import 这个模块。`Investigator` 只认 `TraceSink` 协议，入库发生在
  阶段边界，由 `main.py` 组装。
- 建表语句是权威，这里只做映射。改字段先改 `migrations/`。
- 写入分两类：证据层 append-only（正文永不 UPDATE），语义层按 stage 整体重写。

为什么事件、事实这些"可重算"的数据也要入库：
报告一旦发出去，就必须能回答"当时是这么判的"。B 级数据允许重算，
但**不允许算出来和当时不一样还没人知道**——所以每次运行产生独立的 stage_run，
重跑是 attempt+1 新增一份，不是覆盖旧的那份。
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import uuid
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session, sessionmaker

from agent.schemas import (
    Event as DomainEvent,
    Evidence,
    Fact as DomainFact,
    GapRecord as DomainGap,
    Investigation,
    Limitation as DomainLimitation,
    Metrics,
    SearchRecord as DomainSearch,
)

from .blobs import BlobStore
from .models import (
    Artifact,
    ClaimCheck,
    Citation,
    DocumentFetch,
    Event,
    EventEvidence,
    EventStatement,
    Fact,
    FactcheckSource,
    FactSupport,
    GapRecord,
    LLMCall,
    Limitation,
    RunMetric,
    SearchRecord,
    SourceCheck,
    SourceDocument,
    StageRun,
    TraceEvent,
    WorkflowRun,
)
from .db import PROJECT_ROOT

DEFAULT_BLOB_DIR = PROJECT_ROOT / "artifacts"

#: 数据分级只用在这几个地方，写死了才不会有人随手把 A 级数据当缓存删掉
QUALITY_LABELS = {"full": "完整（含全文快照）", "degraded": "降级（历史导入，无全文快照）"}


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    """可读前缀 + 时间戳 + 随机后缀。

    时间戳是给人看的（查日志时一眼知道先后），随机后缀才是唯一性来源。
    """
    return f"{prefix}_{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:6]}"


def canonical_url(url: str, mid: str = "") -> str:
    """去重用的规范 URL。

    微博帖子的地址在搜索页、详情页、移动端各不相同，但 mid 是同一个，
    所以有 mid 就按 mid 归一——否则同一条帖子会被当成 3 个来源。
    """
    mid = (mid or "").strip()
    if mid:
        return f"weibo:{mid}"
    url = (url or "").strip()
    if not url:
        return ""
    parts = urlsplit(url)
    path = parts.path.rstrip("/")
    query = f"?{parts.query}" if parts.query else ""
    if parts.scheme:
        return f"{parts.scheme}://{parts.netloc}{path}{query}".lower()
    return (path + query).lower()


def name_key(name: str) -> str:
    """事件名的稳定标识：只留中日韩文字、字母、数字。

    事件名每轮归纳都可能被改写（"深圳APEC交通管制" → "APEC 期间交通管制"），
    没有这个 key，缺口回溯就只能写"无法判定"。
    """
    return "".join(ch for ch in (name or "").lower() if ch.isalnum())[:80]


def _host_of(url: str) -> str:
    return urlsplit(url or "").netloc.lower()


#: 微博详情页的 mid：`/detail/5366736759032400` 或 `/status/5366736759032400`
_MID_IN_URL = re.compile(r"/(?:detail|status)/(\d{6,})")


def mid_from_url(url: str) -> str:
    """从详情页 URL 里取 mid。

    需要它是因为 `canonical_url` 对"有 mid 的帖子"统一成 `weibo:<mid>`，
    而核查清单里登记的是原始 URL。不补这一步，`source_check → document`
    永远匹配不上，M3 那个"结论 → 原文"的问题就答不了。
    """
    match = _MID_IN_URL.search(url or "")
    return match.group(1) if match else ""


def _dumps(value) -> str:
    return json.dumps(value if value is not None else [], ensure_ascii=False)


def _loads(text, default):
    try:
        return json.loads(text) if text else default
    except (TypeError, ValueError):
        return default


@lru_cache(maxsize=1)
def app_version() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=3,
        )
        return out.stdout.strip() if out.returncode == 0 else ""
    except Exception:  # noqa: BLE001
        return ""


# --------------------------------------------------------------------------- #
# RunStore
# --------------------------------------------------------------------------- #
class RunStore:
    def __init__(self, session_factory: sessionmaker[Session], blob_dir: str | Path | None = None) -> None:
        self.Session = session_factory
        self.blobs = BlobStore(blob_dir or DEFAULT_BLOB_DIR)

    # ---------------------------------------------------------------- 编排
    def create_run(
        self,
        topic: str,
        *,
        trigger: str = "cli",
        config: dict | None = None,
        quality: str = "full",
        mode: str = "",
        parent_run_id: str | None = None,
        status: str = "running",
        started_at: str | None = None,
    ) -> str:
        run_id = new_id("run")
        now = started_at or now_iso()
        with self.Session() as s:
            s.add(
                WorkflowRun(
                    id=run_id,
                    topic=topic,
                    status=status,
                    trigger=trigger,
                    quality=quality,
                    mode=mode,
                    config_json=json.dumps(config or {}, ensure_ascii=False),
                    app_version=app_version(),
                    parent_run_id=parent_run_id,
                    started_at=now,
                    finished_at=now if status != "running" else None,
                )
            )
            s.commit()
        return run_id

    def begin_stage(
        self, run_id: str, stage: str, *, input_stage_id: str | None = None
    ) -> str:
        stage_id = new_id("stg")
        with self.Session() as s:
            prev = s.execute(
                select(func.max(StageRun.attempt)).where(
                    StageRun.run_id == run_id, StageRun.stage == stage
                )
            ).scalar()
            s.add(
                StageRun(
                    id=stage_id,
                    run_id=run_id,
                    stage=stage,
                    attempt=int(prev or 0) + 1,
                    status="running",
                    input_stage_id=input_stage_id,
                    started_at=now_iso(),
                )
            )
            # 重跑会把已结束的 run 重新打开，否则状态与事实不符
            run = s.get(WorkflowRun, run_id)
            if run is not None:
                run.status = "running"
                run.finished_at = None
                run.error = ""
            s.commit()
        return stage_id

    def end_stage(self, stage_id: str, status: str = "succeeded", error: str = "") -> None:
        with self.Session() as s:
            row = s.get(StageRun, stage_id)
            if row is None:
                return
            row.status = status
            row.error = error or ""
            row.finished_at = now_iso()
            s.commit()

    def finish_run(
        self,
        run_id: str,
        status: str = "succeeded",
        *,
        stop_reason: str = "",
        error: str = "",
        partial: bool = False,
    ) -> None:
        with self.Session() as s:
            row = s.get(WorkflowRun, run_id)
            if row is None:
                return
            row.status = status
            row.stop_reason = stop_reason or row.stop_reason
            row.error = error or ""
            row.partial = partial
            row.finished_at = now_iso()
            s.commit()

    def latest_stage(self, run_id: str, stage: str) -> str | None:
        with self.Session() as s:
            row = s.execute(
                select(StageRun)
                .where(StageRun.run_id == run_id, StageRun.stage == stage)
                .order_by(StageRun.attempt.desc())
            ).scalars().first()
            return row.id if row else None

    # ---------------------------------------------------------------- 证据
    def _upsert_document(
        self,
        s: Session,
        cache: dict[tuple[str, str], str],
        fetched: set[str],
        *,
        run_id: str,
        stage_id: str | None,
        url: str,
        mid: str,
        kind: str,
        author: str,
        published_at: str,
        text: str,
        is_official: bool = False,
        detail_verified: bool = False,
    ) -> str:
        canonical = canonical_url(url, mid)
        if not canonical:
            # 没有 URL 也没有 mid 时退回正文哈希，否则所有空来源会撞成一行
            canonical = f"text:{hashlib.sha256((text or '').encode()).hexdigest()[:16]}"
        text_ref, digest, nbytes = self.blobs.put_text(text or "")
        key = (canonical, digest)

        doc_id = cache.get(key)
        if doc_id is None:
            row = s.execute(
                select(SourceDocument).where(
                    SourceDocument.url_canonical == canonical,
                    SourceDocument.content_hash == digest,
                )
            ).scalars().first()
            if row is not None:
                doc_id = row.id
                # 正文绝不覆盖；但"这次复核通过了"是可以补记的事实
                if detail_verified and not row.detail_verified:
                    row.detail_verified = True
            else:
                doc_id = new_id("doc")
                s.add(
                    SourceDocument(
                        id=doc_id,
                        url=url or "",
                        url_canonical=canonical,
                        mid=mid or "",
                        host=_host_of(url),
                        kind=kind or "weibo_post",
                        author=author or "",
                        published_at=published_at or "",
                        content_hash=digest,
                        bytes=nbytes,
                        text_inline=None if text_ref else (text or ""),
                        text_ref=text_ref,
                        is_official=is_official,
                        detail_verified=detail_verified,
                        first_seen_at=now_iso(),
                    )
                )
                s.flush()
            cache[key] = doc_id

        if doc_id not in fetched:
            fetched.add(doc_id)
            s.add(
                DocumentFetch(
                    id=new_id("fch"),
                    run_id=run_id,
                    stage_run_id=stage_id,
                    document_id=doc_id,
                    fetched_at=now_iso(),
                    http_status=200,
                    source_url=url or "",
                )
            )
        return doc_id

    def _document_id_for_url(self, s: Session, url: str) -> str | None:
        """按 URL 找已有的全文快照。mid 优先——同一条帖子有三种地址写法。"""
        keys: list[str] = []
        mid = mid_from_url(url)
        if mid:
            keys.append(f"weibo:{mid}")
        canonical = canonical_url(url, "")
        if canonical and canonical not in keys:
            keys.append(canonical)
        for key in keys:
            row = s.execute(
                select(SourceDocument)
                .where(SourceDocument.url_canonical == key)
                .order_by(SourceDocument.first_seen_at.desc())
            ).scalars().first()
            if row is not None:
                return row.id
        return None

    def _doc_of_evidence(
        self,
        s: Session,
        cache: dict[tuple[str, str], str],
        fetched: set[str],
        *,
        run_id: str,
        stage_id: str | None,
        item: Evidence,
        mid_doc: dict[str, str],
    ) -> str:
        """证据 → 文档。优先复用已抓到的全文快照，没有就用证据里那段摘录建一条。"""
        if item.mid and item.mid in mid_doc:
            return mid_doc[item.mid]
        return self._upsert_document(
            s,
            cache,
            fetched,
            run_id=run_id,
            stage_id=stage_id,
            url=item.url,
            mid=item.mid,
            kind=item.kind,
            author=item.author,
            published_at=item.time,
            text=item.content,
            is_official=item.kind in ("official_site",),
            detail_verified=item.verified,
        )

    # ---------------------------------------------------------------- 调查产物
    def save_investigation(
        self,
        inv: Investigation,
        run_id: str,
        stage_id: str,
        *,
        posts: dict[str, dict] | None = None,
    ) -> dict[str, int]:
        """把一次调查完整落库。

        `posts` 是 `Investigator.posts`（mid → 原始帖子）。必须传：
        `Investigation` 里的 `Evidence.content` 是截断过的摘录，只靠它入库，
        这条最该留住的 A 级数据就永久丢了。
        """
        posts = posts or {}
        cache: dict[tuple[str, str], str] = {}
        fetched: set[str] = set()
        stats = {"documents": 0, "events": 0, "facts": 0, "gaps": 0, "searches": 0}

        with self.Session() as s:
            run = s.get(WorkflowRun, run_id)
            if run is not None:
                run.mode = inv.mode
                # 报告的标题才是这段内容的正式名称（`APEC` → `APEC 微博舆情`），
                # 用搜索词当 topic 会让读回时少半截
                if inv.topic:
                    run.topic = inv.topic
                if inv.stop_reason:
                    run.stop_reason = inv.stop_reason
                try:
                    run.report_at = inv.generated_at.isoformat(timespec="seconds")
                except Exception:  # noqa: BLE001
                    pass

            # 1) 所有读到的帖子都留全文快照，不只是被引用为证据的那些
            mid_doc: dict[str, str] = {}
            for mid, post in posts.items():
                mid_doc[str(mid)] = self._upsert_document(
                    s,
                    cache,
                    fetched,
                    run_id=run_id,
                    stage_id=stage_id,
                    url=str(post.get("url") or ""),
                    mid=str(mid or ""),
                    kind="weibo_post",
                    author=str(post.get("author") or ""),
                    published_at=str(post.get("time") or ""),
                    text=str(post.get("text") or ""),
                    is_official=bool(post.get("verified")),
                    detail_verified=bool(post.get("detail_verified")),
                )
            stats["documents"] = len(fetched)

            # 2) 事件 → 证据 → 事实 → 观点/推测
            for seq, ev in enumerate(inv.events, 1):
                event_id = new_id("ev")
                s.add(
                    Event(
                        id=event_id,
                        stage_run_id=stage_id,
                        seq=seq,
                        event_key=name_key(ev.name),
                        name=ev.name,
                        summary=ev.summary or "",
                        heat=ev.heat,
                        heat_basis=ev.heat_basis or "",
                        judgement=ev.judgement or "",
                        keywords_json=_dumps(ev.keywords),
                        merged_from_json=_dumps(ev.merged_from),
                    )
                )
                s.flush()
                stats["events"] += 1

                for role, items in (("evidence", ev.evidence), ("official", ev.official_sources)):
                    seen: set[str] = set()
                    for seq, item in enumerate(items, 1):
                        doc_id = self._doc_of_evidence(
                            s,
                            cache,
                            fetched,
                            run_id=run_id,
                            stage_id=stage_id,
                            item=item,
                            mid_doc=mid_doc,
                        )
                        if doc_id in seen:
                            continue
                        seen.add(doc_id)
                        s.add(
                            EventEvidence(
                                event_id=event_id,
                                document_id=doc_id,
                                role=role,
                                seq=seq,
                                verified=bool(item.verified),
                                note=item.note or "",
                                content_excerpt=item.content or "",
                            )
                        )
                stats["documents"] = len(fetched)

                for fseq, fact in enumerate(ev.facts, 1):
                    fact_id = new_id("fct")
                    s.add(
                        Fact(
                            id=fact_id,
                            event_id=event_id,
                            seq=fseq,
                            text=fact.text,
                            kind=fact.kind,
                            source_kind=fact.source_kind,
                            inferred_link=bool(fact.inferred_link),
                        )
                    )
                    s.flush()
                    stats["facts"] += 1
                    link_kind = "inferred" if fact.inferred_link else "annotated"
                    linked: set[str] = set()
                    for mid in fact.supporting_mids:
                        doc_id = mid_doc.get(str(mid))
                        if not doc_id or doc_id in linked:
                            continue
                        linked.add(doc_id)
                        s.add(
                            FactSupport(
                                fact_id=fact_id, document_id=doc_id, link_kind=link_kind
                            )
                        )

                for kind, texts in (("opinion", ev.opinions), ("speculation", ev.speculations)):
                    for i, text in enumerate(texts, 1):
                        if not str(text).strip():
                            continue
                        s.add(
                            EventStatement(
                                id=new_id("stm"),
                                event_id=event_id,
                                kind=kind,
                                seq=i,
                                text=str(text),
                            )
                        )

            # 3) 决策链与限制
            for g in inv.gaps:
                s.add(
                    GapRecord(
                        id=new_id("gap"),
                        stage_run_id=stage_id,
                        round=g.round,
                        kind=g.kind,
                        target=g.target,
                        detail=g.detail,
                        action=g.action,
                        resolved=g.resolved,
                    )
                )
                stats["gaps"] += 1

            for lim in inv.limitations:
                s.add(
                    Limitation(
                        id=new_id("lim"),
                        stage_run_id=stage_id,
                        step=lim.step,
                        detail=lim.detail,
                        hit_count=1,
                    )
                )

            for i, rec in enumerate(inv.searches, 1):
                s.add(
                    SearchRecord(
                        id=new_id("sch"),
                        stage_run_id=stage_id,
                        seq=i,
                        round=rec.round,
                        keyword=rec.keyword,
                        url=rec.url or "",
                        posts_read=rec.posts_read,
                        new_posts=rec.new_posts,
                        limitation=rec.limitation or "",
                    )
                )
                stats["searches"] += 1

            self._write_metrics(s, run_id, inv.metrics.model_dump())
            s.commit()

        return stats

    # ---------------------------------------------------------------- 核查产物
    def save_factcheck(
        self,
        report,
        run_id: str,
        stage_id: str,
        *,
        checked: bool = True,
        artifacts: dict[str, str] | None = None,
    ) -> dict[str, int]:
        """把一次核查落库。

        `checked=False`（跳过机检）时不写 `source_check`——否则会留下一批
        「不可达」的假记录，日后无法与"真的校验失败"区分。
        """
        stats = {"sources": 0, "claims": 0, "citations": 0, "checks": 0}

        with self.Session() as s:
            src_ids: dict[str, str] = {}
            for src in report.sources:
                sid = new_id("src")
                src_ids[src.id] = sid
                doc_id = self._document_id_for_url(s, src.url)
                s.add(
                    FactcheckSource(
                        id=sid,
                        stage_run_id=stage_id,
                        source_key=src.id,
                        kind=src.kind,
                        publisher=src.publisher or "",
                        title=src.title or "",
                        url=src.url or "",
                        published_at=src.published_at or "",
                        quote=src.quote or "",
                        independence_group=src.independence_group or "",
                        note=src.note or "",
                        document_id=doc_id,
                    )
                )
                s.flush()
                stats["sources"] += 1
                if checked:
                    s.add(
                        SourceCheck(
                            id=new_id("chk"),
                            source_id=sid,
                            checked_at=now_iso(),
                            http_status=int(src.http_status or 0),
                            final_url="",
                            reachable=bool(src.reachable),
                            quote_found=src.quote_found,
                            page_title=src.page_title or "",
                            note=src.check_note or "",
                        )
                    )
                    stats["checks"] += 1

            for i, claim in enumerate(report.claims, 1):
                cid = new_id("clm")
                s.add(
                    ClaimCheck(
                        id=cid,
                        stage_run_id=stage_id,
                        seq=i,
                        claim_key=claim.id,
                        claim=claim.claim,
                        report_line=claim.report_line or "",
                        verdict=claim.verdict,
                        final_verdict=claim.final_verdict,
                        confidence=claim.confidence,
                        analysis=claim.analysis or "",
                        citation_check=claim.citation_check,
                        semantic_note=claim.semantic_note or "",
                        cross_conflict=bool(claim.cross_conflict),
                        cross_conflict_reason=claim.cross_conflict_reason or "",
                        independent_groups=int(claim.independent_groups or 0),
                        downgraded=bool(claim.downgraded),
                        semantic_revised=bool(claim.semantic_revised),
                        handoff_fact_id=None,
                    )
                )
                s.flush()
                stats["claims"] += 1

                for side, cites in (("supporting", claim.supporting), ("counter", claim.counter)):
                    for cite in cites:
                        sid = src_ids.get(cite.source_id)
                        if not sid:
                            # 引用了未登记来源，报告里本来就该标出来，这里不编一条
                            continue
                        s.add(
                            Citation(
                                id=new_id("cit"),
                                claim_id=cid,
                                source_id=sid,
                                side=side,
                                point=cite.point or "",
                                support=cite.support,
                                support_reason=cite.support_reason or "",
                            )
                        )
                        stats["citations"] += 1

            metrics = {
                f"fc_{k}": float(v)
                for k, v in (report.summary or {}).items()
                if isinstance(v, (int, float)) and not isinstance(v, bool)
            }
            self._write_metrics(s, run_id, metrics)
            s.commit()

        # 写 artifact 要新开连接，必须等上面的事务结束再写，否则 SQLite 会锁住
        for kind, text in (artifacts or {}).items():
            self.save_artifact(run_id, stage_id, kind, text)

        return stats

    # ---------------------------------------------------------------- 其他
    def _write_metrics(self, s: Session, run_id: str, values: dict) -> None:
        """指标是 C 级：整批覆盖，不需要历史版本。"""
        s.execute(RunMetric.__table__.delete().where(RunMetric.run_id == run_id))
        for name, value in values.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            s.add(RunMetric(run_id=run_id, name=name, value=float(value)))

    def save_artifact(
        self, run_id: str, stage_id: str | None, kind: str, text: str
    ) -> str:
        digest, nbytes = self.blobs.put(text.encode("utf-8"))
        artifact_id = new_id("art")
        with self.Session() as s:
            s.add(
                Artifact(
                    id=artifact_id,
                    run_id=run_id,
                    stage_run_id=stage_id,
                    kind=kind,
                    content_ref=digest,
                    sha256=digest,
                    bytes=nbytes,
                    created_at=now_iso(),
                )
            )
            s.commit()
        return artifact_id

    def read_artifact(self, run_id: str, kind: str | None = None) -> str:
        with self.Session() as s:
            q = select(Artifact).where(Artifact.run_id == run_id)
            if kind:
                q = q.where(Artifact.kind == kind)
            row = s.execute(q.order_by(Artifact.created_at.desc())).scalars().first()
        if row is None or not row.content_ref:
            return ""
        return self.blobs.get_text(row.content_ref)

    # ---------------------------------------------------------------- 留痕
    def save_trace_event(
        self,
        run_id: str,
        stage_run_id: str | None,
        *,
        seq: int,
        ts: str,
        step: str,
        payload: dict,
    ) -> None:
        """写一条轨迹。调用方负责递增 seq（同一个 sink 内计数，天然有序）。"""
        with self.Session() as s:
            s.add(
                TraceEvent(
                    run_id=run_id,
                    stage_run_id=stage_run_id,
                    seq=seq,
                    ts=ts,
                    step=step,
                    # default=str：轨迹字段来自领域层的任意对象，遇到 datetime
                    # 之类不该让整条记录写不进去
                    payload_json=json.dumps(payload, ensure_ascii=False, default=str),
                )
            )
            s.commit()

    def save_llm_call(self, run_id: str, stage_run_id: str | None, call) -> str:
        """一次模型调用落库：元数据进列，题面与输出走"小于 4KB 内联 / 否则 CAS"。

        `call` 是 `agent.trace.LLMCall`（dataclass）。这里不 import 它的类型做校验：
        仓储层只依赖字段名，测试里可以用一个同名属性的假对象替代。
        """
        prompt_text = call.request_text
        prompt_ref, _, _ = self.blobs.put_text(prompt_text)
        response_ref, _, _ = self.blobs.put_text(call.content or "")
        call_id = new_id("llm")
        with self.Session() as s:
            s.add(
                LLMCall(
                    id=call_id,
                    run_id=run_id,
                    stage_run_id=stage_run_id,
                    purpose=call.purpose,
                    attempt=int(call.attempt or 1),
                    ok=bool(call.ok),
                    model=call.model or "",
                    base_url=call.base_url or "",
                    prompt_digest=call.prompt_digest or "",
                    request_chars=int(call.request_chars),
                    response_chars=len(call.content or ""),
                    duration_ms=int(call.duration_ms or 0),
                    error=call.error or "",
                    prompt_ref=prompt_ref or "",
                    response_ref=response_ref or "",
                    prompt_inline=None if prompt_ref else prompt_text,
                    response_inline=None if response_ref else (call.content or ""),
                    created_at=call.created_at or now_iso(),
                )
            )
            s.commit()
        return call_id

    def _llm_call_dict(self, row: LLMCall, include_text: bool = True) -> dict:
        def full_text(inline: str | None, ref: str) -> str:
            if ref:
                return self.blobs.get_text(ref)
            return inline or ""

        prompt = full_text(row.prompt_inline, row.prompt_ref) if include_text else ""
        response = full_text(row.response_inline, row.response_ref) if include_text else ""
        return {
            "id": row.id,
            "purpose": row.purpose,
            "attempt": row.attempt,
            "ok": bool(row.ok),
            "model": row.model,
            "base_url": row.base_url,
            "prompt_digest": row.prompt_digest,
            "request_chars": row.request_chars,
            "response_chars": row.response_chars,
            "duration_ms": row.duration_ms,
            # 与 JSONL 轨迹同名，查看器可以直接复用同一套渲染
            "response_preview": (response or "")[:300],
            "error": row.error,
            "created_at": row.created_at,
            "prompt_ref": row.prompt_ref,
            "response_ref": row.response_ref,
            "prompt": prompt,
            "response": response,
        }

    def load_llm_calls(
        self, run_id: str, limit: int = 200, offset: int = 0, include_text: bool = True
    ) -> list[dict]:
        with self.Session() as s:
            rows = list(
                s.execute(
                    select(LLMCall)
                    .where(LLMCall.run_id == run_id)
                    .order_by(LLMCall.created_at, LLMCall.id)
                    .limit(limit)
                    .offset(offset)
                ).scalars()
            )
            return [self._llm_call_dict(r, include_text=include_text) for r in rows]

    def count_llm_calls(self, run_id: str) -> int:
        with self.Session() as s:
            return int(
                s.execute(
                    select(func.count()).select_from(LLMCall).where(LLMCall.run_id == run_id)
                ).scalar()
                or 0
            )

    def load_llm_call(self, call_id: str) -> dict | None:
        with self.Session() as s:
            row = s.get(LLMCall, call_id)
            return self._llm_call_dict(row) if row is not None else None

    def load_trace(
        self,
        run_id: str,
        limit: int = 5000,
        step: str | None = None,
        offset: int = 0,
    ) -> list[dict]:
        """按 JSONL 的同一形状读回轨迹，让查看器只写一套渲染。

        `step` 过滤在这里做而不是调用方做：轨迹条数会随轮次线性增长，
        查"只看缺口判定"时没必要把几千条读进内存。
        """
        with self.Session() as s:
            q = select(TraceEvent).where(TraceEvent.run_id == run_id)
            if step:
                q = q.where(TraceEvent.step == step)
            rows = list(
                s.execute(
                    q.order_by(TraceEvent.seq, TraceEvent.id).limit(limit).offset(offset)
                ).scalars()
            )
        return [{"ts": r.ts, "step": r.step, **_loads(r.payload_json, {})} for r in rows]

    def count_trace(self, run_id: str, step: str | None = None) -> int:
        with self.Session() as s:
            q = select(func.count()).select_from(TraceEvent).where(TraceEvent.run_id == run_id)
            if step:
                q = q.where(TraceEvent.step == step)
            return int(s.execute(q).scalar() or 0)

    def set_run_topic(self, run_id: str, topic: str) -> None:
        """核查阶段的 run 先按文件名建出来，报告标题要等跑完才知道。

        不改的话，`--runs` 里会留下一堆 `claims_handoff` 这种文件名当主题。
        """
        topic = (topic or "").strip()
        if not topic:
            return
        with self.Session() as s:
            row = s.get(WorkflowRun, run_id)
            if row is None:
                return
            row.topic = topic
            s.commit()

    # ---------------------------------------------------------------- 读回
    def load_investigation(self, run_id: str) -> Investigation | None:
        """从库里重建 `Investigation`。

        这不是为了替代文件导出，而是校验 schema 是否够用：如果重建结果
        与原始 JSON 对不上，说明有字段没存下来。
        """
        with self.Session() as s:
            run = s.get(WorkflowRun, run_id)
            if run is None:
                return None
            stage = s.execute(
                select(StageRun)
                .where(StageRun.run_id == run_id, StageRun.stage == "investigate")
                .order_by(StageRun.attempt.desc())
            ).scalars().first()
            if stage is None:
                return None

            events: list[DomainEvent] = []
            for row in s.execute(
                select(Event).where(Event.stage_run_id == stage.id).order_by(Event.seq)
            ).scalars():
                evidence: list[Evidence] = []
                official: list[Evidence] = []
                for ee, doc in s.execute(
                    select(EventEvidence, SourceDocument)
                    .join(SourceDocument, EventEvidence.document_id == SourceDocument.id)
                    .where(EventEvidence.event_id == row.id)
                    .order_by(EventEvidence.role, EventEvidence.seq)
                ).all():
                    text = ee.content_excerpt or doc.text_inline or ""
                    item = Evidence(
                        kind=doc.kind,
                        mid=doc.mid,
                        author=doc.author,
                        time=doc.published_at,
                        content=text,
                        url=doc.url,
                        verified=bool(ee.verified),
                        note=ee.note,
                    )
                    (official if ee.role == "official" else evidence).append(item)

                facts: list[DomainFact] = []
                for f in s.execute(
                    select(Fact).where(Fact.event_id == row.id).order_by(Fact.seq)
                ).scalars():
                    mids = s.execute(
                        select(SourceDocument.mid)
                        .join(FactSupport, FactSupport.document_id == SourceDocument.id)
                        .where(FactSupport.fact_id == f.id)
                    ).scalars().all()
                    facts.append(
                        DomainFact(
                            text=f.text,
                            kind=f.kind,
                            supporting_mids=[m for m in mids if m],
                            source_kind=f.source_kind,
                            inferred_link=bool(f.inferred_link),
                        )
                    )

                opinions: list[str] = []
                speculations: list[str] = []
                for st in s.execute(
                    select(EventStatement)
                    .where(EventStatement.event_id == row.id)
                    .order_by(EventStatement.kind, EventStatement.seq)
                ).scalars():
                    (speculations if st.kind == "speculation" else opinions).append(st.text)

                events.append(
                    DomainEvent(
                        name=row.name,
                        summary=row.summary,
                        heat=row.heat,
                        heat_basis=row.heat_basis,
                        facts=facts,
                        opinions=opinions,
                        speculations=speculations,
                        evidence=evidence,
                        official_sources=official,
                        judgement=row.judgement,
                        keywords=_loads(row.keywords_json, []),
                        merged_from=_loads(row.merged_from_json, []),
                    )
                )

            searches = [
                DomainSearch(
                    keyword=r.keyword,
                    url=r.url,
                    posts_read=r.posts_read,
                    new_posts=r.new_posts,
                    round=r.round,
                    limitation=r.limitation,
                )
                for r in s.execute(
                    select(SearchRecord)
                    .where(SearchRecord.stage_run_id == stage.id)
                    .order_by(SearchRecord.seq)
                ).scalars()
            ]
            gaps = [
                DomainGap(
                    round=g.round,
                    kind=g.kind,
                    target=g.target,
                    detail=g.detail,
                    action=g.action,
                    resolved=g.resolved,
                )
                for g in s.execute(
                    select(GapRecord)
                    .where(GapRecord.stage_run_id == stage.id)
                    .order_by(GapRecord.round)
                ).scalars()
            ]
            limitations = [
                DomainLimitation(step=x.step, detail=x.detail)
                for x in s.execute(
                    select(Limitation).where(Limitation.stage_run_id == stage.id)
                ).scalars()
            ]
            metrics_raw = {
                r.name: r.value
                for r in s.execute(
                    select(RunMetric).where(RunMetric.run_id == run_id)
                ).scalars()
                if not r.name.startswith("fc_")
            }
            allowed = set(Metrics.model_fields)
            metrics = Metrics(**{k: int(v) for k, v in metrics_raw.items() if k in allowed})

            finished = run.report_at or stage.finished_at or run.started_at
            try:
                generated_at = datetime.fromisoformat(finished)
            except (TypeError, ValueError):
                generated_at = datetime.now()

            return Investigation(
                topic=run.topic,
                generated_at=generated_at,
                mode=run.mode or "heuristic",
                events=events,
                searches=searches,
                limitations=limitations,
                gaps=gaps,
                stop_reason=run.stop_reason,
                metrics=metrics,
                run_id=run_id,
            )

    def run_summary(self, run_id: str) -> dict | None:
        with self.Session() as s:
            run = s.get(WorkflowRun, run_id)
            if run is None:
                return None
            stages = [
                {
                    "stage": x.stage,
                    "attempt": x.attempt,
                    "status": x.status,
                    "started_at": x.started_at,
                    "finished_at": x.finished_at,
                    "error": x.error,
                    "id": x.id,
                }
                for x in s.execute(
                    select(StageRun)
                    .where(StageRun.run_id == run_id)
                    # started_at 只到秒：三个阶段常常同一秒内建完，再按 rowid 兜底，
                    # 否则顺序由 SQLite 心情决定，报告里的阶段顺序会跳来跳去
                    .order_by(StageRun.started_at, text("stage_run.rowid"))
                ).scalars()
            ]
            stage_ids = [x["id"] for x in stages]
            counts = {
                "events": s.execute(
                    select(func.count()).select_from(Event).where(Event.stage_run_id.in_(stage_ids))
                ).scalar() or 0,
                "facts": s.execute(
                    select(func.count()).select_from(Fact).where(Fact.event_id.in_(
                        select(Event.id).where(Event.stage_run_id.in_(stage_ids))
                    ))
                ).scalar() or 0,
                "claims": s.execute(
                    select(func.count()).select_from(ClaimCheck).where(
                        ClaimCheck.stage_run_id.in_(stage_ids)
                    )
                ).scalar() or 0,
                "documents": s.execute(
                    select(func.count()).select_from(DocumentFetch).where(
                        DocumentFetch.run_id == run_id
                    )
                ).scalar() or 0,
                "llm_calls": s.execute(
                    select(func.count()).select_from(LLMCall).where(LLMCall.run_id == run_id)
                ).scalar() or 0,
                "trace_events": s.execute(
                    select(func.count()).select_from(TraceEvent).where(
                        TraceEvent.run_id == run_id
                    )
                ).scalar() or 0,
            }
            metrics = {
                r.name: r.value
                for r in s.execute(
                    select(RunMetric).where(RunMetric.run_id == run_id)
                ).scalars()
            }
            return {
                "id": run.id,
                "topic": run.topic,
                "status": run.status,
                "trigger": run.trigger,
                "quality": run.quality,
                "mode": run.mode,
                "stop_reason": run.stop_reason,
                "started_at": run.started_at,
                "finished_at": run.finished_at,
                "error": run.error,
                "partial": bool(run.partial),
                "parent_run_id": run.parent_run_id,
                "stages": stages,
                "counts": counts,
                "metrics": metrics,
            }

    def list_runs(self, limit: int = 20, topic: str | None = None, offset: int = 0) -> list[dict]:
        with self.Session() as s:
            q = (
                select(WorkflowRun)
                .order_by(WorkflowRun.started_at.desc())
                .limit(limit)
                .offset(offset)
            )
            if topic:
                q = q.where(WorkflowRun.topic.contains(topic))
            rows = list(s.execute(q).scalars())
            out = []
            for run in rows:
                stages = s.execute(
                    select(func.count()).select_from(StageRun).where(StageRun.run_id == run.id)
                ).scalar() or 0
                out.append(
                    {
                        "id": run.id,
                        "topic": run.topic,
                        "status": run.status,
                        "trigger": run.trigger,
                        "quality": run.quality,
                        "started_at": run.started_at,
                        "stages": stages,
                        "stop_reason": run.stop_reason,
                    }
                )
            return out

    # ---------------------------------------------------------------- 只读视图
    # 这些方法只服务 API，不改任何状态。分开的理由：写入路径要小心事务，
    # 读取路径要方便分页与拼装，混在一起两边都会被对方的约束拖住。
    def run_provenance(self, run_id: str) -> dict | None:
        """这份产物是从哪来的。

        M3 的验收问题之一是「这份核查报告核的是哪一次调查」。答案有两层：
        - 新运行：核查与调查在同一个 run 里，`investigate` 阶段就是上游；
        - 历史导入：当年靠固定文件名串接，只有 `config_json.source_run_id`
          可能记得上游，多数时候是空的 —— 这时如实说"没有链"，不猜。
        """
        with self.Session() as s:
            run = s.get(WorkflowRun, run_id)
            if run is None:
                return None
            stages = list(
                s.execute(
                    select(StageRun)
                    .where(StageRun.run_id == run_id)
                    .order_by(StageRun.started_at, text("stage_run.rowid"))
                ).scalars()
            )
            by_id = {x.id: x for x in stages}
            chain: list[dict] = []
            seen: set[str] = set()
            cur = stages[-1] if stages else None
            while cur is not None and cur.id not in seen:
                seen.add(cur.id)
                chain.append(
                    {"stage": cur.stage, "stage_id": cur.id, "attempt": cur.attempt}
                )
                cur = by_id.get(cur.input_stage_id or "")
            chain.reverse()

            config = _loads(run.config_json, {}) or {}
            investigate = next((x for x in reversed(stages) if x.stage == "investigate"), None)
            factcheck = next((x for x in reversed(stages) if x.stage == "factcheck"), None)
            note = ""
            if factcheck is not None and investigate is None:
                note = (
                    "该 run 只有核查阶段，且没有登记上游调查：历史产物当年靠固定文件名串接，"
                    "无法确定核对的是哪一次调查。"
                )
            return {
                "run_id": run.id,
                "topic": run.topic,
                "trigger": run.trigger,
                "quality": run.quality,
                "investigate_stage_id": investigate.id if investigate else None,
                "factcheck_stage_id": factcheck.id if factcheck else None,
                # 旧产物导入时写在 config 里的上游提示，可能为空
                "source_run_id": str(config.get("source_run_id") or ""),
                "chain": chain,
                "note": note,
            }

    def list_documents(self, run_id: str, limit: int = 50, offset: int = 0) -> dict:
        """一次 run 抓到的证据文档（元数据，不含全文）。"""
        with self.Session() as s:
            run = s.get(WorkflowRun, run_id)
            quality = run.quality if run is not None else "full"
            total = int(
                s.execute(
                    select(func.count())
                    .select_from(DocumentFetch)
                    .where(DocumentFetch.run_id == run_id)
                ).scalar()
                or 0
            )
            rows = s.execute(
                select(DocumentFetch, SourceDocument)
                .join(SourceDocument, DocumentFetch.document_id == SourceDocument.id)
                .where(DocumentFetch.run_id == run_id)
                .order_by(DocumentFetch.fetched_at, DocumentFetch.id)
                .limit(limit)
                .offset(offset)
            ).all()
            items = []
            for fetch, doc in rows:
                items.append(
                    {
                        "id": doc.id,
                        "mid": doc.mid,
                        "author": doc.author,
                        "published_at": doc.published_at,
                        "url": doc.url,
                        "kind": doc.kind,
                        "bytes": doc.bytes,
                        "is_official": bool(doc.is_official),
                        "detail_verified": bool(doc.detail_verified),
                        # 有 ref 或内联都算"有可读正文"；degraded 导入的那份
                        # 只是当年截断后的摘录，用 excerpt_only 标出来，别当全文用
                        "has_full_text": bool(doc.text_ref or doc.text_inline),
                        "excerpt_only": quality != "full",
                        "fetched_at": fetch.fetched_at,
                        "excerpt": (doc.text_inline or "")[:80],
                    }
                )
            return {"total": total, "quality": quality, "items": items}

    def get_document(self, doc_id: str) -> dict | None:
        with self.Session() as s:
            doc = s.get(SourceDocument, doc_id)
            if doc is None:
                return None
            text = self.blobs.get_text(doc.text_ref) if doc.text_ref else (doc.text_inline or "")
            # 只被 degraded 的 run（历史导入）抓过 = 这段正文是当年截断后的摘录
            qualities = list(
                s.execute(
                    select(WorkflowRun.quality)
                    .join(DocumentFetch, DocumentFetch.run_id == WorkflowRun.id)
                    .where(DocumentFetch.document_id == doc_id)
                ).scalars()
            )
            return {
                "id": doc.id,
                "url": doc.url,
                "url_canonical": doc.url_canonical,
                "mid": doc.mid,
                "host": doc.host,
                "kind": doc.kind,
                "author": doc.author,
                "published_at": doc.published_at,
                "content_hash": doc.content_hash,
                "bytes": doc.bytes,
                "is_official": bool(doc.is_official),
                "detail_verified": bool(doc.detail_verified),
                "first_seen_at": doc.first_seen_at,
                "storage": "blob" if doc.text_ref else "inline",
                "excerpt_only": bool(qualities) and all(q != "full" for q in qualities),
                "text": text,
            }

    def document_history(self, doc_id: str) -> dict | None:
        """这条帖子被抓过几次、内容变没变。

        同一 `url_canonical` 出现多个 `content_hash` 就是内容被改过；
        这里把同 URL 的所有版本都列出来，让"改没改"可判定，而不是靠猜。
        """
        doc = self.get_document(doc_id)
        if doc is None:
            return None
        with self.Session() as s:
            fetches = [
                {
                    "id": f.id,
                    "run_id": f.run_id,
                    "stage_run_id": f.stage_run_id,
                    "fetched_at": f.fetched_at,
                    "http_status": f.http_status,
                    "source_url": f.source_url,
                }
                for f in s.execute(
                    select(DocumentFetch)
                    .where(DocumentFetch.document_id == doc_id)
                    .order_by(DocumentFetch.fetched_at)
                ).scalars()
            ]
            versions = [
                {
                    "id": v.id,
                    "content_hash": v.content_hash,
                    "bytes": v.bytes,
                    "first_seen_at": v.first_seen_at,
                    "is_current": v.id == doc_id,
                }
                for v in s.execute(
                    select(SourceDocument)
                    .where(SourceDocument.url_canonical == doc["url_canonical"])
                    .order_by(SourceDocument.first_seen_at)
                ).scalars()
            ]
        return {
            "document": {
                k: doc[k]
                for k in ("id", "url", "url_canonical", "mid", "author", "excerpt_only")
            },
            "fetch_count": len(fetches),
            "fetches": fetches,
            "version_count": len(versions),
            "versions": versions,
            "content_changed": len(versions) > 1,
        }

    def run_gaps(self, run_id: str) -> list[dict]:
        with self.Session() as s:
            stage_ids = select(StageRun.id).where(StageRun.run_id == run_id)
            rows = list(
                s.execute(
                    select(GapRecord)
                    .where(GapRecord.stage_run_id.in_(stage_ids))
                    .order_by(GapRecord.round, GapRecord.id)
                ).scalars()
            )
            return [
                {
                    "id": g.id,
                    "round": g.round,
                    "kind": g.kind,
                    "target": g.target,
                    "detail": g.detail,
                    "action": g.action,
                    # 三态：True 已补齐 / False 仍缺 / None 无法判定
                    "resolved": g.resolved,
                }
                for g in rows
            ]

    def run_claims(self, run_id: str) -> list[dict]:
        """核查结论 + 它引用的来源 + 来源对应的原文快照 id。

        这是「这份核查的依据是哪几条帖子原文」的实现：`citation → factcheck_source
        → document_id`，再配合 `GET /api/documents/{id}` 拿到全文。
        历史导入的核查多数拿不到 document_id（当年没存 URL 归一后的文档），
        这时 `document_id` 为 null，`quote` 仍然在，能看出"当时引的是哪一段"。
        """
        with self.Session() as s:
            stage_ids = select(StageRun.id).where(StageRun.run_id == run_id)
            claims = list(
                s.execute(
                    select(ClaimCheck)
                    .where(ClaimCheck.stage_run_id.in_(stage_ids))
                    .order_by(ClaimCheck.seq)
                ).scalars()
            )
            out: list[dict] = []
            for c in claims:
                sources: list[dict] = []
                for cite in s.execute(
                    select(Citation)
                    .where(Citation.claim_id == c.id)
                    .order_by(Citation.side, Citation.id)
                ).scalars():
                    src = s.get(FactcheckSource, cite.source_id)
                    if src is None:
                        # 引用了未登记来源：报告里本来就该暴露，这里不补一条假的
                        sources.append(
                            {
                                "source_key": "",
                                "missing": True,
                                "side": cite.side,
                                "point": cite.point,
                                "support": cite.support,
                                "reason": cite.support_reason,
                            }
                        )
                        continue
                    sources.append(
                        {
                            "source_key": src.source_key,
                            "publisher": src.publisher,
                            "title": src.title,
                            "url": src.url,
                            "kind": src.kind,
                            "quote": src.quote,
                            "side": cite.side,
                            "point": cite.point,
                            "support": cite.support,
                            "reason": cite.support_reason,
                            "document_id": src.document_id,
                            "document_available": bool(src.document_id),
                        }
                    )
                out.append(
                    {
                        "claim_key": c.claim_key or c.id,
                        "claim": c.claim,
                        "report_line": c.report_line,
                        "verdict": c.verdict,
                        "final_verdict": c.final_verdict,
                        # 对外只用这个，避免"字段说 A、正文说 B"
                        "effective": c.final_verdict or c.verdict,
                        "confidence": c.confidence,
                        "analysis": c.analysis,
                        "citation_check": c.citation_check,
                        "semantic_note": c.semantic_note,
                        "cross_conflict": bool(c.cross_conflict),
                        "cross_conflict_reason": c.cross_conflict_reason,
                        "independent_groups": c.independent_groups,
                        "downgraded": bool(c.downgraded),
                        "semantic_revised": bool(c.semantic_revised),
                        "handoff_fact_id": c.handoff_fact_id,
                        "sources": sources,
                    }
                )
            return out

    def close(self) -> None:
        """释放连接池。API 进程退出与测试清理都要调，否则 Windows 删不掉 .db。"""
        try:
            self.engine.dispose()
        except Exception:  # noqa: BLE001
            pass

    @property
    def engine(self):
        return self.Session.kw["bind"]
