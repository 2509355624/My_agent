# -*- coding: utf-8 -*-
"""QQ 接入测试（app/qq_api.py + app/qq_bot.py + 多会话线）。

这条链路上最容易出错的不是网络，而是三处「翻译」：

1. Markdown → QQ 纯文本：QQ 不渲染 Markdown，漏降级就是满屏符号；
2. 长文 → 多条消息：单条有长度上限，不切就是被截断；
3. 事件 → 要不要回：判错就是机器人乱插话，或者被叫到却不理。

再加会话线隔离（每个人 / 每个群各一条历史，不能串）和「工具发过就别重复回发」。
真机（连 NapCat 收真实群消息）另做验证，这里只管逻辑。
"""

import asyncio
import json
import os
import tempfile
import unittest
from unittest import mock

import app.agents as agents
import app.qq_api as qq_api
import app.qq_bot as qq_bot
from app.tools.normal import send_qq_message


# ─── Markdown 降级 ──────────────────────────────────

class ToQqTextTest(unittest.TestCase):
    def test_strips_heading_and_bold(self):
        self.assertEqual(qq_api.to_qq_text("## 标题\n**重点**在这"),
                         "标题\n重点在这")

    def test_strips_code_fence_but_keeps_code(self):
        self.assertEqual(qq_api.to_qq_text("```python\nprint(1)\n```"),
                         "print(1)")

    def test_inline_code_loses_backticks(self):
        self.assertEqual(qq_api.to_qq_text("用 `search_kb` 查"), "用 search_kb 查")

    def test_link_becomes_text_plus_url(self):
        self.assertEqual(qq_api.to_qq_text("[点这里](https://a.com)"),
                         "点这里 https://a.com")

    def test_image_keeps_url_with_marker(self):
        self.assertEqual(qq_api.to_qq_text("![图](https://a.com/x.png)"),
                         "[图片] https://a.com/x.png")

    def test_bullet_and_hr(self):
        # 分隔线整行去掉；列表符号换成 ·，不要把内容吃掉
        self.assertEqual(qq_api.to_qq_text("- 甲\n- 乙\n\n---"), "· 甲\n· 乙")

    def test_quote_marker_removed(self):
        self.assertEqual(qq_api.to_qq_text("> 引用内容"), "引用内容")

    def test_snake_case_identifier_is_untouched(self):
        # 下划线强调的规则不能误伤代码里的蛇形命名
        self.assertEqual(qq_api.to_qq_text("my_var_name"), "my_var_name")

    def test_blank_input(self):
        self.assertEqual(qq_api.to_qq_text(""), "")
        self.assertEqual(qq_api.to_qq_text(None), "")


# ─── 长文切分 ───────────────────────────────────────

class SplitMessageTest(unittest.TestCase):
    def test_short_text_stays_one_message(self):
        self.assertEqual(qq_api.split_message("你好", 100), ["你好"])

    def test_empty_returns_nothing(self):
        self.assertEqual(qq_api.split_message(""), [])
        self.assertEqual(qq_api.split_message("   \n  "), [])

    def test_splits_on_paragraph_boundary(self):
        # 两个都装不下的段落要各自成条，而不是从中间劈开
        text = "A" * 60 + "\n\n" + "B" * 60
        self.assertEqual(qq_api.split_message(text, 100),
                         ["A" * 60, "B" * 60])

    def test_merges_short_paragraphs_to_fill_one_message(self):
        text = "a" * 30 + "\n\n" + "b" * 30 + "\n\n" + "c" * 30
        parts = qq_api.split_message(text, 70)
        self.assertEqual(parts, ["a" * 30 + "\n" + "b" * 30, "c" * 30])

    def test_overlong_single_line_is_hard_split(self):
        # 模型偶尔一整段不换行，这时只能硬切
        parts = qq_api.split_message("X" * 250, 100)
        self.assertEqual([len(p) for p in parts], [100, 100, 50])

    def test_every_chunk_fits_the_limit(self):
        text = "\n".join("第%d行" % i + "内容" * 20 for i in range(30))
        parts = qq_api.split_message(text, 80)
        self.assertTrue(parts)
        self.assertTrue(all(len(p) <= 80 for p in parts), "有超限的消息条")

    def test_no_content_lost(self):
        text = "第一段" * 20 + "\n\n" + "第二段" * 20
        joined = "".join(qq_api.split_message(text, 60))
        self.assertEqual(joined.replace("\n", ""), text.replace("\n", ""))


# ─── 图片消息段 ─────────────────────────────────────

