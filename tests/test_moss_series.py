"""`app/moss_series.py` + `app/moss_msgs.py` 的档案层测试。

连载能不能用，全看这一层记不记得住、删得干不干净。三个最容易出错的地方
各钉一组：

1. **分支**（群聊）：同一页号可能有多份，`latest()` 必须给最后创建的那份，
   不然 B 从第 5 页重开之后，下一个续集又接回 A 的分支去了。
2. **截断**（私聊）：回头之后后面的页要标死，但**图不能删**——用户回头
   可能还想捞回来。
3. **重画**：换种子顶替同一页，旧版本进 history，页号 parent 都不动。

全部用临时目录，不碰真档案。
"""

import os
import shutil
import tempfile
import unittest

from app import moss_msgs, moss_series


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._old = (moss_series.STATE_ROOT, moss_series.INDEX_FILE,
                     moss_msgs.LEDGER_FILE)
        moss_series.STATE_ROOT = self.tmp
        moss_series.INDEX_FILE = os.path.join(self.tmp, "_index.json")
        moss_msgs.LEDGER_FILE = os.path.join(self.tmp, "_msgs.json")

    def tearDown(self):
        (moss_series.STATE_ROOT, moss_series.INDEX_FILE,
         moss_msgs.LEDGER_FILE) = self._old
        shutil.rmtree(self.tmp, ignore_errors=True)


class SeriesBasicsTest(_Base):
    def test_new_series_has_a_four_char_code(self):
        s = moss_series.open_new("group", "1103174141", title="醒来之后")
        self.assertEqual(len(s["id"]), 4)
        self.assertEqual(s["pages"], [])
        # 编号里不许出现会看错的字符
        for ch in s["id"]:
            self.assertIn(ch, moss_series._ID_CHARS)

    def test_two_series_never_collide(self):
        a = moss_series.open_new("group", "1")["id"]
        b = moss_series.open_new("group", "2")["id"]
        self.assertNotEqual(a, b)

    def test_page_number_follows_parent(self):
        s = moss_series.open_new("group", "1")
        sid = s["id"]
        p1 = moss_series.mutate(sid, lambda x: moss_series.add_page(
            x, parent_id="", task="T1", brief="一", seed=1))
        p2 = moss_series.mutate(sid, lambda x: moss_series.add_page(
            x, parent_id=p1["id"], task="T2", brief="二", seed=2))
        self.assertEqual((p1["n"], p2["n"]), (1, 2))

    def test_code_roundtrip(self):
        s = moss_series.open_new("group", "1")
        code = moss_series.code_of(s["id"], 7)
        self.assertEqual(moss_series.parse_code("《x》· %s · 第 07 页" % code),
                         (s["id"], 7))

    def test_parse_code_on_garbage(self):
        for text in ("", "今天天气不错", "第 7 页", "ABCD-P", "没有编号"):
            self.assertEqual(moss_series.parse_code(text), ("", 0), text)

    def test_strip_code_keeps_the_story(self):
        """编号是定位信息，绝不能留在剧情里被当成画面内容画出来。

        只负责抹编号——「续集」这个词是 `parse` 阶段就切掉的，不归这里管。
        """
        s = moss_series.open_new("group", "1")
        code = moss_series.code_of(s["id"], 2)
        got = moss_series.strip_code("%s 她推门进来" % code)
        self.assertEqual(got, "她推门进来")
        self.assertNotIn(code, got)


