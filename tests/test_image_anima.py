"""anima（唯一的动漫渠道）：两段采样工作流的性质锁。

## 这套用例锁的是什么

2026-09-30 起动漫渠道**只剩 anima 一个**，工作流直接取自用户在 ComfyUI 里调好的
`anime2`（`D:\\AI\\ComfyUI\\user\\default\\workflows\\anime2.json`）。
**每次用户在这个文件里调完参数，`skills/anima/workflow.json` 都要跟着重转**
（UI 格式 → API 格式，步骤见 `skills/anima/skill.md`）。当前拓扑：

    一段：UNETLoader(5, Ani1.1) → LoRA(16 kibro 1.0) → LoRA(15 baka skin 0.5)
          → KSampler(2) euler/simple 10 步 cfg3 denoise 1.0
    二段：UNETLoader(20, Ani1.1 同一块) → KSampler(19) euler/simple 10 步
          cfg7 denoise 0.25（latent 来自 KSampler(2)，**不接 LoRA**）

这些性质全写在 JSON 里、肉眼看不出来，但每一个都有明确的「为什么」：

- 两段是「细节更多」的来源，所以**两个 KSampler 必须都真的在跑**；
- 第二段 denoise=0.25 是**精修不是重画**，改成 1.0 就变成画两张不同的图；
- 分辨率停在 768×1024（**安全锁**，见下）；
- 节点 4 必须是**可代入的模板**，不能是写死的角色；
- `__SEED__` 必须是占位符，写死成数字等于每张图一模一样。

数据一被人改动就得有人知道——这就是这套用例存在的全部理由。

> ⚠️ **上游 `5fe5d81` 的 `anima` 和本机这套不是同一个工作流**：上游是
> 「单底模 + 两段 + `LatentUpscaleBy(scale_by=1)` + 728×1024」，本机是
> 「两段 + 无放大 + 768×1024」。所以**别照抄上游的用例**，
> 节点编号/底模数量都要按本机实际写。安全性的锁要留，只换数值。

> **参数旋钮 vs 架构**：步数、CFG、LoRA 强度是用户随手调的旋钮，**别锁**——
> 锁死了每次调参都要改测试，反而会让人嫌麻烦去删断言。
> 这里只锁**架构性的**东西：几个采样器/几个装载器、谁连谁、denoise 的量级关系、
> 分辨率上限、占位符。底模文件单独锁一条，因为换底模=换画风，值得被注意到。

零网络、零显卡：只读 skills 目录里的 workflow.json。
"""

import os
import unittest

from app.skills import list_skills, load_skill, load_workflow, skill_priority

SKILLS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "skills")


def _load(name):
    data = load_skill(name)
    assert data and data["workflow"], name
    return data["workflow"]


def _types(wf):
    return [n.get("class_type") for n in wf.values()]


class ChannelExistsTest(unittest.TestCase):
    """anima 得在、得被扫到——它是默认渠道，少了模型就选不着。"""

    def test_scanned(self):
        self.assertIn("anima", list_skills())

    def test_loads_as_image_skill(self):
        data = load_skill("anima")
        self.assertIsNotNone(data)
        self.assertEqual(data["kind"], "生图")
        self.assertTrue(data["workflow"])

    def test_not_marked_as_heavy(self):
        """没被标成重渠道——它靠的是「默认就是它」，不是队列权重。"""
        self.assertEqual(skill_priority("anima"), 1)