class MergeBatchTest(unittest.TestCase):
    """排队攒下的多条消息合并成一条，并施加条数 / 字数上限。

    静默窗口只负责「等连发到齐」，它自己不限制攒多少——群里被刷屏时
    _pending 会一直涨，直接 join 出来的那一条会长到离谱，一次全灌进模型。
    """

    @staticmethod
    def _batch(*texts):
        return [{"text": t, "sender": ""} for t in texts]

    def test_joins_in_arrival_order(self):
        out = qq_bot._merge_batch(self._batch("第一句", "第二句"))
        self.assertEqual(out, "第一句\n第二句")

    def test_skips_empty_texts(self):
        out = qq_bot._merge_batch(self._batch("有", "", "有"))
        self.assertEqual(out, "有\n有")

    def test_empty_batch(self):
        self.assertEqual(qq_bot._merge_batch([]), "")

    def test_item_limit_keeps_newest(self):
        b = self._batch(*["m%d" % i for i in range(30)])
        out = qq_bot._merge_batch(b, max_items=3)
        self.assertEqual(out, "m27\nm28\nm29")

    def test_char_limit_keeps_newest(self):
        b = self._batch("a" * 100, "b" * 100, "c" * 100)
        out = qq_bot._merge_batch(b, max_items=0, max_chars=150)
        self.assertEqual(out, "c" * 100)          # 只装得下最近一条

    def test_char_limit_accumulates_multiple(self):
        b = self._batch("a" * 60, "b" * 60, "c" * 60)
        out = qq_bot._merge_batch(b, max_items=0, max_chars=150)
        self.assertEqual(out, "b" * 60 + "\n" + "c" * 60)

    def test_latest_message_kept_even_if_oversized(self):
        """最新那条永远保留——否则会把用户刚说的话整个吞掉，比超长更糟。"""
        b = self._batch("旧" * 10, "新" * 500)
        out = qq_bot._merge_batch(b, max_items=0, max_chars=100)
        self.assertEqual(out, "新" * 500)

    def test_zero_limits_mean_no_cap(self):
        b = self._batch(*["m%d" % i for i in range(30)])
        out = qq_bot._merge_batch(b, max_items=0, max_chars=0)
        self.assertEqual(len(out.split("\n")), 30)

    def test_only_quote_message_is_not_filtered_out(self):
        """只有引用、没有文字的消息不能被当成「空消息」丢掉。

        否则「引用一条 + 直接发送」会被静默过滤，用户等半天没反应。这里
        用「只留最近一条」反证：留下的必须是那条没有文字、只有引用的消息
        （它拼不出文字，所以结果为空串）。
        """
        batch = [
            {"text": "更早的一句", "sender": "", "images": [], "quotes": []},
            {"text": "", "sender": "", "images": [],
             "quotes": [{"kind": "reply", "id": "1"}]},
        ]
        self.assertEqual(qq_bot._merge_batch(batch, max_items=1), "")


class ImageSegmentTest(unittest.TestCase):
    def test_http_url_passes_through(self):
        url = "http://127.0.0.1:8188/view?filename=a.png"
        self.assertEqual(qq_api.image_segment(url)["data"]["file"], url)

    def test_local_path_becomes_file_uri(self):
        seg = qq_api.image_segment(r"D:\AI\agent_my_test\data\a.png")
        self.assertTrue(seg["data"]["file"].startswith("file:///"))
        self.assertNotIn("\\", seg["data"]["file"])
        self.assertIn("a.png", seg["data"]["file"])


# ─── 多会话线（agents.session_file）──────────────────

class SessionKeyTest(unittest.TestCase):
    def test_key_lands_under_sessions_dir(self):
        path = agents.session_file("qq", "group_123")
        self.assertTrue(path.endswith(os.path.join("sessions", "group_123.jsonl")),
                        path)

    def test_without_key_still_uses_main_session(self):
        self.assertTrue(agents.session_file("qq").endswith("session.jsonl"))

    def test_different_keys_get_different_files(self):
        a = agents.session_file("qq", "private_1")
        b = agents.session_file("qq", "private_2")
        self.assertNotEqual(a, b)

    def test_illegal_key_falls_back_to_main_session(self):
        # 会话存不下来比抛异常打断一轮对话更糟，所以是兜底而不是报错
        for bad in ("../evil", "a/b", "a\\b", "..", ".hidden", "x" * 100, "中文键"):
            path = agents.session_file("qq", bad)
            self.assertTrue(path.endswith("session.jsonl"), bad)

    def test_safe_session_key(self):
        self.assertEqual(agents.safe_session_key("private_12345"), "private_12345")
        self.assertEqual(agents.safe_session_key(" group_9 "), "group_9")
        self.assertIsNone(agents.safe_session_key(""))
        self.assertIsNone(agents.safe_session_key(None))
        self.assertIsNone(agents.safe_session_key("中文"))


# ─── 事件解析 ───────────────────────────────────────

