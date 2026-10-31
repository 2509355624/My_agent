"""`app/moss_bot.py` 的识别与回执文案测试（不出图、不连 QQ）。

出图那条链路靠 `tests/test_moss_api.py` 和手工实跑覆盖——这里只钉住
「什么消息该被接管、被接管成什么动作」这两件纯逻辑。

## 2026-10-31 改过三次，这只是最后那一版

第一次是加触发门槛（必须 @ 或带「大大怪」），第二次是**砍指令**：原来打算
做七个（`moss续集 5 <剧情>` 那种），改成三个裸词 `续集` / `重画` / `合集`，
靠「有没有引用」判断该不该接管。**第三次当晚就被用户推翻了**：

    你不能说续集、重画、合集，你必须得给我加上 moss。
    这 4 个字母必须加上来，这样我才能够精确地带走代码闸门。

理由不是误触发，是**判断权**：裸词必须靠「引用能不能对上账本」去猜，
猜宽了白跑一张 100 秒的图，猜窄了用户明明引用了却不理他。带上前缀就没有
猜的余地——**带 `moss` 就是指令，不带就是聊天**。

所以 `parse` 现在只做一件事：认词、切出载荷。**定位（引用/编号）在
`start` 里做**，失败必须明确报错，绝不猜一个系列往下画。

下面钉四条规矩：
  · 四个词都必须带 `moss`，裸词一律不接管；
  · 带 `moss` 的三个动作词无条件接管（哪怕没引用，也要回一句怎么用）；
  · 定位只认「引用 id / 引用正文里的编号 / 正文手打编号」，三条都不通就报错；
  · 报错要分清「没给编号」「编号错」「没这一页」。
"""

import os
import shutil
import tempfile
import unittest
from unittest import mock

from app import (image_out, moss_api, moss_bot, moss_comic, moss_msgs,
                 moss_series, qq_api)


class ParseTest(unittest.TestCase):
    """新开一条线：`moss漫画 <剧情>`，不需要引用。"""

    def _new(self, text, at_me=True):
        hit, act, payload, tip = moss_bot.parse(text, at_me=at_me)
        self.assertTrue(hit, text)
        self.assertEqual(act, "new", text)
        return payload, tip

    def test_plain(self):
        brief, tip = self._new("moss漫画 雨夜便利店，共撑一把伞")
        self.assertEqual(brief, "雨夜便利店，共撑一把伞")
        self.assertEqual(tip, "")

    def test_space_between_words(self):
        brief, _ = self._new("moss 漫画 一只橘猫跳上主人的胸口")
        self.assertEqual(brief, "一只橘猫跳上主人的胸口")

    def test_case_insensitive(self):
        brief, _ = self._new("MOSS漫画 雨夜")
        self.assertEqual(brief, "雨夜")

    def test_qq_nickname_prefix_is_dropped(self):
        """群消息前面带「昵称：」和机器人名字——都不能进剧情。

        造卡那边踩过：昵称被当成了角色名，卡就叫「大大怪」。
        这条不需要 at_me：文本里就有「大大怪」，本身满足门槛。
        """
        hit, act, brief, _ = moss_bot.parse("大大怪，moss漫画 雨夜便利店")
        self.assertTrue(hit)
        self.assertEqual(act, "new")
        self.assertEqual(brief, "雨夜便利店")

    def test_hash_prefix(self):
        brief, _ = self._new("#moss漫画 一句话剧情")
        self.assertEqual(brief, "一句话剧情")

    def test_bare_word_returns_usage(self):
        hit, act, payload, tip = moss_bot.parse("moss漫画", at_me=True)
        self.assertTrue(hit)
        self.assertEqual(act, "")            # 空动作 = 只回说明，不跑图
        self.assertEqual(payload, "")
        self.assertEqual(tip, moss_bot.USAGE)

    def test_question_is_not_a_brief(self):
        """问句不能当成剧情，否则会画一张叫「怎么用」的图。"""
        # 「怎么用」直接给说明书（比 USAGE 全）
        hit, act, payload, tip = moss_bot.parse("moss漫画 怎么用", at_me=True)
        self.assertTrue(hit)
        self.assertEqual((act, payload), ("", ""))
        self.assertEqual(tip, moss_bot.MANUAL)
        for text in ("moss漫画 支持什么", "moss漫画 是什么"):
            hit, act, payload, tip = moss_bot.parse(text, at_me=True)
            self.assertTrue(hit, text)
            self.assertEqual(act, "", text)
            self.assertEqual(payload, "", text)
            self.assertEqual(tip, moss_bot.USAGE, text)

    def test_one_char_is_not_a_brief(self):
        hit, act, _payload, tip = moss_bot.parse("moss漫画 a", at_me=True)
        self.assertTrue(hit)
        self.assertEqual(act, "")
        self.assertEqual(tip, moss_bot.USAGE)

    def test_other_commands_untouched(self):
        """silver漫画 是另一条路（多图批量），不能让这边吃掉。"""
        for text in ("今天天气不错", "silver漫画 10个 雷电将军", "anima 女骑士"):
            self.assertEqual(moss_bot.parse(text), (False, "", "", ""), text)


