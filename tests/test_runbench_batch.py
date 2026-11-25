# -*- coding: utf-8 -*-
"""「连续多图」批量出图（2026-11-25）守卫测试。

测的是**判定与账目**，不真出图：把 `_run_batch` 换成桩，只验证
「整单先校验 → 重复渠道拒整单 → 高峰拒 → 一组只占一个在途名额 →
全部入队后只发一条回执 → 一图多风只反推一次」。

为什么要单独锁这几条：它们都是「错了也照样出图、只是体验崩掉」的那类问题
（重复渠道偷偷去重、回执发 5 条、反推 5 遍白烧额度），跑图跑通了看不出错。
"""
import os
import unittest

import app as app_pkg  # noqa: F401  （确保 app 包先加载）
from app.features.bench import web as rb


def _app():
    from flask import Flask
    a = Flask(__name__)
    a.register_blueprint(rb.bp)
    a.config["TESTING"] = True
    return a


def _tok():
    from app.gateways import friend_auth
    return friend_auth.make_runbench_token("private", "123456789")


def _chan_ids(n):
    return [c["id"] for c in rb._channel_rows()][:n]


IMG = "data:image/jpeg;base64,AAA"


class ParseBatchTest(unittest.TestCase):
    """整单校验：动手之前必须全部验完。"""

    def setUp(self):
        self.ids = _chan_ids(6)
        self.assertTrue(len(self.ids) >= 6, "渠道表里至少要有 6 个渠道")

    def test_same_mode_uses_one_image_for_all(self):
        body = {"mode": "same", "prompt": "一只猫",
                "items": [{"skill": s, "image": IMG} for s in self.ids[:3]]}
        (mode, rows), err, _ = rb._parse_batch(body)
        self.assertEqual(err, "")
        self.assertEqual(mode, "same")
        self.assertEqual(len(rows), 3)
        # 三个渠道共用同一张参考图
        self.assertTrue(all(r["image"] == IMG for r in rows))

    def test_same_mode_image_only_on_first_item(self):
        # 前端只发一份参考图（省请求体积），挂在第一项上；其余项 image=""。
        body = {"mode": "same", "prompt": "一只猫", "items": [
            {"skill": self.ids[0], "image": IMG},
            {"skill": self.ids[1], "image": ""},
            {"skill": self.ids[2], "image": ""}]}
        (_, rows), err, _ = rb._parse_batch(body)
        self.assertEqual(err, "")
        self.assertTrue(all(r["image"] == IMG for r in rows),
                        "参考图要铺给所有渠道")

    def test_same_mode_image_on_later_item_still_works(self):
        # 第一格没选渠道时，图会落在别的项上——不能因为 rows[0] 空就报错。
        body = {"mode": "same", "prompt": "一只猫", "items": [
            {"skill": self.ids[0], "image": ""},
            {"skill": self.ids[1], "image": IMG}]}
        (_, rows), err, _ = rb._parse_batch(body)
        self.assertEqual(err, "")
        self.assertTrue(all(r["image"] == IMG for r in rows))

    def test_each_mode_skips_empty_slots(self):
        body = {"mode": "each", "prompt": "", "items": [
            {"skill": self.ids[0], "image": IMG},
            {"skill": self.ids[1], "image": ""},        # 空格子 → 不跑
            {"skill": self.ids[2], "image": IMG},
        ]}
        (mode, rows), err, _ = rb._parse_batch(body)
        self.assertEqual(err, "")
        self.assertEqual([r["skill"] for r in rows], [self.ids[0], self.ids[2]])

    def test_duplicate_channel_rejects_same_mode(self):
        # 一图多风：同一张图撞同一个渠道等于白跑，整单拒，不偷偷去重。
        body = {"mode": "same", "prompt": "x",
                "items": [{"skill": self.ids[0], "image": IMG},
                          {"skill": self.ids[0], "image": IMG}]}
        _, err, code = rb._parse_batch(body)
        self.assertIn("两遍", err)
        self.assertEqual(code, 400)

    def test_duplicate_channel_allowed_in_each_mode(self):
        # 2026-11-25 用户拍板：五图各画是 5 张不同的图，用同一个渠道是常态
        # （「全部同第一款」按钮就是干这个的），必须放行。
        body = {"mode": "each", "prompt": "",
                "items": [{"skill": self.ids[0], "image": IMG},
                          {"skill": self.ids[0], "image": IMG},
                          {"skill": self.ids[0], "image": IMG}]}
        (mode, rows), err, _ = rb._parse_batch(body)
        self.assertEqual(err, "")
        self.assertEqual(mode, "each")
        self.assertEqual([r["skill"] for r in rows], [self.ids[0]] * 3)

    def test_unknown_channel_rejects(self):
        body = {"mode": "same", "prompt": "x",
                "items": [{"skill": "no-such-channel", "image": IMG}]}
        _, err, code = rb._parse_batch(body)
        self.assertIn("没有", err)
        self.assertEqual(code, 404)

    def test_more_than_five_rejects(self):
        body = {"mode": "same", "prompt": "x",
                "items": [{"skill": s, "image": IMG} for s in self.ids[:6]]}
        _, err, code = rb._parse_batch(body)
        self.assertNotEqual(err, "")
        self.assertEqual(code, 400)

    def test_same_mode_needs_image_and_prompt(self):
        body = {"mode": "same", "prompt": "x",
                "items": [{"skill": self.ids[0], "image": ""}]}
        _, err, _ = rb._parse_batch(body)
        self.assertIn("参考图", err)
        body = {"mode": "same", "prompt": "",
                "items": [{"skill": self.ids[0], "image": IMG}]}
        _, err, _ = rb._parse_batch(body)
        self.assertIn("想画", err)

    def test_prompt_cap_is_20000(self):
        # 用户：「我最长是允许 2 万字的」
        body = {"mode": "same", "prompt": "字" * 20000,
                "items": [{"skill": self.ids[0], "image": IMG}]}
        _, err, _ = rb._parse_batch(body)
        self.assertEqual(err, "")
        self.assertEqual(rb._PROMPT_MAX_BATCH, 20000)


