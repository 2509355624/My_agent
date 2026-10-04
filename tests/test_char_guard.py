# -*- coding: utf-8 -*-
"""角色点名守卫测试（2026-10-04，胡桃桃私聊 3985441738 实测出题）。

实测病根：说「画纳西妲」，模型把历史里高频的胡桃模板整段搬出来
（连角色名都不换），image_guide 正向规则压不住。守卫只判一件
客观的事：原话点名 A、prompt 写的是 B → 拒回点名重写。
"""
import unittest
from unittest import mock

from app import char_guard


HU_TAO_PROMPT = (
    "masterpiece, best quality, one single girl, adult young woman, "
    "Hu Tao from Genshin Impact, long dark brown twintails with red "
    "flower hair ornaments, red eyes, holding and licking a pink "
    "lollipop, black JK pleated super short skirt, back view")

NAHIDA_PROMPT = HU_TAO_PROMPT.replace("Hu Tao", "Nahida")

# 纳西妲的小个子、白色头发——按外貌写的合规路子（image_guide 教的）
APPEARANCE_PROMPT = (
    "masterpiece, best quality, one single girl, small petite girl, "
    "long white hair, green eyes, pointed ears, white dress, "
    "detailed hands, perfect anatomy")


class _Ctx:
    """绑 QQ 轮原话。text=None 模拟网页端（守卫家族判据：放行）。"""

    def __init__(self, text):
        self.text = text

    def __enter__(self):
        p1 = mock.patch("app.qq_api.current_turn_text",
                        return_value=self.text)
        p2 = mock.patch("app.qq_api.current_context",
                        return_value=("group", "123"))
        p1.start(); p2.start()
        self._ps = (p1, p2)
        return self

    def __exit__(self, *exc):
        for p in self._ps:
            p.stop()
        return False


class MentionedTest(unittest.TestCase):
    """用户侧别名识别：长词优先、中英都认。"""

    def test_chinese_alias(self):
        self.assertEqual(char_guard._mentioned("帮我画一个纳西妲"),
                         ["nahida"])

    def test_english_alias(self):
        self.assertEqual(char_guard._mentioned("来一张 hutao"),
                         ["hu tao"])

    def test_long_alias_wins_over_short(self):
        # 「神里绫华」不能被「绫华」拆开抢走（两个都指向同一 canonical，
        # 重点是命中即去重，不重复计数）
        self.assertEqual(char_guard._mentioned("画神里绫华"),
                         ["kamisato ayaka"])

    def test_multiple_characters(self):
        got = char_guard._mentioned("纳西妲和胡桃双人")
        self.assertEqual(sorted(got), ["hu tao", "nahida"])

    def test_no_mention(self):
        self.assertEqual(char_guard._mentioned("正面的视角"), [])
        self.assertEqual(char_guard._mentioned(""), [])

    def test_common_word_not_a_character(self):
        # 「跑个图」「画个弹琴的」不该命中任何角色
        self.assertEqual(char_guard._mentioned("画个弹琴的妹子"), [])


class PromptSideTest(unittest.TestCase):
    """prompt 侧识别：ASCII 词边界、常用词不当角色证据。"""

    def test_canonical_found(self):
        got = char_guard._present_in_prompt(HU_TAO_PROMPT)
        self.assertEqual(got, {"hu tao"})

    def test_word_boundary_no_false_hit(self):
        # "kamisato ayaka" 不该被 "ayaka" 之外的碎片误中；反向同理
        self.assertEqual(char_guard._present_in_prompt("ayaka fan art"),
                         {"kamisato ayaka"} if "ayaka" in
                         char_guard._ASCII_CANON_RE else set())

    def test_ambiguous_words_ignored(self):
        # amber eyes / jean jacket 是常用词，不当角色证据
        self.assertEqual(char_guard._present_in_prompt(
            "girl with amber eyes, jean jacket"), set())


class CheckTest(unittest.TestCase):
    """主判据：点名与产出对不上 → 拒回点名重写。"""

    def test_nahida_asked_hutao_given_rejected(self):
        """实测原题：要纳西妲，prompt 是胡桃模板 → 拒。"""
        with _Ctx("帮我画一个纳西妲"):
            out = char_guard.check(HU_TAO_PROMPT)
        self.assertIsNotNone(out)
        self.assertIn("纳西妲", out)
        self.assertIn("胡桃", out)
        self.assertIn("重写", out)

    def test_nahida_asked_nahida_given_pass(self):
        with _Ctx("帮我画一个纳西妲"):
            self.assertIsNone(char_guard.check(NAHIDA_PROMPT))

    def test_appearance_only_pass(self):
        """prompt 里没有任何已知角色 = 可能按外貌写的合规路子，放行。"""
        with _Ctx("帮我画一个纳西妲"):
            self.assertIsNone(char_guard.check(APPEARANCE_PROMPT))

    def test_no_mention_passes_any_prompt(self):
        with _Ctx("正面的视角"):
            self.assertIsNone(char_guard.check(HU_TAO_PROMPT))

    def test_same_character_pass(self):
        with _Ctx("胡桃，换白丝"):
            self.assertIsNone(char_guard.check(HU_TAO_PROMPT))

    def test_pair_image_one_missing_rejected(self):
        """双人图：点名两个角色，prompt 只写了一个 → 拒。"""
        p = NAHIDA_PROMPT  # 只有纳西妲
        with _Ctx("纳西妲和胡桃双人"):
            out = char_guard.check(p)
        self.assertIsNotNone(out)
        self.assertIn("胡桃", out)

    def test_pair_image_both_present_pass(self):
        p = NAHIDA_PROMPT + ", hu tao standing beside her"
        with _Ctx("纳西妲和胡桃双人"):
            self.assertIsNone(char_guard.check(p))

    def test_none_turn_text_passes(self):
        """网页端 / 单测直调（无原话）一律放行——守卫家族判据。"""
        with _Ctx(None):
            self.assertIsNone(char_guard.check(HU_TAO_PROMPT))

    def test_chinese_prompt_char_name_recognized(self):
        """qwen 中文 prompt 里写「胡桃」也能认出来。"""
        with _Ctx("画纳西妲"):
            out = char_guard.check(
                "一个穿着红黑衣服的年轻女性，胡桃，双马尾，红色的眼睛")
        self.assertIsNotNone(out)


if __name__ == "__main__":
    unittest.main()
