# -*- coding: utf-8 -*-
"""引用一张出好的图 = 引用那串提示词，不是图生图（用户 2026-11-27 拍板）。

用户原话：「引用的其实是那一个提示词……然后我说我的需求，比如把这个衣服
换成 JK 服，那么 AI 它就直接去改提示词，然后重新跑一遍就可以了」。

现场 bug：引用一张 `anima-line` 出的图 + 「换成正面的视角」，工作台把请求
送进 `_revise`，模型判 `source_image=1`，`_build_i2i` 因为没有 i2i 骨架直接
抛出「「anima-line」不支持图生图，换一个画风再试」（logs/agent.log
2026-11-27 19:38:44）。这条路本来就不该存在——引用是改词重画，不是垫图。
"""
import io
import os
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
WS = os.path.join(ROOT, "web", "workspace")


def _read(*parts):
    with io.open(os.path.join(WS, *parts), "r", encoding="utf-8") as f:
        return f.read()


def _seg(text, start, end):
    i = text.index(start)
    return text[i:text.index(end, i)]


class TestQuoteCarriesPromptOnTheFrontend(unittest.TestCase):
    """前端只交提示词：不传图、不进 refs、不碰识图标记。"""

    def test_quote_card_records_prompt_not_bytes(self):
        js = _read("js", "ref.js")
        seg = _seg(js, "function quoteCard", "function init")
        self.assertIn("S.quote = { file: file, prompt: prompt }", seg)
        for forbidden in ("chatUpload", "addRef", "fetch(", "toDataUrl"):
            self.assertNotIn(forbidden, seg, "引用不该做这件事：%s" % forbidden)

    def test_quote_prompt_is_readable_and_clearable(self):
        js = _read("js", "ref.js")
        self.assertIn("function quotePrompt()", js)
        self.assertIn("quotePrompt: quotePrompt", js)
        self.assertIn("clearQuote", js)

    def test_submit_stores_anchor_and_clears_the_bar(self):
        js = _read("js", "gen_flow.js")
        seg = _seg(js, "function submit(", "function continueSubmit")
        self.assertIn("global.WSRef.quotePrompt", seg)
        self.assertIn("anchor: anchor", seg)
        self.assertIn("clearQuote", seg)

    def test_anchor_only_goes_on_the_ask_route(self):
        """`submit`（翻译直通 / 重画）不吃 anchor，只有聊天页那句话才吃。"""
        js = _read("js", "gen_flow.js")
        seg = _seg(js, "function fire(", "var sent =")
        self.assertIn("if (rec.anchor) body.anchor = rec.anchor", seg)
        i_ask = seg.index("if (ask)")
        self.assertLess(i_ask, seg.index("if (rec.anchor)"),
                        "anchor 必须挂在 ask 分支里")


class TestQuoteSkipsImageToImage(unittest.TestCase):
    """后端有 anchor 就走 `_translate`，绝不进 `_revise`。"""

    def _decide(self, ctx, skill="anima-line"):
        from app.features.image import routes as r
        from app.features.image import direct_gen as dg
        calls = {}

        def fake_translate(text, hist, **kw):
            calls["anchor"] = kw.get("anchor")
            return {"skill": skill, "prompt": "1girl, front view, jk uniform"}

        def fake_revise(*a, **kw):
            calls["revise"] = True
            return {"skill": skill, "prompt": "x"}

        def fake_enqueue(body, **kw):
            calls["body"] = body
            return dict(body, id="J1", ts=0, state="queued"), ""

        with mock.patch.object(dg, "_translate", fake_translate), \
                mock.patch.object(dg, "_llm_target", lambda *a, **k: (None, None, False)), \
                mock.patch.object(r, "_web_revise", fake_revise), \
                mock.patch.object(r, "_owner_key", lambda: "__test__"), \
                mock.patch.object(r, "_enqueue_job", fake_enqueue):
            r._web_decide("把这件衣服换成 JK 服", skill, [], ctx)
        return calls

    def test_anchor_goes_through_translate(self):
        calls = self._decide({"images": ["data:image/png;base64,AAA"],
                              "anchor": "1girl, back view, white dress"})
        self.assertEqual(calls.get("anchor"), "1girl, back view, white dress")
        self.assertNotIn("revise", calls, "有 anchor 就不该走改图管道")
        self.assertNotIn("source_images", calls.get("body") or {},
                         "引用不该带图生图源图")

    def test_uploaded_image_still_uses_revise(self):
        """相册上传（没有 anchor）保持原样：它才是真的「在图上报」。"""
        calls = self._decide({"images": ["data:image/png;base64,AAA"]})
        self.assertTrue(calls.get("revise"))


class TestAnchorBecomesPreviousPrompt(unittest.TestCase):
    """anchor 要塞进主模板的【上一张】，并且压过内存 / 账本里那条。"""

    def test_template_sees_the_anchor_and_not_the_stale_job(self):
        from app.features.image import direct_gen as dg
        seen = {}

        def fake_ask(content):
            seen["content"] = content
            return {"skill": "anima-line", "prompt": "1girl, front view"}

        with mock.patch.object(dg, "_ask", fake_ask), \
                mock.patch.object(dg, "_last_job",
                                  lambda *a, **k: {"skill": "jank",
                                                   "prompt": "STALE-JOB-PROMPT"}):
            got = dg._translate_body("把这件衣服换成 JK 服", [],
                                     skill="anima-line",
                                     anchor="1girl, back view, white dress")
        self.assertEqual(got["prompt"], "1girl, front view")
        self.assertIn("1girl, back view, white dress", seen["content"])
        self.assertNotIn("STALE-JOB-PROMPT", seen["content"],
                         "用户引用的那张优先，别拿别的上一张顶")

    def test_no_anchor_keeps_reading_the_last_job(self):
        from app.features.image import direct_gen as dg
        seen = {}

        def fake_ask(content):
            seen["content"] = content
            return {"skill": "silver", "prompt": "1girl"}

        with mock.patch.object(dg, "_ask", fake_ask), \
                mock.patch.object(dg, "_last_job",
                                  lambda *a, **k: {"skill": "silver",
                                                   "prompt": "LAST-JOB-PROMPT"}):
            dg._translate_body("再来一张", [], skill="silver")
        self.assertIn("LAST-JOB-PROMPT", seen["content"])

    def test_long_anchor_is_not_cut_short(self):
        """引用的是「那一个提示词」，不能按「上一张」那 200 字截掉一半。"""
        from app.features.image import direct_gen as dg
        seen = {}
        long_tags = "1girl, " + ", ".join("tag%d" % i for i in range(120))
        self.assertGreater(len(long_tags), 300)

        def fake_ask(content):
            seen["content"] = content
            return {"skill": "anima-line", "prompt": "1girl"}

        with mock.patch.object(dg, "_ask", fake_ask):
            dg._translate_body("换成 JK", [], skill="anima-line", anchor=long_tags)
        self.assertIn(long_tags[-40:], seen["content"], "整串都要给模型看到")


if __name__ == "__main__":
    unittest.main()
