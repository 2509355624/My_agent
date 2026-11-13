# -*- coding: utf-8 -*-
"""ANIMA 随心配（anima_random）单元测试（2026-11-13）。

只测**抽卡 + 占位符替换 + 描述**这套纯逻辑，不碰 ComfyUI / 出图链路
（那些要在线，归 e2e）。判据都是「代码只登记事实，不判断好不好」——
抽到什么就是什么，测试只验证「池子范围、数量、不重复」这些事实。
"""
import json
import os
import random
import unittest

from app import anima_random


class DrawTest(unittest.TestCase):
    """抽卡：模型不重复选 2、LoRA 不重复选 5。"""

    def test_draw_counts(self):
        d = anima_random.draw(rng=random.Random(1))
        self.assertEqual(len(d["models"]), 2)
        self.assertEqual(len(d["loras"]), anima_random.LORA_COUNT)

    def test_loras_no_duplicate(self):
        d = anima_random.draw(rng=random.Random(2))
        self.assertEqual(len(d["loras"]), len(set(d["loras"])),
                         "LoRA 池抽 5 个必须不重复")

    def test_models_in_pool(self):
        d = anima_random.draw(rng=random.Random(3))
        for m in d["models"]:
            self.assertIn(m, anima_random.MODEL_POOL)
        for l in d["loras"]:
            self.assertIn(l, anima_random.LORA_POOL)

    def test_draw_reproducible(self):
        """同种子同结果（复刻用）。"""
        a = anima_random.draw(rng=random.Random(99))
        b = anima_random.draw(rng=random.Random(99))
        self.assertEqual(a, b)

    def test_models_no_duplicate(self):
        """两个底模必须不同（去放回）。"""
        for seed in range(20):
            d = anima_random.draw(rng=random.Random(seed))
            self.assertEqual(len(set(d["models"])), 2,
                             "seed=%d 抽出了重复底模 %r" % (seed, d["models"]))

    def test_pool_sizes(self):
        """模型池 ≥ 2（不重复选 2）、LoRA 池 ≥ 5（不重复选 5）。"""
        self.assertGreaterEqual(len(anima_random.MODEL_POOL), 2)
        self.assertGreaterEqual(len(anima_random.LORA_POOL),
                                anima_random.LORA_COUNT)


class CooldownTest(unittest.TestCase):
    """冷却期：不带 rng 的生产路径要避开最近抽中的项。"""

    def setUp(self):
        anima_random.reset_history()

    def tearDown(self):
        anima_random.reset_history()

    def test_models_cooled_next_round(self):
        """上一轮抽中的两个底模，下一轮不能再出现。"""
        first = anima_random.draw()
        second = anima_random.draw()
        cooled = set(first["models"])
        self.assertTrue(set(second["models"]).isdisjoint(cooled),
                        "上一轮底模 %r 又被抽中 %r" % (cooled, second["models"]))

    def test_loras_cooled_next_round(self):
        """上一轮抽中的 LoRA，下一轮不能再出现。"""
        first = anima_random.draw()
        second = anima_random.draw()
        cooled = set(first["loras"])
        self.assertTrue(set(second["loras"]).isdisjoint(cooled),
                        "上一轮 LoRA %r 又被抽中 %r" % (cooled, second["loras"]))

    def test_models_not_repeat_within_cooldown_window(self):
        """连续 MODEL_COOLDOWN+1 轮内，同一个底模不重复出现。"""
        seen = set()
        window = anima_random.MODEL_COOLDOWN + 1
        for _ in range(window):
            d = anima_random.draw()
            for m in d["models"]:
                self.assertNotIn(m, seen, "底模 %r 在 %d 轮内重复" % (m, window))
            seen.update(d["models"])

    def test_rng_path_ignores_history(self):
        """带 rng 的调用是纯函数，不读也不写冷却历史。"""
        anima_random.draw()  # 先抽一次，把历史填上
        before = list(anima_random._history)
        anima_random.draw(rng=random.Random(7))  # 纯函数路径
        after = list(anima_random._history)
        self.assertEqual(len(before), len(after),
                         "rng 路径不应改动历史")

    def test_history_capped(self):
        """历史只保留够最长冷却的轮数，不无限增长。"""
        for _ in range(anima_random._HIST_MAX + 5):
            anima_random.draw()
        self.assertLessEqual(len(anima_random._history),
                             anima_random._HIST_MAX)


class ApplyRandomTest(unittest.TestCase):
    """占位符替换：__MODEL_*__ / __LORA_*__ 全部换成真实名，不动其它节点。"""

    def _workflow(self):
        p = os.path.join(os.path.dirname(anima_random.__file__), "..",
                         "skills", "_anima_random", "workflow.json")
        with open(p, encoding="utf-8") as f:
            return json.load(f)

    def test_placeholders_replaced(self):
        wf = self._workflow()
        d = {"models": ["m1.safetensors", "m2.safetensors"],
             "loras": ["l1.safetensors", "l2.safetensors", "l3.safetensors",
                       "l4.safetensors", "l5.safetensors"]}
        out = anima_random.apply_random(wf, d)
        s = json.dumps(out, ensure_ascii=False)
        self.assertNotIn("__MODEL_", s)
        self.assertNotIn("__LORA_", s)
        self.assertEqual(out["5"]["inputs"]["unet_name"], "m1.safetensors")
        self.assertEqual(out["36"]["inputs"]["unet_name"], "m2.safetensors")
        self.assertEqual(out["16"]["inputs"]["lora_name"], "l1.safetensors")
        self.assertEqual(out["30"]["inputs"]["lora_name"], "l5.safetensors")

    def test_seed_placeholder_untouched(self):
        """apply_random 只替换模型/LoRA，__SEED__ / __MULTI_PROMPTS__ 留给
        apply_prompt_placeholders，别在这里越界替掉。"""
        wf = self._workflow()
        d = {"models": ["m1.safetensors", "m2.safetensors"],
             "loras": ["l1.safetensors"] * 5}
        out = anima_random.apply_random(wf, d)
        s = json.dumps(out, ensure_ascii=False)
        self.assertIn("__SEED__", s)
        self.assertIn("__MULTI_PROMPTS__", s)

    def test_template_unchanged(self):
        """apply_random 在深拷贝上做，不动模板文件内容。"""
        wf = self._workflow()
        before = json.dumps(wf, sort_keys=True)
        anima_random.apply_random(wf, {"models": ["a", "b"], "loras": ["c"] * 5})
        self.assertEqual(json.dumps(wf, sort_keys=True), before)


class DescribeTest(unittest.TestCase):
    """参数描述：模型 + LoRA + seed 都在文本里。"""

    def test_describe_contains_all(self):
        d = {"models": ["miaomiaoRealskin_anima13.safetensors",
                        "silvermoonmixAnimaEvolved_v2329BTurbo.safetensors"],
             "loras": ["ANIMA_MODARE_V2.safetensors", "_cowani_epoch_10.safetensors",
                       "qaq.safetensors", "reina_v1_epoch14.safetensors",
                       "star cat-000022.safetensors"]}
        txt = anima_random.describe(d, seed=12345)
        self.assertIn("miaomiaoRealskin_anima13", txt)
        self.assertIn("ANIMA_MODARE_V2", txt)
        self.assertIn("seed：12345", txt)


if __name__ == "__main__":
    unittest.main()
