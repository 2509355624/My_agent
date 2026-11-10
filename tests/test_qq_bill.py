"""QQ 按会话账单 + 用量流水判据（2026-11-10）。

真实背景：`sys.modules` 里混进 unittest 时，旧判据把 QQ 机器人整天的流水
写进系统临时目录，管理页按会话的用量整个缺失。这里既测折钱口径，也把
「usage.py 必须复用 config.py 的判据」钉死，防止两份实现再次漂移。
"""

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

from app import config
from app import persona_billing
from app import qq_bill
from app import usage as usage_stats

DAY = "2026-11-10"          # 周二，10:00 属高峰、03:00 属闲时


def _row(tag, t, hit=1000, miss=2000, out=500, **kw):
    r = {"date": DAY, "t": t, "tag": tag, "kind": "sync",
         "prov": "deepseek", "model": "deepseek-flash",
         "hit": hit, "miss": miss, "out": out, "ok": True}
    r.update(kw)
    return r


def _write(tmp, rows, date=DAY):
    d = os.path.join(tmp, "usage")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "calls-%s.jsonl" % date), "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


class TestQQBill(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.p = mock.patch.object(usage_stats, "BASE_DIR", self.tmp.name)
        self.p.start()
        self.addCleanup(self.p.stop)

    def test_按会话归口折钱(self):
        _write(self.tmp.name, [
            _row("private_123", "03:00:00"),
            _row("private_123", "10:00:00"),
            _row("group_456", "03:30:00"),
            _row("web_789__junior", "03:40:00"),
            _row("other", "03:50:00"),
        ])
        d = qq_bill.daily(DAY)
        # 闲时 1000*.02 + 2000*1 + 500*4 = 4020；高峰翻倍 = 8040（微元）
        by_tag = {r["tag"]: r for r in d["rows"]}
        self.assertEqual(set(by_tag), {"private_123", "group_456"})
        self.assertEqual(by_tag["private_123"]["cost"], 4020 + 8040)
        self.assertEqual(by_tag["private_123"]["kind"], "私聊")
        self.assertEqual(by_tag["private_123"]["id"], "123")
        self.assertEqual(by_tag["group_456"]["cost"], 4020)
        self.assertEqual(by_tag["group_456"]["kind"], "群聊")
        self.assertEqual(d["total"]["cost"], 16080)
        self.assertEqual(d["total"]["calls"], 3)
        self.assertEqual(d["web"]["cost"], 4020)
        self.assertEqual(d["other"]["cost"], 4020)
        self.assertEqual(d["local"]["cost"], 4020 * 4 + 8040)

    def test_只算官方_key_且跳过失败与空_usage(self):
        _write(self.tmp.name, [
            _row("private_1", "03:00:00", prov="custom"),
            _row("private_2", "03:00:00", model="deepseek-v4-pro"),
            _row("private_3", "03:00:00", ok=False),
            _row("private_4", "03:00:00", hit=0, miss=0, out=0),
            _row("private_5", "03:00:00"),
        ])
        d = qq_bill.daily(DAY)
        self.assertEqual([r["tag"] for r in d["rows"]], ["private_5"])
        self.assertEqual(d["local"]["calls"], 1)

    def test_时段按流水时间而不是当前时间(self):
        _write(self.tmp.name, [_row("private_1", "10:00:00")])
        d = qq_bill.daily(DAY)
        self.assertEqual(d["total"]["cost"], 8040, "10 点应按高峰价")

    def test_空账本与日期格式(self):
        d = qq_bill.daily(DAY)
        self.assertEqual(d["rows"], [])
        self.assertEqual(d["total"]["cost"], 0)
        self.assertIn(DAY, d["dates"])
        with self.assertRaises(ValueError):
            qq_bill.daily("2026/11/10")

    def test_csv_带_BOM_与汇总行(self):
        _write(self.tmp.name, [_row("group_9", "03:00:00")])
        text = qq_bill.csv_text(qq_bill.daily(DAY))
        self.assertTrue(text.startswith("\ufeff"))
        self.assertIn("QQ / 群号", text)
        self.assertIn("group_9", text)
        self.assertIn("合计（QQ 已归属）", text)
        self.assertIn("无归属（后台隐形调用）", text)
        self.assertIn("0.004020", text)

    def test_价目表与_paid_target_一致(self):
        pp, pm = persona_billing.paid_target()
        self.assertEqual(qq_bill.daily(DAY)["paid_model"], "%s/%s" % (pp, pm))


class TestUsageProcessGuard(unittest.TestCase):

    def test_用量判据必须复用_config_的实现(self):
        self.assertIs(usage_stats._is_test_process, config._is_test_process)

    def test_sys_modules_混进_unittest_但入口是生产进程时不隔离(self):
        main = sys.modules["__main__"]
        env = dict(os.environ)
        env.pop("AGENT_STATE_TEST", None)
        with mock.patch.dict(sys.modules, {"unittest": object(), "pytest": object()}), \
             mock.patch.object(sys, "argv", ["qq_bot.py"]), \
             mock.patch.object(sys, "orig_argv", ["python", "-m", "app.qq_bot"]), \
             mock.patch.object(main, "__file__", "D:/AI/agent_my_test/app/qq_bot.py",
                               create=True), \
             mock.patch.dict(os.environ, env, clear=True):
            self.assertFalse(usage_stats._is_test_process())


if __name__ == "__main__":
    unittest.main()
