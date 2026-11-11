# -*- coding: utf-8 -*-
"""「/换装」网页后端测试（2026-11-11）。

钉死四件最容易出错、也最该防回归的事：
  1. **图序对调**：用户图2（被穿的人）→ image_1 主图；用户图1（来源）→
     image_2 参考。反了就整功能废掉（Qwen 的 latent 从 image_1 出）。
  2. 令牌：签发/验证往返、篡改/过期/非法来源一律拒（免登录的唯一凭证）。
  3. 接口：check / upload / gen / status 的鉴权、限流、排队与状态机。
  4. 路由接入：5174 Blueprint、5175 页面 + 反代、QQ 命令正则。

全程不碰真实 ComfyUI / QQ / state：出图线程用 mock 拦掉，落盘钉到临时目录。
"""

import io
import json
import os
import re
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import friend_auth, outfit_api, main


def _png_bytes():
    """一张最小合法 PNG（PIL 能读，供 upload 的 EXIF/转码路径用）。"""
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (200, 100, 50)).save(buf, "PNG")
    return buf.getvalue()


class TokenTest(unittest.TestCase):
    """令牌 = 免登录唯一凭证，必须严。"""

    def test_roundtrip_private_and_group(self):
        for tgt, tid in (("private", "1234567890"), ("group", "987654321")):
            k = friend_auth.make_outfit_token(tgt, tid)
            self.assertTrue(k)
            self.assertEqual(friend_auth.parse_outfit_token(k), (tgt, tid))

    def test_tamper_rejected(self):
        k = friend_auth.make_outfit_token("private", "1234567890")
        bad = k[:-1] + ("0" if k[-1] != "0" else "1")
        self.assertIsNone(friend_auth.parse_outfit_token(bad))

    def test_expired_rejected(self):
        k = friend_auth.make_outfit_token("private", "1234567890")
        p = k.split(".")
        p[2] = str(int(p[2]) - 100000)
        self.assertIsNone(friend_auth.parse_outfit_token(".".join(p)))

    def test_bad_source_rejected(self):
        self.assertEqual(friend_auth.make_outfit_token("hacker", "12345"), "")
        self.assertEqual(friend_auth.make_outfit_token("private", "abc"), "")
        self.assertIsNone(friend_auth.parse_outfit_token(""))
        self.assertIsNone(friend_auth.parse_outfit_token("a.b.c"))


class WorkflowBuildTest(unittest.TestCase):
    """核心：双图对调 + 提示词 + 占位符全替换。"""

    def test_target_goes_to_image1_source_to_image2(self):
        # target=被穿的人(用户图2)，source=衣服来源(用户图1)
        wf = outfit_api._build_workflow("TARGET.png", "SOURCE.png", "outfit")
        self.assertEqual(wf["13"]["inputs"]["image"], "TARGET.png")  # 主图
        self.assertEqual(wf["14"]["inputs"]["image"], "SOURCE.png")  # 参考
        self.assertEqual(wf["20"]["inputs"]["images.image_1"], ["13", 0])
        self.assertEqual(wf["20"]["inputs"]["images.image_2"], ["14", 0])
        # latent 从 image_1 出 → 被穿的人定尺寸/构图
        self.assertEqual(wf["30"]["inputs"]["latent_image"], ["20", 2])

    def test_no_placeholder_left(self):
        for mode in outfit_api.MODES:
            wf = outfit_api._build_workflow("t.png", "s.png", mode)
            blob = json.dumps(wf)
            for ph in ("__OUTFIT_TARGET__", "__OUTFIT_SOURCE__",
                       "__MULTI_PROMPTS__", "__SEED__"):
                self.assertNotIn(ph, blob)
            self.assertTrue(wf["30"]["inputs"]["seed"])

    def test_prompt_selected_by_mode(self):
        wf_o = outfit_api._build_workflow("t.png", "s.png", "outfit")
        wf_p = outfit_api._build_workflow("t.png", "s.png", "pose")
        self.assertIn("clothing", wf_o["20"]["inputs"]["prompt"])
        self.assertIn("pose", wf_p["20"]["inputs"]["prompt"])
        self.assertNotEqual(wf_o["20"]["inputs"]["prompt"],
                            wf_p["20"]["inputs"]["prompt"])

    def test_two_modes_only(self):
        self.assertEqual(outfit_api.MODES, ("outfit", "pose"))
        self.assertIn("first image", outfit_api.PROMPTS["outfit"])  # 主图=保留项
        self.assertIn("second image", outfit_api.PROMPTS["outfit"])  # 参考=来源


