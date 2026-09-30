"""comfy_workflow（get_workflow / update_workflow）的工作流解析测试。

原则：零网络、零显卡、**不读真实 skills/**（那份是 gitignore 的，节点编号随
工作流更新而变）。全部用手工构造的小工作流，形状照抄真实的那几个：
- image_gen_v1: CheckpointLoaderSimple + 3 个 LoraLoader（带 clip）
- anima / anima_2: 2 个 UNETLoader + 2 个 LoraLoaderModelOnly，两段采样
- krea2: UnetLoaderGGUF + 1 个 LoraLoaderModelOnly

这里锁的是一类**静默改坏工作流**的 bug：LoRA 链没认出来时 update_workflow
不会报错，而是把 KSampler 的 model 直接改指底模（LoRA 全掉）、把
CLIPTextEncode 的 clip 接到没有 clip 输出槽的加载器上，提交才被 ComfyUI 拒。
"""

import copy
import unittest
from unittest import mock

from app.tools.normal import comfy_workflow as cw


def _two_stage_model_only():
    """anima 形状。注意 dict 顺序故意让链尾 15 排在链头侧的 16 前面——
    真实 anima 就是这样（节点 15/16），按 dict 顺序取「第一个 LoRA」会拿错。"""
    return {
        "1": {"class_type": "VAELoader", "inputs": {"vae_name": "qwen_vae.safetensors"}},
        "2": {"class_type": "KSampler", "inputs": {
            "model": ["15", 0], "positive": ["4", 0], "negative": ["8", 0],
            "latent_image": ["9", 0], "steps": 10, "cfg": 5, "denoise": 1.0,
            "sampler_name": "euler", "scheduler": "simple", "seed": "__SEED__"}},
        "3": {"class_type": "VAEDecode", "inputs": {"samples": ["19", 0], "vae": ["1", 0]}},
        "4": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["6", 0], "text": "p"}},
        "5": {"class_type": "UNETLoader", "inputs": {"unet_name": "base_a.safetensors"}},
        "6": {"class_type": "CLIPLoader", "inputs": {"clip_name": "qwen.safetensors"}},
        "8": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["6", 0], "text": "n"}},
        "9": {"class_type": "EmptyLatentImage", "inputs": {"width": 768, "height": 1024}},
        "15": {"class_type": "LoraLoaderModelOnly", "inputs": {
            "model": ["16", 0], "lora_name": "skin.safetensors", "strength_model": 0.5}},
        "16": {"class_type": "LoraLoaderModelOnly", "inputs": {
            "model": ["5", 0], "lora_name": "style.safetensors", "strength_model": 1.0}},
        "19": {"class_type": "KSampler", "inputs": {
            "model": ["20", 0], "positive": ["4", 0], "negative": ["8", 0],
            "latent_image": ["2", 0], "steps": 5, "cfg": 5, "denoise": 0.25,
            "sampler_name": "euler", "scheduler": "simple", "seed": "__SEED__"}},
        "20": {"class_type": "UNETLoader", "inputs": {"unet_name": "base_b.safetensors"}},
    }


