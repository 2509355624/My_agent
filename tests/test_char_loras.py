# -*- coding: utf-8 -*-
"""角色 LoRA 库（app/char_loras.py）。

只测机制，不测 Sunna 这个具体条目（库内容会变）：
匹配、文件缺失跳过、链头插桩（含两段采样双底模结构）、模板零改动、
build_t2i_workflow 集成（触发词进 prompt）。
"""

import copy
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from app import char_loras


ENTRY = {"key": "sunna", "name": "苏娜",
         "file": "char-test-lora.safetensors",
         "trigger": "sunna_test", "strength": 0.8,
         "aliases": ["sunna", "苏娜"]}


def _wf():
    """迷你两段采样工作流：两段用**不同底模**（anima-line 实测结构）。

    一段：5(anima13) → 16(lora) → 2 KSampler
    二段：35(另一个底模) → 27 KSampler（本来就不挂 LoRA，不该被动）
    """
    return {
        "5": {"class_type": "UNETLoader",
              "inputs": {"unet_name": "anima13.safetensors",
                         "weight_dtype": "default"}},
        "35": {"class_type": "UNETLoader",
               "inputs": {"unet_name": "turbo.safetensors",
                          "weight_dtype": "default"}},
        "16": {"class_type": "LoraLoaderModelOnly",
               "inputs": {"model": ["5", 0],
                          "lora_name": "fixed.safetensors",
                          "strength_model": 0.35}},
        "2": {"class_type": "KSampler",
              "inputs": {"model": ["16", 0], "seed": 1}},
        "27": {"class_type": "KSampler",
               "inputs": {"model": ["35", 0], "seed": 2}},
    }


class CharLoraTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.loras_dir = os.path.join(self.tmp, "loras")
        os.makedirs(self.loras_dir)
        self.skills_dir = os.path.join(self.tmp, "skills")
        self.idx_dir = os.path.join(self.skills_dir, "anima-line")
        os.makedirs(self.idx_dir)
        # 假 LoRA 文件（只查存在性，不解析）
        with open(os.path.join(self.loras_dir, ENTRY["file"]), "wb") as f:
            f.write(b"\x00" * 16)
        self._env = None

    def tearDown(self):
        if self._env is not None:
            self._env.stop()

    def _start(self, chars=None, skills=("anima-line",)):
        """把模块指向临时目录。chars=None 表示不写索引文件。"""
        if chars is not None:
            with open(os.path.join(self.idx_dir, "char_loras.json"),
                      "w", encoding="utf-8") as f:
                json.dump({"characters": chars}, f, ensure_ascii=False)
        p = mock.patch.multiple(
            char_loras,
            CHAR_LORA_SKILLS=frozenset(skills),
            _loras_dirs=mock.Mock(return_value=[self.loras_dir]),
            _index_path=mock.Mock(
                side_effect=lambda s: os.path.join(self.idx_dir,
                                                   "char_loras.json")))
        self._env = p
        p.start()

    # ── 匹配 ──────────────────────────────────────────────
    def test_match_hits_alias_case_insensitive(self):
        self._start([ENTRY])
        for text in ("sunna (zenless zone zero), 1girl",
                     "SUNNA, black hair",
                     "画苏娜"):
            self.assertEqual(char_loras.match("anima-line", text)["key"],
                             "sunna")

    def test_match_miss_and_disabled_skill(self):
        self._start([ENTRY])
        self.assertIsNone(char_loras.match("anima-line", "1girl, black hair"))
        # 渠道没开 → 即使名字命中也不干预
        self.assertIsNone(char_loras.match("silver", "sunna"))

    def test_missing_file_skips_entry(self):
        self._start([dict(ENTRY, file="不存在的文件.safetensors")])
        self.assertIsNone(char_loras.match("anima-line", "sunna"))

    def test_no_index_or_bad_json_is_noop(self):
        self._start()                                   # 不写索引
        self.assertEqual(char_loras.entries("anima-line"), [])
        self.assertIsNone(char_loras.match("anima-line", "sunna"))
        with open(os.path.join(self.idx_dir, "char_loras.json"),
                  "w", encoding="utf-8") as f:
            f.write("{坏的")
        self.assertEqual(char_loras.entries("anima-line"), [])

    # ── 装填 ──────────────────────────────────────────────
    def test_patch_inserts_at_lora_chain_head(self):
        self._start([ENTRY])
        wf = _wf()
        out = char_loras.patch(wf, ENTRY)
        # 新节点吃 UNETLoader(5)，链头 16 改接角色节点
        self.assertEqual(out["char_lora"]["inputs"]["model"], ["5", 0])
        self.assertEqual(out["char_lora"]["inputs"]["lora_name"],
                         ENTRY["file"])
        self.assertEqual(out["16"]["inputs"]["model"], ["char_lora", 0])
        # 二段（35 → 27）一根不动：另一个底模，本来就不挂 LoRA
        self.assertEqual(out["27"]["inputs"]["model"], ["35", 0])
        # 模板零改动
        self.assertEqual(wf, _wf())

    def test_patch_no_lora_nodes_falls_back_to_model_source(self):
        self._start([ENTRY])
        wf = _wf()
        del wf["16"]                                    # 拆掉唯一 LoRA
        wf["2"]["inputs"]["model"] = ["5", 0]
        out = char_loras.patch(wf, ENTRY)
        self.assertEqual(out["char_lora"]["inputs"]["model"], ["5", 0])
        self.assertEqual(out["2"]["inputs"]["model"], ["char_lora", 0])

    def test_patch_without_model_source_is_noop(self):
        self._start([ENTRY])
        wf = {"4": {"class_type": "CLIPTextEncode",
                    "inputs": {"text": "x", "clip": ["6", 0]}}}
        self.assertEqual(char_loras.patch(wf, ENTRY), wf)


