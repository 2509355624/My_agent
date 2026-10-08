"""连续小漫画（app/comic_story.py + tools/normal/generate_comic.py）的契约测试。

覆盖：解析（固定块 + `---` 动态块）、组装、格数收敛、编剧校验/回炉、
后台逐格渲染的入队顺序与失败隔离、工具参数形态。LLM 与队列全部 mock，
不碰真机 ComfyUI。
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
    def _fake_enqueue(self, seen):
        class FakeJob:
            def wait(self):
                return {}
        def fn(target, target_id, wf, skill=None, prompt=None, **kw):
            seen.append({"target": target, "skill": skill, "prompt": prompt,
                         "landscape": kw.get("landscape")})
            return FakeJob(), None
        return fn

    def test_render_enqueues_in_order(self):
        from app import image_jobs
        from app.tools.normal import generate_image as gi
        seen = []
        with mock.patch.object(gi, "build_t2i_workflow", return_value={"x": 1}), \
             mock.patch.object(image_jobs, "enqueue", side_effect=self._fake_enqueue(seen)), \
             mock.patch.object(gi, "_charge_quota", return_value=""):
            ok = comic_story.render("group", "123", "fixed_tag",
                                    ["d1", "d2", "d3"], "silver")
        self.assertEqual(ok, 3)
        self.assertEqual(len(seen), 3)
        self.assertTrue(seen[0]["prompt"].startswith("fixed_tag"))
        self.assertIn("d1", seen[0]["prompt"])
        self.assertIn("d3", seen[2]["prompt"])
        self.assertEqual(seen[0]["skill"], "silver")
        self.assertFalse(seen[0]["landscape"])     # 漫画是竖版

    def test_render_survives_one_bad_panel(self):
        from app import image_jobs
        from app.tools.normal import generate_image as gi
        seen = []
        calls = {"n": 0}

        def flaky_enqueue(target, target_id, wf, skill=None, prompt=None, **kw):
            calls["n"] += 1
            if calls["n"] == 2:
                return None, "这个会话已经排着 5 张了，画完这些再说。"
            return self._fake_enqueue(seen)(target, target_id, wf, skill, prompt, **kw)

        with mock.patch.object(gi, "build_t2i_workflow", return_value={"x": 1}), \
             mock.patch.object(image_jobs, "enqueue", side_effect=flaky_enqueue), \
             mock.patch.object(gi, "_charge_quota", return_value=""):
            ok = comic_story.render("group", "123", "fixed_tag",
                                    ["d1", "d2", "d3"], "silver")
        self.assertEqual(ok, 2)                    # 中间一格被拒，其余照发


class ToolTest(unittest.TestCase):
    def test_tool_shape(self):
        from app.tools.normal.generate_comic import tool
        self.assertEqual(tool["name"], "generate_comic")
        props = tool["parameters"]["properties"]
        self.assertIn("brief", props)
        self.assertIn("panels", props)
        self.assertIn("skill", props)
        self.assertEqual(tool["parameters"]["required"], ["brief"])

    def test_style_normalize(self):
        from app.tools.normal.generate_comic import _norm_style
        self.assertEqual(_norm_style(None), "silver")
        self.assertEqual(_norm_style(""), "silver")
        self.assertEqual(_norm_style("bogus"), "silver")
        self.assertEqual(_norm_style("qwen_image_v1"), "qwen_image_v1")
        self.assertEqual(_norm_style("hd_3_clear"), "hd_3_clear")


if __name__ == "__main__":
    unittest.main()
