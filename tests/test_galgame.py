"""galgame 模式的测试。

三条是这个模块的底线，测试就围着它们写：

1. **选项由代码从剧本取，模型碰不到**——模型再怎么乱写，分支不会错
2. **选项之外的话不推进剧情**——「我想打招呼」不会把故事带到别处
3. **同一个节点只出一次 CG**——读档回去不重画，一张几十秒

LLM 一律 mock 掉：这里测的是状态机，不是模型写得好不好。
"""

import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from app import galgame


def _fake_llm(msgs):
    return "她抬眼看了你一下，把素描本往怀里收了收。「……嗯。」"


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="gal_")
        self._patches = [
            mock.patch.object(galgame, "_STATE_PATH",
                              os.path.join(self.tmp, "sessions.json")),
            mock.patch.object(galgame, "_SAVE_DIR",
                              os.path.join(self.tmp, "saves")),
            mock.patch.object(galgame.llm, "call_llm", _fake_llm),
        ]
        for p in self._patches:
            p.start()
        self.key = "private_10001"

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def node(self):
        return (galgame._entry(self.key) or {}).get("node")

    def start(self):
        return galgame.handle_command(self.key, "gal 开局")

    def say(self, text):
        return galgame.run_turn(self.key, text, "private", "测试")


class CommandTest(_Base):
    def test_not_a_command(self):
        for t in ("hello", "galaxy 是什么意思", "gallery 打不开", "我 gal 了"):
            self.assertIsNone(galgame.handle_command(self.key, t), t)

    def test_bare_gal_shows_help(self):
        turn = galgame.handle_command(self.key, "gal")
        self.assertIn("gal 开局", turn.reply)
        self.assertFalse(galgame.active(self.key))

    def test_start_puts_session_into_game(self):
        turn = self.start()
        self.assertTrue(galgame.active(self.key))
        self.assertEqual(self.node(), "ch1")
        # 选项是代码拼的，一定在
        self.assertIn("A. 先开口打个招呼", turn.reply)
        self.assertIn("B. 装作没看见", turn.reply)

    def test_start_unknown_script_lists_available(self):
        turn = galgame.handle_command(self.key, "gal 开局 不存在的剧本")
        self.assertIn("找不到剧本", turn.reply)
        self.assertIn("sakura", turn.reply)
        self.assertFalse(galgame.active(self.key))

    def test_start_by_title(self):
        turn = galgame.handle_command(self.key, "gal 开局 樱花庄的午后")
        self.assertTrue(galgame.active(self.key))
        self.assertEqual(self.node(), "ch1")

    def test_end_and_status(self):
        self.start()
        st = galgame.handle_command(self.key, "gal 状态").reply
        self.assertIn("樱花树下", st)          # 当前节点标题
        self.assertIn("好感度：0", st)
        turn = galgame.handle_command(self.key, "gal 结束")
        self.assertIn("游戏结束", turn.reply)
        self.assertFalse(galgame.active(self.key))
        # 不在游戏里再说结束，不该报错
        self.assertIn("没在玩", galgame.handle_command(self.key, "gal 结束").reply)

    def test_scripts_list(self):
        turn = galgame.handle_command(self.key, "gal 剧本")
        self.assertIn("sakura", turn.reply)
        self.assertIn("樱花庄的午后", turn.reply)


class OptionTest(_Base):
    def test_a_moves_and_adds_affection(self):
        self.start()
        turn = self.say("A")
        self.assertEqual(self.node(), "ch2a")
        self.assertEqual(galgame._entry(self.key)["vars"]["aff"], 5)
        self.assertIn("A. 蹲下捡起素描本还给她", turn.reply)

    def test_number_key(self):
        self.start()
        self.say("1")
        self.assertEqual(self.node(), "ch2a")

    def test_prefix_and_punctuation(self):
        self.start()
        for form in ("选B", "b", "B.", "B、", "选 B"):
            with self.subTest(form=form):
                galgame.leave(self.key)
                self.start()
                self.say(form)
                self.assertEqual(self.node(), "ch2b")

    def test_free_text_does_not_advance(self):
        self.start()
        self.say("我想跟她打招呼")
        self.say("你好呀")
        self.say("算了")
        self.assertEqual(self.node(), "ch1")

    def test_out_of_range_letter_ignored(self):
        self.start()
        self.say("D")            # 只有 A/B
        self.assertEqual(self.node(), "ch1")

    def test_merged_batch_takes_first_option(self):
        """群里两人前后脚各发一个 A，_dispatch 会合并成 "A\\nA"。"""
        self.start()
        self.say("A\nA")
        self.assertEqual(self.node(), "ch2a")


class EndingTest(_Base):
    def test_terminal_node_ends_game(self):
        self.start()
        self.say("B")            # ch2b
        turn = self.say("B")     # end_stranger，没有 opts
        self.assertTrue(turn.ended)
        self.assertIn("【擦肩而过】", turn.reply)
        self.assertFalse(galgame.active(self.key))

    def test_good_ending_reachable(self):
        self.start()
        self.say("A")            # ch2a
        self.say("A")            # ch3a
        turn = self.say("A")     # end_good
        self.assertTrue(turn.ended)
        self.assertIn("【樱花色的下午】", turn.reply)

    def test_no_option_text_on_ending(self):
        self.start()
        self.say("B")
        turn = self.say("B")
        self.assertNotIn("A.", turn.reply)


