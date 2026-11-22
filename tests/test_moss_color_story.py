# -*- coding: utf-8 -*-
"""2026-11-22 全彩化 + 剧情 beat：颜色不许漂，剧情不许是流水账。

彩色化不是加一句「full color」就完：旧链路的画风锁写死 monochrome/no color，
而衣服串里保留着 navy / pale blue，结果就是「衣服有色、背景黑白」的局部留色漫。
这里锁的是那条规矩本身——两个链路的常量都不能再出现写死的黑白。

配色表由编剧全本定一次、代码逐页原文接上；转折/冲突是可选输入，
填了照它排 beat，没填让编剧自己设计一个意外。
"""
import unittest

from app.features.comic import moss_agent as A
from app.features.comic import moss_twopass as M
from app.features.comic import moss_comic as C


class FullColorTest(unittest.TestCase):
    def test_both_style_locks_are_full_color(self):
        for name, lock in (("twopass.STYLE_LOCK", M.STYLE_LOCK),
                           ("agent.STYLE_LOCK", A.STYLE_LOCK),
                           ("twopass.STYLE_ITEM", M.STYLE_ITEM)):
            low = lock.lower()
            self.assertIn("color", low, name)
            self.assertIn("cel shading", low, name)
            # 只许作为否定的反声明出现（no screentone / no monochrome），
            # 不许再是「整页是黑白的」这种正向要求
            self.assertNotIn("no color cast", low, name)
            self.assertNotIn("screentones, inked lineart", low, name)

    def test_system_no_longer_asks_for_black_and_white(self):
        self.assertIn("全彩日漫", A.SYSTEM)
        self.assertNotIn("你写黑白", A.SYSTEM)
        self.assertNotIn("只写明暗、材质、网点，不写颜色词", A.SYSTEM)
        self.assertIn("palette", A.SYSTEM)
        self.assertIn("颜色写具体词", A.SYSTEM)

    def test_text_tails_are_color_not_screentone(self):
        for tail in (M.TAIL_GIRL_ONLY, M.TAIL_WITH_BOY):
            self.assertIn("Clean flat anime coloring", tail)
            self.assertNotIn("screentone", tail.lower())
        self.assertIn("Clean flat anime coloring", C._NO_TEXT_TAIL)

    def test_meta_tags_are_color(self):
        self.assertEqual(M.STYLE_LOCK_KEYS,
                         ("cel shading", "anime coloring", "black ink outlines"))
        self.assertIn("cel_shading", A.EXAMPLE["summary"]["meta"])

    def test_old_screentone_tail_is_still_rewritten(self):
        """改彩色之前写好的 spec 也要能把文字尾句剥干净。"""
        import json
        ov = {"summary": {"canvas": "single", "caption":
                          "Manga page. a girl. Flat screentone shading, clear Chinese text: hello."},
              "items": [{"label": "text", "text": "你好", "caption": "bubble"}]}
        spec = {"pages": [{"prompt_override": json.dumps(ov, ensure_ascii=False)}]}
        out = json.loads(C._strip_text_items(spec["pages"][0]["prompt_override"]))
        self.assertNotIn("clear Chinese text", out["summary"]["caption"])
        self.assertIn("no text", out["summary"]["caption"])


class PaletteTest(unittest.TestCase):
    def test_palette_stamped_verbatim(self):
        pj = {"summary": {"caption": "A full-color page. she sweeps. clean cel shading."}}
        A._stamp_palette(pj, "navy blue top, pale blue collar, off-white headband")
        self.assertEqual(
            pj["summary"]["caption"],
            "A full-color page. she sweeps. clean cel shading. "
            "Fixed palette for every panel: navy blue top, pale blue collar, "
            "off-white headband.")

    def test_palette_missing_does_not_invent_colors(self):
        pj = {"summary": {"caption": "A full-color page."}}
        A._stamp_palette(pj, "")
        self.assertEqual(pj["summary"]["caption"], "A full-color page.")

    def test_audit_warns_when_palette_missing(self):
        js = {"cast": {"girl": "a girl about twelve, slender teen proportions, "
                                "long legs, small head, dark hair, eyes, face, scrunchie"},
              "pages": [{"layout": "S1", "beat": "灯绳断了",
                         "summary": {"caption": "x", "canvas": "single"},
                         "items": [{"label": "character", "bbox": [-768, -494, 768, 494],
                                    "caption": "a girl"}]}]}
        _bad, warns = A.audit(js, cap=3)
        self.assertTrue(any("palette" in w for w in warns))


