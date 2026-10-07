"""生图提交回执：由**后台直接发**，不经过模型那张嘴（2026-10-04 用户拍板）。

用户现场：「AI 提交任务之后直接发一个回执，就说任务已经提交、前面还有 XX 在
排队，这个是直接发的、不是经过 AI——它老是瞎编东西，我要的是最直接的来自后台
的回执。」

链路三段，缺一段就退回老样子（模型转述，于是编张数）：

    1. `generate_image._qq_receipt` 提交成功后自己 `_send_text` 发进会话，并把
       那句人话放进返回值（整段以 `image_jobs.RECEIPT_SENT_MARK` 开头）；
    2. `agent._queued_note_for_user` 认这个标记——它是「提交成功」的判据，
       「一轮一张」的硬闸和掐断循环都靠它；
    3. `qq_bot` 看到标记就**丢弃本轮模型正文**，只按「机器人开过口」记冷却与
       群背景。

发不出去（适配层掉线 / 私聊非好友）时第 1 段退回老文案让模型转述——
**绝不谎报「已提交」**，那正是这次要修的毛病。
"""

import unittest
from unittest import mock

from app import agent as agent_mod
from app import agents as agent_store
from app import image_jobs, qq_api, qq_bot
from app.tools.normal import generate_image as gi

MARK = image_jobs.RECEIPT_SENT_MARK


def _entry():
    return {"outputs": {"9": {"images": [{"filename": "a.png"}]}}}


class ReceiptLineTest(unittest.TestCase):
    """标记的解析只有一处（image_jobs.receipt_line），别在别处重写格式。"""

    def test_reads_the_line_that_went_out(self):
        text = MARK + "任务已提交，前面还有 2 张在排队。\n（写给模型的叮嘱…）"
        self.assertEqual(image_jobs.receipt_line(text),
                         "任务已提交，前面还有 2 张在排队。")

    def test_legacy_and_error_give_empty(self):
        for text in ("已经排上队了（前面还有 1 张），排到就画。",
                     "错误：ComfyUI 现在没在线", "", None):
            self.assertEqual(image_jobs.receipt_line(text), "")


class _SubmitHarness(unittest.TestCase):
    """跑 generate_image 本体，在 QQ 侧提交那一刻看它到底发了什么。"""

    def setUp(self):
        image_jobs._reset()
        self.addCleanup(image_jobs._reset)
        self.sent = []
        for target, repl in (
            ("comfy_alive", lambda *a, **k: True),
            ("_ensure_worker", lambda: None),
            ("_queue_prompt", lambda wf: "pid"),
            ("_free_vram_gb", lambda: None),
            ("_send_text", lambda target, target_id, text:
                self.sent.append((target, target_id, text))),
        ):
            p = mock.patch.object(image_jobs, target, repl)
            p.start()
            self.addCleanup(p.stop)
        for target, repl in (
            ("load_skill", mock.Mock(return_value={"workflow": {"1": {}}})),
            ("_qq_gate", mock.Mock(return_value=None)),
            ("is_cancelled", mock.Mock(return_value=False)),
        ):
            p = mock.patch.object(gi, target, repl)
            p.start()
            self.addCleanup(p.stop)

    def _submit(self, ctx, prompt="a cat"):
        with mock.patch.object(qq_api, "current_context", return_value=ctx):
            return gi.tool["function"](prompt)


class DirectReceiptTest(_SubmitHarness):
    def test_qq_receipt_is_sent_by_the_backend(self):
        out = self._submit(("group", "9"))
        self.assertEqual(len(self.sent), 1)
        target, tid, text = self.sent[0]
        self.assertEqual((target, tid), ("group", "9"))
        # 第一行永远是那句状态（`receipt_line` 就取它，掐断循环与「本轮别
        # 再复述」都靠这一行对齐）
        self.assertEqual(text.splitlines()[0], "任务已提交，正在画了。")
        # 2026-10-06 用户：「提交任务的时候，可以说明生图的渠道是什么」
        self.assertIn("当前渠道：silver", text)
        self.assertTrue(out.startswith(MARK))
        # 给模型那段里，第一行就是发出去的那句（qq_bot 靠 receipt_line 取它）
        self.assertEqual(image_jobs.receipt_line(out), "任务已提交，正在画了。")

    def test_queued_receipt_carries_the_backend_count(self):
        """张数是后台 `ahead_of` 现算的，不是模型猜的。"""
        with mock.patch.object(qq_api, "current_context",
                               return_value=("group", "9")):
            gi.tool["function"]("a cat")          # 先占住队首
            out = gi.tool["function"]("a dog")    # 这一张排在后面
        self.assertEqual(self.sent[-1][2].splitlines()[0],
                         "任务已提交，前面还有 1 张在排队。")
        self.assertTrue(out.startswith(MARK))

    def test_private_chat_goes_to_the_private_session(self):
        self._submit(("private", "42"))
        self.assertEqual(len(self.sent), 1)
        target, tid, text = self.sent[0]
        self.assertEqual((target, tid), ("private", "42"))
        self.assertEqual(text.splitlines()[0], "任务已提交，正在画了。")

    def test_web_side_sends_no_receipt(self):
        """网页端本来就要同步等到出图，没有这条回执（也发不出去）。"""
        with mock.patch.object(image_jobs.Job, "wait",
                               lambda self: _entry()):
            out = self._submit((None, None))
        self.assertEqual(self.sent, [])
        self.assertIn("生成成功", out)


