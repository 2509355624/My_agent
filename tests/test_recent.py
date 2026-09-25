# -*- coding: utf-8 -*-
"""群聊背景测试（app/recent.py + qq_bot 的接入点）。

机器人原先收到群消息、不 @ 它就直接丢掉，所以每轮只看得到「有人问了它
一句」，只能一问一答、像个问答助手。这里守三件事：

1. 群消息真的被记下来了（含不 @ 的、含自己发的）；
2. 背景真的被带给了模型，而且走的是**不落会话历史**的那条通道；
3. 背景不会无限膨胀（条数闸、字数闸、文件裁剪）。

最要紧的是第 2 条的后半句：背景一旦拼进正文就会写进 history，每轮重复
堆一份，十几轮下来把预算占满——测试把它固住。
"""

import json
import os
import tempfile
import unittest
from unittest import mock

import app.agent as agent
import app.agents as agents
import app.qq_bot as qq_bot
import app.recent as recent


class _TmpAgentsMixin:
    """把 AGENTS_DIR 指到临时目录，别碰真实数据。"""

    def _setup_tmp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.join(self.tmp.name, "agents")
        # 建出 agent 目录：safe_agent_id 会做「确实是 AGENTS_DIR 直接子目录」
        # 的校验，目录不存在时可能退化成默认 agent
        os.makedirs(os.path.join(self.root, "qq"), exist_ok=True)
        p = mock.patch.object(agents, "AGENTS_DIR", self.root)
        p.start()
        self.addCleanup(p.stop)


# ─── 缓存本身 ───────────────────────────────────────

