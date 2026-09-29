"""anima（唯一的动漫渠道）：两段采样工作流的性质锁。

## 为什么还留着这套用例

2026-09-30 起动漫渠道**只剩 anima 一个**——原先单段的 `anima` 和两段的
`anima_2` 合并了：`skills/anima/workflow.json` 现在就是用户优化过的那套
两段采样（一段 10 步 er_sde 建构图 + 二段 5 步 euler 补细节）。

`anima_2` 目录已删。但下面这些性质**仍然值得锁**，因为它们是「画质/速度能
成立」的前提，而它们全写在 JSON 里，肉眼看不出来：

- 只有**一块**底模（UNETLoader 只有一个）；
- 第一段 denoise=1、第二段 denoise=0.25（是精修不是重画）；
- 第二段的 latent 来自放大节点（链路别接错）；
- 分辨率停在 728×1024（**安全锁**，见下）；
- 二次采样的作用 = 补细节，所以第二段必须真的在跑（KSampler 两个）。

数据一被人改动就得有人知道——这就是这套用例存在的全部理由。

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

    def test_the_old_two_stage_channel_is_gone(self):
        """`anima_2` 已删除，不能有任何残留让它被重新列出来。"""
        self.assertNotIn("anima_2", list_skills())
        self.assertIsNone(load_skill("anima_2"))


class TwoStageTest(unittest.TestCase):
    """两段采样：这些性质就是「细节更丰富但只花 ~11 秒」的定义。"""

    def test_two_samplers_one_loader(self):
        """**核心不变量之一**：两个采样器，但只有一个底模装载器。

        ⚠️ 别把这条当成「装载只有一次」的证明——`CacheSet` 按「节点 id + 输入
        签名」缓存的是**节点输出**，而「要不要往显存里装」是 `model_management`
        按 **model 对象**算的，两者不是一回事。这条只挡住「又塞回第二块底模」。
        """
        wf = _load("anima")
        t = _types(wf)
        self.assertEqual(t.count("KSampler"), 2)
        self.assertEqual(t.count("UNETLoader"), 1)

    def test_base_model_is_realskin(self):
        """底模必须是 `miaomiaoRealskin_anima13`——换底模等于换画风。"""
        wf = _load("anima")
        unets = [wf[k]["inputs"]["unet_name"] for k, n in wf.items()
                 if n.get("class_type") == "UNETLoader"]
        self.assertEqual(unets, ["miaomiaoRealskin_anima13.safetensors"])

    def test_positive_prompt_is_a_template_not_a_fixed_character(self):
        """**核心不变量（09-30 修）**：节点 4 必须是可代入的模板，不能是写死的角色。

        踩过的坑：把用户在 ComfyUI 里调好的工作流搬进 skills 时，节点 4 里存的
        是**那张图当时的完整提示词**（1166 字的银白双马尾角色 + 门口场景）。
        `generate_image` 的机制是「把工作流串里的 `__MULTI_PROMPTS__` 换成模型
        给的提示词」——那串字里**没有占位符**，于是模型传什么都被原样丢弃，
        私聊里连着画了三四张都是同一个角色，用户问「1girl, orange cat ears」
        出来的还是银白发夹，差点以为是模型不听话。

        所以这条锁两件事：① 有占位符；② 正文里不含具体角色/场景词。
        **只留画风前缀**（`@kibro` 是 lora 触发词、`anime illustration style`
        是画风锚点，这两个不能丢）。
        """
        wf = _load("anima")
        text = wf["4"]["inputs"]["text"]
        self.assertIn("__MULTI_PROMPTS__", text)
        self.assertTrue(text.startswith("@kibro, anime illustration style,"),
                        "画风前缀丢了：%r" % text)
        # 具体角色词一个都不许留——留了就是又写死了
        for word in ("silver-white", "twin tails", "hairclip", "genkan",
                     "stockings", "camisole", "pearl"):
            self.assertNotIn(word, text, "节点 4 又写死了角色：%r" % word)
        # 模板本身该很短，写死的角色串都上千字
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
        import os
        p = os.path.join(SKILLS, "anima", "workflow.json")
        with open(p, encoding="utf-8") as f:
            raw = f.read()
        self.assertEqual(raw.count("__SEED__"), 2,
                         "两个 KSampler 的 seed 都该是 __SEED__ 占位符")

    def test_second_pass_refines_not_rebuilds(self):
        """第一段 denoise=1（从零建），第二段 denoise=0.25（精修，不是重画）。"""
        wf = _load("anima")
        self.assertEqual(wf["2"]["inputs"]["denoise"], 1)
        self.assertEqual(wf["27"]["inputs"]["denoise"], 0.25)

    def test_second_pass_uses_euler_not_er_sde(self):
        """**这是 09-30 缩小画质差距的关键**，锁住它。

        第二段只有 5 步：`er_sde` 是随机（SDE）采样器，步数少时噪声没收敛完，
        表现为头发糊、珍珠/蕾丝融在一起、手指断。换成确定性的 `euler` 之后
        5 步也能干净出图。第一段保留 `er_sde`（10 步、从零构图，要它的多样性）。
        """
        wf = _load("anima")
        self.assertEqual(wf["2"]["inputs"]["sampler_name"], "er_sde")
        self.assertEqual(wf["27"]["inputs"]["sampler_name"], "euler")
        self.assertEqual(wf["2"]["inputs"]["steps"], 10)
        self.assertEqual(wf["27"]["inputs"]["steps"], 5)

    def test_second_pass_reads_the_upscaled_latent(self):
        """第二段的 latent 来自放大节点，不是从零开始——链路别接错。"""
        wf = _load("anima")
        self.assertEqual(wf["25"]["class_type"], "LatentUpscaleBy")
        self.assertEqual(wf["27"]["inputs"]["latent_image"], ["25", 0])
        self.assertEqual(wf["3"]["inputs"]["samples"], ["27", 0])

    def test_second_pass_model_reference(self):
        """第二段指向**裸底模**（节点 5）——这是**有意如此**，别「优化」成节点 15。

        09-30 查过：把第二段改成引用 LoRA 链尾（节点 15）能让 ComfyUI 少装一次
        3988MB、快 3~4 秒。但第二段的画风会跟着变（从「裸底模精修」变成
        「带 LoRA 精修」），用户明确否掉了这个方案。

        同时实测证明：**这次重装（`3988MB Staged. 0 patches`）并不致命** ——
        09-30 的 comfyui_8188.log 里这套两段工作流在同一个脏进程上连跑 8 次
        全成（10.7~11.7 秒），最密两张只隔 1 秒。所以保持原样。
        """
        wf = _load("anima")
        self.assertEqual(wf["27"]["inputs"]["model"], ["5", 0])
        self.assertEqual(wf["2"]["inputs"]["model"], ["15", 0])
        self.assertEqual(wf["5"]["class_type"], "UNETLoader")
        self.assertEqual(wf["15"]["class_type"], "LoraLoaderModelOnly")

    def test_loras_only_on_the_first_pass(self):
        """LoRA 链：节点 15（baka skin 0.5）← 节点 16（kibro 1.0）← 节点 5。"""
        wf = _load("anima")
        loras = {k for k, n in wf.items()
                 if n.get("class_type") == "LoraLoaderModelOnly"}
        self.assertEqual(loras, {"15", "16"})
        self.assertEqual(wf["16"]["inputs"]["model"], ["5", 0])   # 接底模
        self.assertEqual(wf["15"]["inputs"]["model"], ["16", 0])  # 接上一环
        self.assertEqual(wf["15"]["inputs"]["strength_model"], 0.5)


class ResolutionTest(unittest.TestCase):
    """**安全锁**：分辨率是独立杠杆，别往上调。"""

    def test_resolution_stays_at_the_safe_728x1024(self):
        """2026-09-27 把它调到 1024×1536 后，单张就把整机拖到黑屏关机（日志里
        只有一次 3988MB 装载、采样到一半整机重启）。728×1024 是量出来的安全档
        ——想调高先读 `anima/SKILL.md` 里那段警告，别直接改这个数。
        """
        wf = _load("anima")
        self.assertEqual(wf["9"]["inputs"]["width"], 728)
        self.assertEqual(wf["9"]["inputs"]["height"], 1024)

    def test_upscale_does_not_change_size(self):
        """`LatentUpscaleBy` 的 `scale_by` 必须是 1（改尺寸的手段，不是放大器）。"""
        wf = _load("anima")
        self.assertEqual(wf["25"]["inputs"]["scale_by"], 1)


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
        """两份工具描述都不能再提 `anima_2`——渠道已删，提它只会诱导模型乱传。"""
        from app.tools.normal.generate_image import tool
        from app.config import QQ_AGENT_ID
        for desc in (tool["description"],
                     tool["description_overrides"][QQ_AGENT_ID]):
            self.assertNotIn("anima_2", desc)


class RestoreTest(unittest.TestCase):
    """备份还在——用户明确要求保留，删了就没法回滚了。"""

    def test_backups_survive(self):
        for name in ("workflow_1stage.json.bak", "workflow_1stage_ani11.json.bak-prev",
                     "workflow_2stage.json.bak",
                     "workflow_2pass_noscale.json.bak"):
            p = os.path.join(SKILLS, "anima", name)
            self.assertTrue(os.path.exists(p), name)
            self.assertIsNotNone(load_workflow(p), name)


if __name__ == "__main__":
    unittest.main()
