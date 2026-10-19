# -*- coding: utf-8 -*-
"""QQ 造卡：识别、卡片组装、消息账本、重画。

不测真发消息——`qq_api` 一律 mock 掉。要验证的是「一条指令进来，出去的是
一张合并转发卡片，而且账记得上」。
"""

import sys
import unittest
from unittest import mock

sys.path.insert(0, ".")

from app import card_bot as cb
from app import direct_gen


def _card(**kw):
    card = {
        "name": "沈念", "gender": "女",
        "categories": ["女性向", "原创", "crush"],
        "tags": ["傲娇"],
        "greeting": "（她把书包甩到肩上）……你站这儿干嘛。",
        "intro": "高二学生，嘴比脑子快。",
        "speech": "说话爱用短句，语气冲。",
        "memory": "从初一开始同班。",
        "rules": "永远不主动结束对话。",
        "cover": "1girl, black hair",
        "note": "补了名字和年级",
    }
    card.update(kw)
    return card


class ParseTest(unittest.TestCase):
    def test_channel_comes_first(self):
        self.assertEqual(cb.parse("anima线条 造卡 一个傲娇的女高中生"),
                         (True, "anima-line", "一个傲娇的女高中生"))

    def test_channel_is_optional(self):
        self.assertEqual(cb.parse("造卡 一个话很少的图书管理员"),
                         (True, cb.DEFAULT_SKILL, "一个话很少的图书管理员"))

    def test_chinese_channel_name_is_understood(self):
        self.assertEqual(cb.parse("krea2 造卡 古代宫廷里的女帝"),
                         (True, "krea2-general", "古代宫廷里的女帝"))

    def test_no_spaces_at_all(self):
        """用户手打经常一个空格都不给。"""
        self.assertEqual(cb.parse("anima线条造卡一个傲娇女高中生"),
                         (True, "anima-line", "一个傲娇女高中生"))

    def test_hash_prefix_is_accepted(self):
        self.assertEqual(cb.parse("#造卡 温柔学姐"), (True, cb.DEFAULT_SKILL, "温柔学姐"))

    def test_missing_brief_returns_usage(self):
        hit, skill, msg = cb.parse("造卡")
        self.assertTrue(hit)
        self.assertEqual(skill, "")
        self.assertIn("造卡这样用", msg)

    def test_asking_how_it_works_returns_usage_not_a_card(self):
        """「造卡怎么用」里的「怎么用」不是人物描述。

        不挡的话它会当成 brief 去画一张主题是「怎么用」的卡。
        """
        for text in ("造卡怎么用", "造卡怎么弄", "造卡是什么玩意", "造卡 猫"):
            with self.subTest(text=text):
                hit, skill, msg = cb.parse(text)
                self.assertTrue(hit)
                self.assertEqual(skill, "", "被当成需求了：%s" % text)
                self.assertIn("造卡这样用", msg)

    def test_two_character_brief_still_works(self):
        """别把门槛抬太高：「造卡 女帝」这种极简需求是合法的。"""
        self.assertEqual(cb.parse("造卡 女帝"), (True, cb.DEFAULT_SKILL, "女帝"))

    def test_cloud_channel_is_refused_with_a_reason(self):
        """NAI 不读 workflow，尺寸/步数全在云端请求里——造卡这条路走不通。"""
        hit, skill, msg = cb.parse("nai 造卡 一个女孩")
        self.assertTrue(hit)
        self.assertEqual(skill, "")
        self.assertIn("nai", msg)

    def test_plain_draw_request_is_not_a_card(self):
        self.assertEqual(cb.parse("给我画一个女孩"), (False, "", ""))

    def test_channel_before_the_word_still_counts(self):
        """渠道写在「造卡」前面也认，需求只取后面那段。"""
        self.assertEqual(cb.parse("krea2 造卡 一个女帝"),
                         (True, "krea2-general", "一个女帝"))

    def test_channel_after_the_word_also_counts(self):
        self.assertEqual(cb.parse("造卡 anima线条 一个女高中生"),
                         (True, "anima-line", "一个女高中生"))

    def test_group_nickname_prefix_is_dropped(self):
        """群消息进来时 qq_bot 会拼「昵称：」——那不是人设的一部分。

        2026-10-19 实测：拼进去的「233：」加上对方喊的机器人昵称一起被模型
        当成角色名，卡就叫「大大怪」。
        """
        got = cb.parse("233：大大怪，造卡，👩\u200d🦱 妹妹闺蜜 设定：金发双马尾")
        self.assertEqual(got[0], True)
        self.assertEqual(got[1], cb.DEFAULT_SKILL)
        self.assertEqual(got[2], "👩\u200d🦱 妹妹闺蜜 设定：金发双马尾")
        self.assertNotIn("大大怪", got[2])
        self.assertNotIn("233", got[2])

    def test_wake_word_before_the_command_is_dropped(self):
        self.assertEqual(cb.parse("大大怪 造卡 一个傲娇的女高中生"),
                         (True, cb.DEFAULT_SKILL, "一个傲娇的女高中生"))

    def test_word_inside_the_brief_is_kept(self):
        """需求里自己提到「造卡」时，只按**第一个**切，后面的原样留着。"""
        self.assertEqual(cb.parse("anima线条 造卡 一个会造卡的魔法师"),
                         (True, "anima-line", "一个会造卡的魔法师"))


