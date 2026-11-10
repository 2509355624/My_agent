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
import time
import unittest
from unittest import mock

from PIL import Image

from app import friend_auth, gallery, gallery_api

# 预热线程在 _Base 里默认被关掉（它会往缓存目录写文件，干扰数目录的用例），
# 这里先留一份真的，给专门验它的用例用。
_REAL_WARM = gallery.warm
_REAL_WARM_ALL = gallery.warm_all


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
        # 后台预热会往 CACHE 目录里写文件，而有的用例正在数那个目录里有
        # 几个文件（"已是 webp 的不该再生成缓存副本"）—— 后台线程一写，
        # 那个用例就偶发失败。默认关掉，要验预热本身的 WarmCacheTest
        # 自己打开。
        for name in ("warm", "warm_all"):
            p = mock.patch.object(gallery, name, (lambda *a, **k: 0))
            p.start()
            self.addCleanup(p.stop)

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

    def test_rescan_refreshes_metadata_without_rebuilding_items(self):
        """指纹没变也要更新 grp/title（2026-11-05 修 moss 分组错乱）。

        `scan` 原来遇到没变的目录只更新 seen，于是根目录从 `web` 改成
        `moss漫画` 之后，旧作品集永远留在 `web` 组里。现在元数据变了就改，
        items 不动——点赞和收藏不会丢。
        """
        self.mk("1", "a.png")
        gallery.scan([("旧组", self.root, "旧组")])
        wid = self.works()[0]["id"]
        with gallery.connect() as con:
            before = [r["id"] for r in con.execute(
                "SELECT id FROM items WHERE work_id=?", (wid,))]
        out = gallery.scan([("新组", self.root, "新组")])
        self.assertEqual(out["changed"], 1)
        self.assertEqual(self.works()[0]["grp"], "新组")
        with gallery.connect() as con:
            after = [r["id"] for r in con.execute(
                "SELECT id FROM items WHERE work_id=?", (wid,))]
        self.assertEqual(before, after, "元数据刷新不该重建 items")

    def test_natural_order_key_pads_numbers(self):
        """`ord` 排序键：2 必须排在 10 前面，不能按字典序。"""
        self.assertLess(gallery._ord_key("2"), gallery._ord_key("10"))
        self.assertLess(gallery._ord_key("20261031_2321_早起的小猫"),
                        gallery._ord_key("20261031_2331_天黑之后"))


class CategoryAssignTest(unittest.TestCase):
    """分类归并（2026-11-05）：共享一层路径 → 分类；单层包装 → 留在根。"""

    def test_shared_first_level_becomes_category(self):
        found = [
            ("根", "silver系列/便利店/八重神子", "p1", [], "r"),
            ("根", "silver系列/图书馆", "p2", [], "r"),
            ("根", "好评/四宫辉夜", "p3", [], "r"),
            ("根", "好评/早坂爱", "p4", [], "r"),
            ("根", "1", "p5", [], "r"),
        ]
        out = gallery._assign_categories(found)
        self.assertEqual([(x[0], x[1]) for x in out], [
            ("silver系列", "便利店/八重神子"),
            ("silver系列", "图书馆"),
            ("好评", "四宫辉夜"),
            ("好评", "早坂爱"),
            ("根", "1"),
        ])

    def test_single_wrapper_does_not_become_category(self):
        """`moss漫画/<时间戳>` 只有一个作品集时，时间戳不能变成分类。"""
        found = [
            ("moss漫画", "20261031_2321", "p1", [], "r"),
            ("moss漫画", "20261031_2325", "p2", [], "r"),
        ]
        out = gallery._assign_categories(found)
        self.assertEqual([x[0] for x in out], ["moss漫画", "moss漫画"])

    def test_old_web_root_splits_into_moss_category(self):
        """根还是 web 时，`moss漫画/<时间戳>` 也会被拆成 moss漫画 分类。"""
        found = [
            ("web", "moss漫画/20261031_2321", "p1", [], "r"),
            ("web", "moss漫画/20261031_2325", "p2", [], "r"),
        ]
        out = gallery._assign_categories(found)
        self.assertEqual([(x[0], x[1]) for x in out], [
            ("moss漫画", "20261031_2321"),
            ("moss漫画", "20261031_2325"),
        ])


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
        """落在配置根里面就归到根文件夹的实际名字（2026-11-05 改口径）。

        `.env` 里的显示名（「图库」）只留在选目录提示里；墙上分类用
        `Pictures\\new` 的 `new`。这样扫 `new\\35` 不会冒出「35」这种假分类。
        """
        self.mk("35", "x.png")
        p = os.path.join(self.root, "35")
        with mock.patch.object(gallery, "GALLERY_ROOTS",
                               (("图库", self.root),)):
            self.assertEqual(gallery.grp_for(p),
                             gallery._root_name(self.root))

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

    def test_view_of_uploaded_webp_is_the_file_itself(self):
        """群友上传落盘的就是 1600px webp，预览版等于它本身。

        再编码一次只掉画质、白占一份磁盘；后台按作品集预热大图时还会
        白跑一遍 CPU（一张 0.2 秒）。
        """
        p = os.path.join(self.root, "1", "s.webp")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        Image.new("RGB", (1200, 1600), (10, 20, 30)).save(p, "WEBP")
        self.assertEqual(gallery.thumb(p, "v"), p)
        self.assertFalse(os.path.exists(gallery.cache_path(p, "v")))
        # 缩略图另算：那个非得生成不可
        self.assertNotEqual(gallery.thumb(p, "t"), p)


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

    def test_guest_can_read_the_public_wall(self):
        """游客（2026-11-04）：不带 qq 也能列，但「我的」是空的、不能点赞。"""
        d = self.j(self.c.get("/api/gal/list", headers=self.h))
        self.assertTrue(d["ok"])
        self.assertTrue(d["guest"])
        self.assertFalse(d["works"][0]["mine"])

    def test_guest_cannot_write(self):
        r = self.c.post("/api/gal/mark", headers=self.h,
                        json={"item_id": 1, "kind": "like", "on": True})
        self.assertEqual(r.status_code, 400)
        r = self.c.get("/api/gal/mine", headers=self.h)
        self.assertEqual(r.status_code, 400)

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
        """打开面板直接看到「图库 / 小漫画」，不用从盘符一层层点。

        根后面还要留着盘符入口：ComfyUI 的输出目录在别的盘，配了根之后
        不该把站主锁死在这两个目录里（以前就是这个毛病，想扫个别的地方
        得先去改 .env 再重启）。
        """
        with mock.patch.object(gallery, "GALLERY_ROOTS", (("我的图", self.root),)):
            d = self.j(self.c.get("/api/gal/browse?qq=10001", headers=self.h))
        self.assertEqual(d["dirs"][0]["name"], "我的图")
        self.assertEqual(d["dirs"][0]["path"], os.path.abspath(self.root))
        self.assertEqual(d["dirs"][0]["hint"], self.root)
        if os.name == "nt":
            self.assertTrue(any(x["path"].endswith(":\\") for x in d["dirs"]))

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

