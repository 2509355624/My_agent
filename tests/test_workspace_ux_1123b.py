"""2026-11-23 第二轮反馈的回归钉子。

用户在手机上点出来的四件事，逐个钉住，免得下次重构又退回去：

1. 「＋」面板被撑进了输入框（`plus-menu` 那段 CSS 丢了选择器头，整段成了废声明）；
2. 一直点右上角人形会叠出一串「我的」，✕ 按几次都像没反应；
3. 横滑标签条会把页面退掉（浏览器边缘手势）+ 页面自己的边缘手势判据太松；
4. 子页里滑列表，输入栏跟着一起走（底部留白做在了滚动容器外面）。
"""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WS = os.path.join(ROOT, "web", "workspace")


def _read(rel, base=WS):
    with open(os.path.join(base, rel), encoding="utf-8") as f:
        return f.read()


class TestPlusMenuIsFloat(unittest.TestCase):
    """「＋」面板必须是浮层，不能参与输入栏的流式排版。"""

    def test_menu_rule_has_selector(self):
        css = _read("css/chat.css")
        idx = css.index(".plus-menu {")
        block = css[idx:css.index("}", idx)]
        self.assertIn("position: absolute", block)

    def test_no_orphan_declarations(self):
        """CSS 里不允许出现「注释收尾 + 空行 + 顶格两格声明」这种无主块。

        那段 plus-menu 就是这样丢的选择器头：注释后面直接跟 left/top/bottom，
        浏览器把整段当垃圾丢掉，面板于是变成了输入框里的一行——错得很安静。
        正常写法的注释收尾后面要么是 `}`，要么是同一选择器里的其他属性
        （前面已经有「选择器 {」撑着），不会空一行再顶两格。
        """
        css = _read("css/chat.css")
        bad = [m for m in re.findall(r"\*/\n\n  ([a-z-]+):[^\n]*", css)]
        self.assertEqual(bad, [], "有声明没有选择器: " + repr(bad[:3]))


class TestMeDoesNotSelfStack(unittest.TestCase):

    def test_guard_uses_stack_top(self):
        js = _read("js/stack.js")
        # 判据必须是当前栈顶，不能是建节点时的那个 view
        self.assertIn('if (S.top && S.top.dataset.view === "me") return;', js)
        self.assertNotIn('if (view.dataset.view !== "me") push("me");', js)


class TestSwipeIsNotBrowserHistory(unittest.TestCase):

    def test_view_blocks_page_level_gestures(self):
        """view 上要关掉浏览器的手势，否则边缘一滑整页跳走。"""
        css = _read("css/sheet.css")
        idx = css.index(".view {")
        block = css[idx:css.index("}", idx)]
        self.assertIn("overscroll-behavior: none", block)

    def test_edge_swipe_needs_clear_horizontal(self):
        """边缘右滑要同时满足「够远」且「没怎么竖向跑」。"""
        js = _read("js/stack.js")
        self.assertIn("var go = dx > 90 && dy < 90;", js)
        self.assertIn("var EDGE = 24;", js)

    def test_horizontal_strips_own_their_scroll(self):
        css = _read("css/gallery.css")
        idx = css.index(".gtabs {")
        block = css[idx:css.index("}", idx)]
        self.assertIn("touch-action: pan-x", block)


class TestComposerStaysPut(unittest.TestCase):
    """子页滚动时输入栏必须钉在底部：留白只能做在滚动容器里。"""

    def test_dock_body_does_not_scroll(self):
        css = _read("css/chat.css")
        idx = css.index(".vbody.v-dock {")
        block = css[idx:css.index("}", idx)]
        self.assertIn("overflow: hidden", block)
        self.assertIn("padding: 0", block)

    def test_gallery_scroll_is_inner_layer(self):
        js = _read("js/gallery.js")
        self.assertIn("gscroll", js)
        css = _read("css/gallery.css")
        idx = css.index(".gscroll {")
        block = css[idx:css.index("}", idx)]
        self.assertIn("overflow-y: auto", block)
        self.assertIn("--composer-h", block)

    def test_me_scroll_is_inner_layer(self):
        js = _read("js/me.js")
        self.assertIn("mescroll", js)
        css = _read("css/me.css")
        idx = css.index(".mescroll {")
        block = css[idx:css.index("}", idx)]
        self.assertIn("overflow-y: auto", block)

    def test_me_keeps_browse_position(self):
        """从「我的」再进一层、退回来时不该掉到顶部。"""
        js = _read("js/me.js")
        self.assertIn("POS", js)
        self.assertIn("scrollTop = POS", js)


if __name__ == "__main__":
    unittest.main()
