"""跑图台（app/gen_api.py）测试。

覆盖：渠道清单（本地渠道要求 workflow、NAI 云端渠道不要求 workflow）、提交
校验、worker 落账本（ComfyUI 与 NAI 成功/失败）、状态轮询、历史（倒序 / 清空
截止）、供图代理（路径穿越防护 / ComfyUI 不在线 / NAI 临时图片）。

账本经 state_path 在测试进程自动落临时目录，不碰生产 state/。
"""

import json
import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from unittest import mock

from flask import Response

from app import comic, gen_api, image_jobs, main
from app.skills import load_skill


def _wait(pred, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.05)
    return False


class ChannelsTest(unittest.TestCase):
    def setUp(self):
        self.c = main.app.test_client()

    def test_list_includes_nai_and_local_channels_require_workflow(self):
        d = self.c.get("/api/gen/channels").get_json()
        self.assertTrue(d["ok"])
        by_id = {c["id"]: c for c in d["channels"]}
        self.assertIn("silver", by_id)
        self.assertIn("nai", by_id)
        self.assertIn("nai_wide", by_id)
        self.assertTrue(by_id["nai"]["cloud"])
        self.assertTrue(by_id["nai_wide"]["cloud"])
        self.assertIn("竖版", by_id["nai"]["summary"])
        self.assertIn("横版", by_id["nai_wide"]["summary"])
        for cid, meta in by_id.items():
            if meta.get("cloud"):
                continue
            sd = load_skill(cid)
            self.assertIsNotNone(sd, cid)
            self.assertTrue(sd.get("workflow"), cid)

    def test_head_channels_come_first(self):
        d = self.c.get("/api/gen/channels").get_json()
        ids = [c["id"] for c in d["channels"]]
        self.assertEqual(ids[0], "silver")           # 常用置顶，silver 是默认档


class SubmitTest(unittest.TestCase):
    def setUp(self):
        self.c = main.app.test_client()

    def _submit(self, **kw):
        body = {"skill": "silver", "prompt": "1girl, solo"}
        body.update(kw)
        return self.c.post("/api/gen/submit", json=body)

    def tearDown(self):
        # active-cap 用例用占位线程模拟排队；结束后清掉 queued 状态，
        # 避免污染后续提交校验用例。
        with gen_api._JOBS_LOCK:
            for job in gen_api._JOBS.values():
                if job["state"] == "queued":
                    job["state"] = "done"

    def test_empty_prompt_is_400(self):
        r = self._submit(prompt="   ")
        self.assertEqual(r.status_code, 400)
        self.assertIn("空", r.get_json()["error"])

    def test_unknown_skill_is_400(self):
        r = self._submit(skill="不存在的渠道")
        self.assertEqual(r.status_code, 400)
        self.assertIn("未知渠道", r.get_json()["error"])

    def test_too_long_prompt_is_400(self):
        r = self._submit(prompt="x" * (gen_api._MAX_PROMPT + 1))
        self.assertEqual(r.status_code, 400)
        self.assertIn("太长", r.get_json()["error"])

    def test_artist_prefix_is_accepted_only_for_nai(self):
        def finish(job):
            job["state"] = "done"

        with mock.patch.object(gen_api, "_run", side_effect=finish):
            r = self._submit(skill="nai", prompt="1girl, solo",
                             artist_prefix="@artist_a, @artist_b")
        self.assertEqual(r.status_code, 200)
        jid = r.get_json()["job"]["id"]
        self.assertTrue(_wait(lambda: gen_api._JOBS[jid]["state"] == "done"))
        self.assertEqual(r.get_json()["job"]["prompt"],
                         "@artist_a, @artist_b, 1girl, solo")

        with mock.patch.object(gen_api, "_run", side_effect=finish):
            r = self._submit(skill="silver", prompt="1girl, solo",
                             artist_prefix="@must_not_leak")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["job"]["prompt"], "1girl, solo")

    def test_artist_prefix_too_long_is_400(self):
        r = self._submit(skill="nai", artist_prefix="x" * (gen_api._NAI_PREFIX_MAX + 1))
        self.assertEqual(r.status_code, 400)
        self.assertIn("画师串太长", r.get_json()["error"])

    def test_active_cap_rejects_sixth(self):
        """在途满 5 张时第 6 张被拒（image_jobs 每会话在途也是 5，话术要先说清）。"""
        gate = threading.Event()

        def _block(job):
            gate.wait(5)

        with mock.patch.object(gen_api, "_run", _block):
            for _ in range(5):
                self.assertEqual(self._submit().status_code, 200)
            r = self._submit()
            self.assertEqual(r.status_code, 400)
            self.assertIn("等一张再发", r.get_json()["error"])
            gate.set()      # 放 5 条占位线程退场，别留挂在测试进程里


