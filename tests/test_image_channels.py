"""四个动漫生图渠道（`anima_soft` / `anima_gloss` / `anima_curvy` / `anima_clear`）的架构锁。

## 这套用例锁的是什么

2026-09-30 20:xx 用户拍板：**去掉 SD 渠道（`image_gen_v1`），全套切动漫**。
（2026-10-01 用户又拍板把 SD 保留回来——所以它现在**是可用渠道**、不在 `RETIRED`
里；见下面 `RETIRED` 上方的说明。本文件锁的仍是那 4 个动漫常规渠道。）
四个渠道全部取自用户在 ComfyUI 里调好的双采样工作流：

| 渠道 | 来源 UI 文件 | 一段底模 | 二段底模 | 画布 → 输出 |
|---|---|---|---|---|
| **`anima_clear`（默认）** | `单realskin双采样` | Realskin | Realskin（同一块） | 728×1024 |
| `anima_soft` | `reality和realskin双采样` | Realskin | AnimeReality | 728×1024 |
| `anima_gloss` | `anime2` | AnimeReality | Realskin | 768×1024 → 848×1128 |
| `anima_curvy` | `harem和realskin双采样` | Harem | AnimeReality | 728×1024 |

**每次用户在这些 UI 文件里调完参数，对应的 `skills/*/workflow.json` 都要跟着重转**
（UI 格式 → API 格式，四渠道共用同一套流程，见任一 `skills/anima_*/skill.md` 文末）。

四个渠道**共用同一套两段采样骨架**，差别只在底模组合（表现为画风）：

    一段：UNETLoader(5) ─ LoRA(16 kibro 1.0) ─ LoRA(15 baka skin 0.5) ─┐
         CLIPLoader(6, Qwen3-0.6B) ─ CLIPTextEncode(4 正向 / 8 负向) ──┤
         EmptyLatentImage(9) 728×1024 ────────────────────────────────┤
                                                                      ▼
                                                    KSampler(2) er_sde/simple 10步 denoise 1.0
                                                                      │
                                                    LatentUpscaleBy(25) nearest-exact scale_by
                                                                      │
         UNETLoader(28) 裸底模（**不接 LoRA**）───────────────────────┤
                                                                      ▼
                                                    KSampler(27) euler/simple 5步 denoise 0.25
                                                                      │
                                                    VAEDecode(3) ─ SaveImage(23) prefix=Anima

节点编号在 `anima_gloss` 里整体不同（19/20/24 代替 27/28/25），`anima_clear`
甚至**只有一块底模**（二段直接指向节点 5）。所以这套用例**不按编号取**，
而是按 `class_type` + 连线拓扑去认——认出来的是**结构**，不是巧合的数字。

## 锁什么 / 不锁什么

**锁**（架构性的，改了就是改架构）：
- 两个 KSampler、两块（或一块）底模、一个放大节点、一条 LoRA 链；
- 谁连谁：二段必须吃**放大后**的 latent、LoRA 只能挂第一段、CLIP 不走 LoRA；
- denoise 的量级关系（一段 1.0 建构 / 二段 0.25 精修 → 二段必须用确定性 `euler`）；
- 每渠道的**底模组合**（换底模 = 换画风，值得被注意到）；
- 分辨率上限、`scale_by` 上限；
- 节点 4 是可代入模板（含 `@kibro` 触发词 + `__MULTI_PROMPTS__`，不含写死的角色）。

**不锁**（用户随手调的旋钮）：步数、CFG、一段的采样器名。
锁死了每次调参都要改测试，反而会让人嫌麻烦去删断言。

零网络、零显卡：只读 `skills/*/workflow.json`。

（2026-10-02 起本文件末尾还锁了一个**非动漫**渠道 `nffa`（Illustrious 系 + 手脸
两段修复，见 `NffaChannelTest`）——它跟上面那套两段采样骨架**无关**，别套用
`CHANNELS` / `BUILTIN_NODES` 那批常量。）
"""

import json
import os
import unittest
from unittest import mock

from app.skills import list_skills, load_skill, skill_priority

SKILLS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "skills")

# 渠道 → (一段底模前缀, 二段底模前缀, 画布宽, 画布高, scale_by)
#
# 底模用**前缀**而不是全名，因为真实文件名带一长串 Civitai 后缀
# （`miaomiaoAnimeReality_ani11_3087842.safetensors`），前缀足以区分、又不会
# 因为用户重下了一版改名后缀而误报。
CHANNELS = {
    "anima_soft":  ("miaomiaoRealskin_anima13", "miaomiaoAnimeReality_ani11",
                    728, 1024, 1.0),
    "anima_gloss": ("miaomiaoAnimeReality_ani11", "miaomiaoRealskin_anima13",
                    768, 1024, 1.1),
    "anima_curvy": ("miaomiaoHarem_anima16", "miaomiaoAnimeReality_ani11",
                    728, 1024, 1.0),
    "anima_clear": ("miaomiaoRealskin_anima13", "miaomiaoRealskin_anima13",
                    728, 1024, 1.0),
}

# 默认生图渠道（不点名 skill 时走它）。2026-10-06 用户拍板：「我们默认渠道就是
# sILVR，把它做成默认渠道就行了，不用加那个什么默认渠道管理」——所以它是**写死**
# 的，没有管理页旋钮。
#
# ⚠️ 这个值换过三次（`anima` → `anima_realskin` → `anima_soft` → `anima_clear`
# → `silver`），**每一次都有写死名字的地方漏掉**，见 DefaultChannelTest 的说明。
DEFAULT_CHANNEL = "silver"

# `comfy_workflow` 那两个工具（get/update_workflow）的默认 skill。它跟上面的
# 默认生图渠道**是两回事**：那两个工具按「两段采样」骨架读写工作流，拿
# `anima_clear` 当样本渠道，silver 那种结构套不上去。
WORKFLOW_TOOL_CHANNEL = "anima_clear"

# 归档到 skills/_archive_20260930/ 的老渠道——一个都不许再冒出来。
#
# ⚠️ `image_gen_v1` **不在这个名单里**：2026-10-01 用户拍板「qwen 解封、image_gen_v1
# 保留」，SD 渠道重新算**可用渠道**（`agents/draw/agent.json` 白名单里有它），
# 所以它的目录留在 `skills/` 下、`list_skills()` 照常吐出它。
# 同日结案：它的名字已经**写进工具描述**（16 动漫 + qwen + image_gen_v1 + krea2 +
# nai），QQ 白名单也放了它和 `krea2`。以前那条「已知遗留不一致 —— 模型看得见却
# 不知道什么时候该用」不成立了；`QqWhitelistTest`（test_image_jobs）钉的是新契约。
RETIRED = ("anima", "anima_realskin", "anima_2")

# ─── 2026-10-01 新增的 12 个高清渠道（3 档 × 4 画风）─────────────────
#
# 它们跟 4 个常规 anima 渠道**共用同一套两段采样骨架**——只是画布更大、
# 底模按画风换、一段采样器 / 二段步数按档位换。由 `_make_hd_channels.py` 从
# 「尺寸骨架 + 画风底模组合」展开生成（别手改某一份，否则 12 个会悄悄不一致）。
#
# 档位 → (画布宽, 画布高, scale_by, 输出宽, 输出高, 一段采样器, 一段步数, 二段步数)
HD_TIERS = {
    "fast": (1024, 1536, 1.0, 1024, 1536, "er_sde", 10, 5),
    "2":    (1024, 1536, 1.3, 1328, 2000, "dpmpp_2m", 10, 5),
    "3":    (1024, 1536, 1.5, 1536, 2304, "er_sde", 10, 10),
}
# 画风 → (一段底模前缀, 二段底模前缀)——跟 CHANNELS 那四个是同一套组合。
HD_STYLES = {
    "clear": ("miaomiaoRealskin_anima13", "miaomiaoRealskin_anima13"),
    "soft":  ("miaomiaoRealskin_anima13", "miaomiaoAnimeReality_ani11"),
    "gloss": ("miaomiaoAnimeReality_ani11", "miaomiaoRealskin_anima13"),
    "curvy": ("miaomiaoHarem_anima16", "miaomiaoAnimeReality_ani11"),
}
HD_CHANNELS = ["hd_%s_%s" % (t, s) for t in HD_TIERS for s in HD_STYLES]

# 这台 ComfyUI 的**内建**节点。多出来一个就说明混进了自定义节点（那台机器装不上）。
BUILTIN_NODES = {
    "VAELoader", "KSampler", "VAEDecode", "CLIPTextEncode", "UNETLoader",
    "CLIPLoader", "EmptyLatentImage", "LoraLoaderModelOnly", "SaveImage",
    "LatentUpscaleBy",
}

LORA_NODES = ("LoraLoaderModelOnly", "LoraLoader")


# ─── 取数 / 拓扑小工具 ──────────────────────────────────────────────

def _load(name):
    data = load_skill(name)
    assert data and data["workflow"], name + " 加载不到"
    return data["workflow"]


def _raw(name):
    """磁盘原文——`__SEED__` 是裸占位符，解析后变成字符串，只能数原文。"""
    with open(os.path.join(SKILLS, name, "workflow.json"), encoding="utf-8") as f:
        return f.read()


def _all(wf, cls):
    """按 class_type 取节点 id，按数字序排好。"""
    return sorted((k for k, n in wf.items() if n.get("class_type") == cls),
                  key=int)


def _one(wf, cls):
    hits = _all(wf, cls)
    assert len(hits) == 1, "期望恰好一个 %s，实际 %r" % (cls, hits)
    return hits[0]


def _stage1(wf):
    """一段采样器：latent 来自 `EmptyLatentImage` 的那个。"""
    latent = _one(wf, "EmptyLatentImage")
    for k in _all(wf, "KSampler"):
        if wf[k]["inputs"]["latent_image"][0] == latent:
            return k
    raise AssertionError("找不到一段采样器（没有 KSampler 吃 EmptyLatentImage）")


def _stage2(wf):
    """二段采样器：不是一段的那个。"""
    s1 = _stage1(wf)
    rest = [k for k in _all(wf, "KSampler") if k != s1]
    assert len(rest) == 1, "KSampler 数量不是 2：%r" % _all(wf, "KSampler")
    return rest[0]


def _upscale(wf):
    return _one(wf, "LatentUpscaleBy")


def _base_file(wf, model_ref):
    """沿 LoRA 链走到 UNETLoader，返回底模文件名（找不到返回 None）。"""
    nid = model_ref[0]
    seen = set()
    while nid not in seen:
        seen.add(nid)
        n = wf[nid]
        ct = n.get("class_type")
        if ct == "UNETLoader":
            return n["inputs"]["unet_name"]
        if ct == "CheckpointLoaderSimple":
            return n["inputs"]["ckpt_name"]
        if ct in LORA_NODES:
            nid = n["inputs"]["model"][0]
            continue
        return None
    return None


def _lora_chain(wf):
    """返回 (底模装载节点, [(lora节点, 文件名, 强度), ...])，从采样器那头往里走。"""
    nid = wf[_stage1(wf)]["inputs"]["model"][0]
    chain = []
    while wf[nid].get("class_type") in LORA_NODES:
        chain.append((nid, wf[nid]["inputs"]["lora_name"],
                      wf[nid]["inputs"]["strength_model"]))
        nid = wf[nid]["inputs"]["model"][0]
    return nid, chain


