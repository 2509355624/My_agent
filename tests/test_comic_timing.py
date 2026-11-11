# -*- coding: utf-8 -*-
"""漫画台「生成耗时」测试（2026-11-11）。

钉死四件事：
  1. 每格两阶段各记两个口径：`*_exec`（ComfyUI 纯执行，不含排队）与
     `*_wall`（含排队/重试的墙上时间）；split 分开画 = 一格里两次提交累加。
  2. 落盘往返 + 老任务容错：state.json 没这些键 / 塞了坏数据，load 后都是
     None（界面显示「—」，不倒推、不把前端 .toFixed 弄炸）。
  3. 兼容层 `_submit_timed`：线上真 `_submit` 支持 `with_entry=True` → 拿到
     exec；测试/老替身是固定签名、只返回文件名 → 自动退回、exec=None 但
     wall 照记，绝不能崩（现有 TwoPassBubbleTest 等靠这条不红）。
  4. `/api/comic/status` 每格透出四个字段 + 顶层两段汇总；exec 段只要有一格
     拿不到就整段标 None（不给「只算了一半格」的虚高数字）。
"""

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import comic, comic_api, image_jobs, main

SCRIPT = ("# 标题：耗时测试\n\n## 01\n画面: 1girl, solo, room\n"
          "气泡: 第一句\n气泡2: 第二句\n标签1: 旁白\n")
GOOD = "# 标题：耗时测试\n\n## 01\n画面: 1girl, solo, room\n气泡: 第一句\n"


