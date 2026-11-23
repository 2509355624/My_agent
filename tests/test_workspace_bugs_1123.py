# -*- coding: utf-8 -*-
"""2026-11-23 用户提的三条 bug 的回归钉子。

bug1 进 AI 创作看不到输入栏：输入栏只有一份，子页盖上来把它压住了。
     修法是把输入栏停靠进子页（views.js 的 dock），子页退回时还回聊天页。
bug2 点开图片没有「画同款」：查看器只做了收藏和下载，要补画同款。
bug3 拿不到渠道 + 输入框右边多一条滚动条：
     渠道是 5175 网关没有 /api/gen 反代（404）；滚动条是 #say 行高溢出。

这些是前端行为，跑不了 Python 单测，所以钉源码里的关键约定——
哪天有人把停靠去掉、把前缀改回去、把滚动条样式改回 4px，这里会红。
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WS = os.path.join(ROOT, "web", "workspace")


def _read(rel):
    with open(os.path.join(WS, rel), encoding="utf-8") as f:
        return f.read()


class TestComposerDocking(unittest.TestCase):
    """bug1：输入栏要能被借进 AI 创作页，退回时还回原位。"""

    def test_views_docks_composer_into_chatty_view(self):
        js = _read("js/views.js")
        self.assertIn("function dockComposer", js)
        self.assertIn("host.appendChild(composer)", js)
        # 画廊要借，漫画单本页不借（全屏读图）
        self.assertIn('{ gallery: true }', js)
        self.assertIn("undock", js)

    def test_build_calls_dock_and_marks_view(self):
        js = _read("js/views.js")
        self.assertIn("dockComposer(body, name)", js)
        self.assertIn("dataset.dock", js)

    def test_stack_returns_composer_on_close(self):
        js = _read("js/stack.js")
        self.assertIn("WSViews.undock()", js)

    def test_gallery_has_own_message_area(self):
        """子页里发的话要落在子页，不能写进被子页盖住的聊天页。"""
        self.assertIn('data-chat-scroll', _read("js/gallery.js"))
        chat = _read("js/chat.js")
        self.assertIn("WSViews.scroll()", chat)

    def test_composer_h_recomputed_after_docking(self):
        """量不到高度时会停在 0，底部留白就没了。"""
        js = _read("js/chat.js")
        self.assertIn("function fitWhenSized", js)
        self.assertIn('addEventListener("resize", fitWhenSized)', js)


class TestDrawSame(unittest.TestCase):
    """bug2：查看器要有「画同款」。"""

    def test_viewer_has_same_button(self):
        js = _read("js/gallery.js")
        self.assertIn("gv-same", js)
        self.assertIn("画同款", js)
        self.assertIn("WSChat.useSame", js)

    def test_use_same_stays_on_page(self):
        """画同款只填输入框，不许把页面退掉（点的是「照这个来」）。"""
        js = _read("js/chat.js")
        self.assertIn("function useSame", js)
        start = js.index("function useSame")
        end = js.index("// 输入栏是浮在消息上的", start)
        self.assertNotIn("WSStack.back", js[start:end])


class TestChannelAndScrollbar(unittest.TestCase):
    """bug3：渠道前缀 + 输入框滚动条。"""

    def test_gen_prefix_matches_gateway(self):
        js = _read("js/gen.js")
        self.assertIn('var BASE = "/api/gen";', js)

    def test_gateway_proxies_gen(self):
        with open(os.path.join(ROOT, "friend_app.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertIn('@app.route("/api/gen/<path:sub>"', src)
        self.assertIn('_forward("/api/gen", sub)', src)

    def test_say_has_no_stray_scrollbar(self):
        css = _read("css/chat.css")
        self.assertIn("#say::-webkit-scrollbar { width: 0; }", css)
        # 行高写死成整数，padding 10+10 + 22 正好 42 ≤ min-height 44
        self.assertIn("line-height: 22px;", css)


if __name__ == "__main__":
    unittest.main()
