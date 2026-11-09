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
      2. 左栏渲染（`renderCards`）里真的挂了按钮，游客不挂（他们本来就没有存档）；
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
        # 2026-11-09 左栏改成「官方 / 自定义」两栏后，建卡片那截从
        # `loadCards` 拆进了 `renderCards`——契约没变，只是换了个函数体。
        body = self.html[self.html.index("function renderCards("):]
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


class BackgroundPoursOnMidTest(unittest.TestCase):
    """背景图铺的是**整个中栏**，不是只铺聊天区（2026-11-09 用户两次追问）。

    第一次只把气泡改半透明，用户截图回来：「你管这个叫透明聊天框？」——
    因为图只铺 `.log`，开场导读、输入区、顶栏仍是三块实心黑，图被切成几段。
    所以现在 `has-bg` 挂在 `#mid` 上，聊天区自己不设底色。
    """

    def setUp(self):
        self.html = _read()

    def test_background_is_owned_by_mid_not_log(self):
        self.assertIn('<main class="mid" id="mid">', self.html)
        self.assertIn('.mid.has-bg { background-position: center', self.html)
        self.assertNotIn(".log.has-bg", self.html,
                         "has-bg 还挂在 .log 上，开场导读和输入区会重新变黑")

    def test_js_writes_the_image_to_mid(self):
        fn = self.html[self.html.index("function renderBg("):]
        fn = fn[:fn.index("\n}\n") + 3]
        self.assertIn('$("mid")', fn)
        self.assertIn('host.classList.add("has-bg")', fn)
        self.assertNotIn('$("log").classList', fn)

    def test_intro_and_input_also_go_translucent(self):
        for sel in (".mid.has-bg .intro", ".mid.has-bg .picbar",
                    ".mid.has-bg .pic ", ".mid.has-bg .composer textarea"):
            self.assertIn(sel, self.html, "少了 %s 的透明规则" % sel.strip())

    def test_no_background_keeps_the_solid_boxes(self):
        """透明规则全挂在 `.mid.has-bg` 下——没设背景的用户观感不许变。"""
        for rule in (".intro {", ".composer textarea {"):
            # 只认**行首**那条基础规则（带缩进），不然会先命中
            # `.mid.has-bg .intro {` 那条覆盖规则。
            i = self.html.index("\n  " + rule)
            line = self.html[i:i + 220]
            self.assertIn("var(--bg", line,
                          "%s 的默认底色被改了，没背景的人也变了" % rule[:-2])


class BackgroundFocusTest(unittest.TestCase):
    """背景纵向取景（2026-11-09 用户要求）。

    场景：电脑端拿一张竖屏立绘当背景，`background-size: cover` 会横向铺满、
    纵向只剩中间一段，默认居中正好把脸裁掉。用户要「自己控制显示的上下」。

    所以给一根滑块（0~100）控制 `background-position` 的纵向百分比，并且
    位置跟着背景一起存在本地。四个点必须成立：
      1. 滑块只在**真有背景图**时露面，没图时收起；
      2. 位置写在 `#mid` 上，且只在 `has-bg` 时才写（否则会把没背景的栏也
         设上 position，以后加了背景反而用错位置）；
      3. `bgStore.write` 不能把 pos 冲掉——换图、设剧照都走它；
      4. 拖动时只改样式、松手才写 localStorage（data 可能是几 MB 的
         dataURL，每动一格重写一遍会卡）。
    """

    def setUp(self):
        self.html = _read()

    def test_slider_lives_in_the_background_block(self):
        box = self.html[self.html.index('id="bgBox"'):]
        box = box[:box.index("</div>", box.index('id="fileBg"'))]
        self.assertIn('id="bgPos"', box)
        self.assertIn('id="bgPosRow"', box)
        self.assertIn('min="0" max="100"', box)
        self.assertIn(".bgPos { display: flex", self.html)

    def test_position_is_applied_to_mid_only_with_a_background(self):
        fn = self.html[self.html.index("function applyBgPos("):]
        fn = fn[:fn.index("\n}\n") + 3]
        self.assertIn('$("mid")', fn)
        self.assertIn('host.classList.contains("has-bg")', fn)
        self.assertIn('host.style.backgroundPosition = "center " + p + "%"', fn)
        # 缩略图跟着动，用户才知道自己调到了哪一段
        self.assertIn("objectPosition", fn)

    def test_slider_shows_only_when_there_is_an_image(self):
        fn = self.html[self.html.index("function renderBg("):]
        fn = fn[:fn.index("\n}\n") + 3]
        self.assertIn('$("bgPosRow")', fn)
        self.assertIn('row.style.display = url ? "" : "none"', fn)
        self.assertIn("applyBgPos(bgStore.read().pos, false)", fn)

    def test_store_keeps_the_position_when_the_image_changes(self):
        store = self.html[self.html.index("const bgStore = {"):]
        store = store[:store.index("\n};")]
        # 换图 / 设剧照都走 write，它必须把旧 pos 带上
        self.assertIn("pos: bgPosNum(this.read().pos)", store)
        # 老记录没有 pos 字段时按居中读，不能变成 NaN
        self.assertIn("v.pos = bgPosNum(v.pos)", store)
        self.assertIn("writePos(pos)", store)

    def test_drag_updates_style_and_only_release_persists(self):
        bind = self.html[self.html.index("(function bindBg() {"):]
        bind = bind[:bind.index("})();")]
        self.assertIn('rg.addEventListener("input"', bind)
        self.assertIn("applyBgPos(rg.value, false)", bind)
        self.assertIn('rg.addEventListener("change"', bind)
        self.assertIn("applyBgPos(rg.value, true)", bind)

    def test_pos_number_is_clamped(self):
        fn = self.html[self.html.index("function bgPosNum("):]
        fn = fn[:fn.index("\n}\n") + 3]
        self.assertIn("if (!isFinite(n)) return BG_POS_MID;", fn)
        self.assertIn("Math.min(100, Math.max(0, Math.round(n)))", fn)


