# -*- coding: utf-8 -*-
"""画廊：扫描规则、缩略图、权限。

重点验的不是「功能能用」，是几条**边界**：
- 站主目录里哪些目录算作品集、哪些不算
- 群友只能删自己的、下架之后作者还能看见而别人看不见
- 没配站主时管理接口一律 403（宁可全关也不猜）

DB 走 `config.state_path`，测试进程会自动落到临时目录，不会碰真实数据。
"""

import io
import os
import shutil
import tempfile
import unittest
from unittest import mock

from PIL import Image

from app import friend_auth, gallery, gallery_api


def _png(color=(200, 120, 90), size=(64, 96)):
    b = io.BytesIO()
    Image.new("RGB", size, color).save(b, "PNG")
    return b.getvalue()


def _reset():
    gallery.init()
    with gallery.connect() as con:
        for t in ("works", "items", "marks"):
            con.execute("DELETE FROM " + t)


class _Base(unittest.TestCase):
    def setUp(self):
        _reset()
        self.root = tempfile.mkdtemp(prefix="gal_test_")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.addCleanup(_reset)

    def mk(self, rel, name="a.png"):
        d = os.path.join(self.root, rel)
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, name)
        with open(p, "wb") as f:
            f.write(_png())
        return p

    def works(self):
        with gallery.connect() as con:
            return [dict(r) for r in con.execute(
                "SELECT * FROM works ORDER BY sort")]


class ScanTest(_Base):
    def test_dir_with_images_becomes_one_work(self):
        """目录里直接有图 → 一个作品集；底层子目录各自也是。"""
        self.mk("1", "a.png")
        self.mk("1", "b.png")
        self.mk("2", "c.png")
        out = gallery.scan([("图库", self.root)])
        self.assertEqual(out["items"], 3)
        ws = self.works()
        self.assertEqual([w["title"] for w in ws], ["1", "2"])
        self.assertEqual([w["n"] for w in ws], [2, 1])
        self.assertEqual(ws[0]["grp"], "图库")

    def test_pure_container_dir_is_not_a_work(self):
        """只有子目录、自己没有图 → 不产生作品集，但要往下走。"""
        self.mk("1", "a.png")
        self.mk("sub/2", "b.png")
        gallery.scan([("图库", self.root)])
        titles = [w["title"] for w in self.works()]
        self.assertEqual(titles, ["1", "sub/2"])

    def test_natural_sort(self):
        """1,2,10 而不是 1,10,2。"""
        for n in ("1", "2", "10"):
            self.mk(n, "a.png")
        gallery.scan([("图库", self.root)])
        self.assertEqual([w["title"] for w in self.works()],
                         ["1", "2", "10"])

    def test_second_scan_skips_unchanged(self):
        self.mk("1", "a.png")
        gallery.scan([("图库", self.root)])
        out = gallery.scan([("图库", self.root)])
        self.assertEqual(out["skipped"], 1)
        self.assertEqual(out["works"], 0)
        self.assertEqual(len(self.works()), 1)

    def test_new_image_invalidates_cache(self):
        self.mk("1", "a.png")
        gallery.scan([("图库", self.root)])
        self.mk("1", "b.png")
        out = gallery.scan([("图库", self.root)])
        self.assertEqual(out["changed"], 1)
        self.assertEqual(self.works()[0]["n"], 2)

    def test_missing_root_is_not_fatal(self):
        out = gallery.scan([("没有", os.path.join(self.root, "nope"))])
        self.assertEqual(out["items"], 0)

    def test_cover_is_first_by_natural_order(self):
        self.mk("1", "b.png")
        self.mk("1", "a.png")
        gallery.scan([("图库", self.root)])
        self.assertTrue(self.works()[0]["cover"].endswith("a.png"))


