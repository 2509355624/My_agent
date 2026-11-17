# -*- coding: utf-8 -*-
"""角色卡页的手机端守卫。

2026-11-17：原来左栏固定 260px，手机上右侧表单只剩 130px；字段说明
也全部展开。改成：角色列表横向条 + 表单四个页内分区 + 说明默认收起。
"""

import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGE = os.path.join(ROOT, "web", "characters.html")


def _html():
    with open(PAGE, encoding="utf-8") as f:
        return f.read()


class CharactersMobileTest(unittest.TestCase):
    def test_mobile_uses_single_column_and_horizontal_card_strip(self):
        html = _html()
        start = html.index("@media (max-width: 760px)")
        block = html[start:]
        self.assertIn(".body { flex-direction: column; }", block)
        self.assertIn("overflow-x: auto;", block)
        self.assertIn("max-height: 96px;", block)

    def test_form_is_split_into_four_in_page_tabs(self):
        html = _html()
        self.assertIn('id="charTabs"', html)
        for tab in ("basic", "look", "state", "other"):
            self.assertIn('data-ctab="%s"' % tab, html)
        self.assertIn('data-csec="basic"', html)
        self.assertIn('data-csec="look"', html)
        self.assertIn('data-csec="state"', html)
        self.assertIn('data-csec="other"', html)
        self.assertIn('pickCharTab("basic")', html)
        self.assertIn("#pane .field[data-csec].on", html)

    def test_attributes_are_real_and_not_stray_text(self):
        html = _html()
        self.assertNotIn('class="field"> data-csec', html)
        self.assertNotIn('class="row"> data-csec', html)

    def test_tips_are_hidden_until_asked(self):
        html = _html()
        self.assertIn('.wrap:not(.show-tips) .field .tip', html)
        self.assertIn('id="btnTips"', html)
        self.assertIn('function toggleTips()', html)
        self.assertIn('$("btnTips").onclick = toggleTips;', html)

    def test_all_original_fields_and_ids_are_kept(self):
        html = _html()
        for element_id in (
                "btnNew", "btnSave", "status", "list", "pane", "btnTips",
                "f_name", "f_avatar", "f_charName", "f_desc", "f_personality",
                "f_system", "f_appearance", "f_outfit", "f_room",
                "f_sb_submit", "f_sb_corrupt", "f_sb_affection", "f_sb_suspicion",
                "f_start_level", "f_start_value", "f_level_names", "f_ref",
                "f_hidden", "f_preg", "f_bg_file"):
            self.assertIn('id="%s"' % element_id, html)


if __name__ == "__main__":
    unittest.main()
