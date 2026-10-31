"""「有没有在跟机器人说话」的判据（2026-10-31）。

这道闸是为用户报的误触发加的：菜单和回执把 `更多渠道` / `使用指南` /
`预设网页` / `moss合集` 这些词印在群聊里，谁复述一句就被当成指令。
判据本身很小，但要**三处一致**（`moss_bot` / `direct_gen` / `qq_bot`），
所以单独钉死一份，防止谁改了一处忘了另两处。

口径：
  · 真 @ 过 → 算（`at_me=True`，私聊由调用方传这个放行）
  · 句子里出现「大大怪」→ 算（任何位置，不要求在开头）
  · 都没有 → 不算
"""

import unittest

from app import trigger


class CalledTest(unittest.TestCase):
    def test_at_me_always_counts(self):
        self.assertTrue(trigger.called("", at_me=True))
        self.assertTrue(trigger.called("随便说点什么", at_me=True))
        self.assertTrue(trigger.called(None, at_me=True))

    def test_name_anywhere_counts(self):
        for text in ("大大怪 使用指南",
                     "大大怪：预设网页",
                     "问一下大大怪，更多渠道在哪",
                     "这个大大怪知道"):        # 名字在句中、句尾都算
            self.assertTrue(trigger.called(text), text)

    def test_plain_chatter_does_not_count(self):
        for text in ("更多渠道", "使用指南", "预设网页打不开",
                     "moss合集 挺好用", "", None):
            self.assertFalse(trigger.called(text), repr(text))

    def test_at_me_false_is_still_false_without_name(self):
        self.assertFalse(trigger.called("更多渠道", at_me=False))

    def test_name_constant_is_the_one_users_say(self):
        self.assertEqual(trigger.NAME, "大大怪")
        self.assertIn(trigger.NAME, "大大怪 使用指南")


if __name__ == "__main__":
    unittest.main()
