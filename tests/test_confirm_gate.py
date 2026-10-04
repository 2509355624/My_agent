# -*- coding: utf-8 -*-
"""confirm_gate（生图二次确认闸）单测。

确认词判据来自真实语料（agents/qq/recent/archive 10-01~10-04 点名记录 +
私聊 sessions）：确认/好/行/可以了/对对对/嗯/OK/跑吧/继续吧/就这样/没问题/
你换吧/1，错别字「确实」；修改话（换成白丝/用qwen跑/三档）与否定疑问
（不可以/这样可以吗）绝不当确认。
"""
import contextlib
import unittest
from unittest import mock

from app import confirm_gate, image_jobs


@contextlib.contextmanager
def _fake_ctx(target="group", target_id="123"):
    """同时绑上下文与原话——intercept 用守卫家族判据：没有原话（None）放行。"""
    with mock.patch("app.qq_api.current_context",
                    return_value=(target, target_id)), \
         mock.patch("app.qq_api.current_turn_text",
                    return_value="大大怪跑这个"):
        yield


class IsConfirmTest(unittest.TestCase):
    def test_corpus_confirms_pass(self):
        for t in ("确认", "确认。", "好", "好吧", "行", "可以", "可以了",
                  "对对对", "嗯", "OK", "ok", "确实", "跑", "跑吧",
                  "继续吧", "就这样", "没问题", "你换吧", "1", "开跑",
                  "要", "👌"):
            self.assertTrue(confirm_gate.is_confirm(t), t)

    def test_modify_words_never_confirm(self):
        for t in ("换成白丝", "用qwen跑", "走三档", "再来一张", "重新跑",
                  "加台词", "不要袜子", "换个种子", "跑nai", "anime 跑吧",
                  "衣服少一点，继续生成"):
            self.assertFalse(confirm_gate.is_confirm(t), t)

    def test_refuse_and_questions_never_confirm(self):
        for t in ("不行", "不可以", "不好", "先不", "等等", "算了",
                  "这样可以吗", "能跑吗", "确认？不对吧"):
            self.assertFalse(confirm_gate.is_confirm(t), t)

    def test_loose_short_confirm(self):
        self.assertTrue(confirm_gate.is_confirm("可以了，跑吧"))
        self.assertTrue(confirm_gate.is_confirm("嗯，确认"))

    def test_long_chatter_not_confirm(self):
        self.assertFalse(confirm_gate.is_confirm("今天天气不错我们出去玩吧好累"))


class InterceptTest(unittest.TestCase):
    def setUp(self):
        confirm_gate._PENDING.clear()

    tearDown = setUp

    def test_qq_turn_intercepts_and_stores(self):
        sent = []
        with _fake_ctx(), \
             mock.patch("app.tools.normal.generate_image._send_receipt",
                        side_effect=lambda t, i, x: sent.append(x)):
            gate = confirm_gate.intercept(
                "comfy", skill="anima_clear", prompt="1girl", seed=42,
                intent="k", workflow={"w": 1}, note="")
        self.assertTrue(gate.startswith(image_jobs.RECEIPT_SENT_MARK))
        self.assertEqual(len(sent), 1)
        card = sent[0]
        self.assertIn("将画：anima_clear", card)
        self.assertIn("seed：42", card)
        self.assertIn("提示词：1girl", card)
        self.assertIn("回复「好」开跑", card)
        pend = confirm_gate._PENDING[("group", "123")]
        self.assertEqual(pend["kind"], "comfy")
        self.assertEqual(pend["workflow"], {"w": 1})
        self.assertEqual(pend["seed"], 42)

    def test_web_turn_passes_through(self):
        with mock.patch("app.qq_api.current_context",
                        return_value=(None, None)):
            gate = confirm_gate.intercept(
                "comfy", skill="anima_clear", prompt="1girl",
                workflow={"w": 1})
        self.assertIsNone(gate)
        self.assertEqual(confirm_gate._PENDING, {})

    def test_sessions_are_isolated(self):
        with _fake_ctx("group", "1"):
            confirm_gate.intercept("comfy", skill="a", prompt="p",
                                   workflow={})
        with _fake_ctx("private", "2"):
            confirm_gate.intercept("nai", skill="nai", prompt="p")
        self.assertEqual(len(confirm_gate._PENDING), 2)
        self.assertEqual(
            confirm_gate._PENDING[("private", "2")]["kind"], "nai")


