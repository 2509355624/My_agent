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
        # 判据必须是当前栈顶，不能是建节点时的那个 view。
        # 2026-11-23 用户报「一直点 AI 创作会叠到卡死」之后，这条从「只拦
        # 我的」提升成 push() 里的通用规则：任何同页都不会叠第二层。
        self.assertIn('if (S.top && S.top.dataset.view === name', js)
        self.assertNotIn('if (view.dataset.view !== "me") push("me");', js)


class TestSwipeIsNotBrowserHistory(unittest.TestCase):

    def test_view_blocks_page_level_gestures(self):
        """view 上要关掉浏览器的手势，否则边缘一滑整页跳走。"""
        css = _read("css/sheet.css")
        idx = css.index(".view {")
        block = css[idx:css.index("}", idx)]
        self.assertIn("overscroll-behavior: none", block)

    def test_edge_swipe_needs_clear_horizontal(self):
        """退页手势只认「从左边缘起手」，中间起手一律不接管。

        2026-11-23 用户第三次报：在页面中间往右滑也会退回聊天页。旧判据
        `dx > 90 && dy < 90` 是松手那一刻的累计位移，横向标签条 / 缩略图
        横滑本身就能凑够 90，加上回拉能把 dy 抹小，于是屏幕中间随便一划
        就退出。现在钉的是新规则：起点必须在 EDGE 内、方向要一次性钉死、
        松手要过半或快甩。**这条测试以前钉的正是有 bug 的旧判据。**
        """
        js = _read("js/stack.js")
        self.assertIn("var EDGE = 24;", js)
        # 起点必须在左边缘：中间起手直接不接管
        self.assertIn("if (t.clientX > EDGE) { active = false; return; }", js)
        # 方向判定只看第一次明显位移，之后不再翻转
        self.assertIn("if (!decided) {", js)
        # 松手：过半或快甩，且要求 decided 才可能退
        self.assertIn("var far = dx >= view.clientWidth * 0.5;", js)
        self.assertIn("var flick = quick && dx > 70;", js)
        self.assertIn("var go = decided && (far || flick)", js)
        self.assertNotIn("var go = dx > 90 && dy < 90;", js)

    def test_horizontal_strips_own_their_scroll(self):
        """标签条两个轴都交给浏览器原生滚，不再单锁 pan-x。"""
        css = _read("css/gallery.css")
        idx = css.index(".gtabs {")
        block = css[idx:css.index("}", idx)]
        self.assertIn("overflow-x: auto", block)
        self.assertIn("touch-action: pan-x pan-y", block)


class TestComposerStaysPut(unittest.TestCase):
    """子页滚动时输入栏必须钉在底部：留白只能做在滚动容器里。"""

    def test_dock_body_does_not_scroll(self):
        css = _read("css/chat.css")
        idx = css.index(".vbody.v-dock {")
        block = css[idx:css.index("}", idx)]
        self.assertIn("overflow: hidden", block)
        self.assertIn("padding: 0", block)

    def test_gallery_scroll_is_inner_layer(self):
        # 2026-11-24：造节点那一段挪到了 gallery_shell.js（gallery.js 有 600
        # 行硬线），断言跟着走——两份合起来看，「内层滚动」还在。
        js = _read("js/gallery.js") + _read("js/gallery_shell.js")
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


class TestStyleSheetDrag(unittest.TestCase):
    """2026-11-27：画风抽屉拉到顶把整页拽下去，横条又拖不动。"""

    def test_list_contains_scroll_chain(self):
        """抽屉列表是 fixed 层，.view 的防护管不到它，必须自己断链。"""
        css = _read("css/sheet.css")
        idx = css.index(".style-list {")
        block = css[idx:css.index("}", idx)]
        self.assertIn("overscroll-behavior: contain", block)

    def test_handle_hands_gesture_to_js(self):
        """横条只有 4px，连同标题行一起当把手，并把竖向手势交给 JS。"""
        css = _read("css/sheet.css")
        idx = css.index(".sheet-grip, .sheet-head {")
        block = css[idx:css.index("}", idx)]
        self.assertIn("touch-action: none", block)
        js = _read("js/stack.js")
        self.assertIn('".sheet-grip, .sheet-head"', js)
        self.assertIn('body.style.transform = "translateY(" + dy + "px)"', js)
        self.assertIn("{ passive: false }", js)

    def test_drag_needs_down_direction_and_closes_on_release(self):
        """往上拉不接管（那是列表滚），往下过阈值/快甩才关闭。"""
        js = _read("js/stack.js")
        i = js.index("function bindSheetDrag()")
        block = js[i:js.index("\n  }", i)]
        self.assertIn("if (dy < 0) { d = null; return; }", block)
        self.assertIn("closeStyles();", block)

    def test_close_clears_inline_transform(self):
        """不清 inline transform，CSS 的收起位移会被盖掉，抽屉停在半路。"""
        js = _read("js/stack.js")
        i = js.index("function closeStyles()")
        block = js[i:js.index("\n  }", i)]
        self.assertIn('style.transform = ""', block)

    def test_page_overscroll_is_off(self):
        """下拉刷新圈是页面级橡皮筋，得在 html/body 这层关。"""
        css = _read("css/base.css")
        idx = css.index("html, body {")
        block = css[idx:css.index("}", idx)]
        self.assertIn("overscroll-behavior: none", block)

    def test_whole_sheet_owns_touch_and_list_is_released(self):
        """横条只有 4px，落偏到空隙/遮罩也会被浏览器接管，整层都得关；
           列表是里面唯一的滚动容器，必须单独放行 pan-y。"""
        css = _read("css/sheet.css")
        idx = css.index(".sheet {")
        block = css[idx:css.index("}", idx)]
        self.assertIn("touch-action: none", block)
        idx = css.index(".style-list {")
        block = css[idx:css.index("}", idx)]
        self.assertIn("touch-action: pan-y", block)


if __name__ == "__main__":
    unittest.main()