class ReceiptNoteTest(_SubmitHarness):
    """附言那段的三态（2026-10-06 用户：「那些字我就希望我可以自己去附加」）。

    没设 = 内置默认；设成空串 = 这段不要；其余 = 用户自己写的那份。
    """

    def _note(self, stored):
        with mock.patch.object(agent_store, "receipt_note",
                               return_value=stored):
            return gi._receipt_text(0, "silver")

    def test_unset_falls_back_to_the_builtin_default(self):
        text = self._note(None)
        self.assertIn("渠道名 + 你的需求", text)
        self.assertIn("更多渠道", text)

    def test_default_note_mentions_the_4x_channel(self):
        """回执附言给 silver-hd 一行曝光（2026-10-07 用户拍板）——它是点名渠道，
        单独占一行讲用法，不混进渠道清单里。"""
        text = self._note(None)
        self.assertIn("silver-hd", text)

    def test_default_note_mentions_landscape(self):
        """回执给横屏一行曝光（2026-10-07 用户拍板）。

        横屏的判据是**本轮原话**里的词（`image_jobs.turn_is_landscape` 认
        「横屏 / 横版 / 横图」），不写出来用户根本不知道有这功能。
        只写「横屏」一个词——另外两个是代码顺手认的同义词，回执每张图都跟着
        发、能短就短（用户 10-07 选的「只写横屏」）。
        """
        text = self._note(None)
        self.assertIn("要横屏", text)
        self.assertIn("silver 横屏", text)

    def test_default_note_mentions_qwen_hd(self):
        """回执给 qwen 超清（qwen-hd，4096×6144）曝光，并讲清它的图生图触发指令。

        2026-10-07（晚）补：qwen-hd 是 qwen 的 4x 超清版，路由走
        `skill=qwen-hd` + `source_image=1`（见 generate_image 提示词与
        `_I2I_SKILLS`）。用户得知道「qwen 超清，图生图，描述」这个入口。
        """
        text = self._note(None)
        self.assertIn("qwen 超清", text)
        self.assertIn("图生图", text)
        self.assertIn("qwen 超清，图生图，描述", text)

    def test_default_note_stays_five_lines(self):
        """回执跟着**每一张图**发，行数是最直接的成本 → 钉住。

        现 8 行 = 状态行（任务已提交…）+ 渠道行（当前渠道：…）+ 附言 6 行
        （想换渠道就发… / 渠道：… / 要 4x 超清… / qwen 超清支持图生图… /
        要横屏… / 更多玩法…）。
        以后想再加一行，先想清楚值不值。
        """
        self.assertEqual(len(self._note(None).splitlines()), 8)

    def test_default_note_lists_every_nameable_channel(self):
        """2026-10-07 用户拍板「把我全部的可用渠道都加上去」——能点名的 11 个
        都要在回执里出现（原「常用」那行只列 5 个，sd / krea2 / nffa / miao /
        cunny 一直没有曝光位）。

        `image_gen_v1_hires` / `nai_wide` 没有短名、点名不到，故意不列。
        """
        text = self._note(None)
        for name in ("silver", "anima", "qwen", "nai", "jank", "sd",
                     "krea2", "nffa", "miao", "cunny", "silver-hd"):
            self.assertIn(name, text, name)
        for name in ("image_gen_v1_hires", "nai_wide"):
            self.assertNotIn(name, text, name)

    def test_empty_string_switches_the_note_off(self):
        """清空 = 「这段不要了」，只留状态行和渠道行。"""
        text = self._note("")
        self.assertEqual(text.splitlines(),
                         ["任务已提交，正在画了。", "当前渠道：silver"])

    def test_custom_note_replaces_the_default(self):
        text = self._note("画好我喊你")
        self.assertEqual(text.splitlines()[-1], "画好我喊你")
        self.assertNotIn("更多渠道", text)

    def test_channel_line_survives_without_a_skill(self):
        """老调用方 / 手工构造的 Job 没带 skill：少报一行，不硬编渠道名。"""
        with mock.patch.object(agent_store, "receipt_note",
                               return_value=""):
            self.assertEqual(gi._receipt_text(0), "任务已提交，正在画了。")


