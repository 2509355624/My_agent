# -*- coding: utf-8 -*-
"""换种子重跑：旧图先删、新图占原编号、落图原子替换（2026-11-11 用户拍板）。

只验「磁盘上到底剩什么」，不调真模型、不起服务、不碰真实的 comics/ 目录。
背景：站主是**去文件夹里挑图发**的，所以目录里只要有一张上一版或半截 PNG
顶着格号，就会被误当成新图发出去；zip 那层过滤帮不上忙。
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import comic, comic_api, comic_portrait, main


def _touch(path, data=b"x"):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    return path


def _put(root, rel, data=b"x"):
    return _touch(os.path.join(root, *rel.split("/")), data)


def _exists(root, rel):
    return os.path.isfile(os.path.join(root, *rel.split("/")))


class DiscardNumberedImagesTest(unittest.TestCase):
    """删的范围必须锁死在「同一任务、同一编号」。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name

    def test_only_the_same_number_is_removed(self):
        old = {"raw/01_raw.png", "final/01_final.png",
               "thumb/01_raw_thumb.webp", "thumb/01_final_thumb.webp"}
        keep = {"raw/02_raw.png", "final/02_final.png",
                "thumb/02_final_thumb.webp", "script.md", "state.json"}
        for rel in old | keep:
            _put(self.dir, rel)
        removed = comic.discard_numbered_images(self.dir, "01", ("raw", "final"))
        self.assertEqual(set(removed), old, "删掉的清单要跟实际一致")
        for rel in old:
            self.assertFalse(_exists(self.dir, rel), "没删干净：%s" % rel)
        for rel in keep:
            self.assertTrue(_exists(self.dir, rel), "误删了：%s" % rel)

    def test_portrait_kind_only_touches_that_number(self):
        old = {"portraits/01.png", "thumb/01_portrait_thumb.webp"}
        keep = {"portraits/02.png", "raw/01_raw.png", "final/01_final.png"}
        for rel in old | keep:
            _put(self.dir, rel)
        comic.discard_numbered_images(self.dir, "01", ("portrait",))
        for rel in old:
            self.assertFalse(_exists(self.dir, rel), "没删干净：%s" % rel)
        for rel in keep:
            self.assertTrue(_exists(self.dir, rel), "写真重跑不该动漫画格：%s" % rel)

    def test_number_is_normalized(self):
        """前端可能给 1 / "1" / "01"，都得落到同一个编号上。"""
        for no in (1, "1", "01", " 1 "):
            fp = _put(self.dir, "raw/01_raw.png")
            comic.discard_numbered_images(self.dir, no, ("raw",))
            self.assertFalse(os.path.isfile(fp), "编号 %r 没归一化" % (no,))

    def test_missing_files_are_a_noop(self):
        self.assertEqual(comic.discard_numbered_images(self.dir, "09"), [])

    def test_delete_failure_only_warns(self):
        """权限问题不能把整格重跑拦死。"""
        _put(self.dir, "raw/01_raw.png")
        with mock.patch.object(comic.os, "remove", side_effect=OSError("denied")):
            comic.discard_numbered_images(self.dir, "01", ("raw",))