class ComicGroupingTest(_Base):
    """漫画归一（2026-11-10）：所有漫画折成一个「漫画」标签，别一本一个标签。

    真实库里漫画有两种落库形态，必须都认到、且都只出现在「漫画」这一个分类：
      1) 双渠道本 → grp=`漫画_C...`，title=渠道名；
      2) 单渠道本 → grp=`web`，title=`漫画_C.../anima-line`。
    web 组里还夹着一条真·非漫画（title=生图页），不能把它吞进漫画、也不能丢。
    """

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
        self._insert_works([
            ("漫画_C20261104-01_妹妹的闺蜜", "anima-line", "owner", 15),
            ("漫画_C20261104-01_妹妹的闺蜜", "qwen_image_v1", "owner", 10),
            ("漫画_C20261105-01_烟火", "anima-line", "owner", 13),   # 第 2 本双渠道
            ("web", "漫画_C20261102-01_草莓/anima-line", "owner", 6),  # 单渠道藏在 web
            ("web", "生图页", "owner", 226),                           # web 里的真·非漫画
            ("moss漫画", "20261031_2321", "owner", 4),                 # 普通分类
        ])

    def j(self, r):
        return r.get_json()

    def _insert_works(self, rows):
        """rows: [(grp, title, src, n)]，直接插库，绕开扫描造历史数据形态。"""
        with gallery.connect() as con:
            for i, (grp, title, src, n) in enumerate(rows):
                con.execute(
                    "INSERT INTO works (key,src,owner,grp,title,path,n,sort,ord,ts,seen)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    ("owner:cg:%d:%d" % (time.time_ns(), i),
                     src, "" if src == "owner" else "20002",
                     grp, title, "/tmp/x", n, i, "", 0, 0))

    def test_分类条把漫画折成一个标签(self):
        d = self.j(self.c.get("/api/gal/list?qq=10001&scope=owner", headers=self.h))
        grps = d["grps"]
        self.assertIn(gallery_api.COMIC_LABEL, grps)
        # 2 个 `漫画_C...` 碎标签一个都不该再出现
        self.assertFalse(any(g.startswith("漫画_C") for g in grps))
        # 普通分类还在，web 也还在（还有生图页那条）
        self.assertIn("moss漫画", grps)
        self.assertIn("web", grps)

    def test_点漫画捞全两种形态(self):
        d = self.j(self.c.get("/api/gal/list?qq=10001&scope=owner&grp=漫画",
                              headers=self.h))
        # 双渠道第 1 本 2 行 + 第 2 本 1 行 + 藏在 web 组的单渠道 1 行 = 4
        self.assertEqual(len(d["works"]), 4)
        self.assertTrue(all(("漫画_" in w["grp"] or "漫画_" in w["title"])
                            for w in d["works"]))

    def test_点web不含漫画只剩生图页(self):
        d = self.j(self.c.get("/api/gal/list?qq=10001&scope=owner&grp=web",
                              headers=self.h))
        self.assertEqual(len(d["works"]), 1)
        self.assertEqual(d["works"][0]["title"], "生图页")

    def test_其它分类不被NOT误杀(self):
        d = self.j(self.c.get("/api/gal/list?qq=10001&scope=owner&grp=moss漫画",
                              headers=self.h))
        self.assertEqual(len(d["works"]), 1)
        self.assertEqual(d["works"][0]["title"], "20261031_2321")

    def test_公开墙不冒出空白漫画标签(self):
        # 公开墙全是 src='user'，没有磁盘漫画 → 不该出「漫画」标签
        self._insert_works([("群友", "某群友作品", "user", 3)])
        d = self.j(self.c.get("/api/gal/list?qq=20002&scope=pub", headers=self.h))
        self.assertNotIn(gallery_api.COMIC_LABEL, d["grps"])

    def test_无漫画时不显示漫画标签(self):
        with gallery.connect() as con:
            con.execute("DELETE FROM works")
        d = self.j(self.c.get("/api/gal/list?qq=10001&scope=owner", headers=self.h))
        self.assertNotIn(gallery_api.COMIC_LABEL, d["grps"])



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


