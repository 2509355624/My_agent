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


class ScanPathTest(_Base):
    """站主自己挑一个文件夹扫：只扫这一个，标题用目录名而不是分组名。"""

    def test_scan_path_only_touches_that_dir(self):
        self.mk("a/1", "x.png")
        self.mk("b/2", "y.png")
        out = gallery.scan_path(os.path.join(self.root, "a"))
        self.assertEqual(out["items"], 1)
        self.assertEqual([w["title"] for w in self.works()], ["1"])

    def test_scanning_another_dir_keeps_the_first(self):
        """分两次选不同目录，两边都要在（扫描只增不删）。"""
        self.mk("a/1", "x.png")
        self.mk("b/2", "y.png")
        gallery.scan_path(os.path.join(self.root, "a"))
        gallery.scan_path(os.path.join(self.root, "b"))
        self.assertEqual(sorted(w["title"] for w in self.works()), ["1", "2"])

    def test_root_own_title_is_dir_name(self):
        """选 new\\35 扫，作品集标题必须是 35，不能变成分组名「图库」。"""
        self.mk("35", "x.png")
        gallery.scan_path(os.path.join(self.root, "35"))
        w = self.works()[0]
        self.assertEqual(w["title"], "35")
        # 临时目录不在 GALLERY_ROOTS 里 → 自成一组，用目录自己的名字
        self.assertEqual(w["grp"], "35")

    def test_grp_follows_configured_root(self):
        """落在配置根里面就沿用原分组名，别把「图库」改成「35」。"""
        self.mk("35", "x.png")
        p = os.path.join(self.root, "35")
        with mock.patch.object(gallery, "GALLERY_ROOTS",
                               (("图库", self.root),)):
            self.assertEqual(gallery.grp_for(p), "图库")

    def test_rescan_does_not_reorder_existing_works(self):
        """单独扫一个子目录，不能把别的作品集挤到后面去。"""
        self.mk("a/1", "x.png")
        self.mk("b/2", "y.png")
        gallery.scan_path(self.root)
        before = [(w["id"], w["sort"]) for w in self.works()]
        gallery.scan_path(os.path.join(self.root, "b"))
        after = [(w["id"], w["sort"]) for w in self.works()]
        self.assertEqual(before, after)

    def test_missing_path_reports_error(self):
        out = gallery.scan_path(os.path.join(self.root, "nope"))
        self.assertIn("error", out)
        self.assertNotIn("works", out)

    def test_browse_lists_subdirs_with_counts(self):
        self.mk("a", "x.png")
        self.mk("a", "y.png")
        os.makedirs(os.path.join(self.root, "empty"))
        out = gallery.browse(self.root)
        names = [d["name"] for d in out["dirs"]]
        self.assertIn("a", names)
        self.assertIn("empty", names)
        a = [d for d in out["dirs"] if d["name"] == "a"][0]
        empty = [d for d in out["dirs"] if d["name"] == "empty"][0]
        self.assertEqual(a["n"], 2)
        self.assertEqual(empty["n"], 0)
        self.assertEqual(out["images"], 0)
        self.assertEqual(out["parent"], os.path.dirname(self.root))

    def test_browse_reports_direct_images(self):
        """目录自己也直接放图时，面板要能显示「这里有 N 张」。"""
        self.mk("", "z.png")
        out = gallery.browse(self.root)
        self.assertEqual(out["images"], 1)

    def test_browse_missing_dir_is_error(self):
        self.assertIn("error", gallery.browse(os.path.join(self.root, "nope")))


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

    def test_reuse_direct_only_for_webp_within_edge(self):
        """已经是我们压过的 webp 就不该再编码一次——掉画质还白占缓存。"""
        png = self.mk("1", "a.png")
        self.assertFalse(gallery.reuse_direct(png))

        small = os.path.join(self.root, "1", "s.webp")
        Image.new("RGB", (300, 400), (10, 20, 30)).save(small, "WEBP")
        self.assertTrue(gallery.reuse_direct(small))

        big = os.path.join(self.root, "1", "b.webp")
        Image.new("RGB", (2000, 3000), (10, 20, 30)).save(big, "WEBP")
        self.assertFalse(gallery.reuse_direct(big), "超 1600 的 webp 仍要缩")


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

    def test_owner_mine_includes_disk_works(self):
        """站主的「我的」必须包含磁盘那批目录。

        只查 src='user' 的话这里是 0 条，而「扫描文件夹」按钮恰好挂在这个 tab
        上——扫描报「13285 张图」却一张都不显示，站主会以为扫描没生效。
        """
        d = self.j(self.c.get("/api/gal/mine?qq=10001", headers=self.h))
        self.assertTrue(d["ok"])
        self.assertEqual(len(d["works"]), 1)
        self.assertEqual(d["works"][0]["title"], "1")
        self.assertEqual(d["alias"], "站长")

    def test_normal_user_mine_is_empty(self):
        d = self.j(self.c.get("/api/gal/mine?qq=20002", headers=self.h))
        self.assertEqual(d["works"], [])
        self.assertNotEqual(d["alias"], "站长")

    def test_only_owner_can_browse(self):
        r = self.c.get("/api/gal/browse?qq=20002", headers=self.h)
        self.assertEqual(r.status_code, 403)
        with mock.patch.object(gallery, "GALLERY_ROOTS", ()):
            d = self.j(self.c.get("/api/gal/browse?qq=10001", headers=self.h))
        self.assertTrue(d["ok"])
        self.assertEqual(d["path"], "")          # 不带 path 时给起点
        self.assertTrue(any(x["path"].endswith(":\\") for x in d["dirs"]))

    def test_browse_starts_from_configured_roots(self):
        """打开面板直接看到「图库 / 小漫画」，不用从盘符一层层点。"""
        with mock.patch.object(gallery, "GALLERY_ROOTS", (("我的图", self.root),)):
            d = self.j(self.c.get("/api/gal/browse?qq=10001", headers=self.h))
        self.assertEqual([x["name"] for x in d["dirs"]], ["我的图"])
        self.assertEqual(d["dirs"][0]["path"], os.path.abspath(self.root))
        self.assertEqual(d["dirs"][0]["hint"], self.root)

    def test_scan_accepts_a_chosen_path(self):
        self.mk("2", "c.png")
        r = self.c.post("/api/gal/scan",
                        json={"qq": "10001", "path": os.path.join(self.root, "2")},
                        headers=self.h)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.j(r)["result"]["items"], 1)

    def test_scan_with_bad_path_is_400(self):
        r = self.c.post("/api/gal/scan",
                        json={"qq": "10001", "path": os.path.join(self.root, "nope")},
                        headers=self.h)
        self.assertEqual(r.status_code, 400)
        self.assertFalse(self.j(r)["ok"])

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

    def test_full_no_longer_serves_the_original(self):
        """原图下载已取消：传 s=full 也只拿到 webp 压缩图。

        原图中位 5MB、最大 149MB，一个作品集拖一遍就是几 GB 上行。
        站主要原图直接开磁盘，不该从网页出去。
        """
        wid = self.j(self.c.get("/api/gal/list?qq=10001", headers=self.h))["works"][0]["id"]
        iid = self.j(self.c.get("/api/gal/work/%d?qq=10001" % wid, headers=self.h))["items"][0]["id"]
        with gallery.connect() as con:
            src = con.execute("SELECT path FROM items WHERE id=?", (iid,)).fetchone()["path"]
        r = self.c.get("/api/gal/img/%d?s=full&qq=10001" % iid, headers=self.h)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["Content-Type"], "image/webp")
        with open(src, "rb") as f:
            self.assertNotEqual(r.data, f.read(), "s=full 不该再吐出源文件")

    def test_item_ext_is_webp_even_for_png_sources(self):
        """下载拿到的是 webp，文件名就不能按 .png 拼——手机上打不开。"""
        wid = self.j(self.c.get("/api/gal/list?qq=10001", headers=self.h))["works"][0]["id"]
        items = self.j(self.c.get("/api/gal/work/%d?qq=10001" % wid, headers=self.h))["items"]
        self.assertTrue(all(x["ext"] == "webp" for x in items))


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

    def test_uploaded_webp_served_without_extra_cache(self):
        """群友上传落盘时就是 1600px webp，下载直接发源文件，不再压一遍。"""
        wid = self.upload("20002").get_json()["work_id"]
        iid = self.c.get("/api/gal/work/%d?qq=20002" % wid,
                         headers=self.h).get_json()["items"][0]["id"]
        before = set(os.listdir(gallery.CACHE)) if os.path.isdir(gallery.CACHE) else set()
        r = self.c.get("/api/gal/img/%d?s=v&qq=20002" % iid, headers=self.h)
        after = set(os.listdir(gallery.CACHE)) if os.path.isdir(gallery.CACHE) else set()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["Content-Type"], "image/webp")
        self.assertEqual(before, after, "已是 webp 的图不该再生成缓存副本")

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

    def test_scope_owner_is_only_the_owners(self):
        """站长 tab：磁盘目录 + 站主自己上传的，群友传的不在里面。"""
        self.mk("1", "a.png")
        gallery.scan([("图库", self.root)])
        self.upload("20002", title="群友的")
        self.upload("10001", title="站主传的")
        d = self.c.get("/api/gal/list?scope=owner&qq=30003",
                       headers=self.h).get_json()
        titles = sorted(w["title"] for w in d["works"])
        self.assertEqual(titles, ["1", "站主传的"])

    def test_scope_pub_is_only_other_people(self):
        """公开 tab：只有群友上传的，站长的磁盘目录和站主上传都不在。"""
        self.mk("1", "a.png")
        gallery.scan([("图库", self.root)])
        self.upload("20002", title="群友的")
        self.upload("10001", title="站主传的")
        d = self.c.get("/api/gal/list?scope=pub&qq=30003",
                       headers=self.h).get_json()
        self.assertEqual([w["title"] for w in d["works"]], ["群友的"])

    def test_list_without_scope_is_still_everything(self):
        """不带 scope 的老链接行为不能变，否则旧地址会突然少一半。"""
        self.mk("1", "a.png")
        gallery.scan([("图库", self.root)])
        self.upload("20002", title="群友的")
        d = self.c.get("/api/gal/list?qq=30003", headers=self.h).get_json()
        self.assertEqual(len(d["works"]), 2)

    def test_like_shows_up_in_likes(self):
        wid = self.upload("20002").get_json()["work_id"]
        iid = self.c.get("/api/gal/work/%d?qq=20002" % wid,
                         headers=self.h).get_json()["items"][0]["id"]
        self.c.post("/api/gal/mark",
                    json={"item_id": iid, "kind": "like", "on": True,
                          "qq": "30003"}, headers=self.h)
        d = self.c.get("/api/gal/likes?qq=30003", headers=self.h).get_json()
        self.assertEqual(len(d["items"]), 1)
        self.assertEqual(d["items"][0]["id"], iid)

    def test_marked_items_carry_work_and_position(self):
        """收藏/点赞返回的是单张图，必须带 work + idx 才能直接跳到那一张。"""
        wid = self.upload("20002").get_json()["work_id"]
        items = self.c.get("/api/gal/work/%d?qq=20002" % wid,
                           headers=self.h).get_json()["items"]
        for it in items:
            self.c.post("/api/gal/mark",
                        json={"item_id": it["id"], "kind": "fav", "on": True,
                              "qq": "30003"}, headers=self.h)
        d = self.c.get("/api/gal/favs?qq=30003", headers=self.h).get_json()
        self.assertEqual(len(d["items"]), 2)
        for row in d["items"]:
            self.assertEqual(row["work"], wid)
            self.assertIn(row["idx"], (0, 1))


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


