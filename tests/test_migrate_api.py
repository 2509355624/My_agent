# -*- coding: utf-8 -*-
"""图片迁移的路由层测试（app/main.py 的 /migrate 与 /api/migrate/*）。

用 Flask 自带的 test_client，不起真端口、不发真网络请求。
临时目录里放真图片，不碰 ComfyUI、不碰 state/ 真配置。

这里额外锁一条**入口**：`web/index.html` 里必须有指向 `/migrate` 的按钮 ——
页面做得再好，入口被人删掉就等于功能没了，而且删掉是静默的。
"""

import os
import shutil
import tempfile
import unittest

from PIL import Image

import app.main as main
from app import image_migrate as im


def _png(path, size=(120, 160), color=(180, 40, 40)):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    Image.new("RGB", size, color).save(path)


class MigrateRouteTest(unittest.TestCase):

    def setUp(self):
        self.src = tempfile.mkdtemp(prefix="mig_api_src_")
        self.dst = tempfile.mkdtemp(prefix="mig_api_dst_")
        self.addCleanup(shutil.rmtree, self.src, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.dst, ignore_errors=True)
        for n in ("a.png", "b.png", "c.png", "d.png"):
            _png(os.path.join(self.src, n))
        with open(os.path.join(self.src, "skip.txt"), "w") as f:
            f.write("not an image")
        self.c = main.app.test_client()

    def _list(self, **kw):
        q = {"src": self.src, "dst": self.dst, "limit": 3}
        q.update(kw)
        r = self.c.get("/api/migrate/list", query_string=q)
        self.assertEqual(r.status_code, 200, r.data[:300])
        return r.get_json()

    # ── 页面 ────────────────────────────────────────────────
    def test_page_is_served(self):
        # send_from_directory 的响应握着文件句柄，读完要关，否则 ResourceWarning
        with self.c.get("/migrate") as r:
            self.assertEqual(r.status_code, 200)
            self.assertIn("图片迁移", r.get_data(as_text=True))

    def test_index_has_the_entry_button(self):
        """「个人 Agent」页头部必须留着入口。"""
        with self.c.get("/") as r:
            html = r.get_data(as_text=True)
        self.assertIn("'/migrate'", html)
        self.assertIn("迁移", html)

    # ── 列目录 ──────────────────────────────────────────────
    def test_list_first_page(self):
        d = self._list(offset=0)
        self.assertTrue(d["src_ok"])
        self.assertTrue(d["dst_ok"])
        self.assertEqual(d["total"], 4)          # skip.txt 不算
        self.assertEqual([it["name"] for it in d["items"]], ["a.png", "b.png", "c.png"])

    def test_list_second_page(self):
        d = self._list(offset=3)
        self.assertEqual([it["name"] for it in d["items"]], ["d.png"])

    def test_list_missing_src_is_200_with_error(self):
        """路径填错不该 500，要让界面能显示原因。"""
        r = self.c.get("/api/migrate/list",
                       query_string={"src": os.path.join(self.src, "nope"), "limit": 3})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.get_json()["src_ok"])
        self.assertIn("不存在", r.get_json()["error"])

    def test_list_without_dst_still_lists(self):
        r = self.c.get("/api/migrate/list", query_string={"src": self.src, "limit": 3})
        d = r.get_json()
        self.assertEqual(d["total"], 4)
        self.assertFalse(d["dst_ok"])

    # ── 出图 ────────────────────────────────────────────────
    def test_original_image(self):
        with self.c.get("/api/migrate/image",
                        query_string={"src": self.src, "name": "a.png"}) as r:
            self.assertEqual(r.status_code, 200)
            self.assertTrue(r.data.startswith(b"\x89PNG"))

    def test_thumbnail_is_smaller_jpeg(self):
        with self.c.get("/api/migrate/image",
                        query_string={"src": self.src, "name": "a.png", "w": 64}) as r:
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.mimetype, "image/jpeg")
            self.assertTrue(r.data.startswith(b"\xff\xd8"))

    def test_traversal_is_rejected(self):
        for bad in ("../a.png", "..\\a.png", "sub/a.png", "C:\\a.png"):
            r = self.c.get("/api/migrate/image",
                           query_string={"src": self.src, "name": bad})
            self.assertEqual(r.status_code, 400, bad)

    def test_url_encoded_separator_never_serves_a_file(self):
        """`..%2Fa.png` 经 werkzeug 解码后是 `../a.png` —— 但 test_client 传
        dict 会再编码一次，落到 handler 里其实是字面量 `..%2Fa.png`（一个合法
        但不存在于磁盘的文件名）。两种结果都算过：只要**没读到 src 之外的东西**。"""
        r = self.c.get("/api/migrate/image",
                       query_string={"src": self.src, "name": "..%2Fa.png"})
        self.assertIn(r.status_code, (400, 404))

    def test_missing_image_is_404(self):
        r = self.c.get("/api/migrate/image",
                       query_string={"src": self.src, "name": "nope.png"})
        self.assertEqual(r.status_code, 404)

    def test_missing_src_dir_is_404_not_500(self):
        r = self.c.get("/api/migrate/image",
                       query_string={"src": os.path.join(self.src, "nope"), "name": "a.png"})
        self.assertEqual(r.status_code, 404)

    # ── 复制 ────────────────────────────────────────────────
    def test_copy_then_skip(self):
        body = {"src": self.src, "name": "a.png", "dst": self.dst}
        r = self.c.post("/api/migrate/copy", json=body)
        self.assertEqual(r.status_code, 200, r.data[:300])
        self.assertTrue(r.get_json()["ok"])
        self.assertFalse(r.get_json()["skipped"])
        self.assertTrue(os.path.isfile(os.path.join(self.dst, "a.png")))
        self.assertTrue(os.path.isfile(os.path.join(self.src, "a.png")))   # 原图还在

        r2 = self.c.post("/api/migrate/copy", json=body)
        self.assertEqual(r2.status_code, 200)
        self.assertTrue(r2.get_json()["skipped"])

    def test_copy_marks_the_item_migrated_in_the_next_listing(self):
        self.c.post("/api/migrate/copy",
                    json={"src": self.src, "name": "b.png", "dst": self.dst})
        items = {it["name"]: it["migrated"] for it in self._list(offset=0)["items"]}
        self.assertTrue(items["b.png"])
        self.assertFalse(items["a.png"])

    def test_copy_missing_dst_is_400(self):
        r = self.c.post("/api/migrate/copy",
                        json={"src": self.src, "name": "a.png",
                              "dst": os.path.join(self.dst, "nope")})
        self.assertEqual(r.status_code, 400)
        self.assertFalse(r.get_json()["ok"])

    def test_copy_traversal_name_is_400(self):
        r = self.c.post("/api/migrate/copy",
                        json={"src": self.src, "name": "../a.png", "dst": self.dst})
        self.assertEqual(r.status_code, 400)

    # ── 配置 ────────────────────────────────────────────────
    def test_config_roundtrip(self):
        r = self.c.put("/api/migrate/config",
                       json={"src": self.src, "dst": self.dst, "batch": 5})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["batch"], 5)
        got = self.c.get("/api/migrate/config").get_json()
        self.assertEqual(got["src"], self.src)
        self.assertEqual(got["dst"], self.dst)
        self.assertEqual(got["batch"], 5)

    def test_config_batch_is_clamped(self):
        r = self.c.put("/api/migrate/config", json={"batch": 10 ** 6})
        self.assertEqual(r.get_json()["batch"], im.MAX_BATCH)

    def test_config_put_is_guarded_from_remote(self):
        """写接口跟其它后台接口同一道闸 —— 非本机来源要 403。

        ⚠️ 别拿 `203.0.113.x` 之类当「外网」：那是文档保留段，Python 的
        `ipaddress.is_private` 判它**是内网**，`ADMIN_ALLOW_REMOTE=lan`
        会把请求放行。用真正的公网地址。
        """
        r = self.c.put("/api/migrate/config", json={"src": "x"},
                       environ_base={"REMOTE_ADDR": "8.8.8.8"})
        self.assertEqual(r.status_code, 403)

    def test_copy_is_guarded_from_remote(self):
        r = self.c.post("/api/migrate/copy",
                        json={"src": self.src, "name": "a.png", "dst": self.dst},
                        environ_base={"REMOTE_ADDR": "8.8.8.8"})
        self.assertEqual(r.status_code, 403)
        self.assertFalse(os.path.isfile(os.path.join(self.dst, "a.png")))


if __name__ == "__main__":
    unittest.main()
