# -*- coding: utf-8 -*-
"""酒馆分层写法（app/writing_style.py）与接入链路的回归测试。

覆盖四件事：
1. 纯函数档位归一化 / 卡面速写 / 示例分块 / 贴脸尾注；
2. `build_head` 的三层落点（卡层常驻、档位可控、示例位置不变）；
3. `run_turn` 不传 writing 时不拼档位、尾注只给一句正向锚点，
   传了才带上轮次和「这一幕」；
4. `persona_api` 把 `writing` 一路透传到 `run_turn`。

不调用任何真实 LLM；`run_turn` 那组用隔离的临时 agents/state 目录。
"""

import os
import tempfile
import unittest
from unittest import mock

from flask import Flask, jsonify

import app.agents as agents
import app.persona as persona
import app.persona_api as persona_api
from app import writing_style


def _card(**kw):
    c = {
        "id": "t", "name": "测试角色", "description": "《某作品》里的人",
        "avatar": "", "personality": "羞涩、认真",
        "systemPrompt": "你是测试角色，说话很轻。",
        "exampleDialogue": "", "appearancePrompt": "", "outfitPrompt": "",
        "room": "", "referenceImage": "", "stateBehavior": {},
    }
    c.update(kw)
    return c


class StyleUnitTest(unittest.TestCase):
    def test_default_mode_has_no_line(self):
        self.assertEqual(writing_style.mode_line("normal"), "")
        self.assertEqual(writing_style.head_style("normal"), "")

    def test_all_modes_are_positive(self):
        """正向口径：档位句里不出现「不能 / 不许 / 不要 / 禁止 / 必须」。"""
        for key, item in writing_style.MODES.items():
            with self.subTest(mode=key):
                for bad in ("不能", "不许", "不要", "禁止", "必须", "Never", "Do not"):
                    self.assertNotIn(bad, item["line"])

    def test_norm_missing_and_bad_mode(self):
        self.assertEqual(writing_style.norm(None), ("normal", ""))
        self.assertEqual(writing_style.norm("slow"), ("normal", ""))
        self.assertEqual(writing_style.norm({"mode": "NOT_A_MODE"}), ("normal", ""))

    def test_norm_keeps_note_and_flattens_whitespace(self):
        mode, note = writing_style.norm(
            {"mode": "SLOW", "note": "  慢一点\n换个  物件  "})
        self.assertEqual(mode, "slow")
        self.assertEqual(note, "慢一点 换个 物件")

    def test_norm_caps_note(self):
        _, note = writing_style.norm({"note": "啊" * 500})
        self.assertEqual(len(note), writing_style.NOTE_MAX_CHARS)

    def test_style_list_shape(self):
        styles = writing_style.style_list()
        self.assertTrue(styles)
        self.assertEqual(styles[0]["key"], "normal")
        for s in styles:
            self.assertIn("label", s)
            self.assertIn("line", s)

    def test_card_sketch_uses_existing_fields(self):
        sketch = writing_style.card_sketch(_card())
        self.assertIn("《某作品》里的人", sketch)
        self.assertIn("羞涩、认真", sketch)
        self.assertTrue(sketch.startswith("## 卡面速写"))

    def test_card_sketch_skips_duplicated_text(self):
        card = _card(systemPrompt="《某作品》里的人，羞涩、认真。",
                     description="《某作品》里的人", personality="羞涩、认真")
        self.assertEqual(writing_style.card_sketch(card), "")

    def test_card_sketch_handles_empty(self):
        self.assertEqual(writing_style.card_sketch(None), "")
        self.assertEqual(writing_style.card_sketch({}), "")

    def test_format_examples_single_block_is_untouched(self):
        """存量 21 张卡只有一块，必须一个字都不改。"""
        src = "你：在吗？\n\n她抬起头。"
        self.assertEqual(writing_style.format_examples(src), src)

    def test_format_examples_splits_star_and_start(self):
        for sep in ("***", "<START>"):
            with self.subTest(sep=sep):
                out = writing_style.format_examples("第一段" + sep + "第二段")
                self.assertIn("【示范 1】", out)
                self.assertIn("【示范 2】", out)
                self.assertIn("第一段", out)
                self.assertIn("第二段", out)

    def test_format_examples_empty(self):
        self.assertEqual(writing_style.format_examples(""), "")

    def test_tail_note_is_positive_and_factual(self):
        out = writing_style.tail_note(12, "让雨声多待一会儿")
        self.assertIn("第 12 轮", out)
        self.assertIn("让雨声多待一会儿", out)
        self.assertIn("镜头可以跟着她", out)
        for bad in ("不能", "不许", "不要", "禁止", "必须"):
            self.assertNotIn(bad, out)

    def test_tail_note_sanitizes_turn_no(self):
        self.assertIn("第 1 轮", writing_style.tail_note("oops"))
        self.assertIn("第 1 轮", writing_style.tail_note(-3))

    def test_tail_default_is_positive_anchor(self):
        """常态也贴一句正向锚点：正文用她的名字，玩家是「你」。"""
        out = writing_style.tail()
        self.assertIn("她有名字", out)
        self.assertIn("玩家是「你」", out)
        self.assertIn("镜头可以跟着她", out)
        self.assertNotIn("第", out)
        for bad in ("不能", "不许", "不要", "禁止", "必须"):
            self.assertNotIn(bad, out)

    def test_tail_anchor_carries_card_name(self):
        """给了卡名：人称锚点直接写名字，压住历史里通篇「她」的示范。"""
        out = writing_style.tail(name="学妹")
        self.assertIn("正文用「学妹」叫她", out)
        self.assertNotIn("她有名字", out)   # 通用句换成带名字版，不重复
        self.assertIn("镜头可以跟着她", out)
        # 没名字 / 卡名就叫「她」：退回通用句
        self.assertIn("她有名字", writing_style.tail(name=""))
        self.assertIn("她有名字", writing_style.tail(name="她"))

    def test_tail_with_turn_and_note(self):
        out = writing_style.tail(12, "让雨声多待一会儿")
        self.assertIn("第 12 轮", out)
        self.assertIn("让雨声多待一会儿", out)
        self.assertIn("镜头可以跟着她", out)

    def test_tail_note_delegates_to_tail(self):
        self.assertEqual(writing_style.tail_note(12, "雨"),
                         writing_style.tail(12, "雨"))

    def test_tail_default_carries_length_anchor(self):
        """篇幅锚点在近因位：300~600，停在哪都行，不收作文尾巴。"""
        out = writing_style.tail()
        self.assertIn("300~600 字", out)
        self.assertIn("停", out)
        self.assertNotIn("收得住", out)
        for bad in ("不能", "不许", "不要", "禁止", "必须"):
            self.assertNotIn(bad, out)

    def test_tail_plot_is_last_and_positive(self):
        out = writing_style.tail(plot="让她主动一点")
        self.assertIn("让她主动一点", out)
        self.assertTrue(out.rstrip("）").endswith("让她主动一点"))
        self.assertIn("真的发生在画面里", out)
        self.assertLess(out.index("镜头可以跟着她"),
                        out.index("让她主动一点"))

    def test_tail_without_plot_has_no_direction(self):
        self.assertNotIn("这一拍往这个方向走", writing_style.tail())

    def test_tail_plot_is_normalized_and_capped(self):
        out = writing_style.tail(plot="  换行\n 压成一行  ")
        self.assertNotIn("\n", out)
        self.assertIn("换行 压成一行", out)
        self.assertLessEqual(len(writing_style.tail(plot="方" * 400)), 300)


