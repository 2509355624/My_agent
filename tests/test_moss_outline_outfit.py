# -*- coding: utf-8 -*-
"""2026-11-25 大纲优先于服装 + 逐页对话修正。

用户报的问题：给了分集大纲，明确写了这一页不要外套 / 衣物褪掉 / 光着，
AI 还是把衣服画上去——服饰被「写死」了。

两条修法各测各的：
1. 大纲里点到衣着的页，体检必须把原话摆出来交给模型自查（代码只检测，不判断）；
   大纲原话也要随 prompt_override 一起钉到页 caption 上，给画图的模型当锚。
2. 每页能跟 Agent 来回聊：改的是 prompt_override，判据是「结构还能不能用」。
"""
import json
import os
import shutil
import tempfile
import unittest

from app.features.comic import moss_agent as A
from app.features.comic import moss_revise as R


# ── 一、大纲点到衣着 ─────────────────────────────────────────
class OutlineClothTest(unittest.TestCase):
    def test_mentions_cloth_hits(self):
        for line in ("进屋烤火，外套褪下来挂好",
                     "狗在雪地里，穿一件防寒外套",
                     "她一丝不挂地站在窗边",
                     "换上睡衣坐在床沿",
                     "she takes off the jacket"):
            self.assertTrue(A._mentions_cloth(line), line)

    def test_mentions_cloth_misses(self):
        for line in ("两人登山，风雪渐大", "清点物资，剩三根柴",
                     "女生发现雪停，两人到窗边看"):
            self.assertFalse(A._mentions_cloth(line), line)

    def test_audit_reports_outline_vs_outfits(self):
        """大纲点了衣着，就把原话和 outfits 并排摆出来。"""
        js = {"cast": {"girl": "a young woman, black hair, brown eyes, "
                               "slender teen proportions, long legs, small head"},
              "pages": [{"outfits": {"girl": "a thick down jacket and snow boots"}},
                        {"outfits": {"girl": "bare skin, no outer layer"}}]}
        blk, _ = A.audit(js, 2, outline=[(1, "两人登山，穿防寒外套"),
                                         (2, "进屋烤火，外套褪下来挂好")])
        joined = "\n".join(blk)
        self.assertIn("作者在大纲里点名了衣着", joined)
        self.assertIn("外套褪下来挂好", joined)
        self.assertIn("a thick down jacket", joined)
        # 两页都点了衣着，两页都要摆出来
        self.assertEqual(joined.count("作者在大纲里点名了衣着"), 2)

    def test_audit_quiet_when_outline_has_no_cloth(self):
        js = {"cast": {"girl": "a young woman, black hair, brown eyes, "
                               "slender teen proportions, long legs, small head"},
              "pages": [{"outfits": {"girl": "a white shirt"}}]}
        blk, _ = A.audit(js, 1, outline=[(1, "两人登山，风雪渐大")])
        self.assertNotIn("作者在大纲里点名了衣着", "\n".join(blk))

    def test_audit_without_outline_unchanged(self):
        """不传 outline 时行为与加这个参数之前完全一样。"""
        js = {"cast": {"girl": "a young woman, black hair, brown eyes, "
                               "slender teen proportions, long legs, small head"},
              "pages": [{"outfits": {"girl": "a white shirt"}}]}
        blk, _ = A.audit(js, 1)
        self.assertNotIn("作者在大纲里点名了衣着", "\n".join(blk))

    def test_system_declares_outline_priority(self):
        """SYSTEM 里必须有「大纲点名衣着优先于服饰规则」的仲裁句。"""
        self.assertIn("分集大纲点名要求的衣着状态，优先于本段以下所有服饰规则",
                      A.SYSTEM)
        # 还要说清「不穿」写正向描述，别写否定句
        self.assertIn("正向的状态", A.SYSTEM)
        self.assertIn("bare skin", A.SYSTEM)

    def test_stamp_outfits_carries_author_line(self):
        pj = {"summary": {"caption": "a page"}}
        A._stamp_outfits(pj, {"girl": "bare skin"}, "进屋烤火，外套褪下来挂好")
        cap = pj["summary"]["caption"]
        self.assertIn("Outfit state for this page: girl wears: bare skin", cap)
        self.assertIn("Author's requirement for this page: 进屋烤火，外套褪下来挂好", cap)

    def test_stamp_outfits_note_only(self):
        """只有原话、没有 outfits 时也要能钉上（作者点了但编剧漏了）。"""
        pj = {"summary": {"caption": "a page"}}
        A._stamp_outfits(pj, {}, "光着")
        self.assertIn("Author's requirement for this page: 光着",
                      pj["summary"]["caption"])