class StoryBeatTest(unittest.TestCase):
    def test_parse_twist_line(self):
        p = A.parse_brief("男主：无\n女主：x\n剧情：y\n转折：灯突然灭了，她不敢喊人")
        self.assertEqual(p["twist"], "灯突然灭了，她不敢喊人")
        self.assertEqual(p["plot"], "y")
        self.assertFalse(p["male_none"] is None)

    def test_twist_brackets_and_halfwidth(self):
        p = A.parse_brief("剧情：y\n【转折】：她把钥匙弄丢了")
        self.assertEqual(p["twist"], "她把钥匙弄丢了")

    def test_old_three_sections_have_empty_twist(self):
        p = A.parse_brief("男主：无\n女主：x\n剧情：y")
        self.assertEqual(p["twist"], "")

    def test_compose_obeys_the_given_twist(self):
        out = A.compose_brief(A.parse_brief("剧情：y\n转折：灯绳断了"))
        self.assertIn("必须出现，不许绕开", out)
        self.assertIn("灯绳断了", out)

    def test_compose_designs_one_when_blank(self):
        out = A.compose_brief(A.parse_brief("剧情：y"))
        self.assertIn("转折没指定", out)
        self.assertIn("自己设计", out)

    def test_system_requires_beat_and_forbids_action_list(self):
        self.assertIn("先排 beat", A.SYSTEM)
        self.assertIn("不算 beat", A.SYSTEM)
        self.assertIn("相邻两页的 beat 不许", A.SYSTEM)

    def test_audit_warns_when_beat_missing(self):
        js = {"palette": "navy blue top, cream walls",
              "cast": {"girl": "a girl about twelve, slender teen proportions, "
                                "long legs, small head, dark hair, eyes, face, scrunchie"},
              "pages": [{"layout": "S1",
                         "summary": {"caption": "x", "canvas": "single"},
                         "items": [{"label": "character", "bbox": [-768, -494, 768, 494],
                                    "caption": "a girl"}]}]}
        _bad, warns = A.audit(js, cap=3)
        self.assertTrue(any("beat" in w for w in warns))


class OutlineTest(unittest.TestCase):
    """2026-11-22：分集大纲可选，填了严格一页一条；不填走五段式。"""

    def test_parse_outline_by_page_number(self):
        brief = ("男主：无\n女主：x\n剧情：被困雪山\n"
                 "分集大纲：\n第1页：两人登山\n2：雪崩逃跑\n10：下山回家")
        got = A.parse_outline(brief)
        self.assertEqual([n for n, _ in got], [1, 2, 10])
        self.assertEqual(got[0][1], "两人登山")

    def test_parse_outline_ignores_plot_colons(self):
        """剧情行里的「1：」不能被误当大纲。"""
        brief = "剧情：1：这不是大纲\n2：也不是"
        self.assertEqual(A.parse_outline(brief), [])

    def test_parse_outline_empty_is_falsy(self):
        self.assertEqual(A.parse_outline(""), [])
        self.assertEqual(A.parse_outline("分集大纲：\n"), [])

    def test_outline_does_not_pollute_twist_or_plot(self):
        """大纲段拼在 brief 末尾，不能被 parse_brief 当成转折的一部分。"""
        brief = ("男主：无\n女主：x\n剧情：被困雪山\n转折：柴火用完\n"
                 "分集大纲：\n1：登山\n2：雪崩")
        p = A.parse_brief(brief)
        self.assertEqual(p["twist"], "柴火用完")
        self.assertEqual(p["plot"], "被困雪山")
        g = A.parse_group("群像角色：\nboy: a boy\n剧情：登山\n"
                          "分集大纲：\n1：登山\n2：雪崩")
        self.assertEqual(g["plot"], "登山")

    def test_compose_outline_forces_one_line_one_page(self):
        out = A.compose_outline([(1, "登山"), (2, "雪崩")])
        self.assertIn("第1页：登山", out)
        self.assertIn("第2页：雪崩", out)
        self.assertIn("不许合并", out)

    def test_with_outline_appends_and_keeps_original(self):
        ctx = "== 这一集发生什么 ==\n被困雪山"
        out = A.with_outline(ctx, [(1, "登山")])
        self.assertTrue(out.startswith(ctx))
        self.assertIn("第1页：登山", out)
        self.assertEqual(A.with_outline(ctx, []), ctx)

    def test_system_has_span_and_limb_rules(self):
        self.assertIn("中段每一页必须落在一个具体事件上", A.SYSTEM)
        self.assertIn("分集大纲", A.SYSTEM)
        self.assertIn("脚是脚、手是手、身体连着腿", A.SYSTEM)
        self.assertIn("one boot pressed into the embers", A.SYSTEM)


if __name__ == "__main__":
    unittest.main()
