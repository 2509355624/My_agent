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

    def test_包名用作品名加无气泡有气泡(self):
        d = self.mk_task(name="C20261110-01_学姐的饮料", raw=True, final=True)
        self.assertEqual(comic_delivery._work_name(d), "学姐的饮料")
        self.assertEqual(comic_delivery.pack_name(d, "底图包"),
                         "学姐的饮料 无气泡")
        self.assertEqual(comic_delivery.pack_name(d, "成品包"),
                         "学姐的饮料 有气泡")
        self.assertEqual(comic_delivery.pack_name(d, "写真包"),
                         "学姐的饮料 写真")
        self.assertEqual(comic_delivery.preview(d),
                         {"学姐的饮料 无气泡": 1, "学姐的饮料 有气泡": 1})
        # 老状态文件没有 name，view() 也要按当前命名补上，别露出「底图包」
        comic_delivery._save_state(d, {"packages": {
            "底图包": {"state": "sent", "files": 1}}})
        self.assertEqual(comic_delivery.view(d)["packages"][0]["name"],
                         "学姐的饮料 无气泡")
        # 写真任务没有剧本标题，用需求快照第一行，别退化成纯编号
        p = self.mk_task(name="C20261110-08", script=False)
        with open(os.path.join(p, "portrait.json"), "w",
                  encoding="utf-8") as f:
            json.dump({"brief": "花田里回眸、天台看夕阳"}, f,
                      ensure_ascii=False)
        self.assertEqual(comic_delivery._work_name(p), "花田里回眸、天台看夕阳")
        self.assertEqual(comic_delivery.pack_name(p, "写真包"),
                         "花田里回眸、天台看夕阳 写真")
        for k in ("跑完通知", "检查后再发送", "正常排队"):
            self.assertIn(k, comic_delivery.RULE)

    def test_自动发送按指纹去重且失败写状态(self):
        from app import qq_api
        d = self.mk_task(raw=True, final=True)
        with mock.patch.object(qq_api, "send_private_file") as send, \
                mock.patch.object(qq_api, "send_private") as hint:
            first = comic_delivery.deliver(d, QQ, auto=True)
            self.assertEqual(set(first), {"底图包", "成品包"})   # 内部 key 不变
            self.assertEqual(send.call_count, 2)
            self.assertEqual(hint.call_count, 1)
            # QQ 文本只报包名，不写「正在打包 / 正在发送」那类长句
            text = hint.call_args[0][1]
            self.assertIn("无气泡 已发送", text)
            self.assertIn("有气泡 已发送", text)
            self.assertNotIn("正在打包", text)
            self.assertNotIn("不用守着网页", text)
            self.assertEqual(comic_delivery.deliver(d, QQ, auto=True), [])
            self.assertEqual(send.call_count, 2)             # 没变化不重复发
            comic_delivery.deliver(d, QQ, force=True)
            self.assertEqual(send.call_count, 4)             # 手动强制重发
        v = comic_delivery.view(d)
        self.assertEqual(v["state"], "sent")
        self.assertEqual(len(v["packages"]), 2)
        self.assertEqual(v["hint"], comic_delivery.RULE)
        self.assertEqual({p["name"] for p in v["packages"]},
                         {comic_delivery.pack_name(d, k)
                          for k in ("底图包", "成品包")})
        self.assertTrue(os.path.isfile(os.path.join(
            comic_delivery.delivery_dir(d, QQ),
            comic_delivery.pack_name(d, "底图包") + ".zip")))   # zip 不自动删

        with mock.patch.object(qq_api, "send_private_file",
                               side_effect=RuntimeError("协议端离线")):
            comic_delivery.deliver(d, QQ, force=True)
        self.assertEqual(comic_delivery.view(d)["state"], "failed")
        self.assertEqual(comic_quota.admin_view()["deliveries"][-1]["state"],
                         "failed")

    def test_完成通知只发文字不发zip且同一本只通知一次(self):
        from app import qq_api
        d = self.mk_task(name="C20261110-09", raw=True)
        with mock.patch.object(qq_api, "send_private") as text, \
                mock.patch.object(qq_api, "send_private_file") as files:
            self.assertTrue(comic_delivery.notify_ready(d, QQ))
            self.assertFalse(comic_delivery.notify_ready(d, QQ))
            files.assert_not_called()                  # 关键：一个 zip 都没发
        self.assertEqual(text.call_count, 1)           # 重跑不重复打扰
        msg = text.call_args[0][1]
        self.assertIn("1 张底图已完成", msg)
        self.assertIn("换种子重跑", msg)
        self.assertIn("一键加气泡", msg)
        v = comic_delivery.view(d)
        self.assertEqual(v["state"], "waiting_check")
        self.assertEqual(v["ready_kind"], "comic")
        self.assertEqual(v["ready_count"], 1)
        self.assertEqual(v["packages"], [])
        # 写真没有加气泡那一步，通知里要指向「发送 zip」
        p = self.mk_task(name="C20261110-10", script=False, portrait=True)
        with mock.patch.object(qq_api, "send_private") as t2:
            comic_delivery.notify_ready(p, QQ, "portrait")
        v2 = comic_delivery.view(p)
        self.assertEqual(v2["ready_kind"], "portrait")
        self.assertIn("一键发送 zip", t2.call_args[0][1])

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

    def test_手动发送立刻回正在打包并说清会发什么(self):
        name = "C20261110-07_学姐的饮料"
        d = self.mk_task(name=name, raw=True, final=True)
        # 起线程会被 mock 掉：测试不碰真实 QQ 协议端。
        with mock.patch.object(comic_api.threading, "Thread") as th:
            r = self._post("/api/comic/send_qq", {"task": name}, QQ)
        self.assertEqual(r.status_code, 200)
        j = r.get_json()
        self.assertEqual(j["state"], "packing")
        self.assertEqual(j["packages"],
                         {"学姐的饮料 无气泡": 1, "学姐的饮料 有气泡": 1})
        self.assertIn("学姐的饮料 无气泡 1 张", j["hint"])
        self.assertIn("检查后再发送", j["hint"])
        self.assertIn("正常排队", j["hint"])
        self.assertEqual(comic_delivery.view(d)["state"], "packing")
        self.assertEqual(comic_delivery.view(d)["error"], "")
        th.assert_called_once()

    def test_手动发送没有图时明确拒绝(self):
        self.mk_task(name="A1")
        r = self._post("/api/comic/send_qq", {"task": "A1"}, QQ)
        self.assertEqual(r.status_code, 409)
        self.assertIn("还没有任何图", r.get_json()["error"])

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
        self.assertIn("检查后再发送", r.get_json()["hint"])
        self.assertIn("正常排队", r.get_json()["hint"])
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
        self.assertNotIn('id="friendNote"', r.get_data(as_text=True))
        with mock.patch.object(self.gw, "_logged_in", return_value=True):
            r = self.c.get("/comic")
        self.assertIn('id="friendNote"', r.get_data(as_text=True))
        self.assertIn("缩略图", r.get_data(as_text=True))
        self.assertIn("跑写真", r.get_data(as_text=True))

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