class BranchTest(_Base):
    """群聊：同一页号可以有多份，谁最后跑完就接谁。"""

    def setUp(self):
        super().setUp()
        self.sid = moss_series.open_new("group", "1")["id"]
        self.p1 = moss_series.mutate(self.sid, lambda x: moss_series.add_page(
            x, parent_id="", task="T1", brief="一", seed=1, summary="开场"))

    def test_same_page_number_two_branches(self):
        a = moss_series.mutate(self.sid, lambda x: moss_series.add_page(
            x, parent_id=self.p1["id"], task="T2a", brief="A 分支", seed=2))
        b = moss_series.mutate(self.sid, lambda x: moss_series.add_page(
            x, parent_id=self.p1["id"], task="T2b", brief="B 分支", seed=3))
        s = moss_series.load(self.sid)
        self.assertEqual(a["n"], b["n"])          # 页号相同
        self.assertNotEqual(a["id"], b["id"])     # id 必须不同
        self.assertEqual(len(moss_series.siblings(s, 2)), 2)

    def test_latest_is_the_last_created_not_the_biggest_n(self):
        """A 从第 5 页跑到 6a/7a，B 从第 5 页跑出 6b —— latest 必须是 6b。"""
        p2 = moss_series.mutate(self.sid, lambda x: moss_series.add_page(
            x, parent_id=self.p1["id"], task="T2", brief="二", seed=2))
        p3 = moss_series.mutate(self.sid, lambda x: moss_series.add_page(
            x, parent_id=p2["id"], task="T3", brief="三", seed=3))
        b = moss_series.mutate(self.sid, lambda x: moss_series.add_page(
            x, parent_id=self.p1["id"], task="T2b", brief="另一个二", seed=4))
        self.assertEqual(moss_series.latest(moss_series.load(self.sid))["id"],
                         b["id"])
        self.assertEqual(moss_series.latest(moss_series.load(self.sid))["n"], 2)
        self.assertNotEqual(b["id"], p3["id"])

    def test_active_chain_skips_the_other_branch(self):
        p2 = moss_series.mutate(self.sid, lambda x: moss_series.add_page(
            x, parent_id=self.p1["id"], task="T2", brief="二", seed=2))
        moss_series.mutate(self.sid, lambda x: moss_series.add_page(
            x, parent_id=p2["id"], task="T3", brief="三", seed=3))
        b2 = moss_series.mutate(self.sid, lambda x: moss_series.add_page(
            x, parent_id=self.p1["id"], task="T2b", brief="另一个二", seed=4))
        chain = moss_series.active_chain(moss_series.load(self.sid))
        self.assertEqual([p["id"] for p in chain], [self.p1["id"], b2["id"]])


class TruncateTest(_Base):
    """私聊：回头之后后面的页标死，图留盘。"""

    def setUp(self):
        super().setUp()
        self.sid = moss_series.open_new("private", "1627547639")["id"]
        self.pages = []
        parent = ""
        for i in range(1, 6):
            pg = moss_series.mutate(self.sid, lambda x, p=parent, k=i:
                                    moss_series.add_page(
                                        x, parent_id=p, task="T%d" % k,
                                        brief="第%d页" % k, seed=k,
                                        summary="第 %d 页" % k))
            self.pages.append(pg)
            parent = pg["id"]

    def test_truncate_marks_later_pages_dead(self):
        killed = moss_series.mutate(
            self.sid, lambda x: moss_series.truncate(x, self.pages[1]["id"])) or []
        self.assertEqual([p["n"] for p in killed], [3, 4, 5])
        s = moss_series.load(self.sid)
        self.assertEqual([p["n"] for p in moss_series.alive(s)], [1, 2])

    def test_dead_pages_are_kept_on_disk(self):
        """图不能删——用户回头可能还想捞回来。"""
        moss_series.mutate(
            self.sid, lambda x: moss_series.truncate(x, self.pages[1]["id"]))
        s = moss_series.load(self.sid)
        self.assertEqual(len(s["pages"]), 5)      # 一条都没少
        self.assertEqual(len([p for p in s["pages"] if p["dead"]]), 3)

    def test_latest_after_truncate_is_the_new_tail(self):
        moss_series.mutate(
            self.sid, lambda x: moss_series.truncate(x, self.pages[1]["id"]))
        s = moss_series.load(self.sid)
        self.assertEqual(moss_series.latest(s)["n"], 2)

    def test_continue_after_truncate_reuses_the_page_number(self):
        moss_series.mutate(
            self.sid, lambda x: moss_series.truncate(x, self.pages[1]["id"]))
        again = moss_series.mutate(self.sid, lambda x: moss_series.add_page(
            x, parent_id=self.pages[1]["id"], task="T3new", brief="重开", seed=9))
        self.assertEqual(again["n"], 3)           # 还是第 3 页
        self.assertEqual(len(moss_series.siblings(moss_series.load(self.sid), 3)), 1)

    def test_prev_lines_only_walks_the_live_chain(self):
        """前情不能带上被截断那几页——否则编剧会接着一条已经作废的线写。"""
        moss_series.mutate(
            self.sid, lambda x: moss_series.truncate(x, self.pages[1]["id"]))
        s = moss_series.load(self.sid)
        text = moss_series.prev_lines(s, self.pages[1]["id"])
        self.assertIn("第 01 页", text)
        self.assertIn("第 02 页", text)
        self.assertNotIn("第 03 页", text)


