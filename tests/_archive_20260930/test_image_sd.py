"""SD 渠道（image_gen_v1）：单一渠道、标准节点、按用户 v90 文件重建。

历史与「为什么单独一套用例」：
2026-09-27 用户嫌 SD 慢，把「544×960 采 30 步 → 放大 → 1080×1920 再采 20 步」
（57.2 MP-步）砍成单段直出。当时另开了一个 `image_gen_v1_hires` 渠道装两遍高清版，
**2026-09-30 已整个删掉**——本机只留一个 SD 渠道，所以这里的用例也不再按
「两个渠道的取舍」来写，改成按**这一份工作流的实际结构**锁死。

工作流本身来自用户 19:30 在自己 ComfyUI 里存的 `v90-saturation-contrast-line.json`，
照抄时做了三处刻意偏离（LoRA 换 SDXL 版 / filename_prefix 改扁平 / seed 落裸占位），
理由见 skills/image_gen_v1/skill.md 的「2026-09-30 重建记录」。本文件把这三处
**连同「别改回去」的原因一起**锁成断言——它们每一条都是踩过的坑：

- LoRA 换成 SD1.5 版 → 键名对不上 SDXL 底模，**等于没挂**（出图约等于裸 v90）。
  这个坑 09-28 踩过一次，09-30 又踩一次（用户文件里就是 SD1.5 的），所以必须锁。
- filename_prefix 带 `/` → ComfyUI 存进 output 子目录，而 image_out 只按 filename
  取图、不带 subfolder → **群里永远收不到图**。这是硬约束。
- seed 必须落成**裸** `__SEED__`（不是 `"__SEED__"`）→ 否则 KSampler 收到字符串，
  INT 类型校验不过，提交必被拒。

另外保留「像素×步数」的开销上限：这个数是算出来的、不是拍的，有人把分辨率或步数
拉回去就会重新变慢。零网络、零显卡：只读 skills 目录里的 workflow.json。
"""

import os
import unittest

from app.agent_prompt import _build_skill_list
from app.skills import load_skill, list_skills

CHANNEL = "image_gen_v1"

# 旧参数（544×960@30 + 1080×1920@20）的开销，改回去就会超这条线。
_OLD_COST = 57.2
# 开销上限：旧的七折，留一点余量给微调。
_COST_CAP = 40.0

# 用户 v90 文件里那三个 LoRA 实测是 **SD1.5**（只有 lora_te1、无 lora_te2），
# 挂 SDXL 底模等于没挂。这三个名字出现在工作流里就是回归。
_SD15_LORA_TRAPS = (
    "hotarucontrast_v100",
    "FZB_line_weights",
)
# 应当使用的 SDXL 三件套（顺序 = 加载顺序）。
_SDXL_LORAS = (
    "add_saturation_XL.safetensors",
    "add_contrast_XL.safetensors",
    "add_outline_XL.safetensors",
)


# ─── 取节点的小工具 ───────────────────────────────────

def _wf(name=CHANNEL):
    data = load_skill(name)
    assert data is not None, name + " 加载不到"
    return data["workflow"]


def _nodes(wf, class_type):
    """所有该 class_type 的 (id, node)，按 id 数字序（稳定，便于断言链顺序）。"""
    hit = [(nid, nd) for nid, nd in wf.items()
           if nd.get("class_type") == class_type]
    return sorted(hit, key=lambda kv: int(kv[0]) if str(kv[0]).isdigit() else 0)


def _one(wf, class_type):
    hits = _nodes(wf, class_type)
    assert len(hits) == 1, (
        "期望恰好一个 %s，实际 %d 个" % (class_type, len(hits)))
    return hits[0][1]


def _raw(name=CHANNEL):
    """工作流**磁盘原文**（用于检查裸占位符——load_skill 会替它补引号）。"""
    path = os.path.join(load_skill(name)["path"], "workflow.json")
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _clip_texts(wf):
    return [nd["inputs"]["text"] for _, nd in _nodes(wf, "CLIPTextEncode")]


