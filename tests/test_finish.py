# -*- coding: utf-8 -*-
"""射精选择 + 第五次之后的两条出口（2026-11-05 用户拍板）。

用户原话：「玩家不需要自己去写，他只需要负责推动剧情就行了。女主来到高潮
之后，就给选项让玩家自己选，这样可以避免玩家不知道什么时候去写这个摄入
会比较好——相当于系统帮玩家去做这个决定。」

要钉住的四件事：
1. 选择存得进、取得出、取一次就清（不能后面每轮都在射）；
2. 位置决定「有没有射在里面」——怀孕只认这个，不能靠编剧猜；
3. 「身上」的落点必须跟着姿势走，不能后入却射在小腹；
4. 第五次不再自动跨天，改成 `finish_night` 生成。
"""

import os
import tempfile
import unittest
from unittest import mock

import app.agents as agents
import app.persona as persona


def _card(name="测试角色", **kw):
    c = {"id": "", "name": name, "description": "", "avatar": "",
         "personality": "", "systemPrompt": "你是%s。" % name,
         "appearancePrompt": "1girl", "outfitPrompt": "",
         "referenceImage": ""}
    c.update(kw)
    return c


class FinishBase(unittest.TestCase):
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
        # 这一份用例钉的是 v1 的「射在哪里 / 跨天」流程；2026-11-08 起新线默认
        # 走 v2（schema=2），v2 有自己的一套高潮与怀孕路径（见 test_affection_v2）。
        v = mock.patch.object(persona.affection, "V2_AUTO_NEW", False)
        v.start()
        self.addCleanup(v.stop)
        persona.save_cards([_card()])
        persona.switch_line("private_1", persona.find_card("测试角色"))
        self.key = persona.resolve_line("private_1")


class FinishPickTest(FinishBase):
    def test_set_and_take(self):
        self.assertTrue(persona.set_finish(self.key, "pussy"))
        self.assertEqual(persona.take_finish(self.key), "pussy")

    def test_taken_only_once(self):
        """取一次就清——不然后面每一轮都在射。"""
        persona.set_finish(self.key, "mouth")
        self.assertEqual(persona.take_finish(self.key), "mouth")
        self.assertEqual(persona.take_finish(self.key), "")

    def test_unknown_spot_is_rejected(self):
        self.assertFalse(persona.set_finish(self.key, "somewhere"))
        self.assertEqual(persona.take_finish(self.key), "")

    def test_all_five_spots_are_accepted(self):
        for spot in ("mouth", "body", "pussy", "anus", "none"):
            with self.subTest(spot=spot):
                self.assertTrue(persona.set_finish(self.key, spot))
                self.assertEqual(persona.take_finish(self.key), spot)

    def test_no_pick_returns_empty(self):
        self.assertEqual(persona.take_finish(self.key), "")


class FinishBlockTest(unittest.TestCase):
    def test_each_spot_has_its_own_block(self):
        texts = {}
        for spot in ("mouth", "body", "pussy", "anus", "none"):
            got = persona.finish_block(spot)
            self.assertTrue(got, spot)
            texts[spot] = got
        self.assertEqual(len(set(texts.values())), 5, "五个位置不能共用一套话")

    def test_body_spot_must_follow_the_posture(self):
        """「身上」最容易穿帮：后入不能射在小腹正面。"""
        got = persona.finish_block("body")
        self.assertIn("姿势", got)
        self.assertIn("后", got)          # 提到背后进入/后入

    def test_inside_spots_say_it_explicitly(self):
        for spot, word in (("pussy", "射在里面"), ("anus", "射在肛门里")):
            with self.subTest(spot=spot):
                self.assertIn(word, persona.finish_block(spot))

    def test_unknown_spot_is_silent(self):
        self.assertEqual(persona.finish_block(""), "")
        self.assertEqual(persona.finish_block("xx"), "")


class ClimaxInfoTest(unittest.TestCase):
    def test_no_orgasm_means_no_popup(self):
        self.assertIsNone(persona._climax_info({"just_orgasm": 0}))
        self.assertIsNone(persona._climax_info(None))

    def test_ordinary_orgasm(self):
        got = persona._climax_info({"just_orgasm": 2, "orgasmMax": 5})
        self.assertEqual(got["n"], 2)
        self.assertFalse(got["last"])

    def test_fifth_is_marked_last(self):
        got = persona._climax_info({"just_orgasm": 5, "orgasmMax": 5})
        self.assertTrue(got["last"])


