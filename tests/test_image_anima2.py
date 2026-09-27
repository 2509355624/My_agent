"""anima 两个渠道：单底模（默认）与双底模（只点名才用）。

为什么要单独一套用例：`anima_2` 是**两段采样**那个实测必崩的工作流，用户要求
保留它、但**绝不能让它被自动选中**。这个「只有点名才走」的约束只写在提示词里
是拦不住模型的（同 `DISABLED_IMAGE_SKILLS` 那条教训），所以这里测的是**工作流
本身的性质**：它必须真的还是两段、第二段真的换底模——数据一变就得有人知道。

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
    """两个渠道都得在、都得被扫到——少一个模型就选不着。"""

    def test_both_are_scanned(self):
        names = list_skills()
        self.assertIn("anima", names)
        self.assertIn("anima_2", names)

    def test_both_load_as_image_skills(self):
        for name in ("anima", "anima_2"):
            data = load_skill(name)
            self.assertIsNotNone(data, name)
            self.assertEqual(data["kind"], "生图", name)
            self.assertTrue(data["workflow"], name)

    def test_neither_outranks_the_other(self):
        """都没被标成重渠道——没给 anima_2 任何「优先」或「靠后」的暗示。

        它靠的是**提示词里的「只点名才用」**，不是队列权重。
        """
        self.assertEqual(skill_priority("anima"), 1)
        self.assertEqual(skill_priority("anima_2"), 1)


class SingleStageTest(unittest.TestCase):
    """anima = 单底模单段：一次装载，这是它稳定的原因。"""

    def test_one_sampler_one_loader(self):
        wf = _load("anima")
        t = _types(wf)
        self.assertEqual(t.count("KSampler"), 1)
        self.assertEqual(t.count("UNETLoader"), 1)
        self.assertEqual(t.count("LatentUpscaleBy"), 0)

    def test_resolution_stays_at_the_safe_768x1024(self):
        """**这条是安全锁**：2026-09-27 把这里改成 1024×1536 后，单底模 anima
        单张就把整机拖到黑屏关机（ComfyUI 日志只有一次 3988MB 装载、采样到一半
        整机重启）。768×1024 是量出来的安全档位——想调高先读 `anima/SKILL.md`
        里那段警告，别直接改这个数。
        """
        wf = _load("anima")
        self.assertEqual(wf["9"]["inputs"]["width"], 768)
        self.assertEqual(wf["9"]["inputs"]["height"], 1024)

    def test_single_base_model(self):
        wf = _load("anima")
        unets = [wf[k]["inputs"]["unet_name"] for k, n in wf.items()
                 if n.get("class_type") == "UNETLoader"]
        self.assertEqual(unets, ["miaomiaoAnimeReality_ani11_3087842.safetensors"])


class DoubleStageTest(unittest.TestCase):
    """anima_2 = 双底模两段：这些性质就是「崩机风险」的来源，改了就变了个东西。"""

    def test_two_samplers_two_loaders(self):
        wf = _load("anima_2")
        t = _types(wf)
        self.assertEqual(t.count("KSampler"), 2)
        self.assertEqual(t.count("UNETLoader"), 2)

    def test_two_different_base_models(self):
        """两个底模必须真的是**两块不同的**——这是「双底模」的定义。"""
        wf = _load("anima_2")
        unets = sorted(wf[k]["inputs"]["unet_name"] for k, n in wf.items()
                       if n.get("class_type") == "UNETLoader")
        self.assertEqual(unets, ["miaomiaoAnimeReality_ani11_3087842.safetensors",
                                 "miaomiaoRealskin_anima13.safetensors"])

    def test_second_pass_refines_not_rebuilds(self):
        """第二段 denoise 必须低（0.25 精修），不是 1.0 重画。"""
        wf = _load("anima_2")
        self.assertEqual(wf["2"]["inputs"]["denoise"], 1)
        self.assertEqual(wf["19"]["inputs"]["denoise"], 0.25)

    def test_second_pass_reads_the_upscaled_latent(self):
        """第二段的 latent 来自放大节点，不是从零开始——链路别接错。"""
        wf = _load("anima_2")
        self.assertEqual(wf["18"]["class_type"], "LatentUpscaleBy")
        self.assertEqual(wf["19"]["inputs"]["latent_image"], ["18", 0])
        self.assertEqual(wf["3"]["inputs"]["samples"], ["19", 0])

    def test_resolution_matches_the_single_stage_one(self):
        """双底模也不能偷偷调高——它本来就比单底模更危险。

        两条渠道的底图尺寸必须一致（都 768×1024）：双底模是「同尺寸精修」，
        悄悄换个更大的尺寸等于换了个东西。
        """
        wf = _load("anima_2")
        self.assertEqual(wf["9"]["inputs"]["width"], 768)
        self.assertEqual(wf["9"]["inputs"]["height"], 1024)

    def test_second_pass_has_no_loras(self):
        """第二段底模不挂 lora——原版设计，别「顺手」补上。"""
        wf = _load("anima_2")
        self.assertEqual(wf["19"]["inputs"]["model"], ["20", 0])
        self.assertEqual(wf["20"]["class_type"], "UNETLoader")

    def test_loras_only_on_the_first_pass(self):
        wf = _load("anima_2")
        loras = {k for k, n in wf.items()
                 if n.get("class_type") == "LoraLoaderModelOnly"}
        self.assertEqual(loras, {"15", "16"})
        self.assertEqual(wf["16"]["inputs"]["model"], ["5", 0])   # 接第一段底模


class VisibilityTest(unittest.TestCase):
    """QQ 机器人得能看见它（白名单），否则点名叫了也传不进来。"""

    def test_qq_whitelist_includes_both(self):
        from app import agents
        for name in ("anima", "anima_2"):
            self.assertTrue(agents.allows_skill("qq", name), name)

    def test_skill_list_marks_it_as_named_only(self):
        """「只点名才用」必须写在 **anima_2 自己那一行**上。

        不能只断言整个块里有「点名」两个字——别的 skill 行也有「点名」
        （krea2 就是），那样等于没测（反证 E 就是这么漏过去的）。
        """
        from app.agent_prompt import _build_skill_list
        lines = [l for l in _build_skill_list("qq").splitlines()
                 if "**anima_2**" in l]
        self.assertEqual(len(lines), 1, "anima_2 应当在目录里出现且只出现一次")
        self.assertIn("点名", lines[0])
        # 单底模那行不能被误标成「点名才用」——它是默认渠道
        a1 = [l for l in _build_skill_list("qq").splitlines()
              if "**anima**" in l]
        self.assertEqual(len(a1), 1)
        self.assertIn("默认", a1[0])

    def test_descriptions_warn_not_to_auto_pick(self):
        from app.tools.normal.generate_image import tool
        from app.config import QQ_AGENT_ID
        for desc in (tool["description"],
                     tool["description_overrides"][QQ_AGENT_ID]):
            self.assertIn("anima_2", desc)
            self.assertIn("点名", desc)


class RestoreTest(unittest.TestCase):
    """备份还在——用户明确要求保留，删了就没法回滚了。"""

    def test_backups_survive(self):
        for name in ("workflow_1stage.json.bak", "workflow_2stage.json.bak",
                     "workflow_2pass_noscale.json.bak"):
            p = os.path.join(SKILLS, "anima", name)
            self.assertTrue(os.path.exists(p), name)
            self.assertIsNotNone(load_workflow(p), name)


if __name__ == "__main__":
    unittest.main()
