"""MOSS 输入：谁在这本里（男主 / 女主 / 剧情），2026-11-22。

用户原话：剧情的部分可以参考角色扮演；moss 漫画更多通过镜头表现剧情；
男主的发言不一定要一直出现，可以只有女主。按旧漫画工作台的形式就是
`男主：` / `女主：` / `剧情：`，男主可以写「无」，写了「无」就全程只有女主。

背景：《泥坑之后，热水之前》剧情里压根没有男主，编剧自己塞了
「我来我来，先回家」和「衣服放门口了」两句——旧提示词只教了「男主的话
怎么画」，从没说过「这一集可以没有他」。
"""

import unittest

from app.features.comic import moss_agent as A


class ParseBriefTest(unittest.TestCase):
    def test_three_sections(self):
        p = A.parse_brief("男主：无\n女主：a girl about twelve\n剧情：她在雨里摔倒，自己走回家。")
        self.assertEqual(p["male"], "无")
        self.assertEqual(p["female"], "a girl about twelve")
        self.assertEqual(p["plot"], "她在雨里摔倒，自己走回家。")
        self.assertTrue(p["labeled"])
        self.assertTrue(p["male_none"])

    def test_halfwidth_colon_and_brackets(self):
        for text in ("男主: 无\n女主: x\n剧情: y",
                     "【男主】无\n【女主】x\n【剧情】y",
                     "**男主**：无\n**女主**：x\n**剧情**：y",
                     "## 男主：无\n女主：x\n剧情：y"):
            p = A.parse_brief(text)
            self.assertEqual(p["male"], "无", text)
            self.assertEqual(p["female"], "x", text)
            self.assertEqual(p["plot"], "y", text)

    def test_none_spellings(self):
        for word in ("无", "无男主", "没有", "none", "None", "N/A", "—", "空"):
            self.assertTrue(A.parse_brief("男主：%s\n剧情：y" % word)["male_none"], word)
        # 空的「男主：」不算「没有他」——那只是没填，不能替用户决定
        self.assertFalse(A.parse_brief("男主：\n剧情：y")["male_none"])
        self.assertFalse(A.parse_brief("男主：我（画外）\n剧情：y")["male_none"])

    def test_multiline_value_belongs_to_its_label(self):
        p = A.parse_brief("剧情：第一句\n第二句还在剧情里\n女主：x")
        self.assertEqual(p["plot"], "第一句\n第二句还在剧情里")
        self.assertEqual(p["female"], "x")

    def test_old_plain_brief_is_still_supported(self):
        raw = "她在雨里摔进泥坑，自己走回家，脱鞋洗澡。"
        p = A.parse_brief(raw)
        self.assertFalse(p["labeled"])
        self.assertFalse(p["male_none"])
        self.assertEqual(p["plot"], raw)
        # 旧格式不能被包一层「== 这一本里有谁 ==」
        self.assertEqual(A.compose_brief(p, fallback=raw), raw)


class ComposeBriefTest(unittest.TestCase):
    def test_no_male_says_she_is_the_only_person(self):
        out = A.compose_brief(A.parse_brief("男主：无\n女主：x\n剧情：y"))
        self.assertIn("只有她一个人", out)
        self.assertIn("不写男方框", out)
        self.assertIn("镜头后面的我", out)
        self.assertIn("== 这一集发生什么 ==", out)
        self.assertNotIn("（画外，不出镜）", out)

    def test_male_present_is_off_screen_and_optional(self):
        out = A.compose_brief(A.parse_brief("男主：我\n女主：x\n剧情：y"))
        self.assertIn("男主（画外，不出镜）：我", out)
        self.assertIn("整本一句不说完全正常", out)
        self.assertNotIn("只有她一个人", out)

    def test_male_blank_is_not_declared_absent(self):
        out = A.compose_brief(A.parse_brief("女主：x\n剧情：y"))
        self.assertIn("这一集没指定", out)


JS_NO_TEXT = {
    "title": "泥坑之后",
    "cast": {"girl": "a girl about twelve, slender small frame, long straight dark hair "
                     "with soft blunt bangs pulled back by a wide fluffy pale cloth headband, "
                     "large round dark eyes, round soft face, faint blush, "
                     "a dark fabric scrunchie on her left wrist"},
    "scenes": {"1": "a rain-soaked alley"},
    "pages": [],
}

CHAR = ("medium shot, a girl about twelve, slender small frame, long straight dark hair "
        "with soft blunt bangs pulled back by a wide fluffy pale cloth headband, large round "
        "dark eyes, round soft face, faint blush, a dark fabric scrunchie on her left wrist, "
        "wearing a soaked white sailor blouse, monochrome greyscale screentone")


def _page(caption):
    return {"layout": "S1", "scene": "1",
            "summary": {"canvas": "single",
                        "caption": "A black-and-white manga page. Manga page, monochrome, "
                                   "greyscale screentones. " + caption},
            "items": [
                {"label": "character", "caption": CHAR, "bbox": [-768, -494, 768, 494]},
                {"label": "text", "caption": caption, "text": "先回家",
                 "bbox": [-300, 200, -60, 480]},
            ]}


BOY = ("Boy speech box: plain rectangular box with a thin black ink outline, "
       "sharp square corners, no bubble tail, no pointer, Chinese text written vertically")
GIRL = ("Vertical rounded speech bubble with a thin black ink outline, Chinese text written "
        "vertically with each character stacked top to bottom, short pointer tail")


class NoMaleAuditTest(unittest.TestCase):
    def _run(self, caption, male_none):
        js = dict(JS_NO_TEXT, pages=[_page(caption)])
        issues, _ = A.audit(js, 1, male_none=male_none)
        return issues

    def test_boy_box_when_no_male_is_blocking(self):
        issues = self._run(BOY, True)
        self.assertTrue(any("这一集没有男主" in x for x in issues), issues)

    def test_boy_box_is_fine_when_male_exists(self):
        issues = self._run(BOY, False)
        self.assertFalse(any("这一集没有男主" in x for x in issues), issues)

    def test_girl_bubble_is_fine_when_no_male(self):
        issues = self._run(GIRL, True)
        self.assertFalse(any("这一集没有男主" in x for x in issues), issues)

    def test_default_keeps_old_behaviour(self):
        # 不传 male_none 时不该因为男方框拦人（旧调用点不用改）
        issues, _ = A.audit(dict(JS_NO_TEXT, pages=[_page(BOY)]), 1)
        self.assertFalse(any("这一集没有男主" in x for x in issues), issues)


class SystemPromptTest(unittest.TestCase):
    def test_prompt_says_a_story_can_have_no_male_lead(self):
        s = A.SYSTEM
        self.assertIn("这一本里有谁", s)
        self.assertIn("没有男主的戏，就是没有", s)
        self.assertIn("只有她一个人", s)

    def test_line_count_is_a_ceiling_not_a_quota(self):
        s = A.SYSTEM
        self.assertIn("这些是上限，不是配额", s)
        self.assertIn("整本一句台词都没有也成立", s)
        # 旧的硬下限必须清掉：五格页 3~5 句会逼模型拉人凑台词
        self.assertNotIn("五格页 3~5 句", s)

    def test_camera_section_tells_it_how_to_show_instead_of_tell(self):
        s = A.SYSTEM
        self.assertIn("用镜头讲，别用字讲", s)
        self.assertIn("建场（远）", s)
        self.assertIn("局部（特写）", s)
        self.assertIn("S1 一格只在", s)


if __name__ == "__main__":
    unittest.main()