class TwoStageTest(unittest.TestCase):
    """两段采样 + 两块底模：这些性质就是「细节更丰富」的定义。"""

    def test_two_samplers_two_loaders(self):
        """**核心不变量**：两个采样器，两块底模装载器。

        ⚠️ 2026-09-30 16:26 用户把**节点 20 的底模换成了和节点 5 同一个文件**
        （原先第二段用 `miaomiaoRealskin_anima13`）。所以现在是「两个 UNETLoader
        节点、但装的是同一块 Ani1.1」——功能上等价于单底模：ComfyUI 的
        `model_management` 按 **model 对象**缓存，同一个文件加载两次只装一份，
        不会重复吃那 3988MB。

        节点数仍然是 2（这是图的结构，不是「装了几块模型」），
        这条挡的是「又塞回第三块」或「把某个 UNETLoader 删了」。
        """
        wf = _load("anima")
        t = _types(wf)
        self.assertEqual(t.count("KSampler"), 2)
        self.assertEqual(t.count("UNETLoader"), 2)

    def test_both_stages_use_the_ani11_base(self):
        """两段的底模都是 `miaomiaoAnimeReality_ani11`——换底模等于换画风。

        历史：第二段原先指向 `miaomiaoRealskin_anima13`（真·双底模），
        2026-09-30 16:26 用户换回和第一段同一块。**如果这个断言红了，
        说明底模又被换了**——先确认是有意的，再改这里，别直接删断言。
        """
        wf = _load("anima")
        unets = [wf[k]["inputs"]["unet_name"] for k, n in wf.items()
                 if n.get("class_type") == "UNETLoader"]
        self.assertEqual(unets, ["miaomiaoAnimeReality_ani11_3087842.safetensors",
                                 "miaomiaoAnimeReality_ani11_3087842.safetensors"])

    def test_positive_prompt_is_a_template_not_a_fixed_character(self):
        """**核心不变量**：节点 4 必须是可代入的模板，不能是写死的角色。

        这个坑踩过两次：

        ① 把用户在 ComfyUI 里调好的工作流搬进 skills 时，节点 4 里存的是
           **那张图当时的完整提示词**（1166 字的银白双马尾角色 + 门口场景）。
           `generate_image` 的机制是「把 `__MULTI_PROMPTS__` 换成模型给的
           提示词」——那串字里**没有占位符**，于是模型传什么都被原样丢弃，
           私聊里连着画了三四张都是同一个角色。

        ② 2026-09-30 换成 `anime2` 时**又遇到一次**：`anime2.json` 的节点 4 是
           619 字的金发狐耳红兜帽角色 + 舞台背景。用户要求「原封不动搬过来」，
           但这一处**必须**改成模板，否则等于把坑②原样搬进来。

        ③ 同一天 16:26 用户又调了一版 `anime2`，节点 4 **还是**那个写死的角色
           （只是换了背景描述，改成 444 字的「金发狐耳 + 白底」版）。
           说明这不是一次性的疏忽，而是**每次从 ComfyUI 导工作流都会带上**——
           所以这条锁必须一直在，转工作流时每次都过一遍。

        所以这条锁两件事：① 有占位符；② 正文里不含具体角色/场景词。
        **只留画风前缀**（`@kibro` 是 lora 触发词，不能丢）。
        """
        wf = _load("anima")
        text = wf["4"]["inputs"]["text"]
        self.assertIn("__MULTI_PROMPTS__", text)
        self.assertTrue(text.startswith("@kibro,"),
                        "画风前缀丢了：%r" % text)
        # 具体角色词一个都不许留——留了就是又写死了。
        # 前 6 个是坑①的残留词，其余是坑②/③（anime2 三个版本自带）的残留词。
        for word in ("silver-white", "twin tails", "hairclip", "genkan",
                     "camisole", "pearl",
                     "golden hair", "fox ears", "fox tail", "red hood",
                     "vtuber-style", "neon", "stage background",
                     "sleeveless dress", "thigh-high", "frilled",
                     "isolated on white", "plain white background"):
            self.assertNotIn(word, text, "节点 4 又写死了角色：%r" % word)
        # 模板本身该很短，写死的角色串都几百上千字
        self.assertLess(len(text), 120)

    def test_negative_prompt_is_not_a_fixed_character(self):
        """负向提示词是通用质量词，本来就不该含角色——顺带锁一下别被污染。"""
        wf = _load("anima")
        text = wf["8"]["inputs"]["text"]
        self.assertIn("worst quality", text)
        self.assertNotIn("__MULTI_PROMPTS__", text)

    def test_seed_stays_a_placeholder(self):
        """`__SEED__` 必须是占位符——写死成数字就等于每张图一模一样。

        （我改节点 4 时就犯过：用 `json.loads(replace('__SEED__','0'))` 读进来
        再 `json.dumps` 写回，占位符被永久变成了 0。所以改成字符串级替换，
        并加这条锁。）
        """
        p = os.path.join(SKILLS, "anima", "workflow.json")
        with open(p, encoding="utf-8") as f:
            raw = f.read()
        self.assertEqual(raw.count("__SEED__"), 2,
                         "两个 KSampler 的 seed 都该是 __SEED__ 占位符")

    def test_both_stages_use_deterministic_euler(self):
        """两段都用确定性采样器 `euler`（不是 `er_sde`）。

        `er_sde` 是随机（SDE）的：第二段 denoise 只有 0.25、是在既有 latent 上
        补细节，用 SDE 会把已经干净的图重新注入噪声，表现为头发糊、
        珍珠/蕾丝融在一起、手指断。所以两段都锁 `euler`。
        """
        wf = _load("anima")
        self.assertEqual(wf["2"]["inputs"]["sampler_name"], "euler")
        self.assertEqual(wf["19"]["inputs"]["sampler_name"], "euler")
        self.assertEqual(wf["2"]["inputs"]["scheduler"], "simple")
        self.assertEqual(wf["19"]["inputs"]["scheduler"], "simple")

    def test_first_pass_builds_second_pass_refines(self):
        """第一段 denoise=1 从零建构图；第二段 denoise=0.25 只精修。

        步数/CFG 是画质调校的旋钮（用户 2026-09-30 16:26 把二段从
        「5 步 cfg5」提到「10 步 cfg7」、一段 cfg 从 5 降到 3），
        所以这里**只锁结构性的 denoise**，不锁步数/cfg——那两个改起来
        是调参不是改架构，锁死了反而每次调参都要动测试。
        """
        wf = _load("anima")
        self.assertEqual(wf["2"]["inputs"]["denoise"], 1)
        self.assertEqual(wf["19"]["inputs"]["denoise"], 0.25)
        # 二段是精修 → 必须比一段「轻」，denoise 小是它的定义
        self.assertLess(wf["19"]["inputs"]["denoise"],
                        wf["2"]["inputs"]["denoise"])

    def test_second_pass_reads_the_first_pass_latent(self):
        """第二段吃第一段的输出——链路别接错（这是「精修」的前提）。"""
        wf = _load("anima")
        self.assertEqual(wf["19"]["inputs"]["latent_image"], ["2", 0])
        self.assertEqual(wf["3"]["inputs"]["samples"], ["19", 0])

    def test_there_is_no_upscale_node(self):
        """`anime2` 不放大。加了 `LatentUpscaleBy` 会抬分辨率，撞上显存红线
        （见 ResolutionTest 那段警告）。要放大请用户明确要求，并先量显存。"""
        wf = _load("anima")
        self.assertNotIn("LatentUpscaleBy", _types(wf))

    def test_second_pass_has_its_own_bare_base_model(self):
        """第二段指向**它自己的装载节点**（20），**不接 LoRA**——有意如此。

        两件事别搞混：
        ① 「指向节点 20 而不是节点 15（LoRA 链尾）」：09-30 查过，改成接
           LoRA 链尾能让 ComfyUI 少装一次 3988MB、快 3~4 秒，但第二段画风
           会跟着变（从「裸底模精修」变成「带 LoRA 精修」），用户明确否掉了。
        ② 「节点 20 现在装的文件和节点 5 是同一个」：见
           `test_both_stages_use_the_ani11_base`。**同文件不会重复吃显存**
           （`model_management` 按 model 对象缓存），所以这里是零成本的。
        """
        wf = _load("anima")
        self.assertEqual(wf["19"]["inputs"]["model"], ["20", 0])
        self.assertEqual(wf["20"]["class_type"], "UNETLoader")
        self.assertEqual(wf["2"]["inputs"]["model"], ["15", 0])

    def test_loras_only_on_the_first_pass(self):
        """LoRA 链：节点 15（baka skin 0.5）← 节点 16（kibro 1.0）← 节点 5。"""
        wf = _load("anima")
        loras = {k for k, n in wf.items()
                 if n.get("class_type") == "LoraLoaderModelOnly"}
        self.assertEqual(loras, {"15", "16"})
        self.assertEqual(wf["16"]["inputs"]["model"], ["5", 0])   # 接底模
        self.assertEqual(wf["15"]["inputs"]["model"], ["16", 0])  # 接上一环
        self.assertEqual(wf["15"]["inputs"]["strength_model"], 0.5)
        self.assertEqual(wf["16"]["inputs"]["strength_model"], 1)

    def test_clip_does_not_go_through_lora(self):
        """CLIP 由 `CLIPLoader(6)` 单独喂，**不经过 LoRA**。

        `LoraLoaderModelOnly` 根本没有 clip 输出槽，接线接到它上面会被
        ComfyUI 拒。这条挡住「照抄 SDXL 那套把 clip 也串进 LoRA」。
        """
        wf = _load("anima")
        self.assertEqual(wf["6"]["class_type"], "CLIPLoader")
        self.assertEqual(wf["4"]["inputs"]["clip"], ["6", 0])
        self.assertEqual(wf["8"]["inputs"]["clip"], ["6", 0])


