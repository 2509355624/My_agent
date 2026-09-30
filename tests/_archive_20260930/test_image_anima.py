"""动漫渠道（`anima` / `anima_realskin`）：两段采样工作流的性质锁。

## 这套用例锁的是什么

2026-09-30 18:xx 起动漫渠道有**两个**，都直接取自用户在 ComfyUI 里调好的
`anime2`（`D:\\AI\\ComfyUI\\user\\default\\workflows\\anime2.json`）。
**每次用户在这个文件里调完参数，对应的 `skills/*/workflow.json` 都要跟着重转**
（UI 格式 → API 格式，步骤见 `skills/anima/skill.md`）。

| | `anima_realskin`（**默认**） | `anima`（点名才用） |
|---|---|---|
| 一段底模 | Ani1.1 (reality) | Ani1.1 (reality) |
| 二段底模 | `miaomiaoRealskin_anima13` | Ani1.1（同一块，所以叫「双 reality」） |

两个渠道**共用同一套结构**，差别只在第二段的底模（外加一段的采样器参数）：

    一段：UNETLoader(5, Ani1.1) → LoRA(16 kibro 1.0) → LoRA(15 baka skin 0.5)
          → KSampler(2) → LatentUpscaleBy(24, nearest-exact 1.1×)
    二段：UNETLoader(20) → KSampler(19) denoise 0.25（latent 来自 24，**不接 LoRA**）

**`LatentUpscaleBy(24)` 夹在两段之间**（`2 → 24 → 19`）：768×1024 的 latent（96×128）
先放大到 106×141，**实际输出 848×1128**。

这些性质全写在 JSON 里、肉眼看不出来，但每一个都有明确的「为什么」：

- 两段是「细节更多」的来源，所以**两个 KSampler 必须都真的在跑**；
- 第二段 denoise=0.25 是**精修不是重画**，改成 1.0 就变成画两张不同的图；
- 分辨率停在 768×1024（**安全锁**，见下），放大倍率停在 1.1×；
- 节点 4 必须是**可代入的模板**，不能是写死的角色；
- `__SEED__` 必须是占位符，写死成数字等于每张图一模一样。

数据一被人改动就得有人知道——这就是这套用例存在的全部理由。

> ⚠️ **上游 `5fe5d81` 的 `anima` 和本机这套不是同一个工作流**：上游是
> 「单底模 + 两段 + `LatentUpscaleBy(scale_by=1)` + 728×1024」，本机是
> 「两段 + 1.1× 放大 + 768×1024」。所以**别照抄上游的用例**，
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

        ④ 同一天 18:12 第四次：节点 4 变成 417 字的
           「Firefly (Honkai: Star Rail) + 女仆装 + 奶瓶」。
           这一版是 `anima_realskin` 的来源，**同样在转换时换成了模板**
           （见 `RealSkinChannelTest::test_prompt_is_a_template_with_the_lora_trigger`）。

        所以这条锁两件事：① 有占位符；② 正文里不含具体角色/场景词。
        **只留画风前缀**（`@kibro` 是 lora 触发词，不能丢）。
        """
        wf = _load("anima")
        text = wf["4"]["inputs"]["text"]
        self.assertIn("__MULTI_PROMPTS__", text)
        self.assertTrue(text.startswith("@kibro,"),
                        "画风前缀丢了：%r" % text)
        # 具体角色词一个都不许留——留了就是又写死了。
        # 前 6 个是坑①的残留词，其余是坑②/③/④（anime2 四个版本自带）的残留词。
        for word in ("silver-white", "twin tails", "hairclip", "genkan",
                     "camisole", "pearl",
                     "golden hair", "fox ears", "fox tail", "red hood",
                     "vtuber-style", "neon", "stage background",
                     "sleeveless dress", "thigh-high", "frilled",
                     "isolated on white", "plain white background",
                     # 坑④（18:12 版）的残留词
                     "Firefly", "Honkai", "Star Rail", "flat chest", "petite",
                     "mint green", "maid headband", "baby bottle",
                     "pastel pink", "frilly", "blushing"):
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
        """第二段吃**放大后**的 latent——链路别接错（这是「精修」的前提）。

        2026-09-30 18:12 用户在两段之间插了 `LatentUpscaleBy(24)`，
        所以 19 的 latent 来源从 `2` 变成 `24`。**别把这条改回 `2`** ——
        那等于把放大节点晾在一边（它还占着显存和一次上采样）。
        """
        wf = _load("anima")
        self.assertEqual(wf["19"]["inputs"]["latent_image"], ["24", 0])
        self.assertEqual(wf["24"]["inputs"]["samples"], ["2", 0])
        self.assertEqual(wf["3"]["inputs"]["samples"], ["19", 0])

    def test_upscale_node_sits_between_the_two_passes(self):
        """放大节点必须有，且必须**夹在两段之间**、倍率封顶 1.1×。

        ⚠️ 2026-09-30 之前这条用例是反过来的（`test_there_is_no_upscale_node`
        断言**没有**放大节点）。那天 18:12 用户在 ComfyUI 里加了 1.1× 放大
        并明确要求同步过来，所以锁的方向反了 —— **这是有意的，不是把断言删了**。

        为什么要封顶：`scale_by` 上去是平方级的显存和耗时，而本机
        （RTX 5070 12GB + 16GB）两段还要先后装两块 3988MB 底模，没余量。
        768×1024 → 96×128 latent，×1.1 用 `round()` 得 106×141，
        即实际输出 **848×1128**（`comfy` 的 `LatentUpscaleBy`，见 `nodes.py:1393`）。
        """
        wf = _load("anima")
        self.assertIn("LatentUpscaleBy", _types(wf))
        up = wf["24"]
        self.assertEqual(up["class_type"], "LatentUpscaleBy")
        self.assertEqual(up["inputs"]["upscale_method"], "nearest-exact")
        self.assertLessEqual(up["inputs"]["scale_by"], 1.1,
                             "放大倍率超过 1.1× 了——先量显存再动")

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
    """QQ 机器人得能看见它们（白名单），否则默认渠道也传不进来。"""

    def test_qq_whitelist_includes_both_anime_channels(self):
        from app import agents
        self.assertTrue(agents.allows_skill("qq", "anima"))
        self.assertTrue(agents.allows_skill("qq", "anima_realskin"))

    def test_default_channel_is_realskin(self):
        """2026-09-30 18:xx 用户拍板：默认动漫渠道 = `anima_realskin`。

        历史：09-30 白天默认还是 `anima`，那天 18:12 用户把 `anime2.json` 的
        第二段换成 `miaomiaoRealskin_anima13` 并要求「两个渠道都给，
        这个当默认」，于是常量跟着搬了。
        """
        from app.tools.normal.generate_image import T2I_DEFAULT_SKILL
        self.assertEqual(T2I_DEFAULT_SKILL, "anima_realskin")
        self.assertIsNotNone(load_skill(T2I_DEFAULT_SKILL))

    def test_skill_list_marks_exactly_one_anime_channel_as_default(self):
        """目录里必须**恰好一个**动漫渠道自称「默认」，且那个是 `anima_realskin`。

        注意 `"**anima**"` 这个子串**不会**匹配 `**anima_realskin**`
        （`anima` 后面跟的是 `_` 不是 `*`），所以两行能分开过滤。
        """
        from app.agent_prompt import _build_skill_list
        lines = _build_skill_list("qq").splitlines()

        def line_for(name):
            hits = [l for l in lines if ("**%s**" % name) in l]
            self.assertEqual(len(hits), 1, "%s 应当出现且只出现一次" % name)
            return hits[0]

        self.assertIn("默认", line_for("anima_realskin"))
        self.assertIn("点名", line_for("anima"),
                      "anima 现在应当是「点名才用」")

        defaults = [l for l in lines if "默认" in l and "anima" in l]
        self.assertEqual(len(defaults), 1,
                         "有多个动漫渠道自称默认：%r" % defaults)

    def test_descriptions_never_mention_the_retired_channel(self):
        """两份工具描述都不能提 `anima_2`——它对本机是死目录，提了只会诱导模型乱传。

        注意：描述里**会**出现 `anima` 和 `anima_realskin`（这是有意暴露给模型的
        两个动漫渠道），但 `anima_2` 必须一个字都不出现。
        """
        from app.tools.normal.generate_image import tool
        from app.config import QQ_AGENT_ID
        for desc in (tool["description"],
                     tool["description_overrides"][QQ_AGENT_ID]):
            self.assertNotIn("anima_2", desc)


