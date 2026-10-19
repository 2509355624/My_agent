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
        self.assertEqual(d["upscale"], {
            "scale_by": 1.2,
            "final_model": "2x_Ani4Kv2_G6i2_Compact_107500.pth",
            "final_skip": False})


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

    def test_upscale_scale_and_model_land_on_right_nodes(self):
        """latent 倍率写 LatentUpscaleBy，最终放大模型写 UpscaleModelLoader；
        最终放大节点的 upscale_model 连线不动（它还指着 loader）。"""
        wf = _fresh_workflow()
        opts = ro.defaults("anima-line")
        opts["upscale"] = {"scale_by": 1.5,
                           "final_model": "4x_NMKD-Siax_200k.pth",
                           "final_skip": False}
        out = ro.apply(wf, opts)
        up = ro._upscale_nodes(out)
        self.assertEqual(out[up["latent"][0]]["inputs"]["scale_by"], 1.5)
        self.assertEqual(out[up["loader"]]["inputs"]["model_name"],
                         "4x_NMKD-Siax_200k.pth")
        self.assertEqual(out[up["final"]]["inputs"]["upscale_model"],
                         [up["loader"], 0])

    def test_upscale_skip_rewires_save_image_to_decoder(self):
        """跳过最终放大：SaveImage 直连 VAEDecode；最终放大节点留着但悬空，
        ComfyUI 只跑输出可达的节点，悬空节点不会被执行。"""
        wf = _fresh_workflow()
        opts = ro.defaults("anima-line")
        opts["upscale"]["final_skip"] = True
        out = ro.apply(wf, opts)
        up = ro._upscale_nodes(out)
        save = [n for n in out if out[n]["class_type"] == "SaveImage"][0]
        decode = out[up["final"]]["inputs"]["image"][0]
        self.assertEqual(out[save]["inputs"]["images"], [decode, 0])
        refs = [n for n in out for k, v in (out[n]["inputs"] or {}).items()
                if isinstance(v, list) and v and str(v[0]) == up["final"]]
        self.assertEqual(refs, [])


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

    def test_upscale_scale_clamped_model_checked(self):
        base = ro.defaults("anima-line")
        # 倍率钳位（不拒）
        opts, errors, warnings = ro.normalize(
            {"upscale": {"scale_by": 99}}, base=base)
        self.assertEqual(errors, [])
        self.assertEqual(opts["upscale"]["scale_by"], 4.0)
        self.assertTrue(warnings)
        # 有清单 → 放大模型必须是事实
        _o, errors, _w = ro.normalize(
            {"upscale": {"final_model": "nope.pth"}},
            resources={"upscales": ["2x_Ani4Kv2_G6i2_Compact_107500.pth"]})
        self.assertTrue(any("放大模型" in e for e in errors))
        # 没清单 → 不猜，照收
        _o, errors, _w = ro.normalize(
            {"upscale": {"final_model": "nope.pth"}})
        self.assertEqual(errors, [])
        # 跳过最终放大时模型可以为空
        _o, errors, _w = ro.normalize(
            {"upscale": {"final_skip": True, "final_model": ""}})
        self.assertEqual(errors, [])
        self.assertTrue(_o["upscale"]["final_skip"])
        # 不跳过又不给模型 → 事实问题，拒
        _o, errors, _w = ro.normalize(
            {"upscale": {"final_skip": False, "final_model": ""}})
        self.assertTrue(errors)

    def test_width_is_aligned_to_multiple_of_eight(self):
        """不是 8 的倍数按 //8 对齐（2026-10-18 用户填 812 起改的：ComfyUI 的
        latent 本来就这么截，拒掉只是让人存不进去）。"""
        opts, errors, warnings = ro.normalize({"width": 1001, "height": 812})
        self.assertEqual(errors, [])
        self.assertEqual((opts["width"], opts["height"]), (1000, 808))
        self.assertTrue(any("8 的倍数" in w for w in warnings))

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
        self.assertEqual(opts["upscale"], base["upscale"])


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