class ThumbTest(_Base):
    def test_thumb_and_view_are_small_webp(self):
        p = self.mk("1", "a.png")
        t = gallery.thumb(p, "t")
        v = gallery.thumb(p, "v")
        self.assertTrue(t.endswith(".webp") and os.path.isfile(t))
        self.assertTrue(v.endswith(".webp") and os.path.isfile(v))
        with Image.open(t) as im:
            self.assertLessEqual(max(im.size), 360)

    def test_second_call_hits_cache(self):
        p = self.mk("1", "a.png")
        a = gallery.thumb(p, "t")
        mtime = os.path.getmtime(a)
        b = gallery.thumb(p, "t")
        self.assertEqual(a, b)
        self.assertEqual(mtime, os.path.getmtime(b))

    def test_missing_file_returns_none(self):
        self.assertIsNone(gallery.thumb(os.path.join(self.root, "nope.png"), "t"))

    def test_broken_image_does_not_raise(self):
        p = os.path.join(self.root, "x.png")
        with open(p, "wb") as f:
            f.write(b"not an image")
        self.assertIsNone(gallery.thumb(p, "t"))


class AliasTest(unittest.TestCase):
    def test_same_qq_same_alias(self):
        self.assertEqual(gallery.alias("12345"), gallery.alias("12345"))

    def test_alias_has_no_digits(self):
        a = gallery.alias("987654321")
        self.assertNotIn("987654321", a)
        self.assertTrue(a.startswith("匿名用户 "))


class ApiTest(_Base):
    def setUp(self):
        super().setUp()
        self.mk("1", "a.png")
        self.mk("1", "b.png")
        gallery.scan([("图库", self.root)])
        from flask import Flask
        self.app = Flask(__name__)
        self.app.register_blueprint(gallery_api.bp)
        self.c = self.app.test_client()
        self.h = {"X-Friend-Token": "tok"}
        p1 = mock.patch.object(friend_auth, "secret", lambda: "tok")
        p1.start()
        self.addCleanup(p1.stop)
        p2 = mock.patch.object(gallery_api, "GALLERY_OWNER_QQ", "10001")
        p2.start()
        self.addCleanup(p2.stop)

    def j(self, r):
        return r.get_json()

    def test_no_token_is_403(self):
        r = self.c.get("/api/gal/list?qq=10001")
        self.assertEqual(r.status_code, 403)

    def test_owner_flag(self):
        self.assertTrue(self.j(self.c.get("/api/gal/me?qq=10001", headers=self.h))["owner"])
        self.assertFalse(self.j(self.c.get("/api/gal/me?qq=20002", headers=self.h))["owner"])

    def test_list_shows_works_with_cover(self):
        d = self.j(self.c.get("/api/gal/list?qq=20002", headers=self.h))
        self.assertTrue(d["ok"])
        self.assertEqual(len(d["works"]), 1)
        self.assertEqual(d["works"][0]["title"], "1")
        self.assertEqual(d["works"][0]["by"], "站长")
        self.assertIsNotNone(d["works"][0]["cover"])

    def test_only_owner_can_scan(self):
        r = self.c.post("/api/gal/scan", json={"qq": "20002"}, headers=self.h)
        self.assertEqual(r.status_code, 403)
        r = self.c.post("/api/gal/scan", json={"qq": "10001"}, headers=self.h)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(self.j(r)["ok"])

    def test_owner_work_cannot_be_deleted_from_web(self):
        wid = self.j(self.c.get("/api/gal/list?qq=10001", headers=self.h))["works"][0]["id"]
        r = self.c.delete("/api/gal/work/%d?qq=10001" % wid, headers=self.h)
        self.assertEqual(r.status_code, 403)

    def test_thumbnail_endpoint(self):
        wid = self.j(self.c.get("/api/gal/list?qq=10001", headers=self.h))["works"][0]["id"]
        iid = self.j(self.c.get("/api/gal/work/%d?qq=10001" % wid, headers=self.h))["items"][0]["id"]
        r = self.c.get("/api/gal/img/%d?s=t&qq=10001" % iid, headers=self.h)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["Content-Type"], "image/webp")
        self.assertLess(len(r.data), 200 * 1024)