class PublishFileTest(unittest.TestCase):
    """落图必须走 .tmp + os.replace，半截 PNG 不许占着正式编号。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dst = os.path.join(self.tmp.name, "01_raw.png")

    def test_writes_via_tmp_then_replaces(self):
        _touch(self.dst, b"old")
        seen = []

        def write(tmp):
            seen.append(tmp)
            _touch(tmp, b"new")

        comic._publish_file(self.dst, write)
        with open(self.dst, "rb") as f:
            self.assertEqual(f.read(), b"new")
        self.assertEqual(seen, [self.dst + ".tmp"])
        self.assertFalse(os.path.isfile(self.dst + ".tmp"))

    def test_failure_keeps_the_old_file_and_cleans_tmp(self):
        _touch(self.dst, b"old")

        def write(tmp):
            _touch(tmp, b"half")
            raise RuntimeError("取图被掐断")

        with self.assertRaises(RuntimeError):
            comic._publish_file(self.dst, write)
        with open(self.dst, "rb") as f:
            self.assertEqual(f.read(), b"old")
        self.assertFalse(os.path.isfile(self.dst + ".tmp"))

    def test_empty_write_is_rejected(self):
        with self.assertRaises(RuntimeError):
            comic._publish_file(self.dst, lambda tmp: _touch(tmp, b""))
        self.assertFalse(os.path.isfile(self.dst))
        self.assertFalse(os.path.isfile(self.dst + ".tmp"))


class RerenderWiringTest(unittest.TestCase):
    """单格重跑：删图只在线程真启动之后发生，且必须在 run 之前。"""

    NAME = "C20261111-99"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, self.NAME)
        os.makedirs(self.path)
        with open(os.path.join(self.path, "script.md"), "w",
                  encoding="utf-8") as f:
            f.write("## 01\n画面: 1girl, solo\n气泡: 一\n")
        self.raw = _put(self.path, "raw/01_raw.png", b"old-raw")
        self.final = _put(self.path, "final/01_final.png", b"old-final")
        self.c = main.app.test_client()
        p = mock.patch.object(comic_api, "_COMICS_ROOT", self.tmp.name)
        p.start()
        self.addCleanup(p.stop)

    def _post(self):
        return self.c.post("/api/comic/rerender",
                           json={"task": self.NAME, "no": "01"})

    def test_discards_old_images_before_run(self):
        seen = []

        def _spawn(path, phase, fn, on_error=None, finish=None):
            fn()                       # 测试里同步跑，不起线程
            return True

        with mock.patch.object(comic_api, "_spawn", side_effect=_spawn), \
                mock.patch.object(comic, "run",
                                  side_effect=lambda *a, **k: seen.append(
                                      (os.path.isfile(self.raw),
                                       os.path.isfile(self.final)))):
            self.assertTrue(self._post().get_json()["ok"])
        self.assertEqual(seen, [(False, False)], "run 时旧图必须已经不在了")
        self.assertFalse(os.path.isfile(self.raw))
        self.assertFalse(os.path.isfile(self.final))

    def test_spawn_refusal_deletes_nothing(self):
        """同任务已在跑 → 409，不能出现「图删了却没重跑」。"""
        with mock.patch.object(comic_api, "_spawn", return_value=False):
            r = self._post()
        self.assertEqual(r.status_code, 409)
        self.assertTrue(os.path.isfile(self.raw))
        self.assertTrue(os.path.isfile(self.final))


class ReseedShotWiringTest(unittest.TestCase):
    """写真「🔁 换种子」同样先清编号，再重画。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_discard_happens_before_render(self):
        order = []
        with mock.patch.object(comic_portrait.comic, "_require_comfy"), \
                mock.patch.object(comic_portrait.comic, "_new_seed",
                                  return_value=7), \
                mock.patch.object(comic_portrait.comic,
                                  "discard_numbered_images",
                                  side_effect=lambda *a, **k: order.append(
                                      ("discard", a, k))), \
                mock.patch.object(comic_portrait, "load_prompts",
                                  return_value=[{"no": "01", "prompt": "x"}]), \
                mock.patch.object(comic_portrait, "save_prompts"), \
                mock.patch.object(comic_portrait, "_render_one",
                                  side_effect=lambda *a, **k: order.append(
                                      ("render", a, k)) or
                                  {"no": "01", "state": "done"}):
            comic_portrait.reseed_shot(self.tmp.name, "01")
        self.assertEqual([x[0] for x in order], ["discard", "render"])
        self.assertEqual(order[0][1][1], "01")
        self.assertEqual(order[0][1][2], ("portrait",))


if __name__ == "__main__":
    unittest.main()