class ApiTest(unittest.TestCase):

    def setUp(self):
        self.c = main.app.test_client()
        self.tmp = tempfile.mkdtemp(prefix="outfit_")
        self._root_patch = mock.patch.object(outfit_api, "_ROOT", self.tmp)
        self._root_patch.start()
        with outfit_api._JOBS_LOCK:
            outfit_api._JOBS.clear()
        with outfit_api._IP_LOCK:
            outfit_api._IP_SUBMITS.clear()

    def tearDown(self):
        self._root_patch.stop()

    def _k(self):
        return friend_auth.make_outfit_token("private", "1234567890")

    def test_check_no_token_401(self):
        r = self.c.get("/api/outfit/check")
        self.assertEqual(r.status_code, 401)
        self.assertFalse(r.get_json()["ok"])

    def test_check_valid_token(self):
        r = self.c.get("/api/outfit/check?k=" + self._k())
        self.assertEqual(r.status_code, 200)
        j = r.get_json()
        self.assertTrue(j["ok"])
        self.assertEqual(j["target"], "private")
        self.assertEqual(j["modes"], ["outfit", "pose"])

    def test_upload_two_images_lands_on_disk(self):
        k = self._k()
        r = self.c.post("/api/outfit/upload?k=" + k, data={
            "a": (io.BytesIO(_png_bytes()), "a.png"),
            "b": (io.BytesIO(_png_bytes()), "b.png"),
        }, content_type="multipart/form-data")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["ok"])
        d = outfit_api._tdir(k)
        self.assertTrue(os.path.isfile(os.path.join(d, "a.png")))
        self.assertTrue(os.path.isfile(os.path.join(d, "b.png")))

    def test_upload_missing_one_rejected(self):
        r = self.c.post("/api/outfit/upload?k=" + self._k(), data={
            "a": (io.BytesIO(_png_bytes()), "a.png"),
        }, content_type="multipart/form-data")
        self.assertEqual(r.status_code, 400)

    def test_upload_bad_image_rejected(self):
        r = self.c.post("/api/outfit/upload?k=" + self._k(), data={
            "a": (io.BytesIO(b"not an image"), "a.png"),
            "b": (io.BytesIO(b"not an image"), "b.png"),
        }, content_type="multipart/form-data")
        self.assertEqual(r.status_code, 400)

    def test_gen_queues_and_poll_status(self):
        k = self._k()
        # 先上传两张
        self.c.post("/api/outfit/upload?k=" + k, data={
            "a": (io.BytesIO(_png_bytes()), "a.png"),
            "b": (io.BytesIO(_png_bytes()), "b.png"),
        }, content_type="multipart/form-data")
        # 拦掉真实出图线程：只把状态钉成 done
        def fake_run(tag, kk, parsed, mode, pa, pb):
            outfit_api._write_job(tag, state="done", file="/fake/result.png")
        with mock.patch.object(outfit_api, "_run", side_effect=fake_run):
            r = self.c.post("/api/outfit/gen?k=" + k,
                            json={"mode": "outfit"})
        self.assertEqual(r.status_code, 200)
        tag = r.get_json()["tag"]
        self.assertTrue(tag.startswith("OF-"))
        # 状态（fake_run 同步跑完，直接是 done）
        s = self.c.get("/api/outfit/status?k=%s&tag=%s" % (k, tag))
        self.assertEqual(s.get_json()["state"], "done")

    def test_gen_without_upload_rejected(self):
        r = self.c.post("/api/outfit/gen?k=" + self._k(), json={"mode": "outfit"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("先上传", r.get_json()["error"])

    def test_gen_bad_mode_rejected(self):
        r = self.c.post("/api/outfit/gen?k=" + self._k(), json={"mode": "hack"})
        self.assertEqual(r.status_code, 400)

    def test_status_cross_token_denied(self):
        k1 = friend_auth.make_outfit_token("private", "11111")
        k2 = friend_auth.make_outfit_token("private", "22222")
        outfit_api._write_job("OF-x", token=k1, state="done", file="/f.png")
        r = self.c.get("/api/outfit/status?k=%s&tag=OF-x" % k2)
        self.assertEqual(r.status_code, 404)  # 别人的任务看不到

    def test_per_token_inflight_cap(self):
        k = self._k()
        # 先塞一个 queued 的同令牌任务：再提交就该撞「每令牌在途 ≤ 1」，
        # 这道闸在「检查图片是否存在」之前，所以不用真传图也应 429。
        outfit_api._write_job("OF-1", token=k, state="queued")
        with mock.patch.object(outfit_api, "_run"):
            r = self.c.post("/api/outfit/gen?k=" + k, json={"mode": "outfit"})
        self.assertEqual(r.status_code, 429)
        self.assertIn("上一张", r.get_json()["error"])


class WiringTest(unittest.TestCase):
    """三处接入：5174 路由、5175 反代与页面、QQ 命令正则。"""

    def test_blueprint_routes_registered(self):
        rules = {r.rule for r in main.app.url_map.iter_rules()}
        for p in ("/api/outfit/check", "/api/outfit/upload", "/api/outfit/gen",
                  "/api/outfit/status", "/api/outfit/img"):
            self.assertIn(p, rules)

    def test_qq_command_regex(self):
        from app import direct_gen, qq_bot
        for t in ("/换装", "／换装", "  /换装", "/换装 帮我把衣服换过去"):
            self.assertTrue(direct_gen._OUTFIT_RE.match(t), t)
            self.assertTrue(qq_bot._OUTFIT_RE.match(t), t)
        for t in ("换装", "我想换装", "换装页面好不好看"):  # 无斜杠不命中
            self.assertFalse(direct_gen._OUTFIT_RE.match(t), t)

    def test_regexes_in_sync(self):
        from app import direct_gen, qq_bot
        self.assertEqual(direct_gen._OUTFIT_RE.pattern, qq_bot._OUTFIT_RE.pattern)

    def test_friend_app_page_and_proxy(self):
        import friend_app
        rules = {r.rule for r in friend_app.app.url_map.iter_rules()}
        self.assertIn("/outfit", rules)
        self.assertIn("/api/outfit/<path:sub>", rules)

    def test_outfit_text_without_tunnel(self):
        from app import direct_gen, friend_site
        with mock.patch.object(friend_site, "url", return_value=""):
            txt = direct_gen._outfit_text()
        self.assertIn("隧道没开", txt)

    def test_outfit_text_signs_source(self):
        from app import direct_gen, friend_site, qq_api
        with mock.patch.object(friend_site, "url", return_value="https://x.trycloudflare.com"), \
             mock.patch.object(qq_api, "current_context", return_value=("private", "1234567890")):
            txt = direct_gen._outfit_text()
        self.assertIn("/outfit?k=", txt)
        k = txt.split("/outfit?k=")[1].split("\n")[0]
        self.assertEqual(friend_auth.parse_outfit_token(k),
                         ("private", "1234567890"))


class PageNoticeTest(unittest.TestCase):
    """换装页的「可以退出 + 图自动发」告知（2026-11-11）。

    后端是独立 daemon 线程出图、跑完 _deliver 直接推 QQ，网页只轮询进度——
    关掉页面根本不影响出图。用户常误以为要守着等，所以提交后必须显眼告知
    这两件事。用静态断言钉住文案和显隐逻辑，防止以后改页面时悄悄丢掉。
    """

    @classmethod
    def setUpClass(cls):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "web", "outfit.html"), encoding="utf-8") as f:
            cls.html = f.read()

    def test_hint_tells_user_they_can_leave(self):
        self.assertIn("asyncHint", self.html)
        self.assertIn("可以关掉本页", self.html)
        self.assertIn("不用守着等", self.html)

    def test_hint_tells_user_image_is_pushed_automatically(self):
        self.assertIn("自动发回", self.html)

    def test_hint_hidden_by_default_and_shown_after_enqueue(self):
        # 默认 display:none；gen 成功拿到 tag 后才 add("show")
        self.assertIn("class=\"async-hint\"", self.html)
        self.assertIn('$("asyncHint").classList.add("show")', self.html)

    def test_hint_collapsed_on_fail_done_and_again(self):
        # 失败/出完图/再换一张 都要收起，别留「不用等」的过期提示
        self.assertGreaterEqual(self.html.count(
            '$("asyncHint").classList.remove("show")'), 3)

    def test_waiting_status_line_also_mentions_leaving(self):
        self.assertIn("可以先退出", self.html)


if __name__ == "__main__":
    unittest.main(verbosity=2)
