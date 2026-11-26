# -*- coding: utf-8 -*-
"""「连续多图」批量出图（2026-11-25）守卫测试。

测的是**判定与账目**，不真出图：把 `_run_batch` 换成桩，只验证
「整单先校验 → 一图多风重复渠道拒整单 → 没有任何配额/限流 →
全部入队后只发一条回执 → 一图多风只反推一次」。

为什么要单独锁这几条：它们都是「错了也照样出图、只是体验崩掉」的那类问题
（重复渠道偷偷去重、回执发 5 条、反推 5 遍白烧额度），跑图跑通了看不出错。
"""
import os
import time
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

    def test_duplicate_channel_allowed_in_same_mode(self):
        # 2026-11-26 用户拍板「抽卡」：一张图连跑 5 次同一渠道、种子各不同，
        # 是明确需求。一图多风也放行重复渠道，不再整单拒、不去重。
        body = {"mode": "same", "prompt": "x",
                "items": [{"skill": self.ids[0], "image": IMG}
                          for _ in range(5)]}
        (mode, rows), err, code = rb._parse_batch(body)
        self.assertEqual(err, "")
        self.assertEqual([r["skill"] for r in rows], [self.ids[0]] * 5)

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

    def test_same_mode_needs_image_not_prompt(self):
        # 2026-11-25 用户拍板：需求框**可以留空**——「它是可以留空的呀……留空
        # 的时候点击一键开跑，就是默认反推提示词，然后按照这些渠道去跑图」。
        # 所以 now 只要求有参考图，不再要求有 prompt。
        body = {"mode": "same", "prompt": "",
                "items": [{"skill": self.ids[0], "image": IMG}]}
        r, err, _ = rb._parse_batch(body)
        self.assertEqual(err, "", "留空需求应当放行（靠参考图反推）")
        self.assertEqual(r[0], "same")
        self.assertEqual(len(r[1]), 1)
        self.assertEqual(r[1][0]["image"], IMG)

        # 图也没有 → 仍然要拒，且拒的是「参考图」不是「需求」
        body = {"mode": "same", "prompt": "",
                "items": [{"skill": self.ids[0], "image": ""}]}
        _, err, _ = rb._parse_batch(body)
        self.assertIn("参考图", err)

    def test_same_mode_reference_only_on_first_item(self):
        # 前端把参考图**只挂在第一项**（省请求体积），其余项 image 是空串。
        # 以前「没图也没需求」那条检查对它们也生效 → 第 2 格被误拒，用户看到
        # 「第 2 格没有图，也没有写需求」，可他明明传了图。这条锁住不许复发。
        body = {"mode": "same", "prompt": "",
                "items": [{"skill": self.ids[0], "image": IMG},
                          {"skill": self.ids[1], "image": ""},
                          {"skill": self.ids[2], "image": ""}]}
        r, err, _ = rb._parse_batch(body)
        self.assertEqual(err, "", "只挂第一项的参考图应当摊给所有格")
        self.assertEqual(len(r[1]), 3)
        for row in r[1]:
            self.assertEqual(row["image"], IMG, "每一格都要拿到参考图")

    def test_prompt_cap_is_20000(self):
        # 用户：「我最长是允许 2 万字的」
        body = {"mode": "same", "prompt": "字" * 20000,
                "items": [{"skill": self.ids[0], "image": IMG}]}
        _, err, _ = rb._parse_batch(body)
        self.assertEqual(err, "")
        self.assertEqual(rb._PROMPT_MAX_BATCH, 20000)

    def test_same_mode_drops_prompt(self):
        # 2026-11-25 用户拍板：一图多风**没有提示词输入栏**，「它本身就是反推
        # 图片，然后跑 5 种不同的渠道而已」。前端就算还传了 prompt，后端也要
        # 丢掉，不许掺进反推串里。
        body = {"mode": "same", "prompt": "一只猫在跑",
                "items": [{"skill": self.ids[0], "image": IMG}]}
        (_, rows), err, _ = rb._parse_batch(body)
        self.assertEqual(err, "")
        self.assertEqual(rows[0]["prompt"], "", "一图多风不该带需求")

    def test_text_mode_needs_prompt(self):
        body = {"mode": "text", "prompt": "",
                "items": [{"skill": self.ids[0]}]}
        _, err, _ = rb._parse_batch(body)
        self.assertIn("提示词", err)

    def test_text_mode_runs_how_many_channels_chosen(self):
        # 用户原话：「如果没有选渠道，那就默认跑 3 格、两格」——选几个跑几个。
        body = {"mode": "text", "prompt": "一只猫",
                "items": [{"skill": self.ids[0]}, {"skill": ""},
                          {"skill": self.ids[1]}, {"skill": ""}, {"skill": ""}]}
        (mode, rows), err, _ = rb._parse_batch(body)
        self.assertEqual(err, "")
        self.assertEqual(mode, "text")
        self.assertEqual(len(rows), 2, "只选 2 个渠道就跑 2 张")
        self.assertTrue(all(r["image"] == "" for r in rows), "文生图不带图")
        self.assertTrue(all(r["prompt"] == "一只猫" for r in rows))

    def test_text_mode_allows_same_channel(self):
        body = {"mode": "text", "prompt": "一只猫",
                "items": [{"skill": self.ids[0]}, {"skill": self.ids[0]}]}
        (_, rows), err, _ = rb._parse_batch(body)
        self.assertEqual(err, "")
        self.assertEqual(len(rows), 2)

    def test_text_mode_needs_at_least_one_channel(self):
        body = {"mode": "text", "prompt": "一只猫",
                "items": [{"skill": ""}, {"skill": ""}]}
        _, err, _ = rb._parse_batch(body)
        self.assertIn("渠道", err)