class FinishAffectsPregnancyTest(FinishBase):
    """选在「小穴里/肛门里」＝内射，怀孕记账只认这个。"""

    def _run(self, spot, finish_from_judge="outside"):
        score = {"finish": finish_from_judge, "act": "intense", "excite": 30,
                 "delta": 1}
        aff = {"active": True, "excite": 95, "orgasms": 1, "orgasmMax": 5,
               "just_orgasm": 1, "value": 50, "corrupt": 0, "submit": 0,
               "arousal": 50, "dev": {}, "devNames": {}}
        raw = "正文。\n画面：1girl\n心情：累 强度1\n状态：床上"
        persona.set_finish(self.key, spot)
        with mock.patch.object(persona, "_judge_intent_scored",
                               return_value=("intimate", "", score)), \
             mock.patch.object(persona.affection, "settle", return_value=aff), \
             mock.patch.object(persona.llm, "call_llm", return_value=raw), \
             mock.patch.object(persona, "_preg_record") as rec:
            out = persona.run_turn(self.key, "（继续）", "private", "张三")
        self.assertTrue(out.reply)
        return rec

    def test_inside_spot_forces_inside(self):
        rec = self._run("pussy")
        self.assertTrue(rec.called, "选在小穴里就该记一次内射")
        self.assertEqual(rec.call_args.args[3], "inside")

    def test_anus_counts_as_inside(self):
        rec = self._run("anus")
        self.assertTrue(rec.called)
        self.assertEqual(rec.call_args.args[3], "inside")

    def test_body_spot_is_not_inside(self):
        rec = self._run("body")
        # 编剧判的是 outside，选择也是 outside → 不记
        self.assertFalse(rec.called)

    def test_pick_can_override_the_judge(self):
        """编剧没判出来（unknown），但玩家选了小穴里 → 仍按内射记。"""
        rec = self._run("pussy", finish_from_judge="unknown")
        self.assertTrue(rec.called)
        self.assertEqual(rec.call_args.args[3], "inside")

    def test_no_pick_uses_the_judge(self):
        score = {"finish": "inside", "act": "intense", "excite": 30, "delta": 1}
        aff = {"active": True, "excite": 95, "orgasms": 1, "orgasmMax": 5,
               "just_orgasm": 1, "value": 50, "corrupt": 0, "submit": 0,
               "arousal": 50, "dev": {}, "devNames": {}}
        raw = "正文。\n画面：1girl\n心情：累 强度1\n状态：床上"
        with mock.patch.object(persona, "_judge_intent_scored",
                               return_value=("intimate", "", score)), \
             mock.patch.object(persona.affection, "settle", return_value=aff), \
             mock.patch.object(persona.llm, "call_llm", return_value=raw), \
             mock.patch.object(persona, "_preg_record") as rec:
            persona.run_turn(self.key, "（继续）", "private", "张三")
        self.assertTrue(rec.called)
        self.assertEqual(rec.call_args.args[3], "inside")


class FinishActionTest(FinishBase):
    """前 4 次：选完**立刻生成**，不用玩家再发一句话（2026-11-05 晚改）。"""

    def _run(self, spot="", text="",
             reply="他射了。\n画面：1girl\n心情：空 强度1\n状态：床上"):
        with mock.patch.object(persona.llm, "call_llm", return_value=reply) as m:
            out = persona.finish_action(self.key, spot=spot, text=text)
        return out, m

    def test_generates_a_reply(self):
        out, m = self._run("pussy")
        self.assertIsNotNone(out)
        self.assertTrue(out.reply)
        head = m.call_args.args[0][0]["content"]
        self.assertIn("这一拍：射精", head)

    def test_inside_spot_records_pregnancy(self):
        with mock.patch.object(persona, "_preg_record") as rec:
            self._run("anus")
        self.assertTrue(rec.called)
        self.assertEqual(rec.call_args.args[3], "inside")

    def test_outside_spot_does_not_record(self):
        with mock.patch.object(persona, "_preg_record") as rec:
            self._run("body")
        self.assertFalse(rec.called)

    def test_custom_text_wins_over_the_spot(self):
        out, m = self._run(spot="body", text="他没射，只是抵着她没动")
        head = m.call_args.args[0][0]["content"]
        self.assertIn("他没射，只是抵着她没动", head)
        # 自定义优先：不该再塞预设位置那套
        self.assertNotIn("落点要按你们这一轮的姿势定", head)

    def test_custom_text_can_mean_no_ejaculation(self):
        """「不射」不再是一个按钮，而是玩家自己写的一句话。"""
        with mock.patch.object(persona, "_preg_record") as rec:
            self._run(spot="", text="他没射，停在里面没动")
        self.assertFalse(rec.called, "没射就不该记内射")

    def test_nothing_chosen_means_skip(self):
        self.assertIsNone(persona.finish_action(self.key))

    def test_llm_failure_returns_none(self):
        with mock.patch.object(persona.llm, "call_llm",
                               side_effect=RuntimeError("炸了")):
            self.assertIsNone(persona.finish_action(self.key, spot="mouth"))


