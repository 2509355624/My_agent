# -*- coding: utf-8 -*-
"""气泡切分测试：拆得自然、字不丢、不超上限、URL 不拆。"""

import unittest

from app import bubbles


class SplitTest(unittest.TestCase):
    def test_short_reply_stays_single(self):
        self.assertEqual(bubbles.split_bubbles("好"), ["好"])
        self.assertEqual(bubbles.split_bubbles("哈哈哈"), ["哈哈哈"])

    def test_sentences_become_bubbles(self):
        self.assertEqual(
            bubbles.split_bubbles("上游好像活了。但是还是很慢，先别折腾它。"),
            ["上游好像活了。", "但是还是很慢，先别折腾它。"])

    def test_paragraphs_become_bubbles(self):
        self.assertEqual(
            bubbles.split_bubbles("第一段先说这个\n第二段换个事说"),
            ["第一段先说这个", "第二段换个事说"])

    def test_long_comma_run_on_uses_clause_level(self):
        out = bubbles.split_bubbles(
            "这个说法其实有点问题，因为它忽略了类目匹配的逻辑，所以召回上不去。", 4)
        self.assertEqual(len(out), 3)
        self.assertTrue(all(o for o in out))

    def test_over_limit_merges_shortest_pairs(self):
        out = bubbles.split_bubbles("一。二。三。四。五。六。七。八。", 4)
        self.assertEqual(len(out), 4)
        self.assertEqual(out, ["一。二。", "三。四。", "五。六。", "七。八。"])

    def test_url_paragraph_kept_whole(self):
        out = bubbles.split_bubbles("链接在这 https://example.com/a。快看", 4)
        self.assertEqual(len(out), 1)
        self.assertIn("https://example.com/a", out[0])

    def test_no_content_loss(self):
        text = "先说结论。然后是理由，一共两点。最后补一句。"
        out = bubbles.split_bubbles(text, 4)
        self.assertEqual("".join(out).replace("，", ""),
                         text.replace("，", ""))

    def test_empty(self):
        self.assertEqual(bubbles.split_bubbles(""), [])
        self.assertEqual(bubbles.split_bubbles("   \n  "), [])

    def test_zero_limit_means_single(self):
        out = bubbles.split_bubbles("一。二。三。", 1)
        self.assertEqual(len(out), 1)


if __name__ == "__main__":
    unittest.main()
