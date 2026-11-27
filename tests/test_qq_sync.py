# -*- coding: utf-8 -*-
"""工作台「同步发 QQ」（2026-11-27）守卫测试。

用户原话：「如果用户已经登录的话，那么我们可以加一个按钮，就是默认是勾选
绿色的，就是同步发 QQ」——在网页上画，手机上也能收到。

锁四件事，每件错了都**照样能出图**，跑通了看不出来：

1. 开关只对登录用户露，默认勾上，选过的记在本地。
2. 勾着提交的那一张，任务上带着「发到哪个 QQ」；没勾就不带。
3. 出图后单独一条线程投递，**不占 worker**（压在 worker 里会让
   `_thread_alive` 迟落，`_reap_stuck` 会当成卡死）。
4. 投递失败只留一句提示、不判这张图失败——图本身是好的。
"""
import os
import tempfile
import unittest
from unittest import mock

import app as app_pkg  # noqa: F401  （确保 app 包先加载）
from app.features.image import routes as R

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WS = os.path.join(ROOT, "web", "workspace")


def _read(rel):
    with open(os.path.join(WS, rel), encoding="utf-8") as f:
        return f.read()


class LocalFileTest(unittest.TestCase):
    """任务里的图引用 → 本机真实路径。取错了就会往 QQ 发一张空图。"""

    def test_nai_ref_points_into_out_dir(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "a.png")
            with open(path, "wb") as f:
                f.write(b"x")
            with mock.patch.object(R.image_out, "_out_dir", return_value=d):
                self.assertEqual(R._local_file("nai:a.png"), path)

    def test_nai_ref_with_bad_name_is_rejected(self):
        """`nai:` 后面只认纯文件名，路径穿越一律不认（同 /api/gen/img）。"""
        with mock.patch.object(R.image_out, "_out_dir", return_value="/tmp"):
            self.assertEqual(R._local_file("nai:../x.png"), "")

    def test_comfy_ref_goes_through_local_output(self):
        with mock.patch.object(R.comfy_out, "local_output_file",
                               return_value="C:/out/x.png"), \
                mock.patch.object(R.os.path, "isfile", return_value=True):
            self.assertEqual(R._local_file("web_1/x.png"), "C:/out/x.png")

    def test_missing_file_is_empty(self):
        with mock.patch.object(R.comfy_out, "local_output_file",
                               return_value=None):
            self.assertEqual(R._local_file("web_1/gone.png"), "")

    def test_empty_ref_is_empty(self):
        self.assertEqual(R._local_file(""), "")


class DeliverTest(unittest.TestCase):
    """真正投递那一步（`_qq_deliver`，`_sync_to_qq` 的线程体）。"""

    def setUp(self):
        self.calls = []
        self.notes = []
        self.fail = ""

        def _send(*a, **k):
            self.calls.append((a, k))
            if self.fail:
                raise RuntimeError(self.fail)

        self._p = [
            mock.patch.object(R, "_local_file", return_value="/tmp/a.png"),
            mock.patch.object(R.qq_api, "send_image", side_effect=_send),
            mock.patch.object(R, "_archive_back",
                              side_effect=lambda job, f: self.notes.append(f)),
        ]
        for p in self._p:
            p.start()

    def tearDown(self):
        for p in self._p:
            p.stop()

    def test_sends_a_private_image_to_the_user(self):
        R._qq_deliver({"id": "J1", "qq": "10001"}, "a.png")
        self.assertEqual(self.calls,
                         [(("private", "10001", "/tmp/a.png"), {})])
        self.assertEqual(self.notes, [])

    def test_failure_only_leaves_a_note(self):
        """投递失败只写一句提示：不打 `err`，别把这张好图判成失败。"""
        self.fail = "协议端连不上"
        R._qq_deliver({"id": "J2", "qq": "10001"}, "a.png")
        self.assertEqual(len(self.notes), 1)
        self.assertIn("没发到 QQ", self.notes[0]["note"])
        self.assertIn("协议端连不上", self.notes[0]["note"])
        self.assertNotIn("err", self.notes[0])

    def test_missing_file_explains_itself(self):
        with mock.patch.object(R, "_local_file", return_value=""):
            R._qq_deliver({"id": "J3", "qq": "10001"}, "gone.png")
        self.assertEqual(self.calls, [])
        self.assertIn("没找到文件", self.notes[0]["note"])

    def test_never_raises(self):
        """异常必须自己咽掉：线程里抛出去没人接，只会打一堆 traceback。"""
        self.fail = "boom"
        R._qq_deliver({"id": "J4", "qq": "10001"}, "a.png")