class RedoTest(_Base):
    """重画：换种子顶替同一页，旧版本进 history。"""

    def setUp(self):
        super().setUp()
        self.sid = moss_series.open_new("group", "1")["id"]
        self.pg = moss_series.mutate(self.sid, lambda x: moss_series.add_page(
            x, parent_id="", task="T1", brief="一", seed=111, summary="开场",
            scene="便利店门内"))

    def test_bump_keeps_page_number_and_parent(self):
        pg, old = moss_series.mutate(self.sid, lambda x: moss_series.bump(
            x, self.pg["id"], task="T1b", seed=222))
        self.assertEqual(pg["n"], 1)
        self.assertEqual(pg["parent"], "")
        self.assertEqual(pg["task"], "T1b")
        self.assertEqual(pg["seed"], 222)
        self.assertEqual(old["seed"], 111)

    def test_bump_keeps_the_old_shot_in_history(self):
        moss_series.mutate(self.sid, lambda x: moss_series.bump(
            x, self.pg["id"], task="T1b", seed=222))
        s = moss_series.load(self.sid)
        pg = moss_series.by_id(s, self.pg["id"])
        self.assertEqual(len(pg["history"]), 1)
        self.assertEqual(pg["history"][0]["task"], "T1")

    def test_redo_does_not_add_a_page(self):
        moss_series.mutate(self.sid, lambda x: moss_series.bump(
            x, self.pg["id"], task="T1b", seed=222))
        self.assertEqual(len(moss_series.load(self.sid)["pages"]), 1)

    def test_restore_brings_the_old_one_back(self):
        moss_series.mutate(self.sid, lambda x: moss_series.bump(
            x, self.pg["id"], task="T1b", seed=222))
        ok = moss_series.mutate(self.sid, lambda x: moss_series.restore(
            x, self.pg["id"]))
        self.assertTrue(ok)
        pg = moss_series.by_id(moss_series.load(self.sid), self.pg["id"])
        self.assertEqual(pg["task"], "T1")
        self.assertEqual(pg["seed"], 111)


class SharingTest(_Base):
    """跨会话共用同一份剧情（用户要的「一对 N」）。"""

    def test_group_and_private_point_to_the_same_series(self):
        sid = moss_series.open_new("group", "1103174141")["id"]
        moss_series.bind("private", "1627547639", sid)
        self.assertEqual(moss_series.current("private", "1627547639"), sid)
        self.assertEqual(moss_series.current("group", "1103174141"), sid)

    def test_new_series_rebinds_only_its_own_session(self):
        a = moss_series.open_new("group", "1")["id"]
        b = moss_series.open_new("private", "2")["id"]
        self.assertEqual(moss_series.current("group", "1"), a)
        self.assertEqual(moss_series.current("private", "2"), b)
        moss_series.bind("private", "2", a)        # 私聊接了群的线
        self.assertEqual(moss_series.current("private", "2"), a)
        self.assertEqual(moss_series.current("group", "1"), a)
        self.assertNotEqual(a, b)

    def test_current_is_empty_when_the_series_is_gone(self):
        sid = moss_series.open_new("group", "1")["id"]
        shutil.rmtree(os.path.join(self.tmp, sid))
        self.assertEqual(moss_series.current("group", "1"), "")

    def test_data_is_shared_not_copied(self):
        sid = moss_series.open_new("group", "1")["id"]
        moss_series.mutate(sid, lambda x: moss_series.add_page(
            x, parent_id="", task="T1", brief="一", seed=1))
        moss_series.bind("private", "2", sid)
        self.assertEqual(len(moss_series.load(
            moss_series.current("private", "2"))["pages"]), 1)