class RunTest(unittest.TestCase):
    """worker：提交链路走通 → 记账（成功/失败都记）。"""

    def setUp(self):
        self.c = main.app.test_client()
        try:
            os.remove(gen_api._CLEARED_AT)
        except OSError:
            pass

    def _wait_job(self, jid, state):
        return _wait(lambda: any(
            j["id"] == jid and j["state"] == state
            for j in self.c.get("/api/gen/status").get_json()["jobs"]))

    def test_success_writes_ledger_row(self):
        with mock.patch.object(comic, "_submit", return_value="Ani4K_00001_.png"), \
             mock.patch.object(comic, "_submit_retry",
                               side_effect=lambda fn, what: fn()):
            jid = self.c.post("/api/gen/submit",
                              json={"skill": "silver",
                                    "prompt": "1girl, solo, white hair"}
                              ).get_json()["job"]["id"]
            self.assertTrue(self._wait_job(jid, "done"))
            rows = gen_api._ledger_rows()
            row = next(r for r in rows if r["tag"] == jid)
            self.assertTrue(row["ok"])
            self.assertEqual(row["skill"], "silver")
            self.assertEqual(row["prompt"], "1girl, solo, white hair")
            self.assertEqual(row["file"], "Ani4K_00001_.png")
            self.assertIsInstance(row["seed"], int)

    def test_failure_writes_ledger_row_with_error(self):
        with mock.patch.object(comic, "_submit_retry",
                               side_effect=RuntimeError("连不上 ComfyUI")):
            jid = self.c.post("/api/gen/submit",
                              json={"skill": "silver", "prompt": "1girl"}
                              ).get_json()["job"]["id"]
            self.assertTrue(self._wait_job(jid, "error"))
            row = next(r for r in gen_api._ledger_rows() if r["tag"] == jid)
            self.assertFalse(row["ok"])
            self.assertIn("连不上", row["error"])

    def test_nai_success_uses_artist_prefixed_prompt(self):
        class FakeJob:
            def wait(self):
                return {"images": [r"C:\\Temp\\nai_prefixed.png"]}

        with mock.patch.object(image_jobs, "enqueue",
                               return_value=(FakeJob(), None)) as enqueue:
            jid = self.c.post("/api/gen/submit", json={
                "skill": "nai", "prompt": "1girl, solo",
                "artist_prefix": "@artist_a"
            }).get_json()["job"]["id"]
            self.assertTrue(self._wait_job(jid, "done"))
            args, kw = enqueue.call_args
            self.assertEqual(args[2], "@artist_a, 1girl, solo")
            self.assertEqual(kw["prompt"], "@artist_a, 1girl, solo")
            row = next(r for r in gen_api._ledger_rows() if r["tag"] == jid)
            self.assertEqual(row["prompt"], "@artist_a, 1girl, solo")

    def test_nai_success_uses_cloud_queue_and_records_controlled_ref(self):
        class FakeJob:
            def wait(self):
                return {"images": [r"C:\\Temp\\nai_ab12.png"]}

        with mock.patch.object(image_jobs, "enqueue",
                               return_value=(FakeJob(), None)) as enqueue:
            jid = self.c.post("/api/gen/submit",
                              json={"skill": "nai", "prompt": "1girl, solo"}
                              ).get_json()["job"]["id"]
            self.assertTrue(self._wait_job(jid, "done"))
            job = next(j for j in self.c.get("/api/gen/status").get_json()["jobs"]
                       if j["id"] == jid)
            self.assertEqual(job["file"], "nai:nai_ab12.png")
            self.assertIsNone(job["seed"])
            args, kw = enqueue.call_args
            self.assertIsNone(args[0])
            self.assertEqual(args[2], "1girl, solo")
            self.assertEqual(kw["skill"], "nai")
            row = next(r for r in gen_api._ledger_rows() if r["tag"] == jid)
            self.assertTrue(row["ok"])
            self.assertEqual(row["file"], "nai:nai_ab12.png")
            self.assertIsNone(row["seed"])

    def test_skill_without_workflow_errors_clearly(self):
        with mock.patch.object(gen_api, "generate_image_build",
                               return_value=None):
            jid = self.c.post("/api/gen/submit",
                              json={"skill": "silver", "prompt": "1girl"}
                              ).get_json()["job"]["id"]
            self.assertTrue(self._wait_job(jid, "error"))
            job = next(j for j in self.c.get("/api/gen/status")
                       .get_json()["jobs"] if j["id"] == jid)
            self.assertIn("没有文生图工作流", job["error"])