# ─── 渠道集合 ──────────────────────────────────────────────────────

class ChannelSetTest(unittest.TestCase):
    """恰好四个动漫渠道，老渠道一个都不许回来。"""

    def test_exactly_the_four_channels_exist(self):
        found = [s for s in list_skills() if s.startswith("anima_")]
        self.assertEqual(sorted(found), sorted(CHANNELS),
                         "动漫渠道集合变了——加/删渠道是有意为之吗？")

    def test_each_is_a_scannable_image_skill(self):
        for name in CHANNELS:
            data = load_skill(name)
            self.assertIsNotNone(data, name)
            self.assertEqual(data["kind"], "生图", name)
            self.assertIsNotNone(data["workflow"], name)

    def test_none_is_marked_as_heavy(self):
        """四个都是普通权重——重渠道那套是给 qwen 准备的，跟动漫渠道无关。"""
        for name in CHANNELS:
            self.assertEqual(skill_priority(name), 1, name)

    def test_retired_channels_stay_archived(self):
        """`anima` / `anima_realskin` / `anima_2` 已归档。

        它们在 `skills/_archive_20260930/` 下，`list_skills()` 跳过 `_` 开头的
        目录——**这条锁住那个 skip**（曾经它漏进去过，会让管理页多出死渠道）。

        ⚠️ 这条 2026-10-01 真的红过一次：注释里写了好几天「已归档」，但归档目录
        根本没建、`skills/anima` 一直在原地——所以「归档」必须**物理上挪走**才算数，
        光改注释和名单没用。`image_gen_v1` 那天被用户拍板保留，已移出名单。
        """
        skills = list_skills()
        for name in RETIRED:
            self.assertNotIn(name, skills, "%s 不该再出现在技能列表里" % name)
            self.assertIsNone(load_skill(name),
                              "%s 已经归档，不该还能加载" % name)


# ─── 四个渠道共用的骨架 ────────────────────────────────────────────

class SharedSkeletonTest(unittest.TestCase):
    """四个渠道共用同一套两段采样骨架——逐渠道过一遍。"""

    def test_two_samplers_one_upscale(self):
        for name in CHANNELS:
            wf = _load(name)
            self.assertEqual(len(_all(wf, "KSampler")), 2, name)
            self.assertEqual(len(_all(wf, "LatentUpscaleBy")), 1, name)
            self.assertEqual(len(_all(wf, "VAEDecode")), 1, name)
            self.assertEqual(len(_all(wf, "SaveImage")), 1, name)

    def test_second_pass_reads_the_upscaled_latent(self):
        """链路：一段 → 放大 → 二段 → 解码。

        **别把二段的 latent 改回直接吃一段** —— 那等于把放大节点晾在一边
        （它还占着一次上采样）。
        """
        for name in CHANNELS:
            wf = _load(name)
            s1, s2, up = _stage1(wf), _stage2(wf), _upscale(wf)
            self.assertEqual(wf[up]["inputs"]["samples"], [s1, 0], name)
            self.assertEqual(wf[s2]["inputs"]["latent_image"], [up, 0], name)
            self.assertEqual(wf[_one(wf, "VAEDecode")]["inputs"]["samples"],
                             [s2, 0], name)

    def test_both_passes_carry_the_prompt_not_a_fixed_character(self):
        """两段都读节点 4（正向）/ 8（负向）——两段都真的在跑同一段提示词。"""
        for name in CHANNELS:
            wf = _load(name)
            for s in (_stage1(wf), _stage2(wf)):
                self.assertEqual(wf[s]["inputs"]["positive"], ["4", 0], name)
                self.assertEqual(wf[s]["inputs"]["negative"], ["8", 0], name)

    def test_first_pass_builds_second_pass_refines(self):
        """一段 denoise=1 从零建构，二段 denoise=0.25 只精修。

        **只锁 denoise 这个结构量**（步数/CFG/一段采样器都是用户随手调的旋钮）。
        """
        for name in CHANNELS:
            wf = _load(name)
            d1 = wf[_stage1(wf)]["inputs"]["denoise"]
            d2 = wf[_stage2(wf)]["inputs"]["denoise"]
            self.assertEqual(d1, 1, name)
            self.assertEqual(d2, 0.25, name)
            self.assertLess(d2, d1, name)

    def test_second_pass_is_deterministic(self):
        """二段必须是确定性采样器 `euler` + `simple`。

        denoise 只有 0.25、是在既有 latent 上补细节；用 SDE 系（`er_sde`）
        会把已经干净的图重新注入噪声——表现为头发糊、蕾丝融、手指断。
        **一段不锁**：它 denoise=1.0 从零建构图，用什么都行。
        """
        for name in CHANNELS:
            wf = _load(name)
            s2 = wf[_stage2(wf)]["inputs"]
            self.assertEqual(s2["sampler_name"], "euler", name)
            self.assertEqual(s2["scheduler"], "simple", name)

    def test_loras_only_on_the_first_pass(self):
        """LoRA 链只挂第一段：底模 → kibro(1.0) → baka skin(0.5) → 一段采样器。

        两个硬约束：
        ① 链必须**终止在 UNETLoader**（不是凭空接一个 LoRA）；
        ② **二段走裸底模**，model 必须直接指向 UNETLoader 节点——「优化」成接
           LoRA 链尾能让 ComfyUI 少装一次模型，但二段画风会跟着变，用户否掉了。
        """
        for name in CHANNELS:
            wf = _load(name)
            base_id, chain = _lora_chain(wf)
            self.assertEqual(wf[base_id]["class_type"], "UNETLoader", name)
            self.assertEqual(len(chain), 2, name)
            by_name = {fn: st for _, fn, st in chain}
            self.assertEqual(len(by_name), 2, "%s 的 LoRA 重名了" % name)
            kibro = [st for fn, st in by_name.items() if "kibro" in fn]
            baka = [st for fn, st in by_name.items() if "baka" in fn]
            self.assertEqual(kibro, [1], name)
            self.assertEqual(baka, [0.5], name)
            # 二段不接 LoRA：它的 model 源节点必须是底模装载器
            s2_model = wf[_stage2(wf)]["inputs"]["model"][0]
            self.assertEqual(wf[s2_model]["class_type"], "UNETLoader", name)

    def test_clip_does_not_go_through_lora(self):
        """CLIP 由 `CLIPLoader(6)` 单独喂，**不经过 LoRA**。

        `LoraLoaderModelOnly` 根本没有 clip 输出槽，接到它上面会被 ComfyUI 拒。
        这条挡住「照抄 SDXL 那套把 clip 也串进 LoRA」。
        """
        for name in CHANNELS:
            wf = _load(name)
            clip_id = _one(wf, "CLIPLoader")
            self.assertEqual(wf[clip_id]["inputs"]["clip_name"],
                             "qwen_3_06b_base.safetensors", name)
            for enc in ("4", "8"):
                self.assertEqual(wf[enc]["inputs"]["clip"], [clip_id, 0], name)

    def test_output_is_a_flat_anima_prefix(self):
        """`SaveImage` 前缀必须是 `Anima`——**不能带 `/`**。

        带 `/` 会被 ComfyUI 当成子目录，机器人回传路径时对不上输出目录。
        """
        for name in CHANNELS:
            wf = _load(name)
            img = wf[_one(wf, "SaveImage")]
            self.assertEqual(img["inputs"]["filename_prefix"], "Anima", name)
            self.assertNotIn("/", img["inputs"]["filename_prefix"], name)

    def test_upscale_factor_is_capped(self):
        """放大倍率封顶 1.1×——`scale_by` 上去是平方级的显存和耗时。

        本机（RTX 5070 12GB + 16GB）两段要先后装两块底模，没余量。
        """
        for name in CHANNELS:
            wf = _load(name)
            up = wf[_upscale(wf)]
            self.assertEqual(up["inputs"]["upscale_method"], "nearest-exact", name)
            self.assertLessEqual(up["inputs"]["scale_by"], 1.1, name)


class WorkflowIntegrityTest(unittest.TestCase):
    """结构性体检——工作流得是「能提交」的图，不只是字段齐全。"""

    def test_no_custom_nodes(self):
        for name in CHANNELS:
            wf = _load(name)
            used = {n.get("class_type") for n in wf.values()}
            self.assertTrue(used <= BUILTIN_NODES,
                            "%s 混进了非内建节点：%r" % (name, used - BUILTIN_NODES))

    def test_every_link_points_at_an_existing_node(self):
        """任何 `[node_id, slot]` 连线都得指到真实存在的节点上。

        手改 workflow.json（尤其换底模节点）时最容易留下的就是悬空连线——
        ComfyUI 提交时才报错，那已经太晚了。
        """
        for name in CHANNELS:
            wf = _load(name)
            for nid, node in wf.items():
                for key, val in node.get("inputs", {}).items():
                    if (isinstance(val, list) and len(val) == 2
                            and isinstance(val[0], str)):
                        self.assertIn(val[0], wf,
                                      "%s 节点 %s.%s 指向不存在的节点 %r"
                                      % (name, nid, key, val[0]))


# ─── 每渠道的底模组合（真正的差异点）────────────────────────────────

class BaseModelTest(unittest.TestCase):
    """底模组合 = 画风。换了就是换了渠道，必须被人看见。"""

    def test_stage_bases_match_the_declared_combo(self):
        for name, (base1, base2, _, _, _) in CHANNELS.items():
            wf = _load(name)
            got1 = _base_file(wf, wf[_stage1(wf)]["inputs"]["model"])
            got2 = _base_file(wf, wf[_stage2(wf)]["inputs"]["model"])
            self.assertTrue(got1 and got1.startswith(base1),
                            "%s 一段底模：期望 %s*，实际 %r" % (name, base1, got1))
            self.assertTrue(got2 and got2.startswith(base2),
                            "%s 二段底模：期望 %s*，实际 %r" % (name, base2, got2))

    def test_soft_and_gloss_are_the_same_pair_reversed(self):
        """`anima_soft` = realskin→reality；`anima_gloss` = reality→realskin。

        这就是「两个渠道其实是同一对底模换个顺序」这件事的锁——用户原来的
        `anime2.json` 就是这个组合，`anima_soft` 是把它反过来。
        """
        soft = CHANNELS["anima_soft"]
        gloss = CHANNELS["anima_gloss"]
        self.assertEqual((soft[0], soft[1]), (gloss[1], gloss[0]))

    def test_clear_and_soft_share_the_first_stage_base(self):
        """`anima_clear` 和 `anima_soft` **第一段底模相同**，只差第二段。

        所以它俩出图很像——这也是「默认渠道在它俩之间换」几乎零成本的原因，
        以及为什么 skill.md 里反复写「拿不准就用默认」。
        """
        self.assertEqual(CHANNELS["anima_clear"][0], CHANNELS["anima_soft"][0])
        self.assertNotEqual(CHANNELS["anima_clear"][1], CHANNELS["anima_soft"][1])

    def test_clear_reuses_one_base_for_both_passes(self):
        """`anima_clear` 两段是**同一块** realskin——所以它只有一块底模装载器。

        同文件不会重复吃显存（ComfyUI 的 `model_management` 按 model 对象缓存），
        所以这是零成本的；但它确实是四个里唯一「单底模」的。
        """
        wf = _load("anima_clear")
        self.assertEqual(len(_all(wf, "UNETLoader")), 1)
        s1_base = _base_file(wf, wf[_stage1(wf)]["inputs"]["model"])
        s2_base = _base_file(wf, wf[_stage2(wf)]["inputs"]["model"])
        self.assertEqual(s1_base, s2_base)
        # 二段直接指向节点 5（那段底模装载器本身），没有第二块
        self.assertEqual(wf[_stage2(wf)]["inputs"]["model"],
                         [_one(wf, "UNETLoader"), 0])

    def test_the_other_three_have_two_loaders(self):
        for name in ("anima_soft", "anima_gloss", "anima_curvy"):
            self.assertEqual(len(_all(_load(name), "UNETLoader")), 2, name)