class ParseSegmentsTest(unittest.TestCase):
    def test_array_format_detects_at_self(self):
        ev = {"self_id": 111, "message": [
            {"type": "at", "data": {"qq": "111"}},
            {"type": "text", "data": {"text": " 你好"}},
        ]}
        text, at_me, _imgs = qq_bot._parse_segments(ev)
        self.assertEqual(text, "你好")
        self.assertTrue(at_me)

    def test_at_someone_else_is_not_at_me(self):
        ev = {"self_id": 111, "message": [
            {"type": "at", "data": {"qq": "222"}},
            {"type": "text", "data": {"text": "你好"}},
        ]}
        _text, at_me, _imgs = qq_bot._parse_segments(ev)
        self.assertFalse(at_me)

    def test_image_url_is_collected(self):
        """带 url 的图片段收集地址（不在这里下载，下载在 worker 线程里做）。"""
        ev = {"self_id": 1, "message": [
            {"type": "image", "data": {"file": "x.jpg",
                                       "url": "https://example.com/a.jpg"}},
            {"type": "text", "data": {"text": "看看这个"}},
        ]}
        text, _at, imgs = qq_bot._parse_segments(ev)
        self.assertEqual(text, "看看这个")
        self.assertEqual(imgs, ["https://example.com/a.jpg"])

    def test_multiple_images_all_collected(self):
        ev = {"self_id": 1, "message": [
            {"type": "image", "data": {"url": "u1"}},
            {"type": "text", "data": {"text": "两张"}},
            {"type": "image", "data": {"url": "u2"}},
        ]}
        _text, _at, imgs = qq_bot._parse_segments(ev)
        self.assertEqual(imgs, ["u1", "u2"])

    def test_image_without_url_falls_back_to_placeholder(self):
        """只有本地文件名（file）时拿不到图，退回占位文本，不能当成有图。"""
        ev = {"self_id": 1, "message": [
            {"type": "image", "data": {"file": "x.jpg"}},
            {"type": "face", "data": {"id": "1"}},
            {"type": "text", "data": {"text": "看看"}},
        ]}
        text, _at, imgs = qq_bot._parse_segments(ev)
        self.assertEqual(text, "[图片][表情]看看")
        self.assertEqual(imgs, [])

    def test_cq_string_image_url_is_collected(self):
        """CQ 码格式（raw_message）也要能认出图片地址。"""
        ev = {"self_id": "111",
              "raw_message": "[CQ:image,file=x.jpg,url=https://e.com/b.png] 看"}
        text, _at, imgs = qq_bot._parse_segments(ev)
        self.assertEqual(text, "看")
        self.assertEqual(imgs, ["https://e.com/b.png"])

    def test_cq_string_format(self):
        ev = {"self_id": "111", "raw_message": "[CQ:at,qq=111] 在吗"}
        text, at_me, _imgs = qq_bot._parse_segments(ev)
        self.assertEqual(text, "在吗")
        self.assertTrue(at_me)

    def test_cq_string_at_someone_else(self):
        ev = {"self_id": "111", "raw_message": "[CQ:at,qq=999] 在吗"}
        _text, at_me, _imgs = qq_bot._parse_segments(ev)
        self.assertFalse(at_me)


# ─── 引用消息（reply / forward 段）───────────────────

class ExtractQuotesTest(unittest.TestCase):
    """引用段只带一个 id，被引用的正文不在这条消息里。

    以前这两类段走到 _parse_segments 就被静默丢掉：数组形式不匹配任何
    分支，CQ 码形式被正则替换成空串。于是「引用一句 + 问怎么回」在模型
    眼里就是光秃秃的一句问话，完全不知道在说哪句。
    """

    def test_array_reply(self):
        ev = {"message": [
            {"type": "reply", "data": {"id": "1532711165"}},
            {"type": "text", "data": {"text": "这句怎么回"}},
        ]}
        self.assertEqual(qq_bot._extract_quotes(ev),
                         [{"kind": "reply", "id": "1532711165"}])

    def test_array_forward_keeps_inline_nodes(self):
        nodes = [{"sender": {"nickname": "甲"}, "message": []}]
        ev = {"message": [{"type": "forward",
                           "data": {"id": "768", "content": nodes}}]}
        self.assertEqual(qq_bot._extract_quotes(ev),
                         [{"kind": "forward", "id": "768", "nodes": nodes}])

    def test_forward_without_content_keeps_id_for_lookup(self):
        """协议端不下发 content 时要留下 id，好在展开时回头查一次。"""
        ev = {"message": [{"type": "forward", "data": {"id": "768"}}]}
        self.assertEqual(qq_bot._extract_quotes(ev),
                         [{"kind": "forward", "id": "768", "nodes": None}])

    def test_cq_string_reply(self):
        ev = {"raw_message": "[CQ:reply,id=1532711165] 这句怎么回"}
        self.assertEqual(qq_bot._extract_quotes(ev),
                         [{"kind": "reply", "id": "1532711165"}])

    def test_cq_string_forward(self):
        ev = {"raw_message": "[CQ:forward,id=768] 看这个"}
        self.assertEqual(qq_bot._extract_quotes(ev),
                         [{"kind": "forward", "id": "768", "nodes": None}])

    def test_reply_without_id_is_ignored(self):
        ev = {"message": [{"type": "reply", "data": {}}]}
        self.assertEqual(qq_bot._extract_quotes(ev), [])

    def test_no_quote_returns_empty(self):
        ev = {"message": [{"type": "text", "data": {"text": "你好"}}]}
        self.assertEqual(qq_bot._extract_quotes(ev), [])

    def test_quote_segment_never_leaks_into_text(self):
        """引用段不该混进正文——正文要干干净净地参与触发词匹配。"""
        ev = {"message": [
            {"type": "reply", "data": {"id": "1"}},
            {"type": "text", "data": {"text": "帮我看看"}},
        ]}
        text, _at, _imgs = qq_bot._parse_segments(ev)
        self.assertEqual(text, "帮我看看")


