"""来源校验：链接是否可达、引文是否真的在页面里。

这是整个核查流程里唯一"不能被语言模型糊过去"的部分——
模型可以编造一段解释，但没法让一个 404 的链接变成 200。
"""

from __future__ import annotations

import gzip
import html
import io
import re
import socket
import urllib.error
import urllib.request
from difflib import SequenceMatcher

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
_TAG_RE = re.compile(r"<(script|style)[\s\S]*?</\1>", re.I)
_ANY_TAG_RE = re.compile(r"<[^>]+>")
# 只保留中日韩文字与字母数字，用于引文比对
_KEEP_RE = re.compile(r"[\u4e00-\u9fffA-Za-z0-9]")


def _normalize(text: str) -> str:
    return "".join(_KEEP_RE.findall(text or ""))


def quote_present(page_text: str, quote: str, threshold: float = 0.86) -> bool:
    """引文是否命中。先做严格子串，失败再做滑窗模糊匹配（容忍错字与省略）。"""
    q = _normalize(quote)
    if not q:
        return False
    body = _normalize(page_text)
    if not body:
        return False
    if q in body:
        return True
    # 长引文可能被媒体改标点/删字，用滑窗近似
    n = len(q)
    if n < 8 or len(body) < n:
        return False
    step = max(1, n // 6)
    best = 0.0
    for i in range(0, len(body) - n + 1, step):
        ratio = SequenceMatcher(None, q, body[i : i + n]).ratio()
        if ratio > best:
            best = ratio
            if best >= threshold:
                return True
    return best >= threshold


def _decode(raw: bytes, encoding: str | None) -> str:
    if raw[:2] == b"\x1f\x8b":
        try:
            raw = gzip.decompress(raw)
        except OSError:
            pass
    for enc in filter(None, [encoding, "utf-8", "gb18030"]):
        try:
            return raw.decode(enc, errors="strict")
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="ignore")


# 登录墙特征。urllib 会自动跟随 302，于是"被踢到登录页"会拿到一个 200 的
# 登录页——如果不识别，就会把登录页当成"来源可达"，整套核查结论都失真。
_LOGIN_MARKERS = ("passport", "signin", "/sso/", "visitor", "login")


def _is_login_redirect(original: str, final: str) -> bool:
    """最终 URL 是否被改写到登录/访客校验页。"""
    if not final:
        return False
    if final.rstrip("/") == (original or "").rstrip("/"):
        return False
    f = final.lower()
    return any(m in f for m in _LOGIN_MARKERS)


def fetch_page(url: str, timeout: int = 20) -> tuple[int, str, str, str]:
    """返回 (状态码, 纯文本正文, 页面标题, 最终 URL)。失败时状态码为 0。"""
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Accept-Encoding": "gzip",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            charset = resp.headers.get_content_charset()
            status = resp.status
            final_url = resp.geturl()
    except urllib.error.HTTPError as exc:
        return exc.code, "", "", url
    except (urllib.error.URLError, socket.timeout, TimeoutError, ValueError, OSError):
        return 0, "", "", url

    text = _decode(raw, charset)
    m = re.search(r"<title[^>]*>([\s\S]*?)</title>", text, re.I)
    title = html.unescape(m.group(1)).strip() if m else ""
    body = _TAG_RE.sub(" ", text)
    body = _ANY_TAG_RE.sub(" ", body)
    body = html.unescape(body)
    body = re.sub(r"\s+", " ", body)
    return status, body, title, final_url


def fetch_with_browser(url: str, timeout_ms: int = 25000) -> tuple[int, str, str, str]:
    """urllib 被 403 或拿到空壳时，用真实浏览器再试一次。

    很多站点（微信公号、招聘平台、工商查询）正文靠 JS 渲染或拦 UA，
    用 HTTP 抓取会误判成"引文不存在"。这里复用 POC 已有的 Playwright。
    """
    try:
        from playwright.sync_api import sync_playwright
    except Exception:  # noqa: BLE001
        return 0, "", "", url
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True, channel="chrome", args=["--no-first-run"]
            )
            page = browser.new_page(locale="zh-CN")
            resp = page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            page.wait_for_timeout(1800)
            status = resp.status if resp is not None else 0
            title = page.title()
            final_url = page.url
            body = page.inner_text("body") or ""
            browser.close()
            return status, re.sub(r"\s+", " ", body), title, final_url
    except Exception:  # noqa: BLE001
        return 0, "", "", url


def check_source(source, allow_browser: bool = False) -> None:
    """就地填写 source 的校验字段。"""
    status, body, title, final_url = fetch_page(source.url)
    used = "HTTP"

    # 403 / 空壳 / 明显过短的正文 / 被踢到登录页，都视为抓取失败，可回退浏览器
    thin = status == 200 and len(body.strip()) < 200
    blocked = _is_login_redirect(source.url, final_url)
    if allow_browser and (status != 200 or thin or blocked):
        b_status, b_body, b_title, b_final = fetch_with_browser(source.url)
        b_blocked = _is_login_redirect(source.url, b_final)
        if (
            b_status == 200
            and not b_blocked
            and len(b_body.strip()) > len(body.strip())
        ):
            status, body, title = b_status, b_body, b_title
            final_url, used, blocked = b_final, "浏览器回退", False

    source.http_status = status
    source.page_title = title[:120]

    if blocked:
        # 登录页不是证据。明确记录被拦在哪一步，而不是记成"可达"。
        source.reachable = False
        source.quote_found = None
        source.check_note = f"被重定向到登录/校验页（{final_url[:90]}），未取得正文"
        return

    source.reachable = status == 200 and bool(body.strip())
    if not source.reachable:
        source.quote_found = None
        source.check_note = f"链接不可达（HTTP {status or '连接失败'}）"
        return
    if not source.quote:
        source.quote_found = None
        source.check_note = f"{used} 已打开；页面正文由脚本渲染，未登记可机检引文"
        return
    found = quote_present(body, source.quote)
    source.quote_found = found
    source.check_note = (
        f"{used} 引文命中" if found else f"{used} 已打开，但正文中未找到登记引文"
    )

