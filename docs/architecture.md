# 整体架构设计：工作流留存 + 数据分级 + FastAPI

> 状态：设计已确认，M1~M4 已实施（持久层 + 轨迹入库 + 查询 API + 队列与 worker）。
> 范围：把当前「文件产物 + JSONL 轨迹」的 POC，改造成「以 run 为聚合根、证据可审计、API 可查询」的服务。
>
> 实施进度：
> - [x] **M1** SQLAlchemy 模型、迁移（v1~v3）、`RunStore`、blobs、历史产物导入、CLI 双写
> - [x] **M2** `TraceSink` 协议 + `DbSink` + `llm_call` / `trace_event` 落库（迁移 v4）
> - [x] **M3** FastAPI 只读层（`api/`，同步查询 + 现渲染报告）
> - [x] **M4** `POST /runs` + worker 队列（迁移 v5：`queue_job` / `worker_heartbeat`）
> - [ ] **M5** 分级落地（备份、清理、索引）
> - [ ] **M6** 文档收尾 + 回归
>
> M1 实施中修正了设计稿三处：`workflow_run` 增加 `report_at`（否则读回后报告时间漂移）、
> `event_evidence` 增加 `seq`（否则证据顺序会乱，10 份历史产物里 8 份受影响）、
> 迁移版本号改为由执行器记账（靠 SQL 文件自觉写 `schema_version` 会重复重放）。
>
> M2 又修正一处：设计稿 §3.5 只给了 `prompt_ref` / `response_ref`，但没有内联列。
> 按 §2.4「< 4 KB 存 TEXT」的规则，ping 和短 JSON 的题面都不足 4 KB，指针为空、
> 内容无处可放 —— 恰好丢掉最短也最常用的那批记录。迁移 v4 补 `prompt_inline` /
> `response_inline`，语义与 `source_document.text_inline` 一致。
>
> M3 再修正两处，都是"字段设计看着够、实际查不通"的问题：
> - **URL → 文档的匹配必须按 mid 归一。** 帖子正文按 `weibo:<mid>` 归一，核查清单里
>   登记的却是 `https://m.weibo.cn/detail/<mid>`。只按 canonical 匹配，`claims →
>   原文` 会永远断链（17 条来源 0 条命中）。`_document_id_for_url()` 现在先试 mid。
> - **阶段顺序不能只按 `started_at`。** 四个阶段常在同一秒内建完，时间戳相等后
>   SQLite 的返回顺序不确定，报告与 API 里的阶段顺序会跳。加 `rowid` 兜底。
>
> M4 修正一处（§4.4）：设计稿写的是「轮询 `stage_run where status='queued'`」，
> 但排队不是一个阶段尝试。没被领走的任务若也写 `stage_run`，阶段列表里会多出一次
> 从未发生的尝试；租约每秒在变，混进 `stage_run` 会污染"这个阶段跑了多久"；
> 取消需要作用于"还没开始"的任务，那种状态在 `stage_run` 里没有位置。
> 迁移 v5 因此单独建 `queue_job`（含 `attempts` / `lease_until` / `cancel_requested`），
> `stage_run` 仍只记录**真正执行过**的尝试。同一批迁移顺带加了 `worker_heartbeat`：
> 没有它，"任务一直 queued"和"worker 挂了"在 API 里长得一模一样。

---

## 1. 先看清要解决的问题

不是「给 POC 加个数据库」，而是当前有五个具体的、已经能在产物里看到的问题：

