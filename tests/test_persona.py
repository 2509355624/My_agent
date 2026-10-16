# -*- coding: utf-8 -*-
"""人设卡池 + 扮演态测试：app/persona.py。

对应：
- 卡库读写（`agents/<agent>/characters.json`）
- 会话级扮演态（`state/persona_sessions.json`）
- 三条 `#` 命令
- 扮演一轮：**只调 1 次** LLM、按 `###` 切两路、最近 20 条 + 单条 200 字截断、
  每轮都开一张图

用户 2026-10-16 定的语义：
- 命令是 `#角色扮演`（`#人设列表` 同义）/ `#角色名` / `#出戏`，私聊同样要喊一声；
- 扮演态的作用域是**会话线**，A 群在演、B 群照旧是生图机器人；
- 系统头里**只有角色人设 + 输出契约 + 环境**，不拼工具目录、不拼审核话术。

隔离铁律：碰 agents/ 数据必须换整根 AGENTS_DIR 到临时目录（同 test_session_prompt）。
"""

import os
import tempfile
import unittest
from unittest import mock

import app.agents as agents
import app.persona as persona


def _card(name="测试角色", **kw):
    c = {"id": "", "name": name, "description": "", "avatar": "",
         "personality": "", "systemPrompt": "你是%s，说话很轻。" % name,
         "appearancePrompt": "1girl, white hair, red eyes", "outfitPrompt": "",
         "referenceImage": ""}
    c.update(kw)
    return c


class PersonaBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = os.path.join(self.tmp.name, "agents")
        os.makedirs(root, exist_ok=True)
        p = mock.patch.object(agents, "AGENTS_DIR", root)
        p.start()
        self.addCleanup(p.stop)
        q = mock.patch.object(
            persona, "_STATE_PATH",
            os.path.join(self.tmp.name, "state", "persona_sessions.json"))
        q.start()
        self.addCleanup(q.stop)
        agents.clear_cache()
        self.addCleanup(agents.clear_cache)


class CardStoreTest(PersonaBase):
    def test_roundtrip_and_missing_fields(self):
        persona.save_cards([{"name": "纱雾"}])       # 只给名字，其余该补齐
        got = persona.load_cards()
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["name"], "纱雾")
        self.assertTrue(got[0]["id"])                # id 是补出来的
        for k in ("systemPrompt", "appearancePrompt", "outfitPrompt",
                  "referenceImage", "description", "personality", "avatar"):
            self.assertIn(k, got[0])

    def test_blank_name_is_dropped(self):
        persona.save_cards([_card("有名字"), _card("  ")])
        self.assertEqual([c["name"] for c in persona.load_cards()], ["有名字"])

    def test_duplicate_ids_get_fresh_ones(self):
        persona.save_cards([_card("甲", id="same"), _card("乙", id="same")])
        ids = [c["id"] for c in persona.load_cards()]
        self.assertEqual(len(set(ids)), 2)

    def test_broken_file_reads_as_empty(self):
        persona.save_cards([_card("甲")])
        with open(persona._cards_path(), "w", encoding="utf-8") as f:
            f.write("{ 这不是 json")
        self.assertEqual(persona.load_cards(), [])   # 卡库坏了不该让机器人失灵

    def test_find_card_is_exact(self):
        persona.save_cards([_card("和泉纱雾")])
        self.assertIsNotNone(persona.find_card("和泉纱雾"))
        self.assertIsNotNone(persona.find_card("  和泉纱雾  "))
        # 认不出就认不出，**不做模糊匹配**——模糊匹配会把正常问句吃成命令
        self.assertIsNone(persona.find_card("纱雾"))
        self.assertIsNone(persona.find_card("和泉纱雾怎么画"))