class UploadOneByOneTest(_Base):
    """逐张上传：先开壳，压好一张塞一张。

    换掉「攒一批一次 POST」的原因是第一张就能落地，不用等整批压完。
    这里盯的是壳和图的一致性：序号连续、封面只认第一张、同一序号重传
    不会多出一条、空壳能收回去。
    """

    def _webp(self, color=(10, 20, 30)):
        b = io.BytesIO()
        Image.new("RGB", (1600, 1200), color).save(b, "WEBP", quality=85)
        return b.getvalue()

    def _work(self, wid):
        with gallery.connect() as con:
            r = con.execute("SELECT * FROM works WHERE id=?",
                            (wid,)).fetchone()
        return dict(r) if r else None

    def _items(self, wid):
        with gallery.connect() as con:
            return [dict(r) for r in con.execute(
                "SELECT * FROM items WHERE work_id=? ORDER BY idx", (wid,))]

    def test_shell_starts_empty(self):
        wid = gallery.create_work("20003", "测试")
        w = self._work(wid)
        self.assertEqual(w["n"], 0)
        self.assertEqual(w["cover"], "")
        self.assertEqual(w["title"], "测试")
        self.assertEqual(w["src"], "user")
        self.assertEqual(w["key"], "user:20003/%d" % wid)

    def test_append_keeps_order_and_cover(self):
        wid = gallery.create_work("20003")
        self.assertEqual(
            gallery.append_upload("20003", wid, "a.webp", self._webp()), 1)
        self.assertEqual(
            gallery.append_upload("20003", wid, "b.webp", self._webp()), 2)
        items = self._items(wid)
        self.assertEqual([i["idx"] for i in items], [0, 1])
        self.assertEqual(self._work(wid)["n"], 2)
        # 封面只认第一张，后面追加不动它
        self.assertEqual(self._work(wid)["cover"], items[0]["path"])

    def test_append_same_idx_overwrites(self):
        """同一张失败重传：顶掉旧行，不该变成两条记录。"""
        wid = gallery.create_work("20003")
        gallery.append_upload("20003", wid, "a.webp", self._webp(), idx=1)
        gallery.append_upload("20003", wid, "a.webp", self._webp(), idx=1)
        self.assertEqual(self._work(wid)["n"], 1)
        self.assertEqual(len(self._items(wid)), 1)

    def test_own_work_only_for_owner(self):
        wid = gallery.create_work("20003")
        self.assertEqual(gallery.own_work("20003", wid), (True, 0))
        self.assertEqual(gallery.own_work("99999", wid), (False, 0))

    def test_abort_only_removes_empty_shell(self):
        wid = gallery.create_work("20003")
        self.assertFalse(gallery.abort_upload("99999", wid))   # 不是他的，不动
        self.assertTrue(gallery.abort_upload("20003", wid))    # 空壳 → 收回
        self.assertIsNone(self._work(wid))

    def test_abort_keeps_work_with_images(self):
        wid = gallery.create_work("20003")
        gallery.append_upload("20003", wid, "a.webp", self._webp())
        self.assertFalse(gallery.abort_upload("20003", wid))
        self.assertEqual(self._work(wid)["n"], 1)