class UploadAlreadySmallTest(_Base):
    """浏览器压过的图，服务端别再压第二遍。

    上传慢的根因是传输：一张 8MB 的原图顺着家宽上行推过隧道要十几秒。
    现在前端先在 canvas 里缩到 1600、转 WebP（约 150KB）再传，服务端
    看到「WebP 且长边 ≤1600」就原样落盘 —— 再编一次只是白白丢画质。
    """

    def _webp(self, size, quality=85):
        b = io.BytesIO()
        Image.new("RGB", size, (200, 120, 90)).save(b, "WEBP", quality=quality)
        return b.getvalue()

    def _saved(self, wid):
        with gallery.connect() as con:
            return con.execute("SELECT path FROM items WHERE work_id=?",
                               (wid,)).fetchone()["path"]

    def test_small_webp_is_stored_byte_for_byte(self):
        raw = self._webp((1600, 1200))
        wid, n = gallery.save_upload("20002", [("x.webp", raw)])
        self.assertEqual(n, 1)
        p = self._saved(wid)
        self.assertEqual(os.path.getsize(p), len(raw))
        with open(p, "rb") as f:
            self.assertEqual(f.read(), raw)

    def test_big_webp_still_gets_shrunk(self):
        raw = self._webp((2400, 1800))
        wid, n = gallery.save_upload("20002", [("x.webp", raw)])
        self.assertEqual(n, 1)
        with Image.open(self._saved(wid)) as im:
            self.assertLessEqual(max(im.size), 1600)

    def test_png_without_precompression_still_converted(self):
        """前端压不动的（老浏览器 / 内存不够）传了原图，服务端照旧兜底。"""
        wid, n = gallery.save_upload("20002", [("x.png", _png(size=(900, 1200)))])
        self.assertEqual(n, 1)
        p = self._saved(wid)
        self.assertTrue(p.endswith(".webp"))
        with Image.open(p) as im:
            self.assertEqual(im.format, "WEBP")
