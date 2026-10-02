"""自定义**识图（通用读图）提示词**：管理页存的 vision_prompt 怎么取值、怎么拼。

2026-10-02 用户提的两件事：① 识图「老是分析不清楚图片」，提示词要能自己改；
② 改的位置是管理页，不是改代码 + 重启。整条链仿照审核提示词
（`image_audit_prompt`）那一套：settings.json 存一份 → `agents.vision_prompt`
只认非空字符串 → `vision.build_prompt(base=...)` 用它替掉内置默认 →
`agent._with_vision` 按 agent_id 取。

**口径**：这个开关只管「对方发图 → 转文字给对话模型」这一条链路。生图前的
NSFW 审核有自己的 `image_audit_prompt`，表情包打标签写死在 `stickers._tag`，
两张卡互不影响——所以这里改坏了也不会拦掉任何图，最坏只是读得不细。
"""

import os
import tempfile
import unittest
from unittest import mock

from app import agents
from app import main
from app import vision


class BuildPromptBaseTest(unittest.TestCase):
    """vision.build_prompt 的 base 分支：自定义那份怎么带上用户的问题。"""

    def test_no_base_keeps_the_builtin_prompts(self):
        """没设自定义 → 与从前一字不差（内置两份 + 没问题时不加多图头）。"""
        self.assertEqual(vision.build_prompt(""), vision._PROMPT)
        self.assertEqual(vision.build_prompt("报错啥意思"),
                         vision._PROMPT_WITH_QUESTION % "报错啥意思")

    def test_custom_without_question_is_used_verbatim(self):
        self.assertEqual(vision.build_prompt("", base="只数图里有几个人"),
                         "只数图里有几个人")

    def test_custom_without_placeholder_still_gets_the_question(self):
        """**问题丢不掉**：没写占位符就补在末尾一行。

        这条是整份替换唯一不能让 user 写坏的地方——识图模型看不见对方说了
        什么，只按读图要求泛泛描述，下游就没法知道到底该重点看哪里。
        """
        p = vision.build_prompt("头发为啥糊", base="详细描述画面。")
        self.assertTrue(p.startswith("详细描述画面。"))
        self.assertIn("用户的需求：头发为啥糊", p)

    def test_placeholder_is_replaced_in_place(self):
        p = vision.build_prompt("这是啥报错", base="先回答：{question}\n再描述画面。")
        self.assertEqual(p, "先回答：这是啥报错\n再描述画面。")
        self.assertNotIn("{question}", p)

    def test_placeholder_dropped_when_there_is_no_question(self):
        """没问题时占位符不能孤零零留在文案里。"""
        self.assertNotIn("{question}", vision.build_prompt("", base="答：{question}"))

    def test_percent_sign_in_the_custom_text_does_not_crash(self):
        """用户贴「100% 还原」不能炸——所以走 replace 而不是 % 格式化。

        内置那份 `_PROMPT_WITH_QUESTION % q` 是模块自己的文案、里面只有一个
        成对写法之外的裸 `%s`，不受影响；自定义的那份什么都有可能。
        """
        self.assertEqual(
            vision.build_prompt("", base="色彩还原要 100% 忠实，别漏 % 号"),
            "色彩还原要 100% 忠实，别漏 % 号")

    def test_question_is_still_truncated_for_custom_base(self):
        long_q = "长" * (vision.QUESTION_MAX_CHARS + 50)
        p = vision.build_prompt(long_q, base="看。")
        self.assertIn("长" * vision.QUESTION_MAX_CHARS + "…", p)
        self.assertNotIn("长" * (vision.QUESTION_MAX_CHARS + 1), p)

    def test_multi_image_head_survives_with_custom_base(self):
        """多图逐张送，自定义文案照样得标出这是第几张。"""
        p = vision.build_prompt("看下", 2, 3, base="详细描述。")
        self.assertIn("第 2 张，共 3 张", p)
        self.assertIn("详细描述。", p)


class VisionPromptSettingTest(unittest.TestCase):
    """agents.vision_prompt：与 image_audit_prompt 同口径——只认非空字符串。"""

    def _setting(self, settings):
        with mock.patch.object(agents, "load_settings", return_value=settings):
            return agents.vision_prompt("qq")

    def test_unset_is_empty_string(self):
        self.assertEqual(self._setting({}), "")

    def test_blank_is_treated_as_unset(self):
        self.assertEqual(self._setting({"vision_prompt": "   \n  "}), "")

    def test_non_string_is_treated_as_unset(self):
        for bad in (None, 123, True, ["列表"]):
            self.assertEqual(self._setting({"vision_prompt": bad}), "",
                             "坏配置应当回落内置默认：%r" % (bad,))

    def test_real_value_is_stripped_and_returned(self):
        self.assertEqual(self._setting({"vision_prompt": "  先看手  "}), "先看手")