class CommandTest(PersonaBase):
    def test_plain_text_is_not_a_command(self):
        persona.save_cards([_card("纱雾")])
        self.assertIsNone(persona.handle_command("group_1", "在吗"))
        self.assertIsNone(persona.handle_command("group_1", ""))
        self.assertIsNone(persona.handle_command("group_1", "#"))
        # `#` 开头但认不出卡名 → 放行，当普通消息
        self.assertIsNone(persona.handle_command("group_1", "#银发少女怎么画"))

    def test_list_enter_leave(self):
        persona.save_cards([_card("纱雾", description="害羞的插画师")])
        out = persona.handle_command("group_1", "#角色扮演")
        self.assertIn("纱雾", out)
        self.assertIn("害羞的插画师", out)
        self.assertIn("#出戏", out)
        # 别名同义
        self.assertIn("纱雾", persona.handle_command("group_1", "#人设列表"))

        out = persona.handle_command("group_1", "#纱雾")
        self.assertIn("纱雾", out)
        self.assertIsNotNone(persona.active_card("group_1"))

        out = persona.handle_command("group_1", "#出戏")
        self.assertIn("出戏", out)
        self.assertIsNone(persona.active_card("group_1"))

    def test_leave_when_not_playing_is_not_a_command(self):
        self.assertIsNone(persona.handle_command("group_1", "#出戏"))

    def test_scope_is_per_session_line(self):
        persona.save_cards([_card("纱雾")])
        persona.handle_command("group_1", "#纱雾")
        self.assertIsNotNone(persona.active_card("group_1"))
        self.assertIsNone(persona.active_card("group_2"))      # 别的群不受影响
        self.assertIsNone(persona.active_card("private_9"))

    def test_deleting_the_card_ends_the_roleplay(self):
        persona.save_cards([_card("纱雾")])
        persona.handle_command("group_1", "#纱雾")
        persona.save_cards([])
        # 卡没了就自动当没在演，不会卡在一个取不到的 id 上
        self.assertIsNone(persona.active_card("group_1"))


class SplitTest(unittest.TestCase):
    def test_splits_on_separator(self):
        self.assertEqual(persona.split_reply("你好\n###\n1girl, smile"),
                         ("你好", "1girl, smile"))

    def test_missing_separator_keeps_everything_as_reply(self):
        self.assertEqual(persona.split_reply("只有正文"), ("只有正文", ""))

    def test_empty(self):
        self.assertEqual(persona.split_reply(""), ("", ""))


class TrimTest(unittest.TestCase):
    def test_keeps_last_twenty(self):
        hist = [{"role": "user", "content": "m%d" % i} for i in range(30)]
        out = persona.trim(hist)
        self.assertEqual(len(out), persona.HISTORY_LIMIT)
        self.assertEqual(out[0]["content"], "m10")
        self.assertEqual(out[-1]["content"], "m29")

    def test_truncates_long_message(self):
        out = persona.trim([{"role": "user", "content": "长" * 900}])
        self.assertEqual(len(out[0]["content"]), persona.MSG_MAX_CHARS + 1)

    def test_drops_non_dialogue_roles(self):
        out = persona.trim([{"role": "system", "content": "头"},
                            {"role": "tool_result", "content": "回执"},
                            {"role": "user", "content": "hi"}])
        self.assertEqual([m["role"] for m in out], ["user"])


class HeadTest(PersonaBase):
    def test_head_has_persona_contract_env_and_nothing_else(self):
        head = persona.build_head(_card("纱雾"), "group_1", "张三")
        self.assertIn("说话很轻", head)        # 角色人设
        self.assertIn(persona.SEP, head)       # 输出契约
        self.assertIn("QQ 群", head)           # 群聊环境
        self.assertIn("张三", head)
        # 刻意不带的东西：工具协议、Skill 目录、审核话术（用户点名不要）
        self.assertNotIn("[[TOOL:", head)
        self.assertNotIn("Skill", head)
        self.assertNotIn("审核", head)

    def test_private_env_differs(self):
        head = persona.build_head(_card("纱雾"), "private_9", "")
        self.assertIn("私聊", head)

    def test_note_is_attached(self):
        head = persona.build_head(_card("纱雾"), "group_1", "张三",
                                  "他昨天让我画了只兔子。")
        self.assertIn("【之前的事】", head)
        self.assertIn("画了只兔子", head)


