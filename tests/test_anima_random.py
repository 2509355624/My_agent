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
    """抽卡：模型可重复选 2、LoRA 不重复选 5。"""

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

    def test_pool_sizes(self):
        """模型池 ≥ 2（可重复选 2）、LoRA 池 ≥ 5（不重复选 5）。"""
        self.assertGreaterEqual(len(anima_random.MODEL_POOL), 2)
        self.assertGreaterEqual(len(anima_random.LORA_POOL),
                                anima_random.LORA_COUNT)


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
