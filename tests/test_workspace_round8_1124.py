# -*- coding: utf-8 -*-
"""2026-11-24 第三批：漫画每格服饰 + 画廊工作台视角。

用户的四条原话（按顺序）：

1. 「这个 moss 漫画，我要求 ai 严格按照我的剧情大纲来……每页有时候都不去写
   角色的服饰特征，导致有时候我要求显示不同服饰都不行，角色穿什么衣服，每个
   格子都给我写清楚，不要指望写一个女生就行了」
2. 「我点击已隐藏的作品集，直接看到半透明的已隐藏的图片，然后作品集无法左右
   滑动了……我是用站长账号登陆看的，站长账号直接看图片被卡死」
   → 后半句他补的：「我管理画廊是在管理页进行管理的，站长正常登录工作台不就是
   和普通的用户一样的吗！你分不清管理页入口和站长普通号吗？」
3. 「打开网页的加载速度严重下降，我怀疑你把聊天界面返回 1m 图片的那个讨论用到了
   我的画廊里面，然后我下载了一个图片，180k」→ 网格封面确实带了 `fit=1m`
4. 「站长账号下载的图片，只有 48b，打开显示损坏，群友号没有这个问题」
"""
import io
import os
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
WS = os.path.join(ROOT, "web", "workspace")

from app.features.comic import moss_agent as A   # noqa: E402


def _read(*parts):
    with io.open(os.path.join(*parts), "r", encoding="utf-8") as f:
        return f.read()


