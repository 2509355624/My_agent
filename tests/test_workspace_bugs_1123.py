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

    def test_messages_go_to_the_root_area_only(self):
        """消息一律写根消息区，不能按「哪一页在下层」去猜。

        2026-11-23 的坑：判据原来是「最后一个带 [data-chat-scroll] 的子页」。
        点「画同款」时会先退页、再挂参考卡，那一刻画廊页还在 DOM 里没被摘掉
        （closeNow 先摘 .in，节点要等 transitionend 才 remove），参考卡就挂进
        了正在消失的那一页，节点一回收卡片凭空不见——用户看到的正是
        「点了画同款什么都没发生」。所以现在固定写根消息区，子页不再收消息。
        """
        chat = _read("js/chat.js")
        i = chat.index("function box()")
        seg = chat[i:chat.index("function scrollBottom", i)]
        self.assertIn("return el.scroll;", seg)
        self.assertNotIn("querySelectorAll", seg, "不许再按查询结果挑消息区")
        self.assertNotIn("WSViews.scroll()", chat)

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
        self.assertIn("WSRef.useSame", js)

    def test_use_same_goes_back_to_chat(self):
        """画同款 = 反推提示词后回聊天页生成（2026-11-23 用户改的口径）。

        早先的规矩是「只填输入框、留在 AI 创作页」，用户当场否了：
        画廊里点一张只是想拿它当参考接着出图，不该困在浏览页里。
        现在由 gallery.js 负责退出子页并挂参考图，chat.js 只管收。
        """
        js = _read("js/gallery.js")
        self.assertIn("function sameAs", js)
        start = js.index("function sameAs")
        end = js.index("function current()", start)
        self.assertIn("WSStack.back", js[start:end], "画同款必须退回聊天页")
        self.assertIn("useSame", js[start:end], "画同款要把这张当参考交给聊天页")


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