| # | 现象（代码事实） | 后果 |
|---|---|---|
| 1 | 四阶段靠文件名串接：`investigation.json` → `claims_handoff.json` → `claims_handoff_factcheck.json` 全是固定名 | 第二次运行覆盖第一次。带时间戳的副本只有 `investigation_*.json`，claims / factcheck 没有对应副本 → **这份核查报告核的是哪次调查，无法回答** |
| 2 | `Evidence.content` 是截断文本：`digest_posts` 截 160 字，handoff 取 `quote` 前 60 字 | 事实核查的「引文命中」实际核的是**截断片段**，不是原文。证据不可审计 |
| 3 | `llm_call` 只留 `response_preview`（120 字）+ 题面前 800 字 | 模型原始输出不可复现。`prompt_digest` 只能证明「输入没变」，不能证明「当时输出了什么」 |
| 4 | 同一条帖子在 `day1_APEC.json`、`investigation.json`、`claims_handoff.json`、`claims_handoff_factcheck.json` 各存一份截断副本 | 冗余且不一致；无法判断「这条帖子前后被改过没有」 |
| 5 | `event` 没有稳定标识，事件名每轮归纳都可能改写 | `_resolve_pending()` 里缺口永远记成「无法判定（事件名变化）」；跨 run 更无法追踪同一事件的演变 |

结论：**缺的不是存储，是「一次工作流」这个实体，以及「证据原文」这一层。**

---

## 2. 核心设计主张

### 2.1 run 是聚合根

一次工作流 = `workflow_run`，四阶段是它的子实体 `stage_run`。所有数据（证据、事件、事实、核查结论、模型调用、缺口决策）都挂在 run 下。

```
workflow_run ──┬── stage_run(collect)     ── document_fetch
               ├── stage_run(investigate) ── event ── fact ── fact_support
               ├── stage_run(handoff)     ── claim_check（初始态）
               └── stage_run(factcheck)   ── claim_check ── citation ── source_check
```

`handoff` 的 `input_stage_id` 指向 `investigate` 的 stage_run，`factcheck` 指向 `handoff`。链条由外键保证，不再靠文件名约定。

### 2.2 领域层不碰数据库

现有 `Investigator`、`Controller`、`FactCheckRunner` 全是纯逻辑 + Pydantic 模型，**这个结构要保住**。数据库只出现在两个边界：

1. **过程中**：`Tracer` 抽象成 `TraceSink` 协议，实现 `JsonlSink`（现状）与 `DbSink`（新增）。`LLMClient` 已经 attach 了 tracer，无需改它的调用方式。
2. **阶段结束**：`RunStore.save_investigation(inv, stage_run_id)` 在一个事务里写 event / fact / evidence / gap / limitation。

好处：`investigator.py` 改动极小，99 个单测继续有效，`to_markdown()` 渲染逻辑完全复用。

### 2.3 数据分级：判据只有一条

> **只靠 A 级数据 + 当前代码，能不能重新算出这份报告？**
> 能 → B/C 级；不能 → A 级。

这条判据可执行、可争论，不会变成「我觉得这个重要」。

| 级别 | 定义 | 具体数据 | 写入机制 | 可变性 | 备份 | 清理 |
|---|---|---|---|---|---|---|
| **A 不可再生** | 丢了就永久丢失，或重新获取后已不是当时那个东西 | ① `source_document` 正文**全文快照** + `content_hash` + 抓取时刻 + HTTP 状态<br>② `llm_call` 的 prompt / response **全文**<br>③ `source_check` 每次机检结果（含 final_url、登录墙识别）<br>④ `claim_check` 的 `verdict` / `final_verdict` / 降级原因<br>⑤ `gap_record`（为什么停、补搜了什么）<br>⑥ `trace_event` 原始记录 | 发生即写，事务提交后才继续 | **append-only**，正文与结论禁止 UPDATE | 必进备份，保留 ≥90 天 | 永不自动删 |
| **B 可重算但贵** | 能由 A + 代码重新推导，但要重新调模型、结果不保证一致 | `event` / `fact` / `opinion` / `speculation` 的结构；`claim_check` 的展示字段；渲染出的报告 md/json；`search_record` | 阶段结束一次事务 | 同一 stage_run 内可整体重写 | 随 A 一起 | 可归档 |
| **C 可丢弃** | 纯运行态 / 聚合量 / 缓存 | `run_metric` 数字；候选词池；滚动分页状态；页面 HTML（只留 hash + 长度）；trace 的 `summarize()` 结果 | 异步 / 收尾 | 随时重算 | 不备份 | TTL 30 天可清 |

两个容易划错的地方，特别说明：

