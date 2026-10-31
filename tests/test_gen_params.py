"""跑图台自定义 CFG / 负向提示词（app/gen_params.py）测试。

覆盖三件事：
1. CFG 取值规整（空值 / 字符串 / 布尔 / 越界）与「只改采样器上的数字」；
2. 负向节点靠 `KSampler.negative` 连线反查，不认节点编号（多段渠道共用、
   未接采样器的 CLIP 节点、压根没有 CLIP 节点的渠道都要对）；
3. 负向是**追加**——模板那四段 `<|..._neg|>` 结构化负向池必须原样留着。
"""

import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import gen_params
from app.skills import load_workflow

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _wf_two_samplers():
    """两段采样、共用同一个负向节点（hd_* / silver 的形状）。"""
    return {
        "10": {"class_type": "KSampler",
               "inputs": {"steps": 15, "cfg": 3.5, "seed": "__SEED__",
                          "positive": ["50", 0], "negative": ["51", 0]}},
        "11": {"class_type": "KSampler",
               "inputs": {"steps": 15, "cfg": 3.5,
                          "positive": ["50", 0], "negative": ["51", 0]}},
        "50": {"class_type": "CLIPTextEncode", "inputs": {"text": "__MULTI_PROMPTS__"}},
        "51": {"class_type": "CLIPTextEncode", "inputs": {"text": "lowres, bad hands"}},
        "52": {"class_type": "CLIPTextEncode", "inputs": {"text": "悬空没接采样器"}},
        "20": {"class_type": "SomeUpscale", "inputs": {"hires_steps": 10}},
    }


class CfgNormalizeTest(unittest.TestCase):
    def test_empty_means_template_default(self):
        for v in (None, "", "   "):
            self.assertEqual(gen_params.normalize_cfg(v), (None, None), repr(v))

    def test_plain_string_and_int(self):
        self.assertEqual(gen_params.normalize_cfg(3.5), (3.5, None))
        self.assertEqual(gen_params.normalize_cfg("3.5"), (3.5, None))
        self.assertEqual(gen_params.normalize_cfg(1), (1.0, None))

    def test_bool_is_not_cfg(self):
        cfg, err = gen_params.normalize_cfg(True)
        self.assertIsNone(cfg)
        self.assertIn("数字", err)

    def test_junk_and_out_of_range(self):
        self.assertIn("数字", gen_params.normalize_cfg("abc")[1])
        self.assertIn("0~20", gen_params.normalize_cfg(-0.5)[1])
        self.assertIn("0~20", gen_params.normalize_cfg(20.5)[1])

    def test_range_bounds_inclusive(self):
        self.assertEqual(gen_params.normalize_cfg(0), (0.0, None))
        self.assertEqual(gen_params.normalize_cfg(20), (20.0, None))


class CfgReadTest(unittest.TestCase):
    def test_reads_every_sampler(self):
        self.assertEqual(gen_params.read_cfg(_wf_two_samplers()), [3.5, 3.5])

    def test_describe_single_and_multi(self):
        self.assertEqual(gen_params.describe_cfg({"1": {"inputs": {"cfg": 3.0}}}), "3")
        wf = _wf_two_samplers()
        wf["11"]["inputs"]["cfg"] = 1.0
        self.assertEqual(gen_params.describe_cfg(wf), "3.5+1")

    def test_describe_without_cfg_is_empty(self):
        self.assertEqual(gen_params.describe_cfg({"1": {"inputs": {"steps": 8}}}), "")
        self.assertEqual(gen_params.describe_cfg(None), "")