class RecentStoreTest(_TmpAgentsMixin, unittest.TestCase):
    def setUp(self):
        self._setup_tmp()

    def test_remember_then_load_in_order(self):
        recent.remember("qq", "9", "张三", "第一句")
        recent.remember("qq", "9", "李四", "第二句")
        got = recent.load_recent("qq", "9", 10)
        self.assertEqual([m["x"] for m in got], ["第一句", "第二句"])
        self.assertEqual(got[0]["n"], "张三")
        self.assertEqual(got[1]["n"], "李四")

    def test_limit_keeps_most_recent(self):
        for i in range(5):
            recent.remember("qq", "9", "甲", "m%d" % i)
        self.assertEqual([m["x"] for m in recent.load_recent("qq", "9", 2)],
                         ["m3", "m4"])

    def test_groups_are_isolated(self):
        recent.remember("qq", "9", "甲", "九群")
        recent.remember("qq", "8", "乙", "八群")
        self.assertEqual([m["x"] for m in recent.load_recent("qq", "9", 5)],
                         ["九群"])

    def test_blank_text_skipped(self):
        self.assertFalse(recent.remember("qq", "9", "甲", "   "))
        self.assertFalse(recent.remember("qq", "9", "甲", ""))
        self.assertEqual(recent.load_recent("qq", "9", 5), [])

    def test_multiline_collapsed_to_one_record(self):
        # 正文带换行会把 jsonl 本身写坏（读回来是一堆半截 JSON）
        recent.remember("qq", "9", "甲", "第一行\n第二行")
        self.assertEqual(recent.load_recent("qq", "9", 5)[0]["x"], "第一行 第二行")

    def test_bad_group_id_rejected(self):
        self.assertFalse(recent.remember("qq", "../evil", "甲", "x"))
        self.assertFalse(recent.remember("qq", "a/b", "甲", "x"))
        self.assertEqual(recent.load_recent("qq", "../evil", 5), [])

    def test_missing_cache_is_empty_not_error(self):
        self.assertEqual(recent.load_recent("qq", "9", 5), [])
        self.assertEqual(recent.format_recent("qq", "9", 30, 500), "")

    def test_broken_line_skipped(self):
        recent.remember("qq", "9", "甲", "好的")
        path = os.path.join(self.root, "qq", "recent", "group_9.jsonl")
        with open(path, "a", encoding="utf-8") as f:
            f.write('{"x": "半截\n')
        self.assertEqual([m["x"] for m in recent.load_recent("qq", "9", 5)],
                         ["好的"])

    def test_trim_keeps_file_bounded(self):
        """写到超过 MAX_LINES 时整体重写，只留最近 KEEP_LINES 条。

        注意不是每条都裁：写满 MAX 后裁一次回到 KEEP，再往后几行都不触发，
        所以断言看的是「上界」和「最新的还在」，不是恰好等于 KEEP。
        """
        for name, value in (("MAX_LINES", 5), ("KEEP_LINES", 3)):
            p = mock.patch.object(recent, name, value)
            p.start()
            self.addCleanup(p.stop)
        for i in range(8):
            recent.remember("qq", "9", "甲", "m%d" % i)
        got = [m["x"] for m in recent.load_recent("qq", "9", 100)]
        self.assertLessEqual(len(got), 5)
        self.assertEqual(got[-1], "m7")
        self.assertNotIn("m0", got)      # 最旧的在裁剪时被丢掉了

    def test_trim_fires_on_the_write_that_overflows(self):
        for name, value in (("MAX_LINES", 5), ("KEEP_LINES", 3)):
            p = mock.patch.object(recent, name, value)
            p.start()
            self.addCleanup(p.stop)
        for i in range(6):
            recent.remember("qq", "9", "甲", "m%d" % i)
        self.assertEqual([m["x"] for m in recent.load_recent("qq", "9", 100)],
                         ["m3", "m4", "m5"])

    def test_image_url_kept_in_record(self):
        # 图在上下文里只渲染成 "[图片]" 占位符，但地址要留在记录里——
        # 接话时「把最近那张图捞出来给主模型看」靠的就是它
        recent.remember("qq", "9", "甲", "[图片]", image="http://x/1.jpg")
        recent.remember("qq", "9", "乙", "哈哈哈")
        recs = recent.load_recent("qq", "9", 5)
        self.assertEqual(recs[0].get("m"), "http://x/1.jpg")
        self.assertNotIn("m", recs[1])

    def test_latest_image_returns_newest(self):
        recent.remember("qq", "9", "甲", "[图片]", image="http://x/old.jpg")
        recent.remember("qq", "9", "乙", "什么图")
        recent.remember("qq", "9", "丙", "[图片]", image="http://x/new.jpg")
        self.assertEqual(recent.latest_image("qq", "9"), "http://x/new.jpg")

    def test_latest_image_empty_when_no_image(self):
        recent.remember("qq", "9", "甲", "纯文字聊天")
        self.assertEqual(recent.latest_image("qq", "9"), "")
        self.assertEqual(recent.latest_image("qq", "9", within=1), "")

    def test_latest_image_window_limits_lookback(self):
        # 太老的图多半已经不在当前话题里，往前翻的条数要有闸
        recent.remember("qq", "9", "甲", "[图片]", image="http://x/old.jpg")
        for i in range(3):
            recent.remember("qq", "9", "乙", "闲聊%d" % i)
        self.assertEqual(recent.latest_image("qq", "9", within=2), "")

    def test_trim_archives_dropped_lines(self):
        """裁剪丢掉的行进 archive（按天一个文件），滚动缓存归滚动缓存，
        历史记录留着——以后做长期记忆 / 训练数据用得上。"""
        for name, value in (("MAX_LINES", 5), ("KEEP_LINES", 3)):
            p = mock.patch.object(recent, name, value)
            p.start()
            self.addCleanup(p.stop)
        for i in range(6):
            recent.remember("qq", "9", "甲", "m%d" % i)
        arc = os.path.join(self.root, "qq", "recent", "archive")
        files = os.listdir(arc)
        self.assertEqual(len(files), 1)
        # 文件名本身就是「日期」——归档文件必须长这样，写坏名字的归档不算归档
        self.assertRegex(files[0], r"^group_9_\d{4}-\d{2}-\d{2}\.jsonl$")
        with open(os.path.join(arc, files[0]), encoding="utf-8") as f:
            archived = [json.loads(l)["x"] for l in f if l.strip()]
        self.assertEqual(archived, ["m0", "m1", "m2"])


