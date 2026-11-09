# -*- coding: utf-8 -*-
"""`web/persona.html` 的单文件静态体检（2026-11-09）。

起因是一个**静默**事故：加「角色介绍」时新写了一个 `function renderIntro(id)`，
而开场导读那一个也叫 `renderIntro()`。同一个 <script> 作用域里后声明的会把
前面的整条覆盖掉，于是 `ensureIntro()` 调到了要 id 的那个版本、`!id` 直接
return——**新存档再也铺不出开场白**，页面不报任何错。

JS 不会对这种重名报错，页面照常加载，所以只能靠静态检查兜住：
函数名 / 顶层变量名不许重名。以后往这个 150KB 单文件里加东西时，重名会先
在这里红一次。
"""

import collections
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGE = os.path.join(ROOT, "web", "persona.html")


def _read():
    with open(PAGE, encoding="utf-8") as f:
        return f.read()


class NoShadowingTest(unittest.TestCase):
    def setUp(self):
        self.html = _read()

    def test_no_duplicate_function_names(self):
        """同名函数 = 后者静默覆盖前者（开场白事故就是这么来的）。"""
        names = re.findall(r"^\s*(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(",
                           self.html, re.M)
        dup = {k: v for k, v in collections.Counter(names).items() if v > 1}
        self.assertEqual(dup, {}, "页面里有同名函数：%r" % dup)

    def test_no_duplicate_top_level_variables(self):
        names = re.findall(r"^(?:let|const|var)\s+([A-Za-z_$][\w$]*)\s*=",
                           self.html, re.M)
        dup = {k: v for k, v in collections.Counter(names).items() if v > 1}
        self.assertEqual(dup, {}, "页面里有重名的顶层变量：%r" % dup)


class IntroFunctionsTest(unittest.TestCase):
    """开场导读和角色卡介绍是两件事，函数必须各是各的。"""

    def setUp(self):
        self.html = _read()

    def test_opening_line_keeps_its_own_function(self):
        self.assertEqual(self.html.count("function renderIntro("), 1)
        # 它不吃参数：新存档第一屏那块灰条不依赖任何 id。
        self.assertIn("function renderIntro() {", self.html)
        self.assertIn("ensureIntro", self.html)

    def test_card_detail_uses_a_separate_name(self):
        self.assertEqual(self.html.count("function renderCardIntro("), 1)
        self.assertIn("function renderCardIntro(id) {", self.html)
        self.assertIn("renderCardIntro(card.id)", self.html)

    def test_ensure_intro_calls_the_noarg_version(self):
        """`ensureIntro` 里只许调 `renderIntro()`，带参数的调用会静默什么都不做。"""
        body = self.html[self.html.index("async function ensureIntro("):]
        body = body[:body.index("\n}\n") + 3]
        self.assertIn("renderIntro()", body)
        self.assertNotIn("renderIntro(", body.replace("renderIntro()", ""))


class ShotToBackgroundTest(unittest.TestCase):
    """出图 → 「设为背景」（2026-11-09）。

    背景只有三种本地状态（`off` / `image` dataURL / `none`），这里新增的是
    第四种 `url`：直接引用 `/api/image/` 下那张剧照，不复制、不转码。
    三处必须同时认得它，少一处就会「点了没反应」或「刷新就丢」。
    """

    def setUp(self):
        self.html = _read()

    def test_button_sits_under_the_picture(self):
        body = self.html[self.html.index("function setPic("):]
        body = body[:body.index("\n}\n") + 3]
        self.assertIn("设为背景", body)
        self.assertIn("useAsBg(url)", body)
        # 游客本来就不能换背景，别给一个点了只会报错的按钮。
        self.assertIn('ROLE !== "guest"', body)

    def test_store_and_loader_both_know_the_url_state(self):
        store = self.html[self.html.index("const bgStore = {"):]
        store = store[:store.index("\n};")]
        self.assertIn('v.state === "url"', store)
        loader = self.html[self.html.index("async function loadBg("):]
        loader = loader[:loader.index("\n}\n") + 3]
        self.assertIn('s.state === "url"', loader)
        self.assertIn("BG_SRC_NAMES.shot", loader)

    def test_use_as_bg_writes_url_and_paints_it(self):
        fn = self.html[self.html.index("function useAsBg("):]
        fn = fn[:fn.index("\n}\n") + 3]
        self.assertIn('bgStore.write("url", url)', fn)
        self.assertIn("renderBg(url", fn)
        self.assertIn('ROLE === "guest"', fn)


class SlotButtonTest(unittest.TestCase):
    """角色卡上的黄色「存档」入口（2026-11-09 用户要求）。

    背景：点卡片其实**已经**会展开存档面板，但这个交互全靠猜——用户原话
    「不然你现在的设计用户根本不知道存档怎么看」。所以每张卡右上角挂一颗
    黄色「存档」标签，把入口写到脸上。

    四件事必须同时成立，少一件就会出真问题：
      1. 样式是黄色实心（不是普通灰按钮），否则「明显」不成立；
      2. loadCards 里真的挂了按钮，游客不挂（他们本来就没有存档）；
      3. click / touchend 两条路都把「点的是存档标签」传进 pick——
         手机只走 touchend、桌面只走 click，漏一条就是一半设备没反应；
      4. pick 认得这个标记：**哪怕正在演这个角色**也要开面板，
         否则点存档会被当成「已经在这个角色」直接关掉抽屉。
    """

    def setUp(self):
        self.html = _read()

    def test_style_is_yellow(self):
        css = self.html[self.html.index(".ci .ciSlot {"):]
        css = css[:css.index("}")]
        self.assertIn("background: #ffd24a", css)
        self.assertIn("font: 700 11px", css)
        # 名字行要给标签让位，否则长卡名会压到按钮底下
        self.assertIn("padding-right: 46px", self.html)

    def test_cards_render_the_button_but_guests_dont(self):
        body = self.html[self.html.index("async function loadCards("):]
        body = body[:body.index("\n}\n") + 3]
        self.assertIn('className = "ciSlot"', body)
        self.assertIn("存档", body)
        self.assertIn('ROLE !== "guest"', body)

    def test_click_and_touch_both_flag_the_slot_button(self):
        click = self.html[self.html.index('$("cards").addEventListener("click"'):]
        click = click[:click.index("});") + 3]
        self.assertIn("hitSlotBtn(ev.target)", click)
        self.assertIn("pick(c,", click)

        touch = self.html[
            self.html.index('$("cards").addEventListener("touchend"'):]
        touch = touch[:touch.index("}, { passive: false });")]
        self.assertIn("hitSlotBtn(ev.target)", touch)
        self.assertIn("pick(c,", touch)

    def test_pick_forces_slots_even_if_it_is_the_same_card(self):
        fn = self.html[self.html.index("async function pick("):]
        fn = fn[:fn.index("\n}\n") + 3]
        self.assertIn("async function pick(c, wantSlots)", fn)
        self.assertIn("!wantSlots && card && card.id === c.id", fn)
        self.assertIn("if (wantSlots || !slotPick)", fn)


if __name__ == "__main__":
    unittest.main()