class ResolveQuoteTest(unittest.TestCase):
    """把引用来源展开成「文本 + 图片地址」。

    拉取失败一律降级成一句说明：引用只是本轮的补充上下文，取不到不该让
    整轮对话失败（与取图失败的处理一致）。
    """

    @staticmethod
    def _msg(text, nickname="温知澄", images=()):
        segs = [] if not text else [{"type": "text", "data": {"text": text}}]
        segs += [{"type": "image", "data": {"url": u}} for u in images]
        return {"sender": {"nickname": nickname}, "message": segs}

    def test_reply_is_fetched_and_rendered(self):
        """get_msg 返回的结构与 WS 事件同构，所以直接复用同一套解析。"""
        msg = self._msg("我机器人弄不进来")
        with mock.patch.object(qq_api, "get_message", lambda mid, **kw: msg):
            block, images = qq_bot._resolve_quote(
                {"kind": "reply", "id": "1532711165"}, 3000)
        self.assertEqual(block, "[引用 温知澄 的消息] 我机器人弄不进来")
        self.assertEqual(images, [])

    def test_reply_images_are_returned_for_download(self):
        """引用里的图要一起带出来，否则「引用一张截图问怎么回」等于没解决。"""
        msg = self._msg("", images=["https://e.com/a.jpg"])
        with mock.patch.object(qq_api, "get_message", lambda mid, **kw: msg):
            block, images = qq_bot._resolve_quote(
                {"kind": "reply", "id": "1"}, 3000)
        self.assertEqual(images, ["https://e.com/a.jpg"])
        self.assertIn("（图片）", block)

    def test_reply_fetch_failure_degrades(self):
        def boom(mid, **kw):
            raise RuntimeError("消息不存在")

        with mock.patch.object(qq_api, "get_message", boom):
            block, images = qq_bot._resolve_quote(
                {"kind": "reply", "id": "999"}, 3000)
        self.assertEqual(block, "[引用的消息无法读取]")
        self.assertEqual(images, [])

    def test_group_card_name_wins_over_nickname(self):
        msg = {"sender": {"card": "群名片", "nickname": "真名"},
               "message": [{"type": "text", "data": {"text": "在吗"}}]}
        with mock.patch.object(qq_api, "get_message", lambda mid, **kw: msg):
            block, _ = qq_bot._resolve_quote({"kind": "reply", "id": "1"}, 3000)
        self.assertIn("群名片", block)
        self.assertNotIn("真名", block)

    def test_long_quote_is_truncated(self):
        """引用可能是一整条长回复，不封顶会把上下文预算吃光。"""
        msg = self._msg("长" * 500)
        with mock.patch.object(qq_api, "get_message", lambda mid, **kw: msg):
            block, _ = qq_bot._resolve_quote({"kind": "reply", "id": "1"}, 100)
        self.assertIn("过长已截断", block)
        self.assertLess(len(block), 200)

    def test_forward_uses_inline_nodes_without_api_call(self):
        """forward 段自带 content 时不该再跑一次 HTTP。"""
        nodes = [{"sender": {"nickname": "甲"},
                  "message": [{"type": "text", "data": {"text": "在吗"}}]},
                 {"sender": {"nickname": "乙"},
                  "message": [{"type": "text", "data": {"text": "在"}}]}]
        called = []
        with mock.patch.object(qq_api, "get_forward_msg",
                               lambda fid, **kw: called.append(fid) or {}):
            block, _ = qq_bot._resolve_quote(
                {"kind": "forward", "id": "768", "nodes": nodes}, 3000)
        self.assertEqual(called, [], "自带 content 时不该再请求一次")
        self.assertTrue(block.startswith("[转发的聊天记录]"))
        self.assertIn("甲：在吗", block)
        self.assertIn("乙：在", block)

    def test_forward_falls_back_to_api_without_inline_nodes(self):
        data = {"messages": [{"sender": {"nickname": "甲"},
                              "message": [{"type": "text",
                                           "data": {"text": "在吗"}}]}]}
        with mock.patch.object(qq_api, "get_forward_msg",
                               lambda fid, **kw: data):
            block, _ = qq_bot._resolve_quote(
                {"kind": "forward", "id": "768", "nodes": None}, 3000)
        self.assertIn("甲：在吗", block)

    def test_forward_fetch_failure_degrades(self):
        def boom(fid, **kw):
            raise RuntimeError("找不到相关的聊天记录")

        with mock.patch.object(qq_api, "get_forward_msg", boom):
            block, _ = qq_bot._resolve_quote(
                {"kind": "forward", "id": "1", "nodes": None}, 3000)
        self.assertEqual(block, "[转发的聊天记录无法读取]")

    def test_embedded_quote_is_not_expanded(self):
        """只展开一层：被引用的消息自己又带 reply 段时，不再往下追。

        否则「引用了引用」会一层层查下去，既慢又可能绕成环。
        """
        msg = {"sender": {"nickname": "甲"},
               "message": [{"type": "reply", "data": {"id": "2"}},
                           {"type": "text", "data": {"text": "在吗"}}]}
        calls = []
        with mock.patch.object(qq_api, "get_message",
                               lambda mid, **kw: calls.append(mid) or msg):
            block, _ = qq_bot._resolve_quote({"kind": "reply", "id": "1"}, 3000)
        self.assertEqual(calls, ["1"], "不该递归拉取里层的引用")
        self.assertEqual(block, "[引用 甲 的消息] 在吗")


