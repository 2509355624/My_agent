# -*- coding: utf-8 -*-
"""访客能不能真的生图 / 上传参考图 / 不写聊天存档（2026-11-26）。

用户实测报的三个 bug，各钉一条：

  1. 访客在聊天页发「画一个可莉西装」，蓝色气泡下面挂一行「没登录 · 重画」
     —— 后端 `api_ask` 里 `if not qq: 401 没登录` 把访客当成了没登录。
  2. 点头像进「我的」显示「参数不对」—— `_qq()` 拿不到 qq 就 400。
  3. 重新以访客身份进来，之前的消息还在 —— 其实是不该存（用户改口径：
     「访客模式不需要再给他存聊天记录了」）。

这里只测**身份判定**那一层：不真去排队生图（那要 GPU），用 mock 把
`_web_decide` 换掉，只验「访客有没有被放行到那一步」。
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask

from app.gateways import friend_auth
from app.gateways import visitor


def _headers():
    """一张「5175 转发过来、身份是访客」的请求头。"""
    return {"X-Friend-Token": friend_auth.secret(),
            "X-Friend-Guest": visitor.new_id()}


def _guest_headers():
    return {"X-Friend-Token": friend_auth.secret(),
            "X-Friend-Guest": visitor.new_id()}


class VisitorAskTest(unittest.TestCase):
    """`/api/gen/ask` 对访客必须放行（以前是 401「没登录」）。"""

    def setUp(self):
        visitor.reset()
        self.addCleanup(visitor.reset)
        from app.features.image import routes as img
        from app.features.chat import routes as chat
        self.img = img
        self.chat = chat
        app = Flask(__name__)
        app.register_blueprint(img.bp)
        self.client = app.test_client()

    def _post(self, body, headers, decide):
        with mock.patch.object(self.chat, "_who", return_value=""):
            with mock.patch.object(self.img, "_web_decide", decide):
                return self.client.post("/api/gen/ask", json=body,
                                        headers=headers)

    def test_visitor_ask_is_not_401(self):
        job = {"id": "j1", "skill": "s", "prompt": "p"}

        def fake(text, skill, hist, ctx):
            return dict(job)

        r = self._post({"text": "画一个可莉西装", "skill": "anima-2s"},
                       _guest_headers(),
                       mock.Mock(side_effect=fake))
        self.assertNotEqual(r.status_code, 401, r.get_json())
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json().get("ok"))

    def test_visitor_records_own_history_not_shared(self):
        """访客的历史按身份段分线，不能落进 `web_chat_.jsonl` 公共那份。"""
        app = Flask(__name__)
        with app.test_request_context("/", headers=_guest_headers()):
            hist = self.img._web_history_path("")
        self.assertTrue(hist.endswith(".jsonl"))
        self.assertNotEqual(os.path.basename(hist), "web_chat_.jsonl")
        self.assertIn("guest_", os.path.basename(hist))

    def test_no_identity_at_all_still_401(self):
        r = self._post({"text": "画一个可莉西装", "skill": "anima-2s"},
                       {"X-Friend-Token": friend_auth.secret()},
                       mock.Mock())
        self.assertEqual(r.status_code, 401)


class VisitorUploadTest(unittest.TestCase):
    """参考图上传：访客也得能用（以前只认 qq，一律「没登录」）。"""

    def setUp(self):
        visitor.reset()
        self.addCleanup(visitor.reset)
        from app.features.image import routes as img
        from app.features.chat import routes as chat
        self.img = img
        self.chat = chat
        app = Flask(__name__)
        app.register_blueprint(img.bp)
        self.client = app.test_client()

    def test_visitor_can_upload(self):
        body = {"image": "data:image/png;base64,AAAA"}
        with mock.patch.object(self.chat, "_who", return_value=""):
            r = self.client.post("/api/gen/chat-upload", json=body,
                                 headers=_guest_headers())
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertTrue(r.get_json().get("ref"))

    def test_upload_owner_is_guest_key(self):
        """访客上传的图挂在访客身份段上，不跟别人的那份混在一起。"""
        from app.features.image import routes as img
        body = {"image": "data:image/png;base64,AAAA"}
        with mock.patch.object(self.chat, "_who", return_value=""):
            r = self.client.post("/api/gen/chat-upload", json=body,
                                 headers=_guest_headers())
        ref = r.get_json()["ref"]
        with img._WEB_IMG_LOCK:
            row = img._WEB_IMG_CACHE.get(ref) or {}
        self.assertTrue(row.get("qq", "").startswith("guest_"))


class VisitorArchiveTest(unittest.TestCase):
    """访客不写服务端聊天存档（用户 2026-11-26 改的口径）。"""

    def setUp(self):
        visitor.reset()
        self.addCleanup(visitor.reset)
        from app.features.chat import routes as chat
        app = Flask(__name__)
        app.register_blueprint(chat.bp)
        self.client = app.test_client()
        self.chat = chat

    def test_archive_rejects_visitor(self):
        """带访客头打存档：401，并且明确标 `guest`（不是「没登录」那种）。"""
        for path in ("/api/chat/archive?qq=10001", "/api/chat/archive"):
            r = self.client.get(path, headers=_guest_headers())
            self.assertEqual(r.status_code, 401, path)
            self.assertTrue(r.get_json().get("guest"), path)

    def test_archive_post_rejects_visitor(self):
        r = self.client.post("/api/chat/archive", headers=_guest_headers(),
                             json={"msgs": [{"m": {"k": "user", "t": "hi"}}]})
        self.assertEqual(r.status_code, 401)
        self.assertTrue(r.get_json().get("guest"))

    def test_visitor_path_blocked_at_gateway(self):
        for m in ("GET", "POST", "PATCH", "DELETE"):
            self.assertFalse(visitor.path_allowed("/api/chat/archive", m))

    def test_guest_does_not_see_a_real_slot(self):
        """访客头 + 别人的 qq 参数：不能读到那个人的存档。"""
        r = self.client.get("/api/chat/archive?qq=10001",
                            headers=_guest_headers())
        self.assertEqual(r.status_code, 401)
        self.assertNotIn("msgs", r.get_json())


if __name__ == "__main__":
    unittest.main()