class WeighTest(unittest.TestCase):
    """在途名额：普通任务 1 个，同一批次整组 1 个。"""

    def test_single_jobs_count_one_each(self):
        rows = [{"state": "running"}, {"state": "queued"}]
        self.assertEqual(rb._weigh(rows), 2)

    def test_one_batch_of_five_counts_one(self):
        rows = [{"batch": "B1"} for _ in range(5)]
        self.assertEqual(rb._weigh(rows), 1)

    def test_batch_plus_single(self):
        rows = [{"batch": "B1"} for _ in range(5)] + [{}]
        self.assertEqual(rb._weigh(rows), 2)

    def test_two_batches_count_two(self):
        rows = [{"batch": "B1"}] * 5 + [{"batch": "B2"}] * 5
        self.assertEqual(rb._weigh(rows), 2)


class BatchEndpointTest(unittest.TestCase):
    """接口层：高峰拒、重复渠道拒、正常单回批次号。"""

    def setUp(self):
        self.app = _app()
        self.c = self.app.test_client()
        self.tok = _tok()
        self.ids = _chan_ids(6)
        rb._JOBS.clear()
        rb._BATCHES.clear()
        rb._IP_SUBMITS.clear()
        self._old_peak = rb._batch_peak_blocked
        self._old_run = rb._run_batch
        rb._run_batch = lambda *a, **k: None        # 不真跑

    def tearDown(self):
        rb._batch_peak_blocked = self._old_peak
        rb._run_batch = self._old_run
        rb._JOBS.clear()
        rb._BATCHES.clear()
        rb._IP_SUBMITS.clear()

    def _post(self, body):
        return self.c.post("/api/runbench/gen-many", json=dict(body, k=self.tok))

    def test_peak_is_rejected(self):
        rb._batch_peak_blocked = lambda: (True, "早高峰（10点到12点）排队最挤")
        r = self._post({"mode": "same", "prompt": "x",
                        "items": [{"skill": self.ids[0], "image": IMG}]})
        self.assertEqual(r.status_code, 429)
        self.assertIn("高峰", r.get_json()["error"])

    def test_duplicate_is_rejected_before_anything_starts(self):
        rb._batch_peak_blocked = lambda: (False, "")
        r = self._post({"mode": "same", "prompt": "x", "items": [
            {"skill": self.ids[0], "image": IMG},
            {"skill": self.ids[0], "image": IMG}]})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(rb._BATCHES, {}, "拒单时不该留下任何批次")

    def test_same_channel_twice_is_ok_in_each_mode(self):
        # 五图各画：同渠道 3 张应当正常收单，不被重复校验拦。
        rb._batch_peak_blocked = lambda: (False, "")
        r = self._post({"mode": "each", "prompt": "", "items": [
            {"skill": self.ids[0], "image": IMG},
            {"skill": self.ids[0], "image": IMG},
            {"skill": self.ids[0], "image": IMG}]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["count"], 3)

    def test_ok_returns_bid(self):
        rb._batch_peak_blocked = lambda: (False, "")
        r = self._post({"mode": "each", "prompt": "", "items": [
            {"skill": self.ids[0], "image": IMG},
            {"skill": self.ids[1], "image": IMG}]})
        self.assertEqual(r.status_code, 200)
        j = r.get_json()
        self.assertTrue(j["ok"])
        self.assertTrue(j["bid"].startswith("B-"))
        self.assertEqual(j["count"], 2)

    def test_bad_token_is_401(self):
        r = self.c.post("/api/runbench/gen-many",
                        json={"k": "nope", "mode": "same", "items": []})
        self.assertEqual(r.status_code, 401)


class BatchReceiptTest(unittest.TestCase):
    """回执与反推：一批只发一条、一图多风只反推一次。"""

    def setUp(self):
        self.ids = _chan_ids(3)
        rb._JOBS.clear()
        rb._BATCHES.clear()
        self._patches = []
        self.calls = []
        self.recalls = []
        self.receipts = []
        self.sent = []

        def fake_channel(prompt, skill, image="", tags=None):
            self.calls.append((skill, tags))
            return ""

        def fake_recall(image):
            self.recalls.append(image)
            return "1girl, solo"

        rb._run_channel = fake_channel
        rb._recall_once = fake_recall
        rb._batch_peak_blocked = lambda: (False, "")

        import app.features.image.jobs as image_jobs
        self._old_send = image_jobs._send_text
        image_jobs._send_text = lambda t, i, txt: self.receipts.append(txt)
        self._old_thread = rb.threading.Thread

        class _NoThread(object):
            def __init__(self, *a, **k):
                pass

            def start(self):
                pass

        rb.threading.Thread = _NoThread

    def tearDown(self):
        import app.features.image.jobs as image_jobs
        image_jobs._send_text = self._old_send
        rb.threading.Thread = self._old_thread
        rb._JOBS.clear()
        rb._BATCHES.clear()

    def _run(self, mode, rows):
        rb._BATCHES["B-T"] = {"token": "k", "state": "queued", "total": len(rows),
                              "queued": 0, "error": "", "ts": 0, "tags": []}
        rb._run_batch("B-T", "k", ("private", "123456789"), mode, rows)

    def test_one_image_recalled_once_for_all_channels(self):
        rows = [{"image": IMG, "prompt": "一只猫", "skill": s} for s in self.ids]
        self._run("same", rows)
        self.assertEqual(len(self.recalls), 1, "参考图只该反推一次")
        # 三个渠道都拿到同一段 tags，且都没再去反推
        self.assertEqual([t for _, t in self.calls], ["1girl, solo"] * 3)

    def test_single_merged_receipt(self):
        rows = [{"image": IMG, "prompt": "一只猫", "skill": s} for s in self.ids]
        self._run("same", rows)
        self.assertEqual(len(self.receipts), 1, "一批只发一条回执，发多了刷屏")
        self.assertIn("3 张都排上队", self.receipts[0])

    def test_each_mode_recalls_per_image(self):
        rows = [{"image": IMG, "prompt": "", "skill": s} for s in self.ids]
        self._run("each", rows)
        self.assertEqual(len(self.recalls), 3, "五图各画每张各自反推")

    def test_swallow_restores_original(self):
        import app.features.image.jobs as image_jobs
        before = image_jobs._send_text
        with rb._SwallowReceipts() as sw:
            self.assertIsNot(sw.seam, None)
            self.assertIsNot(image_jobs._send_text, before)
        self.assertIs(image_jobs._send_text, before)


class PresetRetiredTest(unittest.TestCase):
    """预设斜杠命令已下线（2026-11-25）：发 /存预设 不该有任何反应。"""

    def test_save_preset_regex_is_gone(self):
        from app.features.image import direct_gen
        self.assertFalse(hasattr(direct_gen, "_SAVE_PRESET_RE"))
        self.assertFalse(hasattr(direct_gen, "_LIST_PRESET_RE"))


class PageGuardTest(unittest.TestCase):
    """跑图台页面的结构守卫。"""

    def setUp(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "web", "runbench.html"), encoding="utf-8") as f:
            self.html = f.read()

    def test_two_tabs_only(self):
        # 顶部只剩「官方渠道 / 连续多图」
        self.assertIn('data-tab="channel"', self.html)
        self.assertIn('data-tab="batch"', self.html)
        self.assertNotIn('data-tab="preset"', self.html)

    def test_batch_panel_has_both_modes(self):
        self.assertIn('data-mode="same"', self.html)
        self.assertIn('data-mode="each"', self.html)

    def test_uses_batch_endpoint_once(self):
        self.assertIn("/api/runbench/gen-many", self.html)
        # 批量面板的提交必须走新接口，不能在前端 for 循环打 5 次单图接口。
        # 判据：批量提交那一段（bPrompt/items 附近）只出现一次 gen-many。
        self.assertEqual(self.html.count("/api/runbench/gen-many"), 1)
        # 单图弹窗那条路径仍走单图接口（它本来就该是单张）
        self.assertIn('"/api/runbench/gen"', self.html)

    def test_channel_choice_persisted(self):
        self.assertIn("localStorage.setItem", self.html)
        self.assertIn("runbench.batch.v1", self.html)

    def test_dup_check_only_in_same_mode(self):
        # 2026-11-25 用户拍板：重复渠道只在「一图多风」里禁，五图各画允许
        # 同渠道多张。前端两处闸门（置灰 usedBy、提交前 dup）都得带模式判断。
        # usedBy 里必须早退
        self.assertIn('if(bMode !== "same") return false;', self.html)
        # 提交前的重复校验必须包在 same 分支里
        self.assertIn('if(bMode === "same"){', self.html)
        self.assertIn("一图多风每个格子要换不同的渠道", self.html)

    def test_mode_switch_clears_dup_skills(self):
        # 从「五图各画」切回「一图多风」时，5 格可能全是同一个渠道（那边合法）。
        # 必须清掉重复的，否则整单必然被服务端拒，且用户看不出来。
        self.assertIn("var keep = {};", self.html)


if __name__ == "__main__":
    unittest.main()
