# -*- coding: utf-8 -*-
"""图片迁移（`app/image_migrate.py`）的单元测试。

全是**临时目录**上的真实文件操作，不碰 ComfyUI、不碰 state/ 真配置
（`state_path()` 在 unittest 进程里自动导向临时目录，见 app/config.py）。

重点锁三类东西：
1. **路径穿越**：出图接口的名字来自 URL 查询串，`safe_name` 必须把
   `../x`、`a/b`、`a\\b`、`C:foo` 全挡掉。这是这个功能唯一的安全边界。
2. **同名跳过**：目标里已有同名文件时**一个字节都不许写**（用户拍板）。
   「迁没迁过」是从目标目录推导的，所以这条一旦坏了会静默覆盖用户的文件。
3. **不递归**：只扫顶层。递归会把 ComfyUI 的 temp/ 之类也卷进来。
"""

import os
import shutil
import tempfile
import unittest

from app import image_migrate as im


def _write(path, data=b"x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


class SafeNameTest(unittest.TestCase):
    """出图接口的闸门。"""

    def test_accepts_plain_filenames(self):
        for n in ("a.png", "Anima_00479_.png", "带 空格 的名字.JPG", "a.b.c.webp"):
            self.assertEqual(im.safe_name(n), n, n)

    def test_rejects_any_path_component(self):
        for n in ("../a.png", "..\\a.png", "sub/a.png", "sub\\a.png",
                  "/etc/passwd", "C:\\Windows\\a.png", "C:a.png",
                  ".", "..", "", "   ", "a\x00b"):
            self.assertIsNone(im.safe_name(n), n)

    def test_rejects_non_strings(self):
        for n in (None, 5, [], {}):
            self.assertIsNone(im.safe_name(n), n)


class NormalizeDirTest(unittest.TestCase):

    def test_strips_quotes_and_spaces(self):
        """从资源管理器「复制文件地址」粘过来是带引号的。"""
        self.assertEqual(im.normalize_dir('  "D:\\a\\b"  '),
                         os.path.abspath("D:\\a\\b"))
        self.assertEqual(im.normalize_dir("'D:\\a\\b'"),
                         os.path.abspath("D:\\a\\b"))

    def test_empty_is_none(self):
        for v in ("", "   ", None, 5):
            self.assertIsNone(im.normalize_dir(v), v)

    def test_returns_absolute(self):
        p = im.normalize_dir("a/../b")
        self.assertTrue(os.path.isabs(p))
        self.assertTrue(p.endswith(os.sep + "b"), p)


class ListImagesTest(unittest.TestCase):

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="mig_list_")
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)

    def test_only_images_top_level_sorted(self):
        _write(os.path.join(self.d, "b.PNG"))
        _write(os.path.join(self.d, "a.jpg"))
        _write(os.path.join(self.d, "c.webp"))
        _write(os.path.join(self.d, "notes.txt"))          # 非图片
        _write(os.path.join(self.d, "movie.mp4"))          # 非图片
        _write(os.path.join(self.d, "sub", "deep.png"))    # 子目录里的，不扫

        self.assertEqual(im.list_images(self.d), ["a.jpg", "b.PNG", "c.webp"])

    def test_directory_named_like_an_image_is_skipped(self):
        os.makedirs(os.path.join(self.d, "fake.png"))
        self.assertEqual(im.list_images(self.d), [])

    def test_missing_dir_returns_empty(self):
        self.assertEqual(im.list_images(os.path.join(self.d, "nope")), [])

    def test_empty_dir(self):
        self.assertEqual(im.list_images(self.d), [])


