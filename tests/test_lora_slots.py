# -*- coding: utf-8 -*-
"""`MOSSLoRAToggle` 槽位读写测试（2026-10-28）。

背景：用户要求「跑图台 / 漫画台 / MOSS 台都能自己选开哪个 LoRA」。moss 的
LoRA 不是一个节点一条链，而是**一个节点里并联 10 个槽**（`on_i` / `lora_i` /
`strength_i`），所以它不能走 `comic_render_opts._rebuild_loras`——那套会删节点
重建链，把这个连着底模和采样器的节点删掉，整条链断掉。

这份测试钉死五件事：

  1. read_slots() 读的是**有名字**的槽（模板里 8~10 是 `"None"`，不该出现在界面）；
  2. apply_slots() 只改字段：节点数不变、连线不变、步数不变，槽号越界跳过；
  3. normalize() 的钳位与事实校验（槽号 1~10、强度 0~2、重名/越界报错）；
  4. save_default() 写模板：占位符必须原样留着、写后自检、坏文本要拒绝且不落盘；
  5. comic_render_opts 认得出这类渠道（caps.lora=True + lora_kind="toggle"），
     而且 apply() 走的是 toggle 分支、不是重建链。

只跑本文件：python -m unittest discover -s tests -p "test_lora_slots.py"
"""

import io
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import comic_render_opts as ro
from app import lora_slots as L
from app.skills import load_workflow

SKILL = "moss-qunyou"
TPL = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "skills", SKILL, "workflow.json")

MALIKER = "moss-maliker_moss_base_style_v3_e40.safetensors"


def _tpl():
    """每测一份新的模板 dict（load_workflow 每次都会重新读文件）。"""
    wf = load_workflow(TPL)
    assert wf, "读不到 %s" % TPL
    return wf


class ReadSlotsTest(unittest.TestCase):
    def test_reads_the_seven_named_slots(self):
        """模板里 1~7 填了名字、8~10 是 "None" → 界面只该看到 7 槽。"""
        slots = L.read_slots(_tpl())
        self.assertEqual([s["slot"] for s in slots], [1, 2, 3, 4, 5, 6, 7])

    def test_only_slot_two_is_on(self):
        """2026-10-28 的现状：10 槽只开 maliker@0.4（其余都是 on=false）。"""
        on = [s for s in L.read_slots(_tpl()) if s["on"]]
        self.assertEqual(len(on), 1)
        self.assertEqual(on[0]["slot"], 2)
        self.assertEqual(on[0]["name"], MALIKER)
        self.assertAlmostEqual(on[0]["strength"], 0.4)

    def test_no_toggle_node_means_no_slots(self):
        """别的渠道（anima-line 是 LoraLoader 链）不该被当成 toggle。"""
        wf = {"1": {"class_type": "LoraLoaderModelOnly",
                    "inputs": {"lora_name": "x.safetensors",
                               "strength_model": 0.8, "model": ["9", 0]}}}
        self.assertFalse(L.has_toggle(wf))
        self.assertEqual(L.read_slots(wf), [])


class ApplySlotsTest(unittest.TestCase):
    def test_only_touches_the_fields_it_was_given(self):
        wf = _tpl()
        before_nodes, before_steps = len(wf), wf["70"]["inputs"]["steps"]
        n = L.apply_slots(wf, [{"slot": 1, "on": True, "strength": 0.75},
                               {"slot": 2, "on": False}])
        self.assertEqual(n, 2)
        got = {s["slot"]: s for s in L.read_slots(wf)}
        self.assertTrue(got[1]["on"])
        self.assertAlmostEqual(got[1]["strength"], 0.75)
        self.assertFalse(got[2]["on"])
        # 槽 2 的强度没传 → 保持 0.4，别被默认值 1.0 覆盖
        self.assertAlmostEqual(got[2]["strength"], 0.4)
        # 结构没动：节点数、步数、连线原样
        self.assertEqual(len(wf), before_nodes)
        self.assertEqual(wf["70"]["inputs"]["steps"], before_steps)
        self.assertEqual(wf["10"]["inputs"]["model"], ["1", 0])

    def test_out_of_range_slot_is_skipped(self):
        wf = _tpl()
        self.assertEqual(L.apply_slots(wf, [{"slot": 0, "on": True},
                                            {"slot": 11, "on": True}]), 0)

    def test_strength_is_clamped(self):
        wf = _tpl()
        L.apply_slots(wf, [{"slot": 5, "strength": 9.0}])
        got = {s["slot"]: s for s in L.read_slots(wf)}
        self.assertAlmostEqual(got[5]["strength"], 2.0)

    def test_no_toggle_node_is_a_noop(self):
        wf = {"1": {"class_type": "KSampler", "inputs": {}}}
        self.assertEqual(L.apply_slots(wf, [{"slot": 1, "on": True}]), 0)


class NormalizeTest(unittest.TestCase):
    def test_keeps_slot_and_on(self):
        slots, errs, _w = L.normalize([{"slot": 2, "name": MALIKER,
                                        "strength": 0.4, "on": True}])
        self.assertEqual(errs, [])
        self.assertEqual(slots[0]["slot"], 2)
        self.assertTrue(slots[0]["on"])

    def test_slot_number_out_of_range(self):
        _s, errs, _w = L.normalize([{"slot": 99, "on": True}])
        self.assertTrue(any("超出 1~10" in e for e in errs))

    def test_duplicate_slot(self):
        _s, errs, _w = L.normalize([{"slot": 3, "on": True},
                                    {"slot": 3, "on": False}])
        self.assertTrue(any("重复提交" in e for e in errs))

    def test_name_must_exist_in_comfy_inventory(self):
        _s, errs, _w = L.normalize(
            [{"slot": 1, "name": "nope.safetensors"}],
            resources={"loras": [MALIKER]})
        self.assertTrue(any("ComfyUI 里没有" in e for e in errs))

    def test_strength_clamped_with_warning(self):
        slots, _e, warns = L.normalize([{"slot": 1, "strength": 5}])
        self.assertAlmostEqual(slots[0]["strength"], 2.0)
        self.assertTrue(any("钳到" in w for w in warns))

    def test_empty_name_means_keep(self):
        """只勾开关、不动下拉时 name 为空 → 不该写名字，只带 on。"""
        slots, errs, _w = L.normalize([{"slot": 1, "on": True}])
        self.assertEqual(errs, [])
        self.assertNotIn("name", slots[0])
        self.assertTrue(slots[0]["on"])


