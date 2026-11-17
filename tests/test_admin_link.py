# -*- coding: utf-8 -*-
"""站主私聊专属的 /管理页 指令守卫。

两条边界最要紧：
1. 只有 `config.GALLERY_OWNER_QQ` 配的那个 QQ 号能拿到链接（且只认私聊）；
2. 非站主 / 群聊一律静默不理——管理后台地址发到群里就是所有人可见。
"""

import os
import tempfile
import unittest
from unittest import mock

from app import qq_bot


class AdminSiteUrlTest(unittest.TestCase):
    """admin_site.url() 照 friend_site.url() 的口径：读 + 校验，不猜不兜底。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "admin_url.txt")
        patcher = mock.patch("app.features.admin.site.state_path", return_value=self.path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _write(self, text):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(text)

    def test_reads_valid_https_url(self):
        from app.features.admin import site as admin_site
        self._write("https://admin.trycloudflare.com/abc\n")
        self.assertEqual(admin_site.url(),
                         "https://admin.trycloudflare.com/abc")

    def test_strips_trailing_slash(self):
        from app.features.admin import site as admin_site
        self._write("https://admin.trycloudflare.com/")
        self.assertEqual(admin_site.url(), "https://admin.trycloudflare.com")

    def test_missing_file_is_empty(self):
        from app.features.admin import site as admin_site
        self.assertEqual(admin_site.url(), "")

    def test_empty_file_is_empty(self):
        from app.features.admin import site as admin_site
        self._write("  \n")
        self.assertEqual(admin_site.url(), "")

    def test_non_https_is_rejected(self):
        from app.features.admin import site as admin_site
        for bad in ("http://admin.trycloudflare.com",
                    "file:///C:/secret", "127.0.0.1:5176"):
            self._write(bad)
            self.assertEqual(admin_site.url(), "", bad)


class AdminLinkCommandTest(unittest.TestCase):
    def setUp(self):
        self.send = mock.Mock()
        p1 = mock.patch.object(qq_bot.qq_api, "send_private", self.send)
        p2 = mock.patch("app.config.GALLERY_OWNER_QQ", "2509355624")
        p3 = mock.patch("app.features.admin.site.url",
                        return_value="https://admin.trycloudflare.com")
        for p in (p1, p2, p3):
            p.start()
            self.addCleanup(p.stop)
        self.ev = {"message_id": 9001}

    def test_owner_private_gets_link(self):
        handled = qq_bot._maybe_admin_link(
            self.ev, "/管理页", "2509355624", "private", "2509355624")
        self.assertTrue(handled)
        self.send.assert_called_once()
        args, kwargs = self.send.call_args
        self.assertEqual(args[0], "2509355624")
        self.assertIn("https://admin.trycloudflare.com", args[1])
        self.assertEqual(kwargs.get("reply_to"), "9001")

    def test_fullwidth_slash_also_matches(self):
        qq_bot._maybe_admin_link(
            self.ev, "／管理页", "2509355624", "private", "2509355624")
        self.send.assert_called_once()

    def test_non_owner_is_silent(self):
        handled = qq_bot._maybe_admin_link(
            self.ev, "/管理页", "999999", "private", "999999")
        self.assertTrue(handled)      # 吞掉，不落到模型
        self.send.assert_not_called()

    def test_group_is_silent_even_for_owner(self):
        handled = qq_bot._maybe_admin_link(
            self.ev, "/管理页", "2509355624", "group", "123456")
        self.assertTrue(handled)
        self.send.assert_not_called()

    def test_other_text_passes_through(self):
        self.assertFalse(qq_bot._maybe_admin_link(
            self.ev, "你好", "2509355624", "private", "2509355624"))
        self.send.assert_not_called()

    def test_no_url_tells_owner_to_start_tunnel(self):
        with mock.patch("app.features.admin.site.url", return_value=""):
            qq_bot._maybe_admin_link(
                self.ev, "/管理页", "2509355624", "private", "2509355624")
        self.send.assert_called_once()
        self.assertIn("隧道", self.send.call_args[0][1])

    def test_empty_owner_config_closes_feature(self):
        with mock.patch("app.config.GALLERY_OWNER_QQ", ""):
            handled = qq_bot._maybe_admin_link(
                self.ev, "/管理页", "2509355624", "private", "2509355624")
        self.assertTrue(handled)
        self.send.assert_not_called()

    def test_dispatch_calls_the_guard(self):
        import inspect
        src = inspect.getsource(qq_bot.QQBot._dispatch)
        self.assertIn("_maybe_admin_link(ev, text, user_id, target, target_id)",
                      src)
        # 必须拦在 _should_reply 之前，否则会被白名单闸 / 模型队列吞掉
        self.assertLess(src.index("_maybe_admin_link("),
                        src.index("_should_reply("))


if __name__ == "__main__":
    unittest.main()