- **`trace` 的记录本身是 A 级，`trace` 的统计是 C 级。** 耗时汇总可以重算，但「第 3 轮判了 2 个缺口、模型在 keyword_plan_r2 返回了什么」不能重算。
- **`run_metric` 是 C 级，但报告头部的数字不能只从 metric 读。** 否则 C 级被清掉后，历史报告的数字会变成 0。渲染时从 B 级现算，metric 只做加速。

### 2.4 大文本不塞进数据库

帖子正文、prompt 全文、网页 HTML 体积可达 MB 级。策略：

- **< 4 KB**：直接存 `TEXT` 列（`text_inline`）
- **≥ 4 KB**：写内容寻址目录 `artifacts/<sha256[:2]>/<sha256>`，DB 只存 `content_hash` + `bytes`

内容寻址的额外收益：**同一条帖子抓 10 次只存一份**，跨 run 自动去重。这也是解决问题 #4 的手段。

---

## 3. 数据模型

### 3.1 编排层

```sql
workflow_run
  id            TEXT PK        -- run_20261118_2252_ab12
  topic         TEXT NOT NULL
  status        TEXT NOT NULL  -- queued|running|succeeded|failed|cancelled
  trigger       TEXT NOT NULL  -- cli|api|import
  config_json   TEXT NOT NULL  -- Budget / 模型名 / base_url / 参数快照（复现前提）
  app_version   TEXT           -- git sha
  parent_run_id TEXT           -- 重跑或派生关系
  started_at, finished_at, error

stage_run
  id, run_id FK, stage, attempt=1, status
  input_stage_id FK            -- 上游 stage，串成链
  started_at, finished_at, error
  UNIQUE(run_id, stage, attempt)

artifact                        -- 渲染产物（B 级，可重算）
  id, run_id, stage_run_id, kind(md|json), content_ref, bytes, sha256, created_at
```

`config_json` 是 A 级的实质内容之一：没有它，同一份报告无法判断「当时用的是 `--min-evidence 3` 还是 5」。

### 3.2 证据层（A 级核心）

```sql
source_document                 -- 一条内容，按 sha256 去重，永不 UPDATE
  id, url, url_canonical, mid, host, kind
  author, published_at
  content_hash, bytes, text_inline, text_ref
  is_official, detail_verified

document_fetch                  -- 一次抓取事件（哪个 run、什么时候、HTTP 多少）
  id, run_id FK, stage_run_id FK, document_id FK
  fetched_at, http_status, source_url
  UNIQUE(run_id, document_id)
```

拆成两张表是为了回答一个现在答不了的问题：**这条帖子的内容变过没有？** 同一 `url_canonical` 出现多行 `source_document`（不同 hash）即说明内容被改过。

### 3.3 语义层（B 级）

```sql
event            id, stage_run_id, seq, event_key, name, summary, heat, heat_basis,
                 judgement, merged_from_json
event_evidence   event_id, document_id, role(evidence|official), verified, note
fact             id, event_id, seq, text, kind(claim|meta), source_kind, inferred_link
fact_support     fact_id, document_id, link_kind(annotated|inferred)
event_statement  id, event_id, kind(opinion|speculation), seq, text
gap_record       id, stage_run_id, round, kind, target, detail, action, resolved
limitation       id, stage_run_id, step, detail, hit_count
search_record    id, stage_run_id, round, keyword, url, posts_read, new_posts, limitation
```

`event_key` 是新加的：`normalize(name)` 的稳定标识。它顺带修掉一个已有缺陷——`_resolve_pending()` 目前因为事件改名而只能记「无法判定」，有了 `event_key` 就能判断「是不是同一个事件换了名字」，而不是放弃判定。

### 3.4 核查层

```sql
claim_check      id, stage_run_id, seq, claim, report_line
                 verdict, final_verdict, confidence, analysis
                 citation_check, semantic_note, cross_conflict, cross_conflict_reason
                 independent_groups, downgraded, semantic_revised
                 handoff_fact_id          -- 回溯到调查层的 fact，替代 C1/C2 这种序号
factcheck_source id, stage_run_id, source_key, kind, publisher, title, url,
                 published_at, independence_group, document_id
source_check     id, source_id, checked_at, http_status, final_url, reachable,
                 quote_found, page_title, note          -- 每次机检一行，append-only
citation         id, claim_id, source_id, side(supporting|counter),
                 point, support, support_reason
```