class GateTest(unittest.TestCase):
    """触发门槛（2026-10-31 用户要求）。

    只写「moss漫画」不接管；@ 了、或者句子里带「大大怪」，才接管。
    """

    def test_bare_word_without_at_or_name_is_ignored(self):
        for text in ("moss漫画 雨夜便利店", "moss小漫画 开门发现别的男人",
                     "moss 漫画 一只猫", "MOSS漫画 雨夜"):
            self.assertEqual(moss_bot.parse(text), (False, "", "", ""), text)

    def test_at_me_is_enough(self):
        hit, act, brief, _ = moss_bot.parse("moss漫画 雨夜便利店", at_me=True)
        self.assertTrue(hit)
        self.assertEqual(act, "new")
        self.assertEqual(brief, "雨夜便利店")

    def test_name_keyword_is_enough(self):
        for text in ("大大怪 moss漫画 雨夜便利店", "大大怪：moss漫画 雨夜便利店",
                     "喊一下大大怪，moss漫画 雨夜便利店"):
            hit, act, brief, _ = moss_bot.parse(text)
            self.assertTrue(hit, text)
            self.assertEqual(act, "new", text)
            self.assertEqual(brief, "雨夜便利店", text)

    def test_name_does_not_leak_into_brief(self):
        """「大大怪」在词前面，本身就在 rest 之外，不该混进剧情。"""
        _hit, _act, brief, _ = moss_bot.parse("大大怪 moss小漫画 开门发现别的男人")
        self.assertEqual(brief, "开门发现别的男人")

    def test_usage_tells_people_how_to_continue(self):
        """用法里必须写清「要引用」——这是续集唯一的使用方式。"""
        self.assertIn("引用", moss_bot.USAGE)
        self.assertIn("续集", moss_bot.USAGE)
        self.assertIn("重画", moss_bot.USAGE)
        self.assertIn("合集", moss_bot.USAGE)

    def test_chitchat_never_triggers(self):
        """「续集」「重画」「合集」在群里是常用词，没有 moss 前缀就必须放过。"""
        for text in ("这个续集不错啊", "重画一下吧", "合集发我看看",
                     "他续集拍得比第一部好", "把那个重画一遍"):
            self.assertEqual(moss_bot.parse(text), (False, "", "", ""), text)

    def test_bare_word_stays_quiet_even_with_a_real_quote(self):
        """**这条是 2026-10-31 第三次改动的核心。**

        前一版里「引用了一条真实回执 + 说 `续集`」是接管的。用户当晚否掉：
        裸词一旦要靠「引用对不对得上」来判断，代码就在猜；而群里有人
        引用了别的图又说「续集」，猜错就是一张白跑的图。
        现在一律要求带 `moss`，裸词哪怕引用对上了也不接管。
        """
        hit, act, _payload, _tip = moss_bot.parse(
            "续集 她走了", quote_id="MID-2",
            quote_text="%s · 第 02 页" % moss_series.code_of("ABCD", 2))
        self.assertEqual((hit, act), (False, ""))

    def test_prefixed_word_always_takes_over(self):
        """带 moss 前缀的四个词，只要喊了名字就接管（哪怕没引用，也回一句怎么用）。"""
        cases = {
            "moss续集 她走了": "continue",
            "moss 续集 她走了": "continue",
            "moss重画": "redo",
            "moss 重画": "redo",
            "moss合集": "album",
            "MOSS合集 1-5": "album",
        }
        for text, want in cases.items():
            hit, act, _payload, _tip = moss_bot.parse(text, at_me=True)
            self.assertTrue(hit, text)
            self.assertEqual(act, want, text)

    def test_prefix_alone_is_not_enough_in_a_group(self):
        """**2026-10-31 第二次收紧：带前缀也不再免喊名。**

        回执本身把 `moss续集` / `moss重画` / `moss合集` 印在群聊里，
        谁复述一句「moss合集」就被当成指令，回一条「认不出引用」的报错。
        现在四个词口径统一：不带 @ 也不喊「大大怪」就不接管。
        """
        for text in ("moss续集 她走了", "moss重画", "moss合集", "moss合集 1-5"):
            self.assertEqual(moss_bot.parse(text), (False, "", "", ""), text)
            # 喊了名字就认
            self.assertTrue(moss_bot.parse("大大怪 " + text)[0], text)
            self.assertTrue(moss_bot.parse(text, at_me=True)[0], text)

    def test_repeating_the_receipt_stays_quiet(self):
        """群友复述回执里的四个词，只要没在叫机器人就不理。"""
        for text in ("刚那张 moss合集 挺好用", "用 moss续集 就能接着画",
                     "moss重画 是换种子吧？"):
            self.assertEqual(moss_bot.parse(text), (False, "", "", ""), text)

    def test_usage_says_the_prefix_is_required(self):
        """用法里必须写明「这四个词要带 moss」，不然用户还会说裸词。"""
        self.assertIn("带 moss", moss_bot.USAGE)
        self.assertIn("moss续集", moss_bot.USAGE)