class NoLimitTest(unittest.TestCase):
    """2026-11-25 用户拍板：跑图台**没有任何配额/限流**。

    删掉的是四条（原来都在 `bench/web.py`）：每 IP 每小时 8 张、高峰禁批量、
    全局在途 ≤4、每令牌在途 ≤1。这里锁住「它们不许回来」——任何人再往
    gen-many 里加一道闸，这几条会立刻红。
    """

    def setUp(self):
        self.src = open(rb.__file__, encoding="utf-8").read()

    def test_no_ip_hour_quota(self):
        for name in ("_SUBMIT_PER_IP_HOUR", "_ip_ok", "_IP_SUBMITS",
                     "这一小时发太多次"):
            self.assertNotIn(name, self.src, "每 IP 每小时限额必须彻底删掉")

    def test_no_peak_window_block(self):
        # 只查「还有没有会拦人的代码」，不查注释——注释里保留「曾经有过、
        # 已按用户要求删掉」的说明是有价值的。
        for name in ("_batch_peak_blocked", "PEAK_WINDOWS", "peak_label"):
            self.assertNotIn(name, self.src, "高峰禁批量必须彻底删掉")
        self.assertNotIn("一次只能跑一张", self.src, "高峰话术不许回来")

    def test_no_inflight_caps(self):
        for name in ("_PER_TOKEN_INFLIGHT", "_ALL_INFLIGHT", "_inflight(",
                     "_token_inflight", "_weigh"):
            self.assertNotIn(name, self.src, "在途名额闸必须彻底删掉")

    def test_jobs_has_no_peak_layer(self):
        import app.features.image.jobs as image_jobs
        self.assertFalse(hasattr(image_jobs, "PEAK_WINDOWS"),
                         "jobs 里的时段表必须删掉")
        self.assertEqual(image_jobs.MAX_INFLIGHT, 5,
                         "唯一规则：一个会话最多同时在途 5 张")
        # inflight_limit 保留（三处调用点在用），但任何时间都必须是 5。
        for hour in (0, 9, 10, 11, 21, 22, 23):
            ts = time.mktime((2026, 11, 25, hour, 30, 0, 0, 0, -1))
            self.assertEqual(image_jobs.inflight_limit(ts)[0], 5,
                             "%d 点也该是 5 张，不该有高峰" % hour)