class UploadOneByOneApiTest(_Base):
    """`/upload/one`：第一张不带 wid 开壳，后面带着同一个 wid 追加。"""

    def setUp(self):
        super().setUp()
        from flask import Flask
        self.app = Flask(__name__)
        self.app.register_blueprint(gallery_api.bp)
        self.c = self.app.test_client()
        self.h = {"X-Friend-Token": "tok"}
        p1 = mock.patch.object(friend_auth, "secret", lambda: "tok")
        p1.start()
        self.addCleanup(p1.stop)

    def _webp(self):
        b = io.BytesIO()
        Image.new("RGB", (1600, 1200), (30, 60, 90)).save(b, "WEBP",
                                                         quality=85)
        return b.getvalue()

    def post(self, qq="20003", **form):
        data = dict(form)
        data["file"] = (io.BytesIO(self._webp()), "a.webp")
        return self.c.post("/api/gal/upload/one?qq=" + qq, headers=self.h,
                           data=data, content_type="multipart/form-data")

    def test_first_creates_then_appends(self):
        d1 = self.post(title="测试").get_json()
        self.assertTrue(d1["ok"])
        self.assertEqual(d1["n"], 1)
        wid = d1["work_id"]

        d2 = self.post(wid=str(wid)).get_json()
        self.assertTrue(d2["ok"])
        self.assertEqual(d2["work_id"], wid)
        self.assertEqual(d2["n"], 2)

    def test_cannot_append_to_someone_else(self):
        wid = self.post().get_json()["work_id"]
        r = self.post(qq="30003", wid=str(wid))
        self.assertEqual(r.status_code, 404)

    def test_bad_image_is_rejected_not_saved(self):
        r = self.c.post("/api/gal/upload/one?qq=20003", headers=self.h,
                        data={"file": (io.BytesIO(b"not an image"), "x.png")},
                        content_type="multipart/form-data")
        self.assertEqual(r.status_code, 400)

    def test_abort_removes_the_shell(self):
        wid = self.post().get_json()["work_id"]
        r = self.c.post("/api/gal/upload/abort?qq=20003", headers=self.h,
                        data={"wid": str(wid)},
                        content_type="multipart/form-data")
        self.assertFalse(r.get_json()["removed"])     # 已经有一张，不删
        wid2 = gallery.create_work("20003")
        r2 = self.c.post("/api/gal/upload/abort?qq=20003", headers=self.h,
                         data={"wid": str(wid2)},
                         content_type="multipart/form-data")
        self.assertTrue(r2.get_json()["removed"])


class NoiseTailTest(_Base):
    """标题里「几乎每个作品集都多出来的那一层」要去掉，但不许误杀真内容。

    ComfyUI 的漫画输出是「任务名/moss-qunyou/图」，67 个任务全顶着同一个
    moss-qunyou，挂墙上纯噪音；而 `角色A/便利店`、`角色B/便利店` 这种重名
    是真内容，抹掉就把信息弄丢了——靠「多少个作品集共享这一层」来分。
    """

    def found(self, titles):
        return [("g", t, "/x/" + t.replace("/", "_"), [("a.png", 1)])
                for t in titles]

    def titles(self, found):
        return [t for _g, t, _p, _f in found]

    def test_shared_tail_is_dropped(self):
        out = gallery._drop_noise_tail(
            self.found(["任务%d/moss-qunyou" % i for i in range(8)]))
        self.assertEqual(self.titles(out), ["任务%d" % i for i in range(8)])

    def test_a_few_duplicates_are_kept(self):
        """只有两三个重名时，那层很可能是内容，留着。"""
        src = ["角色A/便利店", "角色B/便利店", "角色C/图书馆"]
        self.assertEqual(self.titles(gallery._drop_noise_tail(self.found(src))),
                         src)

    def test_single_level_titles_untouched(self):
        src = ["图库", "小漫画"]
        self.assertEqual(self.titles(gallery._drop_noise_tail(self.found(src))),
                         src)

    def test_empty_input(self):
        self.assertEqual(gallery._drop_noise_tail([]), [])


class UploadLimitTest(_Base):
    """一个作品集最多 100 张，一次最多 10 张 —— 两条限制管的是不同的事。

    单次上限是「一口气别等太久」，作品集上限是「别把一个作品集塞成仓库」。
    群友会分几次往同一个作品集里加图，所以上限必须拦在 append 这一层：
    只拦接口的话，换个入口（旧的批量上传）就绕过去了。
    """

    def _webp(self):
        b = io.BytesIO()
        Image.new("RGB", (1600, 1200), (9, 9, 9)).save(b, "WEBP", quality=85)
        return b.getvalue()

    def test_work_max_is_hundred(self):
        self.assertEqual(gallery.WORK_MAX, 100)

    def test_append_stops_at_cap(self):
        wid = gallery.create_work("20004")
        with mock.patch.object(gallery, "WORK_MAX", 2):
            gallery.append_upload("20004", wid, "a.webp", self._webp())
            gallery.append_upload("20004", wid, "b.webp", self._webp())
            with self.assertRaises(gallery.WorkFull):
                gallery.append_upload("20004", wid, "c.webp", self._webp())

    def test_resend_same_index_past_cap_is_allowed(self):
        """满了之后，重传同一个序号（上一张失败的重试）不算新增，放行。"""
        wid = gallery.create_work("20004")
        with mock.patch.object(gallery, "WORK_MAX", 1):
            gallery.append_upload("20004", wid, "a.webp", self._webp())
            self.assertEqual(
                gallery.append_upload("20004", wid, "a2.webp", self._webp(),
                                      idx=1), 1)

    def test_under_cap_still_appends(self):
        wid = gallery.create_work("20004")
        with mock.patch.object(gallery, "WORK_MAX", 3):
            for _ in range(3):
                gallery.append_upload("20004", wid, "a.webp", self._webp())
        _, n = gallery.own_work("20004", wid)
        self.assertEqual(n, 3)


