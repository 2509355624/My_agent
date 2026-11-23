# -*- coding: utf-8 -*-
"""中文提示词进本地渠道前必须先翻成英文（2026-11-23）。

来路：手机端输入框里写了一大段中文，出的图和描述完全对不上。查下来是
新界面 `/api/gen/submit` 直通 ComfyUI、不经过 QQ 那条翻译，中文被原样
拼进英文 tag 串（`... soft painterly touches, 一位少女单人...`），本地
CLIP 读不懂中文 —— 等于没给提示词。这里钉住三件事：

1. 判定：含中文 + 渠道读不懂中文 → 要翻；qwen / moss 原生认中文 → 不翻；
2. `/api/gen/submit` 真跑一遍，送进 workflow 的 prompt 里没有中文；
3. 翻译失败（模型报错 / 吐回中文）不影响提交，按原文跑，不堵队列。

第 3 条是**故意**的：翻译只是提质，不能因为模型抽风就把用户的图卡住。
"""
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CJK_TEXT = "一位少女单人，戴着一顶宽檐编织草帽，赤脚蹲在泥土与杂草间"
EN_TAGS = "1girl, solo, wide-brimmed straw hat, barefoot, crouching"


class TestNeedsTranslate(unittest.TestCase):
    def setUp(self):
        from app.features.image import routes
        self.f = routes._needs_translate

    def test_chinese_into_local_channel_needs_it(self):
        for sk in ("semi-thick", "silver", "hd_3_clear", "anima-2s", "nai"):
            with self.subTest(skill=sk):
                self.assertTrue(self.f(sk, CJK_TEXT))

    def test_qwen_and_moss_keep_chinese(self):
        """这两个渠道自己认中文，翻过去反而丢信息、白烧 token。"""
        for sk in ("qwen_image_v1", "qwen-charsheet", "qwen-restore",
                   "moss-qunyou", "moss-light"):
            with self.subTest(skill=sk):
                self.assertFalse(self.f(sk, CJK_TEXT))

    def test_english_prompt_is_left_alone(self):
        self.assertFalse(self.f("semi-thick", "1girl, solo, straw hat"))


class TestSubmitTranslates(unittest.TestCase):
    def setUp(self):
        from app.features.image import routes as r
        self.r = r
        self.client = None

    def _submit(self, skill, prompt, fake_llm):
        """跑一次 /api/gen/submit，返回提交给 workflow 的那条 prompt。

        不真出图：`generate_image_build` 只负责拼工作流，这里替换成记录函数，
        worker 线程照样起，但拿不到工作流就自行失败，不影响断言。
        """
        import app.main as main_app
        seen = {}

        def fake_build(skill_, prompt_, seed_):
            seen["skill"] = skill_
            seen["prompt"] = prompt_
            raise RuntimeError("测试不真出图")

        with mock.patch.object(self.r, "generate_image_build", fake_build), \
             mock.patch("app.integrations.llm.call_llm", fake_llm):
            c = main_app.app.test_client()
            resp = c.post("/api/gen/submit",
                          json={"skill": skill, "prompt": prompt})
            body = resp.get_json()
            # 提交是同步成功的（worker 才去调 build）
            self.assertTrue(body.get("ok"), body)
            import time
            for _ in range(50):
                if "prompt" in seen:
                    break
                time.sleep(0.05)
        return seen.get("prompt"), body

    def test_chinese_reaches_workflow_in_english(self):
        prompt, _ = self._submit("semi-thick", CJK_TEXT,
                                 lambda *a, **k: EN_TAGS)
        self.assertIsNotNone(prompt, "没走到建工作流那步")
        self.assertIn("1girl", prompt)
        self.assertNotRegex(prompt, r"[\u4e00-\u9fff]")

    def test_failure_falls_back_to_the_original_text(self):
        """模型炸了也不能把图卡住：按原文跑（虽然效果差，但用户能看到结果）。"""
        def boom(*a, **k):
            raise RuntimeError("模型挂了")
        prompt, _ = self._submit("semi-thick", CJK_TEXT, boom)
        self.assertIn("草帽", prompt)

    def test_model_answering_in_chinese_counts_as_failure(self):
        """模型偷懒把中文抄回来 = 没干活，同样按原文走。"""
        prompt, _ = self._submit("semi-thick", CJK_TEXT,
                                 lambda *a, **k: CJK_TEXT)
        self.assertIn("草帽", prompt)

    def test_qwen_is_not_translated(self):
        """qwen 认中文，不该白调一次模型。"""
        calls = []

        def spy(*a, **k):
            calls.append(1)
            return EN_TAGS
        prompt, _ = self._submit("qwen_image_v1", CJK_TEXT, spy)
        self.assertEqual(calls, [], "qwen 不该触发翻译")
        self.assertIn("草帽", prompt)


if __name__ == "__main__":
    unittest.main()
