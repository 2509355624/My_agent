"""聊天背景（2026-11-05）的存储、优先级与接口权限。

背景分四层：账号 > 这张卡 > 全局默认 > 内置图。游客没有账号层，
上传/恢复接口都回 403；站主在 5174 的 `/characters` 管全局和卡这两层。

这里只测落盘/解析/接口，不测前端；图片处理用 Pillow 现造。
"""
import io
import os
import tempfile
import unittest
from unittest import mock

import flask
from PIL import Image
from werkzeug.datastructures import FileStorage

from app import agents, chat_bg, guest, persona, persona_api, persona_admin_api

QQ = "2509355624"


def _png_bytes(size=(80, 50), color=(90, 140, 200)):
    img = Image.new("RGB", size, color)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _file(name="bg.png", data=None):
    return FileStorage(stream=io.BytesIO(data or _png_bytes()),
                       filename=name, content_type="image/png")


class BgBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        agents.clear_cache()
        self.addCleanup(agents.clear_cache)

        root = os.path.join(self.tmp.name, "agents")
        os.makedirs(root, exist_ok=True)
        pa = mock.patch.object(agents, "AGENTS_DIR", root)
        pa.start()
        self.addCleanup(pa.stop)

        ps = mock.patch.object(persona, "_STATE_PATH",
                               os.path.join(self.tmp.name, "state",
                                            "persona_sessions.json"))
        ps.start()
        self.addCleanup(ps.stop)

        pg = mock.patch.object(guest, "GUEST_ROOT",
                               os.path.join(self.tmp.name, "游客-角色扮演"))
        pg.start()
        self.addCleanup(pg.stop)

        # 背景目录和内置图目录都指到临时目录，绝不碰真实 state / web/assets
        self.bg_root = os.path.join(self.tmp.name, "backgrounds")
        pr = mock.patch.object(chat_bg, "ROOT", self.bg_root)
        pr.start()
        self.addCleanup(pr.stop)

        self.web = os.path.join(self.tmp.name, "web")
        os.makedirs(os.path.join(self.web, "assets"), exist_ok=True)
        pw = mock.patch.object(chat_bg, "WEB_DIR", self.web)
        pw.start()
        self.addCleanup(pw.stop)

        agents.clear_cache()
        persona.save_cards([
            {"id": "rem", "name": "雷姆", "description": "女仆",
             "personality": "温柔", "systemPrompt": "你是雷姆。",
             "appearancePrompt": "an adult woman with blue hair"},
        ])
        agents.clear_cache()

        app = flask.Flask(__name__)
        app.register_blueprint(persona_api.bp)
        app.register_blueprint(persona_admin_api.bp)
        self.c = app.test_client()

    def touch(self, kind, ident=""):
        """不走图片处理，直接摆一个文件——只关心优先级时用。"""
        p = chat_bg.path_for(kind, ident)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as fh:
            fh.write(b"x")
        return p


class SaveTest(BgBase):

    def test_save_writes_webp_and_thumb(self):
        info = chat_bg.save(_file(), "card", "rem")
        self.assertTrue(info["exists"])
        self.assertTrue(os.path.isfile(chat_bg.path_for("card", "rem")))
        self.assertTrue(os.path.isfile(chat_bg.path_for("card", "rem", thumb=True)))
        self.assertIn("kind=card", info["url"])
        self.assertIn("thumb=1", info["thumb"])

    def test_rejects_non_image(self):
        with self.assertRaises(ValueError):
            chat_bg.save(_file(data=b"not an image"), "default")

    def test_non_image_error_is_actionable(self):
        """报错要说人话：HEIC 该导出成什么，别只丢一句「不是可识别的图片」。"""
        with self.assertRaisesRegex(ValueError, "HEIC") as cm:
            chat_bg.save(_file(data=b"not an image"), "default")
        self.assertIn("JPG", str(cm.exception))

    def test_accepts_gif_first_frame(self):
        """格式白名单已撤：GIF 取首帧，落地仍是 WebP。"""
        img = Image.new("RGB", (20, 20), (10, 100, 200))
        buf = io.BytesIO()
        img.save(buf, "GIF")
        info = chat_bg.save(_file(name="a.gif", data=buf.getvalue()), "default")
        self.assertTrue(info["exists"])
        with Image.open(chat_bg.path_for("default")) as out:
            self.assertEqual(out.format, "WEBP")
            self.assertEqual(out.size, (20, 20))

    def test_accepts_bmp(self):
        buf = io.BytesIO()
        Image.new("RGB", (30, 20), (9, 9, 9)).save(buf, "BMP")
        self.assertTrue(chat_bg.save(_file(name="a.bmp", data=buf.getvalue()),
                                    "default")["exists"])

    def test_accepts_over_8mb(self):
        """跑图台导出的高清 PNG 常常过 8MB；反正落地前会压，不该挡在门口。"""
        raw = _png_bytes() + b"0" * (9 * 1024 * 1024)
        self.assertTrue(chat_bg.save(_file(name="big.png", data=raw),
                                    "default")["exists"])

    def test_byte_limit_message_has_real_size(self):
        with mock.patch.object(chat_bg, "MAX_BYTES", 100):
            with self.assertRaisesRegex(ValueError, "不能超过") as cm:
                chat_bg.save(_file(), "default")
        self.assertIn("MB", str(cm.exception))

    def test_pixel_limit_message_has_real_size(self):
        with mock.patch.object(chat_bg, "MAX_PIXELS", 10):
            with self.assertRaisesRegex(ValueError, "像素太大") as cm:
                chat_bg.save(_file(), "default")
        self.assertIn("80×50", str(cm.exception))

    def test_thresholds_were_relaxed(self):
        self.assertGreaterEqual(chat_bg.MAX_BYTES, 20 * 1024 * 1024)
        self.assertGreater(chat_bg.MAX_PIXELS, 24_000_000)

    def test_delete_removes_original_and_thumb(self):
        chat_bg.save(_file(), "default")
        chat_bg.delete("default")
        self.assertFalse(os.path.isfile(chat_bg.path_for("default")))
        self.assertFalse(os.path.isfile(chat_bg.path_for("default", thumb=True)))
        self.assertFalse(chat_bg.info("default")["exists"])


