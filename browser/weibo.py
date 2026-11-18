"""微博页面的读取与提取。

只做三件事：
1. 判断当前 Profile 是否已登录微博（读 m.weibo.cn/api/config）。
2. 打开搜索页并读取帖子（DOM 解析 + 页面自身 container 接口，互为补充）。
3. 打开详情页复核单条帖子的作者 / 时间 / 正文（证据校验）。

不做：绕过登录、破解验证码、伪装指纹、构造未登录无法获得的接口。
"""

from __future__ import annotations

import html
import re
import time
from dataclasses import dataclass, field
from typing import Any, Iterable
from urllib.parse import quote

from playwright.sync_api import BrowserContext, Page

MOBILE_HOME = "https://m.weibo.cn/"
CONFIG_API = "https://m.weibo.cn/api/config"
HOT_BOARD = "https://weibo.cn/pub/"

_TAG_RE = re.compile(r"<[^>]+>")
_HASHTAG_RE = re.compile(r"#([^#\s]{2,40})#")


# --------------------------------------------------------------------------- #
# 纯函数（可单测，不依赖浏览器）
# --------------------------------------------------------------------------- #
def strip_html(text: str | None) -> str:
    """把微博正文 HTML 片段转成纯文本。"""
    if not text:
        return ""
    raw = (
        text.replace("<br />", "\n")
        .replace("<br/>", "\n")
        .replace("<br>", "\n")
        .replace("</p>", "\n")
    )
    plain = _TAG_RE.sub("", raw)
    plain = html.unescape(plain)
    plain = re.sub(r"[ \t\u00a0]+", " ", plain)
    plain = re.sub(r"\n{3,}", "\n\n", plain)
    return plain.strip()


def extract_hashtags(text: str) -> list[str]:
    """抽取话题词，用于事件聚类。"""
    seen: list[str] = []
    for tag in _HASHTAG_RE.findall(text or ""):
        tag = tag.strip()
        if tag and tag not in seen:
            seen.append(tag)
    return seen


def search_url(keyword: str) -> str:
    """移动端综合搜索页。containerid=100103type=1&q=<keyword>"""
    return f"https://m.weibo.cn/search?containerid=100103type%3D1%26q%3D{quote(keyword)}"


def search_api_url(keyword: str, page_no: int = 1) -> str:
    """搜索页自身调用的 container 接口（用于补齐精确字段）。"""
    return (
        "https://m.weibo.cn/api/container/getIndex"
        f"?containerid=100103type%3D1%26q%3D{quote(keyword)}"
        f"&page_type=searchall&page={page_no}"
    )


def detail_url(mid: str) -> str:
    return f"https://m.weibo.cn/detail/{mid}"


def iter_mblogs(cards: Iterable[Any] | None) -> Iterable[dict]:
    """递归遍历 container 返回的 cards，取出所有 mblog。"""
    for card in cards or []:
        if not isinstance(card, dict):
            continue
        mblog = card.get("mblog")
        if isinstance(mblog, dict):
            yield mblog
        for key in ("card_group", "cards"):
            if card.get(key):
                yield from iter_mblogs(card[key])


def post_from_mblog(mblog: dict, keyword: str, source: str = "container_api") -> dict:
    """把 mblog 规整成统一的帖子结构。"""
    user = mblog.get("user") or {}
    mid = str(mblog.get("id") or mblog.get("mid") or "")
    text = strip_html(mblog.get("text"))
    retweeted = strip_html((mblog.get("retweeted_status") or {}).get("text"))
    return {
        "mid": mid,
        "author": user.get("screen_name", "") or "",
        "author_id": str(user.get("id", "") or ""),
        "verified": bool(user.get("verified")),
        "time": mblog.get("created_at", "") or "",
        "text": text,
        "retweeted_text": retweeted[:300],
        "url": detail_url(mid) if mid else "",
        "reposts": mblog.get("reposts_count"),
        "comments": mblog.get("comments_count"),
        "likes": mblog.get("attitudes_count"),
        "source": strip_html(mblog.get("source")),
        "is_long_text": bool(mblog.get("isLongText")),
        "keyword": keyword,
        "evidence_source": source,
        "detail_verified": False,
        "hashtags": extract_hashtags(text),
    }


def posts_from_container(payload: dict, keyword: str) -> list[dict]:
    """从 container 接口返回体提取帖子。"""
    data = (payload or {}).get("data") or {}
    cards = data.get("cards") or []
    return [post_from_mblog(m, keyword) for m in iter_mblogs(cards)]


def _norm_prefix(text: str | None, n: int = 50) -> str:
    """用正文前缀做兜底去重键（DOM 与接口的正文通常同源）。"""
    return re.sub(r"\s+", "", (text or ""))[:n]