def _single_base_two_stage():
    """`anima_clear` 形状：**两段采样、但只有一块底模**。

    四个动漫渠道里只有它是这样——`KSampler(27)` 的 model 直接指向
    `UNETLoader(5)`，**没有第二个 UNETLoader**（所以是 13 个节点，比别的少一个）。

    ⚠️ 这个形状对 `_rebuild_lora` 是个**真陷阱**：节点 27 指的是**底模装载器**
    而不是 LoRA 节点，所以「重建后把指着旧 LoRA 节点的引用改指新链尾」那段
    **不能碰它**。一旦被顺手改指 LoRA 链尾，第二段就变成「带 LoRA 精修」，
    画风会跟着变——那正是用户明确否掉的做法（LoRA 只挂第一段）。

    2026-09-30 21:3x `anima_clear` 成了 `update_workflow` 的默认渠道，
    所以这个形状从「冷门变体」变成了**每次不传 skill 都会走到的路径**。
    """
    return {
        "1": {"class_type": "VAELoader", "inputs": {"vae_name": "qwen_vae.safetensors"}},
        "2": {"class_type": "KSampler", "inputs": {
            "model": ["15", 0], "positive": ["4", 0], "negative": ["8", 0],
            "latent_image": ["9", 0], "steps": 10, "cfg": 5, "denoise": 1.0,
            "sampler_name": "er_sde", "scheduler": "simple", "seed": "__SEED__"}},
        "3": {"class_type": "VAEDecode", "inputs": {"samples": ["27", 0], "vae": ["1", 0]}},
        "4": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["6", 0], "text": "p"}},
        "5": {"class_type": "UNETLoader", "inputs": {"unet_name": "base_a.safetensors"}},
        "6": {"class_type": "CLIPLoader", "inputs": {"clip_name": "qwen.safetensors"}},
        "8": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["6", 0], "text": "n"}},
        "9": {"class_type": "EmptyLatentImage", "inputs": {"width": 728, "height": 1024}},
        "15": {"class_type": "LoraLoaderModelOnly", "inputs": {
            "model": ["16", 0], "lora_name": "skin.safetensors", "strength_model": 0.5}},
        "16": {"class_type": "LoraLoaderModelOnly", "inputs": {
            "model": ["5", 0], "lora_name": "style.safetensors", "strength_model": 1.0}},
        "23": {"class_type": "SaveImage",
               "inputs": {"images": ["3", 0], "filename_prefix": "Anima"}},
        "25": {"class_type": "LatentUpscaleBy", "inputs": {
            "samples": ["2", 0], "upscale_method": "nearest-exact", "scale_by": 1.0}},
        # 二段：model 直接吃底模装载器 5，latent 吃放大节点 25
        "27": {"class_type": "KSampler", "inputs": {
            "model": ["5", 0], "positive": ["4", 0], "negative": ["8", 0],
            "latent_image": ["25", 0], "steps": 5, "cfg": 5, "denoise": 0.25,
            "sampler_name": "euler", "scheduler": "simple", "seed": "__SEED__"}},
    }


def _sd_lora_chain():
    """SD 旧形状（image_gen_v1，已归档）：CheckpointLoaderSimple + 3 个带 clip 的 LoraLoader。"""
    return {
        "4": {"class_type": "CheckpointLoaderSimple",
              "inputs": {"ckpt_name": "sd.safetensors"}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["90", 1], "text": "p"}},
        "7": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["90", 1], "text": "n"}},
        "9": {"class_type": "EmptyLatentImage", "inputs": {"width": 832, "height": 1216}},
        "54": {"class_type": "KSampler", "inputs": {
            "model": ["90", 0], "positive": ["6", 0], "negative": ["7", 0],
            "latent_image": ["9", 0], "steps": 30, "cfg": 7.0, "denoise": 1.0,
            "sampler_name": "dpmpp_2m", "scheduler": "karras", "seed": "__SEED__"}},
        "88": {"class_type": "LoraLoader", "inputs": {
            "model": ["4", 0], "clip": ["4", 1],
            "lora_name": "saturation.safetensors",
            "strength_model": 0.5, "strength_clip": 0.5}},
        "89": {"class_type": "LoraLoader", "inputs": {
            "model": ["88", 0], "clip": ["88", 1],
            "lora_name": "contrast.safetensors",
            "strength_model": 0.5, "strength_clip": 0.5}},
        "90": {"class_type": "LoraLoader", "inputs": {
            "model": ["89", 0], "clip": ["89", 1],
            "lora_name": "outline.safetensors",
            "strength_model": 0.5, "strength_clip": 0.5}},
    }