# ─── 分辨率 / 输出尺寸 ─────────────────────────────────────────────

class ResolutionTest(unittest.TestCase):
    """**安全锁**：分辨率是独立杠杆，别往上调。"""

    def test_canvas_matches_the_declared_size(self):
        """2026-09-27 调到 1024×1536 后，单张图就把整机拖到黑屏关机。

        728×1024 / 768×1024 是实测的安全档。想调高先读 `skills/anima_*/skill.md`
        里那段警告，别直接改这些数。
        """
        for name, (_, _, w, h, _) in CHANNELS.items():
            latent = _load(name)[_one(_load(name), "EmptyLatentImage")]["inputs"]
            self.assertEqual((latent["width"], latent["height"]), (w, h), name)

    # 渠道 → 实际输出尺寸（像素）
    OUTPUT = {
        "anima_soft": (728, 1024),
        "anima_gloss": (848, 1128),
        "anima_curvy": (728, 1024),
        "anima_clear": (728, 1024),
    }

    def test_output_size_is_the_canvas_times_the_upscale(self):
        """实际输出 = 画布 ÷ 8（→ latent）→ `round()` → × scale_by → × 8。

        ComfyUI 的 `LatentUpscaleBy` 先按 8 折成 latent 再放大（`nodes.py:1393`）。
        只有 `anima_gloss` 用 1.1×：768×1024 → 96×128 → `round(105.6)=106`、
        `round(140.8)=141` → **848×1128**。其余三个 `scale_by=1`，输出 = 画布。
        """
        for name, (_, _, w, h, scale) in CHANNELS.items():
            got = (round(w / 8 * scale) * 8, round(h / 8 * scale) * 8)
            self.assertEqual(got, self.OUTPUT[name],
                             "%s 输出尺寸：期望 %r，按公式算得 %r"
                             % (name, self.OUTPUT[name], got))


# ─── 提示词模板 ────────────────────────────────────────────────────

class PromptTemplateTest(unittest.TestCase):
    """节点 4 必须是**可代入的模板**，不能是写死的角色。"""

    def test_positive_is_the_kibro_template(self):
        """模板恒为 `@kibro, __MULTI_PROMPTS__`。

        `@kibro` 是 okitatsuki LoRA 的触发词，**不能丢**——LoRA 文件元数据
        `ss_tag_frequency` 里它出现 165/165 次（训练集每张图都带）。

        这个坑踩过多次：把用户在 ComfyUI 里调好的工作流搬进 skills 时，节点 4
        存的是**那张图当时的完整提示词**（几百上千字）。`generate_image` 的机制
        是「把 `__MULTI_PROMPTS__` 换成模型给的提示词」——那串字里没有占位符，
        于是模型传什么都被原样丢弃，连着画好几张都是同一个角色。**每次从
        ComfyUI 导工作流都会带上这个坑，所以这条锁必须一直在。**
        """
        for name in CHANNELS:
            wf = _load(name)
            self.assertEqual(wf["4"]["inputs"]["text"],
                             "@kibro, __MULTI_PROMPTS__", name)

    def test_negative_is_the_shared_quality_string(self):
        for name in CHANNELS:
            wf = _load(name)
            neg = wf["8"]["inputs"]["text"]
            self.assertIn("worst quality", neg, name)
            self.assertNotIn("__MULTI_PROMPTS__", neg, name)

    def test_seeds_stay_placeholders(self):
        """两个 KSampler 的 seed 都必须是裸 `__SEED__` 占位符。

        （改节点 4 时踩过：用 `json.loads(replace('__SEED__','0'))` 读进来再
        `json.dumps` 写回，占位符被永久变成了 0 —— 每张图都一模一样。所以改成
        字符串级替换，并加这条锁。写死成数字等于每张图同一个种子。）
        """
        for name in CHANNELS:
            self.assertEqual(_raw(name).count("__SEED__"), 2, name)

    def test_no_character_placeholder_anywhere(self):
        """`__CHARACTER__` 随角色底模机制一起下线了，工作流里不该再有。"""
        for name in CHANNELS:
            self.assertNotIn("__CHARACTER__", _raw(name), name)
            self.assertNotIn("character.txt",
                             os.listdir(os.path.join(SKILLS, name)), name)


# ─── 可见性 / 默认渠道 ─────────────────────────────────────────────

class VisibilityTest(unittest.TestCase):
    """QQ 机器人得能看见四个渠道，否则默认渠道也传不进来。"""

    def test_qq_whitelist_includes_all_four(self):
        from app import agents
        for name in CHANNELS:
            self.assertTrue(agents.allows_skill("qq", name), name)

    def test_default_channel_constant_is_the_declared_one(self):
        """不点名时的默认渠道恒为 `DEFAULT_CHANNEL`（当前 = `silver`）。

        signature 的 `skill` 默认值必须是 None——`execute_tool` 是 `fn(**args)`，
        只有「模型压根没传 skill」才会落到默认值，靠它才分得开「没点名」和
        「点名了默认渠道」。
        """
        import inspect
        from app.tools.normal.generate_image import (
            _generate_image, T2I_DEFAULT_SKILL)
        self.assertEqual(T2I_DEFAULT_SKILL, DEFAULT_CHANNEL)
        self.assertIsNotNone(load_skill(T2I_DEFAULT_SKILL))
        self.assertIsNone(inspect.signature(_generate_image)
                          .parameters["skill"].default)

    def test_skill_list_marks_exactly_one_channel_as_default(self):
        """整个 Skill 目录里**恰好一个**渠道自称默认，且那个是 `DEFAULT_CHANNEL`。

        「默认」这两个字**纯粹是从 skill.md 标题读出来的**——
        `agent_prompt._build_skill_list` 没有任何判定逻辑，所以换默认渠道
        **必须手改 skill.md 的标题**（漏了这条就会有两个/零个自称默认）。
        动漫那一族的标题只许说「该族默认画风」，不许说「默认生图渠道」。
        """
        from app.agent_prompt import _build_skill_list
        lines = _build_skill_list("qq").splitlines()
        defaults = [l for l in lines if "默认生图渠道" in l]
        self.assertEqual(len(defaults), 1, "有多个渠道自称默认：%r" % defaults)
        self.assertIn(DEFAULT_CHANNEL, defaults[0])
        for name in CHANNELS:
            line = [l for l in lines if "**%s**" % name in l]
            self.assertEqual(len(line), 1, name)
            self.assertNotIn("默认生图渠道", line[0],
                             "%s 的标题还自称默认生图渠道" % name)

    def test_tool_description_covers_all_four_and_no_retired_names(self):
        """工具描述要写全四个渠道，且**一个老渠道名都不许出现**。

        `anima` 得用词边界匹配——它是 `anima_soft` / `anima_gloss` 的前缀，
        直接 `assertNotIn("anima")` 会误伤新渠道名。
        """
        import re
        from app.tools.normal.generate_image import tool
        desc = tool["description"]
        for name in CHANNELS:
            self.assertIn(name, desc, "描述里缺了渠道 %s" % name)
        self.assertIsNone(re.search(r"\banima\b", desc),
                          "描述里还留着裸的旧渠道名 anima")
        for name in ("anima_realskin", "anima_2"):
            self.assertNotIn(name, desc, "描述里还留着老渠道 %s" % name)
        # `image_gen_v1` 2026-10-01 起是**保留渠道**，所以不再按「老渠道」排除；
        # 但它也确实没被写进描述（见 RETIRED 上面的说明）——这里不断言它的出现与否。
        # 参数说明里的可选值也要对得上
        self.assertIn(DEFAULT_CHANNEL, tool["parameters"]["properties"]["skill"]["description"])