class CardTest(_Base):
    def test_each_node_only_draws_once(self):
        """同一个节点第二次到达不再出图。

        用自环剧本测：`n1 --A--> n1`。第一次进 n1 出图，第二次不该再出。
        （读档会把 `seen` 一起回退，所以读档后重走是会重画的——那是预期行为，
        这里不测它。）
        """
        loop = {
            "id": "loop", "title": "循环", "start": "n1",
            "nodes": {
                "n1": {"say": "还是这里", "cg": "a cat on the windowsill",
                       "opts": [{"k": "A", "t": "再走一圈", "to": "n1"}]},
            },
        }
        old = galgame.SCRIPTS_DIR
        try:
            galgame.SCRIPTS_DIR = self.tmp
            with open(os.path.join(self.tmp, "loop.json"), "w",
                      encoding="utf-8") as f:
                json.dump(loop, f, ensure_ascii=False)

            first = galgame.handle_command(self.key, "gal 开局 loop")
            self.assertEqual(first.scene, "a cat on the windowsill")

            second = galgame.run_turn(self.key, "A", "private")
            self.assertEqual(second.scene, "")     # 画过了，不再画
            self.assertEqual(second.reply.count("A. 再走一圈"), 1)
        finally:
            galgame.SCRIPTS_DIR = old

    def test_node_without_cg_returns_empty_scene(self):
        self.start()
        self.say("B")            # ch2b 没写 cg
        turn = self.say("A")     # ch3a 也没写 cg
        self.assertEqual(turn.scene, "")


class SaveLoadTest(_Base):
    def test_save_then_load_restores_node(self):
        self.start()
        self.assertIn("存好了", galgame.save_slot(self.key, "s1"))
        self.say("A")
        self.assertEqual(self.node(), "ch2a")

        text = galgame.load_slot(self.key, "s1")
        self.assertIn("樱花", text)          # 回到 ch1 的那段骨架
        self.assertEqual(self.node(), "ch1")

    def test_load_missing_slot(self):
        self.start()
        self.assertIn("没有第", galgame.load_slot(self.key, "nope"))

    def test_leave_drops_saves(self):
        self.start()
        galgame.save_slot(self.key, "s1")
        galgame.leave(self.key)
        self.assertIn("没有第", galgame.load_slot(self.key, "s1"))


class RobustnessTest(_Base):
    def test_llm_failure_falls_back_to_skeleton(self):
        """模型挂了也要说话：回退剧本骨架，不能空回。"""
        def boom(msgs):
            raise RuntimeError("model down")

        with mock.patch.object(galgame.llm, "call_llm", boom):
            turn = self.start()
        self.assertTrue(turn.reply.strip())
        self.assertIn("樱花", turn.reply)     # 骨架里的词
        self.assertTrue(galgame.active(self.key))

    def test_llm_empty_reply_falls_back(self):
        with mock.patch.object(galgame.llm, "call_llm", lambda m: ""):
            turn = self.start()
        self.assertTrue(turn.reply.strip())

    def test_broken_next_node_stays_put(self):
        """剧本写错（to 指向不存在的节点）——不能崩，也不能把人卡死。"""
        bad = {
            "id": "bad", "title": "坏剧本", "start": "n1",
            "nodes": {
                "n1": {"say": "只有一句话",
                       "opts": [{"k": "A", "t": "走", "to": "n999"}]},
            },
        }
        old = galgame.SCRIPTS_DIR
        try:
            galgame.SCRIPTS_DIR = self.tmp
            with open(os.path.join(self.tmp, "bad.json"), "w",
                      encoding="utf-8") as f:
                json.dump(bad, f, ensure_ascii=False)
            galgame.handle_command(self.key, "gal 开局 bad")
            turn = self.say("A")
            self.assertIn("断掉了", turn.reply)
            self.assertTrue(galgame.active(self.key))   # 还在原地，能重选
            self.assertEqual(self.node(), "n1")
        finally:
            galgame.SCRIPTS_DIR = old

    def test_missing_script_ends_gracefully(self):
        self.start()
        # 改 state 指向一个不存在的剧本，模拟剧本被删/改名
        st = galgame._load_state()
        st[self.key]["script"] = "gone"
        galgame._save_state(st)
        turn = self.say("A")
        self.assertTrue(turn.ended)
        self.assertIn("不见了", turn.reply)
        self.assertFalse(galgame.active(self.key))


class ScopeTest(_Base):
    def test_group_and_private_are_separate(self):
        gk = "group_555"
        self.start()
        galgame.handle_command(gk, "gal 开局")
        self.say("A")
        self.assertEqual(self.node(), "ch2a")
        self.assertEqual((galgame._entry(gk) or {}).get("node"), "ch1")

    def test_same_group_key_shares_progress(self):
        gk = "group_555"
        galgame.handle_command(gk, "gal 开局")
        galgame.run_turn(gk, "A", "group", "甲")
        # 换一个群友说话，进度还是同一条
        galgame.run_turn(gk, "A", "group", "乙")
        self.assertEqual(galgame._entry(gk)["node"], "ch3a")


if __name__ == "__main__":
    unittest.main()