`handoff_fact_id` 解决了现在 `C1`–`C18` 只是序号的脆弱性：重跑 handoff 后编号全变，无法对上。

### 3.5 留痕层

```sql
llm_call      id, run_id, stage_run_id, purpose, attempt, ok
              model, base_url, prompt_digest
              request_chars, response_chars, duration_ms, error
              prompt_ref, response_ref        -- 全文，走 CAS
              created_at

trace_event   id INTEGER PK AUTOINCREMENT, run_id, stage_run_id,
              seq, ts, step, payload_json
```

`prompt_ref` / `response_ref` 是相对现有 `response_preview` 的关键升级：前者能复现，后者只能猜。

实施（M2）时的三个取舍：

- **JSONL 不存全文，数据库存。** 轨迹文件要能提交、能 diff，被几 MB 提示词撑爆就没人看了；
  全文进 `llm_call`，按 §2.4 内联或走 CAS。两边记录的是同一件事，只是粒度不同。
- **逐条提交，不攒批。** 轨迹要求「发生即写」：进程被杀时最想看的恰好是最后几步。
  代价是一次运行多几十次小事务，SQLite WAL 下可以接受；攒批省下的那点时间，
  换来的是「出事前最后 20 条永久丢失」。
- **写库失败不中断调查。** `FanoutSink` 隔离异常并计数，收尾时明确告警。
  静默吞掉比没有轨迹更糟：事后会以为这一段本来就没有记录。

### 3.6 度量（C 级）

```sql
run_metric   run_id, name, value, PRIMARY KEY(run_id, name)
```

### 3.7 最小可运行子集

20 张表不必一次建全。第一版只需 12 张就能跑通全链路：

`workflow_run` / `stage_run` / `source_document` / `document_fetch` / `event` / `fact` / `fact_support` / `claim_check` / `factcheck_source` / `llm_call` / `trace_event` / `run_metric`

其余（`event_evidence`、`event_statement`、`gap_record`、`limitation`、`search_record`、`source_check`、`citation`、`artifact`）按里程碑补齐。这也是验证分级是否合理的机会：**如果某张表一直没人查，说明它被分高了。**

---

## 4. FastAPI 设计

### 4.1 为什么必须异步

实测一次调查 3–6 分钟（117 条帖子 / 3 轮补搜）。同步 HTTP 会超时，客户端重试会造出重复 run。所以：

```
POST /api/runs  →  202 {run_id, status:"queued", links:{...}}   （立即返回）
GET  /api/runs/{id}  →  轮询阶段状态
```

### 4.2 接口

| 方法 | 路径 | 状态 | 说明 |
|---|---|---|---|
| POST | `/api/runs` | ✅ | 创建并排队一次工作流（202 + 轮询） |
| GET | `/api/runs` | ✅ | 列表（topic / 分页过滤） |
| GET | `/api/runs/{id}` | ✅ | 概览：阶段状态 + 指标 + 停止原因 + `provenance` + `queue` |
| GET | `/api/runs/{id}/stages` | ✅ | 四段状态与耗时 |
| POST | `/api/runs/{id}/retry` | ✅ | 从失败阶段重跑（新 job，旧 attempt 保留） |
| POST | `/api/runs/{id}/cancel` | ✅ | 排队中立即作废；运行中在轮次边界停 |
| GET | `/api/queue` | ✅ | 积压、在线 worker、最近任务（"为什么一直是 queued"） |
| GET | `/api/runs/{id}/report?format=md\|json` | ✅ | 产物，**从 B 级现渲染**（`X-Report-Source` 说明来源） |
| GET | `/api/runs/{id}/trace` | ✅ | trace 事件（按 seq 有序，可分页 / 按 step 过滤） |
| GET | `/api/runs/{id}/llm-calls` | ✅ | 模型调用列表；默认不含全文，`include_text=true` 才返回 |
| GET | `/api/llm-calls/{call_id}` | ✅ | 单次调用的题面与输出全文 |
| GET | `/api/runs/{id}/gaps` | ✅ | 缺口决策链（含 `resolved` 三态统计） |
| GET | `/api/runs/{id}/claims` | ✅ | 结论 → 引用来源 → `document_id`（M3 验收用） |
| GET | `/api/runs/{id}/documents` | ✅ | 本次运行抓到的证据元数据（分页） |
| GET | `/api/documents/{id}` | ✅ | 证据快照（正文 + hash + 抓取时刻 + `excerpt_only`） |
| GET | `/api/documents/{id}/fetches` | ✅ | 同一 URL 的抓取次数、历史版本与 `content_changed` |
| GET | `/api/health` · `/api/ready` | ✅ | 存活 / 依赖检查（DB、CAS、模型 Key、Chrome profile） |

