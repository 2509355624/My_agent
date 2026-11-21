# -*- coding: utf-8 -*-
"""跨页状态台账 + 空间锚点（2026-11-21）。

新场景「夜归开门」暴露：女生从屋里出来后位置被画反、随身物凭空多出背包。
根因不是某个场景写错，而是逐格稿改造把「退场状态 → 下一页入场状态」这层删了；
加上互斥句把随身物件写进否定描述，否定词反而把概念请进画面。

这里钉死的是四条通用修法，不针对门、背包或任何具体场景写规则。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.features.comic import moss_twopass as m


STATE_SCRIPT = """# 夜归
## 角色
- girl: 1girl, black hair
- boy: 1boy, dark coat
## 场景 1
A front yard, a gate, a lit door, hard light.
## 页 1
格1｜远景｜院门口
画面：他背着包走到矮铁门前。
台词：无
格2｜中景｜玄关门
画面：她从门后探出身子，空着手。
台词：女「哥哥回来啦。」
状态：他在院门外，背黑包；她在门里，空手；门已经开着。
## 页 2
格1｜近景｜门口
画面：她迈下台阶，两人面对面。
台词：女「等你等好久。」
状态：两人都在院外，他背黑包，她空手。
"""


class WriterPromptTest(unittest.TestCase):
    def test_script_system_requires_spatial_anchor(self):
        self.assertIn("位置要挂靠具体参照物", m.SCRIPT_SYSTEM)
        self.assertIn("相邻两格人物的相对位置不许变", m.SCRIPT_SYSTEM)
        self.assertIn("另一个人一个字都别提这件东西", m.SCRIPT_SYSTEM)

    def test_script_system_requires_state_line(self):
        self.assertIn("状态：谁在哪 / 谁身上有什么 / 什么成了事实", m.SCRIPT_SYSTEM)
        self.assertIn("状态：他在门外街上", m.SCRIPT_SYSTEM)

    def test_brief_prompt_requires_state_line(self):
        u = m.build_script_user("他送她回家", 3)
        self.assertIn("状态：谁在哪 / 谁身上有什么 / 什么成了事实", u)
        self.assertIn("随身物照上一页的", u)


class ParseStateTest(unittest.TestCase):
    def test_state_line_parsed_into_state_out(self):
        d = m.parse_script(STATE_SCRIPT)
        self.assertEqual(d["acts"][0]["state_out"],
                         "他在院门外，背黑包；她在门里，空手；门已经开着。")
        self.assertEqual(d["acts"][1]["state_out"],
                         "两人都在院外，他背黑包，她空手。")

    def test_page_exit_state_prefers_written_state(self):
        d = m.parse_script(STATE_SCRIPT)
        self.assertEqual(m.page_exit_state(d["acts"][0]), d["acts"][0]["state_out"])

    def test_page_exit_state_falls_back_to_last_cell(self):
        act = {"cells": {1: {"visual": "他背着包走到门前。"},
                         2: {"visual": "她从门后探出身子。"}}}
        self.assertEqual(m.page_exit_state(act), "她从门后探出身子。")

    def test_page_exit_state_appends_established_fact(self):
        act = {"state_out": "他在院外，背黑包。", "fact": "门已经开着。"}
        self.assertIn("established: 门已经开着。", m.page_exit_state(act))


class CompileConstraintsTest(unittest.TestCase):
    def test_next_page_prompt_carries_state_as_hard_constraint(self):
        d = m.parse_script(STATE_SCRIPT)
        u = m.build_moss_user(d, 2, m.page_exit_state(d["acts"][0]), None, "L4")
        self.assertIn("接续与入场状态", u)
        self.assertIn("上一页退场状态（硬约束）", u)
        self.assertIn("背黑包", u)
        self.assertIn("不许凭空增减物件", u)
        self.assertIn("不许交换两人的内外位置", u)

    def test_contract_bans_props_in_exclusion_clause(self):
        self.assertIn("互斥只列外形与衣服", m.MOSS_CONTRACT)
        self.assertIn("随身物件（包 / 伞 / 杯子 / 手机……）一个字都不许写",
                      m.MOSS_CONTRACT)


if __name__ == "__main__":
    unittest.main()