class CfgApplyTest(unittest.TestCase):
    def test_sets_every_sampler(self):
        wf = _wf_two_samplers()
        self.assertEqual(gen_params.apply_cfg(wf, 5.0), 2)
        self.assertEqual(wf["10"]["inputs"]["cfg"], 5.0)
        self.assertEqual(wf["11"]["inputs"]["cfg"], 5.0)

    def test_leaves_links_placeholders_and_other_fields_alone(self):
        wf = _wf_two_samplers()
        gen_params.apply_cfg(wf, 5.0)
        self.assertEqual(wf["10"]["inputs"]["steps"], 15)          # 步数不动
        self.assertEqual(wf["10"]["inputs"]["seed"], "__SEED__")  # 占位符不动
        self.assertEqual(wf["10"]["inputs"]["negative"], ["51", 0])  # 连线不动
        self.assertEqual(wf["51"]["inputs"]["text"], "lowres, bad hands")  # 负向不动
        self.assertEqual(wf["20"]["inputs"]["hires_steps"], 10)

    def test_cfg_wired_by_link_is_skipped(self):
        wf = {"1": {"class_type": "KSampler",
                    "inputs": {"cfg": ["7", 0], "positive": ["2", 0],
                               "negative": ["3", 0]}}}
        self.assertEqual(gen_params.apply_cfg(wf, 5.0), 0)
        self.assertEqual(wf["1"]["inputs"]["cfg"], ["7", 0])

    def test_same_value_counts_zero(self):
        wf = {"1": {"inputs": {"cfg": 3.0}}}
        self.assertEqual(gen_params.apply_cfg(wf, 3), 0)

    def test_bad_value_is_ignored(self):
        wf = {"1": {"inputs": {"cfg": 3.0}}}
        self.assertEqual(gen_params.apply_cfg(wf, 99), 0)
        self.assertEqual(wf["1"]["inputs"]["cfg"], 3.0)


class NegativeNodeTest(unittest.TestCase):
    def test_found_by_link_not_by_number(self):
        """负向节点是 #51，但换到别处编号变了也得找得到。"""
        wf = _wf_two_samplers()
        wf["90"] = wf.pop("51")
        wf["10"]["inputs"]["negative"] = ["90", 0]
        wf["11"]["inputs"]["negative"] = ["90", 0]
        self.assertEqual([nid for nid, _ in gen_params.negative_nodes(wf)], ["90"])

    def test_shared_by_two_samplers_counts_once(self):
        self.assertEqual(len(gen_params.negative_nodes(_wf_two_samplers())), 1)

    def test_unlinked_clip_node_is_ignored(self):
        ids = [nid for nid, _ in gen_params.negative_nodes(_wf_two_samplers())]
        self.assertNotIn("52", ids)

    def test_channel_without_clip_has_no_negative(self):
        p = os.path.join(ROOT, "skills", "qwen-charsheet", "workflow.json")
        if not os.path.exists(p):
            self.skipTest("没有 qwen-charsheet 模板")
        self.assertFalse(gen_params.has_negative(load_workflow(p)))

    def test_two_distinct_negatives_both_written(self):
        wf = {"1": {"inputs": {"positive": ["2", 0], "negative": ["3", 0]}},
              "2": {"inputs": {"text": "p"}},
              "3": {"inputs": {"text": "a"}},
              "4": {"class_type": "KSampler",
                    "inputs": {"positive": ["2", 0], "negative": ["5", 0]}},
              "5": {"inputs": {"text": "b"}}}
        self.assertEqual(gen_params.apply_negative(wf, "extra"), 2)
        self.assertEqual(wf["3"]["inputs"]["text"], "a, extra")
        self.assertEqual(wf["5"]["inputs"]["text"], "b, extra")


