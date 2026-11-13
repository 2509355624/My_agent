# -*- coding: utf-8 -*-
"""runbench_api（随心配预设存储）+ anima_random save_last/load_last 单测。

只测纯读写逻辑，零网络、不碰 ComfyUI。state_path 在测试进程自动切到临时目录。
"""
import os
import unittest

from app import anima_random, runbench_api


class SaveLastTest(unittest.TestCase):
    """anima_random 最近一次配方落盘/读取。"""

    def test_save_and_load_roundtrip(self):
        d = {"models": ["m1.safetensors", "m2.safetensors"],
             "loras": ["l1.safetensors", "l2.safetensors"]}
        anima_random.save_last(d, seed=42)
        got = anima_random.load_last()
        self.assertIsNotNone(got)
        self.assertEqual(got["models"], ["m1.safetensors", "m2.safetensors"])
        self.assertEqual(got["loras"], ["l1.safetensors", "l2.safetensors"])
        self.assertEqual(got["seed"], 42)

    def test_load_when_empty_returns_none(self):
        # 测试进程临时目录里没有文件 → None
        got = anima_random.load_last()
        self.assertIsNone(got)


class RunbenchApiTest(unittest.TestCase):
    """预设 CRUD：存/取/列/删 + 同名覆盖 + 名字校验。"""

    def setUp(self):
        # 清空预设库（测试进程共享同一份临时 state 文件，逐用例隔离）
        for name in runbench_api.list_names():
            runbench_api.delete(name)

    def test_save_and_get(self):
        name, err = runbench_api.save(
            "可爱画风", ["m1.safetensors", "m2.safetensors"],
            ["l1.safetensors"] * 5, seed=7)
        self.assertEqual(err, "")
        rec = runbench_api.get("可爱画风")
        self.assertIsNotNone(rec)
        self.assertEqual(rec["models"], ["m1.safetensors", "m2.safetensors"])
        self.assertEqual(len(rec["loras"]), 5)
        self.assertEqual(rec["seed"], 7)

    def test_same_name_overwrites(self):
        runbench_api.save("a", ["m1"], ["l1"] * 5, seed=1)
        runbench_api.save("a", ["m2"], ["l2"] * 5, seed=2)
        rec = runbench_api.get("a")
        self.assertEqual(rec["models"], ["m2"])
        self.assertEqual(rec["seed"], 2)

    def test_name_rejects_space(self):
        _, err = runbench_api.save("可爱 画风", ["m1"], ["l1"] * 5)
        self.assertNotEqual(err, "")

    def test_name_rejects_symbol(self):
        _, err = runbench_api.save("可爱-画风", ["m1"], ["l1"] * 5)
        self.assertNotEqual(err, "")

    def test_empty_recipe_rejected(self):
        _, err = runbench_api.save("空", [], [])
        self.assertNotEqual(err, "")

    def test_delete(self):
        runbench_api.save("b", ["m1"], ["l1"] * 5)
        self.assertTrue(runbench_api.delete("b"))
        self.assertIsNone(runbench_api.get("b"))
        self.assertFalse(runbench_api.delete("b"))  # 再删一次 False

    def test_list_names(self):
        runbench_api.save("一", ["m1"], ["l1"] * 5)
        runbench_api.save("二", ["m2"], ["l2"] * 5)
        names = runbench_api.list_names()
        self.assertEqual(set(names), {"一", "二"})

    def test_resolve(self):
        runbench_api.save("可爱画风", ["m1"], ["l1"] * 5)
        name, err = runbench_api.resolve("可爱画风")
        self.assertEqual(err, "")
        self.assertEqual(name, "可爱画风")
        _, err2 = runbench_api.resolve("不存在的")
        self.assertNotEqual(err2, "")