def _gguf_model_only():
    """krea2 形状：底模是 UnetLoaderGGUF——按 "UNETLoader" 精确匹配会认不出来。"""
    return {
        "1": {"class_type": "UnetLoaderGGUF", "inputs": {"unet_name": "krea2.gguf"}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": "t5.safetensors"}},
        "4": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["2", 0], "text": "p"}},
        "5": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["2", 0], "text": "n"}},
        "6": {"class_type": "EmptyLatentImage", "inputs": {"width": 1024, "height": 1536}},
        "7": {"class_type": "KSampler", "inputs": {
            "model": ["14", 0], "positive": ["4", 0], "negative": ["5", 0],
            "latent_image": ["6", 0], "steps": 8, "cfg": 1.0, "denoise": 1.0,
            "sampler_name": "euler", "scheduler": "simple", "seed": "__SEED__"}},
        "14": {"class_type": "LoraLoaderModelOnly", "inputs": {
            "model": ["1", 0], "lora_name": "hina.safetensors", "strength_model": 1.0}},
    }


def _dangling(workflow):
    """任何指向不存在节点、或指向 None 的连线都算断线。"""
    bad = []
    for nid, nd in workflow.items():
        for key, val in (nd.get("inputs") or {}).items():
            if isinstance(val, list) and val:
                if val[0] is None or str(val[0]) not in workflow:
                    bad.append((nid, key, val))
    return bad


class LoraChainTest(unittest.TestCase):
    """回溯 LoRA 链：两种加载器都要认。"""

    def test_model_only_chain_is_detected(self):
        """核心回归：只认 LoraLoader 时这条链是空的，update_workflow 随后会把
        工作流改坏。认出来必须是按加载顺序（底模侧在前）。"""
        wf = _two_stage_model_only()
        gid, _ = cw._find_generator(wf)
        chain = cw._lora_chain(wf, gid)
        self.assertEqual([l["name"] for l in chain],
                         ["style.safetensors", "skin.safetensors"])
        self.assertEqual([l["model_only"] for l in chain], [True, True])

    def test_model_only_has_no_clip_strength(self):
        wf = _two_stage_model_only()
        gid, _ = cw._find_generator(wf)
        for l in cw._lora_chain(wf, gid):
            self.assertIsNone(l["strength_clip"])

    def test_sd_chain_still_detected(self):
        wf = _sd_lora_chain()
        gid, _ = cw._find_generator(wf)
        chain = cw._lora_chain(wf, gid)
        self.assertEqual([l["name"] for l in chain],
                         ["saturation.safetensors", "contrast.safetensors",
                          "outline.safetensors"])
        self.assertEqual([l["model_only"] for l in chain], [False, False, False])
        self.assertEqual(chain[0]["strength_clip"], 0.5)

    def test_gguf_chain_detected(self):
        wf = _gguf_model_only()
        gid, _ = cw._find_generator(wf)
        self.assertEqual([l["name"] for l in cw._lora_chain(wf, gid)],
                         ["hina.safetensors"])

    def test_no_lora_is_empty(self):
        wf = {"4": {"class_type": "CheckpointLoaderSimple", "inputs": {}},
              "9": {"class_type": "SaveImage", "inputs": {}}}
        self.assertEqual(cw._lora_chain(wf, "4"), [])


class LoraKindTest(unittest.TestCase):
    """重建要用哪种 LoRA 节点：跟着现有节点走，别凭空换类。"""

    def test_model_only_workflow_keeps_model_only(self):
        self.assertEqual(cw._lora_kind(_two_stage_model_only()),
                         "LoraLoaderModelOnly")

    def test_sd_workflow_keeps_lora_loader(self):
        self.assertEqual(cw._lora_kind(_sd_lora_chain()), "LoraLoader")

    def test_gguf_workflow_keeps_model_only(self):
        self.assertEqual(cw._lora_kind(_gguf_model_only()),
                         "LoraLoaderModelOnly")