请求体：

```json
{
  "topic": "APEC",
  "seed_keywords": ["深圳APEC"],
  "stages": ["collect", "investigate", "handoff", "factcheck"],
  "config": {"rounds": 3, "max_searches": 8, "min_evidence": 3, "semantic": true}
}
```

### 4.3 状态机

```
queued ──→ running ──→ succeeded
              │
              └──→ failed ──(retry, attempt+1)──→ running
              └──→ cancelled
```

一个 stage 失败不终止整个 run：`factcheck` 失败时 `investigate` 的产物仍然有效，run 标 `succeeded` 但带 `partial=true`。**这比全量失败更符合实际**——现在 `--factcheck` 挂了，调查报告已经写出来了。

### 4.4 API 与 Worker 必须分离

这是本设计里唯一有争议的决策，理由是硬的：

- Playwright 同步 API + 单个专用 Chrome profile → **采集阶段天然只能串行**
- 若放在 uvicorn 进程内：`--reload` 会杀掉任务；进程重启丢失在跑的任务；异步事件循环被同步 Chrome 阻塞
- SQLite 多进程写会 `database is locked`

方案：独立 `python -m worker` 进程从 `queue_job` 领任务（不是轮询 `stage_run`，
原因见文档开头 M4 修正）。

- SQLite 下靠「单写者」保证不冲突：worker 是唯一写重数据的进程，API 只做轻量写（建 run、入队、改状态）
- WAL + `busy_timeout=5000`
- 领取是「带条件 UPDATE + 看 rowcount」：两个 worker 读到同一行是允许的，
  第二个 UPDATE 会因为 `status='queued'` 不成立而落空，然后重试下一行
- 租约 30 分钟 + 后台线程续租：worker 被杀之后，任务在租约过期时被回收重排；
  **业务异常不自动重试**（配置错了重跑一百次也一样），只有"进程没了"才重试
- 每个阶段一个 job，成功后排队下一阶段：这样 `POST /retry` 能只重跑失败的那一步，
  而不是把已经成功的调查再跑一遍
- 将来并发上来 → 换 Postgres，仓储层代码不动

代价两条进程一起起。用 `uv run python -m api` + `uv run python -m worker` 两个命令，或一个 `scripts/dev.sh`。

### 4.5 M3 落地时的四个决定

| 决定 | 做法 | 理由 |
|---|---|---|
| 读接口拿不到 session | 依赖只注入 `RunStore` | 一旦路由能拿到 session，就会有人在那里写库，只读层名存实亡 |
| 报告现渲染 | `load_investigation()` → `to_markdown()`，响应头 `X-Report-Source` 标明来源 | 直接吐文件的话，文件与库不一致时没人发现；现渲染顺便校验字段够不够 |
| `llm-calls` 列表默认无全文 | `include_text=false`，全文走 `/llm-calls/{id}` | 一次 `event_build` 题面 17KB，20 条就是几百 KB，列表会被拖死 |
| 新增 `/claims` 端点 | 结论 → 引用 → `document_id` | 原设计稿没有，但「这份核查的依据是哪几条帖子原文」必须靠它回答 |

