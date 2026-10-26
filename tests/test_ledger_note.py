"""账本事实注入（`qq_bot._ledger_note`）。

2026-10-26 用户拍板：引用带编号的图时，后端要自己解析编号，把当初真实用过
的提示词 / 渠道 / 种子附进本轮请求——**别再指望模型想起来调 recall_image**。
这里钉住的是三件事：

1. 有账本就把事实摆出来（原词 + 渠道 + seed 一个不许少）；
2. 查不到就返回空串，绝不编一段假的原词（看别人的图只能靠看图）；
3. 原词过长按上限截断并指路 recall_image，别把上下文吃满。
"""
import unittest
from unittest import mock

from app import qq_bot


class LedgerNoteTest(unittest.TestCase):

    def _note(self, blocks, rows, limit=None, max_items=None):
        """rows = {编号: 账本行}；查不到的编号返回 None（与真账本一致）。

        只 patch lookup：`find_tags` 是纯正则、无副作用，用真的才能一并钉住
        「编号是从引用正文里抠出来的」这件事。
        """
        with mock.patch.object(qq_bot.image_log, "lookup",
                               side_effect=lambda t: rows.get(t)):
            return qq_bot._ledger_note(blocks, limit=limit, max_items=max_items)

    def test_hit_carries_prompt_skill_and_seed(self):
        tag = "HT-20261026-153615-726"
        rows = {tag: {"prompt": "a girl in a cafe", "skill": "moss8",
                      "seed": 1461489868}}
        out = self._note(["[引用 233 的消息] %s · 1024×1536 · moss · seed 1" % tag],
                         rows)
        self.assertIn(tag, out)
        self.assertIn("a girl in a cafe", out)
        self.assertIn("moss8", out)
        self.assertIn("1461489868", out)

    def test_no_tag_returns_empty(self):
        out = self._note(["[引用 233 的消息] 这图怎么样"], {})
        self.assertEqual(out, "")

    def test_unknown_tag_returns_empty(self):
        # 引用别人的图：编号格式对、账本里没有 → 什么都不加，别编
        out = self._note(["[引用 233 的消息] HT-20260101-000000-000"], {})
        self.assertEqual(out, "")

    def test_long_prompt_is_truncated_with_a_pointer(self):
        tag = "HT-20261026-153615-726"
        rows = {tag: {"prompt": "x" * 3000, "skill": "moss8", "seed": 1}}
        out = self._note(["HT-20261026-153615-726"], rows, limit=100)
        self.assertIn("recall_image", out)
        self.assertNotIn("x" * 200, out)      # 确实截了
        self.assertIn("x" * 100, out)          # 前 100 字还在

    def test_multiple_tags_keep_order_and_respect_cap(self):
        rows = {
            "HT-20261026-000001-001": {"prompt": "first", "skill": "a", "seed": 1},
            "HT-20261026-000002-002": {"prompt": "second", "skill": "b", "seed": 2},
            "HT-20261026-000003-003": {"prompt": "third", "skill": "c", "seed": 3},
        }
        out = self._note([
            "HT-20261026-000002-002",
            "HT-20261026-000001-001",
            "HT-20261026-000003-003",
        ], rows, max_items=2)
        self.assertIn("first", out)
        self.assertIn("second", out)
        self.assertNotIn("third", out)
        # 按出现顺序：引用正文里先出现的先列
        self.assertLess(out.index("second"), out.index("first"))

    def test_empty_inputs_are_safe(self):
        self.assertEqual(qq_bot._ledger_note([]), "")
        self.assertEqual(qq_bot._ledger_note(None), "")

    def test_lookup_exception_does_not_break_the_turn(self):
        # 账本文件被锁/损坏时宁可少一段事实，也不能让整轮对话失败
        with mock.patch.object(qq_bot.image_log, "find_tags",
                               return_value=["HT-20261026-153615-726"]), \
             mock.patch.object(qq_bot.image_log, "lookup",
                               side_effect=OSError("boom")):
            self.assertEqual(qq_bot._ledger_note(["HT-20261026-153615-726"]), "")


if __name__ == "__main__":
    unittest.main()
