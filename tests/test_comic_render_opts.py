# -*- coding: utf-8 -*-
"""渲染参数覆盖测试（2026-10-13）：按任务的 LoRA 槽 / 两段采样 / 分辨率。

背景：用户要「6 个 LoRA 槽位都能手动换（有时挂角色 LoRA）、采样步数可改、
分辨率可改」。参数按任务存 render.json，开跑时套在**这一次提交的工作流副本**
上，skill 模板不动。这份测试钉死四件事：

  1. defaults() 从 anima-line 模板读出的就是界面该显示的当前值（6 槽按加载
     顺序、两段采样参数、1024×1536）；
  2. apply() 重建 LoRA 链——换槽 / 摘槽 / 全摘都重连对，且**第二段采样的
     model 连线绝不能被扫到**（它挂另一个底模，这是本模块最大的坑）；
  3. normalize() 的钳位与事实校验（宽高 8 的倍数、名单里的名字才收）；
  4. render.json 存取往返 + 读坏不炸（退回默认值照常出图）。

只跑本文件：python -m unittest discover -s tests -p "test_comic_render_opts.py"
"""

import copy
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import comic, comic_render_opts as ro
from app.tools.normal import generate_image as gi


def _fresh_workflow():
    """一份刚建好的 anima-line 工作流（seed 占位符已填），每测一份新的。"""
    return gi.build_t2i_workflow("anima-line", "1girl, solo, test", 12345)


def _samplers(wf):
    """{base: [id...], hires: [id...]}（按 latent 来源认段，同模块内部逻辑）。"""
    out = {"base": [], "hires": []}
    for nid, nd in wf.items():
        if (nd or {}).get("class_type") != "KSampler":
            continue
        lat = (nd.get("inputs") or {}).get("latent_image")
        src = lat[0] if isinstance(lat, list) and lat else None
        ct = ((wf.get(src) or {}).get("class_type")) if src else None
        if ct == "EmptyLatentImage":
            out["base"].append(nid)
        elif ct in ("LatentUpscaleBy", "LatentUpscale"):
            out["hires"].append(nid)
    return out


def _lora_chain(wf, base_id):
    """从采样器往前回溯 LoRA 链，反转为**加载顺序**返回。"""
    chain, cur, seen = [], (wf[base_id]["inputs"] or {}).get("model"), set()
    while isinstance(cur, list) and cur and str(cur[0]) not in seen:
        seen.add(str(cur[0]))
        nd = wf.get(cur[0]) or {}
        if nd.get("class_type") not in ro.LORA_CLASSES:
            return list(reversed(chain)), str(cur[0])
        inp = nd["inputs"]
        chain.append((inp.get("lora_name"), inp.get("strength_model")))
        cur = inp.get("model")
    return list(reversed(chain)), None


class DefaultsTest(unittest.TestCase):
    """界面没存过参数时显示的「当前值」= anima-line 模板现状。"""

    def test_reads_six_slots_in_load_order(self):
        d = ro.defaults("anima-line")
        names = [s["name"] for s in d["loras"]]
        self.assertEqual(len(names), 6)
        # 加载顺序（靠近底模的在前）：detailer0.35 → star cat → MODARE →
        # cowani → detailer0.8 → reina。detailer 出现两次是模板本身的设计。
        self.assertEqual(names[0], "anima_context_detailer_base10.safetensors")
        self.assertEqual(names[1], "star cat-000022.safetensors")
        self.assertEqual(names[4], "anima_context_detailer_base10.safetensors")
        self.assertEqual(names[5], "reina_v1_epoch14.safetensors")
        self.assertEqual([s["strength"] for s in d["loras"]],
                         [0.35, 0.35, 0.3, 0.8, 0.8, 0.8])

    def test_reads_two_passes_and_size(self):
        d = ro.defaults("anima-line")
        self.assertEqual(d["base"], {"steps": 20, "cfg": 1,
                                     "sampler_name": "er_sde",
                                     "scheduler": "simple"})
        self.assertEqual(d["hires"], {"steps": 20, "cfg": 1,
                                      "sampler_name": "euler",
                                      "scheduler": "simple",
                                      "denoise": 0.25})
        self.assertEqual((d["width"], d["height"]), (1024, 1536))