class GlobalDefaultsTest(unittest.TestCase):
    """全局默认（2026-10-18 用户要求「长期保存某个参数作为默认」）。

    优先级钉死：任务 render.json > 全局默认 > anima-line 模板值。
    全局文件在 state/ 下、进程内共享，所以每条用例先存后清（有旧的先备份）。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="render_opts_g_")
        self._backup = None
        fp = ro.defaults_path()
        if os.path.isfile(fp):
            with open(fp, "rb") as f:
                self._backup = f.read()
        ro.reset_defaults()

    def tearDown(self):
        ro.reset_defaults()
        if self._backup is not None:
            os.makedirs(os.path.dirname(ro.defaults_path()), exist_ok=True)
            with open(ro.defaults_path(), "wb") as f:
                f.write(self._backup)

    def _global_with(self, **kw):
        g = ro.defaults("anima-line")
        for k, v in kw.items():
            g[k] = v
        ro.save_defaults(g)
        return g

    def test_no_global_file_means_template(self):
        self.assertFalse(os.path.isfile(ro.defaults_path()))
        self.assertEqual(ro.load(self.tmp, "anima-line"),
                         ro.defaults("anima-line"))

    def test_global_applies_to_task_without_render_json(self):
        g = ro.defaults("anima-line")
        g["width"], g["height"] = 816, 1024
        g["base"]["steps"] = 10
        ro.save_defaults(g)
        got = ro.load(self.tmp, "anima-line")
        self.assertEqual((got["width"], got["height"]), (816, 1024))
        self.assertEqual(got["base"]["steps"], 10)

    def test_task_level_overrides_global(self):
        g = ro.defaults("anima-line")
        g["base"]["steps"] = 10
        ro.save_defaults(g)
        t = ro.load(self.tmp, "anima-line")
        t["base"]["steps"] = 30
        ro.save(self.tmp, t)
        self.assertEqual(ro.load(self.tmp, "anima-line")["base"]["steps"], 30)
        ro.reset(self.tmp)                     # 删掉任务级 → 回落到全局默认
        self.assertEqual(ro.load(self.tmp, "anima-line")["base"]["steps"], 10)

    def test_reset_global_returns_to_template(self):
        self._global_with(width=816)
        self.assertTrue(ro.reset_defaults())
        self.assertFalse(ro.reset_defaults())   # 再删一次是空操作
        self.assertEqual(ro.load(self.tmp, "anima-line"),
                         ro.defaults("anima-line"))

    def test_broken_global_file_falls_back(self):
        os.makedirs(os.path.dirname(ro.defaults_path()), exist_ok=True)
        with open(ro.defaults_path(), "w", encoding="utf-8") as f:
            f.write("{oops")
        self.assertEqual(ro.load(self.tmp, "anima-line"),
                         ro.defaults("anima-line"))

    def test_load_without_task_dir_uses_global(self):
        """参数卡在没有任务时也要能显示（页面刚打开 / 还没生成剧本）。"""
        g = ro.defaults("anima-line")
        g["base"]["steps"] = 12
        ro.save_defaults(g)
        self.assertEqual(ro.load("", "anima-line")["base"]["steps"], 12)

    def test_global_aligns_odd_size_instead_of_rejecting(self):
        """812×1024 存得进去：按 808×1024 渲染，warning 说清楚（用户 2026-10-18
        就是按 812 报的参数）。"""
        g = ro.defaults("anima-line")
        g["width"], g["height"] = 812, 1024
        saved = ro.save_defaults(g)
        self.assertEqual((saved["width"], saved["height"]), (808, 1024))
        self.assertEqual(ro.load("", "anima-line")["width"], 808)


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


class CapsTest(unittest.TestCase):
    """渠道能力表（2026-10-19 多渠道）：参数卡按 caps 渲染。

    这里画错一个字段，界面就会露出改不动的旋钮（用户点了没反应）或者藏起
    本该能改的（切了渠道发现分辨率改不了）。三种采样节点族各钉一条。
    """

    def test_ks_two_pass_channel(self):
        c = ro.caps_of("anima-line")
        self.assertEqual((c["family"], c["passes"]), ("ks", 2))
        self.assertTrue(c["editable"])
        self.assertTrue(c["lora"])
        self.assertEqual((c["upscale_latent"], c["upscale_final"],
                          c["upscale_skip"]), (True, True, True))
        self.assertEqual((c["width"], c["height"]), (1024, 1536))
        # 1024×1536 → latent ×1.2 → 1229×1843 → 最终放大 2x → 2458×3686
        self.assertEqual((c["out_width"], c["out_height"]), (2458, 3686))

    def test_batch_channel_has_hires_but_no_lora(self):
        """sd / miao 的采样全在 BatchPromptImageGenerator 里，第二段是节点上的
        hires_* 字段；LoRA 链是渠道配方（降对比 / 提饱和 / 表情风格），露出来
        会被全局默认里 anima 那 6 槽一键拆掉 —— 所以不给调。"""
        c = ro.caps_of("miao")
        self.assertEqual((c["family"], c["passes"]), ("batch", 2))
        self.assertTrue(c["editable"])
        self.assertFalse(c["lora"])
        self.assertFalse(c["upscale_latent"])   # 没有 LatentUpscaleBy 这一级
        self.assertEqual(ro.defaults("miao")["hires"], {"steps": 20, "denoise": 0.4})

    def test_batch_final_upscale_counted_from_composite_node(self):
        """sd 的最终放大写在节点的 upscale_model 输入里，没有
        ImageUpscaleWithModel 可绕：不认它就少算一倍（720×1280 会算成 1440×2560）。"""
        c = ro.caps_of("image_gen_v1")
        self.assertTrue(c["upscale_final"])
        self.assertFalse(c["upscale_skip"])     # 没有那个节点 → 不给「跳过」开关
        self.assertEqual((c["out_width"], c["out_height"]), (2880, 5120))

    def test_custom_channel_is_read_only_for_sampling(self):
        """moss 4 步的 DMD 链步数由适配器 step_codes 定死，节点上没有 steps
        可改 —— editable=False，面板只留分辨率。"""
        c = ro.caps_of("moss")
        self.assertEqual((c["family"], c["passes"]), ("custom", 1))
        self.assertFalse(c["editable"])
        self.assertFalse(c["lora"])
        self.assertEqual(ro.defaults("moss")["base"], {})
        self.assertEqual((c["out_width"], c["out_height"]),
                         (c["width"], c["height"]))

    def test_single_pass_channel_has_no_hires(self):
        c = ro.caps_of("qwen_image_v1")
        self.assertEqual((c["family"], c["passes"]), ("ks", 1))
        self.assertFalse(c["lora"])
        self.assertEqual(ro.defaults("qwen_image_v1")["hires"], {})

    def test_unknown_skill_is_empty_caps(self):
        c = ro.caps_of("no-such-skill-xyz")
        self.assertEqual((c["family"], c["passes"]), ("none", 0))
        self.assertFalse(c["editable"])
        self.assertFalse(c["lora"])


class MultiChannelApplyTest(unittest.TestCase):
    """同一份参数卡套到不同渠道上：该改的改、该不动的一个字节不动。"""

    def test_batch_hires_size_follows_base_ratio(self):
        """batch 族的第二段尺寸是**写死的数值**（miao 1024×1536→1536×2304）。
        底图改成一半还得等比跟改，否则画幅被拉伸。"""
        wf = gi.build_t2i_workflow("miao", "1girl, solo", 42)
        nid = ro._batch_hires(wf)
        opts = ro.defaults("miao")
        opts["width"], opts["height"] = 512, 768
        out = ro.apply(wf, opts)
        inp = out[nid]["inputs"]
        self.assertEqual((inp["width"], inp["height"]), (512, 768))
        self.assertEqual((inp["hires_width"], inp["hires_height"]), (768, 1152))

    def test_batch_hires_params_land_on_the_composite_node(self):
        wf = gi.build_t2i_workflow("miao", "1girl, solo", 42)
        nid = ro._batch_hires(wf)
        opts = ro.defaults("miao")
        opts["base"]["steps"] = 30
        opts["hires"] = {"steps": 10, "denoise": 0.3}
        inp = ro.apply(wf, opts)[nid]["inputs"]
        self.assertEqual((inp["steps"], inp["hires_steps"]), (30, 10))
        self.assertEqual(inp["hires_denoise"], 0.3)
        # 这个节点没有第二段的 cfg / 采样器可调，别硬塞
        self.assertNotIn("hires_cfg", inp)
        self.assertNotIn("hires_sampler_name", inp)

    def test_size_lands_on_empty_sd3_latent(self):
        """qwen 的底图节点是 EmptySD3LatentImage，不是 EmptyLatentImage。"""
        wf = gi.build_t2i_workflow("qwen_image_v1", "1girl", 42)
        opts = ro.defaults("qwen_image_v1")
        opts["width"], opts["height"] = 896, 1152
        out = ro.apply(wf, opts)
        lat = ro._latent_ids(out)[0]
        self.assertEqual(out[lat]["class_type"], "EmptySD3LatentImage")
        self.assertEqual((out[lat]["inputs"]["width"], out[lat]["inputs"]["height"]),
                         (896, 1152))

    def test_no_lora_is_injected_where_channel_has_no_chain(self):
        """qwen 模板没 LoRA 链，参数里却带着 anima 那 6 槽（全局默认）。照旧
        重建就是往 CLIPLoader/UNETLoader 链上硬塞 LoraLoaderModelOnly，提交必被
        ComfyUI 拒 —— 这种渠道一律不动 LoRA。"""
        wf = gi.build_t2i_workflow("qwen_image_v1", "1girl", 42)
        before = copy.deepcopy(wf)
        opts = ro.defaults("qwen_image_v1")
        opts["loras"] = [{"name": "reina_v1_epoch14.safetensors", "strength": 0.8}]
        out = ro.apply(wf, opts)
        self.assertEqual([n for n in out
                          if out[n]["class_type"] in ro.LORA_CLASSES], [])
        self.assertEqual(out, before)

    def test_custom_channel_sampler_untouched(self):
        wf = gi.build_t2i_workflow("moss", "1girl", 42)
        nid = [n for n in wf if ro._family(wf[n]) == "custom"][0]
        opts = ro.defaults("moss")
        opts["base"] = {"steps": 9, "cfg": 2.0}      # 面板不会给，防御性传入
        opts["width"], opts["height"] = 1024, 1088
        out = ro.apply(wf, opts)
        self.assertEqual(out[nid], wf[nid])
        lat = ro._latent_ids(out)[0]
        self.assertEqual((out[lat]["inputs"]["width"], out[lat]["inputs"]["height"]),
                         (1024, 1088))


class _GlobalFileCase(unittest.TestCase):
    """碰全局默认文件的用例都从这继承：每条先清空，跑完还原用户原来那份。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="render_opts_mc_")
        self._backup = None
        fp = ro.defaults_path()
        if os.path.isfile(fp):
            with open(fp, "rb") as f:
                self._backup = f.read()
        ro.reset_defaults()

    def tearDown(self):
        ro.reset_defaults()
        if self._backup is not None:
            os.makedirs(os.path.dirname(ro.defaults_path()), exist_ok=True)
            with open(ro.defaults_path(), "wb") as f:
                f.write(self._backup)