# ─── 触发判定 ───────────────────────────────────────

class ShouldReplyTest(unittest.TestCase):
    def setUp(self):
        for name, value in (
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

    def _override(self, name, value):
        p = mock.patch.object(qq_bot, name, value)
        p.start()          # addCleanup 是后进先出，后打的补丁先撤销
        self.addCleanup(p.stop)

    def _reply(self, target="group", target_id="9", text="你好",
               at_me=False, user_id="1"):
        return qq_bot._should_reply({"user_id": user_id}, target, target_id,
                                    text, at_me)

    def test_private_replies_by_default(self):
        ok, reason = self._reply(target="private", target_id="1")
        self.assertTrue(ok)
        self.assertIn("私聊", reason)

    def test_private_can_be_turned_off(self):
        self._override("QQ_PRIVATE_ENABLE", False)
        ok, _ = self._reply(target="private", target_id="1")
        self.assertFalse(ok)

    def test_private_without_text_is_skipped(self):
        # 只发了个表情或图片、没有文字时不去打扰模型
        ok, _ = self._reply(target="private", target_id="1", text="")
        self.assertFalse(ok)

    def test_group_requires_at_by_default(self):
        ok, _ = self._reply(text="随便聊聊")
        self.assertFalse(ok)
        ok, _ = self._reply(text="你好", at_me=True)
        self.assertTrue(ok)

    def test_group_full_mode_replies_without_at(self):
        self._override("QQ_GROUP_AT_ONLY", False)
        ok, _ = self._reply(text="随便聊聊")
        self.assertTrue(ok)

    def test_keyword_bypasses_at_requirement(self):
        self._override("QQ_GROUP_KEYWORDS", ["小助手"])
        ok, reason = self._reply(text="小助手在吗")
        self.assertTrue(ok)
        self.assertIn("关键词", reason)

    def test_group_whitelist_blocks_other_groups(self):
        self._override("QQ_WHITELIST_GROUPS", ["9"])
        ok, _ = self._reply(target_id="8", text="你好", at_me=True)
        self.assertFalse(ok)
        ok, _ = self._reply(target_id="9", text="你好", at_me=True)
        self.assertTrue(ok)

    def test_user_whitelist_blocks_other_users(self):
        self._override("QQ_WHITELIST_USERS", ["1"])
        ok, _ = self._reply(target="private", target_id="2", user_id="2")
        self.assertFalse(ok)

    def test_blacklist_beats_whitelist(self):
        self._override("QQ_WHITELIST_USERS", ["1"])
        self._override("QQ_BLACKLIST_USERS", ["1"])
        ok, _ = self._reply(target="private", target_id="1", user_id="1")
        self.assertFalse(ok)


class QuoteTriggerTest(unittest.TestCase):
    """只引用、不写字的消息也算「有内容」。

    「引用一条 + @机器人 + 直接发送」是常见用法，以前 has_content 只看
    text 和图，这种消息会被整个丢掉，用户等半天没反应。
    """

    def setUp(self):
        for name, value in (
            ("QQ_GROUP_AT_ONLY", True),
            ("QQ_GROUP_KEYWORDS", []),
            ("QQ_WHITELIST_GROUPS", []),
            ("QQ_BLACKLIST_USERS", []),
        ):
            p = mock.patch.object(qq_bot, name, value)
            p.start()
            self.addCleanup(p.stop)

    def _reply(self, text="", at_me=False, has_quote=False, has_image=False):
        return qq_bot._should_reply({"user_id": "1"}, "group", "9", text,
                                    at_me, has_image, has_quote)

    def test_quote_counts_as_content_when_at(self):
        ok, _ = self._reply(at_me=True, has_quote=True)
        self.assertTrue(ok)

    def test_empty_without_quote_is_still_skipped(self):
        ok, _ = self._reply(at_me=True)
        self.assertFalse(ok)

    def test_quote_does_not_bypass_the_at_requirement(self):
        """引用了消息但没 @ 也没命中关键词时，群里仍然不理。

        触发规则只在「这条算不算有内容」上放宽，没有变宽到「引用即唤醒」。
        """
        ok, reason = self._reply(has_quote=True)
        self.assertFalse(ok)
        self.assertIn("未 @", reason)


class RunTurnQuoteTest(unittest.TestCase):
    """展开出来的引用块拼在正文前面，让模型先看到「在说哪句」。"""

    def _run(self, batch, get_message):
        seen = {}

        def fake_stream(text, history, agent_id=None, image=None, **kw):
            seen["text"] = text
            return iter(())

        runner = qq_bot.SessionRunner(None, "group_9", "group", "9")
        with mock.patch.object(qq_bot, "run_agent_stream", fake_stream), \
             mock.patch.object(qq_bot, "load_history", lambda *a, **k: []), \
             mock.patch.object(qq_bot, "_ensure_system_prompt",
                               lambda *a: None), \
             mock.patch.object(qq_bot, "save_history", lambda *a, **k: None), \
             mock.patch.object(qq_api, "get_message", get_message):
            runner._run_turn(batch)
        return seen.get("text")

    def test_quote_block_is_prepended_to_text(self):
        msg = {"sender": {"nickname": "温知澄"},
               "message": [{"type": "text",
                            "data": {"text": "我机器人弄不进来"}}]}
        text = self._run(
            [{"text": "233：这句怎么回", "sender": "233", "images": [],
              "quotes": [{"kind": "reply", "id": "1"}]}],
            lambda mid, **kw: msg)
        self.assertEqual(
            text, "[引用 温知澄 的消息] 我机器人弄不进来\n\n233：这句怎么回")

    def test_failed_quote_still_lets_the_turn_run(self):
        def boom(mid, **kw):
            raise RuntimeError("消息不存在")

        text = self._run(
            [{"text": "这句怎么回", "sender": "", "images": [],
              "quotes": [{"kind": "reply", "id": "999"}]}],
            boom)
        self.assertEqual(text, "[引用的消息无法读取]\n\n这句怎么回")


class DispatchQuoteTest(unittest.TestCase):
    """事件进来时要把引用来源一并交给会话执行器。

    这一环以前是断的：_parse_segments 不认识 reply / forward 段，引用被
    静默丢掉，模型只看到一句无头无尾的问话。
    """

    def setUp(self):
        for name, value in (
            ("QQ_GROUP_AT_ONLY", True),
            ("QQ_GROUP_KEYWORDS", []),
            ("QQ_WHITELIST_GROUPS", []),
            ("QQ_BLACKLIST_USERS", []),
        ):
            p = mock.patch.object(qq_bot, name, value)
            p.start()
            self.addCleanup(p.stop)

    def _dispatch(self, segs, at=True):
        captured = {}

        class FakeRunner:
            def submit(self, text, sender, images, quotes=None):
                captured.update(text=text, sender=sender,
                                images=images, quotes=quotes)

        msg = list(segs)
        if at:
            msg = [{"type": "at", "data": {"qq": "111"}}] + msg
        ev = {"post_type": "message", "message_type": "group",
              "self_id": 111, "user_id": 1, "group_id": "9",
              "sender": {"nickname": "张三"}, "message": msg}
        bot = qq_bot.QQBot()
        bot._runner_for = lambda key, t, tid: FakeRunner()
        bot._dispatch(json.dumps(ev))
        return captured

    def test_quote_is_passed_to_runner(self):
        got = self._dispatch([
            {"type": "reply", "data": {"id": "153"}},
            {"type": "text", "data": {"text": "这句怎么回"}},
        ])
        self.assertEqual(got["text"], "张三：这句怎么回")
        self.assertEqual(got["quotes"], [{"kind": "reply", "id": "153"}])

    def test_quote_only_message_reaches_runner_without_prefix(self):
        """引用一条 + 不写字也要能进来，且不该留下「张三：」这种空归属。"""
        got = self._dispatch([{"type": "reply", "data": {"id": "153"}}])
        self.assertEqual(got["text"], "")
        self.assertEqual(got["quotes"], [{"kind": "reply", "id": "153"}])

    def test_at_without_any_content_is_still_ignored(self):
        self.assertEqual(self._dispatch([]), {})

    def test_quote_alone_without_at_is_ignored_in_group(self):
        """触发规则没有因为引用而放宽：群里不 @ 就还是不理。"""
        got = self._dispatch([
            {"type": "reply", "data": {"id": "1"}},
            {"type": "text", "data": {"text": "看看"}},
        ], at=False)
        self.assertEqual(got, {})


# ─── 生图结果里的图片地址 ───────────────────────────

class ImagePathExtractTest(unittest.TestCase):
    def test_extracts_relative_image_urls(self):
        result = "生成成功！seed: 1\n图片地址:\n/api/image/a.png\n/api/image/b.png"
        self.assertEqual(qq_bot._IMAGE_PATH_RE.findall(result), ["a.png", "b.png"])

    def test_ignores_text_without_image(self):
        self.assertEqual(qq_bot._IMAGE_PATH_RE.findall("生成失败"), [])


# ─── 回发去重与图片投递 ─────────────────────────────

class DeliverTest(unittest.TestCase):
    """本轮调过 send_qq_message 就不再自动回发正文，否则同一句话发两遍。"""

    def _runner(self):
        return qq_bot.SessionRunner(None, "group_9", "group", "9")

    def test_tool_sent_skips_auto_reply(self):
        sent = []
        with mock.patch.object(
                qq_api, "send_group",
                lambda gid, text, limit=None: (sent.append(text), 1)[1]):
            self._runner()._deliver(True, "工具已经发过了", [])
        self.assertEqual(sent, [])

    def test_no_tool_send_replies_normally(self):
        sent = []
        with mock.patch.object(
                qq_api, "send_group",
                lambda gid, text, limit=None: (sent.append(text), 1)[1]):
            self._runner()._deliver(False, "正常回复", [])
        self.assertEqual(sent, ["正常回复"])

    def test_empty_reply_is_not_sent(self):
        sent = []
        with mock.patch.object(
                qq_api, "send_group",
                lambda gid, text, limit=None: (sent.append(text), 1)[1]):
            self._runner()._deliver(False, "   ", [])
        self.assertEqual(sent, [])

    def test_images_go_out_as_comfy_view_urls(self):
        seen = []
        with mock.patch.object(
                qq_api, "send_image",
                lambda target, tid, path, caption="": (seen.append((target, path)), 1)[1]):
            self._runner()._deliver(True, "", ["a.png"])
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0][0], "group")
        self.assertIn("/view?filename=a.png", seen[0][1])

    def test_private_runner_uses_private_send(self):
        sent = []
        runner = qq_bot.SessionRunner(None, "private_1", "private", "1")
        with mock.patch.object(
                qq_api, "send_private",
                lambda uid, text, limit=None: (sent.append(uid), 1)[1]):
            runner._deliver(False, "回你", [])
        self.assertEqual(sent, ["1"])