class ThreadGateTest(unittest.TestCase):
    """什么时候才该起线程。"""

    def test_only_starts_when_both_flag_and_qq_are_there(self):
        started = []

        def _thread(**k):
            started.append(k)
            return mock.Mock()

        with mock.patch.object(R.threading, "Thread", side_effect=_thread):
            R._sync_to_qq({"qq_sync": True, "qq": "10001"}, "a.png")
            self.assertEqual(len(started), 1)
            # 没勾 / 没身份（访客）：都不发
            R._sync_to_qq({"qq_sync": False, "qq": "10001"}, "a.png")
            R._sync_to_qq({"qq_sync": True, "qq": ""}, "a.png")
            R._sync_to_qq({}, "a.png")
            self.assertEqual(len(started), 1)


class DecideCarriesTest(unittest.TestCase):
    """`_web_decide` 把开关和身份一起带进任务 spec。"""

    def setUp(self):
        self.specs = []

        def _enq(spec, owner, user_text="", lowres_on=False):
            self.specs.append(spec)
            return ({"id": "J", "skill": spec["skill"],
                     "prompt": spec["prompt"]}, None)

        from app.features.image import direct_gen
        self.dg = direct_gen
        self._orig = direct_gen._translate
        direct_gen._translate = lambda text, hist, skill=None, **k: {
            "skill": "silver", "prompt": "P"}
        self._p = [
            mock.patch.object(R, "_enqueue_job", new=_enq),
            mock.patch.object(R, "_owner_key", return_value="10001"),
        ]
        for p in self._p:
            p.start()

    def tearDown(self):
        self.dg._translate = self._orig
        for p in self._p:
            p.stop()

    def _run(self, **ctx):
        base = {"images": [], "refs": []}
        base.update(ctx)
        return R._web_decide("画个猫", "silver", [], base)

    def test_flag_and_qq_reach_the_job(self):
        self._run(qq_sync=True, qq="10001")
        self.assertTrue(self.specs[0]["qq_sync"])
        self.assertEqual(self.specs[0]["qq"], "10001")

    def test_off_keeps_the_spec_clean(self):
        """没勾的时候一个键都不多带（直达 / 跑图台那几条路本就没有）。"""
        self._run()
        self.assertNotIn("qq_sync", self.specs[0])
        self.assertNotIn("qq", self.specs[0])

    def test_job_does_not_leak_qq_to_the_page(self):
        """任务下发前要脱敏：QQ 号只留在服务端。"""
        pub = R._public({"id": "J", "qq": "10001", "qq_sync": True})
        self.assertNotIn("qq", pub)


class FrontendTest(unittest.TestCase):
    """开关本身（chat.html / sync.js / gen_flow.js）。"""

    def test_switch_sits_beside_the_chips(self):
        html = _read("chat.html")
        self.assertIn('class="chip-row"', html)
        self.assertIn('id="tog-sync"', html)
        # 默认不露，等 /me 回来（访客没有 QQ，露出来就是死按钮）
        self.assertIn('id="tog-sync" type="button" hidden', html)

    def test_submit_carries_the_switch(self):
        js = _read("js/gen_flow.js")
        self.assertIn("body.qq_sync = true", js)
        self.assertIn("global.WSSync", js)

    def test_module_defaults_to_on_and_hides_for_non_users(self):
        js = _read("js/sync.js")
        self.assertIn("var ON = true;", js)
        self.assertIn('st.role !== "user"', js)
        self.assertIn("localStorage", js)

    def test_module_is_loaded_after_identity(self):
        html = _read("chat.html")
        self.assertLess(html.index("js/visitor.js"), html.index("js/sync.js"))
        self.assertIn("WSSync.init()", html)


if __name__ == "__main__":
    unittest.main()
