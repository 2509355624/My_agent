# -*- coding: utf-8 -*-
"""漫画台全局附加提示词（2026-11-12 站主拍板）。

钉死这几件容易悄悄回退的事：

  * 全局一份、持久保存，写盘走临时文件 + os.replace；
  * 只做格式清洗，**不判断内容合不合理**；
  * 拼在**渲染层**，不写回剧本（`scene` 一变会让所有历史任务旧图作废）；
  * 逐格 / 独占批量（本机 ComfyUI）与 NAI 云端**两路都要吃到**；
  * 群友能跟着吃到后缀，但改不了站主的全局设置；
  * 站主页有设置卡，群友派生页整卡隐藏。
"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import comic, comic_api, comic_prompt_suffix, main

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _page(name):
    with open(os.path.join(_ROOT, "web", name), encoding="utf-8") as f:
        return f.read()


class _Base(unittest.TestCase):
    """每个用例一份独立的后缀文件，不碰生产 state/。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.fp = os.path.join(self.tmp.name, "suffix.json")
        p = mock.patch.object(comic_prompt_suffix, "path", return_value=self.fp)
        p.start()
        self.addCleanup(p.stop)


class CleanTest(unittest.TestCase):
    def test_newlines_become_commas(self):
        self.assertEqual(comic_prompt_suffix.clean("a,\nb\n\nc"),
                         "a, b, c")

    def test_leading_trailing_separators_go_away(self):
        self.assertEqual(comic_prompt_suffix.clean(" , a , b，,"), "a , b")

    def test_whitespace_is_compacted(self):
        self.assertEqual(comic_prompt_suffix.clean("  a   b  "), "a b")

    def test_none_is_empty(self):
        self.assertEqual(comic_prompt_suffix.clean(None), "")


class LoadSaveTest(_Base):
    def test_missing_file_is_empty(self):
        self.assertEqual(comic_prompt_suffix.load(), "")

    def test_round_trip(self):
        self.assertEqual(comic_prompt_suffix.save("  a,\nb  "), "a, b")
        self.assertEqual(comic_prompt_suffix.load(), "a, b")

    def test_atomic_write_leaves_no_tmp(self):
        comic_prompt_suffix.save("x")
        self.assertFalse(os.path.isfile(self.fp + ".tmp"))
        with open(self.fp, encoding="utf-8") as f:
            self.assertEqual(json.load(f), {"suffix": "x"})

    def test_broken_json_is_treated_as_empty(self):
        with open(self.fp, "w", encoding="utf-8") as f:
            f.write("{not json")
        self.assertEqual(comic_prompt_suffix.load(), "")

    def test_plain_text_file_still_reads(self):
        with open(self.fp, "w", encoding="utf-8") as f:
            json.dump("a,\nb", f)
        self.assertEqual(comic_prompt_suffix.load(), "a, b")

    def test_unknown_shape_is_empty(self):
        with open(self.fp, "w", encoding="utf-8") as f:
            json.dump([1, 2], f)
        self.assertEqual(comic_prompt_suffix.load(), "")

    def test_over_limit_raises_and_does_not_write(self):
        with self.assertRaises(ValueError) as cm:
            comic_prompt_suffix.save("x" * (comic_prompt_suffix.MAX_CHARS + 1))
        self.assertIn("最多", str(cm.exception))
        self.assertFalse(os.path.isfile(self.fp))

    def test_empty_string_clears(self):
        comic_prompt_suffix.save("a")
        self.assertEqual(comic_prompt_suffix.save(""), "")
        self.assertEqual(comic_prompt_suffix.load(), "")