class NegativeApplyTest(unittest.TestCase):
    def test_append_keeps_template_pool(self):
        wf = _wf_two_samplers()
        self.assertEqual(gen_params.apply_negative(wf, "extra hands"), 1)
        self.assertEqual(wf["51"]["inputs"]["text"],
                         "lowres, bad hands, extra hands")

    def test_empty_template_gets_no_leading_comma(self):
        wf = {"1": {"inputs": {"positive": ["2", 0], "negative": ["3", 0]}},
              "2": {"inputs": {"text": "p"}},
              "3": {"inputs": {"text": ""}}}
        gen_params.apply_negative(wf, "blurry")
        self.assertEqual(wf["3"]["inputs"]["text"], "blurry")

    def test_blank_and_none_do_nothing(self):
        wf = _wf_two_samplers()
        for v in (None, "", "   "):
            self.assertEqual(gen_params.apply_negative(wf, v), 0, repr(v))
        self.assertEqual(wf["51"]["inputs"]["text"], "lowres, bad hands")

    def test_too_long_is_rejected(self):
        text, err = gen_params.normalize_negative("x" * (gen_params.NEG_MAX + 1))
        self.assertIsNone(text)
        self.assertIn("太长", err)

    def test_positive_is_untouched(self):
        wf = _wf_two_samplers()
        gen_params.apply_negative(wf, "extra")
        self.assertEqual(wf["50"]["inputs"]["text"], "__MULTI_PROMPTS__")


class SizeNormalizeTest(unittest.TestCase):
    def test_empty_means_template_default(self):
        self.assertEqual(gen_params.normalize_size(None, None), (None, None, ""))
        self.assertEqual(gen_params.normalize_size("", ""), (None, None, ""))

    def test_only_one_side_is_error(self):
        for w, h in ((1536, None), (None, 2048), (1536, ""), ("", 2048)):
            size, err, _note = gen_params.normalize_size(w, h)
            self.assertIsNone(size, repr((w, h)))
            self.assertIn("整数", err)

    def test_strings_and_floats(self):
        self.assertEqual(gen_params.normalize_size("1536", "2048")[0],
                         (1536, 2048))
        self.assertEqual(gen_params.normalize_size(1536.0, 2048.0)[0],
                         (1536, 2048))

    def test_aligns_down_to_multiple_of_eight(self):
        size, err, note = gen_params.normalize_size(1530, 2050)
        self.assertIsNone(err)
        self.assertEqual(size, (1528, 2048))
        self.assertIn("8 的倍数", note)

    def test_clamped_into_range(self):
        size, err, note = gen_params.normalize_size(100, 5000)
        self.assertIsNone(err)
        self.assertEqual(size, (256, 4096))
        self.assertIn("256~4096", note)

    def test_junk_and_non_positive(self):
        self.assertIsNone(gen_params.normalize_size("abc", 2048)[0])
        self.assertIn("大于 0", gen_params.normalize_size(0, 2048)[1])
        self.assertIn("大于 0", gen_params.normalize_size(-8, 2048)[1])

    def test_bool_is_not_a_size(self):
        self.assertIsNone(gen_params.normalize_size(True, 2048)[0])


class SizeReadTest(unittest.TestCase):
    def test_reads_latent_size(self):
        wf = {"60": {"class_type": "EmptyFlux2LatentImage",
                     "inputs": {"width": 1024, "height": 1536}},
              "61": {"class_type": "EmptyLatentImage",
                     "inputs": {"width": 1536, "height": 1024}}}
        self.assertEqual(gen_params.read_size(wf), (1024, 1536))
        self.assertEqual(gen_params.describe_size(wf), "1024×1536")

    def test_channel_without_latent(self):
        self.assertEqual(gen_params.read_size({"1": {"inputs": {}}}),
                         (None, None))
        self.assertEqual(gen_params.describe_size(None), "")


