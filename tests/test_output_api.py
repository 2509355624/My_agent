"""output_api 排序测试：网格按文件名编号升序（自然序）。

原则：零网络、不碰真实 output。用临时目录 + 临时把模块级 COMFYUI_OUTPUT_DIR
指过去（`_root()` 读的就是这个模块变量），用例结束还原。
"""

import json
import os
import shutil
import tempfile
import unittest

from flask import Flask

from app import output_api


class NaturalKeyTest(unittest.TestCase):
    def test_numbers_compare_as_numbers(self):
        # 纯字典序下 '00010' < '0009'（'0' < '9'），出图顺序会被颠倒
        names = ["QwenImage_00010_.png", "QwenImage_00009_.png", "QwenImage_00002_.png"]
        got = sorted(names, key=output_api._natural_key)
        self.assertEqual(
            got,
            ["QwenImage_00002_.png", "QwenImage_00009_.png", "QwenImage_00010_.png"],
        )

    def test_text_and_number_segments_do_not_crash(self):
        # 同一位置一边数字一边文本，裸 int/str 混排会 TypeError
        names = ["abc_1.png", "abc_x.png", "abc_2.png"]
        self.assertEqual(
            sorted(names, key=output_api._natural_key),
            ["abc_1.png", "abc_2.png", "abc_x.png"],
        )

    def test_case_insensitive(self):
        names = ["B_2.png", "a_1.png"]
        self.assertEqual(sorted(names, key=output_api._natural_key), ["a_1.png", "B_2.png"])


class TreeOrderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="outapi_")
        self.old = output_api.COMFYUI_OUTPUT_DIR
        output_api.COMFYUI_OUTPUT_DIR = self.tmp
        self.addCleanup(self._restore)

        # 故意乱序创建，且 mtime 与编号相反，确保排序不是靠创建/改时间蒙对
        for i, name in enumerate(
            ["QwenImage_00005_.png", "QwenImage_00002_.png", "QwenImage_00010_.png",
             "QwenImage_00001_.png", "QwenImage_00003_.png"]
        ):
            p = os.path.join(self.tmp, name)
            with open(p, "wb") as f:
                f.write(b"x")
            os.utime(p, (1_700_000_000 + i, 1_700_000_000 + i))  # 越晚建越新

        app = Flask(__name__)
        app.register_blueprint(output_api.bp)
        self.client = app.test_client()

    def _restore(self):
        output_api.COMFYUI_OUTPUT_DIR = self.old
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_files_sorted_by_number_ascending(self):
        d = self.client.get("/api/output/tree?path=").get_json()
        self.assertTrue(d["ok"])
        self.assertEqual(
            [f["name"] for f in d["files"]],
            ["QwenImage_00001_.png", "QwenImage_00002_.png", "QwenImage_00003_.png",
             "QwenImage_00005_.png", "QwenImage_00010_.png"],
        )

    def test_dirs_sorted_naturally(self):
        for name in ["私聊_10", "私聊_2", "私聊_1"]:
            os.makedirs(os.path.join(self.tmp, name), exist_ok=True)
        d = self.client.get("/api/output/tree?path=").get_json()
        self.assertEqual([x["name"] for x in d["dirs"]], ["私聊_1", "私聊_2", "私聊_10"])