class ChainHeadTest(unittest.TestCase):
    """链头（LoRA 该挂上去的底模加载器）。"""

    def test_two_stage_head_is_first_unet_not_other_lora(self):
        """链尾 15 在 dict 里排在 16 前面，按顺序取会拿到 16（另一个 LoRA）。"""
        self.assertEqual(cw._chain_head(_two_stage_model_only()), "5")

    def test_gguf_head_detected(self):
        """krea2 的底模是 UnetLoaderGGUF，写死 "UNETLoader" 会得到 None，
        清空 lora 后 gen.model 变成 [None, 0]。"""
        self.assertEqual(cw._chain_head(_gguf_model_only()), "1")

    def test_sd_head_is_checkpoint(self):
        self.assertEqual(cw._chain_head(_sd_lora_chain()), "4")

    def test_unknown_structure_returns_none(self):
        self.assertIsNone(cw._chain_head({"1": {"class_type": "MysteryNode",
                                                "inputs": {}}}))


class RebuildLoraTest(unittest.TestCase):
    """重建 LoRA 链：改完不能留下断线，也不能改到不该改的节点。"""

    def test_model_only_rebuild_keeps_class_and_no_clip(self):
        wf = _two_stage_model_only()
        cw._rebuild_lora(wf, [{"name": "x.safetensors", "strength_model": 0.8}])
        loras = [(nid, nd) for nid, nd in wf.items()
                 if nd["class_type"].startswith("Lora")]
        self.assertEqual(len(loras), 1)
        nid, nd = loras[0]
        self.assertEqual(nd["class_type"], "LoraLoaderModelOnly")
        self.assertEqual(nd["inputs"]["model"], ["5", 0])
        self.assertNotIn("strength_clip", nd["inputs"])
        self.assertNotIn("clip", nd["inputs"])
        self.assertEqual(_dangling(wf), [])

    def test_model_only_rebuild_leaves_clip_text_encode_alone(self):
        """ModelOnly 工作流的 CLIP 是 CLIPLoader 单独喂的，不能改指 LoRA。
        改坏了会接到一个没有 clip 输出槽的节点上，ComfyUI 直接拒。"""
        wf = _two_stage_model_only()
        cw._rebuild_lora(wf, [{"name": "x.safetensors", "strength_model": 1}])
        self.assertEqual(wf["4"]["inputs"]["clip"], ["6", 0])
        self.assertEqual(wf["8"]["inputs"]["clip"], ["6", 0])

    def test_model_only_rebuild_does_not_touch_second_stage(self):
        """二段底模不接 LoRA，重建后仍直连自己的 UNETLoader。"""
        wf = _two_stage_model_only()
        cw._rebuild_lora(wf, [{"name": "x.safetensors", "strength_model": 1}])
        self.assertEqual(wf["19"]["inputs"]["model"], ["20", 0])

    def test_model_only_rebuild_relinks_generator(self):
        wf = _two_stage_model_only()
        new = cw._rebuild_lora(
            wf, [{"name": "a.safetensors", "strength_model": 1},
                 {"name": "b.safetensors", "strength_model": 0.5}])
        self.assertEqual(len(new), 2)
        self.assertEqual(wf["2"]["inputs"]["model"], [new[-1], 0])
        self.assertNotIn("clip", wf["2"]["inputs"])
        self.assertEqual(_dangling(wf), [])

    def test_sd_rebuild_still_relinks_clip(self):
        """image_gen_v1 的老行为不能丢：LoraLoader 带 clip，文本编码也要跟着改。"""
        wf = _sd_lora_chain()
        new = cw._rebuild_lora(wf, [{"name": "only.safetensors",
                                     "strength_model": 0.5,
                                     "strength_clip": 0.5}])
        self.assertEqual(len(new), 1)
        self.assertEqual(wf["54"]["inputs"]["model"], [new[-1], 0])
        self.assertEqual(wf["54"]["inputs"]["clip"], [new[-1], 1])
        self.assertEqual(wf["6"]["inputs"]["clip"], [new[-1], 1])
        self.assertEqual(wf["7"]["inputs"]["clip"], [new[-1], 1])
        self.assertEqual(_dangling(wf), [])

    def test_single_base_two_stage_keeps_second_pass_off_the_lora_chain(self):
        """**`anima_clear` 形状**（默认渠道）：重建 LoRA 后二段仍直连底模。

        这是本工具最容易踩坏的一条：二段 KSampler(27) 指的是**底模装载器**，
        不是 LoRA 节点。重建时若把「指向底模的 model 引用」也一起改指新链尾，
        二段就从「裸底模精修」变成「带 LoRA 精修」——画风跟着变，而用户明确
        否掉了这种做法（LoRA 只挂第一段）。

        更麻烦的是它**不会报错**：连线是通的，图也出得来，只是画风悄悄变了。
        所以必须有这条锁。
        """
        wf = _single_base_two_stage()
        base_before = wf["27"]["inputs"]["model"]
        new = cw._rebuild_lora(wf, [{"name": "a.safetensors", "strength_model": 1},
                                    {"name": "b.safetensors", "strength_model": 0.5}])
        self.assertEqual(len(new), 2)
        # 一段挂到新链尾
        self.assertEqual(wf["2"]["inputs"]["model"], [new[-1], 0])
        # 二段**原封不动**，仍直连底模装载器
        self.assertEqual(wf["27"]["inputs"]["model"], base_before)
        self.assertEqual(wf["27"]["inputs"]["model"], ["5", 0])
        # 二段的 model 源节点必须仍是 UNETLoader（不是任何 LoRA 节点）
        self.assertEqual(wf[wf["27"]["inputs"]["model"][0]]["class_type"],
                         "UNETLoader")
        self.assertEqual(_dangling(wf), [])

    def test_single_base_two_stage_keeps_both_stages_wired(self):
        """同形状下 `set steps` 只写第一段，二段参数一个字节都不许动。"""
        wf = _single_base_two_stage()
        cw._rebuild_lora(wf, [{"name": "a.safetensors", "strength_model": 1}])
        self.assertEqual(wf["27"]["inputs"]["steps"], 5)
        self.assertEqual(wf["27"]["inputs"]["denoise"], 0.25)
        self.assertEqual(wf["27"]["inputs"]["latent_image"], ["25", 0])
        self.assertEqual(wf["25"]["inputs"]["samples"], ["2", 0])

    def test_clearing_all_loras_falls_back_to_base(self):
        """显式清空时 gen.model 该直连底模，而不是留下 [None, 0]。"""
        wf = _gguf_model_only()
        cw._rebuild_lora(wf, [])
        self.assertEqual(wf["7"]["inputs"]["model"], ["1", 0])
        self.assertEqual(_dangling(wf), [])

    def test_unknown_structure_is_left_untouched(self):
        """结构不认识就一个字节都别改——硬重建比不改更糟。"""
        wf = {"1": {"class_type": "MysteryNode", "inputs": {"model": ["2", 0]}},
              "2": {"class_type": "AnotherMystery", "inputs": {}}}
        before = copy.deepcopy(wf)
        self.assertEqual(cw._rebuild_lora(wf, [{"name": "x", "strength_model": 1}]),
                         [])
        self.assertEqual(wf, before)


