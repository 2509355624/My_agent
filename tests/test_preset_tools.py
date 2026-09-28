# -*- coding: utf-8 -*-
"""preset_tools 单测：不拉起 RAG daemon / 不下载模型，全程 mock。

覆盖：
- 私聊下 QQ 号从上下文注入（collection = presets_<QQ>），模型无法指定 QQ；
- 群聊 / 网页端（无 QQ 上下文）一律拒绝；
- preset_save / preset_search / preset_list / preset_delete 的 CRUD 行为。
"""

import unittest
from unittest.mock import patch

from app.tools.rag import preset_tools as pt


class _FakeClient:
    """内存版 RagClient，记录调用、按 collection 存条目。"""

    def __init__(self):
        self.calls = []
        self.store = {}  # kb_name -> {id: {content, metadata}}

    def ingest(self, kb_name, entries):
        self.calls.append(("ingest", kb_name, entries))
        col = self.store.setdefault(kb_name, {})
        for e in entries:
            col[e["id"]] = {"content": e["content"],
                            "metadata": e.get("metadata", {})}
        return {"ok": True, "count": len(entries)}

    def search(self, kb_name, query, k=4):
        self.calls.append(("search", kb_name, query, k))
        col = self.store.get(kb_name, {})
        docs = [{"id": i, "content": v["content"], "metadata": v["metadata"],
                 "distance": 0.1} for i, v in col.items()]
        return {"ok": True, "docs": docs}

    def list_entries(self, kb_name):
        self.calls.append(("list_entries", kb_name))
        col = self.store.get(kb_name, {})
        entries = [{"id": i, "content": v["content"], "metadata": v["metadata"]}
                   for i, v in col.items()]
        return {"ok": True, "entries": entries}

    def delete_entry(self, kb_name, entry_id):
        self.calls.append(("delete_entry", kb_name, entry_id))
        col = self.store.get(kb_name, {})
        existed = entry_id in col
        col.pop(entry_id, None)
        return {"ok": True, "deleted": existed}


class _FakeRagClient:
    _inst = None

    @classmethod
    def get(cls):
        if cls._inst is None:
            cls._inst = _FakeClient()
        return cls._inst


class PresetToolsTest(unittest.TestCase):

    def setUp(self):
        _FakeRagClient._inst = None
        self._c = patch.object(pt, "RagClient", _FakeRagClient).start()
        self.ctx = patch.object(pt, "current_context").start()
        self.ctx.return_value = ("private", "12345")
        self.fake = _FakeRagClient.get()

    def tearDown(self):
        patch.stopall()

    # ── 私聊：QQ 注入 + collection 命名 ──
    def test_save_uses_injected_qq_and_collection(self):
        out = pt.preset_save("anime", "1girl, silver hair")
        self.assertIn("已保存", out)
        self.assertEqual(len(self.fake.calls), 1)
        cmd, kb, entries = self.fake.calls[0]
        self.assertEqual(cmd, "ingest")
        self.assertEqual(kb, "presets_12345")
        self.assertEqual(entries[0]["id"], "anime")
        self.assertEqual(entries[0]["metadata"]["name"], "anime")
        self.assertEqual(entries[0]["metadata"]["prompt"], "1girl, silver hair")

    # ── 群聊拒绝 ──
    def test_reject_group(self):
        self.ctx.return_value = ("group", "999")
        out = pt.preset_save("x", "y")
        self.assertIn("仅限 QQ 私聊", out)
        self.assertEqual(self.fake.calls, [])

    # ── 网页端（无上下文）拒绝 ──
    def test_reject_web(self):
        self.ctx.return_value = (None, None)
        out = pt.preset_list()
        self.assertIn("仅限 QQ 私聊", out)
        self.assertEqual(self.fake.calls, [])

    # ── 参数校验 ──
    def test_save_requires_name_and_prompt(self):
        self.assertIn("错误", pt.preset_save("", "p"))
        self.assertIn("错误", pt.preset_save("n", ""))
        self.assertEqual(self.fake.calls, [])

    # ── list / search ──
    def test_list_and_search(self):
        pt.preset_save("a", "cat")
        pt.preset_save("b", "dog")
        listing = pt.preset_list()
        self.assertIn("预设列表", listing)
        self.assertIn("a", listing)
        self.assertIn("b", listing)
        self.assertEqual(len(self.fake.store["presets_12345"]), 2)

        searched = pt.preset_search("cat")
        self.assertIn("预设搜索结果", searched)
        self.assertIn("a", searched)

    # ── delete ──
    def test_delete_existing_and_missing(self):
        pt.preset_save("a", "cat")
        out = pt.preset_delete("a")
        self.assertIn("已删除", out)
        out2 = pt.preset_delete("a")
        self.assertIn("未找到", out2)

    # ── 同名覆盖 ──
    def test_save_overwrites_same_name(self):
        pt.preset_save("a", "cat")
        pt.preset_save("a", "cat, fluffy")
        self.assertEqual(len(self.fake.store["presets_12345"]), 1)
        self.assertEqual(
            self.fake.store["presets_12345"]["a"]["metadata"]["prompt"],
            "cat, fluffy")


if __name__ == "__main__":
    unittest.main()