class PendingFinishTest(FinishBase):
    """「射在哪里」的待办（2026-11-06）。

    高潮值在**普通一轮**里就扣了，弹窗是之后才弹的。玩家在点确定之前刷新、
    断网、关页面，弹窗就没了——数值扣过、正文永远缺一块。所以待办要落盘，
    刷新后能重问；只有真正写成功才清。
    """

    def _run(self, reply="他射了。\n画面：1girl\n心情：空 强度1\n状态：床上"):
        with mock.patch.object(persona.llm, "call_llm", return_value=reply):
            return persona.finish_action(self.key, spot="pussy")

    def test_pending_round_trips(self):
        persona._set_pending_finish(self.key, {"n": 2, "of": 5, "last": False})
        got = persona.pending_finish(self.key)
        self.assertEqual(got, {"n": 2, "of": 5, "last": False})
        persona._set_pending_finish(self.key, None)
        self.assertIsNone(persona.pending_finish(self.key))

    def test_failure_keeps_the_popup(self):
        persona._set_pending_finish(self.key, {"n": 1, "of": 5, "last": False})
        with mock.patch.object(persona.llm, "call_llm",
                               side_effect=RuntimeError("断网")):
            self.assertIsNone(persona.finish_action(self.key, spot="pussy"))
        self.assertIsNotNone(persona.pending_finish(self.key),
                             "没写成就不能清待办，否则玩家再也补不回来")

    def test_success_clears_the_popup(self):
        persona._set_pending_finish(self.key, {"n": 1, "of": 5, "last": False})
        self.assertIsNotNone(self._run())
        self.assertIsNone(persona.pending_finish(self.key))

    def test_skip_clears_it_too(self):
        persona._set_pending_finish(self.key, {"n": 1, "of": 5, "last": False})
        persona._set_pending_finish(self.key, None)
        self.assertIsNone(persona.pending_finish(self.key))


class FinishNightTest(FinishBase):
    """第五次之后：结束 / 最后一次。"""

    def _run(self, last, reply="她睡着了。\n画面：1girl\n心情：空 强度1\n状态：床上"):
        with mock.patch.object(persona.llm, "call_llm", return_value=reply) as m:
            out = persona.finish_night(self.key, last=last)
        return out, m

    def _run_next(self, reply="天亮了。\n画面：1girl\n心情：静 强度1\n状态：床边"):
        with mock.patch.object(persona.llm, "call_llm", return_value=reply) as m:
            out = persona.next_morning(self.key)
        return out, m

    def test_last_writes_the_night_only(self):
        """「最后一次」这一步**只写夜里**，不跨天（2026-11-05 晚改）。

        跨天拆成单独一次请求（`next_morning`），前端好在两次之间浮出
        「第二天」那个标识——合成一次就没地方插了。
        """
        persona.set_finish(self.key, "pussy")
        out, m = self._run(True)
        self.assertTrue(out.reply)
        self.assertEqual(persona.game_day(self.key), 1,
                         "夜里那一下不该顺手把天跨了")
        self.assertEqual(persona.take_finish(self.key), "")
        head = m.call_args.args[0][0]["content"]
        self.assertIn("射精", head)

    def test_next_morning_advances_and_writes(self):
        out, m = self._run_next()
        self.assertTrue(out.reply)
        self.assertEqual(persona.game_day(self.key), 2)
        head = m.call_args.args[0][0]["content"]
        self.assertIn("第 2 天", head)      # 跨天后的时间块要进系统头
        self.assertIn("早晨", head)

    def test_last_then_morning_end_to_end(self):
        """两段接连跑：夜里 → 第二天，天数是 2，场景已收。"""
        self._run(True)
        out, _ = self._run_next()
        self.assertTrue(out.reply)
        self.assertEqual(persona.game_day(self.key), 2)
        st = persona.affection.status(self.key)
        self.assertFalse(st["inScene"])
        self.assertEqual(st["excite"], 0)

    def test_end_stays_in_the_same_night(self):
        out, _m = self._run(False)
        self.assertTrue(out.reply)
        self.assertEqual(persona.game_day(self.key), 1,
                         "选了「就到这里」不该跨天")

    def test_scene_is_closed_after_last(self):
        self._run(True)
        st = persona.affection.status(self.key)
        self.assertFalse(st["inScene"], "跨天了就不该还挂着这一场")
        self.assertEqual(st["excite"], 0)

    def test_llm_failure_returns_none(self):
        with mock.patch.object(persona.llm, "call_llm",
                               side_effect=RuntimeError("炸了")):
            self.assertIsNone(persona.finish_night(self.key, last=True))


if __name__ == "__main__":
    unittest.main()