class TestComicAdminPage(_TempMixin, unittest.TestCase):
    """独立管理页：加次数是增量、按账号看用量、页面与入口接线。"""

    def _html(self, name):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "web", name), encoding="utf-8") as f:
            return f.read()

    def test_加次数是增量且自动建号(self):
        self.assertEqual(comic_quota.admin_view()["accounts"], [])
        comic_quota.admin_update({"add": {"qq": QQ, "kind": "script", "n": 1}})
        comic_quota.admin_update({"add": {"qq": QQ, "kind": "revise", "n": 3}})
        rows = comic_quota.admin_view()["accounts"]
        self.assertEqual(len(rows), 1)
        a = rows[0]
        self.assertEqual(a["qq"], QQ)
        self.assertEqual(a["limits"]["script"], comic_quota.DEFAULTS["script"] + 1)
        self.assertEqual(a["limits"]["revise"], comic_quota.DEFAULTS["revise"] + 3)
        self.assertEqual(a["left"]["script"], a["limits"]["script"])
        self.assertEqual(a["last_at"], "")     # 站主加次数不算「他用过」
        self.assertEqual(a["works"], 0)

    def test_只冻结被加的那一项其它仍跟随默认(self):
        comic_quota.admin_update({"add": {"qq": QQ, "kind": "script", "n": 1}})
        comic_quota.admin_update({"defaults": {"comic": 5, "script": 9}})
        a = comic_quota.admin_view()["accounts"][0]
        self.assertEqual(a["limits"]["comic"], 5)
        self.assertEqual(a["limits"]["script"], comic_quota.DEFAULTS["script"] + 1)

    def test_最近使用与作品数来自真实使用(self):
        comic_quota.consume(QQ, "script")
        comic_quota.mark_charged(QQ, "comic", "T1")
        comic_quota.mark_charged(QQ, "comic", "T1")   # 同一本重跑不重复记
        comic_quota.mark_charged(QQ, "portrait", "T2")
        a = comic_quota.admin_view()["accounts"][0]
        self.assertTrue(a["last_at"])
        self.assertEqual(a["works"], 2)
        self.assertEqual(a["used"]["script"], 1)

    def test_加次数走接口且群友不能改(self):
        c = main.app.test_client()
        r = c.post("/api/comic/admin?qq=%s" % QQ,
                   json={"add": {"qq": QQ, "kind": "script", "n": 1}},
                   headers={"X-Friend-Token": SECRET})
        self.assertEqual(r.status_code, 403)
        r = c.post("/api/comic/admin",
                   json={"add": {"qq": QQ, "kind": "script", "n": 1}})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(comic_quota.admin_view()["accounts"][0]
                         ["limits"]["script"], comic_quota.DEFAULTS["script"] + 1)

    def test_一次加100不被截断且超过上限被压到顶(self):
        # 站主给赞助用户一次充 100 次，不该被旧的 min(99) 卡成 99。
        comic_quota.admin_update({"add": {"qq": QQ, "kind": "comic", "n": 100}})
        a = comic_quota.admin_view()["accounts"][0]
        self.assertEqual(a["limits"]["comic"], comic_quota.DEFAULTS["comic"] + 100)
        # 防手抖顶仍在：填一个天量，服务端压到上限，不无限涨。
        comic_quota.admin_update({"add": {"qq": QQ, "kind": "comic",
                                          "n": comic_quota.MAX_ADD_PER_CLICK + 9999}})
        a = comic_quota.admin_view()["accounts"][0]
        self.assertEqual(a["limits"]["comic"],
                         comic_quota.DEFAULTS["comic"] + 100
                         + comic_quota.MAX_ADD_PER_CLICK)

    def test_管理页有手动输入次数入口(self):
        page = self._html("comic_admin.html")
        # 一键填 N 的入口：数量框 + 类型下拉 + 添加按钮，账号卡片与新号区各一套
        self.assertIn("act-custom", page)
        self.assertIn("ca-kind", page)
        self.assertIn('id="newNum"', page)
        self.assertIn('id="newCustom"', page)
        self.assertIn("function submitCustom", page)
        self.assertIn("MAX_ADD", page)
        self.assertIn("kindOptions", page)
        # 成功提示以后端返回为准，不再谎报「加了 n」
        self.assertIn("现在共 ' + total", page)

    def test_独立页路由可达(self):
        r = main.app.test_client().get("/comic_admin")
        self.assertEqual(r.status_code, 200)
        self.assertIn("漫画台管理", r.get_data(as_text=True))

    def test_管理页独立且白名单手填已删(self):
        page = self._html("comic_admin.html")
        self.assertIn("谁用了漫画台", page)
        self.assertIn("+1 剧本", page)
        self.assertNotIn("provider|model", page)
        self.assertIn("每本格数上限", page)
        self.assertNotIn("label: '每本格数'", page)
        adm = self._html("admin.html")
        self.assertNotIn("cfModels", adm)
        self.assertNotIn("cfAccounts", adm)
        self.assertIn('href="/comic_admin"', adm)
        # 搬走卡片时别把「今日用量 / QQ 账单」的初始化一起删掉
        self.assertIn("loadUsage().catch", adm)
        self.assertIn("loadBill().catch", adm)
        self.assertIn('href="/comic_admin"', self._html("comic_studio.html"))

    def test_群友页设定拆三行且没有格数输入(self):
        cli = self._html("comic_studio_friend.html")
        # 格数控件还在（母本按 id 读它），但已经不是能粘贴 30 的输入框了
        self.assertIn('id="panels" type="hidden"', cli)
        self.assertNotIn('id="panels" type="number"', cli)
        for i in ("bfView", "bfHero", "bfPlot"):
            self.assertIn('id="%s"' % i, cli)
        self.assertIn("'视角: ' + v", cli)
        self.assertIn("max_panels", cli)          # 格数只从站主额度取
        self.assertIn("max_shots", cli)           # 写真张数同理
        self.assertNotIn("一句话设定", cli)