class ResolveTest(BgBase):

    def test_priority_user_card_default_builtin(self):
        # 内置图是固定文件名，只能直接摆到 web/assets 下
        with open(os.path.join(self.web, "assets", "chat_bg_default.jpg"), "wb") as fh:
            fh.write(b"x")
        self.assertEqual(chat_bg.resolve("", "")["source"], "builtin")

        self.touch("default")
        self.assertEqual(chat_bg.resolve("", "")["source"], "default")

        self.touch("card", "rem")
        self.assertEqual(chat_bg.resolve("", "rem")["source"], "card")

        self.touch("user", QQ)
        self.assertEqual(chat_bg.resolve(QQ, "rem")["source"], "user")

    def test_guest_skips_user_layer(self):
        """游客即使有同 hash 的文件也不读账号层。"""
        self.touch("user", QQ)
        self.touch("card", "rem")
        self.assertEqual(chat_bg.resolve("", "rem", allow_user=False)["source"],
                         "card")


class GuestApiTest(BgBase):

    def test_guest_gets_card_layer_and_cannot_edit(self):
        # 临时卡库里只有 rem；`_guest_line` 找不到 junior 时会退回第一张，
        # 所以游客这条线用的就是 rem 的卡层背景。
        self.touch("card", "rem")
        d = self.c.get("/api/persona/background").get_json()
        self.assertFalse(d["editable"])
        self.assertEqual(d["source"], "card")

    def test_guest_upload_and_reset_are_403(self):
        r = self.c.post("/api/persona/background",
                        data={"file": (io.BytesIO(_png_bytes()), "bg.png")},
                        content_type="multipart/form-data")
        self.assertEqual(r.status_code, 403)
        self.assertTrue(r.get_json()["limit"])
        r2 = self.c.post("/api/persona/background/reset")
        self.assertEqual(r2.status_code, 403)


class MemberApiTest(BgBase):

    def test_upload_then_read_then_reset(self):
        r = self.c.post("/api/persona/background?qq=" + QQ,
                        data={"file": (io.BytesIO(_png_bytes()), "bg.png")},
                        content_type="multipart/form-data")
        self.assertEqual(r.status_code, 200)
        d = r.get_json()
        self.assertTrue(d["ok"])
        self.assertEqual(d["source"], "user")

        got = self.c.get("/api/persona/background?qq=" + QQ).get_json()
        self.assertEqual(got["source"], "user")
        self.assertTrue(got["editable"])
        self.assertTrue(got["thumb"])

        pic = self.c.get(got["url"] + "&qq=" + QQ)
        self.assertEqual(pic.status_code, 200)
        self.assertEqual(pic.mimetype, "image/webp")
        # Windows 上 send_file 的文件句柄不关掉，后面的删除会失败（WinError 32）
        pic.close()

        reset = self.c.post("/api/persona/background/reset?qq=" + QQ).get_json()
        self.assertTrue(reset["ok"])
        self.assertNotEqual(reset["source"], "user")

    def test_rejects_bad_image_with_400(self):
        r = self.c.post("/api/persona/background?qq=" + QQ,
                        data={"file": (io.BytesIO(b"nope"), "x.png")},
                        content_type="multipart/form-data")
        self.assertEqual(r.status_code, 400)