# ── 二、逐页对话修正 ─────────────────────────────────────────
def _page_json(n=2):
    return {
        "summary": {"caption": "a soft 2D anime manga page"},
        "items": [
            {"label": "character", "bbox": [0, 0, 500, 700],
             "caption": "1girl, long black hair, wearing a thick down jacket, standing"},
            {"label": "character", "bbox": [500, 0, 500, 700],
             "caption": "1girl, long black hair, wearing a thick down jacket, sitting"},
        ],
    }


def _page(prompt_js):
    return {"n": 2, "layout": "2h",
            "prompt_override": json.dumps(prompt_js, ensure_ascii=False)}


class PageChatTest(unittest.TestCase):
    def _call_returning(self, payload):
        def _call(system, user, max_tokens=6000, temperature=None, tag="",
                  provider=None, model=None):
            self.assertIn("原始需求", user)
            self.assertIn("作者这句话", user)
            return payload
        return _call

    def test_adopts_valid_revision(self):
        old = _page_json()
        new = _page_json()
        new["items"][0]["caption"] = "1girl, long black hair, bare skin, standing"
        r = R.checkin(old, 2, "第一格外套脱掉", brief="大纲",
                      call=self._call_returning(json.dumps(new)))
        self.assertTrue(r["changed"])
        self.assertEqual(json.loads(r["prompt_override"])["items"][0]["caption"],
                         "1girl, long black hair, bare skin, standing")
        self.assertEqual(len(r["diff"]), 1)
        self.assertEqual(r["diff"][0]["panel"], 1)

    def test_reply_form_does_not_change(self):
        r = R.checkin(_page_json(), 2, "这页怎么样", brief="大纲",
                      call=self._call_returning("答复：这页构图没问题。"))
        self.assertFalse(r["changed"])
        self.assertIsNone(r["prompt_override"])
        self.assertIn("构图没问题", r["reply"])

    def test_rejects_bbox_change(self):
        old = _page_json()
        new = _page_json()
        new["items"][0]["bbox"] = [10, 0, 500, 700]
        r = R.checkin(old, 2, "挪一下", brief="大纲",
                      call=self._call_returning(json.dumps(new)))
        self.assertFalse(r["changed"])
        self.assertIn("位置被挪动", r["reply"])

    def test_rejects_item_count_change(self):
        old = _page_json()
        new = _page_json()
        new["items"].pop()
        r = R.checkin(old, 2, "改成三格", brief="大纲",
                      call=self._call_returning(json.dumps(new)))
        self.assertFalse(r["changed"])
        self.assertIn("格子数变了", r["reply"])

    def test_rejects_no_diff(self):
        r = R.checkin(_page_json(), 2, "随便看看", brief="大纲",
                      call=self._call_returning(json.dumps(_page_json())))
        self.assertFalse(r["changed"])
        self.assertIn("一模一样", r["reply"])

    def test_tolerates_markdown_fence(self):
        new = _page_json()
        new["items"][1]["caption"] = "1girl, bare fur, lying by the fire"
        r = R.checkin(_page_json(), 2, "第二格不要外套", brief="大纲",
                      call=self._call_returning("```json\n" +
                                                json.dumps(new) + "\n```"))
        self.assertTrue(r["changed"])

    def test_broken_json_not_adopted(self):
        r = R.checkin(_page_json(), 2, "改一下", brief="大纲",
                      call=self._call_returning('{"items": [oops'))
        self.assertFalse(r["changed"])
        self.assertIn("解析失败", r["reply"])

    def test_empty_call_raises_nothing(self):
        r = R.checkin(_page_json(), 2, "改一下", brief="大纲",
                      call=self._call_returning(""))
        self.assertFalse(r["changed"])
        self.assertIn("空内容", r["reply"])

    def test_no_prompt_json(self):
        r = R.checkin(None, 2, "改一下", brief="大纲",
                      call=self._call_returning("{}"))
        self.assertFalse(r["changed"])
        self.assertIn("Layout JSON", r["reply"])

    def test_empty_message(self):
        r = R.checkin(_page_json(), 2, "   ", brief="大纲",
                      call=self._call_returning("{}"))
        self.assertFalse(r["changed"])


class PageChatHistoryTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def test_append_and_read(self):
        R.append_history(self.d, 2, "第一格改一下", "改好了")
        h = R.read_history(self.d)
        self.assertEqual(len(h), 2)
        self.assertEqual(h[0]["role"], "user")
        self.assertIn("第2页", h[0]["content"])
        self.assertEqual(h[1]["role"], "assistant")

    def test_keep_last_n(self):
        for i in range(10):
            R.append_history(self.d, i + 1, "q%d" % i, "a%d" % i)
        self.assertEqual(len(R.read_history(self.d)), R.HISTORY_KEEP)

    def test_clear(self):
        R.append_history(self.d, 1, "q", "a")
        R.clear_history(self.d)
        self.assertEqual(R.read_history(self.d), [])

    def test_missing_file_is_empty(self):
        self.assertEqual(R.read_history(os.path.join(self.d, "nope")), [])


if __name__ == "__main__":
    unittest.main()
