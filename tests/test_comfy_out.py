"""app/comfy_out.py：把 output 按「会话 + 渠道」分目录。

要钉住三件事：
1. **目录名正确**——群是 `qq/群_<号>`、私聊是 `qq/私聊_<号>`、网页是 `web/...`，
   漫画再按任务号分一层；只有数字/ASCII，不带群名（群名会改，还会带非法字符）。
2. **改写幂等**——重抽 / 重跑会把同一份 workflow 再提交一次，前缀不能叠成
   `a/b/a/b/Anima`。
3. **只动 SaveImage**——别的节点（云端渠道、奇怪工作流）原样放行，绝不能因为
   加目录把图卡住。
"""

import os
import unittest

from app import comfy_out


class SplitRelTest(unittest.TestCase):
    def test_relative_path_splits_into_name_and_subfolder(self):
        self.assertEqual(
            comfy_out.split_rel("qq/群_885/anima-line/Anima_00042_.png"),
            ("Anima_00042_.png", "qq/群_885/anima-line"))

    def test_plain_name_has_empty_subfolder(self):
        self.assertEqual(comfy_out.split_rel("Anima_00042_.png"),
                         ("Anima_00042_.png", ""))

    def test_backslash_path_is_understood(self):
        self.assertEqual(comfy_out.split_rel("qq\\群_885\\A_.png"),
                         ("A_.png", "qq/群_885"))

    def test_empty_is_safe(self):
        self.assertEqual(comfy_out.split_rel(""), ("", ""))


class BodyTest(unittest.TestCase):
    def _job(self, target, target_id, skill, **kw):
        from app.image_jobs import Job
        return Job(target, target_id, {}, skill=skill, **kw)

    def test_group_goes_under_its_own_number(self):
        job = self._job("group", "885143276", "anima-line")
        self.assertEqual(comfy_out.body_for(job), "qq/群_885143276/anima-line")

    def test_private_goes_under_its_own_number(self):
        job = self._job("private", "2596056376", "qwen_image_v1")
        self.assertEqual(comfy_out.body_for(job),
                         "qq/私聊_2596056376/qwen_image_v1")

    def test_web_side_without_target(self):
        job = self._job(None, None, "silver")
        self.assertEqual(comfy_out.body_for(job), "web/生图页/silver")

    def test_explicit_out_dir_wins(self):
        """漫画任务号只有它自己知道，入队时显式传。"""
        job = self._job(None, None, "anima-line", out_dir="web/漫画_C20261018-01")
        self.assertEqual(comfy_out.body_for(job),
                         "web/漫画_C20261018-01/anima-line")

    def test_no_skill_still_gives_a_body(self):
        job = self._job("group", "1", None)
        self.assertEqual(comfy_out.body_for(job), "qq/群_1")

    def test_group_name_is_not_used_and_illegal_chars_are_replaced(self):
        """现在只用号码；哪天把群名拼进来，也不能带出 `: ? *` 这些字符。"""
        self.assertEqual(comfy_out._safe("原神:交流?"), "原神_交流_")
        self.assertEqual(comfy_out._safe("..."), "_")

    def test_depth_is_capped(self):
        job = self._job(None, None, "s", out_dir="a/b/c/d/e/f")
        self.assertEqual(comfy_out.body_for(job).count("/"), 3)


class StampTest(unittest.TestCase):
    def _wf(self, prefix="Anima"):
        return {"9": {"class_type": "SaveImage",
                      "inputs": {"filename_prefix": prefix}}}

    def test_prefix_gets_the_directory(self):
        out = comfy_out.stamp(self._wf(), "qq/群_1/anima-line")
        self.assertEqual(out["9"]["inputs"]["filename_prefix"],
                         "qq/群_1/anima-line/Anima")

    def test_stamp_is_idempotent(self):
        """再次 stamp 不能再套一层目录（重抽/重跑会走同一条路）。"""
        once = comfy_out.stamp(self._wf(), "qq/群_1/anima-line")
        twice = comfy_out.stamp(once, "qq/群_1/anima-line")
        self.assertEqual(twice["9"]["inputs"]["filename_prefix"],
                         "qq/群_1/anima-line/Anima")

    def test_original_workflow_is_not_mutated(self):
        wf = self._wf()
        comfy_out.stamp(wf, "qq/群_1/anima-line")
        self.assertEqual(wf["9"]["inputs"]["filename_prefix"], "Anima")

    def test_nodes_other_than_saveimage_are_untouched(self):
        wf = {"1": {"class_type": "KSampler", "inputs": {"filename_prefix": "x"}},
              "9": {"class_type": "SaveImage", "inputs": {"filename_prefix": "A"}}}
        out = comfy_out.stamp(wf, "web/生图页/silver")
        self.assertEqual(out["1"]["inputs"]["filename_prefix"], "x")
        self.assertEqual(out["9"]["inputs"]["filename_prefix"],
                         "web/生图页/silver/A")

    def test_batch_saver_is_not_missed(self):
        """`_persona_fast` / 漫画批量用 BatchPromptImageGenerator 存图。

        2026-10-18 真机踩过：只认 SaveImage 时，扮演态与漫画批量的图**照样躺在
        output 根目录**（前缀没被改），而单图渠道都归好了 —— 一半对一半错最难查。
        """
        wf = {"33": {"class_type": "BatchPromptImageGenerator",
                     "inputs": {"filename_prefix": "Anima_Fast"}}}
        out = comfy_out.stamp(wf, "qq/群_1/_persona_fast")
        self.assertEqual(out["33"]["inputs"]["filename_prefix"],
                         "qq/群_1/_persona_fast/Anima_Fast")

    def test_dual_model_batch_saver_is_not_missed(self):
        wf = {"7": {"class_type": "DualModelBatchPromptImageGenerator",
                    "inputs": {"filename_prefix": "Anima"}}}
        out = comfy_out.stamp(wf, "web/漫画_C1/anima-line")
        self.assertEqual(out["7"]["inputs"]["filename_prefix"],
                         "web/漫画_C1/anima-line/Anima")

    def test_no_saveimage_returns_the_same_object(self):
        wf = {"1": {"class_type": "KSampler", "inputs": {}}}
        self.assertIs(comfy_out.stamp(wf, "qq/群_1/s"), wf)

    def test_empty_body_changes_nothing(self):
        wf = self._wf()
        self.assertIs(comfy_out.stamp(wf, ""), wf)

    def test_missing_prefix_falls_back_to_comfyui(self):
        wf = {"9": {"class_type": "SaveImage", "inputs": {}}}
        out = comfy_out.stamp(wf, "qq/群_1/s")
        self.assertEqual(out["9"]["inputs"]["filename_prefix"], "qq/群_1/s/ComfyUI")


class ManualRunTest(unittest.TestCase):
    def test_output_root_stays_for_manual_runs(self):
        """你自己在 ComfyUI 终端跑的图不带我们的前缀，所以仍落 output 根。

        这条是给维护者的提醒：分目录只发生在 `image_jobs.process` 里，
        skills/*/workflow.json 的 filename_prefix 一个都没改。
        """
        wf_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                              "skills")
        self.assertTrue(os.path.isdir(wf_dir))
        for name in ("moss", "silver", "anima-line"):
            path = os.path.join(wf_dir, name, "workflow.json")
            if not os.path.isfile(path):
                continue
            with open(path, encoding="utf-8") as f:
                text = f.read()
            self.assertNotIn("qq/", text)


if __name__ == "__main__":
    unittest.main()