class DrawFiveTest(unittest.TestCase):
    """2026-11-26 用户拍板「抽卡」：同提示词/图连跑同一渠道 5 张、种子各不同。

    根因：generate_image 的 find_pending_duplicate 把「同会话同参数图在途」当成
    模型的重复误提交拦掉，跑图台批量 5 张串行入队时第 2 张起被吞，只跑出 2 张，
    回执却按用户点的渠道写「5 张全排上」。修法是跑图台落队时显式跳过查重。
    """

    def setUp(self):
        with open(rb.__file__, encoding="utf-8") as f:
            self.rb_src = f.read()
        import app.features.image.direct_gen as dg
        import app.tools.normal.generate_image as gi
        with open(dg.__file__, encoding="utf-8") as f:
            self.dg_src = f.read()
        with open(gi.__file__, encoding="utf-8") as f:
            self.gi_src = f.read()

    def test_run_channel_passes_skip_dup(self):
        # 跑图台落队必须传 skip_dup=True，否则查重会把同渠道的第 2~5 张吞掉。
        self.assertIn("skip_dup=True", self.rb_src)

    def test_generate_image_has_skip_dup_guard(self):
        # 两处查重点（NAI 分支 + 本地分支）都得带 not skip_dup 守卫。
        self.assertIn("character=\"\", skip_dup=False", self.gi_src)
        self.assertEqual(self.gi_src.count("not skip_dup and image_jobs.find_pending_duplicate"), 2,
                         "NAI 与本地两处查重都要能被 skip_dup 跳过")

    def test_enqueue_forwards_skip_dup(self):
        self.assertIn("character=\"\", skip_dup=False", self.dg_src)
        self.assertIn("user_text=text, skip_dup=skip_dup", self.dg_src)

    def test_no_dup_check_keeps_qq_default(self):
        # QQ/群友那条路不传 skip_dup，默认 False，查重行为不变。
        import inspect
        import app.tools.normal.generate_image as gi
        sig = inspect.signature(gi._generate_image)
        self.assertFalse(sig.parameters["skip_dup"].default)


