"""MOSS 阅读顺序 + 编剧模型选择（2026-11-21）。

用户两条需求：
1. 阅读顺序按「从上到下、从左到右」——格号 = 阅读顺序；5 格默认从错位的
   L0/L1（顶条+通高）换成规整 L6；格内气泡先说的在上、后说的在下。
2. MOSS 漫画台像漫画工作台一样可选编剧模型（渠道+模型两级下拉，
   选中项当链头、失败自动降级）——payload 带 provider/model，后端透传。

这些是结构/协议事实，测试只断言事实，不判内容好坏。
"""
import sys
import unittest
import inspect

sys.path.insert(0, "D:/AI/my_agent_restructure")
from app.features.comic import moss_twopass as m          # noqa: E402
from app.features.comic import moss_comic                 # noqa: E402


class _Script:
    """最小编剧结构，够 build_moss_user 用。"""

    @staticmethod
    def make(nlines=4):
        lines = [{"who": "girl", "text": t, "cell": 1}
                 for t in ("啊", "你在这", "真巧", "一起走")][:nlines]
        return {"acts": [{"n": 1, "oneshot": "雨夜", "cast": {"girl": "1girl"},
                          "lines": lines, "cells": {}, "narrs": [], "scene_text": "A door."}],
                "cast": {"girl": "1girl"},
                "cast_text": "1girl, test outfit"}


class ReadingOrderTest(unittest.TestCase):

    def test_five_grid_default_is_l0(self):
        # 2026-11-21 用户拍板：五格只跑 L0（顶条 + 右通高主格 + 左栏三小）。
        # L6「上二下三」下排每格 322×742，比例 1:2.3，人会被拉成竹竿，
        # 只在老剧本显式写 layout="L6" 时仍然认（见下面的注册测试）。
        self.assertEqual(m._layout_for(5), "L0")
        self.assertEqual(m._layout_for(5, prev_layout="L6"), "L0")

    def test_three_grid_default_is_t3(self):
        # 三格不走 L5（左通高 1:3.1 窄条），改「上大横格 + 下两格」。
        self.assertEqual(m._layout_for(3), "T3")
        self.assertEqual(m._layout_for(4), "T3")      # 四格不再产出，收成三格

    def test_t3_read_order_top_then_left_right(self):
        cells = [((b[0] + b[2]) / 2, (b[1] + b[3]) / 2) for b in m.SKELETONS["T3"]]
        self.assertEqual(len(cells), 3)
        self.assertLess(cells[0][0], cells[1][0])          # 上主格在最先
        self.assertEqual(cells[1][0], cells[2][0])         # 下两格同一排
        self.assertLess(cells[1][1], cells[2][1])          # 下排左 → 右

    def test_t3_registered_in_all_tables(self):
        self.assertIn("T3", m.LAYOUT_SLOTS)
        self.assertIn("T3", m._CELL_ROLE)
        self.assertIn("T3", m.LAYOUT_SENTENCE)
        self.assertIn("T3 ·", m.LAYOUT_TABLE)
        self.assertIn("T3", m.SKELETONS)

    def test_l6_cell_order_is_top_to_bottom_left_to_right(self):
        # 每个格取中心点 (y, x)：先按 y 分排，同排 x 递增
        cells = [((b[0] + b[2]) / 2, (b[1] + b[3]) / 2) for b in m.SKELETONS["L6"]]
        self.assertEqual(len(cells), 5)
        # 格1、2 是上排（y 小），格3、4、5 是下排（y 大）
        self.assertLess(cells[0][0], cells[2][0])
        self.assertLess(cells[1][0], cells[2][0])
        self.assertLess(cells[0][1], cells[1][1])          # 上排左→右
        self.assertLess(cells[2][1], cells[3][1])          # 下排左→中
        self.assertLess(cells[3][1], cells[4][1])          # 下排中→右

    def test_l6_registered_in_all_tables(self):
        self.assertIn("L6", m.LAYOUT_SLOTS)
        self.assertIn("L6", m._CELL_ROLE)
        self.assertIn("L6", m.LAYOUT_SENTENCE)
        self.assertIn("L6 ·", m.LAYOUT_TABLE)
        self.assertIn("L6", m.SKELETONS)

    def test_detect_layout_recognizes_l6(self):
        js = {"items": [{"label": "character", "caption": "1girl", "bbox": list(b)}
                        for b in m.SKELETONS["L6"]]}
        self.assertEqual(m.detect_layout(js), "L6")

    def test_writer_and_contract_state_reading_order(self):
        self.assertIn("格号 = 阅读顺序", m.SCRIPT_SYSTEM)
        self.assertIn("格号 = 阅读顺序", m.MOSS_CONTRACT)
        self.assertIn("从上到下、从左到右", m.SCRIPT_SYSTEM)
        self.assertIn("先说的在最上", m.MOSS_CONTRACT)

    def test_bubble_order_is_first_on_top(self):
        # autofix 文案和 build_moss_user 文案都得是「先说的在上」，不能说反
        u = m.build_moss_user(_Script.make(), 1, "", None, "L4")
        self.assertIn("第 1 句最上、第 4 句最下", u)
        self.assertNotIn("第 1 句最下", u)


class ModelChainTest(unittest.TestCase):

    def test_write_accepts_provider_and_model(self):
        sig = inspect.signature(moss_comic.write)
        self.assertIn("provider", sig.parameters)
        self.assertIn("model", sig.parameters)

    def test_repair_accepts_model(self):
        sig = inspect.signature(moss_comic.repair)
        self.assertIn("model", sig.parameters)

    def test_moss_page_wires_model_into_payload(self):
        # 前端 doWrite 必须把 provider/model 带进两个编剧入口的 payload
        html = open("D:/AI/my_agent_restructure/web/moss_studio.html",
                    encoding="utf-8").read()
        self.assertIn('id="prov"', html)
        self.assertIn('id="model"', html)
        self.assertIn("payload.provider", html)
        self.assertIn("payload.model", html)
        self.assertIn("scope=comic", html)

    def test_api_reads_provider_model_for_both_entries(self):
        src = open("D:/AI/my_agent_restructure/app/features/comic/moss_api.py",
                   encoding="utf-8").read()
        # 单次入口和双段入口都要读 model
        self.assertIn('data.get("model")', src)
        self.assertGreaterEqual(src.count('data.get("provider")'), 2)
        # _twopass_call 要把 provider/model 透传给 llm.call_llm
        self.assertIn("provider=provider", src)
        self.assertIn("model=model", src)


if __name__ == "__main__":
    unittest.main()
