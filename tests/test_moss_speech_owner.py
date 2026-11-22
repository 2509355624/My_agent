# -*- coding: utf-8 -*-
"""MOSS 台词归属：男主直角矩形方框 vs 女生气泡（2026-11-21，2026-11-22 改容器）。

用户实测事故：第一人称视角下男主不出镜，他的话却画成带尾巴气泡，
尾巴只能指向画面里唯一的女生 ——「她先回去了」被读成女生说的。

修法不是让代码猜哪句是谁说的（那是编剧的活），而是从**容器**上隔离：
女生用带尾巴的圆气泡，男主用无尾巴的直角矩形方框（2026-11-22 用户按
漫画台设计规则把「白边裸字」换掉——裸字没有边界，翻页时和拟声、网点糊成一团）。
落格仍按编剧写死的 cell。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.features.comic import moss_twopass as m


def _script(lines):
    return {
        "acts": [{"n": 1, "oneshot": "街角偶遇", "lines": lines,
                  "scene_text": "A street corner, a lamp, hard light.",
                  "narrs": []}],
        "cast": {"girl": "1girl, blonde"},
        "cast_text": "1girl, blonde",
        "arc": {},
    }


class SpeakerRuleInPromptTest(unittest.TestCase):
    def test_writer_judges_by_who_speaks(self):
        self.assertIn("看谁在开口，不看话里提到了谁", m.SCRIPT_SYSTEM)
        # 2026-11-21 POV 定调：男主是镜头后的人、绝不出镜；标「男」只是台词归属，
        # 不代表画面里出现男人。
        self.assertIn("镜头后面不出镜的「我」", m.SCRIPT_SYSTEM)
        self.assertIn("绝不代表画面里要出现一个男人", m.SCRIPT_SYSTEM)
        self.assertIn("只有剧情真的需要时才写", m.SCRIPT_SYSTEM)
        self.assertIn("写在他说那句的格子下面", m.SCRIPT_SYSTEM)

    def test_contract_separates_two_containers(self):
        self.assertIn("rectangular box", m.MOSS_CONTRACT)
        self.assertIn("no speech bubble", m.MOSS_CONTRACT.lower())
        self.assertIn("sharp square corners", m.MOSS_CONTRACT.lower())

    def test_summary_tail_names_both_containers(self):
        self.assertIn("plain rectangular box", m.TAIL_WITH_BOY)
        self.assertIn("girl's words in sharp speech bubbles", m.TAIL_WITH_BOY)

    def test_summary_tail_girl_only_has_no_boy(self):
        # 只有女生对话的页：尾句不许提 boy（用户 2026-11-21：右下角不必出现男主话）
        self.assertNotIn("boy", m.TAIL_GIRL_ONLY.lower())
        self.assertIn("no off-screen speaker", m.TAIL_GIRL_ONLY)

    def test_rebuild_summary_tail_depends_on_has_boy(self):
        js = {"items": []}
        m.rebuild_summary(js, {"girl": "1girl, black hair"}, "S1", True, "", has_boy=False)
        self.assertNotIn("boy", js["summary"]["caption"].lower())
        m.rebuild_summary(js, {"girl": "1girl, black hair"}, "S1", True, "", has_boy=True)
        self.assertIn("boy", js["summary"]["caption"].lower())

    def test_moss_user_marks_each_line_container(self):
        s = _script([{"who": "boy", "text": "她先回去了", "cell": 3},
                     {"who": "girl", "text": "你也在这", "cell": 2}])
        u = m.build_moss_user(s, 1, "", None, "L4")
        self.assertIn("直角矩形方框", u)
        self.assertIn("带尾巴气泡", u)
        self.assertIn("男主的话绝不许画成气泡", u)
        self.assertIn("她先回去了", u)


class ContainerKindTest(unittest.TestCase):
    def test_boy_caption_is_plain_not_bubble(self):
        cap = m.boy_text_cap("她先回去了")
        self.assertEqual(m._kind(cap), "plain")
        self.assertIn("boy speech box", cap.lower())
        self.assertIn("rectangular box", cap.lower())
        self.assertIn("no speech bubble", cap.lower())
        self.assertNotIn("pointer tail pointing", cap.lower())

    def test_girl_caption_is_still_bubble(self):
        cap = m.bubble_cap("你也在这")
        self.assertEqual(m._kind(cap), "bubble")
        self.assertIn("the girl's face", cap)

    def test_bubble_tail_names_the_speaker(self):
        # 两人同框时只写「speaker」，模型会把女生的尾巴画到男生脸上。
        cap = m.bubble_cap("我来拿", "boy")
        self.assertIn("the boy's face", cap)
        self.assertNotIn("the girl's face", cap)

    def test_bubble_tail_names_any_group_character(self):
        # 群像里说话的不止 girl/boy：军官等自由描述串也要进尾巴，
        # 否则两条气泡都默认 girl，台词连错人（异界前线 P2）。
        cap = m.bubble_cap("是我。情况怎么样？", "the commanding officer in a navy uniform")
        self.assertIn("pointing toward the face of the commanding officer in a navy uniform", cap)
        self.assertNotIn("the girl's face", cap)

    def test_bubble_tail_unknown_falls_back_to_girl(self):
        self.assertIn("the girl's face", m.bubble_cap("嗯", ""))

    def test_cell_slots_follow_script_not_speaker(self):
        # 「为什么」写在第 3 格，就不能因为说话人是谁被挪到别处。
        lines = [{"who": "girl", "text": "为什么", "cell": 3}]
        self.assertEqual(m.line_slots(lines, "L4"), [3])


class AutofixSpeakerTest(unittest.TestCase):
    def _js(self):
        return {"items": [
            {"label": "character", "caption": "1boy", "bbox": [0, 0, 100, 100]},
        ]}

    def test_missing_lines_get_right_container(self):
        js = self._js()
        lines = [{"who": "boy", "text": "她先回去了", "cell": 3},
                 {"who": "girl", "text": "你也在这", "cell": 2}]
        notes = m.autofix(js, cast={"girl": "1girl, blonde"}, lines=lines,
                          layout="L4", scene="A street corner, a lamp, hard light.")
        texts = {it["text"]: it for it in js["items"] if it.get("label") == "text"}
        self.assertEqual(m._kind(texts["她先回去了"]["caption"]), "plain")
        self.assertEqual(m._kind(texts["你也在这"]["caption"]), "bubble")
        self.assertTrue(any("补漏台词" in n for n in notes))

    def test_model_bubble_for_boy_is_rewritten(self):
        js = self._js()
        js["items"].append({
            "label": "text", "caption": m.bubble_cap("她先回去了"),
            "text": "她先回去了", "bbox": [10, 10, 30, 40],
        })
        lines = [{"who": "boy", "text": "她先回去了", "cell": 3}]
        notes = m.autofix(js, cast={"girl": "1girl, blonde"}, lines=lines,
                          layout="L4", scene="A street corner.")
        t = [it for it in js["items"] if it.get("label") == "text"][0]
        self.assertEqual(m._kind(t["caption"]), "plain")
        self.assertTrue(any("男主方框" in n for n in notes))

    def test_plain_and_bubble_boxes_do_not_overlap(self):
        cell = [0, 0, 100, 100]
        plain, bubble = m.bbox_plain(cell, 0), m.bbox_bubble(cell, 0)
        # 裸字在上、气泡在下：纵向区间不相交。
        self.assertLess(plain[2], bubble[0])


class AgentTailTest(unittest.TestCase):
    """单 Agent 链路（moss_agent）没有剧本台词表，尾句只能从已落进页面的容器认。

    2026-11-22 实测：容器从「白边裸字」改成「直角矩形方框」以后，尾句还在按
    旧关键字 `boy plain text` 认人，于是有男主方框的页也写成
    「no bare text, no off-screen speaker」——画面上有一个方框，指令说没有画外人。
    """

    def _page(self, texts):
        cap = ("A single panel page. Manga page, monochrome, greyscale screentones. "
               "Flat screentone shading, clear Chinese text: the girl's words in sharp "
               "speech bubbles with short pointer tails; no bare text, no off-screen speaker.")
        items = [{"label": "character", "bbox": [-768, -494, 768, 494],
                  "caption": "a girl about twelve, long dark hair, monochrome greyscale screentone"}]
        for i, (txt, caption) in enumerate(texts):
            items.append({"label": "text", "text": txt, "caption": caption,
                          "bbox": [-400 + i * 200, 300, -200 + i * 200, 500]})
        return {"summary": {"canvas": "single", "caption": cap}, "items": items}

    def test_tail_notices_the_boy_box(self):
        js = self._page([("衣服给我", m.boy_text_cap("衣服给我"))])
        m.autofix(js, cast={"girl": "a girl"}, layout="S1", scene="",
                  audit_narr=False, rebuild=False)
        self.assertIn("off-screen boy", js["summary"]["caption"])

    def test_tail_stays_girl_only_without_boy_box(self):
        js = self._page([("谢谢你", m.bubble_cap("谢谢你"))])
        m.autofix(js, cast={"girl": "a girl"}, layout="S1", scene="",
                  audit_narr=False, rebuild=False)
        self.assertNotIn("off-screen boy", js["summary"]["caption"])


if __name__ == "__main__":
    unittest.main()