def _pos(wf=None):
    """正向那一条：带 __MULTI_PROMPTS__ 的。"""
    for t in _clip_texts(wf or _wf()):
        if "__MULTI_PROMPTS__" in t:
            return t
    raise AssertionError("找不到正向 CLIPTextEncode（没有 __MULTI_PROMPTS__）")


def _neg(wf=None):
    """负向那一条。"""
    for t in _clip_texts(wf or _wf()):
        if "bad quality" in t:
            return t
    raise AssertionError("找不到负向 CLIPTextEncode（没有 bad quality）")


def _cost(wf):
    """采样开销 ≈ 像素 × 步数（正比于耗时，够用来比快慢）。"""
    lat = _one(wf, "EmptyLatentImage")["inputs"]
    ks = _one(wf, "KSampler")["inputs"]
    return lat["width"] * lat["height"] / 1e6 * ks["steps"]


# ─── 渠道存在性 ───────────────────────────────────────

class ChannelExistsTest(unittest.TestCase):
    def test_scanned(self):
        self.assertIn(CHANNEL, list_skills())

    def test_hires_channel_is_gone(self):
        """本机只有这一个 SD 渠道。工具描述里也不许再提 hires——
        提了模型真会传，然后只拿到「找不到 Skill」。"""
        self.assertNotIn("image_gen_v1_hires", list_skills())
        from app.tools.normal.generate_image import tool
        from app.config import QQ_AGENT_ID
        for desc in (tool["description"],
                     tool["description_overrides"][QQ_AGENT_ID]):
            self.assertNotIn("image_gen_v1_hires", desc)

    def test_is_a_gen_channel_with_a_character_base(self):
        data = load_skill(CHANNEL)
        # 没有 frontmatter 声明 kind 时，上层按「有没有 workflow.json」推断成生图。
        self.assertIsNotNone(data["workflow"])
        self.assertTrue((data.get("character") or "").strip())


# ─── 工作流结构 ───────────────────────────────────────

class WorkflowShapeTest(unittest.TestCase):
    """标准节点搭出来的单段图。**没有**任何自定义节点。"""

    def test_no_custom_nodes(self):
        """原先工作流靠 BatchPromptImageGenerator 拆多段提示词——这台 ComfyUI
        里根本没有这个节点，整条工作流换成标准节点重搭了。"""
        classes = {nd.get("class_type") for nd in _wf().values()}
        self.assertNotIn("BatchPromptImageGenerator", classes)
        self.assertEqual(classes, {
            "CheckpointLoaderSimple",
            "LoraLoader",
            "CLIPTextEncode",
            "EmptyLatentImage",
            "KSampler",
            "VAEDecode",
            "SaveImage",
        })

    def test_every_link_points_at_an_existing_node(self):
        """任何 [node_id, slot] 连线都得指到真实存在的节点上。

        重建工作流时最容易留下的就是「指向已删节点」的悬空引用——ComfyUI 到
        提交那一刻才报错，群里表现是「画不出来」，很难查。这里提前拦。
        """
        wf = _wf()
        for nid, nd in wf.items():
            for key, val in (nd.get("inputs") or {}).items():
                if isinstance(val, list) and len(val) == 2 and isinstance(val[0], str):
                    self.assertIn(val[0], wf,
                                  "节点 %s 的 %s 指向不存在的节点 %s" % (nid, key, val[0]))

    def test_single_stage_sampling(self):
        """单段直出——两遍高清（hires）那条路已经整个删掉了。"""
        self.assertEqual(len(_nodes(_wf(), "KSampler")), 1)
        ks = _one(_wf(), "KSampler")["inputs"]
        for gone in ("hires_width", "hires_height", "hires_steps", "hires_denoise",
                     "enable_hires"):
            self.assertNotIn(gone, ks, gone)

    def test_chain_is_wired_through_the_lora_tail(self):
        """底模 → LoRA 链 → KSampler.model / 两个 CLIPTextEncode.clip。

        这是「重建 LoRA 之后链接断掉」那类事故的护栏：链尾必须同时喂
        采样器和正负文本编码，只接对一半就会出一张和提示词无关的图。
        """
        wf = _wf()
        lor_ids = [nid for nid, _ in _nodes(wf, "LoraLoader")]
        self.assertEqual(lor_ids, ["10", "11", "12"])
        tail = lor_ids[-1]
        self.assertEqual(_one(wf, "KSampler")["inputs"]["model"], [tail, 0])
        for nd in _nodes(wf, "CLIPTextEncode"):
            self.assertEqual(nd[1]["inputs"]["clip"], [tail, 1])