class SummaryTest(unittest.TestCase):
    """摘要要让模型看得到真实结构，不然它会以为工作流是单段的。"""

    def test_model_only_chain_is_shown_not_hidden(self):
        txt = cw._build_summary(_two_stage_model_only())
        self.assertIn("style.safetensors", txt)
        self.assertIn("skin.safetensors", txt)
        self.assertIn("ModelOnly", txt)
        self.assertNotIn("LoRA: 无", txt)

    def test_both_base_models_listed(self):
        txt = cw._build_summary(_two_stage_model_only())
        self.assertIn("base_a.safetensors", txt)
        self.assertIn("base_b.safetensors", txt)

    def test_gguf_base_listed(self):
        txt = cw._build_summary(_gguf_model_only())
        self.assertIn("krea2.gguf", txt)
        self.assertNotIn("底模模型: 无", txt)

    def test_size_read_from_latent_not_sampler(self):
        """width/height 挂在 EmptyLatentImage 上，采样器上没有。"""
        txt = cw._build_summary(_two_stage_model_only())
        self.assertIn("width=768", txt)
        self.assertIn("height=1024", txt)

    def test_two_samplers_marked(self):
        txt = cw._build_summary(_two_stage_model_only())
        self.assertIn("[2]", txt)
        self.assertIn("[19]", txt)

    def test_single_sampler_workflow_has_no_sampler_section(self):
        """单段工作流不啰嗦地列采样节点。"""
        self.assertNotIn("采样节点", cw._build_summary(_sd_lora_chain()))