class UploadLimitApiTest(_Base):
    """接口层：满了要在**收包前**就说，别让群友白等十张。"""

    def setUp(self):
        super().setUp()
        from flask import Flask
        self.app = Flask(__name__)
        self.app.register_blueprint(gallery_api.bp)
        self.c = self.app.test_client()
        self.h = {"X-Friend-Token": "tok"}
        p1 = mock.patch.object(friend_auth, "secret", lambda: "tok")
        p1.start()
        self.addCleanup(p1.stop)

    def _webp(self):
        b = io.BytesIO()
        Image.new("RGB", (1600, 1200), (40, 50, 60)).save(b, "WEBP",
                                                          quality=85)
        return b.getvalue()

    def post(self, qq="20004", **form):
        data = dict(form)
        data["file"] = (io.BytesIO(self._webp()), "a.webp")
        return self.c.post("/api/gal/upload/one?qq=" + qq, headers=self.h,
                           data=data, content_type="multipart/form-data")

    def test_reports_remaining_slots(self):
        d = self.post(title="配额").get_json()
        self.assertTrue(d["ok"])
        self.assertEqual(d["left"], gallery.WORK_MAX - 1)

    def test_full_work_is_rejected_before_reading(self):
        wid = self.post(title="满").get_json()["work_id"]
        with mock.patch.object(gallery, "WORK_MAX", 1):
            r = self.post(wid=str(wid))
        self.assertEqual(r.status_code, 400)
        self.assertIn("传满", r.get_json()["error"])


class ImageCacheApiTest(_Base):
    """图片要能长期留在浏览器里。

    原先只给 10 分钟，等于「过十分钟再来，几十张缩略图全从这台机器重传一遍」。
    item id 是一次性的（重传 DELETE 再 INSERT 换来新 id），同一个 URL 的字节
    不会变，所以可以 immutable；但必须带 private，否则 Cloudflare 边缘会存下来，
    变成「猜到 id 就能绕过登录拿图」。
    """

    def setUp(self):
        super().setUp()
        from flask import Flask
        self.app = Flask(__name__)
        self.app.register_blueprint(gallery_api.bp)
        self.c = self.app.test_client()
        p1 = mock.patch.object(friend_auth, "secret", lambda: "tok")
        p1.start()
        self.addCleanup(p1.stop)

        b = io.BytesIO()
        Image.new("RGB", (1200, 1600), (30, 60, 90)).save(b, "WEBP", quality=85)
        self.wid = gallery.create_work("20005", "缓存")
        gallery.append_upload("20005", self.wid, "a.webp", b.getvalue())
        with gallery.connect() as con:
            self.iid = con.execute(
                "SELECT id FROM items WHERE work_id=?", (self.wid,)
            ).fetchone()["id"]

    def get(self, headers=None):
        h = {"X-Friend-Token": "tok"}
        if headers:
            h.update(headers)
        return self.c.get("/api/gal/img/%d?qq=20005&s=t" % self.iid, headers=h)

    def test_header_allows_long_private_cache(self):
        cc = self.get().headers.get("Cache-Control") or ""
        self.assertIn("max-age=31536000", cc)
        self.assertIn("immutable", cc)
        self.assertIn("private", cc)

    def test_etag_still_returns_304(self):
        r = self.get()
        self.assertEqual(r.status_code, 200)
        et = r.headers.get("ETag")
        self.assertTrue(et)
        r2 = self.get(headers={"If-None-Match": et})
        self.assertEqual(r2.status_code, 304)
        self.assertEqual(len(r2.data), 0)

    def test_hidden_work_is_404_for_others(self):
        """别的群友能看公开作品，但作者下架之后就该 404，不能被缓存头绕过。"""
        h = {"X-Friend-Token": "tok"}
        r = self.c.get("/api/gal/img/%d?qq=20006&s=t" % self.iid, headers=h)
        self.assertEqual(r.status_code, 200)      # 公开的，谁都能看
        with gallery.connect() as con:
            con.execute("UPDATE works SET hidden=1 WHERE id=?", (self.wid,))
        r2 = self.c.get("/api/gal/img/%d?qq=20006&s=t" % self.iid, headers=h)
        self.assertEqual(r2.status_code, 404)


