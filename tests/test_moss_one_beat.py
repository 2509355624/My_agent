# -*- coding: utf-8 -*-
"""一格 = 一个定格：换装、递衣这类连续动作不许把过程压进一格。

背景（2026-11-21，实跑「操场换衣」5 页）：
模型把"脱→递→接→穿"写成"连贯展示"，于是一格里同时出现动作和动作的结果——
P4 左下格写 "a soaked white shirt …, her fingers gathering the soaked white shirt
into a tight ball"，出图就是身上穿着一件、手里还团着一件；P5 右通高格写
"…both hands catching the thrown fresh shirt, one arm already pulled through a
sleeve, the hem still lifted"，同一格叠了三个时间点。

漫画不是动画：一格是读者一眼看完的一个瞬间，中间帧不画，留给格与格之间的空白。

这三条判据都是封闭句式，不是衣物词表准入——命中即事实，改法唯一（拆格 /
改成确定的瞬间）。漏检只是少提示一次，不会误伤稿子；这里正反两个方向都锁。
"""
import re
import sys
import unittest

sys.path.insert(0, ".")

from app.features.comic import moss_agent as A


def _flags(caption):
    """返回这一格命中的判据名（与 audit 里的顺序一致）。"""
    cc = caption.lower()
    if re.search(A._MIDSTATE, cc):
        return "mid"
    ph = A._cloth_phrases(cc)
    if [x for x in set(ph) if ph.count(x) > 1 and x]:
        return "dup"
    if len(re.findall(A._PHASE, cc)) >= 2:
        return "phase"
    return ""


class OneBeatPerPanel(unittest.TestCase):
    def test_actual_bad_panels_are_caught(self):
        """实跑稿子里这四格的原文，必须被报出来。"""
        cases = [
            # P4 上主格：手指勾着下摆往上拉，露肩——动作画到一半
            ("her fingers hooked in the hem of a soaked white short-sleeve sport shirt "
             "pulling it up over one shoulder, one bare shoulder and collarbone already out",
             "mid"),
            # P4 左下格：衣着和"团成球的那件"是同一串，穿一件拿一件
            ("a soaked white short-sleeve sport shirt and loose white running shorts, "
             "her fingers gathering the soaked white short-sleeve sport shirt into a tight ball",
             "dup"),
            # P5 右通高格：接住 + 手臂已穿进袖子 + 下摆还提着
            ("a fresh dry white short-sleeve sport shirt and loose white running shorts, "
             "both hands catching the thrown fresh shirt against her chest, "
             "one arm already pulled through a sleeve, the hem still lifted",
             "mid"),
            # P5 左中格：正在往下拉
            ("close-up on her hands pulling the fresh dry white short-sleeve sport shirt "
             "down over her chest, one shoulder caught with her elbow crossing the body",
             "mid"),
        ]
        for cap, want in cases:
            with self.subTest(cap=cap[:50]):
                self.assertEqual(_flags(cap), want)

    def test_clean_panels_stay_clean(self):
        """前几页和合法写法不许被误报——误报会白烧一轮重修。"""
        cases = [
            # P3 上格：洗完脸直起身，只是衣着陈述
            "a soaked white short-sleeve sport shirt clinging to her, loose white running "
            "shorts, straightening up from the washbasin, water dripping from her chin",
            # P3 下格：手抬到一半僵住，是定格不是半脱
            "her face from the front, a hand lifted halfway and frozen",
            # 合法：穿内搭，湿衣团在手里——一件在身上、一件在手里，两件都写清
            "wearing a plain white underlayer and the soaked shirt balled up in her right hand",
            # 合法：穿旧衣，手里是新衣（修饰不同，不是同一件）
            "wearing a soaked white track shirt, a fresh dry white shirt held in her left hand",
            # 合法：递出去，衣服已离手（转移动作本身就是明确的归属）
            "close-up on the wet shirt balled up and flung across the frame toward the viewer, "
            "one sleeve caught between her fingertips at the left edge",
            # 合法的拉伸页：动词带的是脚踝不是衣服
            "her right hand pulling her right ankle up behind her thigh",
        ]
        for cap in cases:
            with self.subTest(cap=cap[:50]):
                self.assertEqual(_flags(cap), "")

    def test_midstate_list_does_not_ban_plain_halfway(self):
        """裸的 halfway 是合法定格（"手抬到一半僵住"），只有 half-off 才算半脱。"""
        self.assertIsNone(re.search(A._MIDSTATE, "a hand lifted halfway and frozen"))
        self.assertIsNotNone(re.search(A._MIDSTATE, "her shirt half-off her shoulder"))

    def test_prompt_demands_one_state_per_panel(self):
        """规则本身在不在：删了这节，模型会退回去写"连贯过程"。"""
        s = A.SYSTEM
        self.assertIn("一格 = 一个定格", s)
        self.assertIn("翻页 = 状态重置", s)
        self.assertIn("格数 = 状态数", s)
        # 扔出去的东西不许回到手上：实跑时 P3 已 flung out of frame，
        # P4 又写 crushed in both hands，湿衣倒退了
        self.assertIn("状态只许往前走", s)
        # 骨架选法必须说明 L0 不是时间线，否则五格页还会塞进换装过程
        self.assertIn("L0 装的是同一时刻的不同侧面", s)
        # 旧规则里的「半脱」是这次事故的直接来源，不许回潮
        self.assertNotIn("写清动作和半脱状态", s)
        self.assertIn("脱下来那一格写「已经脱掉」", s)


if __name__ == "__main__":
    unittest.main()