class HistoryTest(unittest.TestCase):
    def setUp(self):
        self.c = main.app.test_client()
        # 账本和截止戳在同进程的测试间共享（temp dir）：每个用例前把截止戳撤掉，
        # 并用「远在未来的时间戳」保证自己的行一定最新、一定过得了截止。
        try:
            os.remove(gen_api._CLEARED_AT)
        except OSError:
            pass
        self.base = time.time() + 10000

    def test_rows_newest_first_and_limited(self):
        tags = ["GP-a", "GP-b", "GP-c", "GP-d", "GP-e"]
        for i, tag in enumerate(tags):
            gen_api._ledger_append({"tag": tag, "skill": "silver",
                                    "prompt": "p%d" % i, "seed": i,
                                    "file": "f%d.png" % i,
                                    "ts": self.base - i * 10, "ok": True})
        rows = self.c.get("/api/gen/history").get_json()["rows"]
        mine = [r["tag"] for r in rows if r["tag"] in tags]
        self.assertEqual(mine, tags)                  # 新的在前
        two = self.c.get("/api/gen/history?limit=2").get_json()["rows"]
        self.assertEqual(len(two), 2)
        self.assertEqual([r["tag"] for r in two],
                         [r["tag"] for r in rows[:2]])   # limit 就是全表前 N 行

    def test_clear_hides_older_rows_but_keeps_new_ones(self):
        gen_api._ledger_append({"tag": "GP-old", "skill": "silver", "prompt": "p",
                                "seed": 1, "file": "a.png",
                                "ts": time.time() - 100, "ok": True})
        r = self.c.post("/api/gen/clear", json={})
        self.assertTrue(r.get_json()["ok"])
        rows = self.c.get("/api/gen/history").get_json()["rows"]
        self.assertNotIn("GP-old", [x["tag"] for x in rows])
        # 截止之后新记的照样出现（清空只挡显示，不挡记账）
        gen_api._ledger_append({"tag": "GP-new", "skill": "silver", "prompt": "p",
                                "seed": 2, "file": "b.png",
                                "ts": self.base + 100, "ok": True})
        rows = self.c.get("/api/gen/history").get_json()["rows"]
        self.assertIn("GP-new", [x["tag"] for x in rows])


class ImgTest(unittest.TestCase):
    def setUp(self):
        self.c = main.app.test_client()

    def test_path_traversal_and_junk_are_rejected(self):
        """相对路径是**允许**的（输出图按会话分目录），但 `..` 和怪名字一律不放。

        2026-10-18 之前这里连 `a/b.png` 都拒——那时 output 是平的。现在
        `f=web/生图页/silver/Anima_1_.png` 是正常形态，所以只有真越界才 400。
        """
        for bad in ("", "..", "../x.png", "a/../../x.png", "x.png;.js",
                    "..\\x.png", "a/..", "空名字?.png"):
            r = self.c.get("/api/gen/img?f=" + bad)
            self.assertEqual(r.status_code, 400, bad)

    def test_relative_path_passes_the_subfolder_through(self):
        resp = mock.Mock(status_code=200, content=b"\x89PNG-fake")
        with mock.patch.object(gen_api._session, "get",
                               return_value=resp) as g:
            r = self.c.get("/api/gen/img?f=web/生图页/silver/Anima_1_.png")
            self.assertEqual(r.status_code, 200)
            self.assertEqual(g.call_args.kwargs["params"],
                             {"filename": "Anima_1_.png",
                              "subfolder": "web/生图页/silver"})

    def test_good_name_proxies_comfy_view(self):
        resp = mock.Mock(status_code=200, content=b"\x89PNG-fake")
        with mock.patch.object(gen_api._session, "get",
                               return_value=resp) as g:
            r = self.c.get("/api/gen/img?f=Ani4K_00001_.png")
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.data, b"\x89PNG-fake")
            self.assertEqual(r.mimetype, "image/png")
            self.assertEqual(g.call_args.kwargs["params"],
                             {"filename": "Ani4K_00001_.png"})

    def test_nai_image_uses_controlled_temp_directory(self):
        sent = Response(b"png", mimetype="image/png")
        with mock.patch.object(gen_api.image_out, "_out_dir",
                               return_value="C:/safe/nai"), \
             mock.patch.object(gen_api, "send_from_directory",
                               return_value=sent) as send:
            r = self.c.get("/api/gen/img?f=nai:nai_ab12.png")
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.data, b"png")
            send.assert_called_once_with("C:/safe/nai", "nai_ab12.png",
                                         mimetype="image/png", conditional=False)

    def test_nai_absolute_path_is_rejected(self):
        r = self.c.get("/api/gen/img?f=nai:C:/Windows/win.ini")
        self.assertEqual(r.status_code, 400)

    def test_comfy_offline_is_502_with_reason(self):
        import requests
        with mock.patch.object(gen_api._session, "get",
                               side_effect=requests.ConnectionError("refused")):
            r = self.c.get("/api/gen/img?f=x.png")
            self.assertEqual(r.status_code, 502)
            self.assertIn("不在线", r.get_json()["error"])

    def test_missing_file_is_404(self):
        resp = mock.Mock(status_code=200, content=b"")
        with mock.patch.object(gen_api._session, "get", return_value=resp):
            r = self.c.get("/api/gen/img?f=gone.png")
            self.assertEqual(r.status_code, 404)


class PageTest(unittest.TestCase):
    def test_gen_page_served(self):
        r = main.app.test_client().get("/gen")
        self.assertEqual(r.status_code, 200)
        self.assertIn("跑图台", r.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