class BatchEndpointTest(unittest.TestCase):
    """接口层：重复渠道放行、正常单回批次号。"""

    def setUp(self):
        self.app = _app()
        self.c = self.app.test_client()
        self.tok = _tok()
        self.ids = _chan_ids(6)
        rb._JOBS.clear()
        rb._BATCHES.clear()
        self._old_run = rb._run_batch
        rb._run_batch = lambda *a, **k: None        # 不真跑

    def tearDown(self):
        rb._run_batch = self._old_run
        rb._JOBS.clear()
        rb._BATCHES.clear()

    def _post(self, body):
        return self.c.post("/api/runbench/gen-many", json=dict(body, k=self.tok))

    def test_duplicate_in_same_mode_is_accepted(self):
        # 2026-11-26「抽卡」：一图多风 5 格全选同一渠道也收单，各跑不同种子。
        r = self._post({"mode": "same", "prompt": "", "items": [
            {"skill": self.ids[0], "image": IMG} for _ in range(5)]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["count"], 5)

    def test_same_channel_twice_is_ok_in_each_mode(self):
        # 五图各画：同渠道 3 张应当正常收单，不被重复校验拦。
        r = self._post({"mode": "each", "prompt": "", "items": [
            {"skill": self.ids[0], "image": IMG},
            {"skill": self.ids[0], "image": IMG},
            {"skill": self.ids[0], "image": IMG}]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["count"], 3)

    def test_five_in_a_row_all_accepted(self):
        # 用户原话：「5 张就是 5 张」——连跑 5 张全部收单，不再有额度和在途闸。
        r = self._post({"mode": "each", "prompt": "", "items": [
            {"skill": self.ids[i], "image": IMG} for i in range(5)]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["count"], 5)

    def test_repeat_submit_is_not_blocked(self):
        # 原来「上一批没跑完不让开下一批」，删掉后连着提交第二批必须放行。
        for _ in range(3):
            r = self._post({"mode": "text", "prompt": "一只猫", "items": [
                {"skill": self.ids[0]}, {"skill": self.ids[1]}]})
            self.assertEqual(r.status_code, 200, r.get_json())

    def test_ok_returns_bid(self):
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

    def test_rejected_slot_not_counted_as_sent(self):
        # 2026-11-26：某格被转译判「不用画」（reply 分支）时，必须以「错误」
        # 开头返回，_run_batch 才不会打绿勾、sent 才不会 +1。
        def fake(prompt, skill, image="", tags=None):
            self.calls.append((skill, tags))
            return "错误: 这一格没排上——模型判断不用画" if skill == self.ids[1] else ""
        rb._run_channel = fake
        rows = [{"image": IMG, "prompt": "x", "skill": s} for s in self.ids]
        self._run("same", rows)
        batch = rb._BATCHES["B-T"]
        self.assertEqual(batch["queued"], 2, "只算真正入队的两张")
        self.assertIn("1 张没排上", self.receipts[0])
        self.assertEqual(len(self.receipts[0].split("渠道：")[1].strip().split("、")), 2,
                         "回执渠道行只列实际入队的两个")

    def test_receipt_lists_only_queued_skills(self):
        # 回执列真正入队的渠道，不列用户点的 rows（2026-11-26）。
        def fake(prompt, skill, image="", tags=None):
            self.calls.append((skill, tags))
            return "" if skill != self.ids[2] else "错误: 拒绝"
        rb._run_channel = fake
        rows = [{"image": IMG, "prompt": "x", "skill": s} for s in self.ids]
        self._run("same", rows)
        listed = self.receipts[0].split("渠道：")[1].strip().split("、")
        self.assertEqual(listed, self.ids[:2])


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

    def test_batch_panel_has_all_three_modes(self):
        self.assertIn('data-mode="same"', self.html)
        self.assertIn('data-mode="each"', self.html)
        # 2026-11-25 新增：文生图五连（一句话，5 家渠道各画一张）
        self.assertIn('data-mode="text"', self.html)

    def test_prompt_box_only_in_text_mode(self):
        # 用户原话：「一图多风删掉那个什么粘贴框、提示词输入栏，不要了」；
        # 「文生图五连只需要有一个提示词输入栏就行了」。
        # 判据：需求框默认是 hidden，且只有 text 模式才显示。
        self.assertIn('<div id="promptWrap" class="hidden">', self.html)
        self.assertIn('$("promptWrap").classList.toggle("hidden", bMode !== "text");',
                      self.html)

    def test_text_mode_has_no_image_slot(self):
        # 文生图五连不要图：只有 each / same 第一格才画图片区。
        self.assertIn('if(showPic || (bMode === "same" && i === 0))', self.html)

    def test_all_same_pops_visible_tip(self):
        # 用户原话：「必须弹出来这样一个提示，已全部同第一款」。
        self.assertIn('id="flashTip"', self.html)
        self.assertIn("已全部同第一款", self.html)
        self.assertIn("function flashTip(", self.html)

    def test_no_limit_wording_left(self):
        # 那句「这一小时发太多次了，歇会儿」和高峰话术都不许出现在页面上。
        self.assertNotIn("发太多次", self.html)
        self.assertNotIn("高峰期", self.html)

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

    def test_no_dup_check_anywhere(self):
        # 2026-11-26 用户拍板「抽卡」：同一渠道连跑 5 张（种子不同）三种玩法
        # 全放开。前端 usedBy 恒 false（不置灰、不拦），提交前不再有 dup 校验，
        # 切模式也不再清空重复渠道。
        self.assertIn("function usedBy(skill, idx){\n    return false;", self.html)
        self.assertNotIn("一图多风每个格子要换不同的渠道", self.html)
        self.assertNotIn("选了两遍", self.html)
        self.assertNotIn("var keep = {};", self.html)

    def test_mode_switch_keeps_dup_skills(self):
        # 从「五图各画」切回「一图多风」时 5 格全是同一渠道也保留——那正是
        # 「一张图连跑 5 次同渠道」的意思（2026-11-26）。
        self.assertNotIn("var keep = {};", self.html)

    def test_same_all_button_only_in_each_mode(self):
        # 2026-11-25 用户拍板：「全部同第一款」只属于五图各画，而且要在
        # 图片格**上方**（「第一个图头顶那边，一个大大的高亮按钮」）。
        # 一图多风里这个按钮必须隐藏——那边 5 格本来就要求不同渠道。
        self.assertIn('$("btnSame").classList.toggle("hidden", same);', self.html)
        # 按钮在 #slots 之前
        i_btn = self.html.index('id="btnSame"')
        i_slots = self.html.index('id="slots"')
        self.assertLess(i_btn, i_slots, "「全部同第一款」必须排在格子容器前面")

    def test_same_all_never_returns_silently(self):
        # 2026-11-25 bug：「5 格都传了图之后点全部同第一款，一点反应都没有，
        # 也没有提示」。根因是开头那句 `if(bBusy || bMode !== "each") return;`
        # 把两种「没铺成」都吞掉了。现在每种情况都要说话。
        i = self.html.index('$("btnSame").addEventListener("click"')
        j = self.html.index("\n  });", i)
        body = self.html[i:j]
        self.assertNotIn('if(bBusy || bMode !== "each") return;', body,
                         "不许再静默 return")
        self.assertIn("这批还在跑", body)      # bBusy 要说
        self.assertIn("先切过去", body)        # 模式不对要说
        # 2026-11-25 用户原话：「必须弹出来这样一个提示，已全部同第一款」。
        self.assertIn("已全部同第一款", body)
        self.assertIn("flashTip(", body, "成功必须弹提示条，不能只写状态行")

    def test_same_all_pins_select_value_to_data(self):
        # 光改 bSlots 不够：界面上的 <select> 还得跟着变，否则用户看到的是
        # 「值改了但四栏没动」。renderSlots 结尾必须按数据显式对齐 value。
        self.assertIn("sel.value = mine;", self.html)


if __name__ == "__main__":
    unittest.main()
