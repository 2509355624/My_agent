"""SD 两个渠道：单段直出（默认）与两遍高清（点名才用）。

为什么单独一套用例：2026-09-27 用户嫌 SD 慢，实测估算后把「544×960 采 30 步 →
放大 → 1080×1920 再采 20 步」（57.2 MP-步）砍成单段 832×1216 采 30 步
（30.4 MP-步）。这个数是算出来的，不是拍的——**有人把它改回去就会重新变慢**，
所以这里按「像素×步数」锁一道开销上限当护栏。

零网络、零显卡：只读 skills 目录里的 workflow.json。
"""

import os
import unittest

from app.agent_prompt import _build_skill_list
from app.skills import list_skills, load_skill

# 旧参数（544×960@30 + 1080×1920@20）的开销，改回去就会超这条线。
_OLD_COST = 57.2
# 两个渠道各自的开销上限：旧的七折，留一点余量给微调。
_COST_CAP = 40.0


def _gen(name):
    """取那个自定义生成节点的输入（全部参数都在它身上）。"""
    wf = load_skill(name)["workflow"]
    for node in wf.values():
        if node.get("class_type") == "BatchPromptImageGenerator":
            return node["inputs"]
    raise AssertionError(name + " 里没有 BatchPromptImageGenerator")


def _cost(inp):
    """采样开销 ≈ 像素 × 步数（正比于耗时，够用来比快慢）。"""
    mp = inp["width"] * inp["height"] / 1e6
    total = mp * inp["steps"]
    if inp.get("enable_hires"):
        total += (inp["hires_width"] * inp["hires_height"] / 1e6
                  * inp["hires_steps"])
    return total


class ChannelExistsTest(unittest.TestCase):
    def test_both_are_scanned(self):
        names = list_skills()
        self.assertIn("image_gen_v1", names)
        self.assertIn("image_gen_v1_hires", names)

    def test_both_have_a_character_base(self):
        """两个都是「带底模」渠道——固定角色底模是 SD 这一系的卖点。"""
        for name in ("image_gen_v1", "image_gen_v1_hires"):
            data = load_skill(name)
            self.assertIsNotNone(data, name)
            self.assertEqual(data["kind"], "生图", name)
            self.assertTrue((data.get("character") or "").strip(), name)


class SinglePassTest(unittest.TestCase):
    """默认渠道：单段直出，快。"""

    def test_hires_is_off(self):
        self.assertFalse(_gen("image_gen_v1")["enable_hires"])

    def test_resolution_is_832x1216(self):
        inp = _gen("image_gen_v1")
        self.assertEqual(inp["width"], 832)
        self.assertEqual(inp["height"], 1216)

    def test_plain_euler_sampler(self):
        """2026-09-27 从 dpmpp_2m_sde_gpu/karras 换成普通 euler——SDE 型每步更慢。"""
        inp = _gen("image_gen_v1")
        self.assertEqual(inp["sampler_name"], "euler")
        self.assertEqual(inp["scheduler"], "normal")

    def test_cost_is_half_of_the_old_two_pass(self):
        cost = _cost(_gen("image_gen_v1"))
        self.assertLess(cost, _COST_CAP)
        self.assertLess(cost, _OLD_COST * 0.6, "应当比旧参数省四成以上")


class HiresPassTest(unittest.TestCase):
    """高清渠道：两遍，但目标尺寸已经降下来了。"""

    def test_hires_is_on_and_refines_only(self):
        inp = _gen("image_gen_v1_hires")
        self.assertTrue(inp["enable_hires"])
        self.assertLess(inp["hires_denoise"], 0.5)   # 精修，不是重画

    def test_hires_target_is_below_the_old_1080(self):
        """旧参数跑 1080×1920 太慢——这是用户点名要降的。"""
        inp = _gen("image_gen_v1_hires")
        self.assertEqual(inp["hires_width"], 896)
        self.assertEqual(inp["hires_height"], 1600)
        self.assertLess(inp["hires_width"] * inp["hires_height"],
                        1080 * 1920)

    def test_base_is_upscaled_not_the_same_size(self):
        """底图必须小于高清目标——否则那遍「放大」没有意义。"""
        inp = _gen("image_gen_v1_hires")
        self.assertLess(inp["width"], inp["hires_width"])
        self.assertLess(inp["height"], inp["hires_height"])

    def test_cost_is_capped_too(self):
        self.assertLess(_cost(_gen("image_gen_v1_hires")), _COST_CAP)

    def test_same_sampler_as_the_default_channel(self):
        """两个渠道用同一套采样器/CFG——差别只在「几遍、多大」。"""
        a, b = _gen("image_gen_v1"), _gen("image_gen_v1_hires")
        self.assertEqual(a["sampler_name"], b["sampler_name"])
        self.assertEqual(a["scheduler"], b["scheduler"])
        self.assertEqual(a["cfg"], b["cfg"])


class TradeoffTest(unittest.TestCase):
    """高清版存在的意义是「更大」，代价是「更慢」——这个关系别反了。"""

    def test_hires_outputs_more_pixels(self):
        a, b = _gen("image_gen_v1"), _gen("image_gen_v1_hires")
        pa = a["width"] * a["height"]
        pb = b["hires_width"] * b["hires_height"]
        self.assertGreater(pb, pa)

    def test_hires_costs_more(self):
        self.assertGreater(_cost(_gen("image_gen_v1_hires")),
                           _cost(_gen("image_gen_v1")))


class VisibilityTest(unittest.TestCase):
    def test_qq_whitelist_includes_both(self):
        from app import agents
        for name in ("image_gen_v1", "image_gen_v1_hires"):
            self.assertTrue(agents.allows_skill("qq", name), name)

    def test_skill_list_marks_hires_as_named_only(self):
        """「点名才用」要写在 **hires 自己那一行**上，不能只断言整块里有「点名」。"""
        lines = [l for l in _build_skill_list("qq").splitlines()
                 if "**image_gen_v1_hires**" in l]
        self.assertEqual(len(lines), 1)
        self.assertIn("点名", lines[0])

    def test_descriptions_tell_the_model_not_to_auto_pick(self):
        from app.tools.normal.generate_image import tool
        from app.config import QQ_AGENT_ID
        for desc in (tool["description"],
                     tool["description_overrides"][QQ_AGENT_ID]):
            self.assertIn("image_gen_v1_hires", desc)
            self.assertIn("点名", desc)


if __name__ == "__main__":
    unittest.main()
