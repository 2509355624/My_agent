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
    def test_reads_named_slots_and_skips_blanks(self):
        """有名字的槽按槽号升序返回，空槽（`"None"`）不露面。

        不钉总数——用户随时会往模板里挂新 LoRA（2026-10-29 加到 11 个槽）。
        """
        slots = L.read_slots(_tpl())
        nums = [s["slot"] for s in slots]
        self.assertEqual(nums, sorted(nums))
        self.assertTrue(set(range(1, 9)) <= set(nums))
        self.assertTrue(all(s["name"] and s["name"].lower() != "none" for s in slots))

    def test_default_on_slots(self):
        """2026-10-29 用户定档的 moss 默认风格组合：模板默认只开这四槽。

        光影风格@1.5（5 号）+ moss-nai5@0.8（8 号，配触发词前缀）
        + 米山舞@0.8（10 号）+ 光润皮肤@0.8（12 号）。
        跑图台 / 漫画台 / QQ 三处都靠这份模板兜底，所以这里钉死集合。
        """
        on = {s["slot"]: s for s in L.read_slots(_tpl()) if s["on"]}
        self.assertEqual(sorted(on), [5, 8, 10, 12])
        self.assertEqual(on[5]["name"], "moss-光影风格sky02mossv1.safetensors")
        self.assertAlmostEqual(on[5]["strength"], 1.5)
        self.assertEqual(on[8]["name"], "moss-nai5.safetensors")
        self.assertAlmostEqual(on[8]["strength"], 0.8)
        self.assertEqual(on[10]["name"], "moss-米山舞.safetensors")
        self.assertAlmostEqual(on[10]["strength"], 0.8)
        self.assertEqual(on[12]["name"], "MOSS-style-光润皮肤.safetensors")
        self.assertAlmostEqual(on[12]["strength"], 0.8)

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
        over = L.SLOT_MAX + 1
        self.assertEqual(L.apply_slots(wf, [{"slot": 0, "on": True},
                                            {"slot": over, "on": True}]), 0)

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
        self.assertTrue(any("超出 1~%d" % L.SLOT_MAX in e for e in errs))

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
        """只该改被点名的字段，别的行一个字都不动（含缩进/键序）。

        请求值取「当前值的反面」——不钉模板现状（用户随时会存默认，
        模板上哪个槽开着是会变的），只钉「改动必须精确到一行」。
        """
        wf = load_workflow(self.path)
        cur = {s["slot"]: s for s in L.read_slots(wf)}
        want = not cur[2]["on"]
        before = self._text().splitlines()
        ok, msg = L.save_default(SKILL, [{"slot": 2, "on": want}])
        self.assertTrue(ok, msg)
        after = self._text().splitlines()
        diff = [ln for ln in after if ln not in before]
        self.assertEqual(len(diff), 1, diff)
        self.assertIn('"on_2": %s' % ("true" if want else "false"), diff[0])
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
        self.assertTrue(caps["editable"])       # 采样步数仍不可改（模板定死 25 步）

    def test_defaults_reads_toggle_slots(self):
        """读的是模板当前槽位，不钉总数——用户随时会挂新 LoRA。"""
        d = ro.defaults(SKILL)
        nums = [x["slot"] for x in d["loras"]]
        self.assertEqual(nums, sorted(nums))
        self.assertTrue(set(range(1, 9)) <= set(nums))

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


class Nai5PrefixTest(unittest.TestCase):
    """nai5 触发词前缀（2026-10-28 深夜）：跟 8 号槽同进同退。

    作者要求：开着 LoRA 没触发词风格出不来，关着 LoRA 留触发词是空喊。
    所以前缀不烘焙进模板，出图时按槽位状态动态拼。
    """

    def test_template_default_on(self):
        """loras=None（QQ / 漫画台）照模板——现在模板开着，必须给前缀。"""
        self.assertTrue(L.nai5_prefix("moss-qunyou").startswith("@//neoanime/"))

    def test_explicit_loras_win(self):
        """跑图台面板显式传了槽位 → 按传入状态，不看模板。"""
        on = L.nai5_prefix("moss-qunyou", [{"slot": 8, "on": False}])
        self.assertEqual(on, "")
        on = L.nai5_prefix("moss-qunyou", [{"slot": 2, "on": True}])
        self.assertTrue(on.startswith("@//neoanime/"))  # 没提 8 号槽 → 回退模板

    def test_other_skills_never_get_it(self):
        self.assertEqual(L.nai5_prefix("silver"), "")
        self.assertEqual(L.nai5_prefix("moss-wide"), "")
        self.assertEqual(L.nai5_prefix(""), "")

    def test_strip_removes_prefix_from_workflow(self):
        """显式关槽时，build 阶段按模板拼进去的前缀要能剥干净。"""
        from app.tools.normal.generate_image import build_t2i_workflow
        built = build_t2i_workflow(SKILL, "1girl, tea", 42)
        self.assertGreaterEqual(L.strip_nai5_prefix(built), 1)
        leftovers = [v for node in built.values()
                     for v in (node.get("inputs") or {}).values()
                     if isinstance(v, str) and "@//neoanime/" in v]
        self.assertEqual(leftovers, [])

    def test_build_bakes_prefix_when_template_on(self):
        """build_t2i_workflow 按模板状态拼前缀（QQ / 重抽路径都走它）。"""
        from app.tools.normal.generate_image import build_t2i_workflow
        built = build_t2i_workflow(SKILL, "1girl, tea", 42)
        baked = [v for node in built.values()
                 for v in (node.get("inputs") or {}).values()
                 if isinstance(v, str) and "@//neoanime/" in v]
        # 前缀拼在渠道固定前缀之后、用户提示词之前，整体是一串长文本，
        # 不判 startswith（固定前缀 @realskin... 在它前面）。
        self.assertEqual(len(baked), 1)
        self.assertIn("1girl, tea", baked[0])
        self.assertIn("realistic material rendering", baked[0])


if __name__ == "__main__":
    unittest.main()