def merge_posts(primary: list[dict], secondary: list[dict]) -> list[dict]:
    """去重合并：先按 mid/url，再按正文前缀兜底；primary 的字段优先。"""
    merged: dict[str, dict] = {}
    order: list[str] = []
    prefix_to_key: dict[str, str] = {}

    for post in list(primary) + list(secondary):
        tag = _norm_prefix(post.get("text"))
        mid = (post.get("mid") or "").strip()
        url = (post.get("url") or "").strip()
        key = mid or url or tag
        if not key:
            continue

        target_key = key if key in merged else prefix_to_key.get(tag or "", "")
        if not target_key:
            merged[key] = post
            order.append(key)
            if tag:
                prefix_to_key[tag] = key
            continue

        target = merged[target_key]
        for k, v in post.items():
            if not target.get(k) and v:
                target[k] = v
        if not target.get("mid") and mid:
            target["mid"] = mid
            target["url"] = target.get("url") or url
        if tag:
            prefix_to_key[tag] = target_key
    return [merged[k] for k in order]


# --------------------------------------------------------------------------- #
# 浏览器动作
# --------------------------------------------------------------------------- #
@dataclass
class SearchResult:
    keyword: str
    url: str
    posts: list[dict] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)


def is_logged_in(context: BrowserContext, timeout_ms: int = 15000) -> bool:
    """用浏览器上下文自身的 Cookie 请求微博配置接口，判断登录态。"""
    try:
        resp = context.request.get(
            CONFIG_API,
            headers={"Referer": MOBILE_HOME, "X-Requested-With": "XMLHttpRequest"},
            timeout=timeout_ms,
        )
        if not resp.ok:
            return False
        payload = resp.json()
        return bool((payload.get("data") or {}).get("login"))
    except Exception:  # noqa: BLE001
        return False


def wait_for_login(
    session,
    timeout_sec: int = 300,
    poll_sec: float = 3.0,
    quiet: bool = False,
) -> bool:
    """导航到微博首页并轮询登录态。不代填账号密码，只等人手动登录。"""
    page = session.page
    try:
        page.goto(MOBILE_HOME, wait_until="domcontentloaded", timeout=45000)
    except Exception as exc:  # noqa: BLE001
        if not quiet:
            print(f"[warn] 打开微博首页失败：{exc}")
    deadline = time.time() + timeout_sec
    last_hint = 0
    while time.time() < deadline:
        if is_logged_in(session.context):
            return True
        remain = int(deadline - time.time())
        if not quiet and remain // 30 != last_hint:
            last_hint = remain // 30
            print(f"  等待登录中……剩余 {remain}s（在打开的 Chrome 窗口里扫码/登录微博）")
        time.sleep(poll_sec)
    return is_logged_in(session.context)


_DOM_EXTRACT_JS = r"""
() => {
  const out = [];
  const seenMid = new Set();
  const seenText = new Set();
  const nodes = Array.from(document.querySelectorAll('.weibo-text'));
  for (const node of nodes) {
    const text = (node.innerText || '').trim();
    if (!text) continue;
    const root = node.closest('div.card9') || node.closest('div.card') || node;
    const html = root.outerHTML || '';
    let mid = '';
    const m = html.match(/(?:detail|status)\/(\d{10,})/);
    if (m) { mid = m[1]; }
    if (mid && seenMid.has(mid)) continue;
    const key = mid || text.slice(0, 50);
    if (!mid && seenText.has(key)) continue;
    if (mid) { seenMid.add(mid); } else { seenText.add(key); }
    const h3 = root.querySelector('h3.m-text-cut');
    const timeEl = root.querySelector('span.time');
    const fromEl = root.querySelector('span.from');
    out.push({
      text: text,
      author: h3 ? (h3.innerText || '').trim() : '',
      time: timeEl ? (timeEl.innerText || '').trim() : '',
      from: fromEl ? (fromEl.innerText || '').trim() : '',
      mid: mid,
      url: mid ? ('https://m.weibo.cn/detail/' + mid) : '',
      raw: (root.innerText || '').slice(0, 600)
    });
  }
  return out;
}
"""

_FETCH_JS = r"""
async (url) => {
  try {
    const resp = await fetch(url, {
      credentials: 'include',
      headers: {'X-Requested-With': 'XMLHttpRequest', 'MWeibo-Pwa': '1'}
    });
    const body = await resp.text();
    try {
      return {status: resp.status, ok: true, json: JSON.parse(body)};
    } catch (e) {
      return {status: resp.status, ok: false, text: body.slice(0, 300)};
    }
  } catch (e) {
    return {status: 0, ok: false, text: String(e)};
  }
}
"""


