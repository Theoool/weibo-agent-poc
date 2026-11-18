"""浏览器层：专用 Chrome Profile + 微博页面读取。"""

from .chrome import BrowserSession, BrowserSetupError, start_browser
from .weibo import (
    SearchResult,
    collect_search,
    detail_url,
    extract_hashtags,
    is_logged_in,
    merge_posts,
    posts_from_container,
    read_hot_board,
    search_url,
    strip_html,
    verify_detail,
    wait_for_login,
)

__all__ = [
    "BrowserSession",
    "BrowserSetupError",
    "start_browser",
    "SearchResult",
    "collect_search",
    "detail_url",
    "extract_hashtags",
    "is_logged_in",
    "merge_posts",
    "posts_from_container",
    "read_hot_board",
    "search_url",
    "strip_html",
    "verify_detail",
    "wait_for_login",
]