class ApplyTest(unittest.TestCase):
    """套参数：改副本、不动入参；LoRA 链重建不波及第二段。"""

    def test_apply_does_not_mutate_input(self):
        wf = _fresh_workflow()
        before = copy.deepcopy(wf)
        ro.apply(wf, ro.defaults("anima-line"))
        self.assertEqual(wf, before)

    def test_replace_and_drop_slots(self):
        wf = _fresh_workflow()
        opts = ro.defaults("anima-line")
        opts["loras"][2] = {"name": "my_char_v1.safetensors", "strength": 0.9}
        opts["loras"][4] = {"name": "", "strength": 0.8}     # 摘掉
        out = ro.apply(wf, opts)
        passes = _samplers(out)
        chain, head = _lora_chain(out, passes["base"][0])
        self.assertEqual(chain, [
            ("anima_context_detailer_base10.safetensors", 0.35),
            ("star cat-000022.safetensors", 0.35),
            ("my_char_v1.safetensors", 0.9),
            ("_cowani_epoch_10.safetensors", 0.8),
            ("reina_v1_epoch14.safetensors", 0.8)])
        # 链头还是第一段自己的底模（miaomiao），没被换成别的加载器
        self.assertEqual(head, "5")
        self.assertEqual(out[head]["class_type"], "UNETLoader")

    def test_hires_pass_is_never_rewired(self):
        """最大的坑：第二段挂的是另一个底模，LoRA 重建不能把它的 model
        改指到链尾（comfy_workflow._rebuild_lora 按 dict 顺序认 generator，
        anima-line 的 27 在 2 前面，直接用就会踩这个坑）。"""
        wf = _fresh_workflow()
        passes = _samplers(wf)
        self.assertEqual(passes["base"], ["2"])
        self.assertEqual(passes["hires"], ["27"])
        opts = ro.defaults("anima-line")
        opts["loras"][0] = {"name": "x.safetensors", "strength": 1.0}
        out = ro.apply(wf, opts)
        hm = out["27"]["inputs"]["model"]
        self.assertEqual(out[hm[0]]["class_type"], "UNETLoader")
        self.assertEqual(out[hm[0]]["inputs"]["unet_name"],
                         "silvermoonmixAnimaEvolved_v2329BTurbo.safetensors")

    def test_all_slots_dropped_falls_back_to_base_model(self):
        wf = _fresh_workflow()
        opts = ro.defaults("anima-line")
        for s in opts["loras"]:
            s["name"] = ""
        out = ro.apply(wf, opts)
        self.assertEqual(out["2"]["inputs"]["model"], ["5", 0])
        self.assertFalse([n for n in out
                          if out[n]["class_type"] in ro.LORA_CLASSES])

    def test_params_and_size_land_on_the_right_nodes(self):
        wf = _fresh_workflow()
        opts = ro.defaults("anima-line")
        opts["base"].update(steps=28, cfg=2.0, sampler_name="dpmpp_2m",
                            scheduler="karras")
        opts["hires"].update(steps=12, denoise=0.4)
        opts["width"], opts["height"] = 960, 1280
        out = ro.apply(wf, opts)
        b, h = out["2"]["inputs"], out["27"]["inputs"]
        self.assertEqual((b["steps"], b["cfg"], b["sampler_name"], b["scheduler"]),
                         (28, 2.0, "dpmpp_2m", "karras"))
        self.assertEqual((h["steps"], h["denoise"]), (12, 0.4))
        # 没给的字段保持模板原值（cfg/采样器不动第二段没让改的部分）
        self.assertEqual(h["cfg"], 1)
        self.assertEqual(h["sampler_name"], "euler")
        lat = [n for n in out if out[n]["class_type"] == "EmptyLatentImage"][0]
        self.assertEqual((out[lat]["inputs"]["width"], out[lat]["inputs"]["height"]),
                         (960, 1280))

    def test_param_only_change_keeps_chain_nodes(self):
        """只改步数时 LoRA 链原样（不重建节点，省得白换一批节点 id）。"""
        wf = _fresh_workflow()
        opts = ro.defaults("anima-line")
        opts["base"]["steps"] = 30
        out = ro.apply(wf, opts)
        names = sorted(out[n]["inputs"]["lora_name"] for n in out
                       if out[n]["class_type"] in ro.LORA_CLASSES)
        orig = sorted(wf[n]["inputs"]["lora_name"] for n in wf
                      if wf[n]["class_type"] in ro.LORA_CLASSES)
        self.assertEqual(names, orig)
        self.assertEqual(out["2"]["inputs"]["steps"], 30)