class MenuTest(unittest.TestCase):
    """三份回执文案里都得有造卡。

    功能做完了但菜单里不提，等于没做——用户根本不会想到去喊这两个字。
    「重新生成」也一起钉住：那是重画那条路的唯一入口。
    """

    def test_every_menu_tells_you_how(self):
        for name in ("MENU_TEXT", "GUIDE_TEXT", "MORE_CHAN_TEXT"):
            text = getattr(direct_gen, name)
            with self.subTest(menu=name):
                self.assertIn("造卡", text)
                self.assertIn("重新生成", text)

    def test_menu_shows_the_default_channel(self):
        """不写渠道走哪条得写出来，否则用户会以为必须点名。"""
        self.assertIn(cb.DEFAULT_SKILL, direct_gen.MENU_TEXT)
        self.assertIn(cb.DEFAULT_SKILL, direct_gen.GUIDE_TEXT)


class RedoWordTest(unittest.TestCase):
    def test_common_words(self):
        for w in ("重新生成", "换一张", "重画", "再来一张"):
            self.assertTrue(cb.wants_redo(w), w)

    def test_new_request_is_not_a_redo(self):
        """带新需求的不是重画，那种要走正常生图。"""
        self.assertFalse(cb.wants_redo("再画一张猫"))


class NodesTest(unittest.TestCase):
    def test_image_and_every_field_land_in_one_card(self):
        nodes = cb.build_nodes(_card(), "C:/x/cover.png", 123, "anima-line", "K1")
        # 标题 + 封面 + 基本信息 + 五段 + 补了什么 + 脚注
        self.assertEqual(len(nodes), 1 + 1 + 1 + 5 + 1 + 1)
        for node in nodes:
            self.assertEqual(node["type"], "node")
            self.assertIn("content", node["data"])
        types = [seg["type"] for seg in nodes[1]["data"]["content"]]
        self.assertEqual(types, ["image"])

    def test_sex_and_categories_are_visible(self):
        """性别/分类/标签是表单上勾的，卡片里也得看得见，否则还得开网页对。"""
        nodes = cb.build_nodes(_card(), None, 1, "anima-line", "K1")
        texts = " ".join(s["data"]["text"] for n in nodes
                         for s in n["data"]["content"] if s["type"] == "text")
        for bit in ("基本信息", "性别：女", "女性向", "傲娇"):
            self.assertIn(bit, texts)

    def test_blocked_cover_leaves_a_note_instead_of_the_image(self):
        nodes = cb.build_nodes(_card(), None, 1, "anima-line", "K1")
        texts = [s["data"]["text"] for n in nodes for s in n["data"]["content"]
                 if s["type"] == "text"]
        self.assertTrue(any("没过审核" in t for t in texts))
        self.assertFalse(any(s["type"] == "image"
                             for n in nodes for s in n["data"]["content"]))

    def test_footer_carries_the_recipte(self):
        """脚注必须写 seed 和卡号：用户照着念也能复现，我们靠它排查。"""
        nodes = cb.build_nodes(_card(), "C:/x.png", 987654, "krea2-general", "K2026")
        footer = nodes[-1]["data"]["content"][0]["data"]["text"]
        self.assertIn("987654", footer)
        self.assertIn("K2026", footer)
        self.assertIn("krea2-general", footer)

    def test_redo_card_does_not_repeat_the_settings(self):
        """重画只发图：人设已经看过一遍，再刷五段等于让用户重读一遍。

        用户原话「引用这个图片…正常去生图即可，不需要再重复发送这个角色卡
        的明细」。
        """
        full = cb.build_nodes(_card(), "C:/x.png", 1, "anima-line", "K1")
        redo = cb.build_nodes(_card(), "C:/x.png", 1, "anima-line", "K1", full=False)
        self.assertLess(len(redo), len(full))
        self.assertEqual(len(redo), 3, "标题 + 图 + 脚注")
        texts = " ".join(s["data"]["text"] for n in redo
                         for s in n["data"]["content"] if s["type"] == "text")
        for label in ("开场白", "人物简介", "说话风格", "背景记忆", "补充规则"):
            self.assertNotIn(label, texts)
        # 图还在，脚注还在（看 seed 是新生成的那一个）
        self.assertTrue(any(s["type"] == "image"
                            for n in redo for s in n["data"]["content"]))
        self.assertIn("K1", texts)

    def test_redo_card_is_labelled(self):
        """重画版要有标记，否则群里两张卡片长得一样，分不清哪张是新的。"""
        redo = cb.build_nodes(_card(), "C:/x.png", 1, "anima-line", "K1", full=False)
        self.assertIn("重画", redo[0]["data"]["content"][0]["data"]["text"])