class ScanTest(unittest.TestCase):

    def setUp(self):
        self.src = tempfile.mkdtemp(prefix="mig_src_")
        self.dst = tempfile.mkdtemp(prefix="mig_dst_")
        self.addCleanup(shutil.rmtree, self.src, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.dst, ignore_errors=True)
        for i in range(1, 8):
            _write(os.path.join(self.src, "img_%02d.png" % i), b"0" * 10)

    def test_total_and_first_page(self):
        d = im.scan(self.src, self.dst, 0, 3)
        self.assertTrue(d["src_ok"])
        self.assertTrue(d["dst_ok"])
        self.assertEqual(d["total"], 7)
        self.assertEqual(d["offset"], 0)
        self.assertEqual([it["name"] for it in d["items"]],
                         ["img_01.png", "img_02.png", "img_03.png"])

    def test_second_page(self):
        d = im.scan(self.src, self.dst, 3, 3)
        self.assertEqual([it["name"] for it in d["items"]],
                         ["img_04.png", "img_05.png", "img_06.png"])

    def test_last_page_is_short(self):
        d = im.scan(self.src, self.dst, 6, 3)
        self.assertEqual([it["name"] for it in d["items"]], ["img_07.png"])

    def test_offset_is_clamped_into_range(self):
        """翻过头 / 负数都要夹回来，不能返回空列表让人以为没图了。"""
        self.assertEqual(im.scan(self.src, self.dst, 999, 3)["offset"], 6)
        self.assertEqual(im.scan(self.src, self.dst, -5, 3)["offset"], 0)

    def test_item_has_size_and_mtime(self):
        it = im.scan(self.src, self.dst, 0, 1)["items"][0]
        self.assertEqual(it["size"], 10)
        self.assertGreater(it["mtime"], 0)

    def test_migrated_flag_tracks_dst(self):
        _write(os.path.join(self.dst, "img_02.png"), b"old")
        items = {it["name"]: it["migrated"] for it in im.scan(self.src, self.dst, 0, 7)["items"]}
        self.assertFalse(items["img_01.png"])
        self.assertTrue(items["img_02.png"])

    def test_missing_src_reports_error_not_crash(self):
        d = im.scan(os.path.join(self.src, "nope"), self.dst, 0, 3)
        self.assertFalse(d["src_ok"])
        self.assertIn("不存在", d["error"])
        self.assertEqual(d["items"], [])

    def test_empty_src_has_no_error_but_no_items(self):
        empty = tempfile.mkdtemp(prefix="mig_empty_")
        self.addCleanup(shutil.rmtree, empty, ignore_errors=True)
        d = im.scan(empty, self.dst, 0, 3)
        self.assertTrue(d["src_ok"])
        self.assertEqual(d["total"], 0)
        self.assertEqual(d["offset"], 0)
        self.assertNotIn("error", d)

    def test_bad_dst_is_flagged_but_listing_still_works(self):
        d = im.scan(self.src, os.path.join(self.dst, "nope"), 0, 3)
        self.assertFalse(d["dst_ok"])
        self.assertEqual(d["total"], 7)
        self.assertFalse(any(it["migrated"] for it in d["items"]))

    def test_limit_is_clamped(self):
        self.assertEqual(im.scan(self.src, self.dst, 0, 999)["limit"], im.MAX_BATCH)
        self.assertEqual(im.scan(self.src, self.dst, 0, 0)["limit"], im.MIN_BATCH)
        self.assertEqual(im.scan(self.src, self.dst, 0, "abc")["limit"], im.DEFAULT_BATCH)


class MigrateTest(unittest.TestCase):

    def setUp(self):
        self.src = tempfile.mkdtemp(prefix="mig_src_")
        self.dst = tempfile.mkdtemp(prefix="mig_dst_")
        self.addCleanup(shutil.rmtree, self.src, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.dst, ignore_errors=True)
        self.name = "pic.png"
        _write(os.path.join(self.src, self.name), b"original-bytes")

    def _dst(self):
        return os.path.join(self.dst, self.name)

    def test_copies_and_keeps_source(self):
        r = im.migrate(self.src, self.name, self.dst)
        self.assertTrue(r["ok"])
        self.assertFalse(r["skipped"])
        self.assertTrue(os.path.isfile(self._dst()))
        with open(self._dst(), "rb") as f:
            self.assertEqual(f.read(), b"original-bytes")
        # 源文件必须还在（用户要的是「复制」不是「移动」）
        self.assertTrue(os.path.isfile(os.path.join(self.src, self.name)))

    def test_same_name_is_skipped_and_never_overwritten(self):
        _write(self._dst(), b"DO-NOT-TOUCH")
        r = im.migrate(self.src, self.name, self.dst)
        self.assertTrue(r["ok"])
        self.assertTrue(r["skipped"])
        with open(self._dst(), "rb") as f:
            self.assertEqual(f.read(), b"DO-NOT-TOUCH")

    def test_missing_source(self):
        r = im.migrate(self.src, "nope.png", self.dst)
        self.assertFalse(r["ok"])
        self.assertIn("源文件", r["error"])

    def test_missing_dst_dir_is_an_error_not_an_auto_create(self):
        """故意**不自动建目录** —— 路径打错一个字就凭空多一个空文件夹更糟。"""
        ghost = os.path.join(self.dst, "nope")
        r = im.migrate(self.src, self.name, ghost)
        self.assertFalse(r["ok"])
        self.assertIn("不存在", r["error"])
        self.assertFalse(os.path.isdir(ghost))

    def test_empty_dst(self):
        r = im.migrate(self.src, self.name, "")
        self.assertFalse(r["ok"])
        self.assertIn("输出文件夹", r["error"])

    def test_traversal_name_is_rejected(self):
        for bad in ("../pic.png", "..\\pic.png", "sub/pic.png"):
            r = im.migrate(self.src, bad, self.dst)
            self.assertFalse(r["ok"], bad)

    def test_copies_a_file_that_is_not_an_image(self):
        """迁移本身不挑扩展名 —— 列表只列图片，但手动点名别的文件也该能复制。"""
        _write(os.path.join(self.src, "readme.txt"), b"hi")
        r = im.migrate(self.src, "readme.txt", self.dst)
        self.assertTrue(r["ok"])
        self.assertTrue(os.path.isfile(os.path.join(self.dst, "readme.txt")))