def _fetch_json_in_page(
    page: Page, url: str, retries: int = 2, backoff_sec: float = 2.0
) -> tuple[dict | None, str]:
    """在页面上下文里发起请求（携带当前登录 Cookie），返回 (JSON, 失败原因)。

    微博对同一会话的连续请求会返回 432/空响应，因此这里做有限重试 + 退避。
    """
    err = ""
    for attempt in range(retries + 1):
        try:
            result = page.evaluate(_FETCH_JS, url)
        except Exception as exc:  # noqa: BLE001
            err = f"页面内请求异常：{exc}"
            result = None
        if isinstance(result, dict) and result.get("ok"):
            payload = result.get("json")
            if isinstance(payload, dict):
                return payload, ""
            err = "响应不是 JSON 对象"
        elif isinstance(result, dict):
            status = result.get("status")
            snippet = (result.get("text") or "").strip()[:120]
            err = f"HTTP {status}：{snippet}" if status else f"请求失败：{snippet}"
        else:
            err = "页面内请求无返回"
        if attempt < retries:
            page.wait_for_timeout(int(backoff_sec * 1000 * (attempt + 1)))
    return None, err


def _dom_posts(page: Page, keyword: str) -> list[dict]:
    try:
        rows = page.evaluate(_DOM_EXTRACT_JS)
    except Exception:  # noqa: BLE001
        return []
    posts: list[dict] = []
    for row in rows or []:
        text = (row.get("text") or "").strip()
        if not text:
            continue
        url = row.get("url") or ""
        mid = (row.get("mid") or "").strip()
        if not mid:
            m = re.search(r"/(?:detail|status)/(\d+)", url)
            mid = m.group(1) if m else ""
        if not mid and len(text) < 30:
            continue
        posts.append(
            {
                "mid": mid,
                "author": (row.get("author") or "").strip(),
                "author_id": "",
                "verified": False,
                "time": (row.get("time") or "").strip(),
                "text": text,
                "retweeted_text": "",
                "url": url or (detail_url(mid) if mid else ""),
                "reposts": None,
                "comments": None,
                "likes": None,
                "source": (row.get("from") or "").strip(),
                "is_long_text": False,
                "keyword": keyword,
                "evidence_source": "dom",
                "detail_verified": False,
                "hashtags": extract_hashtags(text),
            }
        )
    return posts


def page_state(page: Page) -> str:
    """把当前页面状态压缩成一行，用于把"读不到"变成可诊断的限制记录。"""
    try:
        title = page.title()
        cards = page.eval_on_selector_all("div.card", "e => e.length")
        body = " ".join((page.inner_text("body") or "").split())[:90]
        return f"title={title!r} cards={cards} url={page.url[:90]!r} body={body!r}"
    except Exception as exc:  # noqa: BLE001
        return f"无法读取页面状态：{exc}"


def _load_and_read(
    page: Page, keyword: str, scrolls: int, settle_ms: int
) -> tuple[list[dict], list[str], bool]:
    """打开搜索页 → 滚动 → DOM 解析。返回 (posts, limitations, blocked)。

    blocked=True 表示被登录墙/访客校验拦住，此时不应再调用接口。
    """
    limits: list[str] = []
    url = search_url(keyword)
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
    except Exception as exc:  # noqa: BLE001
        return [], [f"搜索页打开失败：{exc}"], False

    page.wait_for_timeout(settle_ms)

    current = page.url
    if "passport." in current or "visitor" in current or "signin" in current:
        return [], [f"被重定向到登录/访客校验页（{current[:120]}）"], True

    for _ in range(max(0, scrolls)):
        try:
            page.mouse.wheel(0, 2400)
            page.wait_for_timeout(1400)
        except Exception:  # noqa: BLE001
            break
    page.wait_for_timeout(800)

    # 渲染过程中可能被重定向到登录页，这里再确认一次
    if "passport." in page.url or "signin" in page.url:
        return [], ["命中登录墙：搜索页被重定向到 passport.weibo.com 登录页"], True
    try:
        probe = (page.inner_text("body") or "")[:300]
    except Exception:  # noqa: BLE001
        probe = ""
    if "扫描二维码登录" in probe or "登录 - 微博" in (page.title() or ""):
        return [], ["命中登录墙：微博要求登录后才返回搜索结果，页面只显示扫码登录框"], True

    posts = _dom_posts(page, keyword)
    if not posts:
        limits.append(f"DOM 未解析出帖子，页面状态：{page_state(page)}")
    return posts, limits, False