class FormatRecentTest(_TmpAgentsMixin, unittest.TestCase):
    def setUp(self):
        self._setup_tmp()

    def test_header_and_speaker(self):
        recent.remember("qq", "9", "张三", "在吗")
        out = recent.format_recent("qq", "9", 30, 500)
        self.assertTrue(out.startswith("[群里最近的对话]"))
        self.assertIn("张三：在吗", out)

    def test_falls_back_to_user_id_without_nickname(self):
        recent.remember("qq", "9", "", "没昵称", user_id="12345")
        self.assertIn("12345：没昵称", recent.format_recent("qq", "9", 30, 500))

    def test_max_chars_drops_oldest(self):
        for i in range(10):
            recent.remember("qq", "9", "甲", "第%d句内容" % i)
        out = recent.format_recent("qq", "9", 30, 30)
        self.assertIn("第9句内容", out)
        self.assertNotIn("第0句内容", out)

    def test_zero_disables_context(self):
        recent.remember("qq", "9", "甲", "x")
        self.assertEqual(recent.format_recent("qq", "9", 0, 500), "")
        self.assertEqual(recent.format_recent("qq", "9", 30, 0), "")


# ─── _dispatch：群消息记不记 ────────────────────────

class DispatchRecordsGroupTest(_TmpAgentsMixin, unittest.TestCase):
    def setUp(self):
        self._setup_tmp()
        for name, value in (
            ("QQ_AGENT_ID", "qq"),
            ("QQ_PRIVATE_ENABLE", True),
            ("QQ_GROUP_AT_ONLY", True),
            ("QQ_GROUP_KEYWORDS", []),
            ("QQ_WHITELIST_GROUPS", []),
            ("QQ_WHITELIST_USERS", []),
            ("QQ_BLACKLIST_USERS", []),
        ):
            p = mock.patch.object(qq_bot, name, value)
            p.start()
            self.addCleanup(p.stop)
        # 这个类测的是「不 @ 也记缓存」。主动接话开着时，不 @ 的消息还会被交给
        # 判断链路，turns 断言就不再是空的——那是另一条链路的用例该管的事
        # （tests/test_interject.py），这里显式关掉，别受 .env 当前配置影响。
        p = mock.patch.object(qq_bot.interject, "enabled", return_value=False)
        p.start()
        self.addCleanup(p.stop)

    def _event(self, text="", user_id="1", group_id="9", mtype="group",
               self_id="999", at=False, image=False):
        segs = []
        if at:
            segs.append({"type": "at", "data": {"qq": self_id}})
        if text:
            segs.append({"type": "text", "data": {"text": text}})
        if image:
            segs.append({"type": "image", "data": {"url": "http://x/1.png"}})
        ev = {"post_type": "message", "message_type": mtype,
              "self_id": self_id, "user_id": user_id, "message": segs,
              "sender": {"nickname": "张三"}}
        if mtype == "group":
            ev["group_id"] = group_id
        return json.dumps(ev)

    def _dispatch(self, raw):
        """跑一次 _dispatch，返回被交给 runner 的轮次（空表示没触发回复）。"""
        bot = qq_bot.QQBot()
        turns = []
        bot._runner_for = lambda *a, **kw: mock.Mock(
            submit=lambda *a, **kw: turns.append(a))
        bot._dispatch(raw)
        return turns

    def test_not_at_me_is_still_recorded(self):
        turns = self._dispatch(self._event("今天天气不错"))
        self.assertEqual(turns, [])            # 没 @，不该回
        self.assertEqual([m["x"] for m in recent.load_recent("qq", "9", 10)],
                         ["今天天气不错"])

    def test_at_me_is_recorded_and_replies(self):
        turns = self._dispatch(self._event("你说是吧", at=True))
        self.assertEqual(len(turns), 1)        # 确实触发了回复
        self.assertEqual([m["x"] for m in recent.load_recent("qq", "9", 10)],
                         ["你说是吧"])

    def test_own_message_recorded_without_replying(self):
        # 机器人自己说的话也是群聊上下文的一部分，但它不该触发自问自答
        turns = self._dispatch(self._event("我是机器人", user_id="999"))
        self.assertEqual(turns, [])
        self.assertEqual([m["x"] for m in recent.load_recent("qq", "9", 10)],
                         ["我是机器人"])

    def test_image_only_message_recorded_as_placeholder(self):
        self._dispatch(self._event(image=True))
        self.assertEqual([m["x"] for m in recent.load_recent("qq", "9", 10)],
                         ["[图片]"])

    def test_private_not_recorded(self):
        # 私聊没有「群里别的消息」这回事，上下文就是会话历史本身
        self._dispatch(self._event("在吗", mtype="private"))
        self.assertEqual(recent.load_recent("qq", "1", 10), [])