class ManualTest(unittest.TestCase):
    """说明书（2026-10-31 用户点名要的）：关键词触发，但同样要喊名字。"""

    def test_manual_by_keyword(self):
        for text in ("moss漫画说明书", "moss说明书", "moss小漫画说明书",
                     "moss漫画 说明书"):
            hit, act, payload, tip = moss_bot.parse(text, at_me=True)
            self.assertTrue(hit, text)
            self.assertEqual((act, payload), ("", ""), text)
            self.assertEqual(tip, moss_bot.MANUAL, text)

    def test_manual_needs_a_name_in_a_group(self):
        """群里光说「moss漫画说明书」不接管——说明书里自己就印着这个词。"""
        self.assertEqual(moss_bot.parse("moss漫画说明书"), (False, "", "", ""))
        self.assertTrue(moss_bot.parse("大大怪 moss漫画说明书")[0])

    def test_manual_is_never_drawn_as_a_comic(self):
        """回归：以前 `moss漫画说明书` 先命中 `moss漫画`，「说明书」被当成剧情。

        真跑起来就是一次 100~250 秒的出图，画一张叫「说明书」的图。
        """
        hit, act, payload, _tip = moss_bot.parse("moss漫画说明书", at_me=True)
        self.assertEqual((hit, act, payload), (True, "", ""))

    def test_manual_covers_everything_a_newcomer_asks(self):
        m = moss_bot.MANUAL
        for word in ("moss漫画", "moss续集", "moss重画", "moss合集"):
            self.assertIn(word, m)
        for must in ("引用", "编号", "私聊", "群聊", "六格", "大大怪"):
            self.assertIn(must, m)
        self.assertIn("moss重画", moss_bot.USAGE)
        self.assertIn("moss合集", moss_bot.USAGE)


