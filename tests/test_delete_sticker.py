"""delete_sticker 工具测试：编号解析 + 单次上限 + 结果汇总。

原则：零网络、零真实 agents 目录。库存层（stickers.delete）全 mock。
"""

import unittest
from unittest import mock

from app.tools.normal import delete_sticker


class DeleteCallTest(unittest.TestCase):
    def setUp(self):
        self.deleted = mock.Mock(return_value=([(3, "猫瘫在桌上打滚")], []))
        p = mock.patch.object(delete_sticker.stickers, "delete", self.deleted)
        p.start()
        self.addCleanup(p.stop)

    def _call(self, nums):
        return delete_sticker.tool["function"](nums)

    def test_single_number(self):
        self.assertIn("已删 3号（猫瘫在桌上打滚）", self._call("3"))
        self.deleted.assert_called_once_with(mock.ANY, "3")

    def test_multiple_numbers_forwarded(self):
        self.deleted.return_value = ([(3, "a"), (7, "b")], [])
        out = self._call("3,7")
        self.assertIn("已删 3号（a）、7号（b）", out)
        self.deleted.assert_called_once_with(mock.ANY, "3,7")

    def test_at_most_five_per_call(self):
        # 上限防模型一口气把库清空（删完还得重新攒图）
        self._call("1,2,3,4,5,6,7,8")
        self.deleted.assert_called_once_with(mock.ANY, "1,2,3,4,5")

    def test_words_around_numbers_are_parsed(self):
        self._call("把3号删了吧")
        self.deleted.assert_called_once_with(mock.ANY, "3")

    def test_no_number_reports_guidance(self):
        out = self._call("随便")
        self.assertIn("没认出表情包编号", out)
        self.deleted.assert_not_called()

    def test_empty_number_reports_guidance(self):
        self.assertIn("没认出表情包编号", self._call(None))
        self.deleted.assert_not_called()

    def test_already_deleted_numbers_are_reported(self):
        self.deleted.return_value = ([], [3])
        out = self._call("3")
        self.assertIn("删不掉", out)
        self.assertIn("3", out)

    def test_partial_success_lists_both(self):
        self.deleted.return_value = ([(3, "a")], [9])
        out = self._call("3,9")
        self.assertIn("已删 3号（a）", out)
        self.assertIn("没删成：9", out)


if __name__ == "__main__":
    unittest.main()
