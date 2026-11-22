# -*- coding: utf-8 -*-
"""moss 漫画台的首屏守卫。

2026-11-17：原来整页等宽字体、技术参数 pill、两行编剧说明和 LoRA 槽位
一起占首屏。改成系统无衬线 + 短 pill + 说明折叠 + LoRA 默认收起。
"""

import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGE = os.path.join(ROOT, "web", "moss_studio.html")


def _html():
    with open(PAGE, encoding="utf-8") as f:
        return f.read()


class MossStudioSimpleTest(unittest.TestCase):
    def test_uses_product_sans_not_monospace_everywhere(self):
        html = _html()
        self.assertNotIn('"SF Mono", "JetBrains Mono"', html)
        self.assertIn('"Segoe UI", "PingFang SC"', html)

    def test_tech_pill_is_short(self):
        html = _html()
        self.assertIn('<span class="pill">一页一次生成</span>', html)

    def test_long_hint_is_folded(self):
        html = _html()
        self.assertIn('<details class="note">', html)
        self.assertIn("<summary>说明</summary>", html)
        self.assertIn("编剧只写「这一格画什么」", html)

    def test_lora_is_folded_but_still_there(self):
        html = _html()
        self.assertIn('<details class="card adv" id="lora-card">', html)
        self.assertIn('<div id="lora-rows"></div>', html)
        self.assertIn('id="btn-lora-save"', html)

    def test_mobile_stacks_editor_and_canvas(self):
        html = _html()
        start = html.index("@media (max-width: 640px)")
        block = html[start:]
        self.assertIn(".headgrid { grid-template-columns: 1fr; }", block)
        self.assertIn(".panel .trow { grid-template-columns: 1fr; }", block)
        self.assertIn("input, textarea, select { font-size: 16px; }", block)

    def test_running_progress_shows_in_page_placeholder(self):
        """画图进度挪进正在画的那页格子（2026-11-22），全局条不再重复播报。"""
        html = _html()
        self.assertIn("it.n === p.running.page", html)
        self.assertIn("画图中 · 已等", html)
        self.assertNotIn("正在画第", html)

    def test_original_ids_are_kept(self):
        html = _html()
        for element_id in (
                # 2026-11-22：一句话剧情拆成「男主 / 女主 / 剧情」三栏，
                # brief 退役，换成 male / female / plot。
                "male", "female", "plot",
                "layout", "style", "pages", "btn-write",
                "spec-card", "spec-title", "spec-style", "cast-list", "issues",
                "btn-save", "btn-draw-all", "lora-card", "lora-rows",
                "btn-lora-save", "pages-wrap", "zoom", "toast", "histsel",
                "jobbar", "outline", "outlineTip", "outlineCount"):
            self.assertIn('id="%s"' % element_id, html)

    def test_each_page_editor_folds_by_default(self):
        """2026-11-22：手机端每页默认只留页头+图，编辑区点开才露。"""
        html = _html()
        self.assertIn('fold.className = "edit-fold";', html)
        self.assertIn("fold.innerHTML = \"<summary>编辑这一页（场景 / 格子 / 台词 / 提示词）</summary>\";", html)
        # 包的是整个编辑器 editor，不是只包 panels：场景框也一起收起
        fold_at = html.index('fold.className = "edit-fold";')
        chunk = html[fold_at:fold_at + 500]
        self.assertIn("fold.appendChild(ed);", chunk)
        self.assertIn("body.appendChild(fold);", chunk)
        # 图片 canvas 不进折叠层，仍常显
        self.assertIn("body.appendChild(cv);", html)
        # 不记状态：刷新全部收起
        self.assertIn("不用 localStorage 记状态", html)
        self.assertIn("details.edit-fold > summary", html)

    def test_one_chain_and_one_style(self):
        """2026-11-22：双段勾选框与自定义画风退役，下拉只留一种画风。"""
        html = _html()
        for gone in ("twoAgent", "twoHint", "styleCustom", "/api/moss/write"):
            self.assertNotIn(gone, html)
        self.assertIn('var r = await api("/api/moss/two_agent", payload);', html)
        style = html[html.index('<select id="style"'):]
        style = style[:style.index("</select>")]
        self.assertEqual(style.count("<option"), 1)
        self.assertIn('<option value="daily" selected>日常明亮</option>', style)

    def test_outline_drives_pages_and_brief(self):
        """分集大纲：填了就按行数当页数、并拼进 brief；留空不改变旧行为。"""
        html = _html()
        self.assertIn("function parseOutline()", html)
        self.assertIn("function outlineBlock()", html)
        # 页数跟随大纲：outline.length 优先于手填的 pages
        self.assertIn("outline.length ? Math.max.apply", html)
        # 两条链路都把大纲拼进 brief
        self.assertIn("if (outline) g.push(outline);", html)
        self.assertIn("if (outline) lines.push(outline);", html)

    def test_zoom_has_navigation_and_blocks_blue_selection(self):
        html = _html()
        for element_id in ("zprev", "znext", "zpage"):
            self.assertIn('id="%s"' % element_id, html)
        self.assertIn("function zoomCollect()", html)
        self.assertIn("function zoomShow(i)", html)
        # 蓝屏 = 浏览器把图片当文本选中。四条 CSS 加 dragstart 拦截，缺一就会复发。
        for rule in ("user-select: none", "-webkit-user-select: none",
                     "-webkit-user-drag: none", "-webkit-touch-callout: none",
                     "-webkit-tap-highlight-color: transparent"):
            self.assertIn(rule, html)
        self.assertIn('addEventListener("dragstart"', html)
        self.assertIn('e.key === "ArrowLeft"', html)
        self.assertIn('e.key === "ArrowRight"', html)
        self.assertIn('e.key === "Escape"', html)


if __name__ == "__main__":
    unittest.main()
