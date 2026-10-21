# -*- coding: utf-8 -*-
"""moss 漫画台 API 测试（2026-10-21 加）。

钉死四件事：

  1. **任务名只认 `state/moss_comic` 的直接子目录**。`os.path.basename("..")`
     还是 `..`，直接 join 就跑出根目录了——这是这个端点唯一的攻击面。
  2. **spec 落盘 → 读回来不掉字段**，提示词跟着重算（页面靠它做预览）。
  3. **老任务兜底**：CLI 时代跑的那几篇没有 state.json，得靠出图目录把图找回来，
     否则页面打开全是空白。
  4. **提示词拼装**：`exactly N` 由脚本出、方位名对得上、气泡进提示词、
     人物外形同一页只展开一次（后面用 `the girl`）。
"""
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask                                # noqa: E402

from app import moss_api                               # noqa: E402


def _spec(layout="2x2", n=9):
    return {
        "title": "测试",
        "style": "anime style, clean line art, flat cel shading, soft light, calm mood",
        "cast": {"girl": "a girl with long black hair, a white shirt"},
        "pages": [{
            "n": n, "layout": layout, "scene": "a quiet room at night",
            "panels": [
                {"desc": "{girl} sits by the window in a medium shot at eye level",
                 "bubble": "晚安", "sfx": "", "caption": ""},
                {"desc": "{girl} pulls the curtain, a close-up of her hand",
                 "bubble": "", "sfx": "哗", "caption": ""},
                {"desc": "the room seen from the doorway, one lamp still on",
                 "bubble": "", "sfx": "", "caption": ""},
                {"desc": "a wide shot of the street outside, snow falling",
                 "bubble": "", "sfx": "", "caption": ""},
            ]}],
    }


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="moss_api_")
        self.state = os.path.join(self.tmp, "state")
        os.makedirs(self.state)
        self._old_state = moss_api.STATE_ROOT
        self._old_out = moss_api.COMFYUI_OUTPUT_DIR
        moss_api.STATE_ROOT = self.state
        moss_api.COMFYUI_OUTPUT_DIR = os.path.join(self.tmp, "output")
        os.makedirs(moss_api.COMFYUI_OUTPUT_DIR)

        app = Flask(__name__)
        app.register_blueprint(moss_api.bp)
        self.c = app.test_client()

    def tearDown(self):
        moss_api.STATE_ROOT = self._old_state
        moss_api.COMFYUI_OUTPUT_DIR = self._old_out
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _mk_task(self, name="20261021_1200_测试"):
        d = os.path.join(self.state, name)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "spec.json"), "w", encoding="utf-8") as fh:
            json.dump(_spec(n=1), fh, ensure_ascii=False)
        return name, d


class TaskDirTest(_Base):
    """任务名解析：只认直接子目录。"""

    def test_rejects_traversal(self):
        for bad in ("..", "../..", "a/../../b", "", ".", "不存在"):
            with self.assertRaises(ValueError, msg=bad):
                moss_api._task_dir(bad)

    def test_rejects_absolute_outside(self):
        outside = os.path.join(self.tmp, "outside")
        os.makedirs(outside)
        with self.assertRaises(ValueError):
            moss_api._task_dir(outside)

    def test_accepts_direct_child(self):
        name, d = self._mk_task()
        self.assertEqual(moss_api._task_dir(name), os.path.realpath(d))

    def test_status_404_on_bad_task(self):
        r = self.c.get("/api/moss/status?task=../..")
        self.assertEqual(r.status_code, 404)
        self.assertFalse(r.get_json()["ok"])


