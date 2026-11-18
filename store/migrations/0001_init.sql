-- 0001_init.sql —— 初始 schema。
--
-- 分级约定（详见 docs/architecture.md）：
--   A 不可再生 → append-only，正文与结论禁止 UPDATE
--   B 可重算但贵 → 允许整体重写
--   C 可丢弃 → 允许 DELETE
--
-- 大文本策略：正文 < 4KB 存 text_inline，否则写 artifacts/ 内容寻址目录，
-- 只在 text_ref 存 sha256。同一内容跨 run 只存一份。

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_version (
    version    INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL,
    note       TEXT NOT NULL DEFAULT ''
);

-- --------------------------------------------------------------------- #
-- 编排层
-- --------------------------------------------------------------------- #
CREATE TABLE IF NOT EXISTS workflow_run (
    id            TEXT PRIMARY KEY,
    topic         TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'running',
    trigger       TEXT NOT NULL DEFAULT 'cli',
    quality       TEXT NOT NULL DEFAULT 'full',
    mode          TEXT NOT NULL DEFAULT '',
    config_json   TEXT NOT NULL DEFAULT '{}',
    app_version   TEXT NOT NULL DEFAULT '',
    stop_reason   TEXT NOT NULL DEFAULT '',
    parent_run_id TEXT,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    error         TEXT NOT NULL DEFAULT '',
    partial       INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_run_topic  ON workflow_run(topic, started_at);
CREATE INDEX IF NOT EXISTS ix_run_status ON workflow_run(status);

CREATE TABLE IF NOT EXISTS stage_run (
    id             TEXT PRIMARY KEY,
    run_id         TEXT NOT NULL REFERENCES workflow_run(id) ON DELETE CASCADE,
    stage          TEXT NOT NULL,
    attempt        INTEGER NOT NULL DEFAULT 1,
    status         TEXT NOT NULL DEFAULT 'running',
    input_stage_id TEXT,
    started_at     TEXT NOT NULL,
    finished_at    TEXT,
    error          TEXT NOT NULL DEFAULT '',
    UNIQUE (run_id, stage, attempt)
);
CREATE INDEX IF NOT EXISTS ix_stage_run ON stage_run(run_id, stage);

CREATE TABLE IF NOT EXISTS artifact (
    id           TEXT PRIMARY KEY,
    run_id       TEXT NOT NULL REFERENCES workflow_run(id) ON DELETE CASCADE,
    stage_run_id TEXT,
    kind         TEXT NOT NULL,
    content_ref  TEXT NOT NULL DEFAULT '',
    sha256       TEXT NOT NULL DEFAULT '',
    bytes        INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL
);

-- --------------------------------------------------------------------- #
-- 证据层（A 级核心）
-- --------------------------------------------------------------------- #
CREATE TABLE IF NOT EXISTS source_document (
    id              TEXT PRIMARY KEY,
    url             TEXT NOT NULL DEFAULT '',
    url_canonical   TEXT NOT NULL,
    mid             TEXT NOT NULL DEFAULT '',
    host            TEXT NOT NULL DEFAULT '',
    kind            TEXT NOT NULL DEFAULT 'weibo_post',
    author          TEXT NOT NULL DEFAULT '',
    published_at    TEXT NOT NULL DEFAULT '',
    content_hash    TEXT NOT NULL,
    bytes           INTEGER NOT NULL DEFAULT 0,
    text_inline     TEXT,
    text_ref        TEXT,
    is_official     INTEGER NOT NULL DEFAULT 0,
    detail_verified INTEGER NOT NULL DEFAULT 0,
    first_seen_at   TEXT NOT NULL,
    UNIQUE (url_canonical, content_hash)
);
CREATE INDEX IF NOT EXISTS ix_doc_mid ON source_document(mid);
CREATE INDEX IF NOT EXISTS ix_doc_host ON source_document(host);

CREATE TABLE IF NOT EXISTS document_fetch (
    id           TEXT PRIMARY KEY,
    run_id       TEXT NOT NULL REFERENCES workflow_run(id) ON DELETE CASCADE,
    stage_run_id TEXT,
    document_id  TEXT NOT NULL REFERENCES source_document(id),
    fetched_at   TEXT NOT NULL,
    http_status  INTEGER NOT NULL DEFAULT 0,
    source_url   TEXT NOT NULL DEFAULT '',
    UNIQUE (run_id, document_id)
);
CREATE INDEX IF NOT EXISTS ix_fetch_doc ON document_fetch(document_id);

-- --------------------------------------------------------------------- #
-- 语义层（B 级）
-- --------------------------------------------------------------------- #
CREATE TABLE IF NOT EXISTS event (
    id              TEXT PRIMARY KEY,
    stage_run_id    TEXT NOT NULL REFERENCES stage_run(id) ON DELETE CASCADE,
    seq             INTEGER NOT NULL DEFAULT 0,
    event_key       TEXT NOT NULL DEFAULT '',
    name            TEXT NOT NULL,
    summary         TEXT NOT NULL DEFAULT '',
    heat            TEXT NOT NULL DEFAULT 'unknown',
    heat_basis      TEXT NOT NULL DEFAULT '',
    judgement       TEXT NOT NULL DEFAULT '',
    keywords_json   TEXT NOT NULL DEFAULT '[]',
    merged_from_json TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS ix_event_stage ON event(stage_run_id, seq);
CREATE INDEX IF NOT EXISTS ix_event_key   ON event(event_key);

CREATE TABLE IF NOT EXISTS event_evidence (
    event_id        TEXT NOT NULL REFERENCES event(id) ON DELETE CASCADE,
    document_id     TEXT NOT NULL REFERENCES source_document(id),
    role            TEXT NOT NULL DEFAULT 'evidence',
    verified        INTEGER NOT NULL DEFAULT 0,
    note            TEXT NOT NULL DEFAULT '',
    -- 报告里引用的那段摘录。全文在 source_document，这里保留"当时引了哪段"
    content_excerpt TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (event_id, document_id, role)
);

CREATE TABLE IF NOT EXISTS fact (
    id           TEXT PRIMARY KEY,
    event_id     TEXT NOT NULL REFERENCES event(id) ON DELETE CASCADE,
    seq          INTEGER NOT NULL DEFAULT 0,
    text         TEXT NOT NULL,
    kind         TEXT NOT NULL DEFAULT 'claim',
    source_kind  TEXT NOT NULL DEFAULT 'unknown',
    inferred_link INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_fact_event ON fact(event_id, seq);

CREATE TABLE IF NOT EXISTS fact_support (
    fact_id     TEXT NOT NULL REFERENCES fact(id) ON DELETE CASCADE,
    document_id TEXT NOT NULL REFERENCES source_document(id),
    link_kind   TEXT NOT NULL DEFAULT 'annotated',
    PRIMARY KEY (fact_id, document_id, link_kind)
);

CREATE TABLE IF NOT EXISTS event_statement (
    id       TEXT PRIMARY KEY,
    event_id TEXT NOT NULL REFERENCES event(id) ON DELETE CASCADE,
    kind     TEXT NOT NULL,
    seq      INTEGER NOT NULL DEFAULT 0,
    text     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_stmt_event ON event_statement(event_id, kind, seq);

CREATE TABLE IF NOT EXISTS gap_record (
    id           TEXT PRIMARY KEY,
    stage_run_id TEXT NOT NULL REFERENCES stage_run(id) ON DELETE CASCADE,
    round        INTEGER NOT NULL DEFAULT 0,
    kind         TEXT NOT NULL DEFAULT '',
    target       TEXT NOT NULL DEFAULT '',
    detail       TEXT NOT NULL DEFAULT '',
    action       TEXT NOT NULL DEFAULT '',
    resolved     INTEGER
);
CREATE INDEX IF NOT EXISTS ix_gap_stage ON gap_record(stage_run_id);

CREATE TABLE IF NOT EXISTS limitation (
    id           TEXT PRIMARY KEY,
    stage_run_id TEXT NOT NULL REFERENCES stage_run(id) ON DELETE CASCADE,
    step         TEXT NOT NULL,
    detail       TEXT NOT NULL,
    hit_count    INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS search_record (
    id           TEXT PRIMARY KEY,
    stage_run_id TEXT NOT NULL REFERENCES stage_run(id) ON DELETE CASCADE,
    seq          INTEGER NOT NULL DEFAULT 0,
    round        INTEGER NOT NULL DEFAULT 1,
    keyword      TEXT NOT NULL,
    url          TEXT NOT NULL DEFAULT '',
    posts_read   INTEGER NOT NULL DEFAULT 0,
    new_posts    INTEGER NOT NULL DEFAULT 0,
    limitation   TEXT NOT NULL DEFAULT ''
);

-- --------------------------------------------------------------------- #
-- 核查层
-- --------------------------------------------------------------------- #
CREATE TABLE IF NOT EXISTS claim_check (
    id                    TEXT PRIMARY KEY,
    stage_run_id          TEXT NOT NULL REFERENCES stage_run(id) ON DELETE CASCADE,
    seq                   INTEGER NOT NULL DEFAULT 0,
    claim_key             TEXT NOT NULL DEFAULT '',
    claim                 TEXT NOT NULL,
    report_line           TEXT NOT NULL DEFAULT '',
    verdict               TEXT NOT NULL DEFAULT '待核查',
    final_verdict         TEXT,
    confidence            TEXT NOT NULL DEFAULT '中',
    analysis              TEXT NOT NULL DEFAULT '',
    citation_check        TEXT NOT NULL DEFAULT 'not_run',
    semantic_note         TEXT NOT NULL DEFAULT '',
    cross_conflict        INTEGER NOT NULL DEFAULT 0,
    cross_conflict_reason TEXT NOT NULL DEFAULT '',
    independent_groups    INTEGER NOT NULL DEFAULT 0,
    downgraded            INTEGER NOT NULL DEFAULT 0,
    semantic_revised      INTEGER NOT NULL DEFAULT 0,
    handoff_fact_id       TEXT
);
CREATE INDEX IF NOT EXISTS ix_claim_stage ON claim_check(stage_run_id, seq);

CREATE TABLE IF NOT EXISTS factcheck_source (
    id                 TEXT PRIMARY KEY,
    stage_run_id       TEXT NOT NULL REFERENCES stage_run(id) ON DELETE CASCADE,
    source_key         TEXT NOT NULL,
    kind               TEXT NOT NULL,
    publisher          TEXT NOT NULL DEFAULT '',
    title              TEXT NOT NULL DEFAULT '',
    url                TEXT NOT NULL DEFAULT '',
    published_at       TEXT NOT NULL DEFAULT '',
    quote              TEXT NOT NULL DEFAULT '',
    independence_group TEXT NOT NULL DEFAULT '',
    note               TEXT NOT NULL DEFAULT '',
    document_id        TEXT
);
CREATE INDEX IF NOT EXISTS ix_fcsrc_stage ON factcheck_source(stage_run_id);
CREATE INDEX IF NOT EXISTS ix_fcsrc_key   ON factcheck_source(stage_run_id, source_key);

-- 每次机检一行，append-only：同一来源复核两次就有两行，可以看校验结果怎么变的
CREATE TABLE IF NOT EXISTS source_check (
    id          TEXT PRIMARY KEY,
    source_id   TEXT NOT NULL REFERENCES factcheck_source(id) ON DELETE CASCADE,
    checked_at  TEXT NOT NULL,
    http_status INTEGER NOT NULL DEFAULT 0,
    final_url   TEXT NOT NULL DEFAULT '',
    reachable   INTEGER NOT NULL DEFAULT 0,
    quote_found INTEGER,
    page_title  TEXT NOT NULL DEFAULT '',
    note        TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_check_src ON source_check(source_id, checked_at);

CREATE TABLE IF NOT EXISTS citation (
    id             TEXT PRIMARY KEY,
    claim_id       TEXT NOT NULL REFERENCES claim_check(id) ON DELETE CASCADE,
    source_id      TEXT NOT NULL REFERENCES factcheck_source(id),
    side           TEXT NOT NULL DEFAULT 'supporting',
    point          TEXT NOT NULL DEFAULT '',
    support        TEXT NOT NULL DEFAULT 'unknown',
    support_reason TEXT NOT NULL DEFAULT ''
);

-- --------------------------------------------------------------------- #
-- 留痕层（A 级）
-- --------------------------------------------------------------------- #
CREATE TABLE IF NOT EXISTS llm_call (
    id             TEXT PRIMARY KEY,
    run_id         TEXT REFERENCES workflow_run(id) ON DELETE CASCADE,
    stage_run_id   TEXT,
    purpose        TEXT NOT NULL DEFAULT '',
    attempt        INTEGER NOT NULL DEFAULT 1,
    ok             INTEGER NOT NULL DEFAULT 0,
    model          TEXT NOT NULL DEFAULT '',
    base_url       TEXT NOT NULL DEFAULT '',
    prompt_digest  TEXT NOT NULL DEFAULT '',
    request_chars  INTEGER NOT NULL DEFAULT 0,
    response_chars INTEGER NOT NULL DEFAULT 0,
    duration_ms    INTEGER NOT NULL DEFAULT 0,
    error          TEXT NOT NULL DEFAULT '',
    prompt_ref     TEXT NOT NULL DEFAULT '',
    response_ref   TEXT NOT NULL DEFAULT '',
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_llm_run ON llm_call(run_id, created_at);

CREATE TABLE IF NOT EXISTS trace_event (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       TEXT REFERENCES workflow_run(id) ON DELETE CASCADE,
    stage_run_id TEXT,
    seq          INTEGER NOT NULL DEFAULT 0,
    ts           TEXT NOT NULL,
    step         TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS ix_trace_run ON trace_event(run_id, seq);

-- --------------------------------------------------------------------- #
-- 度量（C 级，可随时重算）
-- --------------------------------------------------------------------- #
CREATE TABLE IF NOT EXISTS run_metric (
    run_id TEXT NOT NULL REFERENCES workflow_run(id) ON DELETE CASCADE,
    name   TEXT NOT NULL,
    value  REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (run_id, name)
);

INSERT OR IGNORE INTO schema_version (version, applied_at, note)
VALUES (1, datetime('now'), 'init: run/stage/document/event/fact/claim/llm/trace');
