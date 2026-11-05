# -*- coding: utf-8 -*-
"""写作规范注入测试：app/writing_skills.py（2026-11-05 第一步）。

要钉住的三件事：

1. **按轴挑**：这一拍哪根轴最高，就挑哪一根的规范原文。
2. **不超预算**：拼进提示词的整段有硬上限，超了会把演员的历史挤出去。
3. **不把作者的笔记当指令**：规范库里给人看的「用户裁决」「生成前内部规划」
   不能原样进提示词——模型会照着做。
"""

import unittest

import app.persona as persona
import app.writing_skills as ws


def _state(**kw):
    base = {"active": True, "value": 0, "excite": 0,
            "corrupt": 0, "submit": 0, "suspicion": 0}
    base.update(kw)
    return base


class SliceTest(unittest.TestCase):
    """所有切片标记必须能命中——改了 skills/writing 的标题就得同步改这里。"""

    def test_every_marker_resolves(self):
        rows = [(n, r, a, b, u) for n, r, a, b, u in ws._CORE]
        for axis_rows in ws._BY_AXIS.values():
            rows += list(axis_rows)
        for name, rel, a, b, u in rows:
            with self.subTest(name=name, rel=rel):
                self.assertTrue(ws._slice(rel, a, b, u),
                                "切片为空：%s → %r" % (rel, a))

    def test_author_notes_are_stripped(self):
        """规范库里的作者裁决不能被当成指令塞给演员。"""
        for _, rel, a, b, u in ws._CORE:
            got = ws._slice(rel, a, b, u)
            self.assertNotIn("用户裁决", got)
            self.assertNotIn("已按用户裁决定稿", got)
            self.assertNotIn("生成前内部规划", got)


class AxisTest(unittest.TestCase):
    def test_picks_the_highest_axis(self):
        self.assertEqual(ws._top_axis(_state(corrupt=60, submit=10)),
                         "corrupt")
        self.assertEqual(ws._top_axis(_state(submit=75)), "submit")
        self.assertEqual(ws._top_axis(_state(suspicion=30, value=10)),
                         "suspicion")
        self.assertEqual(ws._top_axis(_state(value=60)), "affection")

    def test_suspicion_is_scaled_to_its_own_max(self):
        """戒备满值只有 40，别让一根 30 的戒备输给一根 25 的屈服。"""
        self.assertEqual(ws._top_axis(_state(suspicion=30, submit=25)),
                         "suspicion")

    def test_quiet_state_falls_back_to_intimate(self):
        """四根都不高、已经在场景里 → 用通用的肉感那套。"""
        self.assertEqual(ws._top_axis(_state(value=5, excite=40)), "intimate")

    def test_corrupt_wins_a_tie(self):
        """堕落只涨不掉，和别的轴同分时它先说话。"""
        self.assertEqual(ws._top_axis(_state(corrupt=50, submit=50)),
                         "corrupt")

    def test_negative_affection_is_not_affection(self):
        """负分档（在等你认错）不是关系好，别挑好感那一套。"""
        self.assertEqual(ws._top_axis(_state(value=-30)), "intimate")


class GuideTest(unittest.TestCase):
    def test_free_mode_gets_nothing(self):
        """自由模式没有轴，挑轴就是瞎猜。"""
        self.assertEqual(ws.guide(None), "")
        self.assertEqual(ws.guide({}), "")
        self.assertEqual(ws.guide({"active": False, "corrupt": 60}), "")

    def test_respects_the_char_budget(self):
        for st in (_state(corrupt=90), _state(submit=90), _state(value=90),
                   _state(suspicion=40), _state(excite=50)):
            with self.subTest(st=st):
                self.assertLessEqual(len(ws.guide(st)), ws.MAX_CHARS)

    def test_carries_the_dominant_axis_section(self):
        got = ws.guide(_state(submit=80))
        self.assertIn("最吃这一口", got)
        self.assertIn("权力语言", got)

    def test_missing_files_do_not_raise(self):
        """规范库被挪走时演员层要照常演。"""
        import unittest.mock as mock
        with mock.patch.object(ws, "_ROOT", "Z:/no/such/dir"):
            self.assertEqual(ws.guide(_state(corrupt=60)), "")


class PersonaWiringTest(unittest.TestCase):
    """接进 `intimate_guide()`：有状态就叠笔法层，没状态就退回原来那套。"""

    def test_guide_is_appended_when_raid_is_active(self):
        got = persona.intimate_guide(_state(excite=60, corrupt=60))
        self.assertIn("这一拍的笔法", got)
        # 原有的节奏层还在
        self.assertIn("这一拍", got)

    def test_free_mode_keeps_the_old_guide(self):
        got = persona.intimate_guide({"active": False})
        self.assertEqual(got, "")

    def test_broken_spec_never_breaks_the_turn(self):
        import unittest.mock as mock
        with mock.patch.object(persona.writing_skills, "guide",
                               side_effect=RuntimeError("坏了")):
            got = persona.intimate_guide(_state(excite=60))
        self.assertNotIn("这一拍的笔法", got)


if __name__ == "__main__":
    unittest.main()