class BuildHeadTest(unittest.TestCase):
    def _head(self, **kw):
        return persona.build_head(_card(**kw), "private_1", "张三")

    def test_card_layer_is_always_on(self):
        head = self._head()
        self.assertIn("## 卡面速写", head)
        self.assertIn("《某作品》里的人", head)

    def test_default_mode_has_no_style_block(self):
        head = self._head()
        self.assertNotIn("## 这一局的写法", head)

    def test_mode_line_is_in_head(self):
        head = persona.build_head(_card(), "private_1", "张三", mode="slow")
        self.assertIn("## 这一局的写法", head)
        self.assertIn(writing_style.MODES["slow"]["line"], head)

    def test_example_position_is_unchanged(self):
        """示例仍夹在「人设」和「输出格式」之间，不能跑到契约后面。"""
        head = persona.build_head(
            _card(exampleDialogue="第一段\n\n***\n\n第二段"),
            "private_1", "张三", mode="brisk")
        i_persona = head.find("你是测试角色")
        i_ex = head.find("【示范 1】")
        i_contract = head.find("## 输出格式")
        i_style = head.find("## 这一局的写法")
        self.assertLess(i_persona, i_ex)
        self.assertLess(i_ex, i_contract)
        self.assertLess(i_contract, i_style)

    def test_single_block_example_has_no_numbering(self):
        head = self._head(exampleDialogue="你：在吗？\n\n她抬起头。")
        self.assertIn("她抬起头。", head)
        self.assertNotIn("【示范 1】", head)