class UpdateWorkflowTest(unittest.TestCase):
    """端到端：写盘拦掉，只看结果工作流。"""

    def _run(self, wf, ops):
        data = {"path": ".", "workflow": wf}
        saved = {}

        def _keep(new_wf, d):
            saved.update(copy.deepcopy(new_wf))

        with mock.patch.object(cw, "_load", lambda skill: data), \
             mock.patch.object(cw, "_save", _keep):
            out = cw.update_workflow("whatever", ops)
        return out, saved

    def test_add_lora_on_model_only_keeps_workflow_intact(self):
        out, wf = self._run(_two_stage_model_only(),
                            [{"op": "add_lora", "name": "extra.safetensors",
                              "strength_model": 0.6}])
        self.assertEqual(_dangling(wf), [])
        gid, _ = cw._find_generator(wf)
        names = [l["name"] for l in cw._lora_chain(wf, gid)]
        self.assertEqual(names, ["style.safetensors", "skin.safetensors",
                                 "extra.safetensors"])
        self.assertEqual(wf["4"]["inputs"]["clip"], ["6", 0])
        self.assertIn("extra.safetensors", out)

    def test_remove_lora_on_model_only(self):
        out, wf = self._run(_two_stage_model_only(),
                            [{"op": "remove_lora", "index": 0}])
        self.assertEqual(_dangling(wf), [])
        gid, _ = cw._find_generator(wf)
        self.assertEqual([l["name"] for l in cw._lora_chain(wf, gid)],
                         ["skin.safetensors"])

    def test_set_steps_hits_first_sampler_only(self):
        out, wf = self._run(_two_stage_model_only(),
                            [{"op": "set", "parameter": "steps", "value": 20}])
        self.assertEqual(wf["2"]["inputs"]["steps"], 20)
        self.assertEqual(wf["19"]["inputs"]["steps"], 5)   # 二段不动
        self.assertEqual(_dangling(wf), [])

    def test_set_width_hits_latent_node(self):
        out, wf = self._run(_two_stage_model_only(),
                            [{"op": "set", "parameter": "width", "value": 832}])
        self.assertEqual(wf["9"]["inputs"]["width"], 832)
        self.assertEqual(wf["9"]["inputs"]["height"], 1024)
        self.assertNotIn("width", wf["2"]["inputs"])

    def test_gguf_set_lora_strength(self):
        out, wf = self._run(_gguf_model_only(),
                            [{"op": "set_lora_strength",
                              "name": "hina.safetensors",
                              "strength_model": 0.9}])
        self.assertEqual(_dangling(wf), [])
        gid, _ = cw._find_generator(wf)
        self.assertEqual(cw._lora_chain(wf, gid)[0]["strength_model"], 0.9)

    def test_sd_update_unchanged_behaviour(self):
        out, wf = self._run(_sd_lora_chain(),
                            [{"op": "set_lora_strength",
                              "name": "contrast.safetensors",
                              "strength_model": 0.9}])
        self.assertEqual(_dangling(wf), [])
        gid, _ = cw._find_generator(wf)
        self.assertEqual(cw._lora_chain(wf, gid)[1]["strength_model"], 0.9)


if __name__ == "__main__":
    unittest.main()
