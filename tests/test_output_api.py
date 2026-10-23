"""output_api 排序测试：网格按文件名编号升序（自然序）。

原则：零网络、不碰真实 output。用临时目录 + 临时把模块级 COMFYUI_OUTPUT_DIR
指过去（`_root()` 读的就是这个模块变量），用例结束还原。
"""

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


if __name__ == "__main__":
    unittest.main()