class TaskParamsPerChannelTest(_GlobalFileCase):
    """任务级参数按渠道各存一份（2026-10-19）。

    共用一个 render.json 的后果很具体：anima 那套（20 步 / hires 20 步 / latent
    ×1.2）整份套到 miao 的 45 步模型上，切一次渠道图就废一批。
    """

    def test_each_channel_has_its_own_file(self):
        a = ro.defaults("anima-line")
        a["base"]["steps"] = 24
        ro.save(self.tmp, a, "anima-line")
        m = ro.defaults("miao")
        m["base"]["steps"] = 50
        ro.save(self.tmp, m, "miao")
        self.assertEqual(ro.load(self.tmp, "anima-line")["base"]["steps"], 24)
        self.assertEqual(ro.load(self.tmp, "miao")["base"]["steps"], 50)
        # 默认渠道仍是 render.json（老任务的文件不用迁）
        self.assertEqual(os.path.basename(ro.path_of(self.tmp)), ro.RENDER_FILE)
        self.assertTrue(os.path.isfile(ro.path_of(self.tmp, "miao")))
        self.assertNotEqual(ro.path_of(self.tmp, "miao"), ro.path_of(self.tmp))

    def test_switching_channel_does_not_see_the_other_ones_params(self):
        a = ro.defaults("anima-line")
        a["base"]["steps"] = 24
        a["hires"]["steps"] = 6
        ro.save(self.tmp, a, "anima-line")
        miao = ro.load(self.tmp, "miao")     # 这个任务没在 miao 下存过
        self.assertEqual(miao["base"]["steps"], 45)     # miao 模板值
        self.assertEqual(miao["hires"]["steps"], 20)

    def test_reset_only_drops_that_channel(self):
        ro.save(self.tmp, ro.defaults("anima-line"), "anima-line")
        ro.save(self.tmp, ro.defaults("miao"), "miao")
        self.assertTrue(ro.reset(self.tmp, "miao"))
        self.assertFalse(os.path.isfile(ro.path_of(self.tmp, "miao")))
        self.assertTrue(os.path.isfile(ro.path_of(self.tmp)))
        self.assertFalse(ro.reset(self.tmp, "miao"))    # 再删一次是空操作


