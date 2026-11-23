# -*- coding: utf-8 -*-
"""参考图链路的**真跑**测试：上传 → 取图 id → 读图反推 → 拼进生图提示词。

跟同批的 round3 不一样——那边查代码形状，这边真发 HTTP 请求、真读数据库。
用户这轮的火气就来自「参考图上传了却报找不到它」，所以这条链必须真跑过。
识图那一步打桩（不烧额度），其余全是真的。
"""
import io
import os
import shutil
import tempfile
import unittest
from unittest import mock

from PIL import Image

import app.main as main
from app.features.gallery import gallery, routes as gal_routes


def _png():
    b = io.BytesIO()
    Image.new("RGB", (400, 600), (200, 120, 90)).save(b, "PNG")
    return b.getvalue()


class TestRefPipeline(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="refpipe_")
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        # 落盘与库都指向临时目录，绝不碰 station 的真实画廊
        for name, val in (("UPLOAD", os.path.join(self.tmp, "up")),
                          ("DB", os.path.join(self.tmp, "g.db"))):
            p = mock.patch.object(gallery, name, val)
            p.start()
            self.addCleanup(p.stop)
        # 测试客户端不带内网地址，_guard 会以为自己是公网访客 → 403
        p = mock.patch.object(gal_routes, "_local_trusted", return_value=True)
        p.start()
        self.addCleanup(p.stop)
        gallery.init()          # 临时库里建表，否则第一次查询就 OperationalError
        self.c = main.app.test_client()

    def _upload(self):
        r = self.c.post("/api/gal/upload/one",
                        data={"file": (io.BytesIO(_png()), "t.png")},
                        content_type="multipart/form-data")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return r.get_json()

    def test_upload_returns_item_id(self):
        """上传要直接给出 item_id —— 这是「没找到她」那条的修复点。"""
        d = self._upload()
        self.assertTrue(d["ok"])
        self.assertGreater(d.get("item_id", 0), 0, "必须带上这张图的 id")
        # 库里按 0 基存，返回的 idx 是第几张：两者都成立，id 必须指得对
        j = self.c.get("/api/gal/work/%s" % d["work_id"]).get_json()
        self.assertTrue(j["ok"])
        self.assertIsInstance(j.get("items"), list, "items 必须是顶层字段")
        ids = [it["id"] for it in j["items"]]
        self.assertIn(d["item_id"], ids, "item_id 必须真能在作品集里找到")
        stored = [it["idx"] for it in j["items"] if it["id"] == d["item_id"]][0]
        self.assertEqual(stored, d["idx"] - 1, "库里 idx 是 0 基")

    def test_describe_returns_text(self):
        d = self._upload()
        iid = d["item_id"]
        with mock.patch("app.integrations.vision.describe",
                        return_value="橙色立方体，动漫厚涂，暖色侧光") as m:
            r = self.c.get("/api/gal/describe?id=%d" % iid)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        got = r.get_json()
        self.assertTrue(got["ok"])
        self.assertIn("动漫厚涂", got["text"])
        self.assertTrue(m.called, "必须真的调了识图")
        # 用的是反推提示词，不是通用识图那句
        self.assertIn("提示词", m.call_args.kwargs.get("prompt", ""))

    def test_describe_unknown_id_is_502_not_500(self):
        r = self.c.get("/api/gal/describe?id=999999")
        self.assertEqual(r.status_code, 502)
        self.assertFalse(r.get_json()["ok"])

    def test_describe_failure_degrades(self):
        """识图全挂时给 502 + 一句能懂的话，不能 500 堆栈。"""
        d = self._upload()
        iid = d["item_id"]
        with mock.patch("app.integrations.vision.describe",
                        side_effect=RuntimeError("额度没了")):
            r = self.c.get("/api/gal/describe?id=%d" % iid)
        self.assertEqual(r.status_code, 502)
        self.assertIn("没读出来", r.get_json()["error"])


class TestSubmitBakesRefs(unittest.TestCase):
    """提交生图时参考图要拼进提示词——不真跑 GPU，只验到建 job 为止。"""

    def setUp(self):
        from app.features.image import routes as ir
        self.ir = ir

    def test_ref_ids_filters_junk(self):
        ids, _ = self.ir._ref_ids(["3", "abc", 0, -1, "3", 4.7])
        self.assertEqual(ids, [3], "只留正整数且去重")

    def test_ref_ids_caps_count(self):
        ids, _ = self.ir._ref_ids(list(range(1, 99)))
        self.assertLessEqual(len(ids), self.ir.MAX_REFS)

    def test_ref_ids_ignores_non_list(self):
        self.assertEqual(self.ir._ref_ids("12"), ([], ""))
        self.assertEqual(self.ir._ref_ids(None), ([], ""))

    def test_describe_refs_numbers_multiple(self):
        with mock.patch("app.features.gallery.routes._read_image_text",
                        side_effect=lambda i, p: "图%d" % i):
            out = self.ir._describe_refs([11, 12])
        self.assertIn("参考图1", out)
        self.assertIn("参考图2", out)
        self.assertIn("图11", out)

    def test_describe_refs_empty_when_all_fail(self):
        with mock.patch("app.features.gallery.routes._read_image_text",
                        return_value=""):
            self.assertEqual(self.ir._describe_refs([11, 12]), "")


if __name__ == "__main__":
    unittest.main()
