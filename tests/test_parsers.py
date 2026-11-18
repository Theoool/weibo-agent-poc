"""纯函数单测：不启动浏览器、不联网，保证解析与合并逻辑正确。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from browser.weibo import (  # noqa: E402
    extract_hashtags,
    merge_posts,
    posts_from_container,
    search_url,
    strip_html,
)

CONTAINER_PAYLOAD = {
    "ok": 1,
    "data": {
        "cards": [
            {
                "card_type": 9,
                "mblog": {
                    "id": "5000000000000001",
                    "created_at": "11-18 15:20",
                    "text": (
                        "APEC 现场：<a href='/k/APEC'>#APEC#</a> 供应链也是共赢链"
                        "<br />详情见 &quot;发布会&quot;"
                    ),
                    "user": {"screen_name": "测试账号A", "id": 123, "verified": True},
                    "reposts_count": 10,
                    "comments_count": 20,
                    "attitudes_count": 30,
                    "source": "<a>iPhone 18 Pro</a>",
                    "isLongText": False,
                    "retweeted_status": {"text": "<span>转发的原帖</span>"},
                },
            },
            {
                "card_type": 11,
                "card_group": [
                    {
                        "card_type": 9,
                        "mblog": {
                            "id": "5000000000000002",
                            "created_at": "11-18 16:00",
                            "text": "#APEC最高级别会议为何叫作非正式# 科普一下",
                            "user": {"screen_name": "测试账号B", "id": 456},
                        },
                    }
                ],
            },
            {"card_type": 4, "title": "广告卡，不含 mblog"},
        ]
    },
}


class TestStripHtml(unittest.TestCase):
    def test_removes_tags_and_unescapes(self) -> None:
        raw = "你好<a href='/x'>#话题#</a><br />第二行 &amp; 结尾"
        self.assertEqual(strip_html(raw), "你好#话题#\n第二行 & 结尾")

    def test_keeps_emoji_and_newlines(self) -> None:
        self.assertEqual(strip_html("甲<br/>乙<br/>丙"), "甲\n乙\n丙")

    def test_empty(self) -> None:
        self.assertEqual(strip_html(None), "")
        self.assertEqual(strip_html(""), "")


class TestHashtags(unittest.TestCase):
    def test_extract_unique_in_order(self) -> None:
        text = "#APEC# 讨论 #供应链# 再说一次 #APEC#"
        self.assertEqual(extract_hashtags(text), ["APEC", "供应链"])

    def test_ignores_empty_topic(self) -> None:
        self.assertEqual(extract_hashtags("## 和 ###"), [])


class TestContainerParsing(unittest.TestCase):
    def setUp(self) -> None:
        self.posts = posts_from_container(CONTAINER_PAYLOAD, "APEC")

    def test_extracts_nested_mblogs(self) -> None:
        self.assertEqual(len(self.posts), 2)
        mids = [p["mid"] for p in self.posts]
        self.assertEqual(mids, ["5000000000000001", "5000000000000002"])

    def test_fields(self) -> None:
        first = self.posts[0]
        self.assertEqual(first["author"], "测试账号A")
        self.assertTrue(first["verified"])
        self.assertEqual(first["time"], "11-18 15:20")
        self.assertEqual(first["url"], "https://m.weibo.cn/detail/5000000000000001")
        self.assertEqual(first["likes"], 30)
        self.assertEqual(first["source"], "iPhone 18 Pro")
        self.assertEqual(first["retweeted_text"], "转发的原帖")
        self.assertIn("供应链也是共赢链", first["text"])
        self.assertNotIn("<", first["text"])

    def test_missing_counts_are_none(self) -> None:
        second = self.posts[1]
        self.assertIsNone(second["likes"])
        self.assertEqual(second["hashtags"], ["APEC最高级别会议为何叫作非正式"])

    def test_empty_payload(self) -> None:
        self.assertEqual(posts_from_container({}, "APEC"), [])
        self.assertEqual(posts_from_container({"data": {"cards": []}}, "APEC"), [])


class TestMerge(unittest.TestCase):
    def test_dedupes_by_mid_and_fills_gaps(self) -> None:
        primary = [
            {"mid": "1", "author": "A", "text": "t1", "url": "u1", "likes": 5},
            {"mid": "2", "author": "B", "text": "t2", "url": "u2", "likes": None},
        ]
        secondary = [
            {"mid": "1", "author": "", "text": "t1", "url": "", "likes": None},
            {"mid": "3", "author": "C", "text": "t3", "url": "u3", "likes": 7},
        ]
        merged = merge_posts(primary, secondary)
        self.assertEqual([p["mid"] for p in merged], ["1", "2", "3"])
        self.assertEqual(merged[0]["author"], "A")  # primary 优先，不被空值覆盖
        self.assertEqual(merged[2]["author"], "C")

    def test_dom_post_without_mid_uses_url(self) -> None:
        merged = merge_posts(
            [{"mid": "", "url": "https://m.weibo.cn/detail/9", "text": "x"}],
            [{"mid": "", "url": "https://m.weibo.cn/detail/9", "text": "x", "author": "D"}],
        )
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["author"], "D")

    def test_dedupe_by_text_prefix_when_dom_has_no_mid(self) -> None:
        api = [{"mid": "777", "url": "u777", "text": "同一条内容 #APEC# 结尾", "author": "甲"}]
        dom = [{"mid": "", "url": "", "text": "同一条内容 #APEC# 结尾", "likes": 9}]
        merged = merge_posts(api, dom)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["mid"], "777")
        self.assertEqual(merged[0]["likes"], 9)

    def test_dom_entry_gets_mid_from_duplicate(self) -> None:
        dom = [{"mid": "", "url": "", "text": "只有正文没有链接的帖子内容足够长一些", "author": "乙"}]
        api = [{"mid": "888", "url": "u888", "text": "只有正文没有链接的帖子内容足够长一些"}]
        merged = merge_posts(api, dom)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["mid"], "888")
        self.assertEqual(merged[0]["author"], "乙")


class TestSearchUrl(unittest.TestCase):
    def test_encodes_keyword(self) -> None:
        url = search_url("APEC 供应链")
        self.assertTrue(url.startswith("https://m.weibo.cn/search?containerid=100103type%3D1%26q%3D"))
        self.assertIn("%20", url)


if __name__ == "__main__":
    unittest.main(verbosity=2)
