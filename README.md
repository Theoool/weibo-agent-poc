# 微博舆情调查 Agent POC

最小可跑版本：**Playwright 驱动的专用 Chrome → 手动登录一次微博 → 搜索 → 读取帖子 → 结构化落盘**，
再在其上叠一个受约束的 Agent 调度层（关键词规划 / 扩展 / 事件归纳 / 证据复核）。

第一版没有数据库，产物就是 `output/investigation.json` + `output/investigation.md`。

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

`uv sync` 会按 `pyproject.toml` 建 `.venv` 并装好 playwright / openai / pydantic / python-dotenv。

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
| 4 | 从已读帖子的话题词里挑扩展关键词，做**第二轮搜索**（保证"自主扩展 ≥1 次"） |
| 5 | 归纳 3~5 个事件（LLM 或频率聚类），强制拆分 事实 / 观点 / 推测 |
| 6 | 每个事件回填证据，并对 2 条做详情页复核 |
| 7 | 写 `output/investigation.json` / `output/investigation.md` |

预算硬限制（默认值，可改）：搜索 ≤10 次、页面 ≤30 个、时长 ≤10 分钟。

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
├─ main.py                  # CLI：--doctor / Day-1 / --agent / --trace / --handoff / --factcheck
├─ pyproject.toml
├─ .env                     # OPENAI_API_KEY / OPENAI_MODEL（留空即降级）
├─ agent/
│  ├─ investigator.py       # 调度层：预算、去重、事件归纳、证据复核、落盘
│  ├─ merge.py              # 事件合并：同一事件的不同话题标签并成一个
│  ├─ handoff.py            # 调查产物 → 核查待办（打通 POC 1 与 POC 2）
│  ├─ trace.py              # 运行轨迹 JSONL，事后可回放每一步决策
│  ├─ prompts.py            # 约束性提示词（禁止编造/猜链接）
│  └─ schemas.py            # Pydantic 模型 + JSON→Markdown 渲染
├─ browser/
│  ├─ chrome.py             # 专用 profile、持久化登录态
│  └─ weibo.py              # 登录检测、搜索读取、详情页复核、热搜榜
├─ factcheck/               # POC 2：事实核查
│  ├─ claims_ysjf.json      # 待核查说法 + 登记来源（人工维护）
│  ├─ verify.py             # 链接可达性、引文命中、登录墙识别
│  ├─ runner.py             # 降级规则、信源独立性计数、报告渲染
│  └─ schemas.py            # Source / ClaimCheck / 报告模型
├─ tests/
│  ├─ test_parsers.py       # 抓取层纯函数单测（不联网）
│  ├─ test_factcheck.py     # 引文比对、降级规则、转载不独立
│  └─ test_workflow.py      # 事件合并、Fact 兼容、handoff、trace
└─ output/                  # 调查与核查产物
```

## 6. 工作流：从"管道"到"有留痕的链路"

三处结构性改动，都是为了消除"看起来跑通了、其实没法追溯"的地方。

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

```powershell
uv run python main.py --trace output/trace_20261118_212101.jsonl
```

```
[2/2] 模型调用 4 次（成功 1｜失败 3）｜累计 4.5 秒
   1. ok   ping                 0.4s  9 → 2 字  digest a1b2c3d4e5f6  第 1 次
   2. FAIL keyword_plan_r1      3.9s  1204 → 0 字  digest ff00aa11bb22  第 1 次
      原因：HTTP 402 InsufficientBalance：Insufficient Balance（账户余额不足…）
```

读法：同一 `purpose` 的 `prompt_digest` 相同，说明两次输入完全一致；此时输出不同才是模型随机性，输出相同而结论不同则是后处理（白名单过滤、合并、降级）的问题。`Investigation.metrics` 里也带了 `llm_calls / llm_failures / llm_ms`，报告头部会显示一行「模型调用：成功 N 次｜失败尝试 M 次」。

## 7. 单测

```powershell
uv run python -m unittest discover -s tests -t . -v
```

覆盖 HTML 清洗、话题词抽取、container cards 递归解析、按 mid 去重合并、事件合并阈值、Fact 兼容、handoff 抽取，以及模型调用留痕（402 只记一次、软错误重试两次、解析失败后回退成功）——不依赖网络。

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

1. **`Controller` 循环**：把「搜索 → 扩展 → 归纳 → 结束」换成「评估缺口 → 补一个 → 再评估」，让"证据够不够"由判据决定，而不是由 `--detail-checks` 参数决定。
2. **查询意图分层**：扩展关键词现在只来自 hashtag 频次，产不出"官方回应""质疑/辟谣"这类反向查询，所以"是否存在对立观点"在结构上永远无法回答。
3. **评测基线**：固定 topic 与人工判定的理想事件集合，每次改工作流跑一遍算召回。没有度量就没法判断改动是变好还是变坏。

数据库和 FastAPI 放到这些都稳定之后再引入。

## 12. POC 2：事实核查（`--factcheck`）

用于对一份已生成的舆情报告做独立复核：逐条判断“已证实 / 部分证实 / 无法证实 / 存在矛盾”。

```powershell
uv run python main.py --factcheck factcheck/claims_ysjf.json
# 或者对 POC 1 的产物自动生成待核查清单（见第 6 节）
uv run python main.py --handoff output/investigation.json
uv run python main.py --factcheck output/claims_handoff.json
```

不需要模型 Key。输出 `output/claims_ysjf_factcheck.json` + `.md`。

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
