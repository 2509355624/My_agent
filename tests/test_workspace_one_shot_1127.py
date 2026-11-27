# -*- coding: utf-8 -*-
"""工作台「图 + 需求，一次调用出词」守卫（2026-11-27）。

用户口径（原话）：「一个识图模型，它本身就能够拿到图片和用户的需求，然后
去跑图……两次调用反而是最亏的」。

以前是「前端先识一遍填进输入框、后端拿那段中文再转一遍」：两次调用，而且
第二次的模型根本没看过图。现在锁三件事：

1. `direct_gen._tags_with_request` 只调**一次**视觉接口，用户需求进 prompt；
2. `_web_images` 认画廊私有 ref_id（以前只认 `cr_`，上传的图全被丢掉）；
3. 前端不再有发送前识图（`ref.js` / `chat.js` 静态检查）。
"""
import os
import tempfile
import unittest
from unittest import mock


class TagsWithRequestTest(unittest.TestCase):
    """图 + 需求 → 一次视觉调用 → 出图提示词。"""

    def test_one_call_carries_the_request(self):
        from app.features.image import direct_gen as dg
        calls = []

        def fake_describe(url, prompt=None, **kw):
            calls.append({"url": url, "prompt": prompt})
            return "1girl, white dress"

        with mock.patch("app.integrations.vision.describe", new=fake_describe):
            out = dg._tags_with_request(
                ["data:image/png;base64,AA"], "把裙子换成白色")
        self.assertEqual(out, "1girl, white dress")
        self.assertEqual(len(calls), 1, "一次调用就该出词，不许再走第二次")
        self.assertIn("把裙子换成白色", calls[0]["prompt"])

    def test_empty_request_still_one_call(self):
        """只挂了图没写字：也要一次调用，需求位给一句「照这张画」。"""
        from app.features.image import direct_gen as dg
        calls = []

        def fake_describe(url, prompt=None, **kw):
            calls.append(prompt)
            return "1girl"

        with mock.patch("app.integrations.vision.describe", new=fake_describe):
            dg._tags_with_request(["data:image/png;base64,AA"], "")
        self.assertEqual(len(calls), 1)
        self.assertIn("照这张画", calls[0])

    def test_read_failure_returns_empty(self):
        """读不出来返回空串，让调用方说人话；不静默退回纯文生图。"""
        from app.features.image import direct_gen as dg
        with mock.patch("app.integrations.vision.describe",
                        side_effect=RuntimeError("boom")):
            self.assertEqual(
                dg._tags_with_request(["data:image/png;base64,AA"]), "")

    def test_no_image_no_call(self):
        from app.features.image import direct_gen as dg
        with mock.patch("app.integrations.vision.describe") as d:
            self.assertEqual(dg._tags_with_request([], "画只猫"), "")
        d.assert_not_called()