class TurnTest(PersonaBase):
    def setUp(self):
        super().setUp()
        persona.save_cards([_card("纱雾")])
        self.card = persona.find_card("纱雾")
        persona.switch("group_1", self.card)

    def _run(self, reply_text, text="在吗", sender="张三"):
        with mock.patch.object(persona.llm, "call_llm",
                               return_value=reply_text) as m, \
             mock.patch("app.comic_story.start") as draw:
            out = persona.run_turn("group_1", text, "group", "1", sender)
        return out, m, draw

    def test_one_call_splits_reply_and_starts_one_image(self):
        out, m, draw = self._run("你好呀\n###\n1girl, smiling")
        self.assertEqual(m.call_count, 1)              # 就一次调用
        self.assertEqual(out, "你好呀")                 # `###` 之后的不发给人看
        draw.assert_called_once()
        args, kw = draw.call_args
        self.assertEqual(args[0], "group")
        self.assertEqual(args[1], "1")
        self.assertEqual(args[2], "1girl, white hair, red eyes")   # 固定块＝卡里的外观
        self.assertEqual(args[3], ["1girl, smiling"])              # 动态块＝这轮画面
        self.assertEqual(kw.get("style"), "anima")

    def test_missing_separator_still_draws_one(self):
        out, m, draw = self._run("只有正文")
        self.assertEqual(out, "只有正文")
        draw.assert_called_once()
        # 忘写 `###` 也照出一张（用户定的是「每轮必出一张」）
        self.assertEqual(draw.call_args[0][3], [persona._FALLBACK_SCENE])

    def test_history_keeps_dialogue_only(self):
        self._run("你好呀\n###\n1girl, smiling")
        hist = persona._load_hist("group_1")
        self.assertEqual([m["role"] for m in hist], ["user", "assistant"])
        self.assertEqual(hist[0]["content"], "张三：在吗")
        self.assertEqual(hist[1]["content"], "你好呀")   # 画面标签不进历史
        self.assertNotIn("1girl, smiling", hist[1]["content"])

    def test_group_prefixes_sender_private_does_not(self):
        self._run("嗯", text="在吗", sender="张三")
        self.assertEqual(persona._load_hist("group_1")[0]["content"], "张三：在吗")
        persona.switch("private_9", self.card)
        with mock.patch.object(persona.llm, "call_llm", return_value="嗯"), \
             mock.patch("app.comic_story.start"):
            persona.run_turn("private_9", "在吗", "private", "9", "张三")
        self.assertEqual(persona._load_hist("private_9")[0]["content"], "在吗")

    def test_empty_reply_says_so_and_draws_nothing(self):
        """模型整轮没交作业：说一句「卡了一下」，**不配图**。

        连话都没说出来，再配一张「她的样子」发过去更莫名其妙。所以这条上
        「每轮必出一张」让位——它不算一轮正常的对话。
        """
        out, m, draw = self._run("")
        self.assertIn("卡", out)
        draw.assert_not_called()

    def test_not_in_roleplay_is_a_noop(self):
        persona.leave("group_1")
        with mock.patch.object(persona.llm, "call_llm") as m:
            self.assertEqual(persona.run_turn("group_1", "在吗", "group", "1"), "")
        m.assert_not_called()

    def test_history_is_separate_from_the_image_bot(self):
        self._run("你好呀")
        # 生图机器人的历史落在同一目录的另一份文件里，这边读到的只有对话
        self.assertFalse(os.path.exists(
            os.path.join(agents.AGENTS_DIR, "qq", "sessions", "group_1.jsonl")))
        self.assertTrue(os.path.exists(persona._hist_path("group_1")))


if __name__ == "__main__":
    unittest.main()