class PrevContextTest(_Base):
    """给编剧的那两段前情：摘要全给，场景卡只给上一页。"""

    def setUp(self):
        super().setUp()
        self.sid = moss_series.open_new("group", "1")["id"]
        self.p1 = moss_series.mutate(self.sid, lambda x: moss_series.add_page(
            x, parent_id="", task="T1", brief="一", seed=1,
            summary="她推门进来", scene="门内收银台在右，吊灯暖黄"))
        self.p2 = moss_series.mutate(self.sid, lambda x: moss_series.add_page(
            x, parent_id=self.p1["id"], task="T2", brief="二", seed=2,
            summary="她拿起伞", scene="货架沿左墙，她站在第三排前"))

    def test_prev_lines_lists_every_page_in_order(self):
        text = moss_series.prev_lines(moss_series.load(self.sid), self.p2["id"])
        self.assertEqual(text.split("\n"),
                         ["第 01 页：她推门进来", "第 02 页：她拿起伞"])

    def test_prev_scene_is_only_the_last_page(self):
        """场景卡只给上一页——跨两三页场景本来就要变，给多了互相打架。"""
        text = moss_series.prev_scene(moss_series.load(self.sid), self.p2["id"])
        self.assertIn("货架", text)
        self.assertNotIn("收银台", text)

    def test_prev_lines_skips_pages_without_summary(self):
        moss_series.mutate(self.sid, lambda x: moss_series.fill(
            x, self.p1["id"], summary=""))
        text = moss_series.prev_lines(moss_series.load(self.sid), self.p2["id"])
        self.assertNotIn("第 01 页", text)

    def test_unknown_page_returns_empty(self):
        s = moss_series.load(self.sid)
        self.assertEqual(moss_series.prev_lines(s, "不存在"), "")
        self.assertEqual(moss_series.prev_scene(s, "不存在"), "")


class LedgerTest(_Base):
    """回执账本 + 定位两道保险。"""

    def setUp(self):
        super().setUp()
        self.sid = moss_series.open_new("group", "1")["id"]
        self.pg = moss_series.mutate(self.sid, lambda x: moss_series.add_page(
            x, parent_id="", task="T1", brief="一", seed=1))

    def test_remember_then_recall(self):
        moss_msgs.remember("MID-1", self.sid, self.pg["id"], 1)
        rec = moss_msgs.recall("MID-1")
        self.assertEqual(rec["series"], self.sid)
        self.assertEqual(rec["n"], 1)

    def test_missing_id_is_not_recorded(self):
        """拿不到 message_id 只打日志，不抛——图已经发出去了才是正事。"""
        self.assertEqual(moss_msgs.remember("", self.sid, self.pg["id"], 1), "")
        self.assertIsNone(moss_msgs.recall(""))

    def test_locate_by_id_wins(self):
        moss_msgs.remember("MID-1", self.sid, self.pg["id"], 1)
        sid, pg, how = moss_msgs.locate("MID-1", "")
        self.assertEqual((sid, pg["id"], how), (self.sid, self.pg["id"], "id"))

    def test_locate_falls_back_to_the_printed_code(self):
        """转发之后 id 失效：靠卡片正文里印的 `XXXX-P01` 兜底。"""
        code = moss_series.code_of(self.sid, 1)
        sid, pg, how = moss_msgs.locate("MID-转发后不认识",
                                        "《醒来之后》· %s · 第 01 页" % code)
        self.assertEqual((sid, pg["id"], how), (self.sid, self.pg["id"], "code"))

    def test_locate_returns_empty_when_nothing_matches(self):
        sid, pg, how = moss_msgs.locate("MID-别的", "一句普通的话")
        self.assertEqual((sid, pg, how), ("", None, ""))

    def test_ledger_caps_its_size(self):
        keep = moss_msgs.KEEP
        try:
            moss_msgs.KEEP = 5
            for i in range(9):
                moss_msgs.remember("M%02d" % i, self.sid, self.pg["id"], 1)
            self.assertLessEqual(len(moss_msgs._load()), 5)
        finally:
            moss_msgs.KEEP = keep


if __name__ == "__main__":
    unittest.main()