class AdminApiTest(BgBase):

    def test_default_upload_and_delete(self):
        r = self.c.post("/api/persona_admin/background/default",
                        data={"file": (io.BytesIO(_png_bytes()), "bg.png")},
                        content_type="multipart/form-data")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["background"]["exists"])

        d = self.c.get("/api/persona_admin/background/default").get_json()
        self.assertTrue(d["default"]["exists"])

        r2 = self.c.delete("/api/persona_admin/background/default")
        self.assertEqual(r2.status_code, 200)
        self.assertFalse(r2.get_json()["default"]["exists"])

    def test_card_upload_requires_existing_card(self):
        r = self.c.post("/api/persona_admin/background/card/nope",
                        data={"file": (io.BytesIO(_png_bytes()), "bg.png")},
                        content_type="multipart/form-data")
        self.assertEqual(r.status_code, 404)

        r2 = self.c.post("/api/persona_admin/background/card/rem",
                         data={"file": (io.BytesIO(_png_bytes()), "bg.png")},
                         content_type="multipart/form-data")
        self.assertEqual(r2.status_code, 200)
        self.assertTrue(r2.get_json()["background"]["exists"])

        r3 = self.c.delete("/api/persona_admin/background/card/rem")
        self.assertEqual(r3.status_code, 200)
        self.assertFalse(r3.get_json()["background"]["exists"])


class OffFlagTest(BgBase):
    """「移除背景」要跨设备生效（2026-11-12）：标记存在服务端，不是 localStorage。"""

    def test_off_beats_every_layer(self):
        self.touch("user", QQ)
        self.touch("card", "rem")
        self.touch("default")
        self.assertEqual(chat_bg.resolve(QQ, "rem")["source"], "user")

        self.assertTrue(chat_bg.set_off(QQ, True))
        d = chat_bg.resolve(QQ, "rem")
        self.assertEqual(d["source"], "off")
        # 连站主设的卡 / 全局默认也不返回：玩家点过「不要背景」就该一路没有。
        self.assertEqual(d["url"], "")
        self.assertFalse(d["exists"])

    def test_off_is_per_account(self):
        """QQ 关掉背景不影响另一个账号。"""
        self.touch("user", QQ)
        self.touch("user", "10001")
        chat_bg.set_off(QQ, True)
        self.assertEqual(chat_bg.resolve(QQ, "")["source"], "off")
        self.assertEqual(chat_bg.resolve("10001", "")["source"], "user")

    def test_clearing_the_flag_restores_the_picture(self):
        self.touch("user", QQ)
        chat_bg.set_off(QQ, True)
        self.assertFalse(chat_bg.set_off(QQ, False))
        self.assertEqual(chat_bg.resolve(QQ, "")["source"], "user")
        self.assertFalse(os.path.isfile(chat_bg._off_path(QQ)))

    def test_empty_who_is_never_off(self):
        """游客没有账号层，不能写标记。"""
        self.assertFalse(chat_bg.set_off("", True))
        self.assertFalse(chat_bg.is_off(""))

    def test_off_survives_a_fresh_import(self):
        """标记就是磁盘上一个文件：进程重启后照样读得到。"""
        chat_bg.set_off(QQ, True)
        self.assertTrue(os.path.isfile(chat_bg._off_path(QQ)))
        # 换个调用方重新问一遍，走的是文件系统而不是任何内存状态
        self.assertTrue(chat_bg.is_off(QQ))


class OffApiTest(BgBase):
    """接口层：POST /api/persona/background/off。"""

    def _off(self, on, qq=QQ):
        return self.c.post("/api/persona/background/off?qq=" + qq,
                           json={"on": on})

    def test_toggle_is_visible_in_the_next_read(self):
        self.touch("user", QQ)
        self.assertEqual(self.c.get("/api/persona/background?qq=" + QQ)
                         .get_json()["source"], "user")

        d = self._off(True).get_json()
        self.assertTrue(d["ok"])
        self.assertTrue(d["off"])
        self.assertEqual(d["source"], "off")

        got = self.c.get("/api/persona/background?qq=" + QQ).get_json()
        self.assertEqual(got["source"], "off")
        self.assertEqual(got["url"], "")

        d2 = self._off(False).get_json()
        self.assertFalse(d2["off"])
        self.assertEqual(d2["source"], "user")

    def test_guest_is_403(self):
        # 不传 qq = 游客
        r = self.c.post("/api/persona/background/off", json={"on": True})
        self.assertEqual(r.status_code, 403)
        self.assertTrue(r.get_json()["limit"])

    def test_upload_clears_the_off_flag(self):
        """先移除、再换图：新图必须显示，否则用户以为上传失败了。"""
        self.touch("user", QQ)
        self._off(True)
        r = self.c.post("/api/persona/background?qq=" + QQ,
                        data={"file": (io.BytesIO(_png_bytes()), "bg.png")},
                        content_type="multipart/form-data")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(chat_bg.is_off(QQ))
        self.assertEqual(self.c.get("/api/persona/background?qq=" + QQ)
                         .get_json()["source"], "user")

    def test_reset_clears_the_off_flag(self):
        """恢复默认 = 要背景，只是不想要自己那张。"""
        self.touch("user", QQ)
        self.touch("default")
        self._off(True)
        d = self.c.post("/api/persona/background/reset?qq=" + QQ).get_json()
        self.assertFalse(chat_bg.is_off(QQ))
        self.assertEqual(d["source"], "default")


if __name__ == "__main__":
    unittest.main()
