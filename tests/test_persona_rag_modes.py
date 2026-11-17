# -*- coding: utf-8 -*-
"""RAG 三态里的 summary/detail 组装边界（2026-11-18）。

P0 已经让编剧决定这一轮要不要翻旧账；P1 只做一件事：
  · summary —— 只拿批次摘要（少量事实），不拿原文、不拿锚点；
  · detail  —— 保持原样，锚点 + 批次 + 事实 + 原文全在。

这里不拉向量库，只钉组装层的取舍；检索层仍由 test_persona_rag.py 守。
"""
import unittest
from unittest import mock

from app.features.persona import rag as rank
from app.features.persona import rag_modes


def _batch(i, text=None):
    return {"id": "b%d" % i,
            "content": text or "【第%d天】当时在厨房做饭。" % i,
            "distance": 0.30 + i * 0.01,
            "metadata": {"kind": "batch", "turn_start": i,
                         "turn_end": i + 4, "source": rank._SOURCE}}


def _fact(i, text=None):
    return {"id": "f%d" % i,
            "content": text or "用户：事实 %d。" % i,
            "distance": 0.35 + i * 0.01,
            "metadata": {"kind": "fact", "about": "user",
                         "turn_start": i, "turn_end": i}}


def _raw(i, text=None):
    return {"id": "raw-%d-u" % i,
            "content": text or "用户说：第 %d 天的原话。" % i,
            "distance": 0.40,
            "metadata": {"kind": "raw", "source": rank._RAW_SOURCE}}


def _anchor(i, text=None):
    return {"id": "a%d" % i,
            "content": text or "第一次见面是在旧书市。",
            "distance": 0.10,
            "metadata": {"kind": "anchor", "about": "shared",
                         "source": rank._SOURCE, "_anchor": True}}


def _anchor_kind(i, text=None):
    return {"id": "a%d" % i,
            "content": text or "第一次见面是在旧书市。",
            "distance": 0.10,
            "metadata": {"kind": "anchor", "about": "shared",
                         "source": rank._SOURCE}}


class RagQueryHintsTest(unittest.TestCase):
    def test_anchor_and_quote_hints_are_narrow(self):
        self.assertTrue(rag_modes.needs_anchor("第一次是在哪"))
        self.assertFalse(rag_modes.needs_anchor("上次我们干嘛了"))
        self.assertTrue(rag_modes.needs_quote("你当时原话怎么说的"))
        self.assertFalse(rag_modes.needs_quote("上次我们干嘛了"))


