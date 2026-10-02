"""recall_image 工具：按编号把「当时真正跑的那段提示词」还给模型。

这个工具存在的唯一理由是**模型记不住**：那段提示词在工具调用参数里，而历史
到预算就会被 trim_history 摘要掉。模型于是只能现编一段，跟原图对不上，对方
照着跑会翻车。所以测试里最要紧的两条是：
① 查到就必须是原文，一字不改；② 查不到必须老实说不知道，不许编。
"""

import os
import tempfile
import unittest

from app import image_log
from app.tools.normal import recall_image


class RecallTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._old = image_log.PATH
        image_log.PATH = os.path.join(self.tmp.name, "image_log.jsonl")
        self.addCleanup(setattr, image_log, "PATH", self._old)

    def _call(self, text):
        return recall_image.tool["function"](text)

    def test_returns_the_original_prompt_verbatim(self):
        image_log.save("HT-20261001-081132-772", prompt="1girl, silver hair, "
                                                        "detailed eyes",
                       skill="hd_fast")
        out = self._call("HT-20261001-081132-772")
        self.assertIn("1girl, silver hair, detailed eyes", out)
        self.assertIn("hd_fast", out)

    def test_accepts_a_whole_quote_block(self):
        """模型多半会把一整段引用正文贴进来，工具得自己抠出编号。"""
        image_log.save("HT-20261001-081132-772", prompt="原始词条")
        out = self._call("[引用 胡桃桃 的消息] HT-20261001-081132-772[图片]")
        self.assertIn("原始词条", out)

    def test_unknown_tag_says_it_does_not_know(self):
        """查不到 = 老实说不知道。绝不能编一段出来。"""
        out = self._call("HT-20261001-081132-772")
        self.assertIn("查不到", out)
        self.assertIn("别自己编", out)

    def test_no_number_at_all_points_at_the_quote(self):
        out = self._call("这张图的词条发我一份")
        self.assertIn("没认出图号", out)
        self.assertIn("引用", out)

    def test_partial_hits_are_reported_separately(self):
        image_log.save("HT-20261001-081132-772", prompt="有的")
        out = self._call("HT-20261001-081132-772 和 HT-20261001-000000-000")
        self.assertIn("有的", out)
        self.assertIn("HT-20261001-000000-000", out)
        self.assertIn("别替它们编", out)

    def test_empty_prompt_is_honest(self):
        """记账时就没拿到提示词（老图）——说实话，不要去编。"""
        image_log.save("HT-20261001-081132-772", prompt="")
        out = self._call("HT-20261001-081132-772")
        self.assertIn("没记下提示词", out)

    def test_returns_the_seed_that_ran(self):
        """编号能查回**当初那个种子**：对方只报了个号、图上那行没带 seed 时，
        这是唯一一条答得出种子的路（不依赖模型记忆）。"""
        image_log.save("HT-20261001-081132-772", prompt="1girl",
                       skill="anima_soft", seed=4100493889)
        out = self._call("HT-20261001-081132-772")
        self.assertIn("4100493889", out)
        self.assertIn("动漫渠道两段采样共用这一个数", out)

    def test_seed_zero_is_a_real_seed(self):
        """0 是合法种子，不是「没填」——用 `if row.get("seed"):` 判就把这张的
        种子报成不知道。"""
        image_log.save("HT-20261001-081132-772", prompt="1girl", seed=0)
        out = self._call("HT-20261001-081132-772")
        self.assertIn("seed 0", out)
        self.assertNotIn("种子没记下", out)

    def test_old_row_admits_it_has_no_seed(self):
        """2026-10-02 之前的图账本里没有 seed 字段：明说「没记下」，否则模型会
        从别处凑一个数报给对方——那比说不知道糟得多。"""
        image_log.save("HT-20261001-081132-772", prompt="1girl")
        out = self._call("HT-20261001-081132-772")
        self.assertIn("种子没记下", out)

    def test_no_seed_records_the_empty_string_as_unknown(self):
        """NAI 那种云端图存的是空串（见 image_jobs.process）：同样报「没记下」。"""
        image_log.save("HT-20261001-081132-772", prompt="1girl", seed="")
        self.assertIn("种子没记下", self._call("HT-20261001-081132-772"))

    def test_tells_the_model_how_to_reroll(self):
        """查到之后要顺手说清「换提示词、同一个种子、同一个渠道」——这就是对方
        问这句话的来意（图有多手多脚，想在同种子上改一版）。"""
        image_log.save("HT-20261001-081132-772", prompt="1girl", seed=7)
        out = self._call("HT-20261001-081132-772")
        self.assertIn("generate_image", out)
        self.assertIn("同一个", out)

    def test_tool_is_registered_with_a_description(self):
        """注册信息本身要提醒模型别编——这是这个工具最容易翻车的地方。"""
        d = recall_image.tool["description"]
        self.assertIn("不要自己编", d)
        self.assertIn("HT-", d)
        # 描述里得写明那行 caption 带 seed，模型才知道引用回来的数字是种子，
        # 而不是又去查账本 / 干脆忽略它。
        self.assertIn("seed", d)
        self.assertEqual(recall_image.tool["name"], "recall_image")
