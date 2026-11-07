"""`app/soda_letter` 的手构 page_info 与工作流接线测试。

不 import soda_storyboard（agent 进程的 my_env 没装），只按 schema 断言：
字段对照 soda-storyboard 0.5.0 `models.Page.from_dict` /
`nodes/core.parse_info`；真正的排字效果由 `tools/moss_soda_e2e.py`
端到端验证。
"""

import json
import os
import unittest
from unittest import mock

from app import moss_comic, soda_letter

SPEC = {
    "title": "测试",
    "style": "soft light",
    "cast": {"woman": "an adult woman with black hair"},
    "pages": [{
        "n": 1, "layout": "1t2m2b",
        "scene": "a kitchen at dusk",
        "panels": [
            {"desc": "A medium shot of {woman} lifting a pot lid",
             "caption": "她掀开锅盖。", "bubbles": ["好香。", "快点快点。"]},
            {"desc": "A close-up of the pot", "sfx": "咕嘟咕嘟", "bubbles": []},
            {"desc": "A close-up of {woman} tasting", "bubble": "嗯，淡了一点。",
             "bubbles": []},
            {"desc": "A medium shot of {woman}", "bubbles": []},
            {"desc": "An extreme close-up of salt", "bubbles": []},
        ],
    }],
}


def _texts_by_id(info):
    return {t["id"]: t for t in info["page"]["texts"]}


class PageInfoTests(unittest.TestCase):
    def setUp(self):
        self.info = json.loads(soda_letter.page_info(SPEC, SPEC["pages"][0]))

    def test_top_level_is_parse_info_shape(self):
        self.assertEqual(self.info["version"], 1)
        self.assertIn("page", self.info)

    def test_lettering_mode_and_vertical(self):
        pg = self.info["page"]
        self.assertEqual(pg["dialogue_mode"], "lettering")
        self.assertEqual(pg["text_direction"], "vertical")
        self.assertEqual(pg["canvas_width"], 1024)
        self.assertEqual(pg["canvas_height"], 1536)

    def test_panels_match_layout_boxes(self):
        pg = self.info["page"]
        boxes = moss_comic.LAYOUT_BOXES["1t2m2b"]
        self.assertEqual(len(pg["panels"]), len(boxes))
        for p, cell in zip(pg["panels"], boxes):
            s = p["shape"]
            self.assertEqual(s["type"], "rect")
            # 中心原点 → 左上原点：x1-x0 = w，右下角不越画布
            self.assertEqual(s["w"], cell[3] - cell[1])
            self.assertEqual(s["h"], cell[2] - cell[0])
            self.assertGreaterEqual(s["x"], 0)
            self.assertGreaterEqual(s["y"], 0)
            self.assertLessEqual(s["x"] + s["w"], 1024)
            self.assertLessEqual(s["y"] + s["h"], 1536)
            # 画面描述里的角色占位符已展开
            self.assertNotIn("{woman}", p["description"])

    def test_text_entries_cover_all_dialogue(self):
        texts = _texts_by_id(self.info)
        self.assertEqual(texts["cap-0"]["text"], "她掀开锅盖。")
        self.assertEqual(texts["cap-0"]["kind"], "narration")
        self.assertEqual(texts["cap-0"]["bubble"], "none")
        self.assertEqual(texts["bub-0-0"]["text"], "好香。")
        self.assertEqual(texts["bub-0-1"]["text"], "快点快点。")
        self.assertEqual(texts["bub-0-0"]["bubble"], "speech")
        self.assertEqual(texts["bub-2-0"]["text"], "嗯，淡了一点。")
        self.assertEqual(texts["sfx-1"]["text"], "咕嘟咕嘟")
        self.assertEqual(texts["sfx-1"]["bubble"], "shout")
        # 老字段 `bubble`（单条字符串）也要认（_bubbles 的口径）
        self.assertIn("bub-2-0", texts)

    def test_bboxes_inside_own_panel(self):
        """文字框必须落在所属格子里：气泡贴边生乱码的教训（_text_bbox 文档）。"""
        pg = self.info["page"]
        panels = {p["id"]: p["shape"] for p in pg["panels"]}
        for t in pg["texts"]:
            box, pid = t["bbox"], t["panel_id"]
            self.assertIsNotNone(pid)
            s = panels[pid]
            self.assertGreaterEqual(box["x"], s["x"] - 2)
            self.assertGreaterEqual(box["y"], s["y"] - 2)
            self.assertLessEqual(box["x"] + box["w"], s["x"] + s["w"] + 2)
            self.assertLessEqual(box["y"] + box["h"], s["y"] + s["h"] + 2)

    def test_text_max_chars_is_column_limit(self):
        self.assertEqual(self.info["page"]["text_max_chars"], 6)


class RewireTests(unittest.TestCase):
    def _wf(self):
        return {
            "10": {"class_type": "VAEDecodeTiled",
                   "inputs": {"samples": ["7", 0], "vae": ["3", 0]}},
            "90": {"class_type": "SaveImage",
                   "inputs": {"images": ["10", 0], "filename_prefix": "x"}},
            "7": {"class_type": "KSampler", "inputs": {}},
        }

    def test_letter_node_inserted_before_save(self):
        out = soda_letter.rewire(self._wf(), '{"version":1}')
        nid = out["90"]["inputs"]["images"][0]
        self.assertNotEqual(nid, "10")
        node = out[nid]
        self.assertEqual(node["class_type"], "SodaLetterPage")
        self.assertEqual(node["inputs"]["image"], ["10", 0])
        self.assertEqual(node["inputs"]["page_info"], '{"version":1}')
        # 原工作流不被改动
        self.assertEqual(self._wf()["90"]["inputs"]["images"], ["10", 0])

    def test_node_id_does_not_collide(self):
        out = soda_letter.rewire(self._wf(), "{}")
        nid = out["90"]["inputs"]["images"][0]
        self.assertNotIn(nid, self._wf())


class SwitchTests(unittest.TestCase):
    def test_default_on(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(soda_letter._ENV, None)
            self.assertTrue(soda_letter.enabled())

    def test_env_off(self):
        with mock.patch.dict(os.environ, {soda_letter._ENV: "0"}):
            self.assertFalse(soda_letter.enabled())


if __name__ == "__main__":
    unittest.main()
