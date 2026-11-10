# -*- coding: utf-8 -*-
"""群友漫画台（2026-11-10）：身份 / 配额 / 缩略图 / QQ 直发 / 落盘归属。

钉死几条容易悄悄漂掉的边界：

  1. 站主直连 5174 一个限制都不加；只有「内部令牌 + qq」才算群友。
  2. 修订助手只有 changed=true 且剧本真落地才扣次数；反问 / 失败要退。
  3. 网页只拿缩略图 URL，高清原图不进浏览器；缩略图落服务端磁盘。
  4. 群友任务跳过最终 2x 放大，且 ComfyUI output 按账号 / 作品分流。
  5. 发送状态、手动重发不回退网页下载。
"""

import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask

from app import (comic, comic_api, comic_delivery, comic_quota,
                 comic_render_opts, comic_thumb, friend_auth, main)

SECRET = "unit-test-friend-secret"
QQ = "123456"
OTHER = "999999"


def _png(path, size=(64, 96), color=(200, 30, 40)):
    from PIL import Image
    os.makedirs(os.path.dirname(path), exist_ok=True)
    Image.new("RGB", size, color).save(path)


class _TempMixin:
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="comic_friend_")
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.state_file = os.path.join(self.tmp, "comic_friend.json")
        p = mock.patch.object(comic_quota, "_FILE", self.state_file)
        p.start()
        self.addCleanup(p.stop)
        p2 = mock.patch.object(friend_auth, "secret", return_value=SECRET)
        p2.start()
        self.addCleanup(p2.stop)

    def mk_task(self, name="C20261110-01", owner=QQ, script=True, raw=False,
                final=False, portrait=False):
        d = os.path.join(self.tmp, name)
        os.makedirs(d, exist_ok=True)
        if owner:
            comic_quota.set_owner(d, owner)
        if script:
            with open(os.path.join(d, "script.md"), "w", encoding="utf-8") as f:
                f.write("# 标题：测试\n\n## 01\n画面: 1girl, room\n气泡: 一\n")
        if raw:
            _png(os.path.join(d, "raw", "01_raw.png"))
        if final:
            _png(os.path.join(d, "final", "01_final.png"))
        if portrait:
            _png(os.path.join(d, "portraits", "01.png"))
        return d


class TestFriendIdentity(_TempMixin, unittest.TestCase):
    def test_只认令牌加qq(self):
        app = Flask(__name__)
        with app.test_request_context("/?qq=%s" % QQ,
                                      headers={"X-Friend-Token": SECRET}):
            self.assertEqual(comic_quota.friend_qq(), QQ)
        with app.test_request_context("/?qq=%s" % QQ):
            self.assertEqual(comic_quota.friend_qq(), "")     # 伪造 qq 无效
        with app.test_request_context("/", headers={"X-Friend-Token": SECRET}):
            self.assertEqual(comic_quota.friend_qq(), "")     # 站主直连
        with app.test_request_context("/?qq=abc",
                                      headers={"X-Friend-Token": SECRET}):
            self.assertEqual(comic_quota.friend_qq(), "")

    def test_归属只写一次(self):
        d = self.mk_task()
        self.assertEqual(comic_quota.owner_of(d), QQ)
        comic_quota.set_owner(d, OTHER)                       # 接上一集不许改主人
        self.assertEqual(comic_quota.owner_of(d), QQ)
        self.assertTrue(comic_quota.can_read(d, QQ))
        self.assertFalse(comic_quota.can_read(d, OTHER))
        self.assertTrue(comic_quota.can_read(d, ""))          # 站主放行

    def test_配额扣退与按本幂等(self):
        self.assertEqual(comic_quota.remaining(QQ, "revise"), 3)
        self.assertTrue(comic_quota.consume(QQ, "revise"))
        self.assertEqual(comic_quota.remaining(QQ, "revise"), 2)
        comic_quota.refund(QQ, "revise")
        self.assertEqual(comic_quota.remaining(QQ, "revise"), 3)
        self.assertTrue(comic_quota.consume(QQ, "script"))
        self.assertFalse(comic_quota.consume(QQ, "script"))   # 免费 1 次
        self.assertFalse(comic_quota.charged(QQ, "comic", "C1"))
        comic_quota.mark_charged(QQ, "comic", "C1")
        self.assertTrue(comic_quota.charged(QQ, "comic", "C1"))

    def test_模型白名单与收敛(self):
        comic_quota.admin_update({"models": [
            {"provider": "relay", "model": "v4", "label": "中转站 V4"}]})
        self.assertEqual(comic_quota.resolve_model("relay", "v4"),
                         ("relay", "v4"))
        self.assertEqual(comic_quota.resolve_model("official", "xxx"),
                         ("", ""))                            # 不在白名单退默认
        self.assertEqual(len(comic_quota.admin_view()["models"]), 1)

    def test_按账号改额度(self):
        comic_quota.admin_update({"account": {
            "qq": QQ, "limits": {"script": 5, "max_panels": 30},
            "used": {"script": 1}, "note": "老朋友"}})
        lim = comic_quota.limits(QQ)
        self.assertEqual(lim["script"], 5)
        self.assertEqual(lim["max_panels"], 30)
        self.assertEqual(comic_quota.remaining(QQ, "script"), 4)
        self.assertEqual(comic_quota.limits(OTHER)["script"], 1)  # 不影响别人

    def test_输出目录按账号分三段(self):
        d = self.mk_task(name="C20261110-01_加了料的饮料")
        out = comic_quota.output_base(d)
        self.assertEqual(out, "群友漫画台/%s/C20261110-01_加了料的饮料" % QQ)
        self.assertEqual(len(out.split("/")), 3)   # 第 4 段留给渠道，_MAX_DEPTH=4
        self.assertEqual(comic_quota.output_base(
            self.mk_task(name="B1", owner=None)), "")
        self.assertEqual(comic._out_dir_for(mock.Mock(dir=d, task_id="X")), out)


