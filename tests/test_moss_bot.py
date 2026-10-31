"""`app/moss_bot.py` 的识别与回执文案测试（不出图、不连 QQ）。

出图那条链路靠 `tests/test_moss_api.py` 和手工实跑覆盖——这里只钉住
「什么消息该被接管、回执长什么样」这两件纯逻辑。
"""

import unittest

from app import moss_bot


class ParseTest(unittest.TestCase):
    def test_plain(self):
        hit, brief, tip = moss_bot.parse("moss漫画 雨夜便利店，共撑一把伞")
        self.assertTrue(hit)
        self.assertEqual(brief, "雨夜便利店，共撑一把伞")
        self.assertEqual(tip, "")

    def test_space_between_words(self):
        hit, brief, _ = moss_bot.parse("moss 漫画 一只橘猫跳上主人的胸口")
        self.assertTrue(hit)
        self.assertEqual(brief, "一只橘猫跳上主人的胸口")

    def test_case_insensitive(self):
        hit, brief, _ = moss_bot.parse("MOSS漫画 雨夜")
        self.assertTrue(hit)
        self.assertEqual(brief, "雨夜")

    def test_qq_nickname_prefix_is_dropped(self):
        """群消息前面带「昵称：」和机器人名字——都不能进剧情。

        造卡那边踩过：昵称被当成了角色名，卡就叫「大大怪」。
        """
        hit, brief, _ = moss_bot.parse("大大怪，moss漫画 雨夜便利店")
        self.assertTrue(hit)
        self.assertEqual(brief, "雨夜便利店")

    def test_hash_prefix(self):
        hit, brief, _ = moss_bot.parse("#moss漫画 一句话剧情")
        self.assertTrue(hit)
        self.assertEqual(brief, "一句话剧情")

    def test_bare_word_returns_usage(self):
        hit, brief, tip = moss_bot.parse("moss漫画")
        self.assertTrue(hit)
        self.assertEqual(brief, "")
        self.assertEqual(tip, moss_bot.USAGE)

    def test_question_is_not_a_brief(self):
        for text in ("moss漫画 怎么用", "moss漫画 支持什么", "moss漫画 是什么"):
            hit, brief, tip = moss_bot.parse(text)
            self.assertTrue(hit, text)
            self.assertEqual(brief, "", text)
            self.assertEqual(tip, moss_bot.USAGE, text)

    def test_one_char_is_not_a_brief(self):
        hit, brief, tip = moss_bot.parse("moss漫画 a")
        self.assertTrue(hit)
        self.assertEqual(brief, "")
        self.assertEqual(tip, moss_bot.USAGE)

    def test_other_commands_untouched(self):
        """silver漫画 是另一条路（多图批量），不能让这边吃掉。"""
        for text in ("今天天气不错", "silver漫画 10个 雷电将军", "anima 女骑士"):
            self.assertEqual(moss_bot.parse(text), (False, "", ""), text)


class SmallComicWordTest(unittest.TestCase):
    """「moss小漫画」也走同一条路（2026-10-31 用户口头就这么说）。

    不额外维护第二个关键词：`小\\s*` 挂在 `moss\\s*` 后面，三种写法落一套逻辑。
    """

    def test_small_comic_hit(self):
        for text in ("moss小漫画 雨夜便利店", "moss 小漫画 雨夜便利店",
                     "moss 小 漫画 雨夜便利店", "MOSS小漫画 开门"):
            hit, brief, tip = moss_bot.parse(text)
            self.assertTrue(hit, text)
            self.assertEqual(tip, "", text)
            self.assertTrue(brief, text)

    def test_small_comic_still_drops_prefix(self):
        hit, brief, _ = moss_bot.parse("大大怪：moss小漫画 开门发现别的男人")
        self.assertTrue(hit)
        self.assertEqual(brief, "开门发现别的男人")

    def test_small_comic_bare_returns_usage(self):
        hit, brief, tip = moss_bot.parse("moss小漫画")
        self.assertTrue(hit)
        self.assertEqual(brief, "")
        self.assertEqual(tip, moss_bot.USAGE)

    def test_layout_is_six_panels(self):
        """单页六格：LAYOUT 和 LAYOUTS 必须对得上，不然 check 会报格数不符。"""
        from app import moss_comic
        self.assertEqual(moss_bot.LAYOUT, "3x2")
        self.assertEqual(moss_bot.PAGES, 1)
        self.assertEqual(len(moss_comic.LAYOUTS[moss_bot.LAYOUT][1]), 6)

    def test_usage_mentions_six_panels(self):
        self.assertIn("六格", moss_bot.USAGE)


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
