"""双段链路（prompt_override）+ Soda 程序排字的三处断点（2026-11-21 实测）。

背景：页 2 出图出现「三层字重叠 + 旁白糊脸 + 字被模型画错」。查下来是三个
独立 bug 叠在一起，这个文件负责把三条都钉住：

1. `build_page_prompt(with_text=False)` 对 `prompt_override` 完全无效 ——
   底图提示词仍带中文，模型先画一遍、Soda 再画一遍；
2. 旁白的 `bubble` 传 `"none"`，不在 Soda 的 BUBBLE_TYPES 里 —— 不画白底框，
   字直接压在画面上；`"narration"` 才是白底方框；
3. `page_info` 只从 `panels[].caption` 读文字（多句拼成一段）—— 应该优先用
   `prompt_override` 里拆好的逐条 text items（每句一条 + 精确 bbox）。
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.features.comic import moss_comic, soda_letter  # noqa: E402


def _override_page():
    """一页带上双段链路 override 的 spec page（两句旁白 + 一句气泡）。"""
    items = [
        {"label": "character", "bbox": [0, -300, 400, 300],
         "caption": "Full-bleed single panel, the girl at the desk."},
        {"label": "text", "text": "蛋糕是她惦记了一路的事。",
         "bbox": [-706, -424, -384, 59],
         "caption": "Small rectangular caption box in the upper left corner, "
                    "no bubble tail, single column."},
        {"label": "text", "text": "说这话时她没抬头，可嘴角已经翘起来了。",
         "bbox": [-353, -424, -30, 59],
         "caption": "Small rectangular caption box in the upper left corner, "
                    "no bubble tail, single column."},
        {"label": "text", "text": "等我写完作业再吃。",
         "bbox": [368, 9, 691, 424],
         "caption": "Vertical rounded speech bubble with a thin black ink outline."},
    ]
    ov = {"summary": {"canvas": "single",
                      "caption": "Manga page. 1girl at a desk. "
                                 "A study desk upstairs, a wooden chair. "
                                 "Flat screentone shading, clear Chinese text: "
                                 "the girl's words in sharp speech bubbles."},
          "items": items}
    return {"n": 1, "layout": "S1", "scene": "A study desk upstairs.",
            "prompt_override": json.dumps(ov, ensure_ascii=False),
            "panels": [{"desc": "the girl at the desk",
                        "caption": "蛋糕是她惦记了一路的事。 说这话时她没抬头，可嘴角已经翘起来了。",
                        "bubbles": ["等我写完作业再吃。"]}]}


SPEC = {"cast": {"girl": "1girl, long black hair"}, "pages": [_override_page()]}


class WithTextSwitchTest(unittest.TestCase):
    """Bug 1：with_text=False 必须真的把文字剥掉。"""

    def setUp(self):
        self.page = SPEC["pages"][0]

    def test_with_text_keeps_items(self):
        d = json.loads(moss_comic.build_page_prompt(SPEC, self.page, with_text=True))
        texts = [i for i in d["items"] if i.get("label") == "text"]
        self.assertEqual(len(texts), 3)

    def test_without_text_strips_items(self):
        d = json.loads(moss_comic.build_page_prompt(SPEC, self.page, with_text=False))
        texts = [i for i in d["items"] if i.get("label") == "text"]
        self.assertEqual(texts, [])

    def test_without_text_swaps_tail(self):
        d = json.loads(moss_comic.build_page_prompt(SPEC, self.page, with_text=False))
        cap = d["summary"]["caption"]
        self.assertIn("no text, no lettering", cap)
        self.assertNotIn("clear Chinese text", cap)

    def test_without_text_keeps_scene(self):
        """剥字不能连画风/场景一起剥掉，否则底图就没描述了。"""
        d = json.loads(moss_comic.build_page_prompt(SPEC, self.page, with_text=False))
        cap = d["summary"]["caption"]
        self.assertIn("Manga page", cap)
        self.assertIn("study desk", cap)


class SodaTextsFromOverrideTest(unittest.TestCase):
    """Bug 3：文字要一条一句、带 override 的 bbox，不能拼成一段。"""

    def setUp(self):
        info = json.loads(soda_letter.page_info(SPEC, SPEC["pages"][0]))
        self.texts = info["page"]["texts"]

    def test_one_entry_per_sentence(self):
        self.assertEqual(len(self.texts), 3)
        got = [t["text"] for t in self.texts]
        self.assertIn("蛋糕是她惦记了一路的事。", got)
        self.assertIn("说这话时她没抬头，可嘴角已经翘起来了。", got)

    def test_narration_gets_box_shape(self):
        """Bug 2：旁白必须是 `narration`（白底方框），不是 "none"。"""
        narrs = [t for t in self.texts if t["kind"] == "narration"]
        self.assertEqual(len(narrs), 2)
        for t in narrs:
            self.assertEqual(t["bubble"], "narration")

    def test_dialogue_keeps_speech_bubble(self):
        bubs = [t for t in self.texts if t["kind"] == "dialogue"]
        self.assertEqual(len(bubs), 1)
        self.assertEqual(bubs[0]["bubble"], "speech")

    def test_narration_box_is_clamped(self):
        """旁白框要收小，否则 S1 满版时会糊住人脸。"""
        for t in self.texts:
            if t["kind"] != "narration":
                continue
            b = t["bbox"]
            self.assertLessEqual(b["w"], 340)
            self.assertLessEqual(b["h"], 240)

    def test_falls_back_to_panels_without_override(self):
        """老任务没有 override，仍要从 panels 拼出文字，不能什么都不排。"""
        page = {"n": 1, "layout": "S1", "scene": "x",
                "panels": [{"desc": "a girl", "caption": "一句旁白。",
                            "bubbles": ["一句台词。"]}]}
        info = json.loads(soda_letter.page_info({"cast": {}}, page))
        texts = info["page"]["texts"]
        self.assertEqual(len(texts), 2)
        self.assertTrue(any(t["kind"] == "narration" for t in texts))


if __name__ == "__main__":
    unittest.main()
