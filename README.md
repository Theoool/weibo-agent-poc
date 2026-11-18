# 微博舆情调查 Agent POC

最小可跑版本：**Playwright 驱动的专用 Chrome → 手动登录一次微博 → 搜索 → 读取帖子 → 结构化落盘**，
再在其上叠一个受约束的 Agent 调度层（关键词规划 / 扩展 / 事件归纳 / 证据复核）。

产物有两条出口：`output/` 下的文件（导出视图，方便阅读与 diff）和 `data/workflow.db`（权威）。
一次工作流是一个 `workflow_run`，证据全文、模型题面与输出、缺口决策都挂在它下面，
用 `--runs` / `--show` / `--trace` / `--llm` 可以查回来。

---

## 0. 环境要求

- Windows 10/11，已安装 **Google Chrome**（默认路径 `C:\Program Files\Google\Chrome\Application\chrome.exe`）
- Python ≥ 3.10（本机为 3.13）
- [uv](https://docs.astral.sh/uv/)

## 1. 安装

```powershell
cd weibo-agent-poc
uv sync
```

`uv sync` 会按 `pyproject.toml` 建 `.venv` 并装好 playwright / openai / pydantic / sqlalchemy / python-dotenv。

用的是本机 Chrome（`channel="chrome"`），**不需要**再下载 Chromium。只有在没有装 Chrome 时才需要：

```powershell
uv run playwright install chromium
# 然后在命令里加 --no-real-chrome
```

## 2. 自检

```powershell
uv run python main.py --doctor
```

输出会告诉你：依赖是否齐、Chrome 能否被拉起、当前 profile 是否已登录。

## 3. Day-1：先只跑通浏览器链路（不接 AI）

```powershell
uv run python main.py --keyword APEC --verify-detail 3
```

会发生什么：

1. 弹出一个**专用 Chrome 窗口**（profile 在 `.chrome-profile/`，和你的日常 Chrome 完全隔离）
2. 如果没登录，程序会停在 `m.weibo.cn` 等你**手动扫码/登录**（最多等 300 秒，`--login-timeout` 可调）
3. 登录态写入 profile 后，打开搜索页 `https://m.weibo.cn/search?containerid=100103type%3D1%26q%3DAPEC`
4. 自动滚动触发懒加载，读取帖子（DOM 解析 + 页面自身 container 接口互为补充）
5. 打印前 10 条（作者 / 时间 / 点赞 / 正文 / 链接），落盘到 `output/day1_APEC.json`
6. 跑一次 Day-1 验收清单

**验收标准只有一个：**

```
Python → Playwright → 专用 Chrome → 手动登录微博 → 搜索 APEC → 读取 ≥10 条
```

`--verify-detail N` 会额外打开前 N 条详情页，核对作者、时间、正文是否一致（这是第三阶段"证据验证"的雏形）。

只想知道读取链路通不通、暂时不想登录时：

```powershell
uv run python main.py --keyword APEC --headless --allow-guest --verify-detail 2
```

游客态只能读首屏，且连续搜索很快会被要求登录——**正式跑请务必登录**，否则样本会很小。

## 4. 再接 Agent

```powershell
# 先在 .env 里填 OPENAI_API_KEY（不填也能跑，会走 heuristic 降级）
uv run python main.py --agent --keyword APEC
```

Agent 会做：

| 步骤 | 动作 |
| --- | --- |
| 1 | 读公开热搜榜 `https://weibo.cn/pub/`（无需登录）拿到当前话题线索 |
| 2 | 规划第一轮 3~5 个关键词（必须是能从上一步推导出来的，不许凭空想事件） |
| 3 | 逐个搜索、去重累计帖子 |
| 4 | 归纳事件，然后由代码算出证据缺口（样本太薄 / 某事件证据不足 / 信源不独立 / 缺官方口径） |
| 5 | 按缺口优先级补搜，再重新归纳，直到没得补、没新数据、或预算/轮次用尽 |
| 6 | 每个事件回填证据，并对 2 条做详情页复核 |
| 7 | 写 `output/investigation.json` / `output/investigation.md` |

预算硬限制（默认值，可改）：搜索 ≤10 次、页面 ≤30 个、时长 ≤10 分钟、轮次 ≤3（含第一轮）。

### 两种模式的区别

- `llm`：模型做关键词规划与事件归纳，能拆事实/观点/推测并给热度依据。
- `heuristic`：纯本地降级，按"话题词 / 关键词"频率聚类成事件候选，`speculations` 会明确写"尚未做事实拆分"。结构完全一致，不会假装自己做了语义判断。

### POC 成功标准（程序自动判定）

| 能力 | 阈值 |
| --- | --- |
| 读取微博 | ≥10 条 |
| 自主扩展关键词 | ≥1 次（搜索总数 ≥2） |
| 形成事件 | ≥3 个 |
| 证据链 | 每事件 ≥2 条 |

未达标时程序退出码为 4，并打印 `limitations` 供定位。

## 5. 目录结构

```
weibo-agent-poc/
├─ main.py                  # CLI：--doctor / Day-1 / --agent / --trace / --llm / --handoff
│                           #       --factcheck / --runs / --show / --import-output
├─ pyproject.toml
├─ .env                     # OPENAI_API_KEY / OPENAI_MODEL（留空即降级）
├─ docs/
│  └─ architecture.md       # 数据分级、表设计、API 规划（改造前先写的设计稿）
├─ store/                   # 持久层：领域对象与数据库之间唯一的转换点
│  ├─ models.py             # SQLAlchemy 2.0 映射（与 migrations 一一对应）
│  ├─ migrations/           # 顺序 SQL + schema_version，不引 Alembic
│  ├─ repositories.py       # RunStore：建 run、写阶段、落调查/核查产物、读回
│  ├─ db_sink.py            # DbSink：轨迹行 + 模型题面/输出全文落库
│  ├─ blobs.py              # 内容寻址存储，长正文走 artifacts/ 而不是 SQLite
│  └─ importer.py           # 历史产物导入（标记 degraded）
├─ agent/
│  ├─ investigator.py       # 调度层：预算、去重、事件归纳、证据复核、落盘
│  ├─ controller.py         # 缺口判定与补搜计划：什么时候再搜、搜什么、什么时候停
│  ├─ merge.py              # 事件合并：同一事件的不同话题标签并成一个
│  ├─ handoff.py            # 调查产物 → 核查待办（打通 POC 1 与 POC 2）
│  ├─ trace.py              # 轨迹协议 TraceSink + JSONL sink + 全文 sink 分发
│  ├─ prompts.py            # 约束性提示词（禁止编造/猜链接）
│  └─ schemas.py            # Pydantic 模型 + JSON→Markdown 渲染
├─ browser/
│  ├─ chrome.py             # 专用 profile、持久化登录态
│  └─ weibo.py              # 登录检测、搜索读取、详情页复核、热搜榜
├─ factcheck/               # POC 2：事实核查
│  ├─ claims_ysjf.json      # 待核查说法 + 登记来源（人工维护）
│  ├─ verify.py             # 链接可达性、引文命中、登录墙识别
│  ├─ semantic.py           # 受限语义层：引文到底支不支持这句话
│  ├─ runner.py             # 降级规则、信源独立性计数、报告渲染
│  └─ schemas.py            # Source / ClaimCheck / 报告模型
├─ tests/
│  ├─ test_parsers.py       # 抓取层纯函数单测（不联网）
│  ├─ test_controller.py    # 缺口判定、候选白名单、补搜循环的停止条件
│  ├─ test_factcheck.py     # 引文比对、降级规则、转载不独立、语义只降不升
│  ├─ test_workflow.py      # 事件合并、Fact 兼容、handoff、trace、模型调用留痕
│  ├─ test_store.py         # 持久层：schema 与模型一致、读回逐字一致、分级规则
│  └─ test_trace_db.py      # 轨迹入库：全文可复现、失败隔离、坏 sink 不拖垮调查
├─ data/                    # SQLite（gitignore）
├─ artifacts/               # 内容寻址的长文本（gitignore）
└─ output/                  # 调查与核查产物（导出视图，gitignore）
```

## 6. 工作流：从"管道"到"有留痕的链路"

若干处结构性改动，都是为了消除"看起来跑通了、其实没法追溯"的地方。

**① 事件合并（`agent/merge.py`）**

模型和 hashtag 聚类都倾向照抄话题标签，于是「APEC」和「深圳APEC」会被输出成两个事件——直接违反"不要把同一事件拆成多个事件"。合并是纯规则，不调模型：名字归一化后互为子串且短名覆盖 40% 以上算同一事件，否则看字符集 Jaccard。阈值取保守值，宁可少合并，也不能把两件事并成一件。

合并发生在详情页复核**之前**，这样同一条帖子只复核一次，省预算。合并结果记在 `Event.merged_from` 里，报告顶部会显示"合并来源"。

**② Fact 带 provenance（`agent/schemas.py`）**

`facts` 从 `list[str]` 升级成 `list[Fact]`：

```python
class Fact(BaseModel):
    text: str
    kind: Literal["claim", "meta"]      # claim=可核查断言；meta=本次样本统计
    supporting_mids: list[str]          # 哪几条帖子支撑这句话
    source_kind: ...
    inferred_link: bool                 # 支撑关系是否由代码推断
```

旧产物（`facts: ["句子"]`）由 `before` validator 自动升级，不需要迁移脚本。支撑关系是推断出来的会标 `inferred_link`，报告里显示为「〔支撑自动关联〕」——这种关系只能当线索，不能当引用依据。

**③ handoff：调查 → 核查（`agent/handoff.py`）**

原先这一步是我人手读 `investigation.md`、抄出 8 条待核查说法。现在由代码完成：每条 `Fact` 连同它声称的支撑帖子，直接变成一条 `verdict="待核查"` 的 claim；支撑帖子会登记为来源，**同一作者只算一个独立信源**。

```powershell
uv run python main.py --handoff output/investigation.json        # 全部事件
uv run python main.py --handoff output/investigation.json --events 1,2
uv run python main.py --factcheck output/claims_handoff.json     # 接着核查
```

核查的起点因此不是"什么都没有"，而是"报告说有这几条证据"——核查要补的是报告之外的独立来源。`kind="meta"` 的事实会被跳过，因为"样本内 27 条帖子"描述的是本次抓取，外部无从核实。

**④ 运行轨迹（`agent/trace.py`）**

每次 `--agent` 都会写一份 `output/trace_<时间戳>.jsonl`，逐行记录 `run_start / llm_mode / search / llm_call / events_built / merge / run_end`。多轮判断之后，同一份输入跑两次结果可能不同，没有 trace 就只能猜差异出在哪一轮。

其中 `llm_call` 是**每一次模型尝试**（含重试）的留痕：`purpose / attempt / ok / duration_ms / request_chars / response_chars / prompt_digest / response_preview`，失败时额外附 `error` 和 `prompt_preview`（题面前 800 字）。这样"模型练坏了"和"喂进去的数据本来就不够"才能分开。

轨迹有两条出口，粒度不同：

| 出口 | 内容 | 用途 |
|---|---|---|
| `output/trace_*.jsonl` | 摘要（300 字预览） | 能提交、能 diff，看"哪一轮、多久、成没成" |
| `trace_event` + `llm_call` 表 | 同一条记录 + 题面与输出**全文** | 逐字复现"模型当时看到了什么、答了什么"，长文本走 `artifacts/` |

两个 sink 由一个 `FanoutSink` 分发。数据库写失败不会中断调查（文件产物还在），但会在收尾时明确打印降级原因——静默吞掉会让人误以为"这一段本来就没记录"。

```powershell
uv run python main.py --trace output/trace_20261118_212101.jsonl   # 读文件
uv run python main.py --trace run_20261118_232238_21138b          # 读库（同一个查看器）
uv run python main.py --llm llm_20261118_232304_03a4bd            # 某次调用的题面与输出全文
```

```
[2/2] 模型调用 4 次（成功 1｜失败 3）｜累计 4.5 秒
   1. ok   ping                 0.4s  9 → 2 字  digest a1b2c3d4e5f6  第 1 次
   2. FAIL keyword_plan_r1      3.9s  1204 → 0 字  digest ff00aa11bb22  第 1 次
      原因：HTTP 402 InsufficientBalance：Insufficient Balance（账户余额不足…）
```

读法：同一 `purpose` 的 `prompt_digest` 相同，说明两次输入完全一致；此时输出不同才是模型随机性，输出相同而结论不同则是后处理（白名单过滤、合并、降级）的问题。`Investigation.metrics` 里也带了 `llm_calls / llm_failures / llm_ms`，报告头部会显示一行「模型调用：成功 N 次｜失败尝试 M 次」。

**⑤ 缺口驱动的补搜循环（`agent/controller.py`）**

原先固定两轮：种子词 → 话题词扩展。问题在第二轮**搜什么与当前证据缺什么无关**——已经攒了 8 条证据的事件和只有 1 条的事件被同等对待，预算花在已经够了的地方，真正薄的事件反而没人管。

现在每轮结束后由**代码**算缺口，再决定搜什么：

| 缺口 | 触发条件 | 优先级 |
|---|---|---|
| `THIN_SAMPLE` | 已读帖子低于 10 条 | 0 |
| `NEED_EVIDENCE` | 某事件证据少于 `--min-evidence`（默认 3） | 1 |
| `NEED_INDEPENDENT` | 证据条数够了，但只来自 1 个账号 | 2 |
| `NEED_OFFICIAL` | 信源独立了，但没有一个认证账号 | 3 |

判定顺序是有意的：证据只有 1 条时，不再同时要求"补独立信源"和"补官方口径"，否则三份预算会花在同一件事上。

三条约束保证模型不能借这一步"发挥"：

- **候选只来自已抓到的数据**——正文话题词、榜单条目。不允许构造「XX 官方回应」这类事件名，那些词搜回来全是噪声。
- **模型只能在候选里挑**，挑到候选之外的直接丢弃并在 trace 里记一条 `gap_plan_rejected`，和 mid 白名单同一个道理。挑不中就用代码按优先级排好的结果。
- **只降不升**：模型不能说"证据够了"来跳过补搜，它没有这个开关。

停止条件比继续条件更重要——不会停的 Agent 会把预算均匀烧光，最后拿着同一批证据写报告：

1. 没有可行动的缺口（候选词已用尽）；
2. 上一轮补搜**没带来任何新帖子**（同一话题第二轮通常返回同一批内容，再搜只是重复消耗配额、增加风控概率）；
3. 搜索/页面/时间预算耗尽；
4. 达到 `--rounds` 轮次上限（默认 3，含第一轮）。

停止原因写进 `Investigation.stop_reason`，报告头部直接显示；每个缺口的"补了没补上"写进 `gaps`，报告里有「补搜记录」一节：

```
- 搜索轮次：2 轮｜判出缺口 4 个｜补上 1 个
- 停止原因：第 2 轮补搜未带来新帖子，继续搜索只会重复同一批内容

## 补搜记录（缺口驱动）

- 第 2 轮｜证据不足｜深圳APEC峰会交通管制：证据 2 条，低于下限 3 条｜补搜：深圳APEC｜仍缺
- 第 2 轮｜缺官方口径｜深圳APEC峰会交通管制：证据均来自非认证账号｜补搜：（未搜）｜无法判定
```

`resolved` 有三态：`true` 补齐、`false` 仍缺、`null` 无法判定。第三种出现在事件被重新归纳后改了名——这时缺口的"消失"与补搜无关，不能冒充成已补齐。

trace 里对应新增 `gaps`、`round_plan`、`round_stop`、`gap_plan_rejected` 四类记录，每一轮判了什么、搜了什么、为什么停，都能事后对齐。

**⑥ 证据引用：模型给的是行号怎么办**

实测（2026-11-18，真实模型）踩到一个隐蔽失效：喂给模型的每条帖子都带「第 N 条」和 `mid=长数字串`，模型倾向把**序号**当 mid 填回 `evidence_mids`。结果全部引用被白名单判为无效，三个事件的证据被清空，而报告只是安静地少了证据——不报错。

处理分两层：

1. **提示词写清**：mid 是长数字串，不是「第 N 条」，填序号会被判无效；
2. **代码做确定性映射**：序号 N → 输入里第 N 条帖子的 mid。这是可逐条核对的格式归一，不是补全，映射条数写进 `limitations` 并在 trace 里记 `mids_mapped`。

映射必须用**给模型看的那一批**帖子（`DIGEST_LIMIT=60` 条），不能用全部：输入被截断时，第 70 条并不在模型眼前，按序号映射会张冠李戴。越界序号照旧丢弃。

同时修掉一个计数错误：`kind="other"` 的「防编造校验」提示曾被当成证据计入条数，导致 `--agent` 的验收项「每事件证据 ≥2 条」显示 PASS 而实际一条证据都没有。现在 `REAL_EVIDENCE_KINDS` 只认 `weibo_post / official_site / media_report`；只剩校验提示的事件直接剔除。

## 7. 单测

```powershell
uv run python -m unittest discover -s tests -t . -v
```

覆盖 HTML 清洗、话题词抽取、container cards 递归解析、按 mid 去重合并、事件合并阈值、Fact 兼容、handoff 抽取、模型调用留痕（402 只记一次、软错误重试两次、解析失败后回退成功）、缺口判定与补搜循环的停止条件（无新数据即停、轮次上限、预算耗尽、候选白名单）、证据引用的行号映射与证据计数，以及持久层（schema 与模型逐列一致、存读后 `to_markdown()` 逐字一致、短文本内联／长文本走 CAS、坏 sink 不拖垮调查）——不依赖网络，共 129 例。

## 8. 边界：这个程序刻意不做什么

- **不代填账号密码、不处理验证码、不做指纹伪装**，遇到登录页就停下来等人。
- **不使用未登录无法获得的接口**：读取用的是页面自身渲染的数据 + 页面自己调用的 container 接口（带的是你手动登录的 Cookie）。
- **不复用日常 Chrome 的 profile**，`.chrome-profile/` 独立且已加进 `.gitignore`。
- **不允许 Agent 编造**：`evidence_mids` 里出现抓取结果之外的 mid 会被丢弃并记一条"防编造校验"证据；链接只从数据里取，没有就写"无可验证直链"。

## 9. 实测记录（2026-11-18，游客态）

写这份代码时在本机实跑过，供你排查时对照：

| 观测项 | 结果 |
| --- | --- |
| `weibo.cn/pub/` 热搜榜 | 免登录可读，稳定 20 条（本程序用它做关键词来源） |
| `m.weibo.cn/search?...` 首屏 | 游客态可渲染，能解析出 16~21 条帖子 |
| 连续搜索第 2 个关键词起 | 会被 302 到 `passport.weibo.com/sso/signin`，即"登录墙" |
| container 接口第 2 页起 | 返回 `{"ok":-100}`，需要登录态 |
| 详情页 `statuses/show?id=` | 游客态仍可用，能复核到作者与精确时间 |
| 单次 `--agent` 实测 | 4 组关键词、40 条帖子、3 个事件、11 条证据，6 项验收全 PASS（游客态） |

**结论：登录态决定样本规模。** 不登录也能跑通全链路，但每个关键词只有首屏、且很快被拦；登录后翻页与详情页复核才稳定。

## 10. 常见问题

| 现象 | 原因 / 处理 |
| --- | --- |
| 弹窗提示 profile 被占用 | 上一次 Chrome 没退干净，关掉窗口或删 `.chrome-profile/SingletonLock` |
| `浏览器启动失败：Executable doesn't exist` | 本机没装 Chrome，改用 `--no-real-chrome` + `playwright install chromium` |
| 搜索页解析出 0 条 | 前端结构变动或命中风控页，程序会把 URL 和限制原因写进 `limitations`；先把 `output/day1_*.json` 的 `limitations` 发出来 |
| 一直等登录 | 微博要求手机扫码；`--login-timeout` 可加大，或先手动在该 Chrome 窗口登录再重跑 |
| 详情页复核 FAIL | 帖子被删/仅粉丝可见，属正常，已记入 `limitations` |
| `--agent` 输出事件很粗 | 没配 `OPENAI_API_KEY`，走的是 heuristic 聚类 |

## 11. 下一步

已完成：事件合并、Fact 带 provenance、handoff 自动抽取、trace 留痕。

还没做（按价值排序）：

1. ~~**`Controller` 循环**~~：已实现（见 6-⑤），按证据缺口决定补搜什么，并显式记录停止原因。
2. **查询意图分层**：扩展关键词现在只来自 hashtag 频次和榜单条目，产不出"官方回应""质疑/辟谣"这类反向查询，所以"是否存在对立观点"在结构上仍然无法回答。要做需要先定义"哪些反向意图可以机械构造"，而不是让模型自由发挥。
3. **评测基线**：固定 topic 与人工判定的理想事件集合，每次改工作流跑一遍算召回。没有度量就没法判断改动是变好还是变坏——缺口循环里"证据 ≥3 条"这个阈值目前就是拍的，没有数据支撑。

数据库和 FastAPI 放到这些都稳定之后再引入。

## 12. POC 2：事实核查（`--factcheck`）

用于对一份已生成的舆情报告做独立复核：逐条判断“已证实 / 部分证实 / 无法证实 / 存在矛盾”。

```powershell
uv run python main.py --factcheck factcheck/claims_ysjf.json
# 或者对 POC 1 的产物自动生成待核查清单（见第 6 节）
uv run python main.py --handoff output/investigation.json
uv run python main.py --factcheck output/claims_handoff.json
```

不需要模型 Key 也能跑（不加 Key 时语义判定自动跳过）。输出 `output/claims_ysjf_factcheck.json` + `.md`。

### 第三道：引文与说法的对应关系（`factcheck/semantic.py`）

前两道机检只能证明“这句话确实出现在那一页”，证明不了“这句话支持这个说法”。实测里就有一条：报告写“发了 PS5 Pro”，引用的帖子通篇在讲放假安排——链接可达、引文也命中，但两者没关系。这种错误人工读一遍能看出来，机检看不出来。

所以加了一层受限的语义判定：模型只标注每条引文与说法的关系——`supports / partial / unrelated / contradicts / insufficient`，代码据此聚合成 `citation_check`（`supported / partial / unrelated / conflicting / insufficient`）。

边界是硬的，全部写在代码里而不是提示词里：

- **模型不产出核查结论**。它没有"判定为已证实"这个选项，报告里的结论仍由 `apply_checks` 按证据规则计算；
- **只有降级通道**。`_apply_semantic()` 里没有任何升级分支——「已证实」必须由独立信源数量支撑，不会因为模型说"看起来很支持"就给；
- **来源间矛盾要两个独立信源**。`cross_conflict=true` 且 `independent_groups >= 2` 才改写为「存在矛盾」；同一稿源的两条引文对不上，更可能是表述差异；
- **待核查不参与改判**。handoff 出来的说法本来就没有结论，语义判定只作为展示信息；
- **白名单**。模型编出不存在的 `source_id`、或给出非法等级，一律丢弃（和调查层对 mid 的校验同一个道理）；
- **只喂引文，不喂 URL**。避免模型去"推测"页面内容，等于是拿它的记忆当证据。

报告里会多两段：每条说法的「引文与说法的关系」和（如有）「来源间矛盾」。头部会写明执行情况，跳过时也写明原因，不会假装做过。

```powershell
uv run python main.py --factcheck output/claims_handoff.json               # 有 Key 就跑语义判定
uv run python main.py --factcheck output/claims_handoff.json --no-semantic  # 只看机检
uv run python main.py --factcheck output/claims_handoff.json --no-check     # 只跑语义判定（快速重渲）
uv run python main.py --factcheck output/claims_handoff.json --semantic-limit 5
```

实测一条（`C2 影视飓风官方称该解读太恶毒`）：报告登记了 5 条来源，模型判出 1 条支持、1 条部分支持、3 条只提到待遇与吐槽、与"官方回应"无关。机检对这种情况完全无感——链接都可达，引文也都命中。

### 它真正做的事

不是让模型写结论，而是**让结论经得起机检**。每个登记来源都要过两道：

1. **链接可达**：HTTP 200 且正文非空（403/空壳/被踢到登录页时会回退真实浏览器再试一次，适合微信公号、招聘站、工商站）；
2. **引文命中**：登记的 `quote` 必须真的出现在页面正文里（去标点后严格匹配，失败再走滑窗模糊匹配，容忍删字与改标点）。

规则是硬的，代码里写死：

- **支持来源全部校验失败 → 结论自动降级**，原判定保留在 `verdict`，生效判定写在 `final_verdict`，报告里标“机检降级，原判定「X」”；
- **待核查状态不参与降级**：本来就没下结论，不存在“结论不成立”；
- **同一 `independence_group` 的转载只计一个信源**，所以“两家转载同一篇稿”不会变成“两个独立信源”；
- **登录页不算证据**：`urllib` 会自动跟随 302，登录页返回 200 时若不加识别，就会把“被拦住”记成“来源可达”。现在会比对最终 URL，命中 passport / visitor / sso 的一律判为阻截并写明拦在哪一步。
- 微博用户发言可以登记，但不会被当成事实依据——报告里会明确标注来源类型为“社交平台用户发言”。

### claims 文件怎么写

```json
{
  "claims": [{
    "id": "C1",
    "claim": "待核查的说法",
    "report_line": "原报告里的原句",
    "verdict": "已证实",           // 人工初判，会被机检修正
    "analysis": "核查说明",
    "supporting": [{"source_id": "S1", "point": "这条来源支持了什么"}],
    "counter":    [{"source_id": "S2", "point": "反向证据是什么"}]
  }],
  "sources": [{
    "id": "S1",
    "kind": "media",              // official_account/official_api/company_registry/media/social_post/aggregator
    "publisher": "媒体名",
    "url": "https://...",
    "published_at": "2026-07-20",
    "quote": "页面里必须能找到的原文片段",
    "independence_group": "稿源分组名"   // 同一篇稿件的转载填同一个值
  }]
}
```

`quote` 留空表示“只验可达、不验引文”，适用于正文由 JS 渲染的页面；这类来源在报告里会标成“未登记可机检引文”，不会伪装成已核验。

### 一个刻意的设计

“无法证实”不等于“假的”。程序只回答“能否用可打开的原始来源支撑这句话”，不替读者推断真伪。同样，“未发现反向证据”也不等于“不存在反向证据”，报告里对这两点都有固定提示语。

## 13. 数据留存：run 是聚合根

改造前有个说不清的问题：四阶段靠**固定文件名**串接（`investigation.json` → `claims_handoff.json` → `claims_handoff_factcheck.json`），第二次运行会覆盖第一次，而带时间戳的副本只保留了 investigation。于是“这份核查报告核的是哪一次调查”根本答不上来。

现在每次运行是一个 `workflow_run`，四个阶段是它的 `stage_run`，靠外键串链。设计稿见 [`docs/architecture.md`](docs/architecture.md)。

```powershell
uv run python main.py --agent --keyword APEC           # 跑完自动落库，输出里带 run_id
uv run python main.py --runs                           # 看所有 run
uv run python main.py --show <run_id>                  # 看某次留了什么（阶段、文档数、缺口、事件）
uv run python main.py --show <run_id> --report         # 从数据库重新渲染报告
uv run python main.py --trace <run_id>                 # 轨迹与每次模型调用（读库，不依赖文件还在）
uv run python main.py --llm <call_id>                  # 某次调用的题面与输出全文
uv run python main.py --import-output output           # 把改造前的历史产物补进库
uv run python main.py --agent --keyword APEC --no-db   # 只写文件，不碰数据库
```

### 数据分三级，判据只有一条

> **只靠 A 级数据 + 当前代码，能不能重新算出这份报告？** 能 → B/C 级；不能 → A 级。

| 级别 | 内容 | 机制 |
|---|---|---|
| **A 不可再生** | 帖子全文快照、提示词与模型原始输出、机检结果、降级原因、缺口决策 | append-only，正文与结论不 UPDATE，永不自动清理 |
| **B 可重算但贵** | event / fact / 报告渲染产物 / 搜索记录 | 阶段结束一次事务写入，可整体重写 |
| **C 可丢弃** | `run_metric`、候选词池、页面 HTML、trace 统计 | 随时可重算，可设 TTL 清理 |

落在这些表上：`workflow_run` / `stage_run` / `source_document` / `document_fetch` / `event` / `event_evidence` / `fact` / `fact_support` / `gap_record` / `limitation` / `search_record` / `claim_check` / `factcheck_source` / `source_check` / `citation` / `llm_call` / `trace_event` / `run_metric`。

### 三个容易划错的地方

- **报告里引的是摘录，库里存的是全文。** `Evidence.content` 是被截断过的（摘要取 160 字、handoff 取 60 字）。所以入库时要把 `Investigator.posts` 一起传进去，只存 `Investigation` 会把最该留住的正文永久丢掉。`source_document.text_inline` 存全文，`event_evidence.content_excerpt` 存当时引用的那段。
- **trace 的记录是 A 级，trace 的统计是 C 级。** 耗时汇总可以重算，“第 3 轮判了 2 个缺口”不能重算。
- **跳过机检时不写 `source_check`。** 否则会留下一批“不可达”的假记录，和真的校验失败无法区分。`--no-check` 时传 `checked=False`。

### 内容寻址：同一条帖子只存一份

正文 < 4KB 直接进 `text_inline`，否则写 `artifacts/<sha256[:2]>/<sha256>`，库里只存 `text_ref`。同一 URL 被抓 10 次只占一份；内容变了会产生第二条 `source_document`（hash 不同），于是“这条帖子改过没有”变得可回答。

备份要同时覆盖 `data/` 和 `artifacts/`——只备份 `.db` 会得到一堆指向空文件的引用。

### 历史产物导入标 degraded

改造前的 `output/*.json` 里，证据正文当年就被截断过，全文快照从来没存过。导入的 run 一律 `quality=degraded`，`--runs` 和 `--show` 都会显示这个标记，不假装和新数据等价。`day1_*.json` 会被跳过：它没法挂到某次调查上，硬导只会造出一批孤立的 run。

### 模型调用可复现

`llm_call` 存的是全文，不是 300 字预览：题面（system + user）与输出各自按「< 4KB 内联 / 否则内容寻址」落库，读回来与当时送出去的一致。一次真实核查的留痕：

```
run_20261118_232238_21138b
[1/2] 1 条记录（来自数据库）
  llm_call            1
[2/2] 模型调用 1 次（成功 1｜失败 0）｜累计 26.0 秒
   1. ok   claim_citation_check  26.0s  1379 → 333 字  digest addb5b081f06  第 1 次
      全文：uv run python main.py --llm llm_20261118_232304_03a4bd
```

这条记录回答的正是以前答不了的问题：送到模型面前的是哪两条引文、它把 S1 判成 `partial` 的理由（"引文仅提及日期和地点，未包含具体场馆名称"）、以及这次判定花了 26 秒。摘要留在 JSONL 里负责"哪一轮、多久、成没成"，全文留在库里负责"到底看到了什么"。

### 与领域层的关系

`Investigator`、`Controller`、`factcheck/runner.py` 几乎没改，仍然只认 Pydantic 模型和 `TraceSink`。数据库只出现在三个边界：`main.py` 组装时调 `RunStore` 建 run/阶段、`FanoutSink` 把轨迹与模型全文转给 `DbSink`、阶段结束时 `save_investigation` 一次性写入语义层。所以原有的一百多个单测全部继续有效（现为 129 例）。

`tests/test_store.py` 里有一条最关键的用例：**保存后从库里读回，渲染出的 Markdown 必须与原始报告逐字一致**。少存任何一个字段这条就会失败——这是判断 schema 够不够用的硬标准。用 10 份真实历史产物验证过，全部逐字一致。