# ─── 模型与参数 ───────────────────────────────────────

class ModelTest(unittest.TestCase):
    def test_base_model_is_v90(self):
        self.assertEqual(_one(_wf(), "CheckpointLoaderSimple")["inputs"]["ckpt_name"],
                         "unholyDesireMixSinister_v90.safetensors")

    def test_three_sdxl_loras_in_order(self):
        names = [nd["inputs"]["lora_name"] for _, nd in _nodes(_wf(), "LoraLoader")]
        self.assertEqual(tuple(names), _SDXL_LORAS)

    def test_no_sd15_loras_left(self):
        """用户文件里那三个是 SD1.5 的，挂 SDXL 底模等于没挂。别改回去。"""
        text = _raw()
        for trap in _SD15_LORA_TRAPS:
            self.assertNotIn(trap, text, trap + " 是 SD1.5 LoRA，挂 SDXL 等于没挂")

    def test_lora_strengths(self):
        for _, nd in _nodes(_wf(), "LoraLoader"):
            self.assertEqual(nd["inputs"]["strength_model"], 0.5)
            self.assertEqual(nd["inputs"]["strength_clip"], 0.5)

    def test_loras_are_chained_head_to_tail(self):
        """10 ← 底模, 11 ← 10, 12 ← 11；每级 model/clip 都从上一级取。"""
        wf = _wf()
        self.assertEqual(wf["10"]["inputs"]["model"], ["1", 0])
        self.assertEqual(wf["10"]["inputs"]["clip"], ["1", 1])
        self.assertEqual(wf["11"]["inputs"]["model"], ["10", 0])
        self.assertEqual(wf["11"]["inputs"]["clip"], ["10", 1])
        self.assertEqual(wf["12"]["inputs"]["model"], ["11", 0])
        self.assertEqual(wf["12"]["inputs"]["clip"], ["11", 1])


class SamplingParamsTest(unittest.TestCase):
    """分辨率 / 步数 / CFG / 采样器 —— 都照用户文件。"""

    def test_resolution_is_768x1024(self):
        lat = _one(_wf(), "EmptyLatentImage")["inputs"]
        self.assertEqual(lat["width"], 768)
        self.assertEqual(lat["height"], 1024)
        self.assertEqual(lat["batch_size"], 1)

    def test_sampler_settings(self):
        ks = _one(_wf(), "KSampler")["inputs"]
        self.assertEqual(ks["sampler_name"], "dpmpp_2m")
        self.assertEqual(ks["scheduler"], "karras")
        self.assertEqual(ks["steps"], 24)
        self.assertEqual(ks["cfg"], 7)
        self.assertEqual(ks["denoise"], 1)

    def test_ksampler_reads_the_latent_and_both_prompts(self):
        wf = _wf()
        ks = _one(wf, "KSampler")["inputs"]
        self.assertEqual(ks["latent_image"], ["4", 0])
        self.assertEqual(ks["positive"], ["2", 0])
        self.assertEqual(ks["negative"], ["3", 0])

    def test_cost_is_well_under_the_old_two_pass(self):
        cost = _cost(_wf())
        self.assertLess(cost, _COST_CAP)
        self.assertLess(cost, _OLD_COST * 0.6, "应当比旧的两遍高清省四成以上")