def collect_search(
    session,
    keyword: str,
    max_posts: int = 30,
    scrolls: int = 2,
    api_pages: int = 2,
    settle_ms: int = 3000,
    attempts: int = 2,
) -> SearchResult:
    """打开搜索页读取帖子。

    两路取数：
    - DOM：真实渲染后的页面（最贴近"人在看页面"）；
    - container 接口：页面自身调用的接口，字段更精确（作者、时间、互动数）。
    命中限流（432 / Failed to fetch）时整页重载并退避，最多 attempts 次。
    """
    page = session.page
    url = search_url(keyword)
    result = SearchResult(keyword=keyword, url=url)
    all_dom: list[dict] = []
    all_api: list[dict] = []

    for attempt in range(1, max(1, attempts) + 1):
        dom_posts, limits, blocked = _load_and_read(page, keyword, scrolls, settle_ms)
        for item in limits:
            result.limitations.append(f"第 {attempt} 次尝试：{item}")
        all_dom = merge_posts(all_dom, dom_posts)
        if blocked:
            break  # 被登录墙拦住，再调接口只会得到 Failed to fetch

        api_posts: list[dict] = []
        for page_no in range(1, max(1, api_pages) + 1):
            payload, err = _fetch_json_in_page(page, search_api_url(keyword, page_no))
            if payload is None:
                result.limitations.append(f"第 {attempt} 次尝试：container 第 {page_no} 页失败：{err}")
                break
            if payload.get("ok") == -100:
                result.limitations.append(
                    f"第 {attempt} 次尝试：container 返回 ok=-100（该分页需要登录态）"
                )
                break
            chunk = posts_from_container(payload, keyword)
            if not chunk:
                break
            api_posts.extend(chunk)
            if page_no < api_pages:
                page.wait_for_timeout(800)

        all_api = merge_posts(all_api, api_posts)

        if all_api or all_dom:
            break
        if attempt < attempts:
            page.wait_for_timeout(6000)  # 退避后再试，避免把限流当成"没有内容"

    posts = merge_posts(all_api, all_dom)
    result.posts = posts[: max(0, max_posts)]
    if not posts:
        if any("登录墙" in x or "登录/访客校验" in x for x in result.limitations):
            result.limitations.append(
                "结论：被登录墙拦住，没拿到搜索内容。请先在这个专用 Chrome 窗口里登录微博，然后重跑。"
            )
        else:
            result.limitations.append(
                "最终未读到任何帖子：可能是限流、空结果页或前端结构变更，请对照上面的页面状态排查"
            )
    return result


def verify_detail(session, mid: str, expect_author: str = "", keyword: str = "") -> dict:
    """打开详情页，用页面自身接口核对作者 / 时间 / 正文。"""
    page = session.page
    info: dict = {"mid": mid, "url": detail_url(mid), "ok": False, "reason": ""}
    if not mid:
        info["reason"] = "缺少 mid"
        return info
    try:
        page.goto(info["url"], wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(1800)
    except Exception as exc:  # noqa: BLE001
        info["reason"] = f"详情页打开失败：{exc}"
        return info

    if "passport." in page.url or "visitor" in page.url:
        info["reason"] = "详情页被重定向到登录/访客校验页"
        return info

    payload, err = _fetch_json_in_page(page, f"https://m.weibo.cn/statuses/show?id={mid}")
    if payload and isinstance(payload.get("data"), dict):
        post = post_from_mblog(payload["data"], keyword, source="detail_api")
        post["detail_verified"] = True
        info.update({"ok": True, "post": post, "reason": ""})
        return info

    # 退化：从渲染后的 DOM 里取正文
    try:
        text = page.inner_text("body")[:2000]
    except Exception:  # noqa: BLE001
        text = ""
    if text.strip():
        info.update(
            {
                "ok": False,
                "dom_text": text,
                "reason": f"详情接口无数据（{err or '未知'}），仅取到页面文本",
            }
        )
    else:
        info["reason"] = f"详情接口无数据且页面无文本（{err or '未知'}）"
    return info


def read_hot_board(session=None, timeout_sec: int = 25) -> list[str]:
    """读取 https://weibo.cn/pub/ 的公开热搜榜单（无需登录）。

    仅作为"发现可用关键词"的线索，不作为帖子证据。
    """
    from playwright.sync_api import sync_playwright

    own = session is None
    pw = None
    browser = None
    page = None
    try:
        if own:
            pw = sync_playwright().start()
            browser = pw.chromium.launch(
                headless=True, channel="chrome", args=["--no-first-run"]
            )
            page = browser.new_page()
        else:
            page = session.context.new_page()
        page.goto(HOT_BOARD, wait_until="domcontentloaded", timeout=timeout_sec * 1000)
        items = page.eval_on_selector_all(
            "div.c a", "els => els.map(e => (e.innerText || '').trim()).filter(Boolean)"
        )
        return [i for i in items if i]
    except Exception:  # noqa: BLE001
        return []
    finally:
        if own:
            try:
                if browser is not None:
                    browser.close()
            finally:
                if pw is not None:
                    pw.stop()
        elif page is not None:
            try:
                page.close()
            except Exception:  # noqa: BLE001
                pass