`provenance` 对历史导入的 run 会返回 `note`："无法确定核对的是哪一次调查"。
这比编一个上游 run_id 诚实——当年确实没记。

### 4.6 M4 落地时的四个决定

| 决定 | 做法 | 理由 |
|---|---|---|
| 队列单独一张表 | `queue_job`，不往 `stage_run` 加 `status='queued'` | 见开头 M4 修正：排队不是一次阶段尝试，租约和取消也没有地方放 |
| worker 单并发 | 一次只领一个任务，不设线程池 | 专用 Chrome profile 同一时刻只能被一个进程占用；多开会互相抢锁，失败得莫名其妙 |
| 业务异常不自动重试 | 处理器抛异常 → job `failed`，等人工 `retry`；只有租约过期（进程没了）才重排 | 配置错了重跑一百次也是错；而进程被杀是环境问题，重试有意义 |
| 抽取 `workflow/` | 阶段执行器从 `main.py` 移出，命令行与 worker 共用 | 两份实现迟早会分岔，而"API 跑出来的报告和命令行不一样"是最难查的那类 bug |
| 每个 run 独占输出目录 | `output/runs/<run_id>/` | 命令行时代所有人都写 `output/investigation.json`，队列里同时排着多条调查就会互相覆盖 |

`cancel` 的语义值得单独说：**排队中的立即作废，运行中的只打标记**。正在跑的进程
还在写文件与库，由 API 单方面改状态会留下"cancelled 但文件写了一半"的记录。
worker 在每轮补搜的边界检查一次标记——不做到"中断正在翻的页"是有意的：
那会把浏览器的半截状态留给下一次运行，比多跑 30 秒代价更大。

---

## 5. 与现有代码的接缝

| 现有文件 | 改动 | 风险 |
|---|---|---|
| `agent/schemas.py` | 不改。领域模型继续用 Pydantic | 无 |
| `agent/investigator.py` | 只改 `Tracer` 的类型为 `TraceSink` 协议 | 低；`tracer.log` 签名不变 |
| `agent/trace.py` | 抽出 `TraceSink` 协议，现有 `Tracer` 改名 `JsonlSink` | 低 |
| `agent/controller.py` | 不改 | 无 |
| `factcheck/runner.py` | 不改。入库在返回后做 | 无 |
| `main.py` | 增加 `--db` 开关，阶段结束调 `RunStore` | 中；需保证旧流程照常 |
| 新增 `store/` | models / migrations / repositories / blobs | 新增 |
| 新增 `api/` `worker.py` | FastAPI + 队列消费者 | 新增 |

### 迁移策略：双写，文件降级为导出视图

不从第一天就砍掉文件产物。理由：GitHub 上的可读性、99 个单测、回归对比都依赖它。

1. **M1 起**：DB 与文件同时写，文件仍是既有的 `output/*.json|md`
2. **M3**：API 只读层上线，用**历史真实产物**导入验证字段是否够用
3. **M5**：确认 API 能回答报告里的每个问题后，文件改为按需 `export`

历史产物导入：`python main.py --import-output output/` 把现有 8 份 `investigation_*.json`、2 份 factcheck 补成历史 run。旧产物没有全文快照，导入时标 `quality=degraded`，不假装它们和新的等价。

---

## 6. 目录结构变化