class UploadTest(_Base):
    def setUp(self):
        super().setUp()
        from flask import Flask
        self.app = Flask(__name__)
        self.app.register_blueprint(gallery_api.bp)
        self.c = self.app.test_client()
        self.h = {"X-Friend-Token": "tok"}
        for target, attr, val in ((friend_auth, "secret", lambda: "tok"),
                                  (gallery_api, "GALLERY_OWNER_QQ", "10001")):
            p = mock.patch.object(target, attr, val)
            p.start()
            self.addCleanup(p.stop)

    def upload(self, qq, n=2, title="作品"):
        files = [(io.BytesIO(_png()), "x%d.png" % i) for i in range(n)]
        return self.c.post("/api/gal/upload?qq=" + qq,
                           data={"title": title, "files": files},
                           headers=self.h, content_type="multipart/form-data")

    def test_upload_creates_private_work(self):
        d = self.upload("20002").get_json()
        self.assertTrue(d["ok"])
        self.assertEqual(d["n"], 2)
        mine = self.c.get("/api/gal/mine?qq=20002", headers=self.h).get_json()
        self.assertEqual(len(mine["works"]), 1)
        self.assertEqual(mine["works"][0]["n"], 2)
        # 别人也能在公开墙上看到（群友上传默认公开）
        other = self.c.get("/api/gal/list?qq=30003", headers=self.h).get_json()
        self.assertEqual(len(other["works"]), 1)
        # 但看不到 QQ 号
        self.assertNotIn("20002", str(other))

    def test_author_can_delete_other_cannot(self):
        wid = self.upload("20002").get_json()["work_id"]
        r = self.c.delete("/api/gal/work/%d?qq=30003" % wid, headers=self.h)
        self.assertEqual(r.status_code, 403)
        r = self.c.delete("/api/gal/work/%d?qq=20002" % wid, headers=self.h)
        self.assertEqual(r.status_code, 200)

    def test_hidden_only_visible_to_author_and_owner(self):
        wid = self.upload("20002").get_json()["work_id"]
        self.c.post("/api/gal/visible",
                    json={"work_id": wid, "hidden": True, "qq": "10001"},
                    headers=self.h)
        self.assertEqual(self.c.get("/api/gal/work/%d?qq=30003" % wid,
                                    headers=self.h).status_code, 404)
        self.assertEqual(self.c.get("/api/gal/work/%d?qq=20002" % wid,
                                    headers=self.h).status_code, 200)
        self.assertEqual(self.c.get("/api/gal/work/%d?qq=10001" % wid,
                                    headers=self.h).status_code, 200)
        # 公开墙上没有
        pub = self.c.get("/api/gal/list?qq=30003", headers=self.h).get_json()
        self.assertEqual(len(pub["works"]), 0)

    def test_bad_image_rejected(self):
        files = [(io.BytesIO(b"garbage"), "x.png")]
        r = self.c.post("/api/gal/upload?qq=20002",
                        data={"title": "t", "files": files},
                        headers=self.h, content_type="multipart/form-data")
        self.assertEqual(r.status_code, 400)

    def test_like_toggles(self):
        wid = self.upload("20002").get_json()["work_id"]
        iid = self.c.get("/api/gal/work/%d?qq=20002" % wid,
                         headers=self.h).get_json()["items"][0]["id"]
        d = self.c.post("/api/gal/mark",
                        json={"item_id": iid, "kind": "like", "on": True,
                              "qq": "30003"}, headers=self.h).get_json()
        self.assertEqual(d["count"], 1)
        d = self.c.post("/api/gal/mark",
                        json={"item_id": iid, "kind": "like", "on": False,
                              "qq": "30003"}, headers=self.h).get_json()
        self.assertEqual(d["count"], 0)

    def test_fav_shows_up_in_favs(self):
        wid = self.upload("20002").get_json()["work_id"]
        iid = self.c.get("/api/gal/work/%d?qq=20002" % wid,
                         headers=self.h).get_json()["items"][0]["id"]
        self.c.post("/api/gal/mark",
                    json={"item_id": iid, "kind": "fav", "on": True,
                          "qq": "30003"}, headers=self.h)
        d = self.c.get("/api/gal/favs?qq=30003", headers=self.h).get_json()
        self.assertEqual(len(d["items"]), 1)
        self.assertEqual(d["items"][0]["id"], iid)


class ConfigTest(unittest.TestCase):
    def test_parse_roots(self):
        from app.config import _parse_gallery_roots
        out = _parse_gallery_roots(r"图库|D:\a\b;D:\c")
        self.assertEqual(out[0], ("图库", r"D:\a\b"))
        self.assertEqual(out[1][1], r"D:\c")
        self.assertEqual(out[1][0], "c")

    def test_empty_roots(self):
        from app.config import _parse_gallery_roots
        self.assertEqual(_parse_gallery_roots(""), ())
        self.assertEqual(_parse_gallery_roots("  ;  "), ())


if __name__ == "__main__":
    unittest.main()