class TrashTest(unittest.TestCase):
    """删除 = 移入回收站（2026-10-30 用户要求）。

    之前是 `os.remove` / `shutil.rmtree`，图一删就没，误删无解。现在挪进
    `state/output_trash/` 并**保留原目录结构**，想恢复把那一层拖回 output 即可。

    ⚠️ 全程在临时目录里跑：`COMFYUI_OUTPUT_DIR` 和 `TRASH_ROOT` 都指到 tmp，
    否则这些用例会去动用户真实的 output。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="outapi_trash_")
        self.out = os.path.join(self.tmp, "output")
        self.trash = os.path.join(self.tmp, "trash")
        os.makedirs(self.out)
        os.makedirs(self.trash)

        self._old_out = output_api.COMFYUI_OUTPUT_DIR
        self._old_trash = output_api.TRASH_ROOT
        self._old_log = output_api._TRASH_LOG
        output_api.COMFYUI_OUTPUT_DIR = self.out
        output_api.TRASH_ROOT = self.trash
        output_api._TRASH_LOG = os.path.join(self.trash, "_deleted.jsonl")
        self.addCleanup(self._restore)

        app = Flask(__name__)
        app.register_blueprint(output_api.bp)
        self.client = app.test_client()

    def _restore(self):
        output_api.COMFYUI_OUTPUT_DIR = self._old_out
        output_api.TRASH_ROOT = self._old_trash
        output_api._TRASH_LOG = self._old_log
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, rel, data=b"x"):
        p = os.path.join(self.out, *rel.split("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(data)
        return p

    def test_file_goes_to_trash_keeping_its_folder(self):
        src = self._write("web/生图页/anima-line/a.png", b"1234")
        d = self.client.post("/api/output/delete",
                             json={"paths": ["web/生图页/anima-line/a.png"]}).get_json()
        self.assertTrue(d["ok"])
        self.assertEqual(d["moved"], ["web/生图页/anima-line/a.png"])
        self.assertEqual(d["moved_bytes"], 4)
        self.assertFalse(os.path.exists(src), "原位置必须已经空出来")
        self.assertTrue(os.path.exists(
            os.path.join(self.trash, "web", "生图页", "anima-line", "a.png")),
            "回收站里要能按原路径找到")

    def test_directory_moves_as_a_whole(self):
        self._write("web/miao/1.png")
        self._write("web/miao/sub/2.png")
        d = self.client.post("/api/output/delete", json={"paths": ["web/miao"]}).get_json()
        self.assertTrue(d["ok"])
        self.assertFalse(os.path.exists(os.path.join(self.out, "web", "miao")))
        self.assertTrue(os.path.exists(
            os.path.join(self.trash, "web", "miao", "sub", "2.png")))

    def test_same_name_never_overwrites(self):
        """回收站里已有同名项时加后缀。覆盖等于换个地方真删一次，白建。"""
        self._write("a.png", b"first")
        self.client.post("/api/output/delete", json={"paths": ["a.png"]})
        self._write("a.png", b"second")
        d = self.client.post("/api/output/delete", json={"paths": ["a.png"]}).get_json()
        self.assertTrue(d["ok"])
        base = os.path.join(self.trash, "a.png")
        with open(base, "rb") as f:
            self.assertEqual(f.read(), b"first")
        with open(os.path.join(self.trash, "a-1.png"), "rb") as f:
            self.assertEqual(f.read(), b"second")

    def test_path_escape_is_refused(self):
        """`..` 一律不认——回收站是真实可写目录，拼错一次就是往外写。"""
        outside = os.path.join(self.tmp, "outside.txt")
        with open(outside, "wb") as f:
            f.write(b"keep")
        d = self.client.post("/api/output/delete",
                             json={"paths": ["../outside.txt"]}).get_json()
        self.assertFalse(d["ok"])
        self.assertEqual(d["moved"], [])
        self.assertTrue(os.path.exists(outside))

    def test_missing_target_reports_without_crashing(self):
        d = self.client.post("/api/output/delete",
                             json={"paths": ["nope/gone.png"]}).get_json()
        self.assertFalse(d["ok"])
        self.assertEqual(len(d["failed"]), 1)

    def test_every_delete_leaves_a_log_line(self):
        self._write("b.png")
        self.client.post("/api/output/delete", json={"paths": ["b.png"]})
        with open(output_api._TRASH_LOG, encoding="utf-8") as f:
            lines = f.read().strip().split("\n")
        self.assertEqual(len(lines), 1)
        row = json.loads(lines[0])
        self.assertEqual(row["rel"], "b.png")
        self.assertEqual(row["trash"], "b.png")
        self.assertIn("ts", row)


if __name__ == "__main__":
    unittest.main()