class RunTurnWritingTest(unittest.TestCase):
    KEY = "private_1"

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
        persona.save_cards([_card()])
        persona.switch_line(self.KEY, persona.find_card("测试角色"))

    def _capture(self, writing="absent"):
        kw = {} if writing == "absent" else {"writing": writing}
        with mock.patch.object(persona, "_llm_call", return_value="你好呀") as m:
            persona.run_turn(self.KEY, "在吗", "private", "张三", **kw)
        # 最后一次调用才是主回复那段（意图判断可能也走 _llm_call）
        return m.call_args_list[-1][0][0]

    def test_no_writing_keeps_tail_clean(self):
        msgs = self._capture()
        self.assertNotIn("## 这一局的写法", msgs[0]["content"])
        self.assertEqual(msgs[-1]["role"], "user")
        self.assertNotIn("（第", msgs[-1]["content"])
        # 2026-11-07：常态也贴统一正向锚点，但不带轮次和调子，也不写禁令。
        self.assertIn("镜头可以跟着她", msgs[-1]["content"])
        self.assertNotIn("不要用「他」", msgs[-1]["content"])

    def test_aux_paths_share_the_same_tail(self):
        """不走 run_turn 的收尾链路也贴同一句锚点。"""
        with mock.patch.object(
                persona, "_llm_call",
                return_value="你好呀\n画面：坐着\n心情：平静 强度1\n状态：坐着") as m:
            persona._gen_and_save(self.KEY, {}, _card(), "head", "收尾任务")
        tail = m.call_args[0][0][-1]["content"]
        self.assertIn("收尾任务", tail)
        self.assertIn("镜头可以跟着她", tail)
        for bad in ("不要", "不许", "禁止"):
            self.assertNotIn(bad, tail)

    def test_writing_adds_head_block_and_tail(self):
        msgs = self._capture({"mode": "sensory", "note": "让雨声多待一会儿"})
        self.assertIn("## 这一局的写法", msgs[0]["content"])
        self.assertIn(writing_style.MODES["sensory"]["line"], msgs[0]["content"])
        tail = msgs[-1]["content"]
        self.assertIn("（第", tail)
        self.assertIn("让雨声多待一会儿", tail)
        self.assertIn("镜头可以跟着她", tail)

    def test_note_alone_enables_tail_without_style_block(self):
        msgs = self._capture({"mode": "NOT_A_MODE", "note": "慢一点"})
        self.assertNotIn("## 这一局的写法", msgs[0]["content"])
        self.assertIn("慢一点", msgs[-1]["content"])

    def test_message_roles_are_unchanged(self):
        """不插中途 system：消息序列仍是 system + user/assistant 交替。"""
        msgs = self._capture({"mode": "slow"})
        self.assertEqual(msgs[0]["role"], "system")
        self.assertEqual(msgs[-1]["role"], "user")
        self.assertEqual([m["role"] for m in msgs[1:]].count("system"), 0)


class ApiWritingTest(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.register_blueprint(persona_api.bp)

    def test_chat_forwards_writing(self):
        with self.app.test_request_context(
                "/api/persona/chat", method="POST",
                json={"text": "在吗", "writing": {"mode": "slow", "note": "雨"}}):
            with mock.patch.object(persona_api, "_chat_turn",
                                   return_value=jsonify({"ok": True})) as m:
                persona_api.chat()
        self.assertEqual(m.call_args[0][2], {"mode": "slow", "note": "雨"})

    def test_chat_without_writing_passes_none(self):
        with self.app.test_request_context(
                "/api/persona/chat", method="POST", json={"text": "在吗"}):
            with mock.patch.object(persona_api, "_chat_turn",
                                   return_value=jsonify({"ok": True})) as m:
                persona_api.chat()
        self.assertIsNone(m.call_args[0][2])

    def test_member_turn_receives_writing(self):
        fake = mock.MagicMock()
        t = fake.run_turn.return_value
        t.reply, t.scene, t.mood, t.intensity, t.state = "你好", "", "平静", 1, ""
        t.aff, t.finish = None, ""
        fake.active_card.return_value = {"name": "测试角色"}
        fake.archive_len.return_value = 0
        fake.game_day.return_value, fake.game_phase.return_value = 1, "中午"
        fake.pregnancy_status.return_value, fake.turn_total.return_value = {}, 0
        w = {"mode": "quiet", "note": ""}
        with self.app.test_request_context("/api/persona/chat", method="POST"), \
             mock.patch.object(persona_api, "persona", fake), \
             mock.patch.object(persona_api, "_who",
                               return_value=("member", "123", None)), \
             mock.patch.object(persona_api.usage, "scope"):
            persona_api._chat_turn("在吗", None, w)
        self.assertIs(fake.run_turn.call_args.kwargs["writing"], w)

    def test_guest_turn_receives_writing(self):
        w = {"mode": "brisk", "note": ""}
        with mock.patch.object(persona_api, "_who",
                               return_value=("guest", "g1", "tok")), \
             mock.patch.object(persona_api, "_guest_chat",
                               return_value=mock.MagicMock()) as m:
            persona_api._chat_turn("在吗", None, w)
        self.assertIs(m.call_args[0][4], w)

    def test_styles_endpoint(self):
        with self.app.test_client() as c:
            r = c.get("/api/persona/writing_styles")
        self.assertEqual(r.status_code, 200)
        d = r.get_json()
        self.assertTrue(d["ok"])
        self.assertEqual(d["default"], "normal")
        self.assertEqual(d["styles"], writing_style.style_list())


if __name__ == "__main__":
    unittest.main()
