# -*- coding: utf-8 -*-
"""群像模式（2026-11-22）：多角色同屏合照。

验三件事：
  1. parse_group / compose_group 认「群像角色」多行，单女主输入不受影响
  2. audit 在 group=True 时跳过 POV 的「只画女生」判据，但仍拦真硬伤（格子对不上）
  3. generate(group=True) 用群像 SYSTEM、群像 cast；group=False 行为一字不变
"""
import json
import unittest

from app.features.comic import moss_agent as A


GROUP_BRIEF = """群像角色：
boy: a slim teenage boy with short black hair and round glasses, lean build
girl: a teenage girl with long dark hair, a pale cloth headband, round eyes
chubby_uncle: a chubby middle-aged man with a round belly and a receding hairline
athlete: a muscular young man with short cropped hair and broad shoulders
剧情：四个人在学校天台合照，挤在一起比手势。
转折：一阵风吹跑了其中一个人的帽子。"""


class ParseGroupTest(unittest.TestCase):
    def test_parses_all_four_roles(self):
        g = A.parse_group(GROUP_BRIEF)
        self.assertEqual(sorted(g["cast"]),
                         sorted(["boy", "girl", "chubby_uncle", "athlete"]))
        self.assertIn("slim teenage boy", g["cast"]["boy"])
        self.assertIn("群像角色", A.compose_group(g))  # 输出带标记给编剧读

    def test_plot_and_twist_split(self):
        g = A.parse_group(GROUP_BRIEF)
        self.assertIn("天台合照", g["plot"])
        self.assertIn("风吹跑", g["twist"])
        self.assertNotIn("天台合照", g["twist"])

    def test_no_group_lines_gives_empty_cast(self):
        g = A.parse_group("男主：无\n女主：a girl\n剧情：她扫地")
        self.assertEqual(g["cast"], {})

    def test_key_normalized(self):
        g = A.parse_group("群像角色：\nChubby Uncle: a chubby man\n剧情：合照")
        self.assertIn("chubbyuncle", g["cast"])   # 空格去掉、小写

    def test_role_lines_after_group_captured(self):
        g = A.parse_group("群像角色：\nboy: a boy\n剧情：合照\n转折：出意外")
        self.assertEqual(g["cast"], {"boy": "a boy"})
        self.assertIn("意外", g["twist"])


class AuditGroupTest(unittest.TestCase):
    def _min_spec(self):
        """一页一格、一个 character、一个 boy 男性描述的最小 spec。"""
        return {
            "palette": "navy top, cream wall",
            "cast": {"boy": "a slim teenage boy with short black hair, lean build",
                     "athlete": "a muscular man with broad shoulders"},
            "pages": [{
                "layout": "S1", "beat": "四人合照",
                "summary": {"canvas": "single",
                            "caption": "A group shot. " + A.STYLE_LOCK + " " + A.NO_TAIL},
                "items": [{
                    "label": "character",
                    "caption": "full shot of four friends, a boy and a muscular man side by side",
                    "bbox": [0, 0, 100, 100],
                }],
                "bbox_coord_space": "abs_centralized", "bbox_format": "yxyx",
            }],
        }

    def test_pov_mode_flags_male(self):
        out, _ = A.audit(self._min_spec(), cap=2, group=False)
        self.assertTrue(any("男性" in x for x in out),
                        "单女主模式仍要拦男性人形：%s" % out)

    def test_group_mode_allows_male(self):
        out, _ = A.audit(self._min_spec(), cap=2, group=True)
        self.assertFalse(any("男性" in x for x in out),
                         "群像模式不该拦男性：%s" % out)

    def test_group_mode_requires_every_cast_filled(self):
        js = self._min_spec()
        js["cast"]["athlete"] = ""
        out, _ = A.audit(js, cap=2, group=True)
        self.assertTrue(any("空外形串" in x for x in out), out)

    def test_group_mode_still_flags_layout(self):
        """跳过 POV 不等于放行一切：格子错位照样拦。"""
        js = self._min_spec()
        js["pages"][0]["items"][0]["bbox"] = [0, 0, 50, 50]  # 不是 S1 的整格
        out, _ = A.audit(js, cap=2, group=True)
        self.assertTrue(any("对不上" in x for x in out), out)

    def test_group_mode_flags_clothes_in_any_cast(self):
        js = self._min_spec()
        js["cast"]["athlete"] = "a muscular man wearing a red jersey"
        out, _ = A.audit(js, cap=2, group=True)
        self.assertTrue(any("cast.athlete 写了衣服" in x for x in out), out)


class GenerateGroupTest(unittest.TestCase):
    def test_group_uses_group_system_and_no_male_flag(self):
        captured = {}

        def call(system, user, **kw):
            captured["system"] = system
            captured["user"] = user
            return json.dumps({
                "title": "天台合照", "palette": "navy top, cream wall",
                "cast": {"boy": "a slim teenage boy with short black hair",
                         "athlete": "a muscular man with broad shoulders"},
                "pages": [{
                    "layout": "S1", "beat": "四人挤在一起",
                    "summary": {"canvas": "single",
                                "caption": "A group shot. " + A.STYLE_LOCK + " " + A.NO_TAIL},
                    "items": [{"label": "character",
                               "caption": "four friends close together, a boy and a muscular man",
                               "bbox": [0, 0, 100, 100]}],
                    "bbox_coord_space": "abs_centralized", "bbox_format": "yxyx",
                }],
            })

        spec, report = A.generate(GROUP_BRIEF, 1, call, group=True)
        self.assertIn("群像模式", captured["system"])
        self.assertIn("群像角色", captured["user"])
        self.assertEqual(sorted(spec["cast"]), ["athlete", "boy"])
        self.assertEqual(report["pages"][0]["n"], 1)

    def test_solo_unchanged(self):
        """不传 group：SYSTEM 原样、按男主/女主解析，行为与改动前一致。"""
        captured = {}

        def call(system, user, **kw):
            captured["system"] = system
            captured["user"] = user
            return json.dumps({
                "title": "扫地", "palette": "navy top",
                "cast": {"girl": "a girl about twelve, slender teen proportions, "
                                 "long legs, small head, long dark hair, round eyes, "
                                 "round face, a wrist band"},
                "pages": [{
                    "layout": "S1", "beat": "灯绳断了",
                    "summary": {"canvas": "single",
                                "caption": "A girl sweeping. " + A.STYLE_LOCK + " " + A.NO_TAIL},
                    "items": [{"label": "character",
                               "caption": "a girl sweeping the floor",
                               "bbox": [0, 0, 100, 100]}],
                    "bbox_coord_space": "abs_centralized", "bbox_format": "yxyx",
                }],
            })

        A.generate("男主：无\n女主：a girl\n剧情：她扫地", 1, call)
        self.assertNotIn("群像模式", captured["system"])
        self.assertEqual(captured["system"], A.SYSTEM)
        self.assertIn("只有她一个人", captured["user"])


if __name__ == "__main__":
    unittest.main()