class RagModeBlockTest(unittest.TestCase):
    def setUp(self):
        self._save = (rank._TESTING, rank.PERSONA_RAG, rank._BLOCKED)
        rank._TESTING = False
        rank.PERSONA_RAG = True
        rank._BLOCKED.clear()
        self.addCleanup(self._restore)

    def _restore(self):
        rank._TESTING, rank.PERSONA_RAG, rank._BLOCKED = self._save

    def test_summary_uses_batches_without_raw_or_anchors(self):
        docs = [_batch(1), _fact(1), _raw(1)]
        recent = [_batch(9, "【第9天】昨天在旧书市被人看见。")]
        with mock.patch.object(rank, "retrieve", return_value=docs), \
             mock.patch.object(rank, "retrieve_raw") as raw, \
             mock.patch.object(rank, "_anchor_docs") as anchor, \
             mock.patch.object(rank, "_recent_docs", return_value=recent):
            block = rank.build_block("web_10001__rem", "上次发生什么",
                                     mode="summary")
        self.assertIn("当时的经过", block)
        self.assertIn("厨房做饭", block)
        self.assertIn("旧书市", block)
        self.assertNotIn("当时的原话", block)
        self.assertNotIn("第 1 天的原话", block)
        raw.assert_not_called()
        anchor.assert_not_called()

    def test_summary_prefers_batches_and_caps_facts(self):
        docs = [_batch(1), _batch(2), _batch(3), _batch(4), _batch(5),
                _fact(1), _fact(2), _fact(3), _fact(4), _fact(5)]
        with mock.patch.object(rank, "retrieve", return_value=docs), \
             mock.patch.object(rank, "retrieve_raw", return_value=[]), \
             mock.patch.object(rank, "_anchor_docs", return_value=[]), \
             mock.patch.object(rank, "_recent_docs", return_value=[]):
            block = rank.build_block("web_10001__rem", "上次", mode="summary")
        self.assertIn("厨房做饭", block)
        self.assertIn("事实 1", block)
        self.assertIn("事实 2", block)
        self.assertNotIn("事实 3", block)
        self.assertNotIn("事实 4", block)

    def test_detail_keeps_raw_and_anchor_path(self):
        docs = [_batch(1), _raw(1)]
        with mock.patch.object(rank, "retrieve", return_value=docs), \
             mock.patch.object(rank, "retrieve_raw",
                               return_value=[_raw(2)]) as raw, \
             mock.patch.object(rank, "_anchor_docs", return_value=[]) as anchor, \
             mock.patch.object(rank, "_recent_docs", return_value=[]):
            block = rank.build_block("web_10001__rem", "第一次在哪")
        self.assertIn("当时的原话", block)
        self.assertIn("第 2 天的原话", block)
        raw.assert_called_once()
        anchor.assert_called_once()

    def test_lean_detail_skips_raw_and_anchor_for_generic_query(self):
        docs = [_batch(1), _fact(1), _raw(1)]
        with mock.patch.object(rank, "retrieve", return_value=docs), \
             mock.patch.object(rank, "retrieve_raw") as raw, \
             mock.patch.object(rank, "_anchor_docs") as anchor, \
             mock.patch.object(rank, "_recent_docs", return_value=[]):
            block = rank.build_block("web_10001__rem", "上次我们干嘛了",
                                     mode="detail", lean=True)
        self.assertIn("厨房做饭", block)
        self.assertNotIn("当时的原话", block)
        raw.assert_not_called()
        anchor.assert_not_called()

    def test_lean_detail_drops_anchor_kind_returned_by_search(self):
        docs = [_batch(1), _anchor_kind(1)]
        with mock.patch.object(rank, "retrieve", return_value=docs), \
             mock.patch.object(rank, "retrieve_raw") as raw, \
             mock.patch.object(rank, "_anchor_docs") as anchor, \
             mock.patch.object(rank, "_recent_docs", return_value=[]):
            block = rank.build_block("web_10001__rem", "上次我们干嘛了",
                                     mode="detail", lean=True)
        self.assertIn("厨房做饭", block)
        self.assertNotIn("旧书市", block)
        raw.assert_not_called()
        anchor.assert_not_called()

    def test_lean_detail_keeps_anchor_for_first_time_query(self):
        with mock.patch.object(rank, "retrieve", return_value=[_batch(1)]), \
             mock.patch.object(rank, "retrieve_raw") as raw, \
             mock.patch.object(rank, "_anchor_docs",
                               return_value=[_anchor(1)]) as anchor, \
             mock.patch.object(rank, "_recent_docs", return_value=[]):
            block = rank.build_block("web_10001__rem", "第一次在哪",
                                     mode="detail", lean=True)
        self.assertIn("锚点", block)
        self.assertIn("旧书市", block)
        raw.assert_not_called()
        anchor.assert_called_once()

    def test_lean_detail_keeps_raw_for_quote_query(self):
        with mock.patch.object(rank, "retrieve", return_value=[_batch(1)]), \
             mock.patch.object(rank, "retrieve_raw",
                               return_value=[_raw(2)]) as raw, \
             mock.patch.object(rank, "_anchor_docs") as anchor, \
             mock.patch.object(rank, "_recent_docs", return_value=[]):
            block = rank.build_block("web_10001__rem", "你当时原话怎么说的",
                                     mode="detail", lean=True)
        self.assertIn("当时的原话", block)
        self.assertIn("第 2 天的原话", block)
        raw.assert_called_once()
        anchor.assert_not_called()

    def test_default_mode_is_detail_for_old_callers(self):
        with mock.patch.object(rank, "retrieve", return_value=[]), \
             mock.patch.object(rank, "retrieve_raw", return_value=[]) as raw, \
             mock.patch.object(rank, "_anchor_docs", return_value=[]) as anchor, \
             mock.patch.object(rank, "_recent_docs", return_value=[]):
            rank.build_block("web_10001__rem", "天气")
        raw.assert_called_once()
        anchor.assert_called_once()


if __name__ == "__main__":
    unittest.main()