class ThumbsBatchApiTest(_Base):
    """一屏缩略图一次拿回来 —— 这是「首屏十几秒」唯一划算的解法。

    瓶颈不是带宽（一屏 400KB），是往返：走隧道取一张中位 593ms，24 张各自
    等一次就是十几秒。批接口把 24 次等待压成 1 次。所以这里要验的是
    「一次请求真的能拿到多张」以及「权限照旧，不能因为批量就放松」。
    """

    def setUp(self):
        super().setUp()
        from flask import Flask
        self.app = Flask(__name__)
        self.app.register_blueprint(gallery_api.bp)
        self.c = self.app.test_client()
        p1 = mock.patch.object(friend_auth, "secret", lambda: "tok")
        p1.start()
        self.addCleanup(p1.stop)

        b = io.BytesIO()
        Image.new("RGB", (1200, 1600), (70, 90, 120)).save(b, "WEBP",
                                                          quality=85)
        self.wid = gallery.create_work("30001", "批量")
        for _ in range(3):
            gallery.append_upload("30001", self.wid, "a.webp", b.getvalue())
        with gallery.connect() as con:
            self.ids = [r["id"] for r in con.execute(
                "SELECT id FROM items WHERE work_id=? ORDER BY idx",
                (self.wid,))]

    def get(self, ids, qq="30001", s="t"):
        h = {"X-Friend-Token": "tok"}
        return self.c.get("/api/gal/thumbs?qq=%s&s=%s&ids=%s"
                          % (qq, s, ",".join(str(i) for i in ids)), headers=h)

    def test_all_requested_images_come_back_in_one_response(self):
        d = self.get(self.ids).get_json()
        self.assertTrue(d["ok"])
        self.assertEqual(sorted(int(k) for k in d["items"]), sorted(self.ids))
        for v in d["items"].values():
            self.assertTrue(v.startswith("data:image/webp;base64,"))
            self.assertGreater(len(v), 200)

    def test_ids_are_capped(self):
        """要 40 个只给 24 个：一屏的量，再多就是一次传几 MB。"""
        many = list(range(1, 41))
        d = self.get(many).get_json()
        self.assertLessEqual(len(d["items"]), gallery_api.THUMB_BATCH)

    def test_junk_and_missing_ids_are_dropped_silently(self):
        h = {"X-Friend-Token": "tok"}
        r = self.c.get("/api/gal/thumbs?qq=30001&ids=abc,,%d,999999"
                       % self.ids[0], headers=h)
        d = r.get_json()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(list(d["items"]), [str(self.ids[0])])

    def test_hidden_work_is_not_leaked_to_others(self):
        """别人下架的作品集，批量接口也必须一张都不给（不能绕过单张的 404）。"""
        with gallery.connect() as con:
            con.execute("UPDATE works SET hidden=1 WHERE id=?", (self.wid,))
        d = self.get(self.ids, qq="30002").get_json()
        self.assertEqual(d["items"], {})
        # 作者自己还能看见
        self.assertEqual(len(self.get(self.ids).get_json()["items"]), 3)

    def test_empty_ids_is_not_a_500(self):
        h = {"X-Friend-Token": "tok"}
        r = self.c.get("/api/gal/thumbs?qq=30001&ids=", headers=h)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["items"], {})


class WarmApiTest(_Base):
    """打开详情页就把「接下来要看的大图」提前算好。

    一张 5MB 原图现转码 0.4 秒，点开的那 0.4 秒浏览器是纯黑屏。前端知道
    整集列表，就把活提前派出去；后端算完，回来只是 send_file。
    这里验的是排得对不对：排的是 `v` 不是 `t`、顺序跟请求一致（第一个才
    是马上就要看的）、越权的图一张都不排。
    """

    def setUp(self):
        super().setUp()
        from flask import Flask
        self.app = Flask(__name__)
        self.app.register_blueprint(gallery_api.bp)
        self.c = self.app.test_client()
        p = mock.patch.object(friend_auth, "secret", lambda: "tok")
        p.start()
        self.addCleanup(p.stop)

        self.wid = gallery.create_work("30001", "预热")
        b = io.BytesIO()
        Image.new("RGB", (900, 1200), (10, 20, 30)).save(b, "PNG")
        for _ in range(3):
            gallery.append_upload("30001", self.wid, "a.png", b.getvalue())
        with gallery.connect() as con:
            rows = con.execute("SELECT id, path FROM items WHERE work_id=?"
                               " ORDER BY idx", (self.wid,)).fetchall()
        self.items = [(r["id"], r["path"]) for r in rows]
        self.calls = []

    def post(self, ids, qq="30001"):
        def fake(paths, kinds=("t",), front=False):
            self.calls.append((list(paths), kinds, front))
            return len(paths)
        with mock.patch.object(gallery, "warm", fake):
            return self.c.post("/api/gal/warm?qq=" + qq,
                               json={"ids": ids},
                               headers={"X-Friend-Token": "tok"})

    def test_queues_view_size_in_requested_order(self):
        ids = [self.items[2][0], self.items[0][0], self.items[1][0]]
        d = self.post(ids).get_json()
        self.assertTrue(d["ok"])
        self.assertEqual(d["n"], 3)
        paths, kinds, front = self.calls[0]
        self.assertEqual(kinds, ("v",), "大图预热就该排预览图，不是缩略图")
        self.assertTrue(front, "正在看的那几张要插队")
        self.assertEqual(paths, [self.items[2][1], self.items[0][1],
                                 self.items[1][1]])

    def test_hidden_work_is_not_queued_for_others(self):
        with gallery.connect() as con:
            con.execute("UPDATE works SET hidden=1 WHERE id=?", (self.wid,))
        self.post([i for i, _ in self.items], qq="30002")
        self.assertEqual(self.calls, [], "别人下架的图不该为外人占 CPU")

    def test_ids_are_capped_and_junk_dropped(self):
        """一次最多 WARM_MAX 个：这是「提前算几张」的闸门，不是全库预热入口。"""
        junk = ["x", None, {}, 999999]
        real = [i for i, _ in self.items]
        self.post(junk + real)
        paths = self.calls[0][0]
        self.assertEqual(paths, [p for _, p in self.items],
                         "垃圾 id 跳过，真的照顺序排")
        # 假的排在前面，挤满名额之后真的一个也进不去 —— 说明截断发生在
        # 查库之前，不是查完再砍。
        many = list(range(900000, 900000 + gallery_api.WARM_MAX + 20)) + real
        self.calls = []
        self.post(many)
        self.assertEqual(self.calls, [], "超上限的 id 压根不该查库")

    def test_empty_is_not_a_500(self):
        r = self.post([])
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.calls, [])