class NormalizeTest(unittest.TestCase):
    """钳位（给人填的表，不拒）与事实校验（这填得不对，存不进去）。"""

    def test_clamps_out_of_range_numbers(self):
        opts, errors, warnings = ro.normalize({
            "loras": [{"name": "a.safetensors", "strength": 9}],
            "base": {"steps": 999}, "hires": {"denoise": 5},
            "width": 99992, "height": 8,
        })
        self.assertEqual(errors, [])
        self.assertEqual(opts["loras"][0]["strength"], 2.0)
        self.assertEqual(opts["base"]["steps"], 150)
        self.assertEqual(opts["hires"]["denoise"], 1.0)
        self.assertEqual((opts["width"], opts["height"]), (4096, 256))
        self.assertTrue(warnings)

    def test_width_must_be_multiple_of_eight(self):
        _opts, errors, _w = ro.normalize({"width": 1001})
        self.assertTrue(any("8 的倍数" in e for e in errors))

    def test_unknown_names_rejected_only_with_a_list(self):
        # 有清单（ComfyUI 在线）→ 名字必须是事实
        _o, errors, _w = ro.normalize(
            {"loras": [{"name": "nope.safetensors", "strength": 1}]},
            resources={"loras": ["a.safetensors"]})
        self.assertTrue(any("没有 LoRA" in e for e in errors))
        _o, errors, _w = ro.normalize(
            {"base": {"sampler_name": "deis"}},
            resources={"samplers": ["euler"]})
        self.assertTrue(any("采样器" in e for e in errors))
        # 没清单 → 不猜，照收（写错的后果是那一格渲染失败、明说在哪）
        _o, errors, _w = ro.normalize(
            {"loras": [{"name": "nope.safetensors", "strength": 1}]})
        self.assertEqual(errors, [])

    def test_partial_update_keeps_other_fields(self):
        base = ro.defaults("anima-line")
        opts, _e, _w = ro.normalize({"base": {"steps": 8}}, base=base)
        self.assertEqual(opts["base"]["steps"], 8)
        self.assertEqual(opts["base"]["sampler_name"], "er_sde")
        self.assertEqual(len(opts["loras"]), 6)
        self.assertEqual((opts["width"], opts["height"]), (1024, 1536))


class StoreTest(unittest.TestCase):
    """render.json 存取：往返一致、读坏不炸、reset 回到默认。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="render_opts_")

    def test_roundtrip_and_reset(self):
        opts = ro.defaults("anima-line")
        opts["loras"][0] = {"name": "char_x.safetensors", "strength": 0.75}
        opts["base"]["steps"] = 24
        opts["width"], opts["height"] = 896, 1152
        ro.save(self.tmp, opts)
        back = ro.load(self.tmp, "anima-line")
        self.assertEqual(back["loras"][0]["name"], "char_x.safetensors")
        self.assertEqual(back["base"]["steps"], 24)
        self.assertEqual((back["width"], back["height"]), (896, 1152))
        self.assertTrue(ro.reset(self.tmp))
        self.assertFalse(os.path.isfile(ro.path_of(self.tmp)))
        self.assertEqual(ro.load(self.tmp, "anima-line"),
                         ro.defaults("anima-line"))

    def test_broken_file_falls_back_to_defaults(self):
        with open(ro.path_of(self.tmp), "w", encoding="utf-8") as f:
            f.write("{not json")
        self.assertEqual(ro.load(self.tmp, "anima-line"),
                         ro.defaults("anima-line"))

    def test_hand_edited_out_of_range_is_clamped_not_fatal(self):
        with open(ro.path_of(self.tmp), "w", encoding="utf-8") as f:
            json.dump({"base": {"steps": 9999}, "width": 7}, f)
        opts = ro.load(self.tmp, "anima-line")   # 不抛，钳位后照常出图
        self.assertEqual(opts["base"]["steps"], 150)


class RunIntegrationTest(unittest.TestCase):
    """comic.run 开跑时读一次 render.json，整批用同一套（跑到一半改参数不会
    这批用新、下批用旧）。四个入口（确认开跑 / 换种子重跑 / 跑这一格 / 改画面词）
    都走 run，所以这条链路通了就全通。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="render_opts_run_")
        self.script = os.path.join(self.tmp, "script.md")
        with open(self.script, "w", encoding="utf-8") as f:
            f.write("# 标题：t\n\n## 01\n画面: 1girl, solo\n气泡: 一\n标签1: 甲\n")
        opts = ro.defaults("anima-line")
        opts["loras"][0] = {"name": "char_x.safetensors", "strength": 0.75}
        opts["base"]["steps"] = 24
        opts["width"], opts["height"] = 896, 1152
        ro.save(self.tmp, opts)

    def _run(self, **kw):
        with mock.patch.object(comic, "_require_comfy"), \
             mock.patch.object(comic, "render_scene") as m:
            comic.run(self.script, silver_only=True, **kw)
        return m

    def test_saved_opts_reach_render_scene(self):
        m = self._run()
        opts = m.call_args.kwargs.get("opts")
        self.assertEqual(opts["loras"][0]["name"], "char_x.safetensors")
        self.assertEqual(opts["base"]["steps"], 24)
        self.assertEqual((opts["width"], opts["height"]), (896, 1152))

    def test_no_render_json_means_template_defaults(self):
        os.remove(ro.path_of(self.tmp))
        m = self._run()
        opts = m.call_args.kwargs.get("opts")
        self.assertEqual(opts, ro.defaults("anima-line"))

    def test_nai_channel_does_not_read_opts(self):
        # NAI 的参数在别人机器上，run 不该把本机覆盖塞给它
        m = self._run(scene_skill="nai")
        self.assertIsNone(m.call_args.kwargs.get("opts"))


