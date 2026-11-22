# -*- coding: utf-8 -*-
"""2026-11-22 实测四条：衣服要写清、内搭要接得上、台词跟动作、不许独白。

用户实拍《湿透的短袖》五页，提了六个问题，能落到提示词的是这四条：

1. 第一页是黑运动内衣、第三页变白背心 —— 湿透的外衣没写内搭，模型每页自己配一件。
2. 第三页「给你」画在了还没递东西的那一格 —— 台词没跟动作对齐。
3. 第五页衬衣没有扣子、还换成了 polo —— 衣服只写 "a shirt"，款式全靠模型编。
4. 第五页「下次还你一件」是她的内心独白 —— 女生不该有独白，只有气泡。

锁住提示词里对应的四句话，防止以后改提示词时被顺手删掉。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.features.comic import moss_agent as A  # noqa: E402


class ClothesNeedStyleDetailTest(unittest.TestCase):
    def test_every_garment_names_its_cut(self):
        s = A.SYSTEM
        self.assertIn("每件衣服都写清款式", s)
        self.assertIn("buttoned placket", s)
        # 只写 "a white shirt" 会被模型自己编一件
        self.assertIn("只写 a white shirt", s)

    def test_same_garment_uses_same_words_across_pages(self):
        self.assertIn("同一件衣服跨页用同一串词", A.SYSTEM)
        self.assertIn("shirt / blouse / polo", A.SYSTEM)


class InnerLayerContinuityTest(unittest.TestCase):
    def test_wet_outer_must_declare_inner_layer(self):
        s = A.SYSTEM
        self.assertIn("第一次出现就把里面那一层写清", s)
        self.assertIn("underlayer tank", s)
        # 反例要写出来：黑运动内衣 → 白背心，就是这条要防的
        self.assertIn("黑运动内衣", s)

    def test_inner_layer_is_not_in_cast(self):
        # 内搭属于「这一身」，不是「这个人」——cast 里仍然一个字衣服都不许有
        seg = A.SYSTEM.split("════ cast 长这样")[1].split("════ 一页长这样")[0]
        self.assertNotIn("wearing", seg.lower())


class LineFollowsTheActionTest(unittest.TestCase):
    def test_line_lands_where_the_action_happens(self):
        s = A.SYSTEM
        self.assertIn("台词跟动作对齐", s)
        self.assertIn("已经离手", s)
        # 一句台词只出现一次，不许两格各放一遍
        self.assertIn("一句台词只出现一次", s)


class NoInnerMonologueTest(unittest.TestCase):
    def test_only_two_containers_exist(self):
        s = A.SYSTEM
        # 2026-11-22 起是「最多两种」：没有男主的一集只有她的圆气泡。
        self.assertIn("这一集最多两种字", s)
        self.assertIn("没有旁白框", s)
        self.assertIn("rectangular caption box", s)   # 明确点名不要它

    def test_item_rule_forbids_her_thoughts_as_text(self):
        s = A.SYSTEM
        self.assertIn("一个字都不许上画面", s)
        self.assertIn("不写独白、不写旁白", s)

    def test_boy_line_is_a_direct_request(self):
        # 2026-11-22 用户：男主说「有换的吗」不如说「衣服给我」——反问把动作推回给她
        s = A.SYSTEM
        self.assertIn("写成一句直接的要求", s)
        self.assertIn("有换的吗", s)

    def test_caption_box_no_longer_feeds_her_inner_voice(self):
        # 旧文案把「她心里那句话」当成方框的正当内容，正是独白的来源
        self.assertNotIn("她心里那句话），或者她接下来要做什么", A.SYSTEM)


class AuditPolicingTest(unittest.TestCase):
    def _page(self, text_caption, text="好大", caption=None):
        return {"layout": "S1",
                "summary": {"canvas": "single",
                            "caption": caption or (
                                "A single panel page. Manga page, monochrome, greyscale "
                                "screentones, inked lineart, bold black ink outlines, pen "
                                "crosshatching. a girl about twelve, long straight dark hair, "
                                "wearing a plain white underlayer tank. A washroom. "
                                "Flat screentone shading, clear Chinese text: the girl's words "
                                "in sharp speech bubbles with short pointer tails; no bare text, "
                                "no off-screen speaker.")},
                "items": [
                    {"label": "character", "bbox": [-768, -494, 768, 494],
                     "caption": "a girl about twelve, long straight dark hair, wearing a "
                                "plain white underlayer tank. monochrome greyscale screentone"},
                    {"label": "text", "bbox": [0, 0, 200, 200], "text": text,
                     "caption": text_caption},
                ]}

    def test_narration_box_is_blocking(self):
        from app.features.comic import moss_twopass as m
        js = {"cast": {"girl": "a girl about twelve"}, "pages": [
            self._page(m.CAPTION_BOX, text="浑身都湿了")]}
        out, _ = A.audit(js, 1)
        self.assertTrue(any("旁白框" in x for x in out), out)

    def test_boy_box_is_allowed(self):
        from app.features.comic import moss_twopass as m
        js = {"cast": {"girl": "a girl about twelve"}, "pages": [
            self._page(m.boy_text_cap("衣服给我"), text="衣服给我")]}
        out, _ = A.audit(js, 1)
        self.assertFalse(any("旁白框" in x for x in out), out)

    def test_age_drift_is_blocking(self):
        from app.features.comic import moss_twopass as m
        page = self._page(m.bubble_cap("好大"))
        page["items"][0]["caption"] = ("a girl about eighteen, long straight dark hair, "
                                       "wearing a plain white underlayer tank. "
                                       "monochrome greyscale screentone")
        js = {"cast": {"girl": "a girl about twelve"}, "pages": [page]}
        out, _ = A.audit(js, 1)
        self.assertTrue(any("年龄写成" in x for x in out), out)

    def test_same_age_is_fine(self):
        from app.features.comic import moss_twopass as m
        js = {"cast": {"girl": "a girl about twelve"}, "pages": [self._page(m.bubble_cap("好大"))]}
        out, _ = A.audit(js, 1)
        self.assertFalse(any("年龄写成" in x for x in out), out)


if __name__ == "__main__":
    unittest.main()