class LedgerTest(unittest.TestCase):
    def test_remember_then_recall(self):
        cb.remember("m-100", {"cid": "K1", "seed": 7, "skill": "anima-line"})
        self.assertEqual(cb.recall("m-100")["seed"], 7)

    def test_missing_id_is_skipped(self):
        """协议端没回 message_id 时不能崩，也不能写一条空键。"""
        before = len(cb._load_ledger())
        cb.remember("", {"cid": "K1"})
        self.assertEqual(len(cb._load_ledger()), before)

    def test_unknown_message_is_not_ours(self):
        self.assertEqual(cb.start_redo("group", "1", "not-a-card"), (False, ""))

    def test_card_gone_from_library(self):
        cb.remember("m-200", {"cid": "K-not-exist", "seed": 1,
                              "skill": "anima-line"})
        took, tip = cb.start_redo("group", "1", "m-200")
        self.assertTrue(took)
        self.assertIn("不在库里", tip)


class WorkTest(unittest.TestCase):
    """一条指令进来 → 一张合并转发出去 → 账记得上。"""

    def _patches(self, audit=True, render="C:/x/cover.png"):
        return [
            mock.patch.object(cb.card_maker, "draft", return_value=_card()),
            mock.patch.object(cb.card_maker, "save_card",
                              return_value={"id": "K-test-1"}),
            mock.patch.object(cb.card_maker, "render_cover", return_value=render),
            mock.patch.object(cb.card_maker, "update_card", return_value={}),
            mock.patch.object(cb, "_audit", return_value=audit),
            mock.patch.object(cb, "_say"),
            mock.patch.object(cb.qq_api, "send_forward",
                              return_value={"message_id": 555}),
        ]

    def test_sends_exactly_one_forward_and_records_the_message_id(self):
        ps = self._patches()
        started = [p.start() for p in ps]
        try:
            cb._work("group", "123", "anima-line", "一个傲娇的女高中生", "msg-1")
        finally:
            for p in reversed(ps):
                p.stop()
        self.assertEqual(started[-1].call_count, 1)
        rec = cb.recall("555")
        self.assertIsNotNone(rec, "没有记账：引用这张卡片就重画不出来了")
        self.assertEqual(rec["cid"], "K-test-1")
        self.assertEqual(rec["skill"], "anima-line")

    def test_blocked_cover_still_delivers_the_text(self):
        ps = self._patches(audit=False)
        started = [p.start() for p in ps]
        try:
            cb._work("group", "123", "anima-line", "一个女孩", "msg-2")
            nodes = started[-1].call_args[0][2]
        finally:
            for p in reversed(ps):
                p.stop()
        has_image = any(s["type"] == "image"
                        for n in nodes for s in n["data"]["content"])
        self.assertFalse(has_image, "审核拦下的图不该出现在卡片里")

    def test_draft_failure_says_so_and_sends_no_card(self):
        ps = self._patches()
        ps[0] = mock.patch.object(cb.card_maker, "draft",
                                  side_effect=RuntimeError("模型没回话"))
        started = [p.start() for p in ps]
        try:
            cb._work("group", "123", "anima-line", "一个女孩", "msg-3")
            self.assertEqual(started[-1].call_count, 0)
        finally:
            for p in reversed(ps):
                p.stop()

    def test_redo_changes_the_seed_but_keeps_the_card(self):
        """重画只换 seed，卡号和文案都不动（人设是用户认可过的那版）。"""
        with mock.patch.object(cb.card_maker, "render_cover",
                               return_value="C:/x/new.png") as render, \
                mock.patch.object(cb.card_maker, "update_card"), \
                mock.patch.object(cb, "_audit", return_value=True), \
                mock.patch.object(cb, "_say"), \
                mock.patch.object(cb.qq_api, "send_forward",
                                  return_value={"message_id": 556}) as fwd:
            cb._redo_work("group", "1", "K-test-1", _card(), "anima-line",
                          12345, "msg-4")
        seed = fwd.call_args[0][0] and cb.recall("556")["seed"]
        self.assertNotEqual(seed, 12345)
        self.assertEqual(cb.recall("556")["cid"], "K-test-1")
        self.assertEqual(render.call_args[0][0], "K-test-1")


if __name__ == "__main__":
    unittest.main()