class RenderSceneWiringTest(unittest.TestCase):
    """端到端：render_scene 现场读 render.json → 套进工作流 → 提交的那个
    dict 里就是新参数（LoRA 换了、步数改了、尺寸改了）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="render_opts_wire_")
        self.script = os.path.join(self.tmp, "script.md")
        with open(self.script, "w", encoding="utf-8") as f:
            f.write("# 标题：t\n\n## 01\n画面: 1girl, solo\n气泡: 一\n标签1: 甲\n")
        opts = ro.defaults("anima-line")
        opts["loras"][0] = {"name": "char_x.safetensors", "strength": 0.9}
        opts["base"]["steps"] = 26
        opts["width"], opts["height"] = 960, 1280
        ro.save(self.tmp, opts)
        meta, panels = comic.parse_script(self.script)
        self.task = comic.Task(self.script, meta, panels)
        self.panel = self.task.panels[0]

    def test_overrides_reach_the_submitted_workflow(self):
        with mock.patch.object(comic, "_submit",
                               return_value="Anima_x.png") as sub, \
             mock.patch.object(comic, "_download") as dl, \
             mock.patch.object(comic, "_new_seed", return_value=42):
            comic.render_scene(self.task, self.panel)      # opts=None → 现场读
        wf = sub.call_args.args[0]
        chain = [(n["inputs"]["lora_name"], n["inputs"]["strength_model"])
                 for n in wf.values()
                 if n.get("class_type") in ro.LORA_CLASSES]
        # 第 1 槽 detailer@0.35 被换成 char_x@0.9，其余原样（含第 5 槽那个
        # detailer@0.8——模板里它出现两次，别误判成没换干净）
        self.assertEqual(chain, [
            ("char_x.safetensors", 0.9),
            ("star cat-000022.safetensors", 0.35),
            ("ANIMA_MODARE_V2.safetensors", 0.3),
            ("_cowani_epoch_10.safetensors", 0.8),
            ("anima_context_detailer_base10.safetensors", 0.8),
            ("reina_v1_epoch14.safetensors", 0.8)])
        self.assertEqual(wf["2"]["inputs"]["steps"], 26)
        lat = [n for n in wf if wf[n]["class_type"] == "EmptyLatentImage"][0]
        self.assertEqual((wf[lat]["inputs"]["width"], wf[lat]["inputs"]["height"]),
                         (960, 1280))
        dl.assert_called_once_with("Anima_x.png", self.task.raw_path(self.panel))
        self.assertEqual(self.panel.state, "rendered")
        self.assertEqual(self.panel.seed, 42)


if __name__ == "__main__":
    unittest.main()