class ResolutionTest(unittest.TestCase):
    """**安全锁**：分辨率是独立杠杆，别往上调。"""

    def test_resolution_stays_at_the_safe_768x1024(self):
        """2026-09-27 把它调到 1024×1536 后，单张就把整机拖到黑屏关机（日志里
        只有一次 3988MB 装载、采样到一半整机重启）。768×1024 是量出来的安全档
        ——想调高先读 `anima/skill.md` 里那段警告，别直接改这个数。

        注意本渠道比单底模版**更吃显存**：两段要先后装两块 3988MB 底模，
        所以更没余量往上调。
        """
        wf = _load("anima")
        self.assertEqual(wf["9"]["inputs"]["width"], 768)
        self.assertEqual(wf["9"]["inputs"]["height"], 1024)


class VisibilityTest(unittest.TestCase):
    """QQ 机器人得能看见它（白名单），否则默认渠道也传不进来。"""

    def test_qq_whitelist_includes_anima(self):
        from app import agents
        self.assertTrue(agents.allows_skill("qq", "anima"))

    def test_skill_list_marks_it_as_default(self):
        """anima 在目录里要标成默认渠道（而不是「点名才用」）。"""
        from app.agent_prompt import _build_skill_list
        lines = [l for l in _build_skill_list("qq").splitlines()
                 if "**anima**" in l]
        self.assertEqual(len(lines), 1, "anima 应当在目录里出现且只出现一次")
        self.assertIn("默认", lines[0])

    def test_descriptions_no_longer_offer_a_second_anime_channel(self):
        """两份工具描述都不能再提 `anima_2`——渠道已合并，提它只会诱导模型乱传。

        本机 `skills/anima_2` 目录还在（用户自己还要用），但**模型不该知道它**：
        对模型来说动漫渠道只有一个。
        """
        from app.tools.normal.generate_image import tool
        from app.config import QQ_AGENT_ID
        for desc in (tool["description"],
                     tool["description_overrides"][QQ_AGENT_ID]):
            self.assertNotIn("anima_2", desc)


class RestoreTest(unittest.TestCase):
    """备份还在——用户明确要求保留，删了就没法回滚了。

    这几个 `.bak` 是**本机实际存在**的那几份（`skills/` 是 gitignore 的，
    上游无法知道本机有哪些）。`anime2` 之前的那一版是
    `workflow_prev_single_stage.json.bak`（单段单底模）。
    """

    def test_backups_survive(self):
        for name in ("workflow_1stage.json.bak",
                     "workflow_prev_single_stage.json.bak",
                     "workflow_2stage.json.bak",
                     "workflow_2pass_noscale.json.bak",
                     "workflow_2stage_twobase.json.bak",
                     "workflow_anime2_1538.json.bak"):
            p = os.path.join(SKILLS, "anima", name)
            self.assertTrue(os.path.exists(p), name)
            self.assertIsNotNone(load_workflow(p), name)


if __name__ == "__main__":
    unittest.main()