class WarmCacheTest(_Base):
    """扫完之后后台补缩略图：有人点进来时缓存应该已经好了。

    线程是「队列空了就退出」的那种，不是常驻任务，所以这里必须等到文件
    真的落盘再断言 —— 只看「排进去了」是假通过。
    """

    def setUp(self):
        super().setUp()
        with gallery._warm_cv:
            gallery._warm_q.clear()
            gallery._warm_seen.clear()
        for name, fn in (("warm", _REAL_WARM), ("warm_all", _REAL_WARM_ALL)):
            p = mock.patch.object(gallery, name, fn)
            p.start()
            self.addCleanup(p.stop)

    def test_warm_writes_thumb_in_background(self):
        b = io.BytesIO()
        Image.new("RGB", (1200, 1600), (10, 20, 30)).save(b, "WEBP", quality=85)
        wid = gallery.create_work("30003", "预热")
        gallery.append_upload("30003", wid, "a.webp", b.getvalue())
        with gallery.connect() as con:
            p = con.execute("SELECT path FROM items WHERE work_id=?",
                            (wid,)).fetchone()["path"]
        out = gallery.cache_path(p, "t")
        # append_upload 自己会排一次（插队到最前面），等它落盘就是验收标准。
        for _ in range(100):
            if os.path.exists(out):
                break
            time.sleep(0.05)
        self.assertTrue(os.path.exists(out), "后台没把缩略图算出来")

    def test_uploaded_image_jumps_the_queue(self):
        """上传的图插队：站主扫一万张时，群友刚传的十张不能排在后面等。"""
        a = self.mk("1", "a.png")
        b = self.mk("2", "b.png")
        gallery.warm([a], front=False)
        gallery.warm([b], front=True)
        with gallery._warm_cv:
            order = [k[0] for k in gallery._warm_q]
        self.assertEqual(order[:2], [b, a])

    def test_missing_cache_skips_cached_and_vanished(self):
        a = self.mk("1", "a.png")
        b = self.mk("2", "b.png")
        gallery.thumb(a, "t")                      # a 已经有缓存
        missing = gallery.missing_cache([a, b, os.path.join(self.root, "nope.png")])
        self.assertEqual(missing, [b])


class DeleteItemTest(_Base):
    """删单张：权限、文件、重排序号。

    最要紧的一条是「删了中间一张之后再传一张，不能把别人顶掉」——
    append_upload 拿序号当文件名，序号没排干净就是静默覆盖，看着像丢了图。
    """

    def _webp(self, color=(10, 20, 30)):
        b = io.BytesIO()
        Image.new("RGB", (1600, 1200), color).save(b, "WEBP", quality=85)
        return b.getvalue()

    def _items(self, wid):
        with gallery.connect() as con:
            return [dict(r) for r in con.execute(
                "SELECT * FROM items WHERE work_id=? ORDER BY idx", (wid,))]

    def _work(self, wid):
        with gallery.connect() as con:
            r = con.execute("SELECT * FROM works WHERE id=?",
                            (wid,)).fetchone()
        return dict(r) if r else None

    def _make(self, qq="20005", n=5):
        wid = gallery.create_work(qq, "删图测试")
        for i in range(n):
            gallery.append_upload(qq, wid, "a.webp",
                                  self._webp((i * 20, 30, 40)))
        return wid

    def test_delete_middle_reindexes_and_keeps_files(self):
        wid = self._make(n=5)
        before = {i["id"] for i in self._items(wid)}
        victim = self._items(wid)[2]
        ok, why, got = gallery.delete_item("20005", victim["id"])
        self.assertTrue(ok, why)
        self.assertEqual(got, wid)

        left = self._items(wid)
        self.assertEqual(len(left), 4)
        self.assertEqual([i["idx"] for i in left], [0, 1, 2, 3])
        self.assertEqual(sorted(os.path.basename(i["path"]) for i in left),
                         ["001.webp", "002.webp", "003.webp", "004.webp"])
        for i in left:
            self.assertTrue(os.path.isfile(i["path"]), i["path"])
        # 路径会被顶上来复用（003.webp 现在是原来第 4 张），所以这里按
        # item id 判断，不能按路径——剩下 4 条正好是原来 5 条去掉被删那条。
        self.assertNotIn(victim["id"], [i["id"] for i in left])
        self.assertEqual({i["id"] for i in left}, before - {victim["id"]})
        self.assertEqual(self._work(wid)["n"], 4)

    def test_delete_cover_moves_cover_to_next(self):
        wid = self._make(n=3)
        gallery.delete_item("20005", self._items(wid)[0]["id"])
        left = self._items(wid)
        self.assertEqual(self._work(wid)["cover"], left[0]["path"])

    def test_append_after_delete_does_not_overwrite(self):
        """删一张再传一张：老图不能被顶掉。"""
        wid = self._make(n=4)
        before = {i["id"] for i in self._items(wid)}
        victim = self._items(wid)[1]
        gallery.delete_item("20005", victim["id"])

        n = gallery.append_upload("20005", wid, "new.webp", self._webp())
        after = self._items(wid)
        self.assertEqual(len(after), 4)
        self.assertEqual([i["idx"] for i in after], [0, 1, 2, 3])
        self.assertTrue((before - {victim["id"]}).issubset(
            {i["id"] for i in after}))
        self.assertEqual(n, 4)
        for i in after:
            self.assertTrue(os.path.isfile(i["path"]), i["path"])

    def test_only_author_or_owner_can_delete(self):
        wid = self._make(qq="20005", n=2)
        iid = self._items(wid)[0]["id"]
        ok, _why, _ = gallery.delete_item("30003", iid)
        self.assertFalse(ok)
        ok, _why, _ = gallery.delete_item("30003", iid, is_owner=True)
        self.assertTrue(ok)

    def test_missing_item_is_not_an_error_with_work(self):
        ok, _why, wid = gallery.delete_item("20005", 999999)
        self.assertFalse(ok)
        self.assertEqual(wid, 0)

    def test_disk_work_cannot_be_deleted_from_web(self):
        self.mk("35")
        gallery.scan([("图库", self.root, "图库")])
        wid = self.works()[0]["id"]
        iid = self._items(wid)[0]["id"]
        ok, why, _ = gallery.delete_item("20005", iid, is_owner=True)
        self.assertFalse(ok)
        self.assertIn("磁盘", why)

    def test_marks_go_with_the_image(self):
        wid = self._make(qq="20005", n=2)
        iid = self._items(wid)[0]["id"]
        with gallery.connect() as con:
            con.execute("INSERT INTO marks(qq,item_id,kind,ts)"
                        " VALUES(?,?,?,?)", ("30003", iid, "like", time.time()))
        gallery.delete_item("20005", iid)
        with gallery.connect() as con:
            c = con.execute("SELECT COUNT(*) c FROM marks WHERE item_id=?",
                            (iid,)).fetchone()["c"]
        self.assertEqual(c, 0)