# ─── 提示词与出图 ─────────────────────────────────────

class PromptTest(unittest.TestCase):
    def test_positive_keeps_the_artist_style_prefix(self):
        """那串 artist 画风前缀**就是这个渠道的画风来源**，别删。"""
        text = _pos()
        self.assertIn("artist:tianliang duohe fangdongye", text)
        self.assertIn("(artist:nekoda_(maoda):0.7)", text)
        self.assertIn("year 2023", text)

    def test_positive_has_both_placeholders_after_the_blank_lines(self):
        """风格前缀 → 连续空行 → 主体段（角色 + 模型给的提示词）。"""
        text = _pos()
        self.assertIn("__CHARACTER__", text)
        self.assertIn("__MULTI_PROMPTS__", text)
        self.assertLess(text.index("artist:"), text.index("__CHARACTER__"))
        self.assertLess(text.index("__CHARACTER__"), text.index("__MULTI_PROMPTS__"))
        self.assertRegex(text, r"\n\s*\n\s*\n")

    def test_negative_is_the_users_full_list(self):
        neg = _neg()
        for tag in ("worst detail", "(deformed:1.5)", "(bad hand:1.3)",
                    "overexposed", "cloned face", "bad eyes"):
            self.assertIn(tag, neg)

    def test_positive_is_not_the_negative(self):
        """两个 CLIPTextEncode 别接反——接反了会「按负面词画」。"""
        self.assertNotIn("worst quality", _pos())


class OutputTest(unittest.TestCase):
    def test_filename_prefix_is_flat(self):
        """**硬约束**：前缀不许带 `/`。

        带 `/` 时 ComfyUI 把图存进 output 的子目录，而 image_out 只按 filename
        取图（`/view?filename=`，不带 subfolder）→ 图取不回来，群里永远收不到。
        """
        prefix = _one(_wf(), "SaveImage")["inputs"]["filename_prefix"]
        self.assertNotIn("/", prefix)
        self.assertNotIn("\\", prefix)
        self.assertTrue(prefix.strip())

    def test_seed_is_a_bare_placeholder_on_disk(self):
        """磁盘上必须是裸 `__SEED__`（无引号），否则 KSampler 收到字符串、
        INT 校验不过，提交直接失败。"""
        text = _raw()
        self.assertEqual(text.count("__SEED__"), 1)
        self.assertIn('"seed": __SEED__,', text)
        self.assertNotIn('"__SEED__"', text)

    def test_decoding_goes_through_the_checkpoint_vae(self):
        vd = _one(_wf(), "VAEDecode")["inputs"]
        self.assertEqual(vd["vae"], ["1", 2])


# ─── 上层可见性 ───────────────────────────────────────

class VisibilityTest(unittest.TestCase):
    def test_qq_whitelist_includes_it(self):
        from app import agents
        self.assertTrue(agents.allows_skill("qq", CHANNEL))

    def test_skill_list_shows_it_as_a_character_channel(self):
        lines = [l for l in _build_skill_list("qq").splitlines()
                 if "**" + CHANNEL + "**" in l]
        self.assertEqual(len(lines), 1)
        self.assertIn("带底模", lines[0])

    def test_tool_description_points_at_it_as_the_sd_channel(self):
        from app.tools.normal.generate_image import tool
        from app.config import QQ_AGENT_ID
        for desc in (tool["description"],
                     tool["description_overrides"][QQ_AGENT_ID]):
            self.assertIn(CHANNEL, desc)

    def test_not_in_the_disabled_list(self):
        from app.config import DISABLED_IMAGE_SKILLS
        self.assertNotIn(CHANNEL, DISABLED_IMAGE_SKILLS)


if __name__ == "__main__":
    unittest.main()