class DefaultChannelTest(unittest.TestCase):
    """默认渠道是**唯一真相源**——凡是话术里提到它的地方都必须跟着走。

    ## 这条锁为什么必须存在

    默认渠道在本项目换过**四次**（`anima` → `anima_realskin` → `anima_soft`
    → `anima_clear` → `silver`，2026-10-06），**每一次都有写死名字的地方漏掉**。
    最阴的一次是 `generate_image` 里那两句**拒收话术**：

        "直接改用默认的 anima_realskin 重画（prompt 改写成 anima 的标签式英文写法）"

    —— 它们是**字符串**，按代码 grep 搜不到、测试也没覆盖，于是渠道归档之后
    这句话还挂在代码里好几天，模型会照着它去点名一个不存在的渠道。
    现在话术改成 `+ T2I_DEFAULT_SKILL +` 拼接，本类负责钉住「别再写回字面量」。

    ## 换默认渠道时要改的清单（改完跑本类 + 全量）

    `app/tools/normal/generate_image.py`（常量 + 工具描述 + `skill` 参数说明）、
    `app/direct_gen.py`（`_DEFAULT_SKILL` + 菜单 / 指南 / 两个模板里的渠道段）、
    `app/agent_prompt.py`（`_TOOL_HINTS` 的渠道那行 + `_BRIEF_PARAM_EXTRA`）、
    当前默认渠道自己的 `skills/<它>/skill.md` 标题、
    4 个 `skills/anima_*/skill.md` 的渠道表（别再自称默认）。
    **例外**：`comfy_workflow.py` 那两个工具的默认 skill 是「两段采样骨架的样本
    渠道」，跟生图默认渠道无关，见 `WORKFLOW_TOOL_CHANNEL`。
    """

    def test_tool_description_names_exactly_the_current_default(self):
        """描述里要提当前默认，且**不许把别的渠道标成「默认」**。

        「别的渠道（默认）」这种串台写法比漏写更糟——模型会照它去传 skill。
        """
        from app.tools.normal.generate_image import tool
        desc = tool["description"]
        self.assertIn(DEFAULT_CHANNEL, desc)
        for other in CHANNELS:
            if other != DEFAULT_CHANNEL:
                self.assertNotIn(other + "（默认）", desc,
                                 "描述里把 %s 也标成了默认" % other)

    def test_skill_param_description_names_the_current_default(self):
        from app.tools.normal.generate_image import tool
        prop = tool["parameters"]["properties"]["skill"]["description"]
        self.assertIn(DEFAULT_CHANNEL, prop)
        for other in CHANNELS:
            if other != DEFAULT_CHANNEL:
                self.assertNotIn(other + "（默认", prop,
                                 "参数说明里把 %s 也标成了默认" % other)

    def test_comfy_workflow_defaults_follow_the_sample_channel(self):
        """`get_workflow` / `update_workflow` 的默认 `skill` 钉在样本渠道上。

        这里踩过一次：默认值曾经写死 `image_gen_v1`，SD 渠道归档之后
        **不传 skill 直接抛「没有 workflow.json」**——工具看起来像坏了。

        2026-10-06 默认**生图**渠道换成 silver 时，这两个默认值**故意没跟**：
        那两个工具按「两段采样」骨架读写工作流，`anima_clear` 才是套得上去的
        样本渠道。所以这里断言的是 `WORKFLOW_TOOL_CHANNEL`，不是
        `DEFAULT_CHANNEL`——但参数说明必须把「它不是默认生图渠道」讲明白，
        否则模型会以为不传 skill 的生图也走 anima_clear。
        """
        import inspect
        from app.tools.normal import comfy_workflow as cw
        for fn in (cw.get_workflow, cw.update_workflow):
            self.assertEqual(inspect.signature(fn).parameters["skill"].default,
                             WORKFLOW_TOOL_CHANNEL, fn.__name__)
        for name, spec in (("get_workflow", cw.tool), ("update_workflow", cw.tool_update)):
            self.assertEqual(spec["name"], name)
            prop = spec["parameters"]["properties"]["skill"]["description"]
            self.assertIn("默认生图渠道是 silver", prop, name)

    def test_refusal_messages_are_actionable_and_leak_no_retired_channel(self):
        """两句**拒收话术**都得给出路，且不许出现已归档的渠道名。

        ① 垫图被拒的那句（点到了不给垫图的档，如 `hd_3_*`）：要让模型知道
           「去掉 source_image 按文生图重来」，或换一个支持垫图的档。
           —— 这里**不要求**它指到默认渠道：用户点名 hd_3 时把他往 anima_clear
           引是降级，正确出路是「同一个渠道别垫图」或「换到 hd_2 / hd_fast」。
        ② 停用渠道那句：必须指到**当前**默认渠道。
           2026-10-01 起真配置里**已经没有停用渠道**（全部解封），所以这里
           **显式 patch** 一份停用名单来验机制，不再依赖真配置。

        两句都拦在**任何网络调用之前**（垫图那句只读一次本地 skill 文件），
        所以这里能直接调，不需要 mock ComfyUI。
        """
        from app.tools.normal import generate_image as gi

        with mock.patch.object(gi, "DISABLED_IMAGE_SKILLS", ["krea2"]), \
                mock.patch.object(gi, "is_cancelled", lambda: False), \
                mock.patch.object(gi, "_qq_gate", lambda: None):
            i2i = gi._generate_image(prompt="x", skill="hd_3_clear",
                                     source_image="1")
            off = gi._generate_image(prompt="x", skill="krea2")

        self.assertIn("不支持图生图", i2i)            # 确实是那句拒收
        self.assertIn("source_image", i2i)          # 且给出了路：去掉它
        self.assertIn("停用", off)                   # 确实是那句拒收
        self.assertIn(DEFAULT_CHANNEL, off, "停用渠道拒收话术没指到当前默认渠道")

        # 历史渠道名一个都不许出现在话术里（它们已经不在 skills/ 了）
        for msg in (i2i, off):
            for retired in RETIRED:
                self.assertNotIn(retired + " ", msg,
                                 "拒收话术里还留着已归档的渠道名 %s" % retired)

    def test_docs_agree_on_which_channel_is_default(self):
        """默认渠道的 skill.md 标题标「默认」，其余（含动漫那四个）都不许标。

        动漫那一族自己的文档以前写着「`anima_clear`（默认）· 不点名就用它」，
        2026-10-06 起那不成立，也不许写回来：不点名走的是 `silver`。
        """
        def title(name):
            with open(os.path.join(SKILLS, name, "skill.md"), encoding="utf-8") as f:
                return [l for l in f.read().splitlines() if l.startswith("# ")][0]

        self.assertIn("默认生图渠道", title(DEFAULT_CHANNEL),
                      "%s 的 skill.md 标题没标「默认生图渠道」" % DEFAULT_CHANNEL)
        for name in tuple(CHANNELS) + ("jank",):
            first = title(name)
            self.assertNotIn("默认生图渠道", first,
                             "%s 的 skill.md 标题还自称默认生图渠道：%r" % (name, first))
            self.assertNotIn("（默认）", first,
                             "%s 的 skill.md 标题还带着旧的「（默认）」：%r" % (name, first))


class SkillDocTest(unittest.TestCase):
    """每个渠道的 skill.md 得指到**现在还在**的测试文件。"""

    def test_docs_point_at_this_test_module(self):
        for name in CHANNELS:
            path = os.path.join(SKILLS, name, "skill.md")
            with open(path, encoding="utf-8") as f:
                text = f.read()
            self.assertIn("tests.test_image_channels", text,
                          "%s 的 skill.md 没指到本测试文件" % name)
            self.assertNotIn("test_image_anima", text,
                             "%s 的 skill.md 还在指已归档的测试" % name)
            self.assertNotIn("test_image_sd", text, name)


class RoleNameRuleTest(unittest.TestCase):
    """角色还原靠**名字**，不靠外貌堆砌（2026-10-04 拍板）。

    现场：同一轮「婚礼」需求，AI 连跑 5 个渠道。写了 `rudeus greyrat,
    mushoku tensei` 的 nai / anima / nffa 都像，唯独 qwen 那版把名字丢了、
    只写「一位年轻男子」，角色全走形。

    根因是规则本身把「角色」定义成了外貌清单（发型 / 发色 / 瞳色 / 体型 /
    服装 / 年龄），一个字都没提名字——模型照着清单交差，自然只写外貌。

    这组断言钉住四件事：名字写最前、qwen 的自然语言不算例外、
    用户报的名字不许省、认不出要说明。
    """

    @staticmethod
    def _desc():
        from app.tools.normal.generate_image import tool
        return tool["description"]

    def test_tool_description_puts_the_name_first(self):
        desc = self._desc()
        self.assertIn("认得出就把名字写在 prompt 最前面", desc)
        self.assertIn("名字写在 prompt 最前面", desc)
        # 外貌降为补充——不然模型照样堆一长串设定，还容易和原作打架
        self.assertIn("只补与原设定不同的地方", desc)

    def test_qwen_is_not_an_exception(self):
        """qwen 那条最容易被误解成「写自然语言就不用写专有名词」。

        它自己的 skill.md 还写着「像在跟人描述画面」，不点破这层，模型
        切进描述模式就会把角色退化成「一位少女」。
        """
        self.assertIn("自然语言句子里照样要写名字", self._desc())

    def test_prompt_param_also_says_start_with_the_name(self):
        from app.tools.normal.generate_image import tool
        param = tool["parameters"]["properties"]["prompt"]["description"]
        self.assertIn("开头先写角色名", param)

    def test_user_supplied_name_must_be_kept_verbatim(self):
        self.assertIn("一个字都不许省", self._desc())

    def test_unknown_character_must_be_admitted(self):
        self.assertIn("是按外形画的", self._desc())

    def test_qwen_skill_md_carries_the_same_rule(self):
        path = os.path.join(SKILLS, "qwen_image_v1", "SKILL.md")
        with open(path, encoding="utf-8") as f:
            text = f.read()
        self.assertIn("写自然语言 ≠ 省略专有名词", text)


# ─── 12 个高清渠道（3 档 × 4 画风）──────────────────────────────────