class TestEveryCellCarriesTheOutfit(unittest.TestCase):
    """1. 看得到身体的每一格都必须写清她穿什么。"""

    OUTFIT = ("a navy blue sailor top with a white sailor collar,"
              " a pale blue pleated skirt")

    def _page(self):
        return {"items": [
            # 只写「一个女生」——就是用户说的那种
            {"label": "character", "id": "a",
             "caption": "1girl, long black hair, sitting by the window"},
            # 自己写全了
            {"label": "character", "id": "b",
             "caption": "1girl, wearing " + self.OUTFIT + ", standing"},
            # 脸特写：画面里没有身体，不该被塞整件衣服
            {"label": "character", "id": "c",
             "caption": "close-up of her face, black hair, teary eyes"},
            # 物件格：更不该
            {"label": "object", "id": "d", "caption": "a wooden desk"},
        ]}

    def test_cells_get_the_outfit_stamped(self):
        pj = self._page()
        A._stamp_outfits_cells(pj, {"girl": self.OUTFIT})
        by = {it["id"]: it["caption"] for it in pj["items"]}
        self.assertIn(self.OUTFIT, by["a"], "只写一个女生的格子必须补上衣着")
        self.assertEqual(by["b"].count(self.OUTFIT), 1,
                         "自己写全了的格子不许再贴一遍（会被画成两件衣服）")
        self.assertNotIn("wears", by["c"], "脸特写不该被塞整件衣服")
        self.assertNotIn("wears", by["d"], "物件格不该有衣着")

    def test_missing_outfit_is_reported_per_page(self):
        """没交作业要能让重修轮看见（blocking），不是静默放过。"""
        with io.open(os.path.join(ROOT, "app", "features", "comic",
                                  "moss_agent.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertIn("没写她穿什么", src)
        self.assertIn("只写一个女生＝没交作业", src)
        # 判据来自模型自己写的 outfits，不是我另造一份衣物白名单
        self.assertIn("_covered(cap)", src)

    def test_prompt_requires_outfit_in_every_cell(self):
        self.assertIn("看得到身体的每一格，都必须把这页 outfits", A.SYSTEM)
        self.assertIn("光写 `1girl, sitting by the window` 就是没交作业", A.SYSTEM)

    def test_top_variants_count_as_clothing(self):
        """`sailor top` / `crop top` 这类写法不能被当成「没写衣服」。"""
        import re
        for w in ("sailor top", "tank top", "crop top", "pleated skirt"):
            self.assertTrue(re.search(A._CLOTH, w), w)


class TestWorkbenchSeesNothingHidden(unittest.TestCase):
    """2. 工作台里站长和普通用户看到的一模一样，隐藏是管理页的事。"""

    def setUp(self):
        from unittest import mock
        from app.features.gallery import routes as G
        from app.gateways import friend_auth

        p1 = mock.patch.object(friend_auth, "secret", lambda: "tok")
        p1.start()
        self.addCleanup(p1.stop)
        self.owner = "2509355624"
        p2 = mock.patch.object(G, "GALLERY_OWNER_QQ", self.owner)
        p2.start()
        self.addCleanup(p2.stop)

    def _get(self, path, admin=True):
        """走 5175 网关那条路（带 token + qq），管理页和工作台只差一个 __jf。"""
        from flask import Flask
        from app.features.gallery import routes as G
        app = Flask(__name__)
        app.register_blueprint(G.bp)
        h = {"X-Friend-Token": "tok"}
        q = "qq=%s" % self.owner + ("" if admin else "&__jf=1")
        return app.test_client().get(path + "?" + q, headers=h).get_json()

    def _wid_with_hidden(self):
        """临时建一本：两张图，藏掉一张。不碰生产的 state/gallery.db。"""
        import shutil
        import tempfile
        from PIL import Image
        from app.features.gallery import gallery

        root = tempfile.mkdtemp(prefix="gal_jf_")
        self.addCleanup(shutil.rmtree, root, True)
        d = os.path.join(root, "换新衣")
        os.makedirs(d, exist_ok=True)
        for n in ("a.png", "b.png"):
            Image.new("RGB", (64, 96), (200, 120, 90)).save(
                os.path.join(d, n), "PNG")
        gallery.scan_path(root)
        with gallery.connect() as con:
            wid = con.execute("SELECT id FROM works WHERE title='换新衣'"
                              ).fetchone()["id"]
            iid = con.execute("SELECT id FROM items WHERE work_id=?"
                              " ORDER BY idx LIMIT 1", (wid,)).fetchone()["id"]
        gallery.set_item_hidden(iid, 1)
        return wid

    def test_admin_page_still_sees_hidden(self):
        wid = self._wid_with_hidden()
        d = self._get("/api/gal/work/%d" % wid, admin=True)
        self.assertTrue(d["ok"], d)
        self.assertTrue([i for i in d["items"] if i.get("hidden")],
                        "管理页要能看见已隐藏的图（否则没法反悔）")

    def test_workbench_hides_them_from_even_the_owner(self):
        wid = self._wid_with_hidden()
        d = self._get("/api/gal/work/%d" % wid, admin=False)
        self.assertTrue(d["ok"], d)
        self.assertEqual([i for i in d["items"] if i.get("hidden")], [],
                         "工作台里不许出现已隐藏的图（会滑到、会下载、会卡死）")
        self.assertFalse(d["work"]["can_hide_item"],
                         "工作台里不该有「隐藏」按钮")
        self.assertEqual(d["work"]["hidden_n"], 0,
                         "工作台里不该有「已藏 N」")

    def test_gateway_marks_workbench_requests(self):
        src = io.open(os.path.join(ROOT, "friend_app.py"),
                      encoding="utf-8").read()
        self.assertIn("jf=True", src)
        self.assertIn('params["__jf"] = "1"', src)

    def test_tracks_skip_hidden_items(self):
        js = _read(WS, "js", "gallery.js")
        self.assertIn("filter(function (it) { return !it.hidden; })", js,
                      "已隐藏的图不许进横向轨道（滑到它就是卡死）")

    def test_badge_is_an_icon_not_a_number(self):
        js = _read(WS, "js", "gallery.js")
        self.assertNotIn('"已藏 " + work.hidden_n', js)
        self.assertIn("ICON_EYE_MINI", js)


class TestGalleryWeight(unittest.TestCase):
    """3. 网格封面不许再按 1MB 档拉。"""

    def test_grid_cover_uses_thumb(self):
        js = _read(WS, "js", "gallery.js")
        self.assertIn('"/api/gal/img/" + work.cover + "?s=t"', js)
        self.assertNotIn('work.cover + "?s=t&fit=1m"', js,
                         "网格封面带了 fit=1m：一屏多花 8 倍流量（用户实测 180k）")

    def test_tabs_never_rebuild(self):
        js = _read(WS, "js", "gallery.js")
        i = js.index("function fillTabs")
        seg = js[i:js.index("function markTabIntoView", i)]
        self.assertNotIn('el.tabs.innerHTML = ""', seg,
                         "整条重建会把横向滚动位置清零（手指按着就是卡死）")


class TestDownloadIsNotANavigation(unittest.TestCase):
    """4. 下载不许再用 `<a download href=接口地址>`：站长拿到的是 48 字节的
       错误响应体，群友碰巧没事。"""

    def test_download_fetches_bytes_first(self):
        js = _read(WS, "js", "hide.js")
        self.assertIn("function save(o)", js)
        self.assertIn('credentials: "same-origin"', js)
        self.assertIn("createObjectURL", js)
        # 画廊这边只传位置，不自己拼 a.href = 图片地址
        g = _read(WS, "js", "gallery.js")
        self.assertIn("WSHide.save", g)
        self.assertNotIn('a.href = imgUrl(it)', g)


if __name__ == "__main__":
    unittest.main()