class RoleplayFallbackUiTest(unittest.TestCase):
    """扮演降级链的前端（2026-11-09）：多条自定义模型 + 闭站引导 + 当前模型。

    后端那条链是「公用中转站 V4 Pro → 玩家自定义模型（有序）」，前端必须
    同时做到三件事，少一件用户就会在闭站时一头雾水：
      1. 每轮聊天把**数组** custom_models 带上，不是老的单条 custom；
      2. 后端回 relay_down 时弹引导，且弹窗口径不能有"不再回退官方 /
         赞助 / 站主不再承担费用"这类字眼；
      3. 「当前模型」按钮老实显示 model_used，别自己猜。
    """

    def setUp(self):
        self.html = _read()

    def test_request_sends_the_ordered_list_not_a_single_model(self):
        self.assertIn('const CM_KEY = "persona_custom_models"', self.html)
        self.assertEqual(self.html.count("body.custom_models = CM;"), 3,
                         "chat / skip_day / set_phase 三条支线都要带链")
        self.assertNotIn("body.custom =", self.html)
        self.assertNotIn('id="cmUrl"', self.html,
                         "老的单模型弹窗还在，玩家没法加第二条")

    def test_panel_prefills_deepseek_and_lets_players_paste_the_key(self):
        for needle in ('DeepSeek / DEP（推荐）', "https://api.deepseek.com",
                       "deepseek-flash", "https://platform.deepseek.com/api_keys",
                       "navigator.clipboard.readText", "测试连接", "保存",
                       "cmAddRow", "data-act=\"up\"", "data-act=\"del\""):
            self.assertIn(needle, self.html, "少了 %s" % needle)

    def test_help_and_local_storage_disclaimer_are_visible(self):
        for needle in ("cmHelpBtn", "创建 API Key", "充值", "粘贴",
                       "Local Storage", "我们这边拿不到你的这一 key",
                       "手机备忘录", "换浏览器"):
            self.assertIn(needle, self.html, "少了 %s" % needle)

    def test_relay_popup_uses_the_agreed_wording(self):
        for needle in ("公用的免费 V4 Pro 暂时连不上", "站主承担不起自掏腰包",
                       "12 亿的免费 token", "连续三次", "模型选择（自定义）</button>"):
            self.assertIn(needle, self.html, "少了 %s" % needle)
        for banned in ("不再回退官方", "赞助", "站主不再承担费用"):
            self.assertNotIn(banned, self.html, "弹窗不该出现 %s" % banned)

    def test_api_opens_the_popup_and_prints_the_model_used(self):
        fn = self.html[self.html.index("async function api("):]
        fn = fn[:fn.index("\n}\n") + 3]
        self.assertIn('if (d.code === "relay_down") openRelay(err.has_custom);', fn)
        self.assertIn("if (d.model_used) renderModelUsed(d.model_used);", fn)

    def test_current_model_button_sits_at_the_bottom_of_the_chat(self):
        # 位置：.log 之后、精确输入之前，才是「聊天区底部」。
        i_log = self.html.index('<div class="log" id="log">')
        i_bar = self.html.index('id="modelBar"')
        i_pre = self.html.index('id="preciseBox"')
        self.assertLess(i_log, i_bar)
        self.assertLess(i_bar, i_pre)
        self.assertIn("当前模型 ▾", self.html)
        self.assertIn('$("modelBody").classList.toggle("on")', self.html)
        self.assertIn('renderModelUsed(used)', self.html)
        self.assertIn('used.kind === "public"', self.html)

    def test_current_model_stays_legible_over_a_character_background(self):
        """2026-11-09 真机：加了角色背景图后这行是灰色小字，糊得看不清。

        .modelBar 比上面那批 `.mid.has-bg` 适配晚加了一步，默认 --text-dim
        直接压在图上。修法跟 .aff / .precise 一致：垫深底 + 提亮字 + 阴影。
        """
        for needle in (".mid.has-bg .modelBar {", ".mid.has-bg .modelBar .mn",
                       ".mid.has-bg .modelBar .btn", ".mid.has-bg .modelBody {",
                       ".mid.has-bg .modelBody b"):
            self.assertIn(needle, self.html, "背景适配少了 %s" % needle)
        i = self.html.index(".mid.has-bg .modelBar {")
        block = self.html[i:self.html.index("}", i) + 1]
        self.assertIn("background: rgba(14, 14, 18, .62)", block,
                      "只调字色压不住花背景，必须垫一层半透明深底")
        self.assertIn("color: #e2e2ec", block)
        self.assertIn("text-shadow", block)
        self.assertIn("font-size: 12px", block)