class HdChannelTest(unittest.TestCase):
    """12 个高清渠道的架构锁。

    2026-10-01 由 `_make_hd_channels.py` 从「尺寸骨架 + 画风底模组合」展开生成。
    它们跟 4 个常规 anima 渠道**共用同一套两段采样骨架**——只是画布更大、
    底模按画风换、一段采样器 / 二段步数按档位换。架构断言基本照搬上面的
    `SharedSkeletonTest` / `BaseModelTest`，只是画布 / 放大倍率 / 采样器按档位走。
    """

    def test_all_twelve_exist_and_nothing_else(self):
        """恰好这 12 个 hd_* 渠道；多一个 / 少一个都说明生成脚本跑歪了。"""
        found = sorted(s for s in list_skills() if s.startswith("hd_"))
        self.assertEqual(found, sorted(HD_CHANNELS),
                         "高清渠道集合变了——重跑 _make_hd_channels.py 了吗？")

    def test_each_is_a_scannable_image_skill(self):
        for name in HD_CHANNELS:
            data = load_skill(name)
            self.assertIsNotNone(data, name)
            self.assertEqual(data["kind"], "生图", name)
            self.assertIsNotNone(data["workflow"], name)

    def test_qq_whitelist_includes_all_twelve(self):
        """QQ 机器人必须能看见这 12 个渠道，否则默认渠道传不进来、也点不到。

        这条直接钉住 `agents/qq/agent.json` 的 skills 白名单——**加高清渠道时
        忘了把它写进白名单**是这类改动最容易踩的坑（模型描述里写了 16 个，
        白名单却只有 4 个，结果点名 hd_* 直接被 `allows_skill` 拒）。
        """
        from app import agents
        for name in HD_CHANNELS:
            self.assertTrue(agents.allows_skill("qq", name), name)

    def test_skeleton_is_the_shared_two_stage(self):
        for name in HD_CHANNELS:
            wf = _load(name)
            self.assertEqual(len(_all(wf, "KSampler")), 2, name)
            self.assertEqual(len(_all(wf, "LatentUpscaleBy")), 1, name)
            self.assertEqual(len(_all(wf, "VAEDecode")), 1, name)
            self.assertEqual(len(_all(wf, "SaveImage")), 1, name)
            self.assertEqual(len(_all(wf, "UNETLoader")), 2, name)
            self.assertEqual(len(_all(wf, "LoraLoaderModelOnly")), 2, name)
            self.assertEqual(len(_all(wf, "CLIPLoader")), 1, name)
            self.assertEqual(len(_all(wf, "EmptyLatentImage")), 1, name)

    def test_topology_stage1_upscale_stage2_decode(self):
        for name in HD_CHANNELS:
            wf = _load(name)
            s1, s2, up = _stage1(wf), _stage2(wf), _upscale(wf)
            self.assertEqual(wf[up]["inputs"]["samples"], [s1, 0], name)
            self.assertEqual(wf[s2]["inputs"]["latent_image"], [up, 0], name)
            self.assertEqual(wf[_one(wf, "VAEDecode")]["inputs"]["samples"],
                             [s2, 0], name)

    def test_both_passes_read_the_prompt_template(self):
        for name in HD_CHANNELS:
            wf = _load(name)
            for s in (_stage1(wf), _stage2(wf)):
                self.assertEqual(wf[s]["inputs"]["positive"], ["4", 0], name)
                self.assertEqual(wf[s]["inputs"]["negative"], ["8", 0], name)

    def test_first_pass_builds_second_pass_refines(self):
        for name in HD_CHANNELS:
            wf = _load(name)
            d1 = wf[_stage1(wf)]["inputs"]["denoise"]
            d2 = wf[_stage2(wf)]["inputs"]["denoise"]
            self.assertEqual(d1, 1, name)
            self.assertEqual(d2, 0.25, name)
            self.assertLess(d2, d1, name)

    def test_second_pass_is_deterministic(self):
        for name in HD_CHANNELS:
            wf = _load(name)
            s2 = wf[_stage2(wf)]["inputs"]
            self.assertEqual(s2["sampler_name"], "euler", name)
            self.assertEqual(s2["scheduler"], "simple", name)

    def test_loras_only_on_the_first_pass(self):
        for name in HD_CHANNELS:
            wf = _load(name)
            base_id, chain = _lora_chain(wf)
            self.assertEqual(wf[base_id]["class_type"], "UNETLoader", name)
            self.assertEqual(len(chain), 2, name)
            by_name = {fn: st for _, fn, st in chain}
            self.assertEqual(len(by_name), 2, name)
            kibro = [st for fn, st in by_name.items() if "kibro" in fn]
            baka = [st for fn, st in by_name.items() if "baka" in fn]
            self.assertEqual(kibro, [1], name)
            self.assertEqual(baka, [0.5], name)
            # 二段不接 LoRA：它的 model 源节点必须是底模装载器
            s2_model = wf[_stage2(wf)]["inputs"]["model"][0]
            self.assertEqual(wf[s2_model]["class_type"], "UNETLoader", name)

    def test_clip_does_not_go_through_lora(self):
        for name in HD_CHANNELS:
            wf = _load(name)
            clip_id = _one(wf, "CLIPLoader")
            self.assertEqual(wf[clip_id]["inputs"]["clip_name"],
                             "qwen_3_06b_base.safetensors", name)
            for enc in ("4", "8"):
                self.assertEqual(wf[enc]["inputs"]["clip"], [clip_id, 0], name)

    def test_base_combos_match_the_style(self):
        """高清渠道的画风 = 两段底模组合，跟同画风的常规渠道是同一套组合。"""
        for name in HD_CHANNELS:
            tier, style = name.split("_")[1], name.split("_")[2]
            base1, base2 = HD_STYLES[style]
            wf = _load(name)
            got1 = _base_file(wf, wf[_stage1(wf)]["inputs"]["model"])
            got2 = _base_file(wf, wf[_stage2(wf)]["inputs"]["model"])
            self.assertTrue(got1 and got1.startswith(base1),
                            "%s 一段底模：期望 %s*，实际 %r" % (name, base1, got1))
            self.assertTrue(got2 and got2.startswith(base2),
                            "%s 二段底模：期望 %s*，实际 %r" % (name, base2, got2))

    def test_resolution_matches_the_tier(self):
        """画布恒为 1024×1536，放大倍率按档位走，输出 = 画布 × scale_by。

        这条锁住「别把高清渠道的画布 / 放大倍率调歪」——它们比常规渠道重得多，
        画布一旦往上调就会重演 2026-09-27 把整机拖黑屏的事故。
        """
        for name in HD_CHANNELS:
            tier = name.split("_")[1]
            cw, ch, scale, ow, oh, _, _, _ = HD_TIERS[tier]
            wf = _load(name)
            latent = wf[_one(wf, "EmptyLatentImage")]["inputs"]
            self.assertEqual((latent["width"], latent["height"]), (cw, ch), name)
            up = wf[_upscale(wf)]
            self.assertEqual(up["inputs"]["upscale_method"], "nearest-exact", name)
            self.assertAlmostEqual(up["inputs"]["scale_by"], scale, msg=name)
            got = (round(cw / 8 * scale) * 8, round(ch / 8 * scale) * 8)
            self.assertEqual(got, (ow, oh),
                             "%s 输出：期望 %r，按公式算得 %r" % (name, (ow, oh), got))

    def test_stage1_sampler_and_steps_match_the_tier(self):
        for name in HD_CHANNELS:
            tier = name.split("_")[1]
            _, _, _, _, _, samp1, st1, st2 = HD_TIERS[tier]
            wf = _load(name)
            s1 = wf[_stage1(wf)]["inputs"]
            self.assertEqual(s1["sampler_name"], samp1, name)
            self.assertEqual(s1["steps"], st1, name)
            self.assertEqual(wf[_stage2(wf)]["inputs"]["steps"], st2, name)

    def test_output_prefix_is_flat_anima(self):
        for name in HD_CHANNELS:
            wf = _load(name)
            img = wf[_one(wf, "SaveImage")]
            self.assertEqual(img["inputs"]["filename_prefix"], "Anima", name)
            self.assertNotIn("/", img["inputs"]["filename_prefix"], name)

    def test_no_custom_nodes(self):
        for name in HD_CHANNELS:
            wf = _load(name)
            used = {n.get("class_type") for n in wf.values()}
            self.assertTrue(used <= BUILTIN_NODES,
                            "%s 混进了非内建节点：%r" % (name, used - BUILTIN_NODES))

    def test_every_link_points_at_an_existing_node(self):
        for name in HD_CHANNELS:
            wf = _load(name)
            for nid, node in wf.items():
                for key, val in node.get("inputs", {}).items():
                    if (isinstance(val, list) and len(val) == 2
                            and isinstance(val[0], str)):
                        self.assertIn(val[0], wf,
                                      "%s 节点 %s.%s 指向不存在的节点 %r"
                                      % (name, nid, key, val[0]))

    def test_prompt_template_and_seeds(self):
        for name in HD_CHANNELS:
            wf = _load(name)
            self.assertEqual(wf["4"]["inputs"]["text"],
                             "@kibro, __MULTI_PROMPTS__", name)
            neg = wf["8"]["inputs"]["text"]
            self.assertIn("worst quality", neg, name)
            self.assertNotIn("__MULTI_PROMPTS__", neg, name)
            self.assertEqual(_raw(name).count("__SEED__"), 2, name)
            self.assertNotIn("__CHARACTER__", _raw(name), name)

    def test_skill_md_is_accurate_and_generated(self):
        """每个 hd 渠道的 skill.md 得指到本渠道、列出正确的两段底模、
        且声明是生成脚本产物（别手改 workflow.json）。"""
        for name in HD_CHANNELS:
            tier, style = name.split("_")[1], name.split("_")[2]
            base1, base2 = HD_STYLES[style]
            path = os.path.join(SKILLS, name, "skill.md")
            with open(path, encoding="utf-8") as f:
                text = f.read()
            first = [l for l in text.splitlines() if l.startswith("# ")][0]
            self.assertTrue(first.startswith("# " + name),
                            "%s 的 skill.md 标题没以渠道名开头：%r" % (name, first))
            self.assertIn(base1, text, "%s 的 skill.md 没列一段底模 %s" % (name, base1))
            self.assertIn(base2, text, "%s 的 skill.md 没列二段底模 %s" % (name, base2))
            self.assertIn("_make_hd_channels.py", text,
                          "%s 的 skill.md 没说明是生成脚本产物" % name)

    def test_tool_description_covers_all_sixteen(self):
        """工具描述要写全 16 个渠道（4 画风 + 12 高清），且数对齐。"""
        from app.tools.normal.generate_image import tool
        desc = tool["description"]
        # 「16 个」现在指的是**动漫渠道**那一族（本机另有 qwen_image_v1、
        # image_gen_v1、krea2、nffa 四个非动漫渠道，加上云端的 nai 共 21 个可传名字），
        # 所以字面量带着「动漫」两个字。
        self.assertIn("16 个", desc)
        self.assertIn("动漫渠道", desc)
        for prefix in ("hd_fast_", "hd_2_", "hd_3_"):
            self.assertIn(prefix, desc, "描述里没提到档位 %s" % prefix)
        self.assertIn(DEFAULT_CHANNEL, desc)
        self.assertIn(DEFAULT_CHANNEL,
                      tool["parameters"]["properties"]["skill"]["description"])