class SaveDefaultTest(unittest.TestCase):
    """全部在临时目录里跑，不碰真模板。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lora_slots_")
        self.skdir = os.path.join(self.tmp, SKILL)
        os.makedirs(self.skdir)
        self.path = os.path.join(self.skdir, "workflow.json")
        shutil.copyfile(TPL, self.path)
        self._orig = L.SKILLS_DIR
        L.SKILLS_DIR = self.tmp

    def tearDown(self):
        L.SKILLS_DIR = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _text(self):
        return io.open(self.path, encoding="utf-8", newline="").read()

    def test_roundtrip_keeps_placeholders_and_steps(self):
        ok, msg = L.save_default(SKILL, [
            {"slot": 1, "on": True, "strength": 0.7},
            {"slot": 2, "on": False},
        ])
        self.assertTrue(ok, msg)
        text = self._text()
        self.assertIn("__SEED__", text)          # 模板占位符一个都不能丢
        self.assertIn("__MULTI_PROMPTS__", text)
        wf = load_workflow(self.path)
        self.assertEqual(wf["70"]["inputs"]["steps"], 25)   # 步数没被顺手改掉
        got = {s["slot"]: s for s in L.read_slots(wf)}
        self.assertTrue(got[1]["on"])
        self.assertAlmostEqual(got[1]["strength"], 0.7)
        self.assertFalse(got[2]["on"])
        self.assertTrue(os.path.isfile(self.path + ".bak-lora"))

    def test_other_bytes_untouched(self):
        """只该改被点名的字段，别的行一个字都不动（含缩进/键序）。"""
        before = self._text().splitlines()
        L.save_default(SKILL, [{"slot": 2, "on": False}])
        after = self._text().splitlines()
        diff = [ln for ln in after if ln not in before]
        self.assertEqual(len(diff), 1, diff)
        self.assertIn('"on_2": false', diff[0])
        self.assertEqual(len(before), len(after))   # 没多行也没少行

    def test_missing_placeholder_is_refused(self):
        """占位符被改坏的文本一律拒绝落盘（宁可存不进去，也不能废掉模板）。"""
        text = self._text().replace("__MULTI_PROMPTS__", "")
        io.open(self.path, "w", encoding="utf-8", newline="").write(text)
        ok, msg = L.save_default(SKILL, [{"slot": 2, "on": False}])
        self.assertFalse(ok)
        self.assertIn("占位符", msg)
        self.assertNotIn("__MULTI_PROMPTS__", self._text())   # 确实没动过

    def test_broken_json_rolls_back(self):
        """写进去之后解析不了 → 回滚到原文件。"""
        good = self._text()
        # 把 on_3 所在行改成非法 JSON（少个逗号之类），让自检必然失败
        broken = good.replace('"on_3": false,', '"on_3": false')
        io.open(self.path, "w", encoding="utf-8", newline="").write(broken)
        ok, msg = L.save_default(SKILL, [{"slot": 1, "on": True}])
        self.assertFalse(ok)
        self.assertIn("回滚", msg)
        self.assertEqual(self._text(), broken)

    def test_unknown_skill(self):
        ok, msg = L.save_default("no-such-skill", [{"slot": 1, "on": True}])
        self.assertFalse(ok)
        self.assertIn("找不到模板", msg)


class ComicCapsTest(unittest.TestCase):
    """漫画台要能认出这类渠道，否则参数卡里那块 LoRA 面板永远不显示。"""

    def test_caps_mark_moss_as_toggle(self):
        caps = ro.caps(_tpl())
        self.assertTrue(caps["lora"])
        self.assertEqual(caps["lora_kind"], "toggle")
        self.assertTrue(caps["editable"])       # 采样步数仍不可改（25 步定死）

    def test_defaults_reads_toggle_slots(self):
        d = ro.defaults(SKILL)
        self.assertEqual(len(d["loras"]), 7)
        self.assertEqual([x["slot"] for x in d["loras"]], list(range(1, 8)))

    def test_apply_does_not_rebuild_chain(self):
        """toggle 渠道走 apply_slots：节点数不变（重建链的话这里会多出节点）。"""
        wf = _tpl()
        out = ro.apply(wf, {"loras": [{"slot": 1, "on": True, "strength": 0.6},
                                      {"slot": 2, "on": False}]})
        self.assertEqual(len(out), len(wf))
        got = {s["slot"]: s for s in L.read_slots(out)}
        self.assertTrue(got[1]["on"])
        self.assertFalse(got[2]["on"])
        self.assertEqual(out["70"]["inputs"]["steps"], 25)

    def test_anima_line_is_still_chain(self):
        """回归护栏：这条改动不能把 anima 那 6 槽的链式面板改没。"""
        caps = ro.caps_of("anima-line")
        self.assertTrue(caps["lora"])
        self.assertEqual(caps["lora_kind"], "chain")


if __name__ == "__main__":
    unittest.main()