class RealSkinChannelTest(unittest.TestCase):
    """`anima_realskin`（默认渠道）：和 `anima` 同源，只换第二段的底模。

    数据来自用户 2026-09-30 18:12 在 ComfyUI 里存的那版 `anime2.json`。
    """

    def test_exists_and_is_an_image_skill(self):
        data = load_skill("anima_realskin")
        self.assertIsNotNone(data)
        self.assertEqual(data["kind"], "生图")
        self.assertTrue(data["workflow"])
        # 没被标成重渠道——它靠「默认就是它」，不靠队列权重
        self.assertEqual(skill_priority("anima_realskin"), 1)

    def test_two_samplers_two_loaders_and_one_upscale(self):
        """核心不变量：两个采样器、两块底模装载器、一个放大节点。"""
        wf = _load("anima_realskin")
        t = _types(wf)
        self.assertEqual(t.count("KSampler"), 2)
        self.assertEqual(t.count("UNETLoader"), 2)
        self.assertEqual(t.count("LatentUpscaleBy"), 1)

    def test_second_pass_uses_realskin_the_first_uses_reality(self):
        """**这就是两个动漫渠道的差异点**，别被同步脚本抹平了。

        `anima` 是「两块都是 Ani1.1 reality」（双 reality）；
        `anima_realskin` 的第二段换成 RealSkin——是真正的双底模，
        两段之间会真的换一次模型。
        """
        wf = _load("anima_realskin")
        self.assertEqual(wf["5"]["inputs"]["unet_name"],
                         "miaomiaoAnimeReality_ani11_3087842.safetensors")
        self.assertEqual(wf["20"]["inputs"]["unet_name"],
                         "miaomiaoRealskin_anima13.safetensors")

    def test_upscale_sits_between_the_passes(self):
        wf = _load("anima_realskin")
        self.assertEqual(wf["24"]["class_type"], "LatentUpscaleBy")
        self.assertEqual(wf["24"]["inputs"]["samples"], ["2", 0])
        self.assertEqual(wf["19"]["inputs"]["latent_image"], ["24", 0])
        self.assertLessEqual(wf["24"]["inputs"]["scale_by"], 1.1,
                             "放大倍率超过 1.1× 了——先量显存再动")

    def test_prompt_is_a_template_with_the_lora_trigger(self):
        """`@kibro` 是 kibro LoRA 的触发词（训练集 165/165 张图都带它），不能丢。"""
        wf = _load("anima_realskin")
        self.assertEqual(wf["4"]["inputs"]["text"], "@kibro, __MULTI_PROMPTS__")
        neg = wf["8"]["inputs"]["text"]
        self.assertIn("worst quality", neg)
        self.assertNotIn("__MULTI_PROMPTS__", neg)

    def test_seeds_stay_placeholders(self):
        p = os.path.join(SKILLS, "anima_realskin", "workflow.json")
        with open(p, encoding="utf-8") as f:
            raw = f.read()
        self.assertEqual(raw.count("__SEED__"), 2,
                         "两个 KSampler 的 seed 都该是 __SEED__ 占位符")

    def test_second_pass_is_deterministic_refinement(self):
        """第二段 denoise 0.25 = 精修，所以必须是确定性采样器 `euler`。

        第一段的采样器**不锁**——它 denoise=1.0 是从零建构图，用什么都行，
        而且那正是用户随手调的旋钮（18:12 那版是 `dpmpp_2m`）。
        """
        wf = _load("anima_realskin")
        self.assertEqual(wf["19"]["inputs"]["sampler_name"], "euler")
        self.assertEqual(wf["19"]["inputs"]["scheduler"], "simple")
        self.assertEqual(wf["19"]["inputs"]["denoise"], 0.25)
        self.assertLess(wf["19"]["inputs"]["denoise"],
                        wf["2"]["inputs"]["denoise"])

    def test_resolution_and_upscale_stay_within_the_safe_box(self):
        wf = _load("anima_realskin")
        self.assertEqual(wf["9"]["inputs"]["width"], 768)
        self.assertEqual(wf["9"]["inputs"]["height"], 1024)
        self.assertLessEqual(wf["24"]["inputs"]["scale_by"], 1.1)

    def test_loras_only_on_the_first_pass(self):
        wf = _load("anima_realskin")
        loras = {k for k, n in wf.items()
                 if n.get("class_type") == "LoraLoaderModelOnly"}
        self.assertEqual(loras, {"15", "16"})
        self.assertEqual(wf["16"]["inputs"]["model"], ["5", 0])   # 接底模
        self.assertEqual(wf["15"]["inputs"]["model"], ["16", 0])  # 接上一环
        self.assertEqual(wf["2"]["inputs"]["model"], ["15", 0])
        # 第二段走裸底模，**有意不接 LoRA 链尾**
        self.assertEqual(wf["19"]["inputs"]["model"], ["20", 0])


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
                     "workflow_anime2_1538.json.bak",
                     # 2026-09-30 加 1.1× 放大之前的那版（双 reality、无放大）
                     "workflow_2reality_noscale.json.bak"):
            p = os.path.join(SKILLS, "anima", name)
            self.assertTrue(os.path.exists(p), name)
            self.assertIsNotNone(load_workflow(p), name)


if __name__ == "__main__":
    unittest.main()
