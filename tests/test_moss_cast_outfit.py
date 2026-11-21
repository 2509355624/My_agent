"""2026-11-21：cast 只写脸和身材，衣服按页走。

用户实测：给的角色外形串里带着一条连衣裙，编剧 AI 把它抄进 cast，于是
五页全是连衣裙——剧情让她脱掉短袖换新的，画出来她还在穿裙子（用户原话
「换装一直失败」）。

根因不是模型笨，是提示词写了一整套反向规则：
  · cast 是唯一事实源，每一格逐字抄
  · 衣物以 cast 为基准，只写已经发生的变化
  · audit 少抄一项就报错重修（于是它把裙子抄满每一页）
本文件锁住修好之后的三条：cast 不许出现衣物；衣服写进本页 ③；补外形串
时也不许把衣服塞回去。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.features.comic import moss_agent as A  # noqa: E402

GIRL = ("a girl about twelve, slender small frame, long straight dark hair with "
        "soft blunt bangs pulled back by a wide fluffy pale cloth headband, large "
        "round dark eyes, round soft face, faint blush, a dark fabric scrunchie "
        "on her left wrist")


def _page(caption):
    return {"layout": "S1",
            "summary": {"canvas": "single", "caption": caption},
            "items": [
                {"label": "character", "bbox": [-768, -494, 768, 494],
                 "caption": caption},
                {"label": "text", "bbox": [-300, 300, -100, 700], "text": "好",
                 "caption": "Vertical rounded speech bubble"},
            ]}


class TestSystemSaysCastHasNoClothes(unittest.TestCase):
    def test_cast_section_exists(self):
        s = A.SYSTEM
        self.assertIn("cast.girl 只写不变项", s)
        self.assertIn("衣服一进 cast", s)
        self.assertIn("换装", s)

    def test_old_rule_is_gone(self):
        # 旧规则「衣物以 cast 为基准」正是锁死连衣裙的那一句，不许回来
        self.assertNotIn("衣物以 cast 为基准", A.SYSTEM)

    def test_cast_example_has_no_wearing(self):
        # 示例串本身就是模型模仿的对象，里面不能有 wearing
        seg = A.SYSTEM.split("════ cast 长这样")[1].split("════ 一页长这样")[0]
        self.assertIn('"cast"', seg)
        self.assertNotIn("wearing", seg.lower())

    def test_outfit_goes_into_summary_line3(self):
        s = A.SYSTEM
        self.assertIn("本页衣着", s)


class TestAuditFlagsClothesInCast(unittest.TestCase):
    def test_wearing_in_cast_is_blocking(self):
        js = {"cast": {"girl": GIRL + ", wearing a plain cotton dress"},
              "pages": [_page("a girl standing on the track")]}
        out, _ = A.audit(js, 1)
        self.assertTrue(any("cast 里写了衣服" in x for x in out), out)

    def test_clean_cast_passes_that_check(self):
        js = {"cast": {"girl": GIRL},
              "pages": [_page("a girl standing on the track")]}
        out, _ = A.audit(js, 1)
        self.assertFalse(any("cast 里写了衣服" in x for x in out), out)

    def test_dressed_in_also_flagged(self):
        js = {"cast": {"girl": GIRL + ", dressed in a school uniform"},
              "pages": [_page("a girl standing on the track")]}
        out, _ = A.audit(js, 1)
        self.assertTrue(any("cast 里写了衣服" in x for x in out), out)


class TestAutofixDoesNotPutClothesBack(unittest.TestCase):
    def test_pasted_spec_carries_no_clothes(self):
        """补外形串是代码行为，它只能补 cast 里有的东西——cast 干净，它就干净。"""
        from app.features.comic import moss_twopass as M
        js = {"summary": {"canvas": "single"},
              "items": [{"label": "character",
                         "bbox": [-768, -494, 768, 494],
                         "caption": "Full body shot, she stands on the track, "
                                    "monochrome greyscale screentone"}]}
        M.autofix(js, {"girl": "a girl about twelve, long straight dark hair"},
                  None, "S1", "", audit_narr=False, rebuild=False)
        cap = js["items"][0]["caption"]
        self.assertIn("a girl about twelve", cap)
        self.assertNotIn("wearing", cap.lower())


if __name__ == "__main__":
    unittest.main()
