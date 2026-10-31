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

from app import moss_bot, moss_comic, moss_msgs, moss_series


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
        for text in ("moss漫画 怎么用", "moss漫画 支持什么", "moss漫画 是什么"):
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
        """带 moss 前缀的四个词无条件接管（哪怕没引用，也要回一句怎么用）。"""
        cases = {
            "moss续集 她走了": "continue",
            "moss 续集 她走了": "continue",
            "moss重画": "redo",
            "moss 重画": "redo",
            "moss合集": "album",
            "MOSS合集 1-5": "album",
        }
        for text, want in cases.items():
            hit, act, _payload, _tip = moss_bot.parse(text)
            self.assertTrue(hit, text)
            self.assertEqual(act, want, text)

    def test_usage_says_the_prefix_is_required(self):
        """用法里必须写明「这四个词要带 moss」，不然用户还会说裸词。"""
        self.assertIn("带 moss", moss_bot.USAGE)
        self.assertIn("moss续集", moss_bot.USAGE)
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
            "moss续集 她发现伞被人拿走了", quote_id="MID-2",
            quote_text="%s · 第 02 页 ·《醒来之后》" % moss_series.code_of(self.sid, 2))
        self.assertTrue(hit)
        self.assertEqual(act, "continue")
        self.assertEqual(payload, "她发现伞被人拿走了")
        self.assertEqual(tip, "")

    def test_redo_by_quote(self):
        hit, act, _payload, _tip = moss_bot.parse("moss重画", quote_id="MID-2")
        self.assertTrue(hit)
        self.assertEqual(act, "redo")

    def test_album_by_quote(self):
        hit, act, _payload, _tip = moss_bot.parse("moss合集", quote_id="MID-2")
        self.assertTrue(hit)
        self.assertEqual(act, "album")

    def test_album_range_is_kept(self):
        hit, act, payload, _tip = moss_bot.parse("moss合集 1-10", quote_id="MID-2")
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
            "moss续集 她走了", quote_id="MID-转发后不认识",
            quote_text="[转发的聊天记录]\n昵称：《醒来之后》· %s · 第 02 页 · 6 格" % code)
        self.assertTrue(hit)
        self.assertEqual(act, "continue")
        sid, pg, how = moss_msgs.locate("MID-转发后不认识",
                                        "[转发的聊天记录]\n《醒来之后》· %s" % code)
        self.assertEqual((sid, pg["n"], how), (self.sid, 2, "code"))

    def test_typed_code_locates_too(self):
        """手打编号：编号只用来定位，不许混进剧情。"""
        code = moss_series.code_of(self.sid, 2)
        hit, act, payload, _tip = moss_bot.parse("moss续集 %s 她推门进来" % code)
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


if __name__ == "__main__":
    unittest.main()