class ReceiptSendFailureTest(_SubmitHarness):
    def test_failure_falls_back_to_the_old_receipt(self):
        """发不出去就不许装作发过——退回老文案，由模型转述。"""
        def boom(*a):
            raise RuntimeError("OneBot 调用失败 send_group_msg")

        # 这条路上 `_send_receipt` 会 log.exception——预期内的失败，别把堆栈
        # 打到测试输出里冒充故障。
        with mock.patch.object(image_jobs, "_send_text", boom), \
                mock.patch.object(gi.log, "exception"):
            out = self._submit(("group", "9"))
        self.assertEqual(self.sent, [])
        self.assertFalse(out.startswith(MARK))
        self.assertIn("已经在画了", out)          # 老文案：模型还能照实说一句


class QueuedNoteTest(unittest.TestCase):
    """掐断循环的「已提交」判据必须认直发回执，否则一轮一张的硬闸整条失效。"""

    def test_direct_receipt_counts_as_submitted(self):
        note = agent_mod._queued_note_for_user(
            MARK + "任务已提交，前面还有 2 张在排队。\n（写给模型的叮嘱…）")
        self.assertEqual(note, "任务已提交，前面还有 2 张在排队。")

    def test_legacy_receipt_still_works(self):
        """直发失败退回时用的老文案照样要认（那是同一条路的备用出口）。"""
        note = agent_mod._queued_note_for_user(
            "已经排上队了（前面还有 1 张），排到就画，画好会自动发到群里。"
            "不要输出图片地址，也不要说「图在下面 / 稍等」。")
        self.assertEqual(
            note, "已经排上队了（前面还有 1 张），排到就画，画好会自动发到群里。")

    def test_error_is_not_a_receipt(self):
        self.assertEqual(
            agent_mod._queued_note_for_user("错误：ComfyUI 现在没在线"), "")


class BotKeepsQuietTest(unittest.TestCase):
    """回执由后台直发后，本轮模型正文一律不发——它爱编排队张数。"""

    def _turn(self, events):
        runner = qq_bot.SessionRunner(None, "group_9", "group", "9")
        sent = []
        batch = [{"text": "画个猫", "sender": "233", "images": [], "quotes": []}]
        send = lambda gid, text, limit=None: (sent.append(text), 1)[1]
        with mock.patch.object(qq_bot, "run_agent_stream",
                               lambda *a, **k: iter(events)), \
                mock.patch.object(qq_bot.direct_gen, "ENABLED", False), \
                mock.patch.object(qq_bot, "load_history", lambda *a, **k: []), \
                mock.patch.object(qq_bot, "_ensure_system_prompt", lambda *a: None), \
                mock.patch.object(qq_bot, "save_history", lambda *a, **k: None), \
                mock.patch.object(qq_bot.stickers, "collect", return_value=0), \
                mock.patch.object(qq_bot.stickers, "catalog", return_value=""), \
                mock.patch.object(qq_api, "send_group", send), \
                mock.patch.object(qq_api, "send_private", send), \
                mock.patch.object(qq_bot.interject, "mark_spoke") as spoke, \
                mock.patch.object(qq_bot.recent, "remember") as rem:
            runner._run_turn(batch)
        return sent, spoke, rem

    @staticmethod
    def _events(result):
        return [{"type": "assistant",
                 "content": "已经排上队了，前面还有 3 张，马上就好"},
                {"type": "tool_result", "name": "generate_image",
                 "result": result}]

    def test_model_text_is_dropped_when_the_receipt_went_out(self):
        sent, spoke, rem = self._turn(
            self._events(MARK + "任务已提交，前面还有 1 张在排队。\n（叮嘱…）"))
        self.assertEqual(sent, [])            # 模型那句「还有 3 张」没发出去
        self.assertTrue(spoke.called)         # 但机器人开过口：冷却重新计时
        self.assertEqual(rem.call_args[0][3], "任务已提交，前面还有 1 张在排队。")

    def test_model_text_is_kept_without_the_marker(self):
        """没有标记（直发失败退回了老文案）时照旧把模型正文发出去。"""
        sent, _, _ = self._turn(self._events("错误：ComfyUI 现在没在线"))
        self.assertEqual(sent, ["已经排上队了，前面还有 3 张，马上就好"])

    def test_deliver_records_the_receipt_as_spoken(self):
        sent = []
        runner = qq_bot.SessionRunner(None, "group_9", "group", "9")
        # recent.remember 也要挡：它落盘到 agents/<agent>/recent/，不挡就会
        # 用假群号 group_9 往真实数据目录里写一条（测试污染）。
        with mock.patch.object(
                qq_api, "send_group",
                lambda gid, text, limit=None: (sent.append(text), 1)[1]), \
                mock.patch.object(qq_bot.interject, "mark_spoke") as spoke, \
                mock.patch.object(qq_bot.recent, "remember"):
            runner._deliver(False, "", [], "任务已提交，正在画了。")
        self.assertEqual(sent, [])
        self.assertTrue(spoke.called)


if __name__ == "__main__":
    unittest.main()