def _mk_task(tmp, text=GOOD):
    path = os.path.join(tmp, "script.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    meta, panels = comic.parse_script(path)
    return comic.Task(path, meta, panels)


def _read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


class PanelTimingFieldTest(unittest.TestCase):
    def test_new_panel_has_four_none_timings(self):
        p = comic.Panel("01", "scene", "bubble")
        for attr in ("scene_exec", "scene_wall", "bubble_exec", "bubble_wall"):
            self.assertIsNone(getattr(p, attr), attr)


class StateTimingRoundTripTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.task = _mk_task(self.tmp.name)

    def test_save_load_roundtrip_keeps_timings(self):
        p = self.task.panels[0]
        p.scene_exec, p.scene_wall = 44.5, 52.0
        p.bubble_exec, p.bubble_wall = 66.0, 75.3
        self.task.save_state()
        # state.json 里四个键都在
        raw = _read_json(self.task.state_path)
        cell = raw["panels"]["01"]
        for k in ("scene_exec", "scene_wall", "bubble_exec", "bubble_wall"):
            self.assertIn(k, cell)
        # 重新解析 + load_state 贴回
        fresh = _mk_task(self.tmp.name)
        fresh.load_state()
        q = fresh.panels[0]
        self.assertEqual((q.scene_exec, q.scene_wall), (44.5, 52.0))
        self.assertEqual((q.bubble_exec, q.bubble_wall), (66.0, 75.3))

    def test_old_state_without_keys_loads_as_none(self):
        p = self.task.panels[0]
        p.state = "done"
        self.task.save_state()
        raw = _read_json(self.task.state_path)
        for k in ("scene_exec", "scene_wall", "bubble_exec", "bubble_wall"):
            raw["panels"]["01"].pop(k, None)      # 模拟老任务
        with open(self.task.state_path, "w", encoding="utf-8") as f:
            json.dump(raw, f, ensure_ascii=False)
        fresh = _mk_task(self.tmp.name)
        fresh.load_state()                         # 不该抛
        q = fresh.panels[0]
        for attr in ("scene_exec", "scene_wall", "bubble_exec", "bubble_wall"):
            self.assertIsNone(getattr(q, attr), attr)

    def test_bad_timing_values_are_ignored(self):
        p = self.task.panels[0]
        p.state = "done"
        self.task.save_state()
        raw = _read_json(self.task.state_path)
        raw["panels"]["01"].update(scene_exec="oops", scene_wall=True,
                                   bubble_exec=None, bubble_wall=12.34)
        with open(self.task.state_path, "w", encoding="utf-8") as f:
            json.dump(raw, f, ensure_ascii=False)
        fresh = _mk_task(self.tmp.name)
        fresh.load_state()
        q = fresh.panels[0]
        self.assertIsNone(q.scene_exec)            # 字符串不认
        self.assertIsNone(q.scene_wall)            # bool 不认
        self.assertIsNone(q.bubble_exec)           # null 不认
        self.assertEqual(q.bubble_wall, 12.34)     # 只有合法数字认

    def test_scene_change_clears_both_stages(self):
        p = self.task.panels[0]
        p.state, p.scene_exec, p.scene_wall = "done", 44.5, 52.0
        p.bubble_exec, p.bubble_wall = 66.0, 75.3
        self.task.save_state()
        # 剧本把画面改了
        fresh = _mk_task(self.tmp.name, SCRIPT.replace("room", "garden"))
        fresh.load_state()
        q = fresh.panels[0]
        self.assertEqual(q.state, "pending")
        for attr in ("scene_exec", "scene_wall", "bubble_exec", "bubble_wall"):
            self.assertIsNone(getattr(q, attr), attr)

    def test_bubble_text_change_clears_only_bubble_stage(self):
        p = self.task.panels[0]
        p.state = "done"
        p.scene_exec, p.scene_wall = 44.5, 52.0
        p.bubble_exec, p.bubble_wall = 66.0, 75.3
        self.task.save_state()
        # 只改台词，底图复用
        fresh = _mk_task(self.tmp.name, GOOD.replace("第一句", "新台词"))
        fresh.load_state()
        q = fresh.panels[0]
        self.assertEqual(q.state, "rendered")
        self.assertEqual((q.scene_exec, q.scene_wall), (44.5, 52.0))
        self.assertIsNone(q.bubble_exec)
        self.assertIsNone(q.bubble_wall)


class AddTimingTest(unittest.TestCase):
    def test_accumulates_both_stages(self):
        p = comic.Panel("01", "s", "b")
        comic._add_timing(p, "bubble", 20.0, 25.0)   # 第一遍
        comic._add_timing(p, "bubble", 19.5, 22.0)   # split 第二遍
        self.assertEqual(p.bubble_exec, 39.5)
        self.assertEqual(p.bubble_wall, 47.0)
        comic._add_timing(p, "scene", 44.0, 50.0)
        self.assertEqual((p.scene_exec, p.scene_wall), (44.0, 50.0))

    def test_none_exec_poisons_stage_exec_but_wall_survives(self):
        p = comic.Panel("01", "s", "b")
        comic._add_timing(p, "scene", 44.0, 50.0)
        comic._add_timing(p, "scene", None, 3.0)     # 有一次拿不到 exec
        self.assertIsNone(p.scene_exec, "半截 exec 比「—」更误导，应整段作废")
        self.assertEqual(p.scene_wall, 53.0)


class SubmitTimedCompatTest(unittest.TestCase):
    """兼容层：真 _submit 拿 exec；固定签名替身退回、exec=None 但不崩。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_fixed_signature_stub_falls_back(self):
        calls = []

        def _stub(wf, skill, prompt, seed, task=None):
            calls.append(prompt)
            return "out.png"

        with mock.patch.object(comic, "_submit", _stub):
            name, ex, wall = comic._submit_timed({}, "krea2", "p", 1)
        self.assertEqual(name, "out.png")
        self.assertIsNone(ex)                        # 替身没 entry → 无 exec
        self.assertIsInstance(wall, float)
        self.assertGreaterEqual(wall, 0.0)
        self.assertEqual(len(calls), 1)             # 只调一次，没因 TypeError 重跑

    def test_with_entry_stub_yields_exec(self):
        def _stub(wf, skill, prompt, seed, task=None, with_entry=False):
            entry = {"status": {"messages": [
                ["execution_start", {"timestamp": 1000}],
                ["execution_success", {"timestamp": 45300}],   # 44.3 秒
            ]}}
            return ("out.png", entry) if with_entry else "out.png"

        with mock.patch.object(comic, "_submit", _stub):
            name, ex, wall = comic._submit_timed({}, "anima-line", "p", 1)
        self.assertEqual(name, "out.png")
        self.assertEqual(ex, 44.3)                  # exec_seconds 从 entry 算
        self.assertIsInstance(wall, float)

    def test_unrelated_typeerror_is_not_swallowed(self):
        def _bad(wf, skill, prompt, seed, task=None, with_entry=False):
            raise TypeError("something else broke")

        with mock.patch.object(comic, "_submit", _bad):
            with self.assertRaisesRegex(TypeError, "something else"):
                comic._submit_timed({}, "x", "p", 1)


class RenderTimingTest(unittest.TestCase):
    """render_bubble / render_scene(NAI) 真记耗时。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _task(self, text=GOOD):
        return _mk_task(self.tmp.name, text)

    def test_render_bubble_records_exec_and_wall(self):
        task = self._task()
        p = task.panels[0]
        os.makedirs(task.raw_dir, exist_ok=True)
        with open(task.raw_path(p), "wb") as f:
            f.write(b"raw")
        os.makedirs(task.final_dir, exist_ok=True)

        def _upload(path):
            return "uploaded.png"

        def _build(skill, prompt, seed, uploaded):
            return {"prompt": prompt}

        def _stub(wf, skill, prompt, seed, task=None, with_entry=False):
            entry = {"status": {"messages": [
                ["execution_start", {"timestamp": 0}],
                ["execution_success", {"timestamp": 21000}],
            ]}}
            return ("out.png", entry) if with_entry else "out.png"

        def _download(name, dst):
            with open(dst, "wb") as f:
                f.write(b"png")

        for target, repl in (("_upload_source", _upload), ("_build_i2i", _build),
                             ("_submit", _stub), ("_download", _download)):
            pp = mock.patch.object(comic, target, repl)
            pp.start()
            self.addCleanup(pp.stop)
        comic.render_bubble(task, p)
        self.assertEqual(p.state, "done")
        self.assertEqual(p.bubble_exec, 21.0)
        self.assertGreaterEqual(p.bubble_wall, 0.0)

    def test_render_scene_nai_records_wall_only(self):
        task = self._task()
        p = task.panels[0]
        src = os.path.join(self.tmp.name, "nai_out.png")
        with open(src, "wb") as f:
            f.write(b"png")
        with mock.patch.object(comic.image_jobs, "NAI_SKILLS", {"nai-test"}), \
                mock.patch.object(comic, "_submit_nai", lambda *a, **k: src):
            comic.render_scene(task, p, "nai-test")
        self.assertEqual(p.state, "rendered")
        self.assertIsNone(p.scene_exec, "NAI 云端无 ComfyUI entry → exec 该缺")
        self.assertIsInstance(p.scene_wall, float)


class StatusTimingTest(unittest.TestCase):
    """status 端点：每格四字段 + 顶层两段汇总。"""

    def setUp(self):
        self.c = main.app.test_client()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.name = "C-timing"
        self.dir = os.path.join(self.tmp.name, self.name)
        os.makedirs(self.dir, exist_ok=True)
        self.task = _mk_task(self.dir, GOOD)
        self.task.save_state()
        self.root = mock.patch.object(comic_api, "_COMICS_ROOT", self.tmp.name)
        self.root.start()
        self.addCleanup(self.root.stop)
        with comic_api._JOBS_LOCK:
            comic_api._JOBS.pop(self.dir, None)

    def _fetch(self):
        return self.c.get("/api/comic/status?task=" + self.name).get_json()

    def test_panels_expose_four_timing_fields(self):
        d = self._fetch()
        item = d["panels"][0]
        for k in ("scene_exec", "scene_wall", "bubble_exec", "bubble_wall"):
            self.assertIn(k, item)
        self.assertIn("timing", d)

    def test_stage_sums_when_all_panels_have_data(self):
        p = self.task.panels[0]
        p.scene_exec, p.scene_wall = 40.0, 50.0
        p.bubble_exec, p.bubble_wall = 60.0, 70.0
        self.task.save_state()
        t = self._fetch()["timing"]
        self.assertEqual(t["scene"], {"exec": 40.0, "wall": 50.0, "n": 1})
        self.assertEqual(t["bubble"], {"exec": 60.0, "wall": 70.0, "n": 1})

    def test_stage_exec_is_none_if_any_panel_lacks_it(self):
        # 两格：一格有 exec、一格没有 → 整段 exec 必须 None（不虚高）
        task = _mk_task(self.dir, GOOD + "\n## 02\n画面: 2girls\n气泡: 二\n")
        task.save_state()
        a, b = task.panels
        a.scene_exec, a.scene_wall = 40.0, 50.0
        b.scene_exec, b.scene_wall = None, 30.0
        task.save_state()
        t = self._fetch()["timing"]
        self.assertIsNone(t["scene"]["exec"])
        self.assertEqual(t["scene"]["wall"], 80.0)
        self.assertEqual(t["scene"]["n"], 2)

    def test_empty_stage_is_null(self):
        t = self._fetch()["timing"]
        self.assertIsNone(t["scene"]["wall"])
        self.assertEqual(t["scene"]["n"], 0)


class PageTimingTest(unittest.TestCase):
    """静态页：耗时渲染分支在、无重名函数、括号平衡。"""

    def test_page_renders_timing_without_breaking(self):
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(base, "web", "comic_studio.html"),
                  encoding="utf-8") as f:
            html = f.read()
        import re
        from collections import Counter
        fns = re.findall(r"function\s+([A-Za-z_$][\w$]*)\s*\(", html)
        dup = [k for k, v in Counter(fns).items() if v > 1]
        self.assertFalse(dup, "同名函数静默覆盖：%s" % dup)
        for a, b in (("{", "}"), ("(", ")"), ("[", "]")):
            self.assertEqual(html.count(a), html.count(b), "括号不平衡 %s%s" % (a, b))
        for token in ("fmtSecs", "stageTiming", "d.timing || {}",
                      "p.scene_exec", "p.bubble_wall", "timingBits"):
            self.assertIn(token, html, token)


if __name__ == "__main__":
    unittest.main()
