"""提示词。核心约束：只能基于已抓到的帖子说话，不许编造、不许猜链接。"""

from __future__ import annotations

SYSTEM_PROMPT = """你是一个微博舆情调查 Agent。你的唯一信息来源是"已经抓取到的帖子列表"和"已经读取的官方/权威网页"。

硬性约束：
1. 不得编造微博，不得编造作者、时间、正文。
2. 不得猜测或拼接微博 URL。链接只能来自输入数据中的 url 字段；没有就写"无可验证直链"。
3. 不得把推测写成事实。事实必须是输入数据可逐字确认的内容，并说明它由哪几条帖子支撑。
4. 不得绕过登录、验证码、人机校验或任何访问限制。遇到限制只记录，不尝试规避。
5. 同一事件不得拆成多个事件；同一事件的不同话题应合并。
6. 区分三类信息：事实（可直接由原始来源确认）、观点（账号/媒体/受访者表达的看法）、推测（证据不足的推断）。
7. 如果不同来源互相矛盾，不要自行裁决，明确指出存在争议。
8. 输入数据不足时，宁可留空并说明缺口，也不要补全。
"""


def keyword_plan_prompt(topic: str, hot_items: list[str], searched: list[str], round_no: int) -> str:
    hot_block = "\n".join(f"- {x}" for x in hot_items[:25]) or "（未获取到热搜榜）"
    used_block = "、".join(searched) or "（无）"
    return f"""调查主题：{topic}
当前是第 {round_no} 轮关键词规划。已搜索过的关键词：{used_block}

以下是刚从公开页面（https://weibo.cn/pub/ 热搜榜单）读取到的当前榜单条目，按页面顺序排列（越靠前位次越高）：
{hot_block}

任务：为本轮挑选 2 到 4 个微博搜索关键词。

规则：
- 关键词必须能从上面的榜单条目或调查主题中直接推导出来，不得凭空想象事件名。
- 优先覆盖"与主题相关但尚未搜索"的榜单条目。
- 如果榜单里有明显属于同一事件的多条条目，只取其中最具代表性的一条，避免重复。
- 不要输出带 # 的完整话题名以外的修饰语，也不要输出解释。

只输出 JSON：{{"keywords": ["关键词1", "关键词2"], "reason": "一句话说明选择依据"}}
"""


def digest_posts(posts: list[dict], limit: int = 60, text_limit: int = 160) -> str:
    """把帖子压成给模型看的摘要块，保留 mid/url 以便回填证据。"""
    rows: list[str] = []
    for i, p in enumerate(posts[:limit], 1):
        text = (p.get("text") or "").replace("\n", " ")
        if len(text) > text_limit:
            text = text[:text_limit] + "…"
        rows.append(
            f"[{i}] mid={p.get('mid') or '?'} | 作者={p.get('author') or '?'} | "
            f"时间={p.get('time') or '?'} | 赞{p.get('likes')}/评{p.get('comments')}/转{p.get('reposts')}\n"
            f"    正文：{text}\n"
            f"    url：{p.get('url') or '无可验证直链'}"
        )
    return "\n".join(rows)


EVENT_SCHEMA_HINT = """{
  "events": [
    {
      "name": "事件名称（不含书名号）",
      "summary": "2-3 句事件摘要",
      "heat": "high | medium | low | unknown",
      "heat_basis": "给出可核对的热度依据，例如'在抓取的 N 条帖子中出现 M 条，占 X%'",
      "keywords": ["归属该事件的关键词"],
      "facts": [
        {
          "text": "可直接由原始来源确认的事实，一句话",
          "supporting_mids": ["支撑这句话的 mid，只能填输入中出现过的；挂不上就留空数组"],
          "source_kind": "weibo_post | official_site | media_report | unknown"
        }
      ],
      "opinions": ["账号/媒体/受访者表达的观点，注明是谁的观点"],
      "speculations": ["证据不足、只能作为推测的内容"],
      "evidence_mids": ["只能填输入中出现过的 mid"],
      "judgement": "用一段话说明事实/观点/推测的边界，以及是否存在来源矛盾",
      "controversy": "若存在互相矛盾的来源，写在这里；否则留空字符串"
    }
  ]
}"""


def event_build_prompt(topic: str, digest: str, total: int, hot_items: list[str]) -> str:
    hot_block = "\n".join(f"- {x}" for x in hot_items[:25]) or "（未获取到热搜榜）"
    return f"""调查主题：{topic}

已抓取 {total} 条微博帖子的摘要如下（每条都带 mid 和 url）：

{digest}

另外，公开热搜榜单（https://weibo.cn/pub/）当前的条目顺序为：
{hot_block}

任务：从上面的帖子中归纳出**讨论度明显不同的 3 到 5 个独立事件**。

要求：
- 事件必须由帖子内容支撑，不得引入输入里没有的事件。
- 如果样本只够支撑 2 个事件，就只输出 2 个，**绝对不要为了凑数而拆事件或编事件**。
- 属于同一事件的不同话题必须合并为一个事件，不要拆分。
- heat 只能依据"该事件的帖子在已抓取样本中的占比"来判断，并在 heat_basis 里写出具体数字。
- evidence_mids 只填输入中出现过的 mid，每条事件至少 3 个（能填满就填满，不够就如实少填）。
- 每条 facts 都要用 supporting_mids 说明"是哪几条帖子支撑了这句话"，只能引用输入里出现过的 mid。
  一句话由多条帖子共同支撑就都填上；确实挂不上就把数组留空，**不要随手填几个 mid 凑数**。
- facts / opinions / speculations 三栏必须分开；无法确认的一律放 speculations。
- 不要输出 Markdown，只输出 JSON。

严格按此结构输出：
{EVENT_SCHEMA_HINT}
"""