class ConsumeTest(unittest.TestCase):
    def setUp(self):
        confirm_gate._PENDING.clear()
        self.allowed = mock.patch("app.agents.image_gen_allowed",
                                  return_value=(True, ""))
        self.allowed.start()
        self.addCleanup(self.allowed.stop)

    tearDown = setUp

    def _pending_comfy(self):
        confirm_gate._PENDING[("group", "123")] = {
            "kind": "comfy", "skill": "anima_clear", "prompt": "1girl",
            "seed": 42, "intent": "k", "workflow": {"w": 1}, "note": "",
        }

    def test_no_pending_is_noop(self):
        self.assertIsNone(
            confirm_gate.consume_if_confirmed("group", "123", "确认"))

    def test_non_confirm_discards_pending_and_passes(self):
        self._pending_comfy()
        self.assertIsNone(
            confirm_gate.consume_if_confirmed("group", "123", "换成白丝"))
        self.assertNotIn(("group", "123"), confirm_gate._PENDING)

    def test_confirm_enqueues_original_params(self):
        self._pending_comfy()
        job = mock.Mock()
        with mock.patch("app.image_jobs.comfy_alive", return_value=True), \
             mock.patch("app.image_jobs.enqueue",
                        return_value=(job, None)) as en, \
             mock.patch("app.tools.normal.generate_image._charge_quota",
                        return_value=""), \
             mock.patch("app.tools.normal.generate_image._qq_receipt",
                        return_value="回执") as rc:
            reply = confirm_gate.consume_if_confirmed("group", "123", "确认")
        self.assertEqual(reply, "回执")
        en.assert_called_once_with("group", "123", {"w": 1}, "anima_clear",
                                   prompt="1girl", intent="k", seed=42)
        rc.assert_called_once()
        self.assertNotIn(("group", "123"), confirm_gate._PENDING)

    def test_comfy_dead_on_confirm_returns_error(self):
        self._pending_comfy()
        with mock.patch("app.image_jobs.comfy_alive", return_value=False):
            reply = confirm_gate.consume_if_confirmed("group", "123", "好")
        self.assertIn("没在线", reply)

    def test_gen_disallowed_between_card_and_confirm(self):
        self._pending_comfy()
        with mock.patch("app.agents.image_gen_allowed",
                        return_value=(False, "生图被关了")):
            reply = confirm_gate.consume_if_confirmed("group", "123", "好")
        self.assertIn("生图被关了", reply)

    def test_nai_confirm_path(self):
        confirm_gate._PENDING[("group", "123")] = {
            "kind": "nai", "skill": "nai", "prompt": "1girl", "seed": None,
            "intent": "k2", "workflow": None,
            "nai_i2i": {"image": "b64", "strength": 0.7, "note": "垫图",
                        "width": 832, "height": 1216},
            "note": "",
        }
        job = mock.Mock()
        with mock.patch("app.agents.nai_allowed",
                        return_value=(True, "")), \
             mock.patch("app.image_jobs.enqueue",
                        return_value=(job, None)) as en, \
             mock.patch("app.tools.normal.generate_image._charge_quota",
                        return_value=""), \
             mock.patch("app.tools.normal.generate_image._qq_receipt",
                        return_value="回执") as rc:
            reply = confirm_gate.consume_if_confirmed("group", "123", "确认")
        self.assertEqual(reply, "回执")
        args, kwargs = en.call_args
        self.assertEqual(args[2], "1girl")
        self.assertEqual(kwargs["skill"], "nai")
        self.assertEqual(kwargs["nai_i2i"]["image"], "b64")
        rc.assert_called_once()


if __name__ == "__main__":
    unittest.main()