class ClampBatchTest(unittest.TestCase):

    def test_bounds(self):
        self.assertEqual(im.clamp_batch(3), 3)
        self.assertEqual(im.clamp_batch(0), im.MIN_BATCH)
        self.assertEqual(im.clamp_batch(-9), im.MIN_BATCH)
        self.assertEqual(im.clamp_batch(10 ** 6), im.MAX_BATCH)

    def test_garbage_falls_back_to_default(self):
        for v in (None, "", "abc", [], {}):
            self.assertEqual(im.clamp_batch(v), im.DEFAULT_BATCH, v)

    def test_numeric_string_is_accepted(self):
        """界面上 <input> 送过来是字符串。"""
        self.assertEqual(im.clamp_batch("7"), 7)


class ConfigTest(unittest.TestCase):
    """配置落在 state_path() 里 —— 测试进程自动隔离到临时目录。"""

    def test_roundtrip(self):
        im.save_config({"src": "D:\\a", "dst": "D:\\b", "batch": 5})
        c = im.load_config()
        self.assertEqual(c["src"], "D:\\a")
        self.assertEqual(c["dst"], "D:\\b")
        self.assertEqual(c["batch"], 5)

    def test_partial_update_keeps_the_rest(self):
        im.save_config({"src": "D:\\a", "dst": "D:\\b", "batch": 5})
        im.save_config({"dst": "D:\\c"})
        c = im.load_config()
        self.assertEqual(c["src"], "D:\\a")
        self.assertEqual(c["dst"], "D:\\c")
        self.assertEqual(c["batch"], 5)

    def test_unknown_keys_are_ignored(self):
        im.save_config({"src": "D:\\a", "evil": "x", "dst": 5})
        c = im.load_config()
        self.assertEqual(c["src"], "D:\\a")
        self.assertNotIn("evil", c)
        self.assertIsInstance(c["dst"], str)     # 非字符串的 dst 被忽略

    def test_batch_is_clamped_on_save(self):
        im.save_config({"batch": 999})
        self.assertEqual(im.load_config()["batch"], im.MAX_BATCH)

    def test_corrupt_file_falls_back_to_defaults(self):
        p = im.state_path(im.CONFIG_FILE)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write("{ this is not json")
        c = im.load_config()
        self.assertEqual(c["batch"], im.DEFAULT_BATCH)
        self.assertIsInstance(c["src"], str)

    def test_json_that_is_not_an_object_is_tolerated(self):
        p = im.state_path(im.CONFIG_FILE)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write("[1, 2, 3]")
        self.assertEqual(im.load_config()["batch"], im.DEFAULT_BATCH)


class ThumbnailTest(unittest.TestCase):

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="mig_thumb_")
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)

    def test_makes_a_smaller_jpeg(self):
        from PIL import Image
        p = os.path.join(self.d, "big.png")
        Image.new("RGB", (1200, 1600), (200, 30, 30)).save(p)

        data = im.thumbnail_bytes(p, max_side=300)
        self.assertIsNotNone(data)
        self.assertTrue(data.startswith(b"\xff\xd8"), "应该是 JPEG（SOI 标记）")

        out = os.path.join(self.d, "thumb.jpg")
        with open(out, "wb") as f:
            f.write(data)
        with Image.open(out) as t:
            self.assertEqual(max(t.size), 300)

    def test_png_with_alpha_is_flattened(self):
        from PIL import Image
        p = os.path.join(self.d, "alpha.png")
        Image.new("RGBA", (200, 200), (0, 0, 0, 0)).save(p)
        self.assertIsNotNone(im.thumbnail_bytes(p, max_side=100))

    def test_non_image_returns_none(self):
        p = os.path.join(self.d, "x.txt")
        with open(p, "w") as f:
            f.write("not an image")
        self.assertIsNone(im.thumbnail_bytes(p))

    def test_missing_file_returns_none(self):
        self.assertIsNone(im.thumbnail_bytes(os.path.join(self.d, "nope.png")))


if __name__ == "__main__":
    unittest.main()