class CustomLoraSurvivesTest(_GlobalFileCase):
    """用户自己挂的角色 LoRA 必须留得住（2026-10-19）。

    试过一版「名字不在这个渠道模板里的槽一律丢掉」来防跨渠道串参数，结果把
    **最常用的功能**一起丢了：模板里当然不会出现他自己训的角色 LoRA。跨渠道
    串参数改由存取层解决（任务级 `render_<渠道>.json`、全局默认按渠道分键），
    读这条链路时不许再按名字猜。
    """

    def test_user_lora_name_survives_a_roundtrip(self):
        opts = ro.defaults("anima-line")
        opts["loras"][0] = {"name": "my_char_v1.safetensors", "strength": 0.9}
        ro.save(self.tmp, opts, "anima-line")
        self.assertEqual(ro.load(self.tmp, "anima-line")["loras"][0],
                         {"name": "my_char_v1.safetensors", "strength": 0.9})

    def test_global_default_keeps_a_user_lora_too(self):
        g = ro.defaults("anima-line")
        g["loras"][2] = {"name": "my_char_v1.safetensors", "strength": 0.7}
        ro.save_defaults(g, "anima-line")
        self.assertEqual(ro.load("", "anima-line")["loras"][2]["name"],
                         "my_char_v1.safetensors")

    def test_that_lora_actually_reaches_the_workflow(self):
        wf = gi.build_t2i_workflow("anima-line", "1girl", 42)
        opts = ro.defaults("anima-line")
        opts["loras"][0] = {"name": "my_char_v1.safetensors", "strength": 0.9}
        out = ro.apply(wf, opts)
        chain, _head = _lora_chain(out, _samplers(out)["base"][0])
        self.assertEqual(chain[0], ("my_char_v1.safetensors", 0.9))