# ─── 主动发送工具 ───────────────────────────────────

class SendQqMessageToolTest(unittest.TestCase):
    def setUp(self):
        qq_api.clear_context()
        self.addCleanup(qq_api.clear_context)

    def test_uses_current_context_when_target_omitted(self):
        qq_api.bind_context("group_9", "group", "9")
        calls = []
        with mock.patch.object(
                qq_api, "send_group",
                lambda gid, text, limit=None: (calls.append((gid, text)), 1)[1]):
            out = send_qq_message._send_qq_message("你好")
        self.assertEqual(calls, [("9", "你好")])
        self.assertIn("已发送", out)

    def test_explicit_target_overrides_context(self):
        qq_api.bind_context("group_9", "group", "9")
        calls = []
        with mock.patch.object(
                qq_api, "send_private",
                lambda uid, text, limit=None: (calls.append((uid, text)), 1)[1]):
            send_qq_message._send_qq_message("悄悄话", target="private",
                                             target_id="123")
        self.assertEqual(calls, [(123, "悄悄话")])

    def test_half_specified_target_is_completed_from_context(self):
        # 只给 target_id 不给 target 时，用当前会话的类型补齐
        qq_api.bind_context("group_9", "group", "9")
        calls = []
        with mock.patch.object(
                qq_api, "send_group",
                lambda gid, text, limit=None: (calls.append(gid), 1)[1]):
            send_qq_message._send_qq_message("转发", target_id="77")
        self.assertEqual(calls, [77])

    def test_without_context_and_target_reports_error(self):
        # 网页端调用又不给目标：不该静默发到随便哪里
        out = send_qq_message._send_qq_message("你好")
        self.assertIn("没有指定发送目标", out)

    def test_non_numeric_target_id_is_reported(self):
        out = send_qq_message._send_qq_message("x", target="group",
                                               target_id="abc")
        self.assertIn("必须是数字", out)

    def test_bad_target_name_is_reported(self):
        out = send_qq_message._send_qq_message("x", target="channel",
                                               target_id="1")
        self.assertIn("group 或 private", out)

    def test_empty_message_is_reported(self):
        qq_api.bind_context("group_9", "group", "9")
        self.assertIn("不能为空", send_qq_message._send_qq_message("   "))

    def test_send_failure_is_reported_not_raised(self):
        # 工具里抛异常会被 execute_tool 包成"工具执行失败"，这里自己先说清楚
        qq_api.bind_context("group_9", "group", "9")

        def boom(gid, text, limit=None):
            raise RuntimeError("连不上协议端")

        with mock.patch.object(qq_api, "send_group", boom):
            out = send_qq_message._send_qq_message("你好")
        self.assertIn("发送失败", out)
        self.assertIn("连不上协议端", out)