class ApplyTest(_Base):
    def test_empty_suffix_returns_the_original_scene(self):
        self.assertEqual(comic_prompt_suffix.apply("1girl, smile", ""),
                         "1girl, smile")

    def test_suffix_is_appended_after_the_scene(self):
        comic_prompt_suffix.save("masterpiece, best quality")
        self.assertEqual(comic_prompt_suffix.apply("1girl, smile"),
                         "1girl, smile, masterpiece, best quality")

    def test_trailing_comma_does_not_double_up(self):
        self.assertEqual(comic_prompt_suffix.apply("1girl, smile,", "extra"),
                         "1girl, smile, extra")

    def test_explicit_suffix_does_not_read_disk(self):
        with mock.patch.object(comic_prompt_suffix, "load",
                               side_effect=AssertionError("不该读盘")):
            self.assertEqual(comic_prompt_suffix.apply("a", "b"), "a, b")

    def test_original_scene_object_is_untouched(self):
        scene = "1girl, smile"
        comic_prompt_suffix.apply(scene, "extra")
        self.assertEqual(scene, "1girl, smile")

    def test_empty_scene_with_suffix(self):
        self.assertEqual(comic_prompt_suffix.apply("", "extra"), "extra")


class InjectionTest(_Base):
    """两路生图都要吃到，且剧本原文一行不改。"""

    def setUp(self):
        super().setUp()
        comic_prompt_suffix.save("GLOBAL_EXTRA")
        self.p = mock.Mock()
        self.p.scene = "1girl, smile"
        self.p.no = "01"
        self.task = mock.Mock()
        self.task.dir = self.tmp.name
        self.task.raw_dir = os.path.join(self.tmp.name, "raw")
        self.task.raw_path.return_value = os.path.join(self.task.raw_dir,
                                                      "01_raw.png")
        self.task._rel.return_value = "raw/01_raw.png"

    def test_scene_prompt_appends_without_writing_back(self):
        self.assertEqual(comic.scene_prompt(self.p),
                         "1girl, smile, GLOBAL_EXTRA")
        self.assertEqual(self.p.scene, "1girl, smile")

    def test_local_comfyui_workflow_gets_the_suffix(self):
        with mock.patch.object(comic.generate_image, "build_t2i_workflow",
                               return_value={"x": 1}) as bw, \
             mock.patch.object(comic.comic_render_opts, "load",
                               return_value={}), \
             mock.patch.object(comic.comic_render_opts, "apply",
                               side_effect=lambda wf, opts: wf), \
             mock.patch.object(comic.comic_render_opts, "describe",
                               return_value=""), \
             mock.patch.object(comic, "_new_seed", return_value=42):
            comic._scene_wf(self.task, self.p, "anima-line")
        self.assertEqual(bw.call_args[0][1], "1girl, smile, GLOBAL_EXTRA")
        self.assertEqual(self.p.scene, "1girl, smile")

    def test_nai_cloud_gets_the_suffix(self):
        with mock.patch.object(comic.image_jobs, "NAI_SKILLS",
                               {"nai-test"}), \
             mock.patch.object(comic, "_submit_nai", return_value="") as sn, \
             mock.patch.object(comic, "_publish_file"), \
             mock.patch.object(comic, "_add_timing"):
            comic.render_scene(self.task, self.p, "nai-test")
        self.assertEqual(sn.call_args[0][0], "1girl, smile, GLOBAL_EXTRA")
        self.assertEqual(self.p.scene, "1girl, smile")

    def test_empty_suffix_keeps_old_behaviour(self):
        comic_prompt_suffix.save("")
        with mock.patch.object(comic.generate_image, "build_t2i_workflow",
                               return_value={"x": 1}) as bw, \
             mock.patch.object(comic.comic_render_opts, "load",
                               return_value={}), \
             mock.patch.object(comic.comic_render_opts, "apply",
                               side_effect=lambda wf, opts: wf), \
             mock.patch.object(comic.comic_render_opts, "describe",
                               return_value=""), \
             mock.patch.object(comic, "_new_seed", return_value=42):
            comic._scene_wf(self.task, self.p, "anima-line")
        self.assertEqual(bw.call_args[0][1], "1girl, smile")

    def test_ledger_still_records_the_writer_original(self):
        """账本里的 prompt 只记编剧原文，不跟着后缀变（enqueue 的语义）。"""
        with open(os.path.join(_ROOT, "app", "comic.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertIn("build_t2i_workflow(scene_skill, scene_prompt(p), seed)", src)
        self.assertIn("_submit_nai(scene_prompt(p), scene_skill)", src)
        self.assertIn("_submit_timed(wf, scene_skill, p.scene, seed", src)
        self.assertIn("job = _submit(wf, scene_skill, p.scene, seed", src)


class ApiTest(_Base):
    def setUp(self):
        super().setUp()
        self.client = main.app.test_client()

    def test_get_empty(self):
        r = self.client.get("/api/comic/prompt_suffix")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["suffix"], "")
        self.assertEqual(r.get_json()["max_chars"],
                         comic_prompt_suffix.MAX_CHARS)

    def test_post_then_get(self):
        r = self.client.post("/api/comic/prompt_suffix",
                             json={"suffix": "  a,\nb  "})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["suffix"], "a, b")
        self.assertEqual(
            self.client.get("/api/comic/prompt_suffix").get_json()["suffix"],
            "a, b")

    def test_post_empty_clears(self):
        self.client.post("/api/comic/prompt_suffix", json={"suffix": "a"})
        r = self.client.post("/api/comic/prompt_suffix", json={"suffix": ""})
        self.assertEqual(r.get_json()["suffix"], "")
        self.assertEqual(comic_prompt_suffix.load(), "")

    def test_over_limit_is_400(self):
        r = self.client.post(
            "/api/comic/prompt_suffix",
            json={"suffix": "x" * (comic_prompt_suffix.MAX_CHARS + 1)})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(comic_prompt_suffix.load(), "")

    def test_friend_cannot_write_but_still_reads(self):
        comic_prompt_suffix.save("keep")
        with mock.patch.object(comic_api, "_qq", return_value="12345"):
            r = self.client.post("/api/comic/prompt_suffix",
                                 json={"suffix": "hack"})
            self.assertEqual(r.status_code, 403)
            g = self.client.get("/api/comic/prompt_suffix")
        self.assertEqual(g.status_code, 200)
        self.assertEqual(comic_prompt_suffix.load(), "keep")

    def test_both_routes_are_registered(self):
        rules = {r.rule for r in main.app.url_map.iter_rules()}
        self.assertIn("/api/comic/prompt_suffix", rules)


class PageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.master = _page("comic_studio.html")
        cls.friend = _page("comic_studio_friend.html")

    def test_master_has_the_setting_card(self):
        self.assertIn('id="suffixCard"', self.master)
        self.assertIn('id="suffixText"', self.master)
        self.assertIn("savePromptSuffix()", self.master)
        self.assertIn("clearPromptSuffix()", self.master)
        self.assertIn("loadPromptSuffix();", self.master)
        self.assertIn("/api/comic/prompt_suffix", self.master)

    def test_master_explains_the_boundaries(self):
        self.assertIn("不改写剧本原文", self.master)
        self.assertIn("已出的图不动", self.master)
        self.assertIn("NAI 云端", self.master)

    def test_friend_page_hides_the_card(self):
        self.assertIn('class="card hidden-by-friend" id="suffixCard"',
                      self.friend)
        self.assertIn(".hidden-by-friend { display: none !important; }",
                      self.friend)

    def test_no_duplicate_function_names(self):
        """漫画台是单文件大页，同名函数会静默覆盖（persona 页出过事故）。"""
        for name in ("loadPromptSuffix", "savePromptSuffix",
                     "clearPromptSuffix"):
            self.assertEqual(self.master.count("function %s(" % name), 1,
                             "%s 定义了不止一次" % name)

    def test_backend_docstring_lists_the_routes(self):
        with open(os.path.join(_ROOT, "app", "comic_api.py"),
                  encoding="utf-8") as f:
            src = f.read()
        self.assertIn("/api/comic/prompt_suffix", src)


if __name__ == "__main__":
    unittest.main()