class DeleteItemApiTest(_Base):
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

    def _one(self, qq="20002"):
        return self.c.post(
            "/api/gal/upload?qq=" + qq,
            data={"title": "t", "files": [(io.BytesIO(_png()), "x.png")]},
            headers=self.h, content_type="multipart/form-data").get_json()

    def test_delete_single_image(self):
        wid = self._one()["work_id"]
        iid = self.c.get("/api/gal/work/%d?qq=20002" % wid,
                         headers=self.h).get_json()["items"][0]["id"]
        r = self.c.delete("/api/gal/item/%d?qq=20002" % iid, headers=self.h)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["left"], 0)

    def test_others_cannot_delete_single_image(self):
        wid = self._one()["work_id"]
        iid = self.c.get("/api/gal/work/%d?qq=20002" % wid,
                         headers=self.h).get_json()["items"][0]["id"]
        r = self.c.delete("/api/gal/item/%d?qq=30003" % iid, headers=self.h)
        self.assertEqual(r.status_code, 403)

    def test_missing_image_is_404(self):
        r = self.c.delete("/api/gal/item/999999?qq=20002", headers=self.h)
        self.assertEqual(r.status_code, 404)


class MergeUploadTest(_Base):
    """默认并进最近一个没装满的作品集，凑满 100 张才开下一个。"""

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

    def _up(self, qq="20006", force_new=False, title="t"):
        data = {"title": title,
                "files": [(io.BytesIO(_png()), "x.png")]}
        if force_new:
            data["force_new"] = "1"
        return self.c.post("/api/gal/upload?qq=" + qq, data=data,
                           headers=self.h,
                           content_type="multipart/form-data").get_json()

    def test_second_upload_merges_into_the_first(self):
        a = self._up()
        b = self._up()
        self.assertEqual(a["work_id"], b["work_id"])
        self.assertTrue(b["merged"])

    def test_force_new_makes_another_one(self):
        a = self._up()
        b = self._up(force_new=True)
        self.assertNotEqual(a["work_id"], b["work_id"])
        self.assertFalse(b["merged"])

    def test_full_work_starts_a_new_one(self):
        a = self._up()
        with mock.patch.object(gallery, "WORK_MAX", 1):
            b = self._up()
        self.assertNotEqual(a["work_id"], b["work_id"])

    def test_two_users_never_share(self):
        self.assertNotEqual(self._up(qq="20006")["work_id"],
                            self._up(qq="20007")["work_id"])

    def test_merge_keeps_the_original_title(self):
        self._up(title="第一个")
        d = self._up(title="第二个")
        with gallery.connect() as con:
            t = con.execute("SELECT title FROM works WHERE id=?",
                            (d["work_id"],)).fetchone()["title"]
        self.assertEqual(t, "第一个")


