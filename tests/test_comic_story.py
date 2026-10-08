"""连续小漫画（app/comic_story.py + tools/normal/generate_comic.py）的契约测试。

覆盖：解析（固定块 + `---` 动态块）、组装、格数收敛、编剧校验/回炉、
后台**整批**渲染的提交形态、漫画渠道的内部性、工具参数形态。LLM 与队列
全部 mock，不碰真机 ComfyUI。
"""

import sys
import os
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import comic_story


VALID = """hatsune_miku, blue_hair, twintails, aqua_eyes, hair_ribbon

concert_stage, blue_dress, singing, excited, from_side, stage_spotlight
---
bedroom, white_pajamas, sitting, sleepy, from_above, warm_lamp_light
"""


class ParseTest(unittest.TestCase):
    def test_parse_basic(self):
        fixed, dyns = comic_story.parse_story(VALID)
        self.assertEqual(fixed, "hatsune_miku, blue_hair, twintails, aqua_eyes, hair_ribbon")
        self.assertEqual(len(dyns), 2)
        self.assertTrue(dyns[0].startswith("concert_stage"))
        self.assertTrue(dyns[1].startswith("bedroom"))

    def test_parse_strips_fences(self):
        fixed, dyns = comic_story.parse_story("```\n" + VALID + "\n```")
        self.assertEqual(len(dyns), 2)

    def test_parse_tolerates_extra_blank_lines(self):
        text = "fixed_tag\n\n\ndyn_a\n\n---\n\ndyn_b\n"
        fixed, dyns = comic_story.parse_story(text)
        self.assertEqual(fixed, "fixed_tag")
        self.assertEqual(dyns, ["dyn_a", "dyn_b"])

    def test_parse_empty(self):
        self.assertEqual(comic_story.parse_story(""), ("", []))


class AssembleTest(unittest.TestCase):
    def test_assemble_joins_fixed_and_dynamic(self):
        fixed, dyns = comic_story.parse_story(VALID)
        prompts = comic_story.assemble(fixed, dyns)
        self.assertEqual(len(prompts), 2)
        self.assertIn("hatsune_miku", prompts[0])
        self.assertIn("concert_stage", prompts[0])
        self.assertIn("bedroom", prompts[1])

    def test_assemble_trims_trailing_commas(self):
        prompts = comic_story.assemble("a, b,", ["c, d,"])
        self.assertEqual(prompts, ["a, b, c, d"])


class ClampTest(unittest.TestCase):
    def test_clamp(self):
        self.assertEqual(comic_story.clamp_panels(None), 10)
        self.assertEqual(comic_story.clamp_panels("abc"), 10)
        self.assertEqual(comic_story.clamp_panels(0), 10)
        self.assertEqual(comic_story.clamp_panels(-3), 10)
        self.assertEqual(comic_story.clamp_panels(5), 5)
        self.assertEqual(comic_story.clamp_panels("7"), 7)
        self.assertEqual(comic_story.clamp_panels(99), comic_story.MAX_PANELS)


class WriteStoryTest(unittest.TestCase):
    def test_ok(self):
        with mock.patch.object(comic_story.llm, "call_llm", return_value=VALID):
            fixed, dyns = comic_story.write_story("演唱会", panels=2)
        self.assertEqual(len(dyns), 2)
        self.assertTrue(fixed.startswith("hatsune_miku"))

    def test_retry_then_ok(self):
        bad = "fixed_tag\n\nonly_one\n"          # 只 1 段，要求 2 段
        with mock.patch.object(comic_story.llm, "call_llm",
                               side_effect=[bad, VALID]) as m:
            fixed, dyns = comic_story.write_story("演唱会", panels=2)
        self.assertEqual(len(dyns), 2)
        self.assertEqual(m.call_count, 2)        # 第一次不合格，回炉一次

    def test_cjk_rejected(self):
        bad = "固定块, 蓝发\n\n中文动作, 中文场景\n---\n又一段中文\n"
        with mock.patch.object(comic_story.llm, "call_llm", return_value=bad):
            with self.assertRaises(RuntimeError):
                comic_story.write_story("x", panels=2)

    def test_empty_brief_raises(self):
        with self.assertRaises(ValueError):
            comic_story.write_story("   ")

    def test_single_user_message(self):
        """本项目约定：LLM 调用只发单条 user 消息，不用 system 角色。"""
        with mock.patch.object(comic_story.llm, "call_llm", return_value=VALID) as m:
            comic_story.write_story("演唱会", panels=2)
        msgs = m.call_args[0][0]
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0]["role"], "user")