class SaveTest(_Base):
    """落盘 → 读回：字段不丢、页号重编、提示词重算。"""

    def test_save_roundtrip(self):
        name, d = self._mk_task()
        r = self.c.post("/api/moss/save", json={"task": name, "spec": _spec(n=9)})
        self.assertEqual(r.status_code, 200)
        out = r.get_json()
        self.assertTrue(out["ok"])
        # 页号被重编成 1
        self.assertEqual(out["pages"][0]["n"], 1)
        self.assertEqual(out["title"], "测试")

        with open(os.path.join(d, "spec.json"), encoding="utf-8") as fh:
            saved = json.load(fh)
        self.assertEqual(saved["pages"][0]["n"], 1)
        self.assertEqual(saved["pages"][0]["panels"][0]["bubble"], "晚安")
        self.assertTrue(os.path.isfile(os.path.join(d, "prompts.txt")))

    def test_prompt_shape(self):
        name, _ = self._mk_task()
        out = self.c.post("/api/moss/save",
                          json={"task": name, "spec": _spec()}).get_json()
        p = out["pages"][0]["prompt"]
        self.assertIn("exactly four panels", p)
        self.assertIn("Top left panel:", p)
        self.assertIn("Bottom right panel:", p)
        self.assertIn("「晚安」", p)          # 气泡进了提示词
        self.assertIn("「哗」", p)            # 拟声也进了
        # 外形串同一页只展开一次，第二次用指代
        self.assertEqual(p.count("a girl with long black hair"), 1)
        self.assertIn("the girl", p)
        # 场景串在
        self.assertIn("a quiet room at night", p)

    def test_save_normalizes_whitespace(self):
        name, _ = self._mk_task()
        spec = _spec()
        spec["title"] = "   有空格  "
        spec["cast"]["girl"] = "  a girl with long black hair  "
        spec["pages"][0]["panels"][0]["bubble"] = "  晚安  "
        out = self.c.post("/api/moss/save", json={"task": name, "spec": spec}).get_json()
        self.assertEqual(out["title"], "有空格")
        self.assertEqual(out["cast"]["girl"], "a girl with long black hair")
        self.assertIn("「晚安」", out["pages"][0]["prompt"])

    def test_layout_mismatch_reported_not_blocked(self):
        """格数不够 → 体检里报，但保存照样成功（判据只报不拦）。"""
        name, _ = self._mk_task()
        spec = _spec()
        spec["pages"][0]["panels"] = spec["pages"][0]["panels"][:2]
        out = self.c.post("/api/moss/save", json={"task": name, "spec": spec}).get_json()
        self.assertTrue(out["ok"])
        self.assertTrue(any("要 4 格" in x for x in out["issues"]))


class LegacyTest(_Base):
    """CLI 时代的老任务没有 state.json，靠出图目录兜底。"""

    def test_legacy_image_found(self):
        name, _ = self._mk_task("20261021_1200_旧任务")
        folder = os.path.join(moss_api.COMFYUI_OUTPUT_DIR, "web", "moss漫画",
                              name, "moss50")
        os.makedirs(folder)
        with open(os.path.join(folder, "MossComic_旧任务_P01_00001_.png"), "wb") as fh:
            fh.write(b"png")

        out = self.c.get("/api/moss/status?task=" + name).get_json()
        self.assertEqual(out["pages"][0]["status"], "done")
        self.assertTrue(out["pages"][0]["url"])
        self.assertEqual(out["pages"][0]["seed"], None)   # 老任务没记种子

        r = self.c.get("/api/moss/img?task=%s&n=1" % name)
        self.assertEqual(r.status_code, 200)

    def test_no_image_is_404(self):
        name, _ = self._mk_task()
        r = self.c.get("/api/moss/img?task=%s&n=1" % name)
        self.assertEqual(r.status_code, 404)


class ListTest(_Base):
    def test_empty(self):
        out = self.c.get("/api/moss/list").get_json()
        self.assertTrue(out["ok"])
        self.assertEqual(out["items"], [])

    def test_lists_tasks_newest_first(self):
        n1, _ = self._mk_task("20261021_1100_甲")
        n2, _ = self._mk_task("20261021_1200_乙")
        # 让甲比乙新
        os.utime(os.path.join(self.state, n1, "spec.json"), (9e9, 9e9))
        out = self.c.get("/api/moss/list").get_json()
        self.assertEqual([x["task"] for x in out["items"]], [n1, n2])


class RenderRejectTest(_Base):
    def test_render_unknown_task(self):
        r = self.c.post("/api/moss/render", json={"task": "没有这个任务"})
        self.assertEqual(r.status_code, 404)

    def test_render_empty_spec(self):
        name, d = self._mk_task()
        with open(os.path.join(d, "spec.json"), "w", encoding="utf-8") as fh:
            json.dump({"title": "空", "pages": []}, fh)
        r = self.c.post("/api/moss/render", json={"task": name})
        self.assertEqual(r.status_code, 400)


if __name__ == "__main__":
    unittest.main()