class CoverTest(unittest.TestCase):
    """封面：save_cover / has_cover / delete_cover / cover_path。"""

    def setUp(self):
        import os
        import tempfile
        self._tmp = tempfile.mkdtemp()
        # 造一张来源图（随便写点字节，save_cover 只复制不校验内容）
        self._src = os.path.join(self._tmp, "src.png")
        with open(self._src, "wb") as f:
            f.write(b"\x89PNG fake-bytes-for-test")
        for name in runbench_api.list_names():
            runbench_api.delete(name)

    def test_save_and_has_cover(self):
        ok, err = runbench_api.save_cover("封面预设", self._src)
        self.assertTrue(ok, err)
        self.assertTrue(runbench_api.has_cover("封面预设"))
        self.assertTrue(os.path.isfile(runbench_api.cover_path("封面预设")))

    def test_save_rejects_missing_src(self):
        ok, err = runbench_api.save_cover("x", "/nonexistent/no.png")
        self.assertFalse(ok)
        self.assertNotEqual(err, "")

    def test_save_rejects_empty_name(self):
        ok, err = runbench_api.save_cover("", self._src)
        self.assertFalse(ok)
        self.assertNotEqual(err, "")

    def test_channel_id_with_dash_allowed(self):
        # 官方渠道 id 带 `-`（如 anima-2s），封面 key 允许它（不走预设名校验）
        ok, err = runbench_api.save_cover("anima-2s", self._src)
        self.assertTrue(ok, err)
        self.assertTrue(runbench_api.has_cover("anima-2s"))

    def test_delete_cover_idempotent(self):
        runbench_api.save_cover("y", self._src)
        self.assertTrue(runbench_api.delete_cover("y"))
        self.assertFalse(runbench_api.has_cover("y"))
        self.assertTrue(runbench_api.delete_cover("y"))  # 幂等

    def test_ensure_thumb_makes_small_jpeg(self):
        # 真图（用 PIL 造一张 800×1200 的图），ensure_thumb 压成长边 480 的 JPEG
        from PIL import Image
        big = os.path.join(self._tmp, "big.png")
        Image.new("RGB", (800, 1200), (200, 60, 90)).save(big)
        runbench_api.save_cover("缩略", big)
        thumb = runbench_api.ensure_thumb("缩略")
        self.assertTrue(thumb)
        self.assertTrue(os.path.isfile(thumb))
        with Image.open(thumb) as im:
            self.assertEqual(max(im.size), runbench_api._THUMB_LONG)
            self.assertEqual(im.format, "JPEG")

    def test_ensure_thumb_no_cover_returns_empty(self):
        self.assertEqual(runbench_api.ensure_thumb("不存在"), "")

    def test_ensure_thumb_reuses_existing(self):
        # 第二次调用复用已生成的缩略图，不重压（mtime 不早于原图）
        from PIL import Image
        big = os.path.join(self._tmp, "big2.png")
        Image.new("RGB", (600, 900), (10, 100, 200)).save(big)
        runbench_api.save_cover("复用", big)
        first = runbench_api.ensure_thumb("复用")
        mtime1 = os.path.getmtime(first)
        second = runbench_api.ensure_thumb("复用")
        self.assertEqual(first, second)
        self.assertEqual(os.path.getmtime(second), mtime1)


class RunbenchTokenTest(unittest.TestCase):
    """friend_auth 跑图台令牌：签名域独立、roundtrip、过期。"""

    def test_roundtrip(self):
        from app import friend_auth
        k = friend_auth.make_runbench_token("private", "123456789")
        self.assertTrue(k)
        parsed = friend_auth.parse_runbench_token(k)
        self.assertEqual(parsed, ("private", "123456789"))

    def test_group_token(self):
        from app import friend_auth
        k = friend_auth.make_runbench_token("group", "987654321")
        parsed = friend_auth.parse_runbench_token(k)
        self.assertEqual(parsed, ("group", "987654321"))

    def test_domain_isolation_from_outfit(self):
        # 跑图台令牌不能被换装 parse 认（签名域不同），反之亦然
        from app import friend_auth
        rk = friend_auth.make_runbench_token("private", "123456789")
        ok = friend_auth.make_outfit_token("private", "123456789")
        self.assertIsNone(friend_auth.parse_outfit_token(rk))
        self.assertIsNone(friend_auth.parse_runbench_token(ok))

    def test_invalid_target_rejected(self):
        from app import friend_auth
        self.assertEqual(friend_auth.make_runbench_token("bot", "123"), "")


if __name__ == "__main__":
    unittest.main()
