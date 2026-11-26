# -*- coding: utf-8 -*-
"""第三批（2026-11-24 晚，用户第 1、2、3 条）。

· 复制按钮要给**实际出图用的那串提示词**，不是用户敲的原话；
· 「排队中…」假卡片的来源是存档里 `file` 和 `job` 都空的那条 `img`，
  它既不会被轮询也不该原样重放；
· 画廊不能因为一次扫描 / 清空就整面空白——`gallery.scan()` 必须能把
  磁盘上的东西全扫回来。
"""
import io
import os
import tempfile
import unittest
from unittest import mock

from app.features.gallery import gallery

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
WS = os.path.join(ROOT, "web", "workspace")


def _read(*parts):
    with io.open(os.path.join(WS, *parts), "r", encoding="utf-8") as f:
        return f.read()


class TestCopyGivesRealPrompt(unittest.TestCase):
    """复制拿到的必须是服务端翻完 / 拼完的那串英文 tags。"""

    def test_card_prompt_takes_job_prompt(self):
        # 2026-11-24 晚：提交 / 轮询搬到 gen_flow.js 了，这一段跟着过去
        # 2026-11-26：挂牌那一段又和「断线认领回来」合并成 archive.js 的
        # `attach`（gen_flow 贴着 600 行硬线，而它本来就是「把卡接回任务」）
        js = _read("js", "archive.js")
        i = js.index("function attach(card, rec, job)")
        seg = js[i:js.index("function revive(", i)]
        self.assertIn("job.prompt", seg,
                      "提交成功要把 job.prompt 记成复制源")
        self.assertIn("card.prompt = job.prompt", seg)

    def test_server_returns_final_prompt(self):
        """最终串在 `_public(job)` 里带出去（翻译 / 画师串 / 参考图都已合并）。"""
        from app.features.image import routes as gen_api
        job = {"id": "x", "skill": "silver", "prompt": "1girl, solo",
               "state": "queued", "ts": 0}
        self.assertEqual(gen_api._public(job)["prompt"], "1girl, solo")


class TestFakeQueueCard(unittest.TestCase):
    """存档里那条假「排队中」必须变成明确的失败态。"""

    def test_replay_fails_card_without_job(self):
        js = _read("js", "archive.js")
        i = js.index('} else if (m.k === "img") {')
        seg = js[i:js.index('} else if (m.k === "comic")', i)]
        # 没有 file 的分支里不能再出现把「排队中」原样铺出来的写法
        self.assertNotIn("again.push({ rec: m, card", seg,
                         "没有 job 的那条不该进轮询队列")
        self.assertIn("failCard", seg, "应该直接判成「没跑完」")

    def test_fail_writes_back_to_server(self):
        js = _read("js", "gen_flow.js")
        i = js.index("function fail(card, msg, stuck)")
        seg = js[i:i + 1100]
        self.assertIn("H.patch", seg, "失败要回写服务端存档")
        # patch 必须在清 job 之前——patch 靠 job 认人
        self.assertLess(seg.index("H.patch"), seg.index('card.rec.job = ""'))

    def test_show_writes_back_to_server(self):
        # 出图 / 判失败 2026-11-26 已经搬到 gen_flow.js（chat.js 只剩转发），
        # 断言跟着过去；那边调的是宿主注入的 `H.patch`。
        js = _read("js", "gen_flow.js")
        i = js.index("function showImage")
        seg = js[i:js.index("function fail(", i)]
        self.assertIn("H.patch", seg, "出图也要回写服务端存档")

    def test_store_has_patch(self):
        js = _read("js", "store.js")
        self.assertIn("function patch(rec, fields)", js)
        self.assertIn('method: "PATCH"', js)
        self.assertIn("patch: patch", js, "得导出")

    def test_patch_keeps_identity_keys(self):
        """回写不能把 `id` / `job` 摊平进 body（2026-11-26 实测 400 的根因）。

        `gen_flow` 会带 `job: ""` 表示「这张已经收尾」，摊平写就等于把唯一的
        身份凭据一起清成空串，服务端只认「没给 job 或 id」→ 400，出图结果
        一条都写不回存档（用户看到的就是「图有了、记录里没有」）。现在要改
        的字段一律走 `m`，身份键单独放顶层。
        """
        js = _read("js", "store.js")
        i = js.index("function patch(rec, fields)")
        seg = js[i:js.index("/* ── 服务端", i)]
        self.assertIn("m: fields", seg, "要改的字段得塞进 m")
        self.assertNotIn("body[k] = f[k]", seg, "不能把 fields 摊平进 body")

    def test_backend_accepts_patch(self):
        from app import main
        rules = [r for r in main.app.url_map.iter_rules()
                 if str(r).startswith("/api/chat/archive")]
        self.assertTrue(rules, "档案路由没注册上")
        meths = set()
        for r in rules:
            meths |= {m.upper() for m in r.methods}
        self.assertIn("PATCH", meths, "后端没挂 PATCH，前端回写会 405")

    def test_gateway_forwards_patch(self):
        with io.open(os.path.join(ROOT, "friend_app.py"),
                     "r", encoding="utf-8") as f:
            src = f.read()
        i = src.index('/api/chat/<path:sub>')
        self.assertIn("PATCH", src[i:i + 200],
                      "网关不放行 PATCH，手机端回写就 405")