class TestThumb(_TempMixin, unittest.TestCase):
    def test_路径解析与源图映射(self):
        self.assertEqual(comic_thumb.parse_rel("thumb/03_final_thumb.webp"),
                         ("03", "final"))
        self.assertEqual(comic_thumb.parse_rel("thumb/1_raw_thumb.webp"),
                         ("01", "raw"))
        self.assertIsNone(comic_thumb.parse_rel("raw/01_raw.png"))
        self.assertIsNone(comic_thumb.parse_rel("thumb/03_evil_thumb.webp"))
        self.assertEqual(comic_thumb.src_rel("7", "portrait"),
                         "portraits/07.png")

    def test_生成webp且缓存复用(self):
        d = self.mk_task(raw=True)
        got = comic_thumb.ensure(d, "01", "raw")
        self.assertTrue(got and os.path.isfile(got))
        self.assertTrue(got.endswith("01_raw_thumb.webp"))
        from PIL import Image
        with Image.open(got) as im:
            self.assertEqual(im.format, "WEBP")
            self.assertLessEqual(max(im.size), comic_thumb.THUMB_EDGE)
        mtime = os.stat(got).st_mtime_ns
        self.assertEqual(comic_thumb.ensure(d, "01", "raw"), got)
        self.assertEqual(os.stat(got).st_mtime_ns, mtime)   # 缓存命中不重写
        _png(os.path.join(d, "raw", "01_raw.png"), color=(10, 90, 200))
        os.utime(os.path.join(d, "raw", "01_raw.png"),
                 ns=(time.time_ns() + 2_000_000_000,
                     time.time_ns() + 2_000_000_000))
        comic_thumb.ensure(d, "01", "raw")                   # 源图变新要重画
        self.assertGreater(os.stat(got).st_mtime_ns, mtime)
        self.assertIsNone(comic_thumb.ensure(d, "09", "final"))  # 源不存在

    def test_预热整本(self):
        d = self.mk_task(raw=True, final=True, portrait=True)
        n = comic_thumb.ensure_all(d, panels=[mock.Mock(no="01")],
                                   portraits=[{"no": "01"}])
        self.assertEqual(n, 3)