class QuoteTest(unittest.TestCase):
    """定位：`moss续集` / `moss重画` / `moss合集` 要知道作用于哪一页。

    建一个真系列 + 一条真回执记录，模拟「用户引用了那页的图」。
    定位本身在 `start` 里（`moss_msgs.locate`），这里两头都钉：
    解析出的载荷对不对，以及定位失败时报的是哪一种错。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._old = (moss_series.STATE_ROOT, moss_series.INDEX_FILE,
                     moss_msgs.LEDGER_FILE)
        moss_series.STATE_ROOT = self.tmp
        moss_series.INDEX_FILE = os.path.join(self.tmp, "_index.json")
        moss_msgs.LEDGER_FILE = os.path.join(self.tmp, "_msgs.json")
        s = moss_series.open_new("group", "1103174141", title="醒来之后")
        self.sid = s["id"]
        self.p1 = moss_series.mutate(self.sid, lambda x: moss_series.add_page(
            x, parent_id="", task="T1", brief="开端", seed=1, summary="她推门进来"))
        self.p2 = moss_series.mutate(self.sid, lambda x: moss_series.add_page(
            x, parent_id=self.p1["id"], task="T2", brief="第二页", seed=2,
            summary="她拿伞"))
        moss_msgs.remember("MID-2", self.sid, self.p2["id"], 2)

    def tearDown(self):
        (moss_series.STATE_ROOT, moss_series.INDEX_FILE,
         moss_msgs.LEDGER_FILE) = self._old
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_continue_by_quote(self):
        hit, act, payload, tip = moss_bot.parse(
            "moss续集 她发现伞被人拿走了", at_me=True, quote_id="MID-2",
            quote_text="%s · 第 02 页 ·《醒来之后》" % moss_series.code_of(self.sid, 2))
        self.assertTrue(hit)
        self.assertEqual(act, "continue")
        self.assertEqual(payload, "她发现伞被人拿走了")
        self.assertEqual(tip, "")

    def test_redo_by_quote(self):
        hit, act, _payload, _tip = moss_bot.parse(
            "moss重画", at_me=True, quote_id="MID-2")
        self.assertTrue(hit)
        self.assertEqual(act, "redo")

    def test_album_by_quote(self):
        hit, act, _payload, _tip = moss_bot.parse(
            "moss合集", at_me=True, quote_id="MID-2")
        self.assertTrue(hit)
        self.assertEqual(act, "album")

    def test_album_range_is_kept(self):
        hit, act, payload, _tip = moss_bot.parse(
            "moss合集 1-10", at_me=True, quote_id="MID-2")
        self.assertTrue(hit)
        self.assertEqual(act, "album")
        self.assertEqual(payload, "1-10")

    def test_forward_loses_id_but_code_saves_it(self):
        """转发之后 `message_id` 变了（连 forward id 也是新的），靠正文明文里的编号兜底。

        真实链路：转发段的正文会被 `qq_bot._resolve_quote` 展开进
        `quoted_text`，卡片上那行 `S7K3 · 第 02 页` 就在里面。
        """
        code = moss_series.code_of(self.sid, 2)
        hit, act, _payload, _tip = moss_bot.parse(
            "moss续集 她走了", at_me=True, quote_id="MID-转发后不认识",
            quote_text="[转发的聊天记录]\n昵称：《醒来之后》· %s · 第 02 页 · 6 格" % code)
        self.assertTrue(hit)
        self.assertEqual(act, "continue")
        sid, pg, how = moss_msgs.locate("MID-转发后不认识",
                                        "[转发的聊天记录]\n《醒来之后》· %s" % code)
        self.assertEqual((sid, pg["n"], how), (self.sid, 2, "code"))

    def test_typed_code_locates_too(self):
        """手打编号：编号只用来定位，不许混进剧情。"""
        code = moss_series.code_of(self.sid, 2)
        hit, act, payload, _tip = moss_bot.parse(
            "moss续集 %s 她推门进来" % code, at_me=True)
        self.assertTrue(hit)
        self.assertEqual(act, "continue")
        self.assertIn(code, payload)          # parse 保留，start 靠它定位
        self.assertEqual(moss_series.strip_code(payload), "她推门进来")

    def test_span_helper(self):
        self.assertEqual(moss_bot._span("1-10", 20), (1, 10))
        self.assertEqual(moss_bot._span("8~3", 20), (3, 8))     # 反着写也认
        self.assertEqual(moss_bot._span("1-999", 20), (1, 20))  # 越界夹回总量
        self.assertEqual(moss_bot._span("", 20), (1, 20))       # 没范围就全篇


class MissNoteTest(unittest.TestCase):
    """定位失败必须说清是哪种失败（用户 2026-10-31 原话要的就是这个）。

    「认不出来」四个字对用户没用：不知道是自己没给、打错了、还是页不存在。
    """

    setUp = QuoteTest.setUp
    tearDown = QuoteTest.tearDown

    def note(self, quote_text="", payload=""):
        return moss_bot._miss_note(quote_text, payload)

    def test_no_code_teaches_how_to_give_one(self):
        text = self.note(payload="她走了")
        self.assertIn("引用", text)
        self.assertIn("S7K3-P05", text)

    def test_wrong_code_is_echoed_back(self):
        """打错的编号要原样贴回去——他自己一眼就能看出错在哪。"""
        text = self.note(payload="moss续集 ZZZZ-P09 她走了")
        self.assertIn("ZZZZ-P09", text)
        self.assertIn("打错", text)

    def test_existing_series_but_no_such_page(self):
        text = self.note(payload="moss续集 %s 她走了"
                         % moss_series.code_of(self.sid, 99))
        self.assertIn("一共 2 页", text)
        self.assertIn("99", text)

    def test_real_code_in_quote_text_is_located(self):
        """引用了正确编号就不该走到报错（这条防的是「报错文案抢跑」）。"""
        code = moss_series.code_of(self.sid, 2)
        sid, pg, how = moss_msgs.locate("", "《醒来之后》· %s · 第 02 页" % code)
        self.assertEqual((sid, pg["n"], how), (self.sid, 2, "code"))


class LayoutDefaultTest(unittest.TestCase):
    """单页默认六格，而且**不是**等分六格。"""

    def test_layout_is_six_panels(self):
        self.assertEqual(moss_bot.LAYOUT, "2t4b")
        self.assertEqual(moss_bot.PAGES, 1)
        self.assertEqual(len(moss_comic.LAYOUTS[moss_bot.LAYOUT][1]), 6)

    def test_default_is_not_the_equal_grid(self):
        """默认不能退回等分六格：等权平铺没有主次，字数预算也砍半。"""
        self.assertNotEqual(moss_bot.LAYOUT, "3x2")

    def test_usage_mentions_default_panels(self):
        self.assertIn("默认六格", moss_bot.USAGE)
        self.assertIn("五格", moss_bot.USAGE)
        self.assertIn("等分六格", moss_bot.USAGE)


class SmallComicWordTest(unittest.TestCase):
    """「moss小漫画」也走同一条路（2026-10-31 用户口头就这么说）。

    不额外维护第二个关键词：`小\\s*` 挂在 `moss\\s*` 后面，三种写法落一套逻辑。
    """

    def test_small_comic_hit(self):
        for text in ("moss小漫画 雨夜便利店", "moss 小漫画 雨夜便利店",
                     "moss 小 漫画 雨夜便利店", "MOSS小漫画 开门"):
            hit, act, payload, tip = moss_bot.parse(text, at_me=True)
            self.assertTrue(hit, text)
            self.assertEqual(act, "new", text)
            self.assertEqual(tip, "", text)
            self.assertTrue(payload, text)

    def test_small_comic_still_drops_prefix(self):
        _hit, _act, brief, _ = moss_bot.parse("大大怪：moss小漫画 开门发现别的男人")
        self.assertEqual(brief, "开门发现别的男人")

    def test_small_comic_bare_returns_usage(self):
        hit, act, payload, tip = moss_bot.parse("moss小漫画", at_me=True)
        self.assertTrue(hit)
        self.assertEqual(act, "")
        self.assertEqual(payload, "")
        self.assertEqual(tip, moss_bot.USAGE)


class DetectLayoutTest(unittest.TestCase):
    """用户在需求里说格数，就必须照着出（2026-10-31 用户要求）。

    这是**硬指定**不是建议：用户说四格出六格，就等于没听懂话。
    """

    def _pick(self, text):
        return moss_bot.detect_layout(text)

    def test_panel_count_words(self):
        cases = {
            "四格：知更鸟登台演唱": "2x2",
            "知更鸟登台演唱，画成四格": "2x2",
            # 裸说六格给「上二下四」，不是等分六格
            "六格 鲁迪推开洛琪希家的门": "2t4b",
            "两格：雨夜便利店": "2h",
            "三格 她走进房间": "3h",
        }
        for text, want in cases.items():
            lay, _clean, ok = self._pick(text)
            self.assertTrue(ok, text)
            self.assertEqual(lay, want, text)

    def test_equal_six_panels_must_be_asked_for(self):
        """要等分六格必须说出来：默认六格是有主次的「上二下四」。"""
        for text in ("等分六格 他愣在原地", "六格一样大 他愣在原地", "上三下三 他愣在原地"):
            lay, _clean, ok = self._pick(text)
            self.assertTrue(ok, text)
            self.assertEqual(lay, "3x2", text)

    def test_five_panels_defaults_to_the_writable_one(self):
        """裸说「五格」给 1t2m2b：四小格 512 宽，能正常写字。"""
        for text in ("五格：她站在门口", "五格 她站在门口", "5格 她站在门口"):
            lay, _clean, ok = self._pick(text)
            self.assertTrue(ok, text)
            self.assertEqual(lay, "1t2m2b", text)

    def test_descriptive_phrasing(self):
        cases = {
            "上面一大格 她推开门，发现屋里有别人": "1t2m2b",
            "上排三个小格 他愣住，然后转身": "3t2b",
            "上面两个大格 她和他隔着一道门": "2t4b",
            "用 1t2m2b 她坐在窗边": "1t2m2b",
            "3t2b 三个反应加两个大画面": "3t2b",
            "用 2t4b 她坐在窗边": "2t4b",
        }
        for text, want in cases.items():
            lay, _clean, ok = self._pick(text)
            self.assertTrue(ok, text)
            self.assertEqual(lay, want, text)

    def test_single_panel_needs_clear_signal(self):
        """「一格」这个词太日常，只有在句首或带指令词时才算版式。"""
        for text in ("一格 整页就是一个大特写", "一格：整页大特写", "画成一格 她回头"):
            lay, _clean, ok = self._pick(text)
            self.assertTrue(ok, text)
            self.assertEqual(lay, "single", text)

    def test_does_not_misfire_on_fiction(self):
        """剧情里出现「一格」不能当成版式指令，更不能被删掉。"""
        text = "他一格一格地爬上楼梯，气喘吁吁"
        lay, clean, ok = self._pick(text)
        self.assertFalse(ok)
        self.assertEqual(lay, moss_bot.LAYOUT)
        self.assertEqual(clean, text)       # 一个字都不许动

    def test_no_layout_word_keeps_brief_intact(self):
        for text in ("雨夜便利店，两个陌生人共用一把伞",
                     "鲁迪推开洛琪希家的门，发现屋里有个陌生男人"):
            lay, clean, ok = self._pick(text)
            self.assertFalse(ok)
            self.assertEqual(lay, moss_bot.LAYOUT)
            self.assertEqual(clean, text)

    def test_layout_word_is_stripped_from_brief(self):
        """版式词不能留在剧情里，否则会被当成画面内容写进 desc。"""
        cases = {
            "四格：知更鸟登台演唱": "知更鸟登台演唱",
            "知更鸟登台演唱，画成四格": "知更鸟登台演唱",
            "六格 鲁迪推开洛琪希家的门": "鲁迪推开洛琪希家的门",
            "等分六格 鲁迪推开洛琪希家的门": "鲁迪推开洛琪希家的门",
            "用 1t2m2b 她坐在窗边": "她坐在窗边",
        }
        for text, want in cases.items():
            _lay, clean, ok = self._pick(text)
            self.assertTrue(ok, text)
            self.assertEqual(clean, want, text)

    def test_every_picked_layout_exists(self):
        """认出来的键必须在 LAYOUTS / LAYOUT_BOXES 里都有，否则编译时 KeyError。"""
        for text in ("四格 x", "六格 x", "五格 x", "两格 x", "三格 x", "一格：x",
                     "上面一大格 x", "上排三个小格 x", "上面两个大格 x",
                     "等分六格 x"):
            lay, _clean, ok = self._pick(text)
            self.assertTrue(ok, text)
            self.assertIn(lay, moss_comic.LAYOUTS, text)
            self.assertIn(lay, moss_comic.LAYOUT_BOXES, text)


class LinesTest(unittest.TestCase):
    SPEC = {"pages": [{"n": 1, "layout": "2x2", "panels": [
        {"desc": "a", "caption": "雨夜，忘带伞。"},
        {"desc": "b", "bubble": "给你。"},
        {"desc": "c"},                                  # 无台词
        {"desc": "d", "sfx": "哗哗"},
    ]}]}

    def test_caption_wins_over_bubble(self):
        spec = {"pages": [{"panels": [{"caption": "旁白", "bubble": "台词"}]}]}
        self.assertIn("旁白", moss_bot._lines(spec))

    def test_every_panel_keeps_its_number(self):
        """没台词的格子补「—」——跳过会变成 1·2·4·，看着像少了一格。"""
        text = moss_bot._lines(self.SPEC)
        lines = text.split("\n")
        self.assertEqual(len(lines), 4)
        self.assertIn("1· 雨夜，忘带伞。", lines[0])
        self.assertIn("2· 给你。", lines[1])
        self.assertIn("3· —", lines[2])
        self.assertIn("4· 哗哗", lines[3])

    def test_empty_spec_does_not_crash(self):
        self.assertEqual(moss_bot._lines({}), "")
        self.assertEqual(moss_bot._lines({"pages": []}), "")


class ReceiptTest(unittest.TestCase):
    """一页 = **一条**消息（2026-10-31 用户报刷屏）。

    用户原话：「我一个生图结果，你给我2个回执，这是刷屏，你直接在这个
    `AAVU-P01 · 第 01 页 ·《早起的小猫》` … 里」。所以图、编号、指令、迁移
    提示全在同一张图的 caption 里，`_render_and_send` 收尾**不许再追发**文字。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._old = (moss_series.STATE_ROOT, moss_series.INDEX_FILE)
        moss_series.STATE_ROOT = self.tmp
        moss_series.INDEX_FILE = os.path.join(self.tmp, "_index.json")
        self.addCleanup(self._restore)
        s = moss_series.open_new("group", "1103174141", title="早起的小猫")
        self.sid = s["id"]
        self.page = moss_series.mutate(self.sid, lambda x: moss_series.add_page(
            x, parent_id="", task="T1", brief="猫醒了", seed=1110055932))

    def _restore(self):
        moss_series.STATE_ROOT, moss_series.INDEX_FILE = self._old
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, hint):
        sent = {"captions": [], "texts": []}
        spec = {"title": "早起的小猫",
                "pages": [{"panels": [{"caption": "a"}] * 6}]}
        with mock.patch.object(moss_api, "_run"), \
             mock.patch.object(moss_api, "_load_state",
                               return_value={"pages": {"1": {"status": "done",
                                                             "file": "x.png"}}}), \
             mock.patch.object(moss_comic, "digest",
                               return_value=("她醒了", "桌上有杯牛奶")), \
             mock.patch.object(moss_bot, "_send_image",
                               lambda *a, **k: sent["captions"].append(a[3])), \
             mock.patch.object(moss_bot, "_say",
                               lambda *a, **k: sent["texts"].append(a[2])):
            moss_bot._render_and_send("group", "1103174141", self.sid,
                                      self.page, spec, "T1", 1110055932,
                                      "", None, hint=hint)
        return sent

    def test_only_one_message(self):
        sent = self._run("迁移到私聊：私聊发 moss续集 X <接下来的剧情>")
        self.assertEqual(len(sent["captions"]), 1)   # 图那条
        self.assertEqual(sent["texts"], [])          # 没有第二条

    def test_caption_carries_code_and_commands(self):
        """编号、指令、迁移提示都在**同一条** caption 里，一个字都不能少。"""
        hint = ("迁移到私聊：私聊发 moss续集 %s <接下来的剧情>"
                % moss_series.code_of(self.sid, 1))
        cap = self._run(hint)["captions"][0]
        lines = cap.splitlines()
        self.assertEqual(len(lines), 4)
        self.assertIn(moss_series.code_of(self.sid, 1), lines[0])
        self.assertIn("第 01 页", lines[0])
        self.assertIn("seed 1110055932", lines[1])
        self.assertIn("moss续集", lines[2])
        self.assertIn("moss合集", lines[2])
        self.assertIn("moss重画", lines[2])
        self.assertIn("迁移到私聊", lines[3])

    def test_continue_page_has_no_migration_line(self):
        """只有第 1 页说「怎么搬走」——每页都说就变成另一种刷屏。"""
        cap = self._run("")["captions"][0]
        self.assertEqual(len(cap.splitlines()), 3)
        self.assertNotIn("迁移到私聊", cap)

    def test_missing_copy_is_refetched(self):
        """副本被外部清掉时重取一份再发，不许报「图画好了但发不出去」。"""
        from app import image_out
        paths = iter(["C:/tmp/a.png", "C:/tmp/b.png"])
        calls = []

        def fake_send(target, tid, path, caption="", **kw):
            calls.append(path)
            if len(calls) == 1:
                raise RuntimeError("OneBot 调用失败 send_private_msg: "
                                   "{'wording': \"ENOENT: no such file\"}")

        with mock.patch.object(moss_api, "_run"), \
             mock.patch.object(moss_api, "_load_state",
                               return_value={"pages": {"1": {"status": "done",
                                                             "file": "x.png"}}}), \
             mock.patch.object(moss_comic, "digest",
                               return_value=("她醒了", "桌上")), \
             mock.patch.object(image_out, "prepare_for_send",
                               lambda f, fmt="jpg": next(paths)), \
             mock.patch("app.image_audit.allow_send", return_value=True), \
             mock.patch("app.agents.image_send_format", return_value="jpg"), \
             mock.patch.object(qq_api, "send_image", fake_send):
            moss_bot._render_and_send("group", "1103174141", self.sid,
                                      self.page,
                                      {"title": "t", "pages": [{"panels": []}]},
                                      "T1", 1, "", None)
        self.assertEqual(calls, ["C:/tmp/a.png", "C:/tmp/b.png"])