class TestScanRestoresGallery(unittest.TestCase):
    """一次扫描要能把磁盘上的东西全扫回来（本轮画廊清空的直接教训）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="gal_scan_")
        self._db = gallery.DB
        gallery.DB = os.path.join(self.tmp, "g.db")
        gallery.init()

    def tearDown(self):
        gallery.DB = self._db

    def _mk(self, name, n=2):
        d = os.path.join(self.tmp, name)
        os.makedirs(d, exist_ok=True)
        for i in range(n):
            with io.open(os.path.join(d, "p%d.png" % i), "wb") as f:
                # 最小合法 PNG 头足够——扫描只看扩展名和大小
                f.write(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
        return d

    def test_scan_picks_up_new_dirs(self):
        self._mk("甲", 3)
        self._mk("乙", 2)
        out = gallery.scan_path(self.tmp)
        self.assertGreaterEqual(out.get("added", 0), 2)
        with gallery.connect() as con:
            n = con.execute("SELECT COUNT(*) c FROM works WHERE n>0").fetchone()["c"]
            items = con.execute("SELECT COUNT(*) c FROM items").fetchone()["c"]
        self.assertEqual(n, 2)
        self.assertEqual(items, 5)

    def test_rescan_keeps_hidden_marks(self):
        d = self._mk("丙", 2)
        gallery.scan_path(d)
        with gallery.connect() as con:
            row = con.execute("SELECT id FROM works WHERE title='丙'").fetchone()
            wid = row["id"]
            iid = con.execute("SELECT id FROM items WHERE work_id=?"
                              " ORDER BY idx LIMIT 1", (wid,)).fetchone()["id"]
        gallery.set_item_hidden(iid, 1)
        gallery.scan_path(d)          # 再来一次，隐藏标记要还在
        with gallery.connect() as con:
            still = con.execute("SELECT COALESCE(hidden,0) h FROM items WHERE id=?",
                                (iid,)).fetchone()["h"]
        self.assertEqual(still, 1, "重新扫描把单张隐藏标记丢了")


class TestUnnamedSessionsDoNotShareOneSlot(unittest.TestCase):
    """各台自助提交（`enqueue(None, None, ...)`）不该互相占名额。

    2026-11-24 实况：一张 moss 漫画在画，跑图台点发送直接被判「你已经有一张
    在画了」——因为无名主全机拼成同一个键 `("", "")`，而 `MAX_INFLIGHT=1`。
    用户看到的是 5174 闲着自己点不动图。
    """

    def setUp(self):
        from app.features.image import jobs as image_jobs
        self.jobs = image_jobs
        # 上限按时段变；这两条用例写的是固定 1/5 的话术，钉在非高峰。
        p = mock.patch.object(image_jobs, "inflight_limit",
                              return_value=(image_jobs.MAX_INFLIGHT, "", ""))
        p.start()
        self.addCleanup(p.stop)
        self._saved = [(c, dict(c.per_session)) for c in
                       (image_jobs._COMFY, image_jobs._NAI)]

    def tearDown(self):
        for chan, snap in self._saved:
            chan.per_session.clear()
            chan.per_session.update(snap)

    def test_two_desk_submits_both_get_through(self):
        a, why_a = self.jobs.enqueue(None, None, {"1": {}}, skill="krea2",
                                    batch="comic")
        self.assertIsNone(why_a)
        b, why_b = self.jobs.enqueue(None, None, {"1": {}}, skill="krea2")
        self.assertIsNone(why_b, "跑图台被一张漫画挡住了：无名主还在共用名额")
        self.assertNotEqual(a.lock_key, b.lock_key, "两把锁还是同一个键")
        self.assertEqual(self.jobs.inflight_count(None, None), 0,
                         "无名主不按会话计数，交给 max_queue / max_heavy 管")

    def test_real_sessions_still_one_at_a_time(self):
        """修正不能把真人那份名额一起放开：同一个人还是一次一张。"""
        from app.features.qq import api as qq_api
        qq_api.bind_context("group_9", "group", "9",
                            message_id="1", sender_uin=1001)
        self.addCleanup(qq_api.clear_context)
        for _ in range(self.jobs.MAX_INFLIGHT):
            job, why = self.jobs.enqueue("group", "9", {"1": {}}, "hd_3_soft")
            self.assertIsNone(why)
        _, why = self.jobs.enqueue("group", "9", {"1": {}}, "hd_3_soft")
        self.assertIn("排着", why)