class GlobalDefaultsPerChannelTest(_GlobalFileCase):
    """全局默认按渠道分键存：anima 的步数 / 放大倍率不许盖到别的渠道头上。"""

    def test_saving_one_channel_does_not_touch_another(self):
        g = ro.defaults("anima-line")
        g["width"], g["height"] = 816, 1024
        g["base"]["steps"] = 10
        ro.save_defaults(g, "anima-line")
        self.assertEqual(ro.load("", "anima-line")["base"]["steps"], 10)
        self.assertEqual(ro.load("", "krea2-general")["base"]["steps"], 8)

    def test_legacy_flat_file_only_applies_to_default_channel(self):
        """多渠道之前整个文件就是一份 opts（那时只有 anima-line）。升上来以后
        它只算默认渠道的，不许串到 krea2 / miao 上。"""
        os.makedirs(os.path.dirname(ro.defaults_path()), exist_ok=True)
        flat = ro.defaults("anima-line")
        flat["base"]["steps"] = 11
        with open(ro.defaults_path(), "w", encoding="utf-8") as f:
            json.dump(flat, f)
        self.assertEqual(ro.load("", "anima-line")["base"]["steps"], 11)
        self.assertEqual(ro.load("", "krea2-general")["base"]["steps"], 8)
        self.assertTrue(ro.has_defaults("anima-line"))
        self.assertFalse(ro.has_defaults("krea2-general"))

    def test_upgrading_a_flat_file_keeps_the_old_values(self):
        os.makedirs(os.path.dirname(ro.defaults_path()), exist_ok=True)
        flat = ro.defaults("anima-line")
        flat["base"]["steps"] = 11
        with open(ro.defaults_path(), "w", encoding="utf-8") as f:
            json.dump(flat, f)
        ro.save_defaults(ro.defaults("krea2-general"), "krea2-general")
        self.assertEqual(ro.load("", "anima-line")["base"]["steps"], 11)
        self.assertTrue(ro.has_defaults("krea2-general"))

    def test_reset_one_channel_keeps_the_other(self):
        ro.save_defaults(ro.defaults("anima-line"), "anima-line")
        ro.save_defaults(ro.defaults("miao"), "miao")
        self.assertTrue(ro.reset_defaults("miao"))
        self.assertFalse(ro.has_defaults("miao"))
        self.assertTrue(ro.has_defaults("anima-line"))
        self.assertFalse(ro.reset_defaults("miao"))     # 再删一次是空操作


if __name__ == "__main__":
    unittest.main()