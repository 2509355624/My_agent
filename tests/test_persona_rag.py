"""角色扮演长期记忆 RAG：提炼、隔离、清档（2026-11-05）。

不拉真向量模型，用 FakeClient 验证：
  · 记忆是**提炼后的事实**，不是对话原文；
  · 一人一角一线一个 collection；
  · 清空存档只删当前这一条线，并且连待提炼缓冲一起清。
"""
import io
import json
import os
import queue
import tempfile
import unittest
from unittest import mock

import app.agents as agents
import app.llm as llm
import app.persona as persona
import app.persona_rag as rank


class FakeClient:
    def __init__(self, docs=None):
        self.docs = docs or []
        self.entries = []
        self.deleted = []
        self.deleted_entries = []
        self.ingested = []
        self.searched = []
        self.collections = set()
        self.empty = set()
        self.ready = True

    def is_ready(self):
        return self.ready

    def start(self):
        return True

    def list_kb(self):
        return {"ok": True, "collections": list(self.collections),
                "counts": {c: (0 if c in self.empty else 1)
                           for c in self.collections}}

    def search(self, coll, query, k=4, timeout=30, allow_start=True,
               where=None):
        self.searched.append((coll, query, k, where))
        return {"ok": True, "docs": list(self.docs)}

    def ingest(self, coll, entries):
        self.ingested.append((coll, entries))
        self.collections.add(coll)
        return {"ok": True, "count": len(entries)}

    def list_entries(self, coll, timeout=30):
        return {"ok": True, "entries": list(self.entries)}

    def delete_kb(self, coll):
        self.deleted.append(coll)
        self.collections.discard(coll)
        return {"ok": True, "deleted": True}

    def delete_entry(self, coll, entry_id):
        self.deleted_entries.append((coll, entry_id))
        return {"ok": True, "deleted": 1}

    def delete_entries(self, coll, entry_ids):
        for eid in entry_ids:
            self.deleted_entries.append((coll, eid))
        return {"ok": True, "deleted": len(entry_ids)}


class RagBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.pending_path = os.path.join(self.tmp.name, "pending.json")
        self.client = FakeClient()
        self._save = (rank._TESTING, rank.PERSONA_RAG, rank._CLIENT, rank._QUEUE,
                      rank._BLOCKED, rank._FAIL_UNTIL, rank._WARMED, rank._WORKER,
                      rank.PERSONA_RAG_INGEST_EVERY, rank._pending_path)
        rank._TESTING = False
        rank.PERSONA_RAG = True
        rank.PERSONA_RAG_INGEST_EVERY = 2      # 测两轮就凑一批
        rank._CLIENT = self.client
        rank._QUEUE = queue.Queue()
        rank._BLOCKED.clear()
        rank._FAIL_UNTIL = 0
        rank._WARMED = False
        rank._WORKER = None
        rank._pending_path = lambda: self.pending_path
        self.addCleanup(self._restore)

    def _restore(self):
        (rank._TESTING, rank.PERSONA_RAG, rank._CLIENT, rank._QUEUE,
         rank._BLOCKED, rank._FAIL_UNTIL, rank._WARMED, rank._WORKER,
         rank.PERSONA_RAG_INGEST_EVERY, rank._pending_path) = self._save


class CollectionTest(RagBase):
    def test_per_line_isolation(self):
        a = rank.collection_name("web_10001__rem")
        b = rank.collection_name("web_10002__rem")
        c = rank.collection_name("web_10001__sagiri")
        self.assertNotEqual(a, b)
        self.assertNotEqual(a, c)
        self.assertEqual(a, rank.collection_name("web_10001__rem"))
        self.assertTrue(a.startswith("persona_"))
        self.assertRegex(a, r"^[a-z][a-z0-9_]{2,63}$")

    def test_hash_does_not_leak_qq(self):
        self.assertNotIn("10001", rank.collection_name("web_10001__rem"))


class RememberTest(RagBase):
    def test_accumulates_until_batch_size(self):
        with mock.patch.object(rank, "_ensure_worker"):
            rank.remember("web_10001__rem", "今天下雨了", "嗯，我带了伞。", None)
            self.assertEqual(rank._QUEUE.qsize(), 0)
            rank.remember("web_10001__rem", "你吃香菜吗", "讨厌，别放。", None)
        kind, line, payload = rank._QUEUE.get_nowait()
        self.assertEqual(kind, "distill")
        self.assertEqual(line, "web_10001__rem")
        turns = payload["turns"]
        self.assertEqual([t["turn"] for t in turns], [1, 2])
        self.assertIn("今天下雨了", turns[0]["user"])
        self.assertIn("我带了伞", turns[0]["assistant"])

        with open(self.pending_path, encoding="utf-8") as fh:
            rec = json.load(fh)
        self.assertEqual(rec["web_10001__rem"]["seq"], 2)
        self.assertEqual(rec["web_10001__rem"]["items"], [])
        self.assertEqual(rec["web_10001__rem"]["last_batch"], [1, 2])

    def test_pending_survives_reload(self):
        with mock.patch.object(rank, "_ensure_worker"):
            rank.remember("web_10001__rem", "第一句", "第一答", None)
        rec = rank.pending_for("web_10001__rem")
        self.assertEqual(len(rec["items"]), 1)
        self.assertEqual(rec["items"][0]["turn"], 1)


class DistillTest(RagBase):
    def test_parse_memory_json_keeps_about_and_source(self):
        raw = ('{"skip": false, "entries": ['
               '{"about": "user", "content": "用户：讨厌香菜",'
               ' "topics": ["饮食"], "primaryEmotion": "", "openThreads": ["点菜"]},'
               '{"about": "character", "content": "角色：养了一只橘猫",'
               ' "topics": [], "primaryEmotion": "", "openThreads": []}]}')
        entries = rank.parse_memory_json(raw, 1, 2)
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]["metadata"]["about"], "user")
        self.assertEqual(entries[0]["metadata"]["source"], "batch_summary_v3")
        self.assertEqual(entries[0]["metadata"]["topics"], "饮食")
        self.assertEqual(entries[0]["id"], "1-2-0")
        self.assertIn("用户：", entries[0]["content"])

    def test_parse_batch_summary_uses_day_and_name(self):
        raw = ('{"skip": false, "summary": "旅馆里，用户把帽子摘了，'
               '伊蕾娜嘴上说别乱放，手却自己整理好。",'
               ' "entries": [{"about": "character",'
               ' "content": "伊蕾娜：嘴上嫌他乱，还是把帽子收好了"}]}')
        entries = rank.parse_memory_json(raw, 21, 25, day=2, phase="中午",
                                        card_name="伊蕾娜")
        self.assertEqual(entries[0]["metadata"]["kind"], "parent")
        self.assertEqual(entries[0]["metadata"]["day"], 2)
        self.assertIn("【第2天 · 中午】", entries[0]["content"])
        self.assertEqual(entries[1]["metadata"]["kind"], "subfact")
        self.assertTrue(entries[1]["content"].startswith("伊蕾娜："))

    def test_remember_flushes_batch_when_day_changes(self):
        with mock.patch.object(rank, "_ensure_worker"):
            rank.remember("web_10001__rem", "早", "答", day=1)
            rank.remember("web_10001__rem", "再", "答", day=1)
            kind, _line, payload = rank._QUEUE.get_nowait()
            self.assertEqual(kind, "distill")
            self.assertEqual([t["day"] for t in payload["turns"]], [1, 1])
            rank.remember("web_10001__rem", "第二天", "答", day=2)
            rec = rank.pending_for("web_10001__rem")
            self.assertEqual([t["day"] for t in rec["items"]], [2])

    def test_parse_skips_garbage_and_empty(self):
        self.assertEqual(rank.parse_memory_json("不是 JSON", 1, 2), [])
        self.assertEqual(
            rank.parse_memory_json('{"skip": true, "entries": []}', 1, 2), [])

    def test_distill_prompt_has_both_sides_and_turns(self):
        raw = ('{"skip": false, "entries": [{"about": "user",'
               ' "content": "用户：讨厌香菜"}]}')
        with mock.patch.object(llm, "call_llm", return_value=raw) as call:
            entries = rank.distill([
                {"turn": 1, "user": "我讨厌香菜", "assistant": "记住了。"},
                {"turn": 2, "user": "点菜别放", "assistant": "好。"},
            ])
        prompt = call.call_args[0][0][0]["content"]
        self.assertIn("用户：我讨厌香菜", prompt)
        self.assertIn("角色：记住了", prompt)
        self.assertIn("第 1–2 次回复", prompt)
        self.assertEqual(len(entries), 1)

    def test_distill_restores_custom_override(self):
        raw = ('{"skip": false, "entries": [{"about": "user",'
               ' "content": "用户：怕黑"}]}')
        cfg = {"base_url": "http://x", "model": "m", "api_key": "k"}
        with mock.patch.object(llm, "call_llm", return_value=raw):
            rank.distill([{"turn": 1, "user": "我怕黑", "assistant": "嗯。"}],
                         custom=cfg)
        self.assertIsNone(llm.get_custom_override())

    def test_parent_summary_splits_into_parts(self):
        summary = "字" * 800
        raw = '{"skip": false, "summary": "%s"}' % summary
        entries = rank.parse_memory_json(raw, 3, 7, day=1, phase="中午",
                                         batch_id="batch-3-7-0")
        parents = [e for e in entries if e["metadata"]["kind"] == "parent"]
        self.assertEqual(len(parents), 2)
        self.assertEqual(parents[0]["metadata"]["part"], 1)
        self.assertEqual(parents[1]["metadata"]["part"], 2)
        self.assertEqual(parents[0]["metadata"]["group_id"], "batch-3-7-0")
        self.assertEqual(parents[1]["metadata"]["group_id"], "batch-3-7-0")

    def test_subfact_no_cap_and_no_hard_truncate(self):
        long_text = "用户：" + "细节" * 120      # 远超旧 160 字硬截断
        facts = [{"about": "user", "content": long_text} for _ in range(6)]
        raw = json.dumps({"skip": False, "facts": facts}, ensure_ascii=False)
        entries = rank.parse_memory_json(raw, 1, 2, phase="中午")
        subs = [e for e in entries if e["metadata"]["kind"] == "subfact"]
        self.assertEqual(len(subs), 6)           # 旧 4 条硬上限已去掉
        for e in subs:
            self.assertGreater(len(e["content"]), 160)
            self.assertEqual(e["metadata"]["parent_group_id"], "batch-1-2-0")
            self.assertEqual(e["metadata"]["phase"], "中午")

    def test_process_ingests_distilled_entries(self):
        entries = [{"id": "1-2-0", "content": "用户：讨厌香菜",
                    "metadata": {"about": "user", "source": "batch_summary_v3",
                                 "turn_start": 1, "turn_end": 2,
                                 "topics": "", "primary_emotion": "",
                                 "open_threads": ""}}]
        with mock.patch.object(rank, "distill", return_value=entries):
            rank._process("distill", "web_10001__rem",
                          {"turns": [{"turn": 1}, {"turn": 2}]})
        self.assertEqual(self.client.ingested,
                         [(rank.collection_name("web_10001__rem"), entries)])


class RetrieveTest(RagBase):
    def test_search_filters_to_distilled_source(self):
        rank.retrieve("web_10001__rem", "她怕什么天气？")
        self.assertEqual(len(self.client.searched), 1)
        coll, _q, _k, where = self.client.searched[0]
        self.assertEqual(coll, rank.collection_name("web_10001__rem"))
        self.assertEqual(where, {"source": "batch_summary_v3"})

    def test_distance_filter(self):
        self.client.docs = [
            {"id": "a", "content": "用户：怕打雷。", "distance": 0.40,
             "metadata": {"about": "user", "turn_start": 1, "turn_end": 2}},
            {"id": "b", "content": "完全无关。", "distance": 0.90,
             "metadata": {"about": "user"}},
        ]
        got = rank.retrieve("web_10001__rem", "天气")
        self.assertEqual([d["id"] for d in got], ["a"])

    def test_blocked_line_returns_nothing(self):
        rank._BLOCKED.add("web_10001__rem")
        self.assertEqual(rank.retrieve("web_10001__rem", "天气"), [])
        self.assertEqual(self.client.searched, [])

    def test_build_block_labels_about_and_ruling(self):
        self.client.docs = [
            {"id": "a", "content": "用户：怕打雷。", "distance": 0.40,
             "metadata": {"about": "user", "turn_start": 1, "turn_end": 3,
                          "topics": "天气"}},
        ]
        block = rank.build_block("web_10001__rem", "天气")
        self.assertIn("用户事实", block)
        self.assertIn("第 1–3 次回复", block)
        self.assertIn("主题：天气", block)
        self.assertIn("只能补充", block)

    def test_build_block_optional_trace_reports_real_selection(self):
        self.client.docs = [
            {"id": "a", "content": "用户：怕打雷。", "distance": 0.40,
             "metadata": {"kind": "fact", "about": "user", "topics": "天气"}},
            {"id": "b", "content": "完全无关。", "distance": 0.90,
             "metadata": {"kind": "fact", "about": "user"}},
        ]
        trace = {}
        block = rank.build_block("web_10001__rem", "天气", trace=trace)
        self.assertEqual(trace["query"], "天气")
        self.assertEqual(trace["hit_count"], 1)
        self.assertEqual(trace["hits"][0]["id"], "a")
        self.assertAlmostEqual(trace["hits"][0]["similarity"], 0.60)
        self.assertEqual(trace["injected_count"], 1)
        self.assertEqual(trace["selected"][0]["id"], "a")
        self.assertEqual(trace["block"], block)

    def test_recent_leaves_the_relevance_slots_alone(self):
        """召回明明命中的旧事实，不能被「最近 2 批」挤下桌。

        2026-11-09 母本副本实测：14 个窗口外考点，检索能召回 9 个，
        真正进注入块的只有 4 个——差额全丢在 `facts[:3]` 被近期占了。
        """
        old = {"id": "f-old", "content": "双方：约定早上七点四十在四号楼楼下碰面。",
               "metadata": {"kind": "fact", "about": "shared",
                            "turn_start": 30, "turn_end": 34}}
        recent = [
            {"id": "b-1", "content": "【第1天】早晨小超市门口，用户提议帮她拿东西。",
             "metadata": {"kind": "batch"}},
            {"id": "f-1", "content": "用户：自己租房子一个人住。",
             "metadata": {"kind": "fact"}},
            {"id": "f-2", "content": "学妹：明天上午没课。",
             "metadata": {"kind": "fact"}},
        ]
        block = rank._fact_block([old], recent)
        self.assertIn("七点四十", block)        # 相关性那条占 facts 名额
        self.assertIn("【第1天】", block)        # 最近批次照带（11-08 的决定没推翻）
        self.assertIn("自己租房子", block)       # recent 还留着 1 条事实名额
        self.assertNotIn("明天上午没课", block)  # 第 2 条往后不再往里塞

    def test_build_block_keeps_recalled_old_fact(self):
        self.client.docs = [
            {"id": "a", "content": "角色：住在四楼走廊尽头，四人一间宿舍。",
             "distance": 0.44,
             "metadata": {"kind": "fact", "about": "character", "topics": "住处"}},
        ]
        recent = [
            {"id": "b-1", "content": "【第2天】两人在市场采买三餐食材。",
             "metadata": {"kind": "batch"}},
            {"id": "f-1", "content": "用户：承诺明天见到学妹之前不吃东西。",
             "metadata": {"kind": "fact"}},
        ]
        with mock.patch.object(rank, "_recent_docs", return_value=recent):
            block = rank.build_block("web_10001__rem", "她住的宿舍在哪")
        self.assertIn("四楼走廊尽头", block)
        self.assertIn("【第2天】", block)

    def test_mount_parents_parts_ordered_by_group(self):
        gid = "batch-3-7-0"
        self.client.entries = [
            {"id": gid + "-p2", "content": "后半段经过。",
             "metadata": {"kind": "parent", "group_id": gid, "part": 2,
                          "part_total": 2, "turn_start": 3, "turn_end": 7}},
            {"id": gid, "content": "前半段经过。",
             "metadata": {"kind": "parent", "group_id": gid, "part": 1,
                          "part_total": 2, "turn_start": 3, "turn_end": 7}},
        ]
        subs = [{"id": "3-7-0", "content": "用户：中午一起吃了玉子烧。",
                 "metadata": {"kind": "subfact", "parent_group_id": gid,
                              "turn_start": 3, "turn_end": 7}}]
        mounted = rank._mount_parents("web_10001__rem", subs)
        self.assertEqual([d["id"] for d in mounted], [gid, gid + "-p2"])

    def test_build_block_mounts_parent_for_hit_subfact(self):
        gid = "batch-1-5-0"
        self.client.docs = [
            {"id": "1-5-0", "content": "用户：晚上答应周末去海边。",
             "distance": 0.30,
             "metadata": {"kind": "subfact", "parent_group_id": gid,
                          "about": "user", "turn_start": 1, "turn_end": 5}},
        ]
        self.client.entries = [
            {"id": gid, "content": "【第1天】傍晚两人聊到周末的安排。",
             "metadata": {"kind": "parent", "group_id": gid, "part": 1,
                          "part_total": 1, "turn_start": 1, "turn_end": 5}},
        ]
        block = rank.build_block("web_10001__rem", "周末去哪")
        self.assertIn("傍晚两人聊到周末", block)

    def test_merge_parent_dedupes_same_id_from_multiple_sources(self):
        # 同一父块从 recent 和挂载两路各读回一份，不能被当成 part 拼接两遍。
        gid = "batch-1-5-0"
        base = {"id": gid, "content": "整批经过叙事。",
                "metadata": {"kind": "parent", "group_id": gid, "part": 1}}
        recent = dict(base); recent["metadata"] = dict(base["metadata"])
        mounted = dict(base); mounted["metadata"] = dict(base["metadata"])
        merged = rank._merge_parent_parts([recent, mounted])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["content"], "整批经过叙事。")


class ForgetTest(RagBase):
    def test_forget_last_pops_pending(self):
        with mock.patch.object(rank, "_ensure_worker"):
            rank.remember("web_10001__rem", "第一句", "第一答", None)
            rank.forget_last("web_10001__rem")
        self.assertEqual(rank._QUEUE.qsize(), 0)
        rec = rank.pending_for("web_10001__rem")
        self.assertEqual(rec["items"], [])
        self.assertEqual(rec["seq"], 0)

    def test_forget_last_targets_last_batch_when_pending_empty(self):
        with mock.patch.object(rank, "_ensure_worker"):
            rank.remember("web_10001__rem", "一", "答一", None)
            rank.remember("web_10001__rem", "二", "答二", None)
            rank._QUEUE.get_nowait()          # 模拟这批已经提炼
            rank.forget_last("web_10001__rem")
        kind, _line, payload = rank._QUEUE.get_nowait()
        self.assertEqual(kind, "forget")
        self.assertEqual(payload["start"], 1)
        self.assertEqual(payload["end"], 2)
        self.assertIn("batch-1-2-0", payload["batch_ids"])
        rank._process(kind, _line, payload)
        # 父块 group_id + part2/part3 兜底 id + 2 轮 × 2 条旧原话兜底
        ids = [eid for _coll, eid in self.client.deleted_entries]
        self.assertIn("batch-1-2-0", ids)
        for rid in ("raw-1-u", "raw-1-a", "raw-2-u", "raw-2-a"):
            self.assertIn(rid, ids)
        # 回退最后一轮后，前一轮放回 pending，下一轮会重新凑批
        rec = rank.pending_for("web_10001__rem")
        self.assertEqual([t["turn"] for t in rec["items"]], [1])
        self.assertEqual(rec["epoch"], 1)

    def test_rollback_cancels_in_flight_distill(self):
        line = "web_10001__rem"
        with mock.patch.object(rank, "_ensure_worker"):
            rank.remember(line, "一", "答一", None)
            rank.remember(line, "二", "答二", None)
            rank._QUEUE.get_nowait()          # 扔掉 distill，模拟还在后台
            rank.forget_last(line)            # epoch +1
            rank._QUEUE.get_nowait()          # 扔掉 forget
        with mock.patch.object(rank, "distill", return_value=[]) as d:
            rank._process("distill", line, {
                "turns": [{"turn": 1}, {"turn": 2}],
                "epoch": 0, "batch_id": "batch-1-2-0"})
        self.assertEqual(self.client.ingested, [])
        self.assertFalse(d.called)


class GuestSkipTest(RagBase):
    """游客只玩 30 轮，不存也不查。"""

    def test_enabled_for(self):
        self.assertTrue(rank.enabled_for("web_10001__rem"))
        self.assertFalse(rank.enabled_for("guest_uuid__junior"))

    def test_remember_and_retrieve_skip_guest(self):
        with mock.patch.object(rank, "_ensure_worker"):
            rank.remember("guest_uuid__junior", "在吗", "在。", None)
        self.assertEqual(rank._QUEUE.qsize(), 0)
        self.assertEqual(rank.pending_for("guest_uuid__junior"), {})
        self.assertEqual(rank.retrieve("guest_uuid__junior", "在吗"), [])
        self.assertEqual(self.client.searched, [])

    def test_drop_skips_guest(self):
        with mock.patch.object(rank, "_ensure_worker"):
            rank.drop_line("guest_uuid__junior")
        self.assertEqual(rank._QUEUE.qsize(), 0)
        self.assertNotIn("guest_uuid__junior", rank._BLOCKED)


class DropTest(RagBase):
    def test_drop_only_current_line_and_unblocks_after_delete(self):
        with mock.patch.object(rank, "_ensure_worker"):
            rank.drop_line("web_10001__rem")
        self.assertIn("web_10001__rem", rank._BLOCKED)
        kind, line, payload = rank._QUEUE.get_nowait()
        self.assertEqual(kind, "drop")
        rank._process(kind, line, payload)
        self.assertEqual(self.client.deleted,
                         [rank.collection_name("web_10001__rem")])
        self.assertNotIn("web_10001__rem", rank._BLOCKED)
        self.assertNotIn(rank.collection_name("web_10002__rem"),
                         self.client.deleted)

    def test_drop_clears_pending_buffer(self):
        with mock.patch.object(rank, "_ensure_worker"):
            rank.remember("web_10001__rem", "一句", "一答", None)
            rank.drop_line("web_10001__rem")
        self.assertEqual(rank.pending_for("web_10001__rem"), {})

    def test_drop_persists_op_and_clears_after_process(self):
        with mock.patch.object(rank, "_ensure_worker"):
            rank.drop_line("web_10001__rem")
        with io.open(self.pending_path, encoding="utf-8") as f:
            data = json.load(f)
        self.assertTrue(any(x["op"] == "drop" and x["line"] == "web_10001__rem"
                            for x in data.get("_ops", [])))
        kind, line, payload = rank._QUEUE.get_nowait()
        rank._process(kind, line, payload)
        with io.open(self.pending_path, encoding="utf-8") as f:
            data = json.load(f)
        self.assertNotIn("web_10001__rem",
                         [x.get("line") for x in data.get("_ops", [])])

    def test_recover_replays_persisted_drop(self):
        coll = rank.collection_name("web_10001__rem")
        self.client.collections.add(coll)
        rank._op_add("drop", "web_10001__rem")
        rank._recover_persisted_ops(self.client)
        self.assertIn(coll, self.client.deleted)
        with io.open(self.pending_path, encoding="utf-8") as f:
            data = json.load(f)
        self.assertNotIn("web_10001__rem",
                         [x.get("line") for x in data.get("_ops", [])])

    def test_with_line_tags_metadata(self):
        entries = [{"id": "1", "content": "x", "metadata": {"kind": "fact"}}]
        rank._with_line(entries, "web_10001__rem")
        self.assertEqual(entries[0]["metadata"]["line"], "web_10001__rem")

    def test_purge_shells_removes_empty_collection(self):
        shell = rank.collection_name("web_10001__shell")
        self.client.collections.add(shell)
        self.client.empty.add(shell)
        rank._purge_shells(self.client)
        self.assertIn(shell, self.client.deleted)


class EraseHookTest(unittest.TestCase):
    """清空存档要连带删这条线的向量库，且只删这一条。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = os.path.join(self.tmp.name, "agents")
        os.makedirs(root, exist_ok=True)
        p = mock.patch.object(agents, "AGENTS_DIR", root)
        p.start()
        self.addCleanup(p.stop)
        q = mock.patch.object(
            persona, "_STATE_PATH",
            os.path.join(self.tmp.name, "state", "persona_sessions.json"))
        q.start()
        self.addCleanup(q.stop)
        agents.clear_cache()
        self.addCleanup(agents.clear_cache)
        persona.save_cards([{"id": "rem", "name": "雷姆",
                             "systemPrompt": "你是雷姆。"}])
        persona.switch_line("private_10001", persona.get_card("rem"))

    def test_clear_drops_only_the_active_line(self):
        line = persona.resolve_line("private_10001")
        with mock.patch.object(rank, "drop_line") as drop:
            persona.clear("private_10001")
        drop.assert_called_once_with(line)
        self.assertTrue(line.startswith("private_10001__"))


class VisibleTextTest(unittest.TestCase):
    """心里话不写进长期记忆。"""

    def test_thought_segments_are_excluded(self):
        segs = [
            {"who": "用户", "type": "说", "text": "你今天去哪了？"},
            {"who": "用户", "type": "想", "text": "她是不是在躲我。"},
            {"who": "用户", "type": "做", "text": "把伞收起来。"},
        ]
        got = persona._visible_user_text(segs, "fallback")
        self.assertIn("你今天去哪了", got)
        self.assertIn("把伞收起来", got)
        self.assertNotIn("躲我", got)

    def test_only_thought_returns_empty_not_fallback(self):
        segs = [{"who": "用户", "type": "想", "text": "算了不说了。"}]
        self.assertEqual(persona._visible_user_text(segs, "原文里有心里话"), "")


class RawMemoryTest(RagBase):
    """原文层（2026-11-07）：和事实层并存，只补细节，回退时一起撤。"""

    def test_raw_entries_shape(self):
        rows = [{"turn": 7, "day": 3, "phase": "晚上",
                 "user": "我带了伞", "assistant": "她看了一眼伞"}]
        out = rank.raw_entries(rows)
        self.assertEqual([e["id"] for e in out], ["raw-7-u", "raw-7-a"])
        self.assertIn("第3天·晚上", out[0]["content"])
        self.assertIn("用户说：我带了伞", out[0]["content"])
        self.assertIn("她：她看了一眼伞", out[1]["content"])
        self.assertEqual(out[0]["metadata"]["source"], rank._RAW_SOURCE)
        self.assertEqual(out[0]["metadata"]["kind"], "raw")
        self.assertEqual(out[0]["metadata"]["role"], "user")

    def test_raw_entries_skip_empty_and_turnless(self):
        self.assertEqual(rank.raw_entries([{"turn": 0, "user": "x"}]), [])
        self.assertEqual(
            rank.raw_entries([{"turn": 2, "user": "", "assistant": ""}]), [])
        self.assertEqual(rank.raw_entries(None), [])

    def test_raw_entries_truncate(self):
        out = rank.raw_entries([{"turn": 1, "user": "字" * 900,
                                 "assistant": ""}])
        self.assertLessEqual(len(out[0]["content"]), rank.RAW_MAX_CHARS)

    def test_retrieve_raw_needs_kind_and_distance(self):
        self.client.docs = [
            {"id": "raw-1", "content": "用户说：怕打雷", "distance": 0.50,
             "metadata": {"kind": "raw", "source": rank._RAW_SOURCE}},
            {"id": "batch-1", "content": "批次叙事", "distance": 0.30,
             "metadata": {"kind": "batch", "source": rank._SOURCE}},
            {"id": "raw-2", "content": "无关", "distance": 0.95,
             "metadata": {"kind": "raw", "source": rank._RAW_SOURCE}},
        ]
        got = rank.retrieve_raw("web_10001__rem", "打雷")
        self.assertEqual([d["id"] for d in got], ["raw-1"])

    def test_build_block_has_distilled_layer_only(self):
        self.client.docs = [
            {"id": "f1", "content": "用户：怕打雷。", "distance": 0.30,
             "metadata": {"kind": "fact", "about": "user",
                          "turn_start": 1, "turn_end": 5,
                          "source": rank._SOURCE}},
        ]
        blk = rank.build_block("web_10001__rem", "打雷")
        self.assertIn("【长期记忆", blk)
        # V3：原话层已取消，注入块里不再出现原话段。
        self.assertNotIn("【当时的原话", blk)
        self.assertIn("正在发生的比旧话更重要", blk)
        self.assertIn("当时记下的话", blk)
        self.assertIn("想提旧事照提", blk)
        self.assertIn("用已经发生过的口吻提", blk)

    def test_raw_block_respects_budget(self):
        docs = [{"content": "字" * 300} for _ in range(10)]
        blk = rank._raw_block(docs)
        body = blk.split("\n", 1)[1]
        self.assertLessEqual(len(body.replace("\n", "")),
                             rank.RAW_BLOCK_CHARS + rank.RAW_ITEM_CHARS)

    def test_process_no_longer_ingests_raw(self):
        turns = [{"turn": 1, "user": "我怕黑", "assistant": "嗯。"}]
        with mock.patch.object(rank, "distill", return_value=[]):
            rank._process("distill", "web_10001__rem", {"turns": turns})
        # V3：不再单独入库原话；提炼为空就什么都不写。
        self.assertEqual(self.client.ingested, [])


class BackfillTest(RagBase):
    """归档原文回填（2026-11-10）：老线补料 + 400 字截断原地变全文。"""

    def setUp(self):
        super().setUp()
        p = mock.patch.object(rank, "_BACKFILL_QUEUED", set())
        p.start()
        self.addCleanup(p.stop)

    def _rows(self, n, chars=120):
        return [{"role": "user" if i % 2 == 0 else "assistant",
                 "content": "第%d句 %s" % (i, "字" * chars)}
                for i in range(n)]

    def test_truncated_entry_is_extended_in_place(self):
        full = "开头一句 " + "细" * 800
        short = ("第2天·上午 她：" + full)[:400]
        self.client.entries = [{
            "id": "raw-62-a", "content": short,
            "metadata": {"kind": "raw", "role": "assistant",
                         "source": rank._RAW_SOURCE, "day": 2,
                         "phase": "上午", "turn": 62},
        }]
        n = rank._backfill_raw(
            self.client, "coll", [{"role": "assistant", "content": full}])
        self.assertEqual(n, 1)
        _coll, entries = self.client.ingested[0]
        self.assertEqual(entries[0]["id"], "raw-62-a",
                         "老条目应原地覆盖，不能新增一条重复的")
        self.assertGreater(len(entries[0]["content"]), 400)
        self.assertIn("细" * 100, entries[0]["content"])
        self.assertTrue(entries[0]["content"].startswith("第2天·上午"),
                        "覆盖时不能把天数/时段丢掉")
        self.assertEqual(entries[0]["metadata"]["day"], 2)

    def test_missing_turns_get_stable_ids(self):
        self.client.entries = []
        n = rank._backfill_raw(self.client, "coll", self._rows(6))
        self.assertEqual(n, 6)
        ids = [e["id"] for _, batch in self.client.ingested for e in batch]
        self.assertEqual(ids, ["rawb-1-u", "rawb-1-a", "rawb-2-u",
                               "rawb-2-a", "rawb-3-u", "rawb-3-a"])

    def test_second_run_writes_nothing(self):
        rows = self._rows(4)
        self.assertEqual(rank._backfill_raw(self.client, "coll", rows), 4)
        self.client.entries = [
            {"id": e["id"], "content": e["content"],
             "metadata": e["metadata"]}
            for _, batch in self.client.ingested for e in batch]
        self.client.ingested = []
        self.assertEqual(rank._backfill_raw(self.client, "coll", rows), 0)
        self.assertEqual(self.client.ingested, [])

    def test_short_line_never_overwrites_an_old_entry(self):
        old = "她：好。后面还有一大段细节" + "字" * 300
        self.client.entries = [{
            "id": "raw-1-a", "content": old,
            "metadata": {"kind": "raw", "role": "assistant",
                         "source": rank._RAW_SOURCE, "day": 1, "phase": "",
                         "turn": 1},
        }]
        n = rank._backfill_raw(
            self.client, "coll", [{"role": "assistant", "content": "好。"}])
        self.assertEqual(n, 1)
        self.assertTrue(self.client.ingested[0][1][0]["id"].startswith("rawb-"))
        self.assertEqual(self.client.entries[0]["content"], old,
                         "短句不能拿去覆盖别条目的全文")

    def test_schedule_only_once_and_archive_is_lazy(self):
        calls = []

        def factory():
            calls.append(1)
            return self._rows(2)

        with mock.patch.object(rank, "_enqueue") as enq:
            rank.schedule_backfill("web_10001__rem", factory)
            rank.schedule_backfill("web_10001__rem", factory)
        self.assertEqual(len(calls), 1, "没排上队列就不该读全量归档")
        self.assertEqual(enq.call_count, 1)
        self.assertEqual(enq.call_args[0][0], "backfill")

    def test_raw_block_keeps_the_whole_900_chars(self):
        tail = "结尾独有的细节"
        blk = rank._raw_block([{"content": "字" * 800 + tail}])
        self.assertIn(tail, blk, "单条 520 字硬截会把后半段细节砍掉")

    def test_process_backfill_distills_into_parent_child(self):
        self.client.entries = []
        entries = [{
            "id": "batch-1-1-0", "content": "父块叙事",
            "metadata": {"kind": "parent", "source": rank._SOURCE},
        }, {
            "id": "1-1-0", "content": "子块",
            "metadata": {"kind": "subfact", "source": rank._SOURCE},
        }]
        with mock.patch.object(rank, "distill", return_value=entries) as dist:
            rank._process("backfill", "web_10001__rem",
                          {"rows": self._rows(2)})
        dist.assert_called_once()
        turns = dist.call_args[0][0]
        self.assertEqual(len(turns), 1, "两条归档配对成一轮")
        self.assertEqual(turns[0]["day"], 0)
        self.assertEqual(turns[0]["phase"], "")
        self.assertEqual(len(self.client.ingested), 1)
        for e in self.client.ingested[0][1]:
            self.assertNotEqual(e["metadata"]["kind"], "raw",
                                "回填不再写旧原话层")

    def test_backfill_pairs_orphan_user_with_empty_assistant(self):
        turns = rank._rows_to_turns([{"role": "user", "content": "在吗"}])
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0]["user"], "在吗")
        self.assertEqual(turns[0]["assistant"], "")
        self.assertEqual(turns[0]["day"], 0)

    def test_backfill_distills_in_batches_by_ingest_every(self):
        # 6 轮（12 条归档）；setUp 里 INGEST_EVERY=2 → 三批，batch_id 按区间定。
        rows = []
        for i in range(12):
            rows.append({"role": "user" if i % 2 == 0 else "assistant",
                         "content": "第%d句" % i})
        with mock.patch.object(rank, "distill", return_value=[]) as dist:
            rank._process("backfill", "web_10001__rem", {"rows": rows})
        self.assertEqual(dist.call_count, 3)
        self.assertEqual(dist.call_args_list[0][1]["batch_id"], "batch-1-2-0")
        self.assertEqual(dist.call_args_list[1][1]["batch_id"], "batch-3-4-0")
        self.assertEqual(dist.call_args_list[2][1]["batch_id"], "batch-5-6-0")


class PastTensePromptTest(unittest.TestCase):
    """2026-11-10：老 fact 里的「明天/下次」是相对时间，召回时无从判断。"""

    def test_batch_narrative_asks_for_a_past_record(self):
        self.assertIn("当时记下的一笔", rank._MEMORY_PROMPT)

    def test_atomic_facts_ask_for_past_tense(self):
        self.assertIn("过去式的一笔记录", rank._MEMORY_PROMPT)

    def test_prompt_still_formats_without_new_placeholders(self):
        """新增的是纯文字；.format 的参数集合没变。"""
        text = rank._MEMORY_PROMPT.format(
            start=1, end=5, dialogue="测试", name="纱雾")
        self.assertIn("当时记下的一笔", text)


if __name__ == "__main__":
    unittest.main()

class ExtraLinesTest(unittest.TestCase):
    """展示页的「母本知识库 + 访客私有线」两路合并（2026-11-15）。

    单库路径必须一点不变——那是角色扮演正在跑的生产链路。
    """

    def _patch(self, retrieve=None, raw=None, anchors=(), recent=()):
        return [
            mock.patch.object(rank, "retrieve", side_effect=retrieve or (lambda *a, **k: [])),
            mock.patch.object(rank, "retrieve_raw", side_effect=raw or (lambda *a, **k: [])),
            mock.patch.object(rank, "_anchor_docs", return_value=list(anchors)),
            mock.patch.object(rank, "_recent_docs", return_value=list(recent)),
        ]

    def test_single_line_still_calls_one_store_only(self):
        calls = []

        def fake_retrieve(line, query, k=None, day=None):
            calls.append(line)
            return [{"id": "a", "content": "用户：怕打雷。", "distance": 0.3,
                     "metadata": {"kind": "fact", "about": "user"}}]

        patches = self._patch(retrieve=fake_retrieve)
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        block = rank.build_block("web_10001__rem", "天气")
        self.assertIn("怕打雷", block)
        self.assertEqual(calls, ["web_10001__rem"])

    def test_extra_line_is_searched_and_tagged(self):
        def fake_retrieve(line, query, k=None, day=None):
            if line == "showcase_guide_v1":
                return [{"id": "kb-1", "content": "用户：鮟鱇头顶有发光钓竿。",
                         "distance": 0.42,
                         "metadata": {"kind": "fact", "about": "user"}}]
            return []

        patches = self._patch(retrieve=fake_retrieve)
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        trace = {}
        block = rank.build_block("showcase_abc", "会发光的鱼",
                                 trace=trace, extra_lines=("showcase_guide_v1",))
        self.assertIn("鮟鱇", block)
        self.assertEqual(trace["hits"][0]["line"], "showcase_guide_v1")
        self.assertEqual(trace["hit_count"], 1)

    def test_missing_distance_is_not_reported_as_perfect_match(self):
        """`_recent_docs` 补进来的条目没有 distance，不能报成 0.000 或 1.000。"""
        recent = [{"id": "recent-1", "content": "【第3天】早晨在海洋馆门口集合。",
                   "metadata": {"kind": "batch"}}]
        docs = [{"id": "f-1", "content": "用户：喜欢深海区。", "distance": 0.35,
                 "metadata": {"kind": "fact", "about": "user"}}]
        patches = self._patch(retrieve=lambda *a, **k: list(docs), recent=recent)
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        trace = {}
        rank.build_block("web_10001__rem", "天气", trace=trace)
        # recent 不在召回候选（hits）里，它是被规则直接挑进注入块的，
        # 轨迹写在 selected 上。
        picked = [hit for hit in trace["selected"] if hit["id"] == "recent-1"]
        self.assertTrue(picked, "最近批次没进轨迹")
        self.assertTrue(picked[0]["no_distance"])
        self.assertIsNone(picked[0]["distance"])
        self.assertIsNone(picked[0]["similarity"])
        # 有真实距离的那条照旧报 similarity
        self.assertAlmostEqual(
            [h["similarity"] for h in trace["hits"] if h["id"] == "f-1"][0], 0.65)

    def test_merge_dedups_same_id_across_stores(self):
        shared = {"id": "dup", "content": "同一条", "distance": 0.30,
                  "metadata": {"kind": "fact"}}

        def fake_retrieve(line, query, k=None, day=None):
            return [dict(shared, _line=line)]

        patches = self._patch(retrieve=fake_retrieve)
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        trace = {}
        rank.build_block("line-a", "问", trace=trace, extra_lines=("line-b",))
        ids = [hit["id"] for hit in trace["hits"]]
        self.assertEqual(ids.count("dup"), 1)

    def test_max_hits_caps_candidates_without_touching_thresholds(self):
        """展示页的「召回上限」：只截候选，锚点与最近批次照旧补入。

        2026-11-16 用户反馈召回 19 条太多。截的是距离更远的候选——
        它们本来也过不了阈值，所以注入块不受影响。
        """
        docs = [{"id": "d%d" % i, "content": "用户：事实 %d。" % i,
                 "distance": 0.30 + i * 0.01,
                 "metadata": {"kind": "fact", "about": "user"}} for i in range(8)]
        anchor = [{"id": "anchor-1", "content": "用户：第一次来是在开门那天。",
                   "distance": 0.20, "metadata": {"kind": "fact", "about": "user"}}]
        patches = self._patch(retrieve=lambda *a, **k: list(docs), anchors=anchor)
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        trace = {}
        rank.build_block("line-a", "问", trace=trace, max_hits=5)
        # 5 条检索候选 + 1 条锚点：锚点是规则补入，不吃这个上限
        self.assertEqual(trace["hit_count"], 6)
        self.assertEqual(trace["hits"][0]["id"], "anchor-1")

    def test_no_max_hits_keeps_every_candidate(self):
        """默认 None：生产路径一条都不许截。"""
        docs = [{"id": "d%d" % i, "content": "用户：事实 %d。" % i,
                 "distance": 0.30 + i * 0.01,
                 "metadata": {"kind": "fact", "about": "user"}} for i in range(4)]
        patches = self._patch(retrieve=lambda *a, **k: list(docs))
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        trace = {}
        rank.build_block("line-a", "问", trace=trace)
        self.assertEqual(trace["hit_count"], 4)

    def test_dual_query_runs_both_when_raw_differs(self):
        """消解短语与原话不同 → 两路向量召回，RRF 融合。"""
        calls = []

        def fake_retrieve(line, query, k=None, day=None):
            calls.append(query)
            return [{"id": "x", "content": "事实",
                     "metadata": {"kind": "fact"}}]

        patches = self._patch(retrieve=fake_retrieve)
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        trace = {}
        rank.build_block("line-a", "消解后的短语", trace=trace,
                         query_raw="用户原话")
        self.assertEqual(calls, ["消解后的短语", "用户原话"])
        self.assertTrue(trace["dual_query"])

    def test_single_query_when_raw_identical(self):
        """两路文本相同（memory_query 空退回原话）→ 只查一路，不重复烧。"""
        calls = []

        def fake_retrieve(line, query, k=None, day=None):
            calls.append(query)
            return [{"id": "x", "content": "事实",
                     "metadata": {"kind": "fact"}}]

        patches = self._patch(retrieve=fake_retrieve)
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        trace = {}
        rank.build_block("line-a", "用户原话", trace=trace, query_raw="用户原话")
        self.assertEqual(len(calls), 1)
        self.assertFalse(trace["dual_query"])


class RrfMergeTest(unittest.TestCase):
    """RRF 位次融合（2026-11-20）：多路去重，多路命中的排前面。"""

    @staticmethod
    def _doc(eid):
        return {"id": eid, "content": eid, "metadata": {"kind": "fact"}}

    def test_dedup_and_favors_multi_hit(self):
        l1 = [self._doc("a"), self._doc("b"), self._doc("c")]
        l2 = [self._doc("b"), self._doc("d"), self._doc("a")]
        out = rank._rrf_merge([l1, l2])
        ids = [d["id"] for d in out]
        self.assertEqual(len(ids), 4)
        self.assertEqual(set(ids), {"a", "b", "c", "d"})
        # 两路都命中的 a、b 排在只命中一路的 c、d 前面
        self.assertIn(ids.index("a"), (0, 1))
        self.assertIn(ids.index("b"), (0, 1))

    def test_single_list_keeps_order(self):
        out = rank._rrf_merge([[self._doc("x"), self._doc("y")]])
        self.assertEqual([d["id"] for d in out], ["x", "y"])

    def test_empty_lists(self):
        self.assertEqual(rank._rrf_merge([]), [])
        self.assertEqual(rank._rrf_merge([[], []]), [])


class StaleDedupeTest(unittest.TestCase):
    """时效裁决（2026-11-20）：同主题事实只留较新的一条。"""

    @staticmethod
    def _fact(fid, content, day, turn):
        return {"id": fid, "content": content,
                "metadata": {"kind": "fact", "day": day,
                             "turn_start": turn, "turn_end": turn}}

    def test_older_duplicate_fact_is_dropped(self):
        old = self._fact("f1", "用户：喜欢草莓蛋糕。", 1, 5)
        new = self._fact("f2", "用户：很喜欢草莓蛋糕，每次都要点。", 4, 20)
        out = rank._superseded_dedupe([old, new])
        ids = [d["id"] for d in out]
        self.assertIn("f2", ids)
        self.assertNotIn("f1", ids)

    def test_distinct_facts_kept(self):
        a = self._fact("f1", "用户：养了一只橘猫叫大福。", 1, 5)
        b = self._fact("f2", "用户：周末喜欢去海边散步。", 2, 10)
        out = rank._superseded_dedupe([a, b])
        self.assertEqual({d["id"] for d in out}, {"f1", "f2"})

    def test_anchor_batch_raw_not_touched(self):
        anchor = {"id": "a1", "content": "第一次在卧室。",
                  "metadata": {"kind": "anchor"}}
        batch = {"id": "b1", "content": "【第1天】经过。",
                 "metadata": {"kind": "batch"}}
        raw = {"id": "raw-1-u", "content": "用户：原话。",
               "metadata": {"kind": "raw"}}
        out = rank._superseded_dedupe([anchor, batch, raw])
        self.assertEqual([d["id"] for d in out], ["a1", "b1", "raw-1-u"])


class ForgetScanTest(RagBase):
    """遗忘扫描：fact 旧版覆盖 + 超龄原话；锚点/批次/近期原话受保护。"""

    def setUp(self):
        super().setUp()
        self._keep = rank._RAW_KEEP_BATCHES
        rank._RAW_KEEP_BATCHES = 1      # 一批 2 轮（INGEST_EVERY=2），只留最近 2 轮

    def tearDown(self):
        rank._RAW_KEEP_BATCHES = self._keep
        super().tearDown()

    def _seed(self):
        self.client.entries = [
            {"id": "anchor-1", "content": "第一次。",
             "metadata": {"kind": "anchor", "day": 1, "turn_start": 3,
                          "turn_end": 3}},
            {"id": "batch-1", "content": "经过。",
             "metadata": {"kind": "batch", "day": 1, "turn_start": 3,
                          "turn_end": 5}},
            {"id": "f1", "content": "用户：喜欢草莓蛋糕。",
             "metadata": {"kind": "fact", "day": 1, "turn_start": 3,
                          "turn_end": 5}},
            {"id": "f2", "content": "用户：很喜欢草莓蛋糕，每次都点。",
             "metadata": {"kind": "fact", "day": 4, "turn_start": 18,
                          "turn_end": 20}},
            {"id": "raw-1-u", "content": "最旧原话",
             "metadata": {"kind": "raw", "turn": 1}},
            {"id": "raw-2-u", "content": "旧原话",
             "metadata": {"kind": "raw", "turn": 2}},
            {"id": "raw-3-u", "content": "近原话",
             "metadata": {"kind": "raw", "turn": 3}},
            {"id": "raw-25-u", "content": "最新原话",
             "metadata": {"kind": "raw", "turn": 25}},
        ]

    def test_dry_run_lists_candidates_without_deleting(self):
        self._seed()
        n = rank._scan_stale(self.client, "coll", "web_x", dry_run=True)
        self.assertGreaterEqual(n, 1)
        self.assertEqual(self.client.deleted_entries, [])

    def test_real_delete_keeps_protected_and_recent(self):
        self._seed()
        rank._scan_stale(self.client, "coll", "web_x", dry_run=False)
        deleted = {eid for _, eid in self.client.deleted_entries}
        self.assertIn("f1", deleted)        # 旧版 fact 被覆盖
        self.assertIn("raw-1-u", deleted)   # 超龄原话
        self.assertIn("raw-2-u", deleted)
        self.assertNotIn("f2", deleted)
        self.assertNotIn("raw-3-u", deleted)
        self.assertNotIn("raw-25-u", deleted)
        self.assertNotIn("anchor-1", deleted)
        self.assertNotIn("batch-1", deleted)