class TestFriendPageFromMaster(unittest.TestCase):
    """群友版必须真的是「站主成品页 + 补丁」，不许再另写一份精简页。

    2026-11-10 实测反馈：另写的 717 行精简页把台词、标签、气泡、完整提示词、
    改画面词内联展开、红色补正、写真换视图全丢了，反馈还全堆到页面顶端，
    「确认开跑」也不置灰。这几条钉死，防止哪天又有人「顺手重写一遍」。
    """
    PAGE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "web", "comic_studio_friend.html")

    def html(self):
        with open(self.PAGE, encoding="utf-8") as f:
            return f.read()

    def test_母本成品UI一项都没丢(self):
        s = self.html()
        for fn in ("function labelHtml",      # 标签一 / 标签二
                   "function manualHtml",     # 跑 Qwen / 传底图 / 完整提示词
                   "function editHtml",       # 改画面词内联展开收回
                   "function renderFix",      # 红色内容补正
                   "function applyMode",      # 写真模式换视图
                   "function renderSlot",
                   "function setBusy"):       # 开跑按钮置灰
            self.assertIn(fn, s, fn)
        for ui in ("一键加气泡", "完整提示词", "改画面词", "传底图",
                   "剧本可直接改字", "写真 · 单人写真集"):
            self.assertIn(ui, s, ui)

    def test_完成待检查时网页弹一次窗且旧自动发文案已清(self):
        s = self.html()
        self.assertIn("waiting_check", s)
        self.assertIn("window.alert", s)
        self.assertIn("readyShown", s)
        self.assertIn("一键发送 zip 到 QQ", s)
        self.assertNotIn("跑完自动发", s)
        self.assertNotIn("可以直接离开网页", s)

    def test_后端只有整批加气泡才发zip(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "app", "comic_api.py"), encoding="utf-8") as f:
            api = f.read()
        with open(os.path.join(root, "app", "comic_delivery.py"), encoding="utf-8") as f:
            dv = f.read()
        self.assertIn('finish="notify"', api)
        self.assertIn('finish="send" if no == "all" else None', api)
        self.assertIn('spawn_notify(path, qq, "portrait")', api)
        self.assertEqual(api.count("spawn_auto("), 1)   # 只剩 _worker 里那一处
        self.assertIn("def notify_ready", dv)
        self.assertIn("waiting_check", dv)
        self.assertIn("ready_notified", dv)

    def test_反馈在按钮正下方而不是页面顶端(self):
        s = self.html()
        for i in ("lsScript", "lsRun", "lsBubble", "lsPortrait", "lsSend"):
            self.assertIn('id="%s"' % i, s)
        self.assertIn("function friendInline", s)
        self.assertIn("window.friendOnStatus", s)
        self.assertIn("friendInline('lsScript'", s)   # 查库进度也写就地
        self.assertIn("⏳ 正在跑 · 正常排队", s)
        self.assertIn("按钮已置灰", s)

    def test_开跑提交瞬间就置灰(self):
        s = self.html()
        # 母本要等 /api/comic/start 返回才 setBusy，中间几秒会被当成卡住连点
        self.assertIn("if (FRIEND.starting) return;", s)
        self.assertIn("if (btn) btn.disabled = true;", s)
        self.assertIn("⏳ 已提交 · 正常排队", s)

    def test_耗时角标与三格一行(self):
        s = self.html()
        self.assertIn("repeat(3, minmax(0, 1fr))", s)
        self.assertIn('class="cost"', s)
        self.assertIn("⏳ ", s)
        self.assertIn("✓ ", s)

    def test_写真与漫画是顶上两个显式入口(self):
        s = self.html()
        self.assertIn('id="modeTabs"', s)
        self.assertIn("📖 跑漫画", s)
        self.assertIn("📷 跑写真", s)
        # 写真卡必须在①卡正下方，不能又掉到整页最底下
        self.assertLess(s.index('id="portraitCard"'), s.index('id="slotCard"'))

    def test_格数与张数都不能手输(self):
        s = self.html()
        self.assertIn('id="panels" type="hidden"', s)
        self.assertNotIn('id="panels" type="number"', s)
        self.assertNotIn('id="pN"', s)          # 旧精简版能填 30 的写真框
        self.assertIn("不能自己改", s)
        self.assertIn("每本最多 ", s)          # 是上限，不是「一本固定 N 格」
        self.assertNotIn("'一本 ' + n + ' 格", s)

    def test_手动发送与失败反馈都在发送卡就地显示(self):
        s = self.html()
        self.assertIn('id="btnSendQQ"', s)
        self.assertIn('id="btnSendInline"', s)
        self.assertIn("async function doSendQQ", s)
        self.assertIn("'/api/comic/delivery_status?task='", s)
        self.assertIn("⏳ 已提交，打包中", s)
        self.assertIn("跑完会通知", s)
        self.assertIn('id="packs"', s)

    def test_发送规则写在顶部和每个关键按钮下(self):
        s = self.html()
        self.assertIn("<b>发送规则：</b>", s)
        self.assertIn("<b>正常排队</b>", s)
        self.assertIn("一键发送 zip", s)
        self.assertIn("⏳ 已提交 · 正常排队 · 跑完会在 QQ 通知你检查", s)
        self.assertIn("✅ 跑完了 · QQ 已通知", s)
        self.assertIn("⏳ 排队打包中", s)
        self.assertIn("作品名 无气泡", s)
        self.assertIn("作品名 有气泡", s)
        for old in ("正在打包 / 通过 QQ 发送", "大文件要等一会儿",
                    "正在打包当前成品", "不用守着网页"):
            self.assertNotIn(old, s)

    def test_不引用只在5174存在的页面且模型下拉改走配额配置(self):
        s = self.html()
        self.assertNotIn('href="/comic_admin"', s)
        self.assertNotIn('href="/gen"', s)
        self.assertIn("loadProviders = async function", s)
        self.assertIn("'/api/comic/friend_config'", s)

    def test_生成脚本可重复且产物一致(self):
        import subprocess
        before = open(self.PAGE, "rb").read()
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        subprocess.check_call(
            [sys.executable, os.path.join(root, "tools", "comic_friend_patch.py")],
            cwd=root)
        self.assertEqual(open(self.PAGE, "rb").read(), before,
                         "重跑生成脚本产物变了，说明有人手改了生成页")


