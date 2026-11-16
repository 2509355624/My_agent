# -*- coding: utf-8 -*-
"""隧道地址文件可配：群友页与管理页各写各的，互不覆盖。"""

import os
import tempfile
import unittest
from unittest import mock

import friend_tunnel


class FriendTunnelTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tun_")

    def test_default_url_file_is_friend_url(self):
        with mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch.object(friend_tunnel, "URL_FILE", "friend_url.txt"):
            self.assertTrue(friend_tunnel._url_path().endswith(
                os.path.join("state", "friend_url.txt")))

    def test_custom_url_file_writes_to_own_file(self):
        # 管理页隧道设 admin_url.txt，写出来的路径必须是它自己的文件。
        with mock.patch.object(friend_tunnel, "ROOT", self.tmp), \
             mock.patch.object(friend_tunnel, "URL_FILE", "admin_url.txt"):
            p = friend_tunnel._write_url("https://abc123.trycloudflare.com")
            self.assertTrue(p.endswith(os.path.join("state", "admin_url.txt")))
            with open(p, encoding="utf-8") as f:
                self.assertEqual(f.read().strip(),
                                 "https://abc123.trycloudflare.com")
            # friend_url.txt 不该被碰
            self.assertFalse(os.path.exists(
                os.path.join(self.tmp, "state", "friend_url.txt")))

    def test_url_file_name_cannot_escape_state_dir(self):
        # 环境变量传 ../evil.txt 也不许写出 state/ 目录。
        with mock.patch.object(friend_tunnel, "ROOT", self.tmp), \
             mock.patch.object(friend_tunnel, "URL_FILE", "../../evil.txt"):
            p = friend_tunnel._url_path()
            self.assertEqual(os.path.dirname(p), os.path.join(self.tmp, "state"))
            self.assertEqual(os.path.basename(p), "evil.txt")

    def test_pid_file_is_per_tunnel(self):
        # 群友页与管理页各有自己的 PID 文件，启动 bat 才能各杀各的。
        with mock.patch.object(friend_tunnel, "ROOT", self.tmp), \
             mock.patch.object(friend_tunnel, "URL_FILE", "friend_url.txt"):
            friend_tunnel._write_pid(1234)
            self.assertTrue(os.path.exists(
                os.path.join(self.tmp, "state", "friend_url.pid")))
        with mock.patch.object(friend_tunnel, "ROOT", self.tmp), \
             mock.patch.object(friend_tunnel, "URL_FILE", "admin_url.txt"):
            friend_tunnel._write_pid(5678)
            with open(os.path.join(self.tmp, "state", "admin_url.pid")) as f:
                self.assertEqual(f.read().strip(), "5678")
            # 群友的 PID 文件没被管理页覆盖
            with open(os.path.join(self.tmp, "state", "friend_url.pid")) as f:
                self.assertEqual(f.read().strip(), "1234")

    def test_clear_only_own_url(self):
        with mock.patch.object(friend_tunnel, "ROOT", self.tmp), \
             mock.patch.object(friend_tunnel, "URL_FILE", "admin_url.txt"):
            p = friend_tunnel._write_url("https://mine.trycloudflare.com")
            # 文件里是别人的地址，_clear_url 不该删
            with open(p, "w", encoding="utf-8") as f:
                f.write("https://other.trycloudflare.com\n")
            friend_tunnel._clear_url("https://mine.trycloudflare.com")
            self.assertTrue(os.path.exists(p))
            # 是自己的地址就删
            friend_tunnel._clear_url("https://other.trycloudflare.com")
            self.assertFalse(os.path.exists(p))


if __name__ == "__main__":
    unittest.main()