class NffaChannelTest(unittest.TestCase):
    """`nffa`（2026-10-02 上线）的形状锁。

    它跟上面那 16 个动漫渠道**不是一套骨架**：底模是 Illustrious 系 SDXL
    （`CheckpointLoaderSimple`，不是 UNETLoader），LoRA 是**两个槽串联**
    （画风 `NffaV1.3` → 描边 `add_outline_XL`，2026-10-03 用户按 UI 实测固化），
    而且出图前后固定跑**两段修复**（`FaceDetailer` + YOLO 检测框，先手后脸）。
    所以它用的是自定义节点（comfyui-impact-pack），`test_no_custom_nodes`
    那两条只圈动漫渠道，别把它套进去。

    锁的是结构（谁修手、谁修脸、LoRA 挂在哪、seed 几处），不锁步数/CFG——
    那些是用户随手调的旋钮。零网络、零显卡：只读 `skills/nffa/`。
    """

    NAME = "nffa"

    def _wf(self):
        return _load(self.NAME)

    def test_base_and_two_lora_slots(self):
        """底模 + 两个 LoRA 槽**串联**：画风槽 `NffaV1.3` 挂在底模之后，
        描边槽 `add_outline_XL` 串在它后面，下游一律吃**链尾**。

        链条顺序是本渠道的关键：漏改引用的话下游还挂在画风槽上，
        add_outline 就**静默不生效**（不报错、只是没描边），所以这里
        还要逐条查「除了链尾，还有谁指着画风槽」。
        """
        wf = self._wf()
        ckpt = _one(wf, "CheckpointLoaderSimple")
        self.assertTrue(wf[ckpt]["inputs"]["ckpt_name"]
                        .startswith("waiIllustriousSDXL"),
                        wf[ckpt]["inputs"]["ckpt_name"])
        slots = _all(wf, "LoraLoader") + _all(wf, "LoraLoaderModelOnly")
        self.assertEqual(len(slots), 2, slots)
        # 画风槽：挂在底模之后，双强度 1.0
        style = [k for k in slots
                 if wf[k]["inputs"]["lora_name"].startswith("NffaV1.3")]
        self.assertEqual(len(style), 1, style)
        style = style[0]
        self.assertEqual(wf[style]["inputs"]["strength_model"], 1)
        self.assertEqual(wf[style]["inputs"]["strength_clip"], 1)
        self.assertEqual(wf[style]["inputs"]["model"], [ckpt, 0])
        # 描边槽：串在画风槽之后（0.3 是用户自己调出来那个强度）
        outline = [k for k in slots if k != style][0]
        self.assertEqual(wf[outline]["inputs"]["lora_name"],
                         "add_outline_XL.safetensors")
        self.assertEqual(wf[outline]["inputs"]["model"], [style, 0])
        self.assertEqual(wf[outline]["inputs"]["clip"], [style, 1])
        self.assertEqual(wf[outline]["inputs"]["strength_model"], 0.3)
        # 除了描边槽自己，**不该有人还指着画风槽**——指着它等于绕开了描边
        for nid, node in wf.items():
            if nid == outline:
                continue
            for key, val in node.get("inputs", {}).items():
                if isinstance(val, list) and val and val[0] == style:
                    self.fail("节点 %s.%s 还挂在画风槽 %s 上，应改指链尾 %s"
                              % (nid, key, style, outline))
        self.assertNotIn("Dogma", json.dumps(wf))   # 那台机器上还没这个文件

    def test_two_stage_sampling_then_two_detailers(self):
        """采样骨架：一段 → 1.1× 放大 → 二段 → VAEDecode → 修手 → 修脸 → 存图。

        顺序是本渠道最大的结构差别，**先手后脸**不能颠倒（两段都改像素，
        后者覆盖前者；用户测成功的那张就是这个顺序）。
        """
        wf = self._wf()
        samplers = _all(wf, "KSampler")
        self.assertEqual(len(samplers), 2, samplers)
        up = _one(wf, "LatentUpscaleBy")
        # 按连线认「二段」：吃放大结果的那个采样器——不认编号
        stage2 = [k for k in samplers if wf[k]["inputs"]["latent_image"][0] == up]
        self.assertEqual(len(stage2), 1, "二段采样器没接在放大之后")
        stage1 = [k for k in samplers if k != stage2[0]][0]
        self.assertEqual(wf[up]["inputs"]["samples"][0], stage1)
        self.assertLessEqual(wf[up]["inputs"]["scale_by"], 1.1)
        decode = _one(wf, "VAEDecode")
        self.assertEqual(wf[decode]["inputs"]["samples"][0], stage2[0])
        detailers = _all(wf, "FaceDetailer")
        self.assertEqual(len(detailers), 2, detailers)
        hand = [k for k in detailers
                if wf[k]["inputs"]["image"][0] == decode]
        self.assertEqual(len(hand), 1, "没有一段修复直接吃 VAEDecode")
        face = [k for k in detailers if wf[k]["inputs"]["image"][0] == hand[0]]
        self.assertEqual(len(face), 1, "二段修复没接在一段修复之后（先手后脸）")
        save = _one(wf, "SaveImage")
        self.assertEqual(wf[save]["inputs"]["images"][0], face[0])
        self.assertEqual(wf[save]["inputs"]["filename_prefix"], "Nffa")

    def test_detectors_are_hand_then_face(self):
        wf = self._wf()
        decode = _one(wf, "VAEDecode")
        by_detector = {}
        for k in _all(wf, "FaceDetailer"):
            det = wf[k]["inputs"]["bbox_detector"][0]
            by_detector[wf[det]["inputs"]["model_name"]] = k
        self.assertEqual(sorted(by_detector),
                         ["bbox/face_yolov8m.pt", "bbox/hand_yolov8s.pt"])
        self.assertEqual(wf[by_detector["bbox/hand_yolov8s.pt"]]["inputs"]["image"][0],
                         decode, "修手那段的检测器不该是脸模型")

    def test_prompt_is_bare_placeholder_and_negative_is_baked(self):
        """正向词**不拼任何画风前缀**（krea2 会拼 `Yoneyama Mai Style`），
        负面词写死在工作流里——所以模型再往 prompt 叠一串负面词就是重复。"""
        wf = self._wf()
        texts = [n["inputs"]["text"] for n in wf.values()
                 if n.get("class_type") == "CLIPTextEncode"]
        self.assertIn("__MULTI_PROMPTS__", texts)
        self.assertTrue(any("worst quality" in t for t in texts))
        self.assertNotIn("@kibro", json.dumps(wf))
        # 修手那段的正向词固定写死（跟主提示词无关）
        self.assertTrue(any(t.startswith("good hand") for t in texts))

    def test_one_seed_for_all_four_samplers(self):
        """两个 KSampler + 两个 FaceDetailer 共用**同一个** `__SEED__`——
        caption 只报得出一个数，凑不齐 4 处就说明有人手改过。"""
        raw = _raw(self.NAME)
        self.assertEqual(raw.count("__SEED__"), 4, raw.count("__SEED__"))
        self.assertNotIn("__CHARACTER__", raw)
        self.assertNotIn("__DENOISE__", raw)      # 不支持垫图，没有重绘占位符

    def test_canvas_is_portrait_single(self):
        wf = self._wf()
        lat = wf[_one(wf, "EmptyLatentImage")]["inputs"]
        self.assertEqual((lat["width"], lat["height"]), (1024, 1536))
        self.assertEqual(lat["batch_size"], 1)

    def test_no_i2i_workflow_and_not_whitelisted(self):
        """它明确**不支持图生图**：既没有 `workflow_i2i.json`，也不在
        `_I2I_SKILLS` 里——传 source_image 会当场被拒，别悄悄退回文生图。"""
        from app.tools.normal.generate_image import _I2I_SKILLS
        self.assertNotIn(self.NAME, _I2I_SKILLS)
        self.assertFalse(os.path.exists(
            os.path.join(SKILLS, self.NAME, "workflow_i2i.json")))

    def test_every_link_points_at_an_existing_node(self):
        wf = self._wf()
        for nid, node in wf.items():
            for key, val in node.get("inputs", {}).items():
                if (isinstance(val, list) and len(val) == 2
                        and isinstance(val[0], str)):
                    self.assertIn(val[0], wf,
                                  "nffa 节点 %s.%s 指向不存在的节点 %r"
                                  % (nid, key, val[0]))


class ToolDescriptionBudgetTest(unittest.TestCase):
    """工具描述改成要点式之后，**判据一条都不许丢**（2026-10-04）。

    背景：接本地 ollama 小模型当主对话时，发现「工具描述给太少」和
    「给太多」两头都要命——

    - 给太少：9B 不会主动去 `load_skill`，所以**决策判据必须在工具描述
      里**（什么时候换渠道、什么时候传 source_image、角色名必写…），
      挪进 skill 文档等于这些判据对小模型不可见。
    - 给太多：`generate_image` 一条 description 就吃掉系统头 1/4，
      而 9B 的窗口只有 16384，挤掉的是留给对话的额度。

    所以这次精简的**唯一合法理由**是「渠道画面特征/参数细节在
    `Available Skills` 的一行摘要和 skill 文档里已经有一份」，不是删判据。
    下面这组断言就是防止「下次再精简时顺手把判据也删了」。

    另一条同样重要：渠道名清单在 `skill` 参数里**不再重复**——
    它已经在 `Available Skills` 里逐行列出，重复两遍只是白占位置。
    """

    #: 每一条都是模型必须看到的**判据**，不是渠道细节。
    #: 措辞别锁死（描述本来就会重写），但语义必须在场。
    RULES = {
        "默认渠道": "不传就是默认渠道 silver",
        "别编渠道名": "别编别的 skill 名出来",
        "换渠道门槛": "只有用户点名画风 / 点名尺寸",
        "高清但没更大": "只是形容词 = 别动",
        "clear_soft难分": "分不清也走默认",
        "角色名最前": "名字写在 prompt 最前面",
        "qwen也写名字": "自然语言句子里照样要写名字",
        "用户报名字不省": "原样写进去",
        "认不出要承认": "我没认出来",
        "nffa槽会顶画风": "画风顶掉",
        "引用图不等于图生图": "永远不构成图生图",
        "source门槛只认点名": "只有明说「qwen 图生图」才传",
        "改动内容不算垫图": "这类**改动内容**不算",
        "分不清就问": "别自己猜",
        "图生图只走qwen": "图生图只有一条路：qwen_image_v1",
        "动漫重绘已撤掉": "重绘已经从用法里撤掉",
        "引用自己的图沿用渠道": "用那张图当初的渠道",
        "hd3不支持垫图": "不支持垫图",
        "改图只写一句": "不要把整张图重新描述一遍",
        "绝不退回文生图": "绝不退回文生图凭空画一张",
        "一次只改一处": "一次只交代一处改动最稳",
        "nai需开通": "仅限管理员为特定群开通",
        "横版才nai_wide": "才传 nai_wide",
        "竖改横做不到": "别应承",
        "唯一多张渠道": "唯一支持一次出多张",
        "分隔只有它认": "只有 skill=image_gen_v1 认",
        "lora格式": "文件名:强度",
        "seed范围": "0 ~ 4294967295",
    }

    @staticmethod
    def _tool():
        from app.tools.normal.generate_image import tool
        return tool

    def test_every_rule_survives(self):
        tool = self._tool()
        blob = tool["description"] + " " + " ".join(
            (p.get("description") or "")
            for p in tool["parameters"]["properties"].values())
        missing = sorted(k for k, needle in self.RULES.items() if needle not in blob)
        self.assertEqual([], missing,
                         "工具描述精简时丢了这些判据：%s" % "、".join(missing))

    def test_description_stays_lean(self):
        """留个天花板，防止改回去又变成长散文。"""
        desc = self._tool()["description"]
        self.assertLess(len(desc), 5000,
                        "主 description 又涨回 %d 字（精简前是 4965）" % len(desc))

    def test_hd_jargon_is_not_a_size_request(self):
        """「高清 / 大图」这类词**不能**触发换尺寸档——它俩曾经自相矛盾。

        旧描述里同时写着两句：
          「只说『大图 / 高清』没说多大 → hd_fast_clear」
          「只说『高清』但没提要更大 → 仍然默认 anima_clear」
        一句让它上去、一句让它下来。9B 撞上这句时是随机的：同一句
        「来张高清的初音未来」连跑三次，实测给出「无工具块 /
        hd_fast_clear / 不传」三种结果。

        2026-10-04：**「高清」有两个意思，得按有没有「档」字分开判**——
        用户是照 ComfyUI 里导出的工作流名字说话的（高清快档 / 高清二档 /
        高清三档），那是**点名了尺寸档、必须传**；而光秃秃的「高清 / 清晰 /
        画质好」只是形容词，**不传**。两条都得写进描述，否则要么吞掉用户
        点名的档位，要么把形容词当换档理由。
        """
        desc = self._tool()["description"]
        # 不带「档」的画质形容词 → 不换档
        self.assertIn("只是形容词 = 别动", desc)
        # 带「档」的点名档位 → 必须换
        for token in ("高清快档", "高清二档", "高清三档"):
            self.assertIn(token, desc,
                          "描述里必须留着 %s 这种带「档」的点名写法" % token)
        self.assertIn("当壁纸", desc)
        # 旧的两句自相矛盾的说法，一句都不许再出现
        self.assertNotIn("没说多大 → `hd_fast_clear`", desc)
        self.assertNotIn("仍然默认 anima_clear**，别自作主张上 hd_", desc)

    def test_skill_param_does_not_relist_channels(self):
        """`skill` 参数不再重复列 16 个渠道。

        `Available Skills` 里已经逐行给出一行摘要（渠道名 + 画风 + 何时用），
        参数描述里再抄一遍是纯浪费。留一句「见 Available Skills」+ 上限。
        """
        param = self._tool()["parameters"]["properties"]["skill"]["description"]
        self.assertIn("Available Skills", param,
                      "应该把渠道清单指到 Available Skills 那份一行摘要")
        self.assertLess(len(param), 400,
                        "skill 参数又涨回 %d 字（精简前是 947）" % len(param))
        # 至少别把四个画风全名逐一抄一遍
        for ch in ("anima_soft", "anima_gloss", "anima_curvy"):
            self.assertNotIn(ch, param,
                             "skill 参数里重复列了 %s（Available Skills 已有一份）" % ch)


