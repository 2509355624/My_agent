# -*- coding: utf-8 -*-
"""`tools/moss_prompt_review.py` 的守卫。

这工具的意义是「不出图先看提示词」——它的判据必须真的抓得住已知坏稿，
否则只是把 spec.json 打印一遍，看着热闹但没用。这里用两段合成稿正反各锁一遍。
"""
import importlib.util
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_spec = importlib.util.spec_from_file_location(
    "moss_prompt_review", os.path.join(ROOT, "tools", "moss_prompt_review.py"))
R = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(R)

GIRL = ("a girl about twelve, slender small frame, long straight dark hair with "
        "soft blunt bangs, a wide fluffy pale cloth headband, large round dark eyes, "
        "round soft face, a dark fabric scrunchie on her left wrist")
D2 = [[-768, -494, -8, 494], [8, -494, 768, 494]]


def _spec_file(caps, texts=()):
    items = [{"label": "character", "bbox": D2[i], "caption": c}
             for i, c in enumerate(caps)]
    items += [{"label": "text", "bbox": D2[0], "text": t,
               "caption": "Vertical rounded speech bubble with a thin black ink outline"}
              for t in texts]
    return {"cast": {"girl": GIRL},
            "pages": [{"n": 1, "layout": "D1", "scene": "", "panels": [],
                       "prompt_override": json.dumps({
                           "layout": "D1", "summary": {"canvas": "multi_panel",
                                                       "caption": "A manga page."},
                           "items": items})}]}


def _issues(spec):
    _, issues, warns = R.review(spec)
    return issues, warns


class ReviewCatchesTheBadDraft(unittest.TestCase):

    def test_midstate_is_reported_with_the_cell_number(self):
        """一格写了「正在脱」= 起点和终点叠在一起，出图必穿一件拿一件。"""
        spec = _spec_file([
            "Top panel, medium shot: %s, pulling it up over her head." % GIRL,
            "Bottom panel: %s, standing still." % GIRL,
        ])
        issues, _ = _issues(spec)
        hit = [m for _, m in issues if "中间态" in m and "格1" in m]
        self.assertTrue(hit, "没报到具体格：%s" % issues)

    def test_same_garment_worn_and_held_in_one_cell(self):
        """同一格里同一串衣物出现两次：一次当衣着、一次拿在手里。"""
        spec = _spec_file([
            "Top panel: %s, wearing a soaked white short-sleeve sport shirt, "
            "her fingers gathering the soaked white short-sleeve sport shirt into a ball."
            % GIRL,
            "Bottom panel: %s, standing still." % GIRL,
        ])
        issues, _ = _issues(spec)
        self.assertTrue([m for _, m in issues if "手里拿着" in m], issues)

    def test_same_garment_written_twice_is_not_a_clash(self):
        """两处都写在穿着语境里 = 描述重复，不是穿一件拿一件。

        实测（更衣话题 P1 背身格）：模型先写「背后的湿衬衫 + 短裤」，句末又贴一遍
        完整外形串和衣着。旧判据把它当矛盾，白烧两轮重修还是不过。
        """
        spec = _spec_file([
            "Top panel: %s, the same girl, her soaked white short-sleeve sport shirt "
            "clinging and dark, loose white running shorts, a girl about eighteen, "
            "slender small frame, wearing a soaked white short-sleeve sport shirt "
            "clinging to her body, loose white running shorts." % GIRL,
            "Bottom panel: %s, standing still." % GIRL,
        ])
        issues, warns = _issues(spec)
        self.assertEqual([m for _, m in issues if "手里拿着" in m], [])
        self.assertTrue([w for w in warns if "写了两遍" in w], warns)

    def test_face_closeup_is_not_forced_to_carry_the_body_string(self):
        """脸特写里没写身材和全身外形串是对的——逼它写就会把镜头拉远。"""
        spec = _spec_file([
            "Small left-top panel, close-up of her face from the front: long straight "
            "dark hair with soft blunt bangs, a wide fluffy pale cloth headband, large "
            "round dark eyes, round soft face, faint blush, eyes narrowed.",
            "Bottom panel: %s, standing still." % GIRL,
        ])
        issues, _ = _issues(spec)
        self.assertEqual([m for _, m in issues if "外形串漏项" in m], [])

    def test_text_outside_any_cell_is_reported(self):
        """文字 bbox 不落在任何一格内，字就会压在格线上。"""
        spec = _spec_file(["Top panel: %s." % GIRL, "Bottom panel: %s." % GIRL])
        page = spec["pages"][0]
        d = json.loads(page["prompt_override"])
        d["items"].append({"label": "text", "bbox": [-100, -900, 100, -800],
                           "text": "飘在外面",
                           "caption": "Vertical rounded speech bubble"})
        page["prompt_override"] = json.dumps(d)
        issues, _ = _issues(spec)
        self.assertTrue([m for _, m in issues if "不落在任何一格内" in m], issues)


class ReviewStaysQuietOnACleanDraft(unittest.TestCase):

    def test_clean_two_cell_page_has_no_blocking_issue(self):
        """两格各写一次衣着、各是确定状态，不许报错（否则工具会天天狼来了）。"""
        spec = _spec_file([
            "Top panel, medium shot: %s, wearing a plain white underlayer tank top, "
            "chin lifted." % GIRL,
            "Bottom panel, medium shot: %s, wearing a plain white underlayer tank top, "
            "right arm extended." % GIRL,
        ])
        issues, _ = _issues(spec)
        self.assertEqual([m for _, m in issues
                          if "中间态" in m or "同一串衣物" in m or "不落在任何一格" in m], [])


if __name__ == "__main__":
    unittest.main()
