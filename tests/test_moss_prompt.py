"""moss 提示词清洗的测试（2026-11-04）。

`app/moss_prompt.py` 只做一件事：把从别家模型抄来的 booru 习惯词从 moss 的
提示词里删掉。这里钉住四件事——

  - 只对 moss 系生效，别的渠道一个字符都不动（43 个渠道共用同一条路径）；
  - 质量词、`score_N`、权重语法、打码词各删一条；
  - 分块写法（`<|left_subject|>` 这种）不能被拍平成一行，也不能被吃掉；
  - 洗空了要退回原文，不能给 ComfyUI 送一条空提示词。
"""
import unittest

from app import moss_prompt


class IsMossTest(unittest.TestCase):
    def test_moss_channels(self):
        for s in ("moss-qunyou", "moss-light", "moss", "MOSS-QUNYOU"):
            self.assertTrue(moss_prompt.is_moss(s), s)

    def test_others(self):
        for s in ("anima-line", "qwen_image_v1", "nai", "", None, "mossy"):
            self.assertFalse(moss_prompt.is_moss(s), s)


class StripTest(unittest.TestCase):
    def test_quality_words(self):
        p = "masterpiece, best quality, score_7, 2girls, blue hair"
        self.assertEqual(moss_prompt.clean("moss-qunyou", p), "2girls, blue hair")

    def test_banned_words(self):
        p = "1girl, watermark, cosplay, sitting on the bed"
        self.assertEqual(moss_prompt.clean("moss-light", p),
                         "1girl, sitting on the bed")

    def test_weight_syntax_unwrapped(self):
        """`(smile:1.2)` → `smile`：moss 会把括号当字面文本塞进编码器。"""
        self.assertEqual(moss_prompt.clean("moss-qunyou", "a girl, (smile:1.2)"),
                         "a girl, smile")

    def test_blocks_keep_lines(self):
        """分块写法靠换行隔离，拍平就等于把两人混成一个。"""
        p = ("<|camera_and_lighting|>warm light<|/camera_and_lighting|>\n"
             "<|left_subject|>On the left, a girl<|/left_subject|>")
        out = moss_prompt.clean("moss-qunyou", p)
        self.assertEqual(out.count("\n"), 1)
        self.assertIn("<|left_subject|>", out)

    def test_sentence_commas_survive(self):
        p = "On the left, a girl sits on the rock, holding a glass."
        self.assertEqual(moss_prompt.clean("moss-qunyou", p), p)

    def test_empty_stays_empty(self):
        self.assertEqual(moss_prompt.clean("moss-qunyou", ""), "")

    def test_nothing_left_falls_back_to_original(self):
        """只剩质量词时宁可原样发出去，也不能发空提示词。"""
        p = "masterpiece, best quality, score_7"
        self.assertEqual(moss_prompt.clean("moss-qunyou", p), p)

    def test_other_channel_untouched(self):
        p = "masterpiece, best quality, score_7, 2girls"
        self.assertEqual(moss_prompt.clean("anima-line", p), p)


if __name__ == "__main__":
    unittest.main()
