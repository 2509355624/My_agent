# -*- coding: utf-8 -*-
"""MOSS 双段漫画：页间接续 + 逐格旁白（2026-11-21）。

钉死三件刚修好的事，防止回退到「每页各讲各的、旁白只画第一句」：

  * 编剧提示词要求每页最后一格停在未完成动作、下一页第一格接住；
    旧禁令「不写页尾钩子」不得回来（那正是割裂的根因）。
  * parse_script 把每一格的旁白收进 narrs（带格号），不再只留第一句。
  * autofix 按 narrs 逐格补旁白框：剧本写了两句，画面就有两个 caption。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.features.comic import moss_twopass as m


class ContinuityInPromptTest(unittest.TestCase):
    def test_system_prompt_requires_page_to_page_continuity(self):
        self.assertIn("页和页要接起来", m.SCRIPT_SYSTEM)
        self.assertIn("每页最后一格停在一个没做完的动作上", m.SCRIPT_SYSTEM)
        self.assertIn("下一页第一格直接接住那个动作", m.SCRIPT_SYSTEM)
        self.assertIn("地点只往前走，不许折返", m.SCRIPT_SYSTEM)

    def test_old_no_hook_ban_is_gone(self):
        # 豆包把「不写页尾钩子」当笔记删掉，正是割裂根因；不得回退。
        self.assertNotIn("不写页尾钩子", m.SCRIPT_SYSTEM)
        self.assertNotIn("不要写页尾钩子", m.build_script_user("测试", 4))

    def test_brief_prompt_restates_continuity(self):
        user = m.build_script_user("冬日偶遇", 4)
        self.assertIn("页和页要接成一条线", user)
        self.assertIn("下一页第一格从那儿接着画", user)


class PerCellNarrParseTest(unittest.TestCase):
    SCRIPT = """# 热可可
## 角色
- girl: 1girl, blonde
## 场景 1
A street corner, a lamp, hard light.
## 页 1
格1｜远景｜街
画面：他沿人行道走，呼出白气。
台词：无
旁白：「今天比平时冷。」
格2｜中景｜他
画面：他低头看手机。
台词：无
旁白：「妹妹发来消息，说她闺蜜在附近。」
格3｜近景｜便利店门
画面：门被推开一条缝。
台词：女「啊——！你也在这。」
"""

    def test_every_cell_narr_kept_with_its_cell_number(self):
        d = m.parse_script(self.SCRIPT)
        narrs = d["acts"][0]["narrs"]
        self.assertEqual(len(narrs), 2)
        self.assertEqual(narrs[0], {"text": "今天比平时冷。", "cell": 1})
        self.assertEqual(narrs[1], {"text": "妹妹发来消息，说她闺蜜在附近。", "cell": 2})

    def test_single_narr_backward_compatible(self):
        # 旧字段 narr / narr_cell 仍填第一句，供老调用方回退。
        d = m.parse_script(self.SCRIPT)
        self.assertEqual(d["acts"][0]["narr"], "今天比平时冷。")
        self.assertEqual(d["acts"][0]["narr_cell"], 1)


class PerCellNarrAutofixTest(unittest.TestCase):
    def _js(self):
        # 一个 4 格骨架（L4 三格主/次/道具），模型只画了画面、没画旁白框。
        return {"items": [
            {"label": "character", "caption": "1boy", "bbox": [0, 0, 100, 100]},
        ]}

    def test_two_narrs_produce_two_caption_boxes(self):
        js = self._js()
        notes = m.autofix(js, cast={"girl": "1girl"}, lines=[], layout="L4",
                          narr="今天比平时冷。", narr_cell=1,
                          narrs=[{"text": "今天比平时冷。", "cell": 1},
                                 {"text": "妹妹发来消息。", "cell": 2}])
        caps = [it for it in js["items"]
                if it.get("label") == "text"
                and m._kind(it.get("caption")) == "caption"]
        self.assertEqual(len(caps), 2)
        self.assertEqual({it["text"] for it in caps},
                         {"今天比平时冷。", "妹妹发来消息。"})
        self.assertTrue(any("旁白落第1格" in n for n in notes))
        self.assertTrue(any("旁白落第2格" in n for n in notes))

    def test_single_narr_still_one_box(self):
        js = self._js()
        m.autofix(js, cast={"girl": "1girl"}, lines=[], layout="L4",
                  narr="今天比平时冷。", narr_cell=1)
        caps = [it for it in js["items"]
                if it.get("label") == "text"
                and m._kind(it.get("caption")) == "caption"]
        self.assertEqual(len(caps), 1)
        self.assertEqual(caps[0]["text"], "今天比平时冷。")

    def test_model_written_narr_kept_not_duplicated(self):
        # 模型已经写了其中一句旁白：认领它原位，只补缺的另一句，不重复。
        js = self._js()
        js["items"].append({
            "label": "text",
            "caption": "some rectangular caption no bubble tail",
            "text": "今天比平时冷。",
            "bbox": [10, 10, 30, 40],
        })
        m.autofix(js, cast={"girl": "1girl"}, lines=[], layout="L4",
                  narr="今天比平时冷。", narr_cell=1,
                  narrs=[{"text": "今天比平时冷。", "cell": 1},
                         {"text": "妹妹发来消息。", "cell": 2}])
        caps = [it for it in js["items"]
                if it.get("label") == "text"
                and m._kind(it.get("caption")) == "caption"]
        self.assertEqual(len(caps), 2)
        self.assertEqual(len([it for it in caps if it["text"] == "今天比平时冷。"]), 1)


if __name__ == "__main__":
    unittest.main()