# ─── _run_turn：背景传没传 ──────────────────────────

class RunTurnContextTest(_TmpAgentsMixin, unittest.TestCase):
    def setUp(self):
        self._setup_tmp()
        for name, value in (("QQ_AGENT_ID", "qq"), ("QQ_CONTEXT_MESSAGES", 30),
                            ("QQ_CONTEXT_MAX_CHARS", 1500)):
            p = mock.patch.object(qq_bot, name, value)
            p.start()
            self.addCleanup(p.stop)

    def _run(self, target="group", target_id="9", text="在吗"):
        runner = qq_bot.SessionRunner(mock.Mock(), "group_" + target_id,
                                      target, target_id)
        seen = {}

        def fake_stream(user_input, history, **kw):
            seen["text"] = user_input
            seen["extra"] = kw.get("extra_context")
            return iter([])          # 空生成器：一轮立刻结束

        with mock.patch.object(qq_bot, "run_agent_stream", fake_stream), \
             mock.patch.object(qq_bot, "load_history",
                               lambda *a, **kw: [{"role": "system",
                                                  "content": "s"}]), \
             mock.patch.object(qq_bot, "sync_session_system",
                               lambda *a, **kw: False), \
             mock.patch.object(qq_bot, "save_history", lambda *a, **kw: True), \
             mock.patch.object(qq_bot.qq_api, "bind_context", lambda *a: None), \
             mock.patch.object(qq_bot.qq_api, "clear_context", lambda: None), \
             mock.patch.object(runner, "_deliver", lambda *a: None):
            runner._run_turn([{"text": text, "sender": "张三"}])
        return seen

    def test_group_gets_context_but_not_in_text(self):
        recent.remember("qq", "9", "李四", "刚才在聊吃饭")
        seen = self._run()
        self.assertIn("刚才在聊吃饭", seen["extra"])
        # 关键：不能拼进正文，否则会写进 history、每轮重复堆一份
        self.assertNotIn("刚才在聊吃饭", seen["text"])
        self.assertEqual(seen["text"], "在吗")

    def test_private_gets_no_context(self):
        recent.remember("qq", "1", "李四", "私聊的话")
        seen = self._run(target="private", target_id="1")
        self.assertIsNone(seen["extra"])

    def test_empty_context_passes_none(self):
        seen = self._run()
        self.assertIsNone(seen["extra"])


# ─── 注入通道：状态栏 ───────────────────────────────

class StatusMessageContextTest(unittest.TestCase):
    """extra_context 挂在末尾那条状态栏消息里，不写回 history。"""

    def test_context_goes_into_status_message(self):
        msg = agent._status_message([], extra_context="[群里最近的对话]\n张三：在吗")
        self.assertEqual(msg["role"], "system")
        self.assertIn("[群里最近的对话]", msg["content"])
        self.assertIn("<status_bar>", msg["content"])

    def test_without_context_content_unchanged(self):
        msg = agent._status_message([])
        self.assertNotIn("[群里最近的对话]", msg["content"])
        self.assertIn("<status_bar>", msg["content"])

    def test_blank_context_ignored(self):
        plain = agent._status_message([])["content"]
        self.assertEqual(agent._status_message([], extra_context="   ")["content"],
                         plain)


if __name__ == "__main__":
    unittest.main()