class HdTierGuardTest(unittest.TestCase):
    """尺寸档守卫 `_hd_tier_guard`（2026-10-04）。

    为什么描述里已经写了判据、还要这层代码：**9B 执行不了负向规则。**
    实测（qwen3.8-9b-heretic，7 个 case 各 2 遍）——
      · 带「档」的（高清二档 / 高清三档 / 高清快档）→ 5/6 传对 hd_* 档；
      · 不带「档」的（高清 / 高清一点 / 清晰点）→ **只有 1/6 传对**，
        其余传成 hd_fast / hd_2 / hd_3，还有一个干脆没调工具。
    也就是说「别为形容词换档」这条它学不会，而「把形容词当换档理由」学得
    很好。判据本身是确定性的（有没有「档」字 / 有没有像素数字），拿代码判
    一次比指望小模型记住可靠 —— 跟 `_i2i_gate` 同一个证据源、同一套思路。
    """

    @staticmethod
    def _guard(skill, text, default="anima_clear"):
        from app.tools.normal import generate_image as gi
        from app import qq_api
        with mock.patch.object(qq_api, "current_turn_text",
                               return_value=text):
            return gi._hd_tier_guard(skill, default)

    def test_tier_named_keeps_hd_channel(self):
        """用户报了档位 → 放行，hd_* 原样保留。"""
        for text, why in (
            ("画一张绫华，高清二档", "带档"),
            ("来个鲁迪乌斯，高清三档", "带档"),
            ("画个黑发少女，高清快档", "快档"),
            ("来个二档的", "光说档位"),
            ("要 1328×2000 那张", "直接报像素"),
            ("来张当壁纸的", "明说用途"),
        ):
            for skill in ("hd_2_clear", "hd_3_clear", "hd_fast_soft"):
                got, note = self._guard(skill, text)
                self.assertEqual(skill, got,
                                 "「%s」（%s）报了档位却被降级了" % (text, why))
                self.assertEqual("", note, "放行时不该带回执备注")

    def test_quality_word_only_downgrades(self):
        """只有画质形容词 → 降回默认档 + 带回执说明。"""
        for text, hit in (("画一张绫华，高清", "高清"), ("高清一点", "高清"),
                          ("清晰点", "清晰"), ("画质好点", "画质"),
                          ("要精细的", "精细"), ("别太糊", "别太糊")):
            got, note = self._guard("hd_fast_clear", text)
            self.assertEqual("anima_clear", got,
                             "「%s」只是画质形容词，不该换 hd_ 档" % text)
            self.assertIn("自动调整", note,
                          "降级必须告诉对方，否则他以为自己要到了大图")
            # %s 忘了替换就会原样出现在给用户看的话里（接缝处就是这么漏的）
            self.assertNotIn("%s", note,
                             "回执里还留着未替换的 %%s 占位符：%r" % note)
            self.assertNotIn("{}%", note)
            # 且要带上原话里**实际命中的那个词**，让他知道是哪个词触发的
            self.assertIn("「%s」" % hit, note,
                          "回执没点名是哪个词触发的降级：%r" % note)

    def test_no_evidence_passes_through(self):
        """网页端（拿不到原话）不拦 —— 没有证据不能当成「他没说要」。"""
        from app import qq_api
        from app.tools.normal import generate_image as gi
        with mock.patch.object(qq_api, "current_turn_text",
                               return_value=None):
            self.assertEqual("hd_3_clear", gi._hd_tier_guard("hd_3_clear",
                                                             "anima_clear")[0])

    def test_unrelated_text_passes_through(self):
        """既没档位也没画质词 → 判不准，别乱动（可能是引用/上文语境）。"""
        for text in ("照这张画一张", "跟刚才一样的", "再来一张"):
            got, note = self._guard("hd_2_clear", text)
            self.assertEqual("hd_2_clear", got,
                             "「%s」信息不足，不该动模型选的档位" % text)
            self.assertEqual("", note)

    def test_non_hd_skill_untouched(self):
        """非 hd_* 渠道（qwen/krea2/nffa/nai）一律不管。"""
        for skill in ("anima_soft", "qwen_image_v1", "krea2", "nai"):
            self.assertEqual(skill, self._guard(skill, "高清一点")[0])

    def test_guard_runs_after_nai_branch(self):
        """守卫必须**排在 NAI 分流之后**。

        NAI 是云端渠道，压根不读本机 skill 目录，画布/档位那套概念对它没有
        意义；把它拦下来降级成 anima_clear 只会让 NAI 根本没画成。
        位置错了不会让任何现有用例变红（`nai` 压根不是 hd_ 前缀，守卫自己
        会放行），所以必须拿源码位置单独钉住。
        """
        import inspect
        from app.tools.normal import generate_image as gi
        src = inspect.getsource(gi._generate_image)
        i_nai = src.index("if skill in image_jobs.NAI_SKILLS:")
        # 注意取**第一处**调用：守卫若被挪到 NAI 前面，源码里就会出现两次
        # 调用，只断言「后一处在 NAI 之后」会漏掉它。
        i_guard = src.index("skill, hd_note = _hd_tier_guard("
                            if "skill, hd_note = _hd_tier_guard(" in src
                            else "_hd_tier_guard(")
        self.assertEqual(1, src.count("_hd_tier_guard(skill"),
                         "_generate_image 里守卫被调了不止一次，"
                         "顺序判据会失效——只留一处")
        self.assertLess(i_nai, i_guard,
                        "守卫跑到 NAI 分流前面去了，NAI 会被误降级")
        # 且守卫必须在读 skill 目录之前（降级后要重新解析渠道）
        self.assertLess(i_guard, src.index("skill_data = load_skill(skill)"))

    def test_guard_only_downgrades_never_upgrades(self):
        """只降不升：对方报了「二档」而模型没传 skill，**不在这里补**。

        补了等于代码替模型猜意图，而且猜画风没依据（该 hd_2_clear 还是
        hd_2_soft？）。实测里「报了档却没传」没出现过 —— 9B 是反过来的，
        见到「高清」就乱传，那才是要治的。
        """
        got, note = self._guard(None, "画一张绫华，高清二档")
        self.assertIsNone(got, "守卫只该拦 hd_ → 默认，不该凭空造渠道")
        self.assertEqual("", note)

    def test_receipt_note_survives_source_image(self):
        """垫图分支里 `source_note` 会被 comfy_src.resolve 覆写，
        降级说明必须一起传下去，不能被顶掉。"""
        import inspect
        from app.tools.normal import generate_image as gi
        src = inspect.getsource(gi._generate_image)
        self.assertIn("hd_note", src,
                      "垫图分支拿到的 source_note 必须并上 hd_note，"
                      "否则降级提示会被垫图说明顶掉")
        # 且不能是裸赋值（那会丢）
        self.assertNotIn("raw, source_note = comfy_src.resolve", src,
                         "不能直接覆写 source_note，会把降级提示冲掉")



class T2iGuardTest(unittest.TestCase):
    """文生图守卫：对方明说「文生图」→ 不许垫图。

    实测（2026-10-04，qwen3.8-9b-heretic，各跑 2 遍）：对方说
    「不要图生图了，文生图」，9B 两次里一次传了 `source_image`、一次才没传。
    跟 `_hd_tier_guard` 同一个病根——**负向指令它执行不了**。所以代码判。
    """

    def _guard(self, is_i2i, text):
        from unittest import mock
        from app.tools.normal import generate_image as gi
        with mock.patch("app.qq_api.current_turn_text", return_value=text):
            return gi._t2i_guard(is_i2i)

    def test_explicit_t2i_drops_source(self):
        for text in ("不要图生图了，文生图", "文生图", "纯文生图",
                     "不要垫图", "别用参考图", "重新生成", "重画一张"):
            ok, note = self._guard(True, text)
            self.assertTrue(ok, "不该拦下整次调用（%s）" % text)
            self.assertIn("文生图", note,
                          "「%s」明说文生图，却没告诉对方已去掉垫图" % text)
            self.assertNotIn("%s", note, "回执里留着未替换的占位符")
            # 回执那边会再套一层「（垫图：%s）」，note 自带括号就变成「（（…））」
            self.assertFalse(note.startswith("（") and note.endswith("）"),
                             "note 自带外层括号，会被回执套成「（（…））」：%r" % note)

    def test_no_intent_keeps_i2i(self):
        """没说文生图 → 不动 i2i，别误伤正常的垫图。"""
        for text in ("改这件外套", "图生图", "垫图重绘", "跑图"):
            ok, note = self._guard(True, text)
            self.assertEqual((ok, note), (True, ""),
                             "「%s」没说文生图，却把垫图去掉了" % text)

    def test_not_i2i_is_noop(self):
        self.assertEqual(self._guard(False, "文生图"), (True, ""))

    def test_web_no_user_text_passes(self):
        """网页端/单测没有原话这个证据源，一律不拦（拿不到证据就别动模型的决定）。"""
        from unittest import mock
        from app.tools.normal import generate_image as gi
        with mock.patch("app.qq_api.current_turn_text", return_value=None):
            self.assertEqual(gi._t2i_guard(True), (True, ""))

    def test_gate_runs_before_nai_split(self):
        """必须挡在 NAI 分流之前——NAI 走云端但共用同一个 source_image，
        放后面就会漏判。"""
        import inspect
        from app.tools.normal import generate_image as gi
        src = inspect.getsource(gi._generate_image)
        self.assertLess(src.index("_t2i_guard(is_i2i)"),
                        src.index("if skill in image_jobs.NAI_SKILLS:"),
                        "文生图守卫在 NAI 分流之后 → NAI 垫图漏判")

    def test_resets_is_i2i_before_branches(self):
        """改完 source_image 必须重算 is_i2i，否则下面所有分支仍按垫图走。"""
        import inspect
        from app.tools.normal import generate_image as gi
        src = inspect.getsource(gi._generate_image)
        i_call = src.index("_t2i_guard(is_i2i)")
        # 紧接着应是清空 + 重算
        tail = src[i_call:i_call + 200]
        self.assertIn('source_image = ""', tail)
        self.assertIn("is_i2i = False", tail)

    def test_note_reaches_receipt(self):
        """改判说明必须进回执通道 source_note，否则对方不知道图变了。"""
        import inspect
        from app.tools.normal import generate_image as gi
        src = inspect.getsource(gi._generate_image)
        self.assertIn("hd_note + t2i_note", src,
                      "t2i_note 没并进 source_note，回执里看不到")