# ─── 线程本地上下文 ─────────────────────────────────

class ThreadLocalContextTest(unittest.TestCase):
    def setUp(self):
        qq_api.clear_context()
        self.addCleanup(qq_api.clear_context)

    def test_context_isolated_between_threads(self):
        # worker 线程是复用的，一个会话的上下文不能串到另一个会话
        import threading
        seen = {}

        def worker():
            seen["ctx"] = qq_api.current_context()

        qq_api.bind_context("group_9", "group", "9")
        t = threading.Thread(target=worker)
        t.start()
        t.join()
        self.assertEqual(seen["ctx"], (None, None))

    def test_clear_context_removes_binding(self):
        qq_api.bind_context("group_9", "group", "9")
        self.assertEqual(qq_api.current_context(), ("group", "9"))
        qq_api.clear_context()
        self.assertEqual(qq_api.current_context(), (None, None))


# ─── 连接层：代理绕过 + 首次失败重试 ──────────────────

class ConnectRobustnessTest(unittest.TestCase):
    """这两条都是真机上炸出来的，不是假想问题。

    本机装了 Clash 这类工具时，代理会写进环境变量与注册表，于是连
    127.0.0.1 的回环请求也被送去代理，换回 502。更麻烦的是 websockets
    把 InvalidProxyStatus 当成不可重试的致命错误直接抛出（它只重试
    ConnectionRefusedError 这类），进程当场就没了 —— 真机日志里正是
    这条路径，而不是普通的连不上。

    所以两条都要治：HTTP 侧 trust_env=False，WS 侧显式 proxy=None。
    外层 while 重试是兜底，防的是同类「不可重试异常」再出现。
    """

    def test_http_session_bypasses_system_proxy(self):
        self.assertFalse(qq_api._session.trust_env,
                         "trust_env 必须为 False，否则会被系统代理接管")

    def test_ws_disables_proxy(self):
        self.assertTrue(qq_bot._WS_SUPPORTS_NO_PROXY,
                        "当前 websockets 版本应支持 proxy=None")

    def test_first_connect_failure_retries_instead_of_crashing(self):
        calls = []

        def fake_connect(url, **kw):
            calls.append(kw)
            raise OSError("connection refused")

        async def scenario():
            bot = qq_bot.QQBot()
            with mock.patch.object(qq_bot, "ws_connect", fake_connect), \
                 mock.patch.object(qq_api, "check_alive",
                                   side_effect=OSError("probe failed")), \
                 mock.patch.object(qq_bot, "_RECONNECT_MIN", 0.01), \
                 mock.patch.object(qq_bot, "_RECONNECT_MAX", 0.01):
                try:
                    await asyncio.wait_for(bot.run(), timeout=0.15)
                except asyncio.TimeoutError:
                    pass                # 预期：一直在重试，所以超时

        asyncio.run(scenario())
        self.assertGreaterEqual(len(calls), 3, "首次连不上时没有重试")
        self.assertIsNone(calls[0].get("proxy"), "未显式关闭代理")


