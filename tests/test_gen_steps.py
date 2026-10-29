"""跑图台自定义步数（app/gen_steps.py）测试。

覆盖：取值规整（空值 / 字符串 / 越界 / 布尔）、模板步数读取（单段与多段）、
覆盖只改 `steps` 整数字段（不动 hires_steps、不动连线、不动占位符）。
"""

import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import gen_steps
from app.skills import load_workflow


def _wf_two_samplers():
    return {
        "10": {"class_type": "KSampler",
               "inputs": {"steps": 8, "cfg": 3.5, "seed": "__SEED__"}},
        "11": {"class_type": "KSampler",
               "inputs": {"steps": 12, "cfg": 3.5}},
        "20": {"class_type": "KSamplerAdvanced",
               "inputs": {"steps": ["10", 1], "noise_seed": 1}},
        "30": {"class_type": "SomeUpscale",
               "inputs": {"hires_steps": 10, "steps_link": 3}},
    }


class NormalizeTest(unittest.TestCase):
    def test_empty_means_template_default(self):
        for v in (None, "", "   "):
            self.assertEqual(gen_steps.normalize(v), (None, None), repr(v))

    def test_plain_and_string_int(self):
        self.assertEqual(gen_steps.normalize(30), (30, None))
        self.assertEqual(gen_steps.normalize("50"), (50, None))

    def test_bool_is_not_steps(self):
        steps, err = gen_steps.normalize(True)
        self.assertIsNone(steps)
        self.assertIn("整数", err)

    def test_junk_and_out_of_range(self):
        self.assertIn("整数", gen_steps.normalize("abc")[1])
        self.assertIn("1~100", gen_steps.normalize(0)[1])
        self.assertIn("1~100", gen_steps.normalize(101)[1])
        self.assertIn("1~100", gen_steps.normalize(120)[1])

    def test_range_bounds_inclusive(self):
        self.assertEqual(gen_steps.normalize(1), (1, None))
        self.assertEqual(gen_steps.normalize(100), (100, None))


class ReadTest(unittest.TestCase):
    def test_reads_only_integer_steps(self):
        self.assertEqual(gen_steps.read_steps(_wf_two_samplers()), [8, 12])

    def test_describe_single_value(self):
        wf = {"1": {"inputs": {"steps": 30}}}
        self.assertEqual(gen_steps.describe(wf), "30")

    def test_describe_multi_stage(self):
        self.assertEqual(gen_steps.describe(_wf_two_samplers()), "8+12")

    def test_describe_without_steps_is_empty(self):
        self.assertEqual(gen_steps.describe({"1": {"inputs": {"cfg": 1}}}), "")
        self.assertEqual(gen_steps.describe(None), "")


class ApplyTest(unittest.TestCase):
    def test_sets_every_integer_steps(self):
        wf = _wf_two_samplers()
        self.assertEqual(gen_steps.apply_steps(wf, 50), 2)
        self.assertEqual(wf["10"]["inputs"]["steps"], 50)
        self.assertEqual(wf["11"]["inputs"]["steps"], 50)

    def test_leaves_links_hires_and_other_fields_alone(self):
        wf = _wf_two_samplers()
        gen_steps.apply_steps(wf, 50)
        self.assertEqual(wf["10"]["inputs"]["cfg"], 3.5)      # 别的字段不动
        self.assertEqual(wf["10"]["inputs"]["seed"], "__SEED__")  # 占位符不动
        self.assertEqual(wf["20"]["inputs"]["steps"], ["10", 1])  # 连线不动
        self.assertEqual(wf["30"]["inputs"]["hires_steps"], 10)   # 二次放大不动

    def test_same_value_counts_zero(self):
        wf = {"1": {"inputs": {"steps": 30}}}
        self.assertEqual(gen_steps.apply_steps(wf, 30), 0)
        self.assertEqual(wf["1"]["inputs"]["steps"], 30)

    def test_bad_value_is_ignored(self):
        wf = {"1": {"inputs": {"steps": 30}}}
        self.assertEqual(gen_steps.apply_steps(wf, 0), 0)
        self.assertEqual(wf["1"]["inputs"]["steps"], 30)

    def test_reads_and_writes_the_real_moss_template(self):
        """真模板：moss-qunyou 目前单段 25 步（2026-10-29 定档）；改完占位符必须还在。"""
        p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "skills", "moss-qunyou", "workflow.json")
        wf = load_workflow(p)
        self.assertEqual(gen_steps.read_steps(wf), [25])
        self.assertEqual(gen_steps.apply_steps(wf, 45), 1)
        self.assertEqual(gen_steps.describe(wf), "45")
        raw = open(p, encoding="utf-8").read()
        self.assertIn("__SEED__", raw)
        self.assertIn("__MULTI_PROMPTS__", raw)
        self.assertTrue(re.search(r"__[A-Z_]+__", raw))


if __name__ == "__main__":
    unittest.main()