class AlbumTest(unittest.TestCase):
    """`moss合集` 拼合并转发卡片（2026-10-31 用户实跑翻车后补的）。

    翻车现场：ComfyUI 停着，`prepare_for_send` 取不到图就回落成
    `http://127.0.0.1:8188/view?…`，塞进节点后协议端去拉 = `fetch failed`，
    **整张卡片一条都发不出去**。现在那一页算缺，其余照发；全缺才报错。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._old = (moss_series.STATE_ROOT, moss_series.INDEX_FILE)
        moss_series.STATE_ROOT = self.tmp
        moss_series.INDEX_FILE = os.path.join(self.tmp, "_index.json")
        self.addCleanup(self._restore)
        s = moss_series.open_new("group", "1103174141", title="红缝")
        self.sid = s["id"]
        self.pages = []
        parent = ""
        for i in (1, 2, 3):
            pg = moss_series.mutate(self.sid, lambda x, p=parent, i=i:
                                    moss_series.add_page(
                                        x, parent_id=p, task="T%d" % i,
                                        brief="第%d页" % i, seed=i,
                                        summary="她醒了" if i == 1 else ""))
            self.pages.append(pg)
            parent = pg["id"]

    def _restore(self):
        moss_series.STATE_ROOT, moss_series.INDEX_FILE = self._old
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _album(self, paths):
        """`paths` = 每页 prepare_for_send 的返回（按页序）。"""
        sent, said = [], []

        def fake_prepare(rel, fmt=None):
            return paths.pop(0)

        with mock.patch.object(moss_api, "_task_dir", lambda t: t), \
             mock.patch.object(moss_api, "_page_file",
                               lambda d, n: "x/%s.png" % d), \
             mock.patch.object(image_out, "prepare_for_send", fake_prepare), \
             mock.patch("app.agents.image_send_format", return_value="jpg"), \
             mock.patch.object(qq_api, "send_forward",
                               lambda t, i, nodes: sent.append(nodes)), \
             mock.patch.object(moss_bot, "_say",
                               lambda t, i, text, r="": said.append(text)):
            moss_bot._do_album("group", "1103174141", self.sid,
                               self.pages[-1]["id"], "", "")
        return sent, said

    def test_pages_without_a_local_file_are_skipped(self):
        """第 2 页只有远端 URL → 跳过它，卡片照发（别把它连累成 fetch failed）。"""
        sent, said = self._album(["C:/a.jpg",
                                  "http://127.0.0.1:8188/view?filename=b.png",
                                  "C:/c.jpg"])
        self.assertEqual(len(sent), 1)
        nodes = sent[0]
        names = [n["data"]["name"] for n in nodes]
        self.assertIn("第 01 页", names)
        self.assertIn("第 03 页", names)
        self.assertNotIn("第 02 页", names)
        self.assertTrue(any("缺了 1 张" in t for t in said))

    def test_all_pages_missing_says_why_instead_of_sending(self):
        """全缺就不发空卡片，直接说清是取不到图。"""
        sent, said = self._album(["http://127.0.0.1:8188/view?a",
                                  "http://127.0.0.1:8188/view?b",
                                  "http://127.0.0.1:8188/view?c"])
        self.assertEqual(sent, [])
        self.assertTrue(any("一张都没取出来" in t for t in said), said)

    def test_send_failure_reads_like_a_sentence(self):
        """协议端失败要给原因，不要把整个返回体 dict 甩给群友。"""
        said = []
        with mock.patch.object(moss_api, "_task_dir", lambda t: t), \
             mock.patch.object(moss_api, "_page_file", lambda d, n: "x.png"), \
             mock.patch.object(image_out, "prepare_for_send",
                               lambda rel, fmt=None: "C:/a.jpg"), \
             mock.patch("app.agents.image_send_format", return_value="jpg"), \
             mock.patch.object(qq_api, "send_forward",
                               mock.Mock(side_effect=RuntimeError(
                                   "OneBot 调用失败 send_group_forward_msg: "
                                   "{'status': 'failed', 'retcode': 100, "
                                   "'wording': 'fetch failed'}"))), \
             mock.patch.object(moss_bot, "_say",
                               lambda t, i, text, r="": said.append(text)):
            moss_bot._do_album("group", "1103174141", self.sid,
                               self.pages[-1]["id"], "", "")
        self.assertTrue(any("fetch failed" in t for t in said), said)
        self.assertFalse(any("retcode" in t for t in said), said)


if __name__ == "__main__":
    unittest.main()
