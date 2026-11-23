# -*- coding: utf-8 -*-
"""漫画工作台「阶段向导」前端结构守卫（2026-11-23）。

面试反馈「前端太复杂、程序员思维」后做的第一块样板：把 44 个按钮按
「写剧本 → 确认分镜 → 出图调整」三步分层，高级控件收进 details，
加专家模式一键全展开。这里钉住结构，防止后续改动把平铺又带回来。
"""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGE = os.path.join(ROOT, "web", "comic_studio.html")


def _html():
    with open(PAGE, encoding="utf-8") as f:
        return f.read()


class ComicStudioWizardTest(unittest.TestCase):
    def test_three_stage_panes(self):
        html = _html()
        self.assertIn('class="stage-pane active" data-pane="1"', html)
        self.assertIn('class="stage-pane" data-pane="2"', html)
        self.assertIn('class="stage-pane" data-pane="3"', html)
        # 三个 pane 都有关闭注释，防止嵌套错位
        self.assertEqual(html.count("/stage-pane"), 3)

    def test_step_navigation_has_three_steps(self):
        html = _html()
        self.assertIn('id="stepbar"', html)
        for label in ("① 写剧本", "② 确认分镜", "③ 出图与调整"):
            self.assertIn(label, html)
        self.assertEqual(len(re.findall(r'class="step[^"]*" data-pane=', html)), 3)

    def test_expert_mode_toggle(self):
        html = _html()
        self.assertIn('id="expertMode"', html)
        self.assertIn("专家模式", html)
        self.assertIn("function setExpert(on)", html)
        # 专家模式会把两个 details 都打开（含折叠的高级控件）
        self.assertIn("d1.open = EXPERT", html)
        self.assertIn("d2.open = EXPERT", html)

    def test_advanced_controls_are_collapsed(self):
        html = _html()
        self.assertIn('id="advancedSetup"', html)
        self.assertIn('id="renderAdvanced"', html)
        # 关键高级控件确实在折叠区里（用 details 起点到对应 summary 后的一段判断）
        adv = html.split('id="advancedSetup"', 1)[1].split("</details>", 1)[0]
        for cid in ("sceneSkill", "prov", "model", "prevTask", "povMode"):
            self.assertIn('id="%s"' % cid, adv)
        render = html.split('id="renderAdvanced"', 1)[1].split("</details>", 1)[0]
        self.assertIn('id="renderCard"', render)

    def test_primary_button_outside_details(self):
        html = _html()
        # 生成剧本是第①步的主行动，必须留在折叠区外面
        adv = html.split('id="advancedSetup"', 1)[1].split("</details>", 1)[0]
        self.assertNotIn('id="btnScript"', adv)
        self.assertIn('id="btnScript"', html)

    def test_stage_functions_and_auto_advance(self):
        html = _html()
        self.assertIn("function setStage(n)", html)
        self.assertIn("function setExpert(on)", html)
        # 三个流程节点会自动进对应步骤
        self.assertIn("setStage(2)", html)   # 剧本生成后
        self.assertIn("setStage(3)", html)   # 开跑 / 写真出来后
        self.assertIn("setStage(ta.value.trim() ? 2 : 1)", html)  # 载入任务

    def test_dev_notes_removed(self):
        html = _html()
        self.assertNotIn("切过去只是打开它，不改内容", html)

    def test_all_original_ids_kept(self):
        """重排不能丢任何功能 id。"""
        html = _html()
        for cid in ("taskPick", "sceneSkill", "brief", "heroine", "character", "panels",
                    "prov", "model", "btnAddProv", "prevTask", "carryOutfit", "povMode",
                    "skipSearch", "silverMode", "btnScript", "script", "silverParts",
                    "fixPanel", "chatBox", "chatInput", "btnChat", "btnChatUndo",
                    "scriptOps", "btnRefix", "btnRerun", "btnSave", "exclusiveOn",
                    "btnStart", "portraitCard", "portraitGrid", "renderCard",
                    "suffixCard", "slotCard", "jobbar", "btnResume", "btnAbort",
                    "bubbleAllRow", "btnBubbleAll", "btnWatermark", "btnZip", "grid"):
            self.assertIn('id="%s"' % cid, html, "丢了 id=%s" % cid)

    def test_inline_onclick_handlers_kept(self):
        html = _html()
        for fn in ("genScript", "saveScript", "chatSend", "chatUndo", "refix",
                   "rerunAll", "abortAll", "resumeJob", "bubbleAll", "openWatermark"):
            self.assertIn("onclick=\"%s(" % fn, html, "丢了 onclick=%s" % fn)


if __name__ == "__main__":
    unittest.main()
