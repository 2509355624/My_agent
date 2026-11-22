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

    def test_new_role_with_look_passes(self):
        """2026-11-22：放宽 POV 男性禁令——写了种族+外形的人类守卫照过。"""
        js = self._min_spec()
        js["pages"][0]["items"][0]["caption"] = (
            "full shot of a human guard with short grey hair and a steel helmet")
        out, _ = A.audit(js, cap=2, group=False)
        self.assertFalse(any("没写种族" in x for x in out), out)

    def test_new_role_bare_title_is_blocked(self):
        """光杆 a guard 没交外形：拦，否则模型拿前几页的哥布林填空。"""
        js = self._min_spec()
        js["pages"][0]["items"][0]["caption"] = "full shot of a guard holding a spear"
        out, _ = A.audit(js, cap=2, group=False)
        self.assertTrue(any("没写种族+外形" in x for x in out), out)

    def test_group_mode_skips_bare_title_check(self):
        """群像有自己的 cast 外形串检查，不重复用单女主的泛称判据。"""
        js = self._min_spec()
        js["pages"][0]["items"][0]["caption"] = "full shot of a guard holding a spear"
        out, _ = A.audit(js, cap=2, group=True)
        self.assertFalse(any("没写种族+外形" in x for x in out), out)

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

    def test_multipage_group_requires_link_but_not_single_page(self):
        """多页群像必须交代从上一拍接哪儿；代码只查有没有交，不判断写得对不对。"""
        one = self._min_spec()
        out, _ = A.audit(one, cap=2, group=True)
        self.assertFalse(any("没写 link" in x for x in out), out)

        two = self._min_spec()
        two["pages"].append(json.loads(json.dumps(two["pages"][0])))
        out, _ = A.audit(two, cap=4, group=True)
        self.assertEqual(len([x for x in out if "没写 link" in x]), 2)
        for p in two["pages"]:
            p["link"] = "接上一页：还在天台，男孩站在左边，手里捏着纸条"
        out, _ = A.audit(two, cap=4, group=True)
        self.assertFalse(any("没写 link" in x for x in out), out)


class GenerateGroupTest(unittest.TestCase):
    def test_group_uses_group_system_and_no_male_flag(self):
        systems, users = [], []

        def call(system, user, **kw):
            systems.append(system)
            users.append(user)
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
        # 每一次调用（含二轮审稿）都得带群像覆写段
        for s in systems:
            self.assertIn("群像模式", s)
        # users[0] 是成稿那次调用；最后一次是二轮审稿（2026-11-22），
        # 审稿提示里只有剧本 JSON，不再带原始 brief。
        self.assertIn("群像角色", users[0])
        self.assertIn("审稿", users[-1])
        self.assertEqual(sorted(spec["cast"]), ["athlete", "boy"])
        self.assertEqual(report["pages"][0]["n"], 1)

    def test_solo_unchanged(self):
        """不传 group：SYSTEM 原样、按男主/女主解析，行为与改动前一致。"""
        systems, users = [], []

        def call(system, user, **kw):
            systems.append(system)
            users.append(user)
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
        # 每一次调用（含二轮审稿）都必须用裸 SYSTEM，群像覆写不许漏进来
        for s in systems:
            self.assertNotIn("群像模式", s)
            self.assertEqual(s, A.SYSTEM)
        self.assertIn("只有她一个人", users[0])


class OutfitContinuityTest(unittest.TestCase):
    """每页 outfits：字段守卫 + 代码钉进页 caption（2026-11-22 P5/P6 穿帮）。"""

    @staticmethod
    def _page(beat="她扫了地"):
        return {
            "layout": "S1", "beat": beat,
            "summary": {"canvas": "single",
                        "caption": "A girl sweeping. " + A.STYLE_LOCK + " " + A.NO_TAIL},
            "items": [{"label": "character",
                       "caption": "a girl sweeping the floor",
                       "bbox": [0, 0, 100, 100]}],
            "bbox_coord_space": "abs_centralized", "bbox_format": "yxyx",
        }

    def test_audit_flags_missing_outfits_on_multi_page(self):
        js = {"cast": {"girl": "a girl about twelve, slender teen proportions, "
                               "long legs, small head, long dark hair, round eyes, "
                               "round face, a wrist band"},
              "pages": [self._page(), self._page()]}
        issues, _ = A.audit(js, cap=4)
        self.assertTrue(any("outfits" in x for x in issues), issues)

    def test_audit_passes_when_every_page_has_outfits(self):
        p1, p2 = self._page(), self._page("她换了衣服")
        p1["outfits"] = {"girl": "navy sailor top, navy pleated skirt"}
        p2["outfits"] = {"girl": "a plain white underlayer tank, wet skin"}
        js = {"cast": {"girl": "a girl about twelve, slender teen proportions, "
                               "long legs, small head, long dark hair, round eyes, "
                               "round face, a wrist band"},
              "pages": [p1, p2]}
        issues, _ = A.audit(js, cap=4)
        self.assertFalse(any("outfits" in x for x in issues), issues)

    def test_stamp_appends_outfit_state_to_caption(self):
        pj = {"summary": {"caption": "A girl in the shower."}}
        A._stamp_outfits(pj, {"girl": "a plain white underlayer tank, wet skin"})
        cap = pj["summary"]["caption"]
        self.assertIn("Outfit state for this page:", cap)
        self.assertIn("girl wears: a plain white underlayer tank", cap)
        # 原句还在（追加不覆盖）
        self.assertTrue(cap.startswith("A girl in the shower"))

    def test_stamp_ignores_empty_or_blank(self):
        for bad in ({}, None, {"girl": ""}, {"girl": "  "}):
            pj = {"summary": {"caption": "unchanged"}}
            A._stamp_outfits(pj, bad)
            self.assertEqual(pj["summary"]["caption"], "unchanged")


if __name__ == "__main__":
    unittest.main()