# ─── 单实例保护 ─────────────────────────────────────

class SingleInstanceTest(unittest.TestCase):
    """两个适配层同时连着 NapCat 时，同一条 QQ 消息会被回两遍，必须拦住。"""

    def setUp(self):
        self._saved = qq_bot._SINGLETON_HANDLE
        qq_bot._SINGLETON_HANDLE = None

    def tearDown(self):
        self._release()
        qq_bot._SINGLETON_HANDLE = self._saved

    def _release(self):
        """关掉当前句柄 —— Windows 上文件被占用时临时目录删不掉。"""
        if qq_bot._SINGLETON_HANDLE is not None:
            qq_bot._SINGLETON_HANDLE.close()
            qq_bot._SINGLETON_HANDLE = None

    def test_second_acquire_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(qq_bot, "BASE_DIR", d):
                self.assertTrue(qq_bot._acquire_single_instance())
                self.assertFalse(qq_bot._acquire_single_instance())
            self._release()

    def test_lock_released_after_handle_closed(self):
        """进程退出后锁要能被下一个进程拿到，不能残留死锁。"""
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(qq_bot, "BASE_DIR", d):
                self.assertTrue(qq_bot._acquire_single_instance())
                self._release()
                self.assertTrue(qq_bot._acquire_single_instance())
            self._release()

    def test_missing_msvcrt_does_not_block(self):
        """非 Windows 平台不做检查，不能因此起不来。"""
        with mock.patch.object(qq_bot, "msvcrt", None):
            self.assertTrue(qq_bot._acquire_single_instance())


if __name__ == "__main__":
    unittest.main()
