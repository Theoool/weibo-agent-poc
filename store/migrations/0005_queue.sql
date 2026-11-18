-- M4：任务队列与 worker 心跳。
--
-- 为什么不直接轮询 `stage_run.status='queued'`（设计稿 §4.4 的写法）：
--   1. 排队不是一个"阶段尝试"。没被 worker 领走的任务如果也写一行 stage_run，
--      阶段列表里会出现一次从未发生的尝试，用户会以为跑过；
--   2. 租约（lease）是队列机制，不属于历史记录。它每秒都在变，混进 stage_run
--      会污染"这个阶段什么时候开始、跑了多久"这类审计字段；
--   3. 取消需要作用于"还没开始"的任务，那种状态在 stage_run 里没有位置。
-- 所以队列单独一张表，stage_run 仍只记录**真正执行过**的尝试。
CREATE TABLE IF NOT EXISTS queue_job (
    id               TEXT PRIMARY KEY,
    run_id           TEXT NOT NULL REFERENCES workflow_run(id) ON DELETE CASCADE,
    -- investigate | handoff | factcheck
    stage            TEXT NOT NULL,
    -- queued | running | succeeded | failed | cancelled
    status           TEXT NOT NULL DEFAULT 'queued',
    priority         INTEGER NOT NULL DEFAULT 0,
    -- 每次被领走就 +1。租约过期回收也算一次，否则崩了又崩会无限重试
    attempts         INTEGER NOT NULL DEFAULT 0,
    max_attempts     INTEGER NOT NULL DEFAULT 3,
    payload_json     TEXT NOT NULL DEFAULT '{}',
    -- 真正开始执行时才创建 stage_run，这里回填它的 id，回收时用来把那次尝试标失败
    stage_run_id     TEXT,
    worker_id        TEXT NOT NULL DEFAULT '',
    -- 租约到期时间。worker 崩了之后，别的 worker 只能等它过期才能接管
    lease_until      TEXT,
    -- 最早可被领取的时间（失败退避用；默认等于 created_at）
    available_at     TEXT NOT NULL,
    -- API 置 1，worker 在轮次边界看到就停。不直接改 status：
    -- 正在跑的进程还在写文件与库，由它自己收尾才不会留下半截状态
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT NOT NULL,
    started_at       TEXT,
    finished_at      TEXT,
    error            TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_queue_claim
    ON queue_job(status, available_at, priority, created_at);

-- 同一 run 的同一阶段最多只有一个活跃任务：重复 POST /retry 不会跑两遍
CREATE UNIQUE INDEX IF NOT EXISTS uq_queue_active
    ON queue_job(run_id, stage) WHERE status IN ('queued', 'running');

-- worker 心跳。没有它，"任务一直 queued" 和 "worker 挂了" 在 API 里长得一模一样，
-- 而这是队列系统最常见的故障。
CREATE TABLE IF NOT EXISTS worker_heartbeat (
    worker_id    TEXT PRIMARY KEY,
    host         TEXT NOT NULL DEFAULT '',
    pid          INTEGER NOT NULL DEFAULT 0,
    started_at   TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    jobs_done    INTEGER NOT NULL DEFAULT 0,
    last_error   TEXT NOT NULL DEFAULT ''
);