class WebImagesTest(unittest.TestCase):
    """`_web_images` 两种 id 都要认得——画廊 ref_id 是工作台真正在用的那种。"""

    def test_gallery_ref_is_read_from_disk(self):
        from app.features.image import routes as R
        with tempfile.NamedTemporaryFile(delete=False, suffix=".webp") as f:
            f.write(b"RIFF")
            path = f.name
        try:
            with mock.patch(
                    "app.features.gallery.routes._read_private_image",
                    return_value=path), \
                 mock.patch("app.integrations.vision.to_data_url",
                            return_value="data:image/webp;base64,AA"):
                got = R._web_images(["A" * 24], "10001")
        finally:
            os.unlink(path)
        self.assertEqual(got, ["data:image/webp;base64,AA"])

    def test_gallery_item_id_is_read(self):
        """「画同款」传的是画廊 item id（数字），也要读出图来。

        以前 `_web_images` 直接 `if not isinstance(r, str): continue`，数字 id
        一个都进不来——画同款全程没把图交给模型。
        """
        from app.features.image import routes as R
        with mock.patch(
                "app.features.gallery.routes._read_image_data",
                return_value="data:image/webp;base64,BB"):
            self.assertEqual(R._web_images([42], "10001"),
                             ["data:image/webp;base64,BB"])
            self.assertEqual(R._web_images(["42"], "10001"),
                             ["data:image/webp;base64,BB"])

    def test_someone_elses_ref_is_skipped(self):
        """读不到（不是自己的 / 过期）就跳过，不猜不错配。"""
        from app.features.image import routes as R
        with mock.patch(
                "app.features.gallery.routes._read_private_image",
                return_value=None):
            self.assertEqual(R._web_images(["B" * 24], "10001"), [])

    def test_guest_without_identity_cannot_read(self):
        from app.features.image import routes as R
        with mock.patch(
                "app.features.gallery.routes._read_private_image") as g:
            self.assertEqual(R._web_images(["C" * 24], ""), [])
        g.assert_not_called()

    def test_logged_in_user_reads_hidden_work(self):
        """我看得见的图，后端就必须读得到（2026-11-27 用户报的 bug）。

        画廊列表带 `__jf=1`，工作台里隐藏单张 / 已下架作品都会露出来；可
        `/api/gen/ask` 本身不带这个标记，`_full()` 于是 False，同一张图被当
        「别人的隐藏图」拒掉 → 后端 0 张图 → 静默纯文生图。用户原话：
        「我看得到的图，后端能够读到就行了」。

        所以这里锁死：登录身份调 `_read_image_data` 时必须带 `full=True`。
        """
        from app.features.image import routes as R
        seen = {}

        def fake(iid, qq, full=None):
            seen["full"] = full
            seen["qq"] = qq
            return "data:image/webp;base64,CC"

        with mock.patch(
                "app.features.gallery.routes._read_image_data", side_effect=fake):
            got = R._web_images([42], "10001")
        self.assertEqual(got, ["data:image/webp;base64,CC"])
        self.assertTrue(seen["full"], "登录用户要按工作台可见性读，否则读不到隐藏图")


class LostImageTest(unittest.TestCase):
    """带了参考图却一张都没读出来：必须说出来，不许静默按纯文字画。"""

    def test_ref_without_image_does_not_fall_back_to_text(self):
        from app.features.image import routes as R
        from app.features.image import direct_gen as dg
        with mock.patch.object(dg, "_translate") as tr, \
             mock.patch.object(dg, "_tags_with_request") as tags, \
             mock.patch.object(R, "_owner_key", return_value="__test__"):
            with self.assertRaises(RuntimeError) as ctx:
                R._web_decide("照着这张画", "silver", [],
                              {"images": [], "refs": [42], "qq": "10001"})
        self.assertIn("没读到", str(ctx.exception))
        tr.assert_not_called()
        tags.assert_not_called()

    def test_pure_text_still_ok(self):
        """没带图就照常走转译——别把纯文生图也拦了。"""
        from app.features.image import routes as R
        from app.features.image import direct_gen as dg
        with mock.patch.object(
                dg, "_translate", return_value={"skill": "silver", "prompt": "P"}), \
             mock.patch.object(R, "_owner_key", return_value="__test__"):
            got = R._web_decide("画只猫", "silver", [],
                                {"images": [], "refs": [], "qq": "10001"})
        self.assertEqual(got["prompt"], "P")


class FrontendGuardTest(unittest.TestCase):
    """前端不许再有「发送前识图」——那是被删掉的那一次调用。"""

    def _read(self, name):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        path = os.path.join(root, "web", "workspace", "js", name)
        with open(path, encoding="utf-8") as f:
            return f.read()

    def test_ref_js_has_no_describe(self):
        ref = self._read("ref.js")
        for dead in ("ensureDescribed", "describeOnly", "describeUrl"):
            self.assertNotIn(dead, ref, "ref.js 里还有前端识图的残留：%s" % dead)

    def test_chat_js_does_not_wait_for_describe(self):
        chat = self._read("chat.js")
        self.assertNotIn("WSRef.ensureDescribed", chat)

    def test_send_falls_back_to_a_default_request(self):
        """只挂图不写字也能发：需求位补一句「照着这张画」。"""
        chat = self._read("chat.js")
        self.assertIn('if (!text) text = "照着这张画"', chat)

    def test_send_button_lights_up_with_image_only(self):
        chat = self._read("chat.js")
        seg = chat[chat.index("function syncSend"):]
        self.assertIn("WSRef.list().length", seg[:400])


if __name__ == "__main__":
    unittest.main()