class ModelSelectionPanelTest(unittest.TestCase):
    """「模型选择（自定义）」统一面板（2026-11-10 用户要求）。

    把公用免费 V4 Pro、自己付费的 DeepSeek、玩家自定义模型放进**同一个**
    折叠列表，并给聊天区加一个「本轮用 X」强制选择器。静态体检要兜住：
      1. 四个入口（聊天区按钮 / 右栏按钮 / 面板标题 / 中转站引导按钮）
         都改名成「模型选择（自定义）」，不再是各自叫「自定义模型」；
      2. 免费行永远第一、不可拖（data-t="public"）；付费行 data-t="paid"、
         自定义行 data-t="custom"；
      3. 付费行按余额亮灭：BALANCE<=0 时 updatePaidDim 给它加 dim；
      4. 「本轮用 X」选择器存在，且发完一轮后 resetForce() 把 FORCE_MODEL 清回默认。
    """

    def setUp(self):
        self.html = _read()

    def test_all_four_entries_renamed(self):
        self.assertIn('id="btnModelCfg" type="button">模型选择（自定义）</button>',
                      self.html)
        self.assertIn('id="btnCustom">模型选择（自定义）</button>', self.html)
        self.assertIn('<h3>模型选择（自定义）</h3>', self.html)
        self.assertIn('id="relayCfg">模型选择（自定义）</button>', self.html)

    def test_force_model_selector_exists(self):
        self.assertIn('<select id="forceModel"', self.html)
        # 切换选择器要写回 FORCE_MODEL
        self.assertIn('$("forceModel").onchange', self.html)
        # 选项文案：默认 / 公用 / 付费 / 自定义
        self.assertIn('默认（走降级链）', self.html)
        self.assertIn('公用 V4 Pro（免费）', self.html)
        self.assertIn('官方 DeepSeek（代充余额）', self.html)

    def test_balance_gates_paid_option(self):
        """余额>0 才给付费选项，否则按钮 disabled（前端禁选 + 后端也会跳过）。"""
        fn = self.html[self.html.index("function renderForceOptions("):]
        fn = fn[:fn.index("\n}\n") + 3]
        self.assertIn('<option value="paid" disabled>', fn,
                      "余额 0 时付费选项必须禁用")
        self.assertIn('<option value="paid">官方 DeepSeek（代充余额）</option>', fn,
                      "余额 >0 时付费选项必须可点")

    def test_paid_row_dim_logic(self):
        """付费行按余额亮灭：BALANCE<=0 时 updatePaidDim 加 dim 类。"""
        paid = self.html[self.html.index("function cmRowPaidHtml("):]
        paid = paid[:paid.index("\n}\n") + 3]
        self.assertIn('data-t="paid"', paid)
        dim = self.html[self.html.index("function updatePaidDim("):]
        dim = dim[:dim.index("\n}\n") + 3]
        self.assertIn('toggle("dim", BALANCE <= 0)', dim)
        # 余额 0 这一句口径要写在付费行里
        self.assertIn('为 0 时这一行自动变灰', paid)

    def test_row_markup_data_t(self):
        fixed = self.html[self.html.index("function cmRowFixedHtml("):]
        fixed = fixed[:fixed.index("\n}\n") + 3]
        self.assertIn('data-t="public"', fixed, "免费行必须 data-t=public（永远第一）")
        paid = self.html[self.html.index("function cmRowPaidHtml("):]
        paid = paid[:paid.index("\n}\n") + 3]
        self.assertIn('data-t="paid"', paid)
        custom = self.html[self.html.index("function cmRowHtml("):]
        custom = custom[:custom.index("\n}\n") + 3]
        self.assertIn('data-t="custom"', custom)

    def test_reset_force_after_one_reply(self):
        """「本轮用 X」只作用下一轮：deliver 成功回复后必须调 resetForce()。"""
        d = self.html[self.html.index("async function deliver("):]
        # 截到下一个 async function 之前
        nxt = self.html.find("async function ", d.index("\n") + 1)
        d = d[:nxt - d.index("async function deliver(")] if nxt != -1 else d
        self.assertIn("resetForce();", d,
                      "deliver 里成功分支必须调 resetForce()，否则强制模型会一直粘着")
        fn = self.html[self.html.index("function resetForce("):]
        fn = fn[:fn.index("\n}\n") + 3]
        self.assertIn('FORCE_MODEL = ""', fn)


if __name__ == "__main__":
    unittest.main()
