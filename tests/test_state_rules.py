# -*- coding: utf-8 -*-
"""状态原文分层（2026-11-18）的回归。

这里只校验三件事：
1. `state_rules.py` 里每一段都是原书的完整原文，没有摘要/截断；
2. 选择器只取当前档，不把低档旧原文一起塞回来；
3. 渲染层有多少命中就拼多少，不设字数上限。
"""

import pathlib
import unittest

from app.features.persona import state_inject, state_policy, state_rules

ROOT = pathlib.Path(__file__).resolve().parents[1]
GUIDE = ROOT / "skills" / "writing" / "00-state-guide" / "state-guide.md"


def _base(**kw):
    res = {
        "active": True, "value": 0, "level": 0, "arousal": 0,
        "excite": 0, "corrupt": 0, "submit": 0, "suspicion": 0,
        "dev": {k: 0 for k in ("mouth", "nipple", "pussy", "anus", "skin")},
        "cross": "", "crossN": 0,
    }
    res.update(kw)
    return res


class StateRulesSourceTest(unittest.TestCase):
    def test_every_block_is_verbatim_source(self):
        md = GUIDE.read_text(encoding="utf-8")
        self.assertTrue(state_rules.BLOCKS)
        for b in state_rules.BLOCKS:
            self.assertIn(b.text.strip(), md, "原文被改动或截断: %s" % b.key)

    def test_dev_band_does_not_carry_part_differences(self):
        band = state_rules.by_key("dev.4")
        self.assertNotIn("五处的差异", band.text)
        for part in ("mouth", "nipple", "pussy", "anus", "skin"):
            self.assertIsNotNone(state_rules.by_key("dev.part.%s" % part))


class StatePolicyTest(unittest.TestCase):
    def test_only_current_value_tier(self):
        blocks = state_policy.select_blocks(_base(value=100), {"phase": "", "contact": ""})
        keys = [b.key for b in blocks]
        self.assertIn("value.11", keys)
        for old in ("value.1", "value.2", "value.5", "value.10"):
            self.assertNotIn(old, keys)
        # 6.1 / 7.1 是单点档（0），不能被区间解析漏掉。
        blocks0 = state_policy.select_blocks(_base(), {})
        keys0 = [b.key for b in blocks0]
        self.assertIn("corrupt.1", keys0)
        self.assertIn("submit.1", keys0)

    def test_high_state_has_many_blocks_without_cap(self):
        res = _base(value=100, level=6, arousal=85, excite=80,
                    corrupt=75, submit=75,
                    dev={"mouth": 100, "nipple": 100, "pussy": 62,
                         "anus": 0, "skin": 0},
                    cross="hard", crossN=2)
        gd = {"phase": "深夜", "contact": "pussy"}
        blocks = state_policy.select_blocks(res, gd)
        self.assertGreaterEqual(len(blocks), 20)
        text = state_inject.build(res, gd)
        self.assertGreater(len(text), 1500)
        # 没有把整本 1.1 万字塞进来，也不是固定截到某个预算。
        self.assertLess(len(text), 6000)

    def test_dev_parts_keep_each_original_line(self):
        res = _base(dev={"mouth": 100, "nipple": 62, "pussy": 0,
                          "anus": 80, "skin": 40})
        blocks = state_policy.select_blocks(res, {})
        keys = [b.key for b in blocks]
        for part in ("mouth", "nipple", "pussy", "anus", "skin"):
            self.assertIn("dev.part.%s" % part, keys)

    def test_v1_only(self):
        self.assertEqual(state_inject.build(_base(schema=2)), "")
        self.assertEqual(state_inject.build({"active": False}), "")
        self.assertEqual(state_inject.build(None), "")


if __name__ == "__main__":
    unittest.main()
