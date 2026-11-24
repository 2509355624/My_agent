# -*- coding: utf-8 -*-
"""单张隐藏 + 刷新不再冒出参考图栏（2026-11-24 晚，用户第 1、2 条）。

两条都是「看起来只是 UI」但根因在数据侧的问题，所以两边都钉住：
  · 单张隐藏要**真的**从别人眼里消失（不是前端不画），并且站主能反悔；
  · works.n 必须是「可见张数」，否则一本藏光了还挂在墙上；
  · 参考图栏自己冒出来，是因为存档里的 `k:"ref"` 记录没人清。
"""
import io
import os
import sqlite3
import tempfile
import unittest
from unittest import mock

from app import main
from app.config import GALLERY_OWNER_QQ
from app.gateways import friend_auth
from app.features.gallery import gallery
from app.features.gallery import routes as gallery_api

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
WS = os.path.join(ROOT, "web", "workspace")


def _read(*parts):
    p = os.path.join(*parts)
    with io.open(p, encoding="utf-8") as f:
        return f.read()


class _Db(unittest.TestCase):
    """临时库，别碰真实的那份 state/gallery.db。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="gal_hide_")
        self.db = os.path.join(self.tmp, "g.db")
        self.patch = mock.patch.object(gallery, "DB", self.db)
        self.patch.start()
        gallery.init()

    def tearDown(self):
        self.patch.stop()
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _work(self, title="测试本", n=3):
        """造一本 n 张图的作品集，返回 (wid, [iid...])。文件不用真存在：
        这一批测的是「看不看得见」，不是「发不发得出图」。"""
        paths = []
        with gallery.connect() as con:
            cur = con.execute(
                "INSERT INTO works(key, src, owner, grp, title, path, cover, n,"
                " ts, seen) VALUES(?,?,?,?,?,?,?,?,?,?)",
                ("k-" + title, "owner", "", "g", title, os.path.join(self.tmp, title),
                 "", n, 1.0, 1.0))
            wid = cur.lastrowid
            for i in range(n):
                p = os.path.join(self.tmp, title, "%02d.webp" % i)
                os.makedirs(os.path.dirname(p), exist_ok=True)
                with open(p, "wb") as f:
                    f.write(b"x")
                paths.append(p)
                con.execute("INSERT INTO items(work_id, idx, path, bytes)"
                            " VALUES(?,?,?,?)", (wid, i, p, 1))
            con.execute("UPDATE works SET cover=? WHERE id=?", (paths[0], wid))
        return wid, paths


class TestSetItemHidden(_Db):

    def test_hidden_item_drops_out_of_count(self):
        wid, paths = self._work(n=3)
        with gallery.connect() as con:
            iid = con.execute("SELECT id FROM items WHERE work_id=?"
                              " ORDER BY idx LIMIT 1", (wid,)).fetchone()["id"]
        ok, why, got_wid, left = gallery.set_item_hidden(iid, 1)
        self.assertTrue(ok, why)
        self.assertEqual(got_wid, wid)
        self.assertEqual(left, 2, "藏了一张之后 n 该是 2")
        with gallery.connect() as con:
            self.assertEqual(con.execute("SELECT n FROM works WHERE id=?",
                                         (wid,)).fetchone()["n"], 2)

    def test_hidden_item_is_still_there(self):
        """藏 != 删：文件、记录都留着，恢复一次就回来。"""
        wid, paths = self._work(n=2)
        with gallery.connect() as con:
            iid = con.execute("SELECT id FROM items WHERE work_id=?",
                              (wid,)).fetchone()["id"]
        gallery.set_item_hidden(iid, 1)
        with gallery.connect() as con:
            row = con.execute("SELECT hidden, path FROM items WHERE id=?",
                              (iid,)).fetchone()
        self.assertEqual(row["hidden"], 1)
        self.assertTrue(os.path.isfile(row["path"]), "藏一张不该删文件")

        ok, _why, _wid, left = gallery.set_item_hidden(iid, 0)
        self.assertTrue(ok)
        self.assertEqual(left, 2)

    def test_cover_moves_off_hidden_first_item(self):
        """封面从不带头那张开始，藏了它就得换一张——不然墙上还显示它。"""
        wid, paths = self._work(n=2)
        with gallery.connect() as con:
            first = con.execute("SELECT id, path FROM items WHERE work_id=?"
                                " ORDER BY idx LIMIT 1", (wid,)).fetchone()
        gallery.set_item_hidden(first["id"], 1)
        with gallery.connect() as con:
            cover = con.execute("SELECT cover FROM works WHERE id=?",
                                (wid,)).fetchone()["cover"]
        self.assertNotEqual(cover, first["path"])

    def test_all_hidden_means_gone_from_wall(self):
        wid, paths = self._work(n=2)
        with gallery.connect() as con:
            ids = [r["id"] for r in con.execute(
                "SELECT id FROM items WHERE work_id=?", (wid,))]
        for i in ids:
            _ok, _why, _w, left = gallery.set_item_hidden(i, 1)
        self.assertEqual(left, 0, "全藏完应该是 0 张可见")

    def test_hidden_items_lists_only_hidden(self):
        wid, paths = self._work(n=3)
        with gallery.connect() as con:
            ids = [r["id"] for r in con.execute(
                "SELECT id FROM items WHERE work_id=? ORDER BY idx", (wid,))]
        gallery.set_item_hidden(ids[1], 1)
        got = gallery.hidden_items(wid)
        self.assertEqual([x["id"] for x in got], [ids[1]])
        # 不传 wid 就是全库的
        self.assertIn(ids[1], [x["id"] for x in gallery.hidden_items()])

    def test_rescan_keeps_hidden_marks(self):
        """站主往目录里新丢几张图、重新扫一次——藏过的还是藏着的。

        扫描走的是「整本重写 items」，不特意搬一次就会全丢。
        """
        wid, paths = self._work(n=2)
        with gallery.connect() as con:
            hidden_path = con.execute(
                "SELECT path FROM items WHERE work_id=? ORDER BY idx LIMIT 1",
                (wid,)).fetchone()["path"]
            iid = con.execute("SELECT id FROM items WHERE work_id=?"
                              " ORDER BY idx LIMIT 1", (wid,)).fetchone()["id"]
        gallery.set_item_hidden(iid, 1)

        # 模拟「目录指纹变了 → 整本重写」：直接把 fp 改掉，再对这本目录跑扫描
        with gallery.connect() as con:
            con.execute("UPDATE works SET fp='stale' WHERE id=?", (wid,))
        # 多加一张新图，让 file 列表和旧的不同
        extra = os.path.join(self.tmp, "测试本", "99.webp")
        with open(extra, "wb") as f:
            f.write(b"x")
        gallery.scan_path(os.path.join(self.tmp, "测试本"))

        with gallery.connect() as con:
            row = con.execute("SELECT hidden FROM items WHERE work_id=?"
                              " AND path=?", (wid, hidden_path)).fetchone()
        self.assertIsNotNone(row, "重新扫描后这张不该消失")
        self.assertEqual(row["hidden"], 1, "重新扫描把隐藏标记弄丢了")


class TestItemHiddenApi(_Db):
    """接口层：权限 + 别人看不到。

    鉴权走内部令牌（和 test_gallery 一样）：`_guard` 先看是不是本机直连，
    测试进程里那条件不成立，所以得假一把 secret + 站主 QQ。
    """

    def setUp(self):
        super().setUp()
        self.c = main.app.test_client()
        self.h = {"X-Friend-Token": "tok"}
        p1 = mock.patch.object(friend_auth, "secret", lambda: "tok")
        p1.start()
        self.addCleanup(p1.stop)
        p2 = mock.patch.object(gallery_api, "GALLERY_OWNER_QQ", "10001")
        p2.start()
        self.addCleanup(p2.stop)
        self.owner = "10001"

    def test_owner_can_hide_and_see_mark(self):
        wid, _ = self._work(n=2)
        with gallery.connect() as con:
            iid = con.execute("SELECT id FROM items WHERE work_id=?"
                              " ORDER BY idx LIMIT 1", (wid,)).fetchone()["id"]
        r = self.c.post("/api/gal/item/hidden?qq=" + self.owner, headers=self.h,
                        json={"item_id": iid, "hidden": True})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["ok"])

        # 站主进这本时，藏的那张仍在列表里，带 hidden 标记（要能反悔）
        d = self.c.get("/api/gal/work/%d?qq=%s" % (wid, self.owner),
                       headers=self.h).get_json()
        hit = [x for x in d["items"] if x["id"] == iid]
        self.assertEqual(len(hit), 1, "站主自己得看得到自己藏了什么")
        self.assertEqual(hit[0]["hidden"], 1)
        self.assertTrue(d["work"]["can_hide_item"])

    def test_others_do_not_see_hidden_item(self):
        wid, _ = self._work(n=2)
        with gallery.connect() as con:
            iid = con.execute("SELECT id FROM items WHERE work_id=?"
                              " ORDER BY idx LIMIT 1", (wid,)).fetchone()["id"]
            gallery.set_item_hidden(iid, 1)
        d = self.c.get("/api/gal/work/%d?qq=30001" % wid, headers=self.h).get_json()
        self.assertNotIn(iid, [x["id"] for x in d["items"]])
        self.assertFalse(d["work"]["can_hide_item"], "群友不该有隐藏按钮")

    def test_non_owner_cannot_hide(self):
        wid, _ = self._work(n=1)
        with gallery.connect() as con:
            iid = con.execute("SELECT id FROM items WHERE work_id=?",
                              (wid,)).fetchone()["id"]
        r = self.c.post("/api/gal/item/hidden?qq=30002", headers=self.h,
                        json={"item_id": iid, "hidden": True})
        self.assertEqual(r.status_code, 403)

    def test_hidden_image_is_closed_for_others(self):
        """别人拿到的是 404（「没这张图」）；群友走网关那条路是 403
        （`_guard` 先拦）。两种都是「拿不到」，不细究状态码。"""
        wid, paths = self._work(n=1)
        with gallery.connect() as con:
            iid = con.execute("SELECT id FROM items WHERE work_id=?",
                              (wid,)).fetchone()["id"]
        gallery.set_item_hidden(iid, 1)
        r = self.c.get("/api/gal/img/%d?qq=30003" % iid, headers=self.h)
        self.assertIn(r.status_code, (403, 404))
        self.assertNotIn("image/", r.headers.get("Content-Type", ""),
                         "别人不该拿到这张的字节")


class TestRefBarDoesNotComeBack(unittest.TestCase):
    """第 1 条：刷新 / 重新进入时，引用图栏不许自己冒出来。"""

    def test_rehydrated_ref_is_not_painted(self):
        js = _read(WS, "js", "ref.js")
        self.assertIn("resumed", js, "恢复出来的那张得带上标记")
        # syncHint 只画没有 resumed 的那些
        i = js.index("function syncHint")
        seg = js[i:i + 900]
        self.assertIn("!r.resumed", seg, "syncHint 得把恢复的那张排除掉")

    def test_commit_clears_archive_refs(self):
        js = _read(WS, "js", "ref.js")
        i = js.index("function commit")
        seg = js[i:i + 700]
        self.assertIn("dropRefRecords", seg,
                      "发送成功要把存档里那条 ref 删掉，否则下次刷新又认回来")

    def test_archive_only_revives_tail_ref(self):
        js = _read(WS, "js", "archive.js")
        self.assertIn("lastRefAt", js, "只认归档末尾那条")
        self.assertIn("lastRefAt === d.msgs.length - 1", js)


class TestFrontendWiring(unittest.TestCase):

    def test_hide_button_in_viewer(self):
        js = _read(WS, "js", "gallery.js")
        self.assertIn("WSHide.makeButton", js)
        self.assertIn('"/api/gal/item/hidden"', js, "按钮要打到新接口")
        self.assertIn("can_hide_item", js)

    def test_hide_module_is_loaded_before_gallery(self):
        html = _read(WS, "chat.html")
        self.assertIn("js/hide.js", html)
        self.assertLess(html.index("js/hide.js"), html.index("js/gallery.js"),
                        "gallery.js 用到 WSHide，必须先加载")

    def test_admin_page_has_per_item_button(self):
        html = _read(ROOT, "web", "gallery.html")
        self.assertIn("hb-item", html, "站主作品集详情页也要有单张隐藏按钮")
        self.assertIn('api("item/hidden"', html)


if __name__ == "__main__":
    unittest.main()