```
weibo-agent-poc/
├─ main.py                 # CLI（四阶段编排改为调 workflow/，行为不变）
├─ worker.py               # 队列消费者（M4）：领任务、建阶段、跑、推进下一阶段
├─ workflow/               # 阶段执行器（命令行与 worker 共用）
│  ├─ context.py           # RunContext：run_id / stage_id / 输出目录 / 取消钩子
│  ├─ investigate.py       # 调查 → 文件产物 + 入库
│  ├─ handoff.py           # 调查产物 → 核查清单（队列里从库里读）
│  ├─ factcheck.py         # 核查（输入优先取 claims_json artifact）
│  └─ sinks.py             # JSONL + 数据库双写
├─ api/                    # HTTP 层（M3 查询 + M4 创建/重跑/取消）
│  ├─ app.py               # app 工厂 + lifespan（建库、关库）
│  ├─ schemas.py           # 写接口请求体与预算上限
│  ├─ deps.py              # get_store / require_run（只暴露 RunStore，不暴露 session）
│  ├─ __main__.py          # `python -m api --port 8000`
│  └─ routers/{runs,queue,documents,reports,trace,health}.py
├─ store/                  # 持久层（新）
│  ├─ models.py            # SQLAlchemy 2.0 模型
│  ├─ repositories.py      # RunStore：编排 / 证据 / 语义 / 队列
│  ├─ blobs.py             # 内容寻址读写
│  ├─ db_sink.py           # DbSink：轨迹与模型调用全文落库
│  └─ migrations/          # 顺序 SQL + schema_version（v1~v5）
├─ agent/ browser/ factcheck/ tests/   # 领域层基本不动
├─ artifacts/              # CAS 目录（gitignore）
├─ output/runs/<run_id>/   # 每次运行独占的输出目录（gitignore）
└─ data/workflow.db        # SQLite（gitignore）
```

依赖新增：`fastapi`、`uvicorn`、`sqlalchemy>=2.0`。（不引 Alembic：schema 变动不频繁，`migrations/` 下顺序 SQL + `schema_version` 表足够，少一个依赖。）

---

## 7. 实施里程碑

| 阶段 | 内容 | 可验证的成果 |
|---|---|---|
| **M0** | 本文档 + 表设计评审 | 你确认分级与选型 |
| **M1** | SQLAlchemy 模型 + 迁移 + `RunStore` + 历史产物导入 + 双写 | `--agent` 跑一次，DB 里能查到完整 run；旧产物能导入 |
| **M2** | `TraceSink` 协议 + `DbSink` + `llm_call` 全文落库 | `--trace <run_id>` 与文件轨迹查到同一份数据；`--llm <call_id>` 逐字复现题面与输出 |
| **M3** | FastAPI 只读层 | 用历史数据回答：「哪次调查产出了这份核查」「某帖子被抓过几次、内容变没变」 |
| **M4** | `POST /runs` + worker 队列 | 一条 curl 起一次调查，轮询到完成 |
| **M5** | 分级落地：A 级备份脚本、C 级清理、索引 | 备份/恢复演练通过；清理 C 级后历史报告数字不变 |
| **M6** | 文档收尾 + 回归 | 全部单测 + API 集成测试全绿（当前 177 例） |

M3 是关键的**验收点**：如果只读层用真实历史数据无法回答「这份核查报告的依据是哪几条帖子原文」，说明 A 级字段设计不够，要回头改，而不是等 M5。

---

## 8. 决策记录

2026-11-18 已确认（均按推荐项）：

| 决策 | 结论 | 理由 |
|---|---|---|
| 存储 | **SQLite（WAL）+ SQLAlchemy 2.0** | 单机零运维；抽象层保证换 Postgres 只改 DSN |
| 文件产物 | **保留为导出视图**，DB 为权威 | GitHub 可读性与现有回归测试都依赖文件；迁移期双写 |
| Worker | **独立进程** | Playwright 同步 API + Chrome 单 profile 决定了采集只能串行，不能塞进 uvicorn |

由此确定的实现约束：

- 不引入 Alembic，用 `store/migrations/` 顺序 SQL + `schema_version` 表
- `StageRunner` 与 `RunStore` 之间只传 Pydantic 对象，不传 ORM 实例（避免 session 生命周期泄漏到领域层）
- API 进程只做轻量写（创建 run、改状态），重数据写入只发生在 worker 进程

---

## 9. 不做的事（明确排除）

- **不做多用户 / 权限**。`owner` 字段留空占位，不做登录。
- **不做对象存储**。本地 CAS 目录够用；将来换 S3 只改 `blobs.py`。
- **不改领域逻辑**。阈值、缺口规则、降级规则一律不动——那是上一轮刚验证过的东西，和本次改造正交。
- **不上前端**。FastAPI 自带 `/docs` 够看。