class TestDelivery(_TempMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.out = os.path.join(self.tmp, "out")
        p = mock.patch.object(comic_delivery, "COMFYUI_OUTPUT_DIR", self.out)
        p.start()
        self.addCleanup(p.stop)

    def test_只收当前生效的图并分包(self):
        d = self.mk_task(raw=True, final=True, portrait=True)
        packs = comic_delivery._collect(d)
        self.assertEqual(set(packs), {"底图包", "成品包", "写真包"})
        self.assertEqual(len(packs["底图包"]), 1)
        before = comic_delivery._fingerprint(packs["底图包"])
        self.assertNotEqual(before, "")
        _png(os.path.join(d, "raw", "01_raw.png"), color=(1, 2, 3))
        os.utime(os.path.join(d, "raw", "01_raw.png"),
                 ns=(time.time_ns() + 2_000_000_000,
                     time.time_ns() + 2_000_000_000))
        self.assertNotEqual(before, comic_delivery._fingerprint(
            comic_delivery._collect(d)["底图包"]))

    def test_自动发送按指纹去重且失败写状态(self):
        from app import qq_api
        d = self.mk_task(raw=True, final=True)
        with mock.patch.object(qq_api, "send_private_file") as send, \
                mock.patch.object(qq_api, "send_private") as hint:
            first = comic_delivery.deliver(d, QQ, auto=True)
            self.assertEqual(set(first), {"底图包", "成品包"})
            self.assertEqual(send.call_count, 2)
            self.assertEqual(hint.call_count, 1)
            self.assertEqual(comic_delivery.deliver(d, QQ, auto=True), [])
            self.assertEqual(send.call_count, 2)             # 没变化不重复发
            comic_delivery.deliver(d, QQ, force=True)
            self.assertEqual(send.call_count, 4)             # 手动强制重发
        v = comic_delivery.view(d)
        self.assertEqual(v["state"], "sent")
        self.assertEqual(len(v["packages"]), 2)
        self.assertIn("QQ 发给你", v["hint"])
        self.assertTrue(os.path.isfile(os.path.join(
            comic_delivery.delivery_dir(d, QQ),
            os.path.basename(d) + "_底图包.zip")))            # zip 不自动删

        with mock.patch.object(qq_api, "send_private_file",
                               side_effect=RuntimeError("协议端离线")):
            comic_delivery.deliver(d, QQ, force=True)
        self.assertEqual(comic_delivery.view(d)["state"], "failed")
        self.assertEqual(comic_quota.admin_view()["deliveries"][-1]["state"],
                         "failed")

    def test_手动重发冷却(self):
        ok, wait = comic_delivery.manual_ok(self.tmp)
        self.assertTrue(ok)
        ok2, wait2 = comic_delivery.manual_ok(self.tmp)
        self.assertFalse(ok2)
        self.assertGreater(wait2, 0)


class TestFriendApi(_TempMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.c = main.app.test_client()
        p = mock.patch.object(comic_api, "_COMICS_ROOT", self.tmp)
        p.start()
        self.addCleanup(p.stop)
        with comic_api._CHAT_LOCK:
            comic_api._CHAT_JOBS.clear()

    def _get(self, path, qq=None, **kw):
        params = dict(kw.pop("query", {}))
        headers = {}
        if qq:
            params["qq"] = qq
            headers["X-Friend-Token"] = SECRET
        return self.c.get(path, query_string=params, headers=headers, **kw)

    def _post(self, path, body, qq=None):
        params, headers = {}, {}
        if qq:
            params["qq"] = qq
            headers["X-Friend-Token"] = SECRET
        return self.c.post(path, json=body, query_string=params, headers=headers)

    def test_启动配置只给白名单模型(self):
        comic_quota.admin_update({"models": [
            {"provider": "relay", "model": "v4", "label": "中转站 V4"}]})
        d = self._get("/api/comic/friend_config", QQ).get_json()
        self.assertTrue(d["ok"])
        self.assertTrue(d["friend"])
        self.assertEqual(d["quota"]["left"]["script"], 1)
        self.assertEqual(d["models"][0]["label"], "中转站 V4")
        self.assertTrue(any(p["key"] == "16:9" for p in d["ratio_presets"]))

    def test_任务列表按owner隔离(self):
        self.mk_task(name="A1", owner=QQ)
        self.mk_task(name="B1", owner=OTHER)
        self.mk_task(name="C1", owner=None)
        mine = self._get("/api/comic/tasks", QQ).get_json()["tasks"]
        self.assertEqual([x["task"] for x in mine], ["A1"])
        all_ = self._get("/api/comic/tasks").get_json()["tasks"]
        self.assertEqual(set(x["task"] for x in all_), {"A1", "B1", "C1"})

    def test_越权取图被owner挡住(self):
        self.mk_task(name="B1", owner=OTHER, raw=True)
        r = self._get("/api/comic/img", QQ,
                      query={"task": "B1", "f": "raw/01_raw.png"})
        self.assertEqual(r.status_code, 404)

    def test_群友拿缩略图URL站主拿原图(self):
        d = self.mk_task(name="A1", raw=True, final=True)
        f = self._get("/api/comic/status", QQ,
                      query={"task": "A1"}).get_json()
        self.assertIn("thumb/01_raw_thumb.webp", f["panels"][0]["scene_url"])
        self.assertIn("thumb/01_final_thumb.webp", f["panels"][0]["final_url"])
        self.assertIsNotNone(f["quota"])
        self.assertIn("queue", f)
        s = self._get("/api/comic/status", query={"task": "A1"}).get_json()
        self.assertIn("raw/01_raw.png", s["panels"][0]["scene_url"])
        self.assertIsNone(s["quota"])
        self.assertIsNone(s["delivery"])

    def test_缩略图端点返回webp(self):
        self.mk_task(name="A1", raw=True)
        r = self._get("/api/comic/img", QQ,
                      query={"task": "A1", "f": "thumb/01_raw_thumb.webp"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.mimetype.startswith("image/webp"))
        self.assertGreater(len(r.data), 0)
        bad = self._get("/api/comic/img", QQ,
                        query={"task": "A1", "f": "thumb/../../etc"})
        self.assertEqual(bad.status_code, 404)

    def test_渲染参数强制task级且跳过放大(self):
        d = self.mk_task(name="A1")
        captured = {}
        with mock.patch.object(comic_api, "_render_resources", return_value={}), \
                mock.patch.object(comic_render_opts, "load",
                                  return_value={"width": 1024, "height": 1536,
                                                "upscale": {}}), \
                mock.patch.object(comic_render_opts, "save",
                                  side_effect=lambda *a: captured.update(
                                      {"opts": a[1]})):
            r = self._post("/api/comic/render_opts", {
                "task": "A1", "scope": "global",
                "opts": {"width": 1216, "height": 832}}, QQ)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["scope"], "task")
        self.assertTrue(captured["opts"]["upscale"]["final_skip"])
        self.assertEqual(captured["opts"]["width"], 1216)
        reset = self._post("/api/comic/render_opts",
                           {"task": "A1", "reset": True}, QQ)
        self.assertEqual(reset.status_code, 403)

    def test_修订助手不落实不扣次数(self):
        from app import comic_agent
        d = self.mk_task(name="A1")
        script = os.path.join(d, "script.md")

        def _run(changed):
            fake = {"reply": "好的" if changed else "你是想改哪格？",
                    "changed": changed, "diff": [], "warnings": [],
                    "script": "# 标题：改过\n\n## 01\n画面: 2girls, room\n气泡: 一\n"
                    if changed else ""}
            with mock.patch.object(comic_agent, "chat_revise",
                                   return_value=fake):
                r = self._post("/api/comic/chat_revise",
                               {"task": "A1", "message": "改一下"}, QQ)
                self.assertEqual(r.status_code, 200)
                job = r.get_json()["job"]
                deadline = time.time() + 5
                while time.time() < deadline:
                    st = self._get("/api/comic/chat_status", QQ,
                                   query={"job": job}).get_json()
                    if st["state"] != "running":
                        break
                    time.sleep(0.02)
                return st

        st = _run(False)
        self.assertFalse(st["changed"])
        self.assertEqual(comic_quota.usage(QQ)["revise"], 0)
        st2 = _run(True)
        self.assertTrue(st2["changed"])
        self.assertTrue(st2["charged"])
        self.assertEqual(comic_quota.usage(QQ)["revise"], 1)
        with open(script, encoding="utf-8") as f:
            self.assertIn("改过", f.read())

    def test_job轮询按owner隔离(self):
        self.mk_task(name="A1", owner=QQ)
        with comic_api._CHAT_LOCK:
            comic_api._CHAT_JOBS["j1"] = {
                "state": "done", "task": "A1", "reply": "x", "changed": False,
                "diff": [], "warnings": [], "error": None, "at": ""}
        self.assertEqual(self._get("/api/comic/chat_status", QQ,
                                   query={"job": "j1"}).status_code, 200)
        self.assertEqual(self._get("/api/comic/chat_status", OTHER,
                                   query={"job": "j1"}).status_code, 404)
        self.assertEqual(self._get("/api/comic/chat_status",
                                   query={"job": "j1"}).status_code, 200)

    def test_管理接口群友一律拒绝(self):
        self.assertEqual(self._get("/api/comic/admin", QQ).status_code, 403)
        self.assertEqual(self._post("/api/comic/admin", {}, QQ).status_code, 403)
        d = self._get("/api/comic/admin").get_json()
        self.assertTrue(d["ok"])
        self.assertIn("accounts", d)

    def test_手动重发不接受非法QQ且失败不回退网关(self):
        self.mk_task(name="A1", owner=None, raw=True)
        r = self._post("/api/comic/send_qq", {"task": "A1", "qq": "abc"})
        self.assertEqual(r.status_code, 400)
        with mock.patch.object(comic_delivery, "deliver") as deliver, \
                mock.patch.object(comic_delivery, "manual_ok",
                                  return_value=(True, 0)):
            r = self._post("/api/comic/send_qq", {"task": "A1", "qq": QQ})
        self.assertEqual(r.status_code, 200)
        self.assertIn("QQ", r.get_json()["hint"])
        time.sleep(0.15)                       # 后台线程把 deliver 调起来
        self.assertTrue(deliver.called)


class _FakeUpstream:
    """够 `_forward` 用的最小上游响应。"""

    def __init__(self, body=b"WEBP-BYTES", ctype="image/webp", status=200):
        self.content = body
        self.status_code = status
        self.headers = {"Content-Type": ctype,
                        "Content-Length": str(len(body))}
        self.cookies = {}
        self.closed = False

    def iter_content(self, n):
        for i in range(0, len(self.content), n):
            yield self.content[i:i + n]

    def close(self):
        self.closed = True


class TestFriendGateway(_TempMixin, unittest.TestCase):
    """5175 入口：登录闸、反代前缀、缩略图流式。"""

    def setUp(self):
        super().setUp()
        import friend_app
        self.gw = friend_app
        self.c = friend_app.app.test_client()

    def test_漫画页必须登录(self):
        with mock.patch.object(self.gw, "_logged_in", return_value=False):
            r = self.c.get("/comic")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("先说清楚怎么用", r.get_data(as_text=True))
        with mock.patch.object(self.gw, "_logged_in", return_value=True):
            r = self.c.get("/comic")
        self.assertIn("先说清楚怎么用", r.get_data(as_text=True))
        self.assertIn("缩略图", r.get_data(as_text=True))

    def test_接口未登录401登录后注入qq(self):
        r = self.c.get("/api/comic/friend_config")
        self.assertEqual(r.status_code, 401)
        self.c.set_cookie("fr_sess", friend_auth.issue_cookie(QQ))
        sess = mock.MagicMock()
        sess.get.return_value = _FakeUpstream(b'{"ok":true}',
                                              ctype="application/json")
        with mock.patch.object(self.gw, "_new_session", return_value=sess):
            r = self.c.get("/api/comic/friend_config")
        self.assertEqual(r.status_code, 200)
        params = sess.get.call_args.kwargs["params"]
        self.assertEqual(params["qq"], QQ)                 # 服务端写死，不信客户端
        hdrs = sess.get.call_args.kwargs["headers"]
        self.assertEqual(hdrs["X-Friend-Token"], SECRET)

    def test_缩略图走流式且不被客户端改qq(self):
        self.c.set_cookie("fr_sess", friend_auth.issue_cookie(QQ))
        sess = mock.MagicMock()
        sess.get.return_value = _FakeUpstream()
        with mock.patch.object(self.gw, "_new_session", return_value=sess):
            r = self.c.get("/api/comic/img?task=A1&f=thumb/01_raw_thumb.webp"
                           "&qq=999999")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data, b"WEBP-BYTES")
        self.assertEqual(r.content_type, "image/webp")
        self.assertEqual(sess.get.call_args.kwargs["params"]["qq"], QQ)
        self.assertTrue(sess.get.call_args.kwargs["stream"])


if __name__ == "__main__":
    unittest.main()