class I2iForceRemovedTest(unittest.TestCase):
    """「代码抢在模型前面补垫图」那道反向守卫已删除（2026-10-06 用户拍板）。

    它原来跟 `_i2i_gate` 成对：原话命中机制词而模型没传 `source_image` → 代码
    自己把垫图补上。删它的依据是用户那句话——「改成让 AI 它自己去判断，没有兜底
    没有问题」。**补垫图比拒收更危险**：那是代码替用户决定「这张要拿去垫」，
    一旦词表误命中（「重绘是什么意思」当年就在拦截名单里、却在这条里被算成
    命令），对方拿到的就是一张被垫糊了的图，而他只是问了一句。
    """

    def test_symbol_is_gone(self):
        from app.tools.normal import generate_image as gi
        self.assertFalse(hasattr(gi, "_i2i_force"), "_i2i_force 又回来了")

    def test_no_source_image_means_text_to_image_whatever_the_words_are(self):
        """原话再怎么「像要改图」，模型没传 `source_image` 就是文生图。

        探针：`load_skill` 收到的必须是默认渠道，且**谁都不许去读源图**。
        """
        import inspect
        from unittest import mock
        from app.tools.normal import generate_image as gi
        self.assertNotIn("is_i2i = _i2i", inspect.getsource(gi._generate_image))
        boom = mock.Mock(side_effect=AssertionError("不该去读源图"))
        with mock.patch.object(gi, "is_cancelled", lambda: False), \
                mock.patch.object(gi, "_qq_gate", lambda: None), \
                mock.patch.object(gi.char_guard, "check", lambda p: None), \
                mock.patch.object(gi, "load_skill", lambda skill: None), \
                mock.patch.object(gi.comfy_src, "resolve", boom), \
                mock.patch("app.qq_api.current_turn_text",
                           return_value="图生图 把衣服换成jk"):
            out = gi._generate_image(prompt="change her coat to red")
        self.assertIn(gi.T2I_DEFAULT_SKILL, out)   # 落默认渠道，没被代码补成垫图

    def test_t2i_guard_is_still_there_but_alone(self):
        """同族的 `_t2i_guard`（明说文生图却不传 source_image）**留着**——
        用户 10-06 点名拆的是图生图那三处，没提这条。这里钉住「没被顺手删掉」，
        也钉住它不再有个反向孪生。
        """
        from app.tools.normal import generate_image as gi
        self.assertTrue(callable(gi._t2i_guard))
        self.assertFalse(hasattr(gi, "_i2i_gate"))


class PromptAskGuardRemovedTest(unittest.TestCase):
    """「要提示词守卫」已删除（2026-10-06 用户拍板），这里钉住**别再加回来**。

    那道闸（原 `generate_image._prompt_ask_guard`）的判据是
    「提示词/种子 + 给我/是什么/停」，只认关键词、不认意图。实测误伤：
    233 群「引用这张图，我要她抓手的手势，角色换成花火」是**改图请求**，
    只因正文带「提示词」三个字就被整单拒掉。

    用户原话：「我们加了太多这种硬编码的规则了，导致 AI 它变得特别傻…
    能就我们让 AI 来做的，我们就直接让 AI 来做，不要为了省那几毛钱」。
    """

    def test_guard_is_gone(self):
        from app.tools.normal import generate_image as gi
        self.assertFalse(hasattr(gi, "_prompt_ask_guard"))
        self.assertFalse(hasattr(gi, "_PROMPT_NOUN_RE"))
        self.assertFalse(hasattr(gi, "_PROMPT_ASK_RE"))

    def test_generate_image_source_has_no_prompt_keyword_gate(self):
        """生图主流程里不许再出现按「提示词」字样拒单的代码。"""
        import inspect
        from app.tools.normal import generate_image as gi
        src = inspect.getsource(gi._generate_image)
        # 查调用形态（带括号）：注释里会提到它解释「为什么删」，那是该留的。
        self.assertNotIn("_prompt_ask_guard()", src)

    def test_asking_for_prompt_is_left_to_the_model(self):
        """要词条的原话不再被代码拦——该由模型判（调 recall_image 还是生成）。"""
        import inspect
        from app.tools.normal import generate_image as gi
        src = inspect.getsource(gi._generate_image)
        # 留着这条注释，是给后来人看的：别因为「它又白烧了一张」就加回来。
        self.assertIn("2026-10-06 用户拍板删除", src)


class BriefToolParamTest(unittest.TestCase):
    """简短模式必须**保留** generate_image 的参数级说明（2026-10-04）。

    踩坑记录：brief 最初把参数说明整段砍掉，只留工具级首句
    「调用 ComfyUI 生成图片」。同一条原话「qwen 重绘一下这只手」各跑 3 遍：

      brief 关 → 3/3  skill=qwen_image_v1 + source_image=1   ✅
      brief 开 → 0/3  1 遍压根不调工具、2 遍传
                     source_image='last' / '[233 发来的图片]'（非法值）

    非法值会被 comfy_src._pick 拒掉 → 垫不了图 → 它转而**编造**
    （「✅ 已完成重绘」+ 假 markdown 图片链接，或一本正经地说它没有生图
    能力、建议你去用 Photoshop）。所以这里断言的是**信息不能丢**。
    """

    def _line(self, brief):
        from app import agent_prompt as ap
        from unittest import mock
        with mock.patch.dict("os.environ", {"PROMPT_BRIEF":
                                            "1" if brief else "0"}):
            for line in ap._build_tool_list("qq", brief=brief).splitlines():
                if line.startswith("- **generate_image**"):
                    return line
        self.fail("工具列表里没有 generate_image")

    def test_brief_keeps_skill_channel_hint(self):
        line = self._line(True)
        self.assertIn("默认 silver", line, "brief 下丢了默认渠道名")
        self.assertIn("hd_fast_", line, "brief 下丢了档位映射")
        self.assertIn("qwen_image_v1", line, "brief 下丢了 qwen 渠道映射")

    def test_brief_keeps_source_image_legal_value(self):
        line = self._line(True)
        self.assertIn("只填数字 1", line,
                      "brief 下没说清 source_image 只认 1 → 模型会传 last/占位符")

    def test_brief_keeps_seed_constraint(self):
        # seed 的坑是「nai 传了直接报错」，属于填错即失败的硬约束。
        self.assertIn("seed", self._line(True))
        self.assertIn("才传", self._line(True))

    def test_brief_other_tools_stay_terse(self):
        # 反向约束：别把 brief 修成「等于没 brief」。2026-10-04 晚白名单
        # 砍成 4 个纯生图工具后，名单外的工具（web_search 等）整行不出现
        # ——比「压短」更彻底。
        from app import agent_prompt as ap
        from unittest import mock
        with mock.patch.dict("os.environ", {"PROMPT_BRIEF": "1"}):
            tools = ap._build_tool_list("qq", brief=True)
        for gone in ("web_search", "send_qq_message", "memory_save",
                     "preset_save", "send_sticker", "collect_sticker"):
            self.assertNotIn("**%s**" % gone, tools,
                             "工具 %s 已出白名单，brief 下不该再列出" % gone)
        self.assertIn("**generate_image**", tools, "生图工具必须在场")

    def test_not_brief_unchanged(self):
        # 非 brief 模式**本来就不带**参数级说明（工具级首句 + 完整参数说明
        # 在别处，见 P_IMAGE 那段生图方法）。这里钉住的是「brief 的改动没
        # 顺手把非 brief 也改了」——两条路径的差异必须一直在。
        line = self._line(False)
        self.assertNotIn("=", line.split(":")[0], "非 brief 下参数行不该有 =说明")
        self.assertIn("source_image:string", line)


class CustomChannelTest(unittest.TestCase):
    """用户自定义渠道 silver / jank 必须靠 **AI 提示词** 暴露给模型（2026-10-06）。

    ## 这条锁的来龙去脉

    渠道路由是两条路：① 代码直判 `_parse_channel`（关键词→渠道，硬路由）；
    ② AI 判 `_translate` / `_MASTER_TEMPLATE`（模型自己决定 skill）。

    silver / jank **只走第 ② 条路**——代码不认这俩词（`_parse_channel` 对
    「silver 和泉纱雾」返回 (None, …)），于是交给 `_translate` 的 AI 去决定 skill。
    所以模型必须**在提示词里看到 silver / jank 是合法 skill**，才会传
    `skill="silver"` / `"jank"`。

    2026-10-06 之前的坑：只改了 `generate_image` 的工具描述，但 QQ 直达路径读的是
    `_MASTER_TEMPLATE`，那份渠道清单里没有 silver / jank，模型不知道这俩是合法
    skill，于是落回 anime 档（群聊实录「silver 和泉纱雾」被画成了 hd_3_clear）。

    ## 用户铁律（别破）

    **禁止在 `_parse_channel` 里硬编码 silver / jank 的关键词→渠道映射**
    （那是代码替模型拍板，用户明确不要）。正确做法就是把它写进 AI 提示词，
    让模型自己选。下面的 `test_custom_channels_are_not_hardcoded_in_parser`
    就是钉住这条红线。
    """

    def test_master_template_lists_custom_channels(self):
        """QQ 直达路径的 AI 提示词必须列出这些渠道名。"""
        from app.direct_gen import _MASTER_TEMPLATE
        for name in ("silver", "silver-hd", "jank"):
            with self.subTest(name=name):
                self.assertIn(name, _MASTER_TEMPLATE)

    def test_tool_description_lists_custom_channels(self):
        """agent / 网页路径的工具描述也得提（双保险）。"""
        from app.tools.normal.generate_image import tool
        for name in ("silver", "silver-hd", "jank"):
            with self.subTest(name=name):
                self.assertIn(name, tool["description"])

    def test_custom_channels_are_allowed_skills(self):
        """模型传 skill=silver / silver-hd / jank 时，不能被 `_allowed_skills` 拒掉。"""
        from app.direct_gen import _allowed_skills
        allowed = _allowed_skills()
        for name in ("silver", "silver-hd", "jank"):
            with self.subTest(name=name):
                self.assertIn(name, allowed)

    def test_custom_channels_have_workflows(self):
        from app.skills import load_skill
        for name in ("silver", "silver-hd", "jank"):
            with self.subTest(name=name):
                data = load_skill(name)
                self.assertIsNotNone(data, name)
                self.assertEqual(data["kind"], "生图", name)
                self.assertIsNotNone(data["workflow"], name)

    def test_custom_channels_are_not_hardcoded_in_parser(self):
        """红线：silver / silver-hd / jank 不许进代码级关键词路由
        （`_FIXED_CHAN_MAP` / 正则），也**不许**写死进 `_FIXED_SKILLS` 允许名单。

        它们靠「落进 skills/ 即是可用渠道」由 `_allowed_skills()` 动态放行
        （见同文件那处改动），而不是写死在任何代码名单里。这条锁保证以后有人
        图省事把它们加进硬路由 / 硬名单时立刻报错——用户明确不要这种硬编码。
        """
        from app.direct_gen import _FIXED_CHAN_MAP, _FIXED_CHAN_RE, _FIXED_SKILLS
        for name in ("silver", "silver-hd", "jank"):
            with self.subTest(name=name):
                self.assertNotIn(name, _FIXED_CHAN_MAP)
                self.assertNotIn(name, _FIXED_SKILLS)
        # 用户在消息里点名这几个词，也不能被那道正则命中
        self.assertIsNone(_FIXED_CHAN_RE.search("silver 和泉纱雾"))
        self.assertIsNone(_FIXED_CHAN_RE.search("用 jank 画一张"))
        self.assertIsNone(_FIXED_CHAN_RE.search("silver-hd 女骑士"))
        self.assertIsNone(_FIXED_CHAN_RE.search("silver超清 女骑士"))


if __name__ == "__main__":
    unittest.main()