class BuildT2IIntegrationTest(unittest.TestCase):
    """build_t2i_workflow 集成：命中 → 触发词进 prompt + 节点进工作流。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.loras_dir = os.path.join(self.tmp, "loras")
        os.makedirs(self.loras_dir)
        self.idx_dir = os.path.join(self.tmp, "skills", "anima-line")
        os.makedirs(self.idx_dir)
        with open(os.path.join(self.loras_dir, ENTRY["file"]), "wb") as f:
            f.write(b"\x00" * 16)
        with open(os.path.join(self.idx_dir, "char_loras.json"),
                  "w", encoding="utf-8") as f:
            json.dump({"characters": [ENTRY]}, f, ensure_ascii=False)

    def _wf(self):
        wf = _wf()
        wf["6"] = {"class_type": "CLIPLoader",
                   "inputs": {"clip_name": "q.safetensors",
                              "type": "stable_diffusion"}}
        wf["4"] = {"class_type": "CLIPTextEncode",
                   "inputs": {"text": "__MULTI_PROMPTS__",
                              "clip": ["6", 0]}}
        wf["2"]["inputs"]["seed"] = "__SEED__"
        return wf

    def test_hit_adds_node_and_trigger(self):
        from app.tools.normal import generate_image as gi
        wf = self._wf()
        with mock.patch.object(char_loras, "_loras_dirs",
                               return_value=[self.loras_dir]), \
             mock.patch.object(char_loras, "_index_path",
                               side_effect=lambda s: os.path.join(
                                   self.idx_dir, "char_loras.json")), \
             mock.patch.object(gi, "load_skill",
                               return_value={"workflow": wf}):
            out = gi.build_t2i_workflow("anima-line", "sunna, smile", 42)
        self.assertIn("char_lora", out)
        self.assertEqual(out["2"]["inputs"]["seed"], 42)     # 种子正常替换
        self.assertIn("sunna_test", out["4"]["inputs"]["text"])
        self.assertIn("sunna", out["4"]["inputs"]["text"])

    def test_miss_leaves_workflow_untouched(self):
        from app.tools.normal import generate_image as gi
        wf = self._wf()
        with mock.patch.object(char_loras, "_loras_dirs",
                               return_value=[self.loras_dir]), \
             mock.patch.object(char_loras, "_index_path",
                               side_effect=lambda s: os.path.join(
                                   self.idx_dir, "char_loras.json")), \
             mock.patch.object(gi, "load_skill",
                               return_value={"workflow": wf}):
            out = gi.build_t2i_workflow("anima-line", "1girl, smile", 7)
        self.assertNotIn("char_lora", out)
        self.assertNotIn("sunna_test", out["4"]["inputs"]["text"])
        # 模板没被 patch 污染
        self.assertEqual(wf["16"]["inputs"]["model"], ["5", 0])


if __name__ == "__main__":
    unittest.main()
