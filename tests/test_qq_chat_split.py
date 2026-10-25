# -*- coding: utf-8 -*-
"""群聊短句分条（`qq_api.split_chat`）的边界。

这个函数只在群聊正文出口用，职责是「把一段话分几次发」，**不改内容**。
所以这里锁的几乎全是"不许动"的边界：短句、URL、系统报错、条数上限。

它拆得对不对不重要（那是提示词的事），**拆完字不能少、不能多、不能乱序**
才重要——群里看到的是它改过的内容，比看到小作文糟得多。
"""

import re
import unittest

from app import qq_api


def _flat(parts):
    """去掉所有空白拼回去，只比内容不比排版（换行可能被合并成同一条）。"""
    return re.sub(r"\s+", "", "".join(parts))


class SplitChatTest(unittest.TestCase):

    def test_short_reply_is_untouched(self):
        msg = "好，画了，等两秒。"
        self.assertEqual(qq_api.split_chat(msg), [msg])

    def test_long_reply_is_split_but_never_loses_words(self):
        msg = ("这个我不能帮——攻破 NASA 官网属于未授权入侵，违法，"
               "而且我这边也没有联网或攻击工具，只有画图能力。"
               "如果你是想画一张相关的梗图，告诉我，我直接给你画。")
        parts = qq_api.split_chat(msg)
        self.assertGreater(len(parts), 1)
        self.assertEqual(_flat(parts), _flat([msg]))

    def test_newline_is_a_message_break(self):
        """提示词教它「回车换行 = 分条」，所以换行要原样变成两条消息。"""
        msg = "哈哈 灰狼本尊\n想换风格说一声"
        parts = qq_api.split_chat(msg)
        self.assertEqual(parts, ["哈哈 灰狼本尊", "想换风格说一声"])

    def test_short_lines_are_not_merged(self):
        """已有换行时不能再攒——攒回去就白拆了。"""
        msg = "？\n草\n行吧"
        self.assertEqual(qq_api.split_chat(msg), ["？", "草", "行吧"])

    def test_each_part_stays_short_when_sentences_allow(self):
        msg = "。".join("这是第%d句话写得稍长一点" % i for i in range(3)) + "。"
        parts = qq_api.split_chat(msg)
        self.assertGreater(len(parts), 1)
        for p in parts:
            self.assertLessEqual(len(p), qq_api.QQ_CHAT_SPLIT_CHARS + 12)

    def test_parts_are_capped(self):
        """条数上限：宁可最后一条长，也不许把群刷屏。"""
        msg = "。".join("这是第%d句话内容" % i for i in range(30)) + "。"
        parts = qq_api.split_chat(msg)
        self.assertLessEqual(len(parts), qq_api.QQ_CHAT_SPLIT_MAX_PARTS)
        self.assertEqual(_flat(parts), _flat([msg]))

    def test_url_blocks_splitting(self):
        """URL 里的 ? & 会被当句读切，宁可整段发。"""
        msg = ("你想用这个接口的话，地址是 https://example.com/api?model=x&size=2 "
               "把它填进配置里就行，别的都不用改，记得保存之后重启一下服务。")
        self.assertEqual(qq_api.split_chat(msg), [msg])

    def test_error_notice_blocks_splitting(self):
        msg = "错误：群里不画连续漫画，一次十张会把群刷屏，这个群只能画单张。"
        self.assertEqual(qq_api.split_chat(msg), [msg])

    def test_compass_list_is_sent_whole(self):
        """🧭 开头 = 渠道清单这类结构化回复：一行一项是排版，不是分条意图。

        28 行的清单按 max_parts=3 会切成「两行 + 一大坨」，比原样发更难读。
        `/更多渠道` 命令回执和 AI 被问「有哪些渠道」时的回答都靠这个前缀。
        """
        from app import direct_gen
        msg = direct_gen.MORE_CHAN_TEXT
        self.assertEqual(qq_api.split_chat(msg), [msg])
        # 不带前缀的同样内容照旧拆——暗号必须是显式的
        self.assertGreater(len(qq_api.split_chat(msg[2:])), 1)

    def test_empty(self):
        self.assertEqual(qq_api.split_chat(""), [])
        self.assertEqual(qq_api.split_chat("   "), [])


if __name__ == "__main__":
    unittest.main()
