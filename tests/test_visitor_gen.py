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
import shutil
import sys
import tempfile
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
    """聊天存档：访客有自己的档位（2026-11-26 用户拍板）。

    上一版这里钉的是「带访客头一律 401」——那时的口径是「访客不存服务端
    记录」。用户 2026-11-26 把口径改回来了，理由很具体：工作台不能靠前端
    轮询去写记录，「服务端跑完就写进档位、用户回来才拿得到图」才是对的
    （QQ 那边就是这样）。所以访客档位是 `guest_<身份段>.json`，和 QQ 那份
    同一套读写、同一条 KEEP。

    ⚠️ 只影响**聊天存档**（走 `_owner()`）。`_who()` 一个字没改——角色扮演、
    小说、跑图台、群聊额度那些地方照旧不认访客，这条边界不能松。
    """

    def setUp(self):
        visitor.reset()
        self.addCleanup(visitor.reset)
        from app.features.chat import routes as chat
        self.tmp = tempfile.mkdtemp(prefix="ws_archive_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        p = mock.patch.object(chat, "DIR", self.tmp)
        p.start()
        self.addCleanup(p.stop)
        app = Flask(__name__)
        app.register_blueprint(chat.bp)
        self.client = app.test_client()
        self.chat = chat

    def _guest(self):
        raw = visitor.new_id()
        return raw, {"X-Friend-Token": friend_auth.secret(),
                     "X-Friend-Guest": raw}

    def test_guest_header_does_not_change_identity(self):
        """访客头不影响 `_who()`：还是按 qq 参数/本机直连那套判。"""
        from app.features.chat import routes as chat
        with self.client.application.test_request_context(
                "/api/chat/archive?qq=10001", headers=_guest_headers()):
            self.assertEqual(chat._who(), "10001")

    def test_guest_has_its_own_file(self):
        """带有效访客头就有自己的档位，落在 `guest_<身份段>.json`。"""
        raw, h = self._guest()
        r = self.client.post("/api/chat/archive", headers=h, json={"msgs": [
            {"k": "img", "id": "imgabc_1", "note": "排队中"}]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual((r.get_json() or {}).get("added"), 1)
        gid = visitor.check_id(raw)
        path = os.path.join(self.tmp, "guest_%s.json" % gid)
        self.assertTrue(os.path.isfile(path), "访客档位没落盘: " + path)
        g = self.client.get("/api/chat/archive", headers=h).get_json() or {}
        self.assertEqual([m.get("id") for m in g.get("msgs") or []],
                         ["imgabc_1"])

    def test_two_guests_do_not_see_each_other(self):
        _, h1 = self._guest()
        self.client.post("/api/chat/archive", headers=h1, json={"msgs": [
            {"k": "img", "id": "imgabc_1"}]})
        _, h2 = self._guest()
        g = self.client.get("/api/chat/archive", headers=h2).get_json() or {}
        self.assertEqual(g.get("msgs") or [], [])

    def test_ask_enqueues_with_the_same_owner_as_submit(self):
        """ask 入队的 owner 必须和 submit / status 收口同一口径（2026-11-26）。

        用户实测：访客在聊天页发「画一个纳西妲」，图 21:58:40 正常落盘，卡片
        却写着「服务重启过，这张没了」。原因是 ask 那条路拿 `_who_qq()` 当
        owner（访客是空串），而 `/api/gen/status` 按 `guest_<id>` 收口 →
        任务在状态里查不到 → 前端 15 秒后判死，等真出图时记录已经写成失败。
        """
        from app.features.chat import routes as chat
        from app.features.image import routes as gen_api
        raw, h = self._guest()
        with self.client.application.test_request_context(
                "/api/gen/ask", headers=h):
            want = chat._guest_owner()
            self.assertTrue(want, "访客应该有自己的 owner 键")
            self.assertEqual(gen_api._owner_key(), want,
                             "ask 的 owner 和访客档位口径不一致")
        self.assertIn(visitor.check_id(raw), want)

    def test_forged_or_missing_token_is_rejected(self):
        """签名不对、或者没有内部令牌 → 当没带，照旧 401。"""
        raw = visitor.new_id()
        gid, exp, _sig = raw.split(".")
        forged = "%s.%s.%s" % (gid, exp, "0" * 32)
        r = self.client.get("/api/chat/archive", headers={
            "X-Friend-Token": friend_auth.secret(), "X-Friend-Guest": forged})
        self.assertEqual(r.status_code, 401)
        r = self.client.get("/api/chat/archive",
                            headers={"X-Friend-Guest": visitor.new_id()})
        self.assertEqual(r.status_code, 401)

    def test_write_back_fills_the_result(self):
        """服务端兜底写档：按记录 id 把 file 盖回去（前端不在也不丢图）。"""
        raw, h = self._guest()
        self.client.post("/api/chat/archive", headers=h, json={"msgs": [
            {"k": "img", "id": "imgabc_1", "job": "GP-1",
             "note": "正在跑", "err": False}]})
        key = "guest_" + visitor.check_id(raw)
        hit = self.chat.write_back(key, "imgabc_1", "GP-1",
                                   {"file": "a.png", "note": "", "err": False,
                                    "job": ""})
        self.assertEqual(hit, 1)
        g = self.client.get("/api/chat/archive", headers=h).get_json() or {}
        m = (g.get("msgs") or [{}])[0]
        self.assertEqual(m.get("file"), "a.png")
        self.assertEqual(m.get("job"), "")

    def test_write_back_falls_back_to_job(self):
        """没有记录 id 时按任务号认（「直达」连排那几条就是这种）。"""
        raw, h = self._guest()
        self.client.post("/api/chat/archive", headers=h, json={"msgs": [
            {"k": "img", "id": "imgabc_2", "job": "GP-2", "note": "正在跑"}]})
        key = "guest_" + visitor.check_id(raw)
        self.assertEqual(self.chat.write_back(key, "", "GP-2", {"file": "b.png"}), 1)


if __name__ == "__main__":
    unittest.main()
