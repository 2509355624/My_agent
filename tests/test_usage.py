# -*- coding: utf-8 -*-
"""每日 token 用量统计（app/usage.py）测试。

关键回归：按「天 + 会话」聚合的正确性、scope 嵌套时内层生效、
落盘节流不丢账、同日重启把磁盘上的半份账并回内存。
"""

import json
import os
import tempfile
import unittest
from unittest import mock

from app import usage


class UsageBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch.object(usage, "BASE_DIR", self.tmp.name)
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(usage, "_daily", {})
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(usage, "_dirty", set())
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(usage, "_last_flush", 0.0)
        p.start()
        self.addCleanup(p.stop)


class RecordAggregateTest(UsageBase):
    def test_records_accumulate_per_tag(self):
        with usage.scope("group_9"):
            usage.record(100, 50, 10)
            usage.record(200, 25, 5)
        with usage.scope("private_1"):
            usage.record(0, 10, 2)
        data = usage.daily()["sessions"]
        self.assertEqual(data["group_9"],
                         {"calls": 2, "hit": 300, "miss": 75, "output": 15})
        self.assertEqual(data["private_1"],
                         {"calls": 1, "hit": 0, "miss": 10, "output": 2})

    def test_scope_restores_previous_and_nesting_wins(self):
        with usage.scope("outer"):
            with usage.scope("inner"):
                usage.record(1, 1)
            usage.record(2, 2)          # 出了内层，回到 outer
        data = usage.daily()["sessions"]
        self.assertEqual(data["inner"]["calls"], 1)
        self.assertEqual(data["outer"]["calls"], 1)

    def test_no_scope_falls_back_to_other(self):
        usage.record(1, 1)
        self.assertIn("other", usage.daily()["sessions"])

    def test_provider_model_accepted_but_not_grouping(self):
        with usage.scope("group_9"):
            usage.record(10, 0, 1, provider="mimo", model="mimo-v2.6-flash")
            usage.record(10, 0, 1, provider="volc", model="glm-5-2")
        self.assertEqual(usage.daily()["sessions"]["group_9"]["calls"], 2)


class LastHitRateTest(UsageBase):
    """usage.last_hit_rate：给「这条会话线最近热不热」留一份**跨线程**记录。

    memory.trim_window 的非群分支靠它决定要不要提前压缩（2026-10-03 之前
    那里硬塞 hit_rate=0.0）。QQ 侧每条消息换线程，压缩跑在 save_history 里，
    所以这份记录必须按**会话 tag** 存，不能读「当前线程」的 usage。
    """

    def setUp(self):
        super().setUp()
        usage._last_hit.clear()
        self.addCleanup(usage._last_hit.clear)

    def test_unknown_tag_returns_none(self):
        """没记过必须是 None，不能是 0——0 会被当成「冷」而误触提前压缩。"""
        self.assertIsNone(usage.last_hit_rate("group_9"))
        self.assertIsNone(usage.last_hit_rate(""))
        self.assertIsNone(usage.last_hit_rate(None))

    def test_records_latest_rate_per_tag(self):
        with usage.scope("group_9"):
            usage.record(880, 120)          # 0.88
            usage.record(500, 500)          # 0.50，覆盖
        with usage.scope("private_1"):
            usage.record(990, 10)           # 0.99
        self.assertAlmostEqual(usage.last_hit_rate("group_9"), 0.50)
        self.assertAlmostEqual(usage.last_hit_rate("private_1"), 0.99)

    def test_zero_total_does_not_overwrite(self):
        """0/0 的调用（理论上不该有）不能把已知命中率抹成 0。"""
        with usage.scope("group_9"):
            usage.record(880, 120)
            usage.record(0, 0)
        self.assertAlmostEqual(usage.last_hit_rate("group_9"), 0.88)

    def test_table_is_capped(self):
        """长期运行不能无限增长。"""
        with mock.patch.object(usage, "_HIT_KEEP", 4):
            for i in range(10):
                with usage.scope("s%d" % i):
                    usage.record(1, 0)
        self.assertLessEqual(len(usage._last_hit), 4)


class FlushTest(UsageBase):
    def test_flush_writes_file_and_clears_dirty(self):
        with usage.scope("group_9"):
            usage.record(100, 50)
        usage.flush()
        path = os.path.join(self.tmp.name, "usage")
        files = os.listdir(path)
        self.assertEqual(len(files), 1)
        data = json.load(open(os.path.join(path, files[0]), encoding="utf-8"))
        self.assertEqual(data["sessions"]["group_9"]["hit"], 100)
        # 落盘后 daily() 从文件也能读回
        self.assertEqual(usage.daily()["sessions"]["group_9"]["hit"], 100)

    def test_flush_throttled(self):
        with usage.scope("group_9"):
            usage.record(100, 50)
        usage.flush()
        path = os.path.join(self.tmp.name, "usage", usage._today() + ".json")
        before = open(path, encoding="utf-8").read()
        with usage.scope("group_9"):
            usage.record(1, 1)              # 节流窗口内：内存有账、暂不写盘
        self.assertEqual(open(path, encoding="utf-8").read(), before)
        self.assertEqual(usage.daily()["sessions"]["group_9"]["hit"], 101)
        usage.flush()                       # 下一次 flush 补上
        self.assertEqual(usage.daily()["sessions"]["group_9"]["hit"], 101)

    def test_same_day_restart_merges_disk_record(self):
        with usage.scope("group_9"):
            usage.record(100, 50)
        usage.flush()
        # 模拟重启：内存清空，重新 import 级的加载逻辑
        p = mock.patch.object(usage, "_daily", {})
        p.start()
        self.addCleanup(p.stop)
        usage._load_from_disk_into_memory()
        data = usage.daily()["sessions"]
        self.assertEqual(data["group_9"]["hit"], 100)
        with usage.scope("group_9"):
            usage.record(10, 0)             # 同日续记要叠加，不能劈两半
        self.assertEqual(usage.daily()["sessions"]["group_9"]["hit"], 110)


if __name__ == "__main__":
    unittest.main()