class TestComicWorks(_TempMixin, unittest.TestCase):
    """站主「群友作品浏览」页：按账号 → 漫画台 / 写真台聚合作品（只读）。

    聚合接口扫 comics/、按 owner.json 归账号，kind 判据 script.md=漫画 /
    prompts.json 或 portraits/=写真。站主态看全部、群友态（5175 通配反代
   过来）403。作品墙只出缩略图 URL，不碰额度 / 发送。
    """

    def setUp(self):
        super().setUp()
        p = mock.patch.object(comic_api, "_COMICS_ROOT", self.tmp)
        p.start()
        self.addCleanup(p.stop)
        self.c = main.app.test_client()

    def _portrait(self, name, owner=QQ, brief="可爱妹妹，刚洗完澡"):
        d = self.mk_task(name, owner=owner, script=False, portrait=True)
        with open(os.path.join(d, "portrait.json"), "w", encoding="utf-8") as f:
            json.dump({"brief": brief, "n": 1}, f, ensure_ascii=False)
        return d

    def test_站主看全部且按账号分漫画写真(self):
        self.mk_task("C20261110-01_学姐的饮料", owner=QQ, script=True, final=True)
        self._portrait("C20261110-02", owner=QQ)
        r = self.c.get("/api/comic/works")
        self.assertEqual(r.status_code, 200)
        accs = r.get_json()["accounts"]
        self.assertEqual(len(accs), 1)
        a = accs[0]
        self.assertEqual(a["qq"], QQ)
        self.assertEqual(a["counts"], {"comic": 1, "portrait": 1})
        cw = a["comic"][0]
        self.assertEqual(cw["kind"], "comic")
        self.assertEqual(cw["count"], 1)
        self.assertIn("thumb/01_final_thumb.webp", cw["cover"])
        self.assertEqual(len(cw["thumbs"]), 1)
        pw = a["portrait"][0]
        self.assertEqual(pw["kind"], "portrait")
        self.assertEqual(pw["title"], "可爱妹妹，刚洗完澡")
        self.assertIn("thumb/01_portrait_thumb.webp", pw["cover"])

    def test_群友态403不能列走所有人作品(self):
        self.mk_task("C20261110-01", owner=QQ, script=True, final=True)
        r = self.c.get("/api/comic/works?qq=%s" % QQ,
                       headers={"X-Friend-Token": SECRET})
        self.assertEqual(r.status_code, 403)
        self.assertFalse(r.get_json()["ok"])

    def test_空壳目录与站主老作品不进账号列表(self):
        # 有归属但没跑（无 script / prompts / portraits）→ 空壳，不算作品
        self.mk_task("C20261110-01", owner=QQ, script=False)
        # 站主老任务无 owner.json → 不进「账号 → 作品」这套
        self.mk_task("C20261110-02", owner="", script=True, final=True)
        r = self.c.get("/api/comic/works")
        self.assertEqual(r.get_json()["accounts"], [])

    def test_多账号分组且新账号排前(self):
        self.mk_task("C20261110-01_A", owner=QQ, script=True, final=True)
        self.mk_task("C20261109-01_B", owner=OTHER, script=True, final=True)
        accs = self.c.get("/api/comic/works").get_json()["accounts"]
        self.assertEqual({a["qq"] for a in accs}, {QQ, OTHER})
        self.assertEqual(accs[0]["qq"], QQ)        # 日期新的排前

    def test_站主态封面也指缩略图不退回高清原图(self):
        self.mk_task("C20261110-01", owner=QQ, script=True, final=True)
        w = self.c.get("/api/comic/works").get_json()["accounts"][0]["comic"][0]
        self.assertTrue(w["cover"].startswith("/api/comic/img?"))
        self.assertNotIn("final/01_final.png", w["cover"])

    def test_页面路由可达且是只读作品浏览(self):
        r = main.app.test_client().get("/comic_works")
        self.assertEqual(r.status_code, 200)
        page = r.get_data(as_text=True)
        self.assertIn("群友作品浏览", page)
        self.assertIn("/api/comic/works", page)
        self.assertIn("漫画台", page)
        self.assertIn("写真台", page)
        # 只读：不出现任何额度 / 发送 / 扣次动作
        for kw in ("send_qq", "consume", "加次数", "mark_charged", "delivery_status"):
            self.assertNotIn(kw, page)


if __name__ == "__main__":
    unittest.main()