class RenderTest(unittest.TestCase):
    """批量语义：**一次**入队，多格提示词用 `---` 拼进同一个 job。

    2026-10-08 之前是逐格 enqueue + wait（10 格 = 10 个 ComfyUI 任务、每格重读
    一遍权重，实测 ≈48 秒/格）；现在整批一个任务，所以下面断言的是「只排了
    1 个 job」而不是「按顺序排了 N 个」。
    """

    def _fake_enqueue(self, seen):
        class FakeJob:
            def wait(self):
                return {}
        def fn(target, target_id, wf, skill=None, prompt=None, **kw):
            seen.append({"target": target, "skill": skill, "prompt": prompt,
                         "landscape": kw.get("landscape")})
            return FakeJob(), None
        return fn

    def test_render_submits_one_batch_job(self):
        from app import image_jobs
        from app.tools.normal import generate_image as gi
        seen = []
        with mock.patch.object(gi, "build_t2i_workflow", return_value={"x": 1}) as bw, \
             mock.patch.object(image_jobs, "enqueue", side_effect=self._fake_enqueue(seen)), \
             mock.patch.object(gi, "_charge_quota", return_value=""):
            ok = comic_story.render("group", "123", "fixed_tag",
                                    ["d1", "d2", "d3"])
        self.assertEqual(ok, 3)
        self.assertEqual(len(seen), 1)              # 整批 = 一个任务
        self.assertEqual(seen[0]["skill"], comic_story.COMIC_SKILL)
        self.assertFalse(seen[0]["landscape"])      # 漫画是竖版
        # 多格拼成一份 `---` 分隔的文本，每格都带固定块。
        prompt = seen[0]["prompt"]
        self.assertEqual(prompt.split("\n---\n"),
                         ["fixed_tag, d1", "fixed_tag, d2", "fixed_tag, d3"])
        # 工作流拿到的是拼好的**多**提示词，不是某一格的单格提示词。
        self.assertEqual(bw.call_args[0][1], prompt)

    def test_render_returns_zero_when_rejected(self):
        """整批被拒（队列满 / ComfyUI 没在线）——一张都没画，返回 0。"""
        from app import image_jobs
        from app.tools.normal import generate_image as gi
        with mock.patch.object(gi, "build_t2i_workflow", return_value={"x": 1}), \
             mock.patch.object(image_jobs, "enqueue",
                               return_value=(None, "这个会话已经排着 5 张了，画完这些再说。")), \
             mock.patch.object(gi, "_charge_quota", return_value=""):
            ok = comic_story.render("group", "123", "fixed_tag", ["d1", "d2"])
        self.assertEqual(ok, 0)

    def test_render_returns_zero_when_workflow_missing(self):
        from app.tools.normal import generate_image as gi
        with mock.patch.object(gi, "build_t2i_workflow", return_value=None):
            ok = comic_story.render("group", "123", "fixed_tag", ["d1"])
        self.assertEqual(ok, 0)

    def test_render_survives_job_error(self):
        """任务失败（超时 / ComfyUI 掉线）不能把后台线程炸出去。"""
        from app import image_jobs
        from app.tools.normal import generate_image as gi

        class BoomJob:
            def wait(self):
                raise RuntimeError("画超时了")

        with mock.patch.object(gi, "build_t2i_workflow", return_value={"x": 1}), \
             mock.patch.object(image_jobs, "enqueue", return_value=(BoomJob(), None)), \
             mock.patch.object(gi, "_charge_quota", return_value=""):
            ok = comic_story.render("group", "123", "fixed_tag", ["d1", "d2"])
        self.assertEqual(ok, 0)

    def test_render_empty_dynamics(self):
        """一格都没有时直接返回，连工作流都不建。"""
        self.assertEqual(comic_story.render("group", "123", "fixed_tag", []), 0)


class ComicChannelTest(unittest.TestCase):
    """漫画渠道的形态约束——这三条塌一条，漫画就会退回到慢路子或漏成可点名渠道。"""

    def test_channel_is_internal(self):
        """目录名必须以 `_` 开头：`list_skills()` 跳过它 → AI 点不到它。"""
        self.assertTrue(comic_story.COMIC_SKILL.startswith("_"))
        from app.skills import list_skills
        self.assertNotIn(comic_story.COMIC_SKILL, list_skills())

    def test_channel_is_still_loadable(self):
        """不对外 ≠ 加载不了：`load_skill` 走纯文件系统路径，工作流要读得到。"""
        from app.skills import load_skill
        sd = load_skill(comic_story.COMIC_SKILL)
        self.assertTrue(sd and sd.get("workflow"))

    def test_channel_is_a_batch_workflow(self):
        """必须真的是批量节点，否则一次只出一张、整批拼提示词就白拼了。"""
        from app.skills import load_skill
        wf = load_skill(comic_story.COMIC_SKILL)["workflow"]
        batch = [n for n in wf.values()
                 if n["class_type"] == "BatchPromptImageGenerator"]
        self.assertEqual(len(batch), 1)
        ins = batch[0]["inputs"]
        # 分隔符必须和 comic_story 的契约同一个符号，否则只会当一段提示词。
        self.assertEqual(ins["delimiter"], "---")
        # 每格一个种子：关掉的话 N 格会画成同一张。
        self.assertTrue(ins["random_seed_per_prompt"])
        # 出图即落盘，批量多大都不爆内存。
        self.assertTrue(ins["save_inline"])


class ToolTest(unittest.TestCase):
    def test_tool_shape(self):
        from app.tools.normal.generate_comic import tool
        self.assertEqual(tool["name"], "generate_comic")
        props = tool["parameters"]["properties"]
        self.assertIn("brief", props)
        self.assertIn("panels", props)
        self.assertEqual(tool["parameters"]["required"], ["brief"])

    def test_tool_has_no_style_knob(self):
        """漫画只有一条渲染路——别再给模型一个「画风」旋钮。

        2026-10-08 用户拍板：「漫画就只有 silver 渠道呀，其他渠道没有漫画的」。
        """
        from app.tools.normal.generate_comic import tool
        self.assertNotIn("skill", tool["parameters"]["properties"])
        # 描述里要**明说**没有画风参数，否则模型会自己编一个出来
        # （不能只断言 "skill" 不在描述里——描述里本来就有 `skills/storyboard-prompt`）。
        self.assertIn("没有画风参数", tool["description"])


if __name__ == "__main__":
    unittest.main()