class SizeApplyTest(unittest.TestCase):
    def _wf(self):
        return {"60": {"class_type": "EmptyFlux2LatentImage",
                       "inputs": {"width": 1024, "height": 1536,
                                  "batch_size": 1}},
                "70": {"class_type": "KSampler",
                       "inputs": {"steps": 25, "cfg": 3.0, "seed": "__SEED__"}}}

    def test_sets_latent_size(self):
        wf = self._wf()
        self.assertEqual(gen_params.apply_size(wf, 1536, 2048), 1)
        self.assertEqual(wf["60"]["inputs"]["width"], 1536)
        self.assertEqual(wf["60"]["inputs"]["height"], 2048)

    def test_leaves_other_inputs_and_samplers_alone(self):
        wf = self._wf()
        gen_params.apply_size(wf, 1536, 2048)
        self.assertEqual(wf["60"]["inputs"]["batch_size"], 1)
        self.assertEqual(wf["70"]["inputs"]["steps"], 25)
        self.assertEqual(wf["70"]["inputs"]["seed"], "__SEED__")

    def test_hires_stage_follows(self):
        wf = {"40": {"class_type": "EmptyLatentImage",
                     "inputs": {"width": 720, "height": 1280,
                                "enable_hires": True,
                                "hires_width": 1440, "hires_height": 2560}}}
        self.assertEqual(gen_params.apply_size(wf, 832, 1216), 1)
        self.assertEqual(wf["40"]["inputs"]["hires_width"], 832)
        self.assertEqual(wf["40"]["inputs"]["hires_height"], 1216)

    def test_same_value_counts_zero(self):
        wf = self._wf()
        self.assertEqual(gen_params.apply_size(wf, 1024, 1536), 0)

    def test_bad_value_is_ignored(self):
        wf = self._wf()
        self.assertEqual(gen_params.apply_size(wf, 0, 0), 0)
        self.assertEqual(wf["60"]["inputs"]["width"], 1024)


class RealMossTemplateTest(unittest.TestCase):
    """真模板：moss-qunyou 当前是单段 cfg 3 + `#51` 四段结构化负向池。"""

    def setUp(self):
        self.path = os.path.join(ROOT, "skills", "moss-qunyou", "workflow.json")
        if not os.path.exists(self.path):
            self.skipTest("没有 moss-qunyou 模板")

    def test_cfg_and_negative_read(self):
        wf = load_workflow(self.path)
        self.assertEqual(gen_params.read_cfg(wf), [3.0])
        self.assertEqual(gen_params.describe_cfg(wf), "3")
        self.assertTrue(gen_params.has_negative(wf))
        self.assertIn("<|pose_and_clothing_neg|>", gen_params.read_negative(wf))

    def test_size_read_and_apply(self):
        wf = load_workflow(self.path)
        self.assertEqual(gen_params.read_size(wf), (1024, 1536))
        self.assertEqual(gen_params.describe_size(wf), "1024×1536")
        self.assertEqual(gen_params.apply_size(wf, 1536, 2048), 1)
        self.assertEqual(gen_params.describe_size(wf), "1536×2048")
        self.assertEqual(wf["50"]["inputs"]["text"], "__MULTI_PROMPTS__")

    def test_apply_keeps_structured_pool_and_placeholders(self):
        wf = load_workflow(self.path)
        self.assertEqual(gen_params.apply_cfg(wf, 3.5), 1)
        self.assertEqual(gen_params.apply_negative(wf, "extra fingers"), 1)
        neg = gen_params.read_negative(wf)
        # 四段池一段都不能少，用户那段追加在最后
        for tag in ("pose_and_clothing_neg", "texture_and_style_neg",
                    "age_and_body_neg", "universal_anatomy_neg"):
            self.assertIn("<|%s|>" % tag, neg)
            self.assertIn("<|/%s|>" % tag, neg)
        self.assertTrue(neg.endswith(", extra fingers"))
        # 正向占位符与种子占位符不受影响
        self.assertEqual(wf["50"]["inputs"]["text"], "__MULTI_PROMPTS__")
        with open(self.path, encoding="utf-8") as f:
            raw = f.read()
        self.assertIn("__SEED__", raw)
        self.assertIn("__MULTI_PROMPTS__", raw)
        self.assertTrue(re.search(r"__[A-Z_]+__", raw))


if __name__ == "__main__":
    unittest.main()