class AgentUsesCustomPromptTest(unittest.TestCase):
    """整条链：run → _with_vision 按 agent_id 取那份自定义文案传给识图。"""

    def _prompts(self, custom, **kw):
        seen = []

        def fake_describe(data_url, timeout=None, prompt=None):
            seen.append(prompt)
            return "一只猫"

        with mock.patch("app.vision.describe", fake_describe), \
             mock.patch.object(agents, "vision_prompt",
                               return_value=custom) as vp:
            out = self._call(**kw)
        return out, seen, vp

    def _call(self, images=None, agent_id="qq"):
        from app import agent
        return agent._with_vision("看看这图", images or ["data:1"],
                                  ["温知澄"], agent_id)

    def test_custom_prompt_reaches_the_vision_call(self):
        out, seen, _ = self._prompts("逐项描述人物、服装、背景。")
        self.assertEqual(seen[0].startswith("逐项描述人物、服装、背景。"), True)
        self.assertNotIn(vision._PROMPT, seen[0])
        self.assertIn("一只猫", out)

    def test_builtin_is_used_when_nothing_is_set(self):
        _, seen, _ = self._prompts("")
        self.assertIn("原样提取其中的所有文字", seen[0])

    def test_settings_are_read_once_per_turn(self):
        """一轮多张图只读一次 settings，不在每张图上重读文件。"""
        _, seen, vp = self._prompts("看细节。", images=["data:1", "data:2"])
        self.assertEqual(len(seen), 2)
        self.assertEqual(vp.call_count, 1)

    def test_no_agent_id_falls_back_to_builtin(self):
        """老调用方不传 agent_id（含测试里的直接调用）→ 行为与从前一致。"""
        seen = []
        with mock.patch("app.vision.describe",
                        lambda data_url, timeout=None, prompt=None:
                        seen.append(prompt) or "一只猫"):
            from app import agent
            agent._with_vision("看看这图", ["data:1"], ["温知澄"])
        self.assertIn("原样提取其中的所有文字", seen[0])


class AdminApiTest(unittest.TestCase):
    """管理页读写接口：PUT /api/agent/<id>/vision_prompt + 列表接口带两个字段。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = os.path.join(self.tmp.name, "agents")
        os.makedirs(root, exist_ok=True)
        p = mock.patch.object(agents, "AGENTS_DIR", root)
        p.start()
        self.addCleanup(p.stop)
        agents.clear_cache()
        self.addCleanup(agents.clear_cache)
        p = mock.patch.object(main, "ADMIN_ALLOW_REMOTE", True)
        p.start()
        self.addCleanup(p.stop)
        self.client = main.app.test_client()

    def url(self):
        return "/api/agent/qq/vision_prompt"

    def test_save_then_read_back(self):
        r = self.client.put(self.url(), json={"prompt": "我的读图要求"})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.get_json()["using_default"])
        self.assertEqual(agents.vision_prompt("qq"), "我的读图要求")

    def test_blank_restores_the_builtin_default(self):
        self.client.put(self.url(), json={"prompt": "我的读图要求"})
        r = self.client.put(self.url(), json={"prompt": "   "})
        self.assertTrue(r.get_json()["using_default"])
        self.assertEqual(agents.vision_prompt("qq"), "")
        self.assertEqual(r.get_json()["effective"], vision.default_prompt())

    def test_missing_field_is_rejected(self):
        self.assertEqual(self.client.put(self.url(), json={}).status_code, 400)

    def test_non_string_is_rejected(self):
        self.assertEqual(
            self.client.put(self.url(), json={"prompt": 123}).status_code, 400)

    def test_too_long_is_rejected(self):
        from app.config import VISION_PROMPT_MAX
        r = self.client.put(self.url(), json={"prompt": "长" * (VISION_PROMPT_MAX + 1)})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(agents.vision_prompt("qq"), "")

    def test_sessions_payload_carries_both_prompt_fields(self):
        """编辑框靠 default 填初始内容，两个字段都得出现在列表接口里。"""
        d = self.client.get("/api/agent/qq/sessions").get_json()
        self.assertEqual(d["vision_prompt"], "")
        self.assertEqual(d["vision_prompt_default"], vision.default_prompt())


class ScopeTest(unittest.TestCase):
    """这个开关**只管通用读图**，另外两处识图各自写死，不许被带下水。"""

    def test_audit_prompt_still_has_its_own_key(self):
        self.assertNotEqual(agents._VISION_PROMPT_KEY, agents._AUDIT_PROMPT_KEY)

    def test_sticker_tagging_passes_its_own_prompt(self):
        """表情包打标签传的是自己那句，不受 vision_prompt 影响。"""
        seen = {}
        with mock.patch.object(vision, "describe",
                               lambda data_url, timeout=None, prompt=None:
                               seen.setdefault("p", prompt) or "猫瘫在桌上｜无语"), \
             mock.patch.object(agents, "vision_prompt",
                               return_value="跟我无关"):
            from app import stickers
            stickers._tag("data:1")
        self.assertIn("情绪或使用场景标签", seen["p"])
        self.assertNotIn("跟我无关", seen["p"])


if __name__ == "__main__":
    unittest.main()
