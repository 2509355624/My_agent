"""galgame 模式的测试。

四条是这个模块的底线，测试就围着它们写：

1. **章内自由，边界收紧**——随便说什么都由模型演，但翻章要过最少轮数
2. **状态由模型提、代码钳**——好感一轮最多动 5，事实表有上限
3. **结局 id 不合法要有兜底**——不能卡在判定循环里出不来
4. **同一章只出一次 CG**——读档回去不重画，一张几十秒

LLM 一律 mock 掉：这里测的是状态机，不是模型写得好不好。
"""

import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from app import galgame


def _reply(say="她把素描本抱得更紧了一点。「……嗯。」", facts=None, aff=0,
           opts=None, ready=False, ending=""):
    """拼一个模型该交的 JSON。"""
    return json.dumps({
        "say": say,
        "facts": facts or [],
        "aff": aff,
        "opts": opts if opts is not None else
        ["把本子还给她", "问她怎么了", "装作没看见"],
        "ready": ready,
        "ending": ending,
    }, ensure_ascii=False)


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="gal_")
        self.replies = []          # 测试自己按顺序填；空了就用默认回复
        self.calls = []            # 每次喂给模型的提示词
        self._patches = [
            mock.patch.object(galgame, "_STATE_PATH",
                              os.path.join(self.tmp, "sessions.json")),
            mock.patch.object(galgame, "_SAVE_DIR",
                              os.path.join(self.tmp, "saves")),
            mock.patch.object(galgame.llm, "call_llm", self._llm),
        ]
        for p in self._patches:
            p.start()
        self.key = "private_10001"

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _llm(self, msgs):
        self.calls.append(msgs[0]["content"])
        if not self.replies:
            return _reply()
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    def ent(self):
        return galgame._entry(self.key) or {}

    def beat(self):
        return self.ent().get("beat")

    def prompt(self):
        """最近一次喂给模型的提示词。"""
        return self.calls[-1] if self.calls else ""

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

    def test_address_prefix_before_gal(self):
        """叫了机器人的消息，前缀（@ / 昵称）要先剥掉再认命令。

        2026-11-02 用户群里实测：发「大大怪 gal」，关键词把消息放行了，
        但正文带着名字、命令只认整条以 gal 开头，于是落到普通聊天链，
        机器人回了一句「gal 有两条路子」。对用户就是「喊了名字它装没听见」。
        """
        for t in ("大大怪 gal 开局", "@大大怪 gal 开局", "大大怪，gal 开局",
                  "@大大怪 大大怪 gal 开局", "大大怪gal开局"):
            galgame.leave(self.key)
            turn = galgame.handle_command(self.key, t, addressed=True)
            self.assertIsNotNone(turn, t)
            self.assertTrue(galgame.active(self.key), t)
            self.assertEqual(self.beat(), 0, t)

    def test_prefix_ignored_when_not_addressed(self):
        """没叫机器人的句子不算命令，否则正常聊天会被整条吃掉。"""
        for t in ("大大怪 gal 开局", "@大大怪 gal", "我不想玩 gal 模式"):
            self.assertIsNone(galgame.handle_command(self.key, t), t)

    def test_gal_mode_glued(self):
        """群里打字常不带空格：`gal模式` / `gal状态` 也认。"""
        galgame.leave(self.key)
        self.assertTrue(galgame.active(self.key) is False)
        galgame.handle_command(self.key, "gal模式")
        self.assertTrue(galgame.active(self.key))
        self.assertIn("第 1 / 4 章",
                      galgame.handle_command(self.key, "gal状态").reply)

    def test_galgame_word_still_not_command(self):
        """`galgame` 连写不能因为前缀是 gal 就被吃掉。"""
        self.assertIsNone(galgame.handle_command(self.key, "galgame 怎么玩", True))

    def test_start_puts_session_into_game(self):
        turn = self.start()
        self.assertTrue(galgame.active(self.key))
        self.assertEqual(self.beat(), 0)
        # 建议动作是模型给的，但代码负责编号
        self.assertIn("A. 把本子还给她", turn.reply)
        self.assertIn("B. 问她怎么了", turn.reply)

    def test_start_explains_free_input(self):
        """开场要告诉玩家：选项只是偷懒用的。"""
        turn = self.start()
        self.assertIn("直接说你想做什么", turn.reply)

    def test_start_unknown_script_lists_available(self):
        turn = galgame.handle_command(self.key, "gal 开局 不存在的剧本")
        self.assertIn("找不到剧本", turn.reply)
        self.assertIn("sakura", turn.reply)
        self.assertFalse(galgame.active(self.key))

    def test_start_by_title(self):
        galgame.handle_command(self.key, "gal 开局 樱花庄的午后")
        self.assertTrue(galgame.active(self.key))
        self.assertEqual(self.beat(), 0)

    def test_end_and_status(self):
        self.start()
        st = galgame.handle_command(self.key, "gal 状态").reply
        self.assertIn("放学后的走廊", st)
        self.assertIn("第 1 / 4 章", st)
        self.assertIn("好感：0", st)
        turn = galgame.handle_command(self.key, "gal 结束")
        self.assertIn("游戏结束", turn.reply)
        self.assertFalse(galgame.active(self.key))
        self.assertIn("没在玩", galgame.handle_command(self.key, "gal 结束").reply)

    def test_scripts_list(self):
        turn = galgame.handle_command(self.key, "gal 剧本")
        self.assertIn("sakura", turn.reply)
        self.assertIn("樱花庄的午后", turn.reply)
        self.assertIn("4 章", turn.reply)


class InputTest(_Base):
    def test_free_text_reaches_model_verbatim(self):
        """自由发言必须原样进提示词——这是自由度的入口。"""
        self.start()
        self.say("我伸手把她最上面那本抽了出来")
        p = self.prompt()
        self.assertIn("我伸手把她最上面那本抽了出来", p)
        self.assertIn("这是自由发言", p)

    def test_free_text_does_not_advance_by_itself(self):
        self.start()
        self.say("我伸手把她最上面那本抽了出来")
        self.assertEqual(self.beat(), 0)

    def test_letter_picks_suggestion(self):
        """回 A 不是走分支，是替玩家把那条建议打出来。"""
        self.start()
        self.say("A")
        p = self.prompt()
        self.assertIn("把本子还给她", p)
        self.assertNotIn("【玩家说】A", p)

    def test_number_key(self):
        self.start()
        self.say("1")
        self.assertIn("把本子还给她", self.prompt())

    def test_prefix_and_punctuation(self):
        self.start()
        for form in ("选B", "b", "B.", "B、", "选 B"):
            with self.subTest(form=form):
                self.say(form)
                self.assertIn("问她怎么了", self.prompt())

    def test_out_of_range_letter_is_refused(self):
        """只有三条建议却回 D：别让模型去猜，直接说清楚。"""
        self.start()
        turn = self.say("D")
        self.assertIn("只有", turn.reply)
        self.assertEqual(self.beat(), 0)

    def test_letter_with_extra_words_is_free_text(self):
        """`A 好可爱` 不是在选，是在说话。"""
        self.start()
        self.say("A 好可爱")
        self.assertIn("A 好可爱", self.prompt())

    def test_merged_batch_takes_first_option(self):
        """群里两人前后脚各发一个 A，_dispatch 会合并成 "A\\nA"。"""
        self.start()
        self.say("A\nA")
        self.assertIn("把本子还给她", self.prompt())


class BeatTest(_Base):
    def test_ready_before_min_turns_is_blocked(self):
        """第 1 轮就说到了也不算——min_turns 由代码硬挡。"""
        self.start()
        self.replies.append(_reply(ready=True))
        self.say("我把本子还给她了")
        self.assertEqual(self.beat(), 0)

    def test_ready_after_min_turns_advances(self):
        self.start()
        self.say("我把本子还给她了")          # 第 1 轮，不够
        self.replies.append(_reply(ready=True))
        turn = self.say("我把散落的纸也收好了")  # 第 2 轮，够了
        self.assertEqual(self.beat(), 1)
        self.assertIn("第 2 章", turn.reply)

    def test_max_turns_forces_advance(self):
        """模型一直不肯判定也不能卡死：到上限强制翻章。"""
        self.start()
        for _ in range(5):                    # ch1 的 max_turns = 5
            self.replies.append(_reply(ready=False))
            self.say("我们继续聊")
        self.assertEqual(self.beat(), 1)

    def test_advance_resets_turn(self):
        self.start()
        self.say("a")
        self.replies.append(_reply(ready=True))
        self.say("b")
        self.assertEqual(self.ent().get("turn"), 0)

    def test_beat_goal_reaches_model(self):
        """这一章「必须发生的事」必须让模型看见，否则它不知道往哪收。"""
        self.start()
        self.assertIn("必须发生的事", self.prompt())
        self.assertIn("素描本", self.prompt())


class MemoryTest(_Base):
    """对话记忆：2026-11-02 用户报「剧情很乱」，根子在这儿。

    之前每轮提示词只有章节骨架 + facts，**没有上一轮**，模型只能从零重演
    这一幕，读起来就是她像个失忆的人、台词接不上。
    """

    def _turn(self, text, reply=None):
        if reply is not None:
            self.replies.append(reply)
        return galgame.run_turn(self.key, text, "private", "")

    def test_previous_reply_reaches_prompt(self):
        self.start()
        self._turn("我蹲下去把纸捡起来",
                   _reply(say="她接过来，指尖有点凉。「谢谢。」"))
        self._turn("你手怎么这么凉")
        p = self.prompt()
        self.assertIn("【刚才的对话】", p)
        self.assertIn("我蹲下去把纸捡起来", p)
        self.assertIn("指尖有点凉", p)

    def test_history_rolls_and_is_capped(self):
        self.start()
        for i in range(galgame.HISTORY_MAX + 3):
            self._turn("第%d句" % i, _reply(say="回%d" % i))
        self._turn("最后一句")
        p = self.prompt()
        self.assertNotIn("第0句", p)                     # 最旧的被挤出去了
        self.assertIn("第%d句" % (galgame.HISTORY_MAX + 2), p)
        self.assertLessEqual(len(self.ent().get("log") or []),
                             galgame.HISTORY_MAX)

    def test_tension_is_not_blurted(self):
        """心里悬着的事不能当台词直接摊开，否则第一章就剧透完了。"""
        self.start()
        self.assertIn("不会主动说出口", self.prompt())

    def test_new_chapter_asks_for_transition(self):
        """翻章要给过渡，不能硬切。"""
        self.start()
        self._turn("我蹲下去把纸捡起来", _reply(ready=True))   # 第 1 轮，被最少轮数挡住
        self.assertEqual(self.beat(), 0)
        self._turn("我把纸递到她手里", _reply(ready=True))     # 第 2 轮，翻章
        self.assertEqual(self.beat(), 1)
        self._turn("这里是天台？")
        p = self.prompt()
        self.assertIn("新的一章开头", p)
        self.assertIn("过渡", p)
        self.assertIn("上一章收尾", p)                    # 历史要标明是上一章的


class StateTest(_Base):
    def _apply(self, ent, out, opening=False):
        sc = galgame.load_script("sakura")
        return galgame._apply(sc, ent, out, opening)[0]

    def _ent(self, **kw):
        e = {"script": "sakura", "beat": 0, "turn": 0, "aff": 0,
             "facts": [], "seen": [], "opts": []}
        e.update(kw)
        return e

    def test_facts_accumulate(self):
        e = self._apply(self._ent(), {"facts": ["她收下了伞", "你碰到了她的手"]})
        self.assertEqual(e["facts"], ["她收下了伞", "你碰到了她的手"])

    def test_facts_dedupe(self):
        e = self._apply(self._ent(facts=["她收下了伞"]), {"facts": ["她收下了伞"]})
        self.assertEqual(e["facts"], ["她收下了伞"])

    def test_facts_capped(self):
        old = ["f%d" % i for i in range(galgame.FACTS_MAX)]
        e = self._apply(self._ent(facts=old), {"facts": ["新的一条"]})
        self.assertEqual(len(e["facts"]), galgame.FACTS_MAX)
        self.assertEqual(e["facts"][-1], "新的一条")
        self.assertNotIn("f0", e["facts"])

    def test_aff_step_clamped(self):
        e = self._apply(self._ent(), {"aff": 99})
        self.assertEqual(e["aff"], galgame.AFF_STEP)

    def test_aff_negative_step_clamped(self):
        e = self._apply(self._ent(), {"aff": -99})
        self.assertEqual(e["aff"], -galgame.AFF_STEP)

    def test_aff_total_clamped(self):
        e = self._apply(self._ent(aff=galgame.AFF_MAX), {"aff": 5})
        self.assertEqual(e["aff"], galgame.AFF_MAX)

    def test_bad_aff_value_ignored(self):
        e = self._apply(self._ent(aff=3), {"aff": "很多"})
        self.assertEqual(e["aff"], 3)

    def test_opts_stored_for_next_turn(self):
        e = self._apply(self._ent(), {"opts": ["喝茶", "走开"]})
        self.assertEqual(e["opts"], ["喝茶", "走开"])


class EndingTest(_Base):
    def test_last_beat_ends_with_model_choice(self):
        self.start()
        # 直接推到最后一章
        st = galgame._load_state()
        st[self.key]["beat"] = 3
        st[self.key]["turn"] = 1
        galgame._save_state(st)
        self.replies.append(_reply(ready=True, ending="best"))
        turn = self.say("你留下吧")
        self.assertTrue(turn.ended)
        self.assertIn("【樱花色的下午】", turn.reply)
        self.assertFalse(galgame.active(self.key))

    def test_invalid_ending_falls_back_to_first(self):
        """模型编了个不存在的结局 id：用第一条兜底，不能卡住。"""
        self.start()
        st = galgame._load_state()
        st[self.key]["beat"] = 3
        st[self.key]["turn"] = 1
        galgame._save_state(st)
        self.replies.append(_reply(ready=True, ending="不存在的结局"))
        turn = self.say("……")
        self.assertTrue(turn.ended)
        self.assertIn("【会再见的】", turn.reply)

    def test_early_ready_on_last_beat_also_ends(self):
        self.start()
        st = galgame._load_state()
        st[self.key]["beat"] = 3
        st[self.key]["turn"] = 1
        galgame._save_state(st)
        self.replies.append(_reply(ready=True, ending="bad"))
        turn = self.say("我什么也没说")
        self.assertTrue(turn.ended)
        self.assertIn("【擦肩而过】", turn.reply)

    def test_no_options_after_ending(self):
        self.start()
        st = galgame._load_state()
        st[self.key]["beat"] = 3
        st[self.key]["turn"] = 1
        galgame._save_state(st)
        self.replies.append(_reply(ready=True, ending="good"))
        turn = self.say("路上小心")
        self.assertNotIn("A. ", turn.reply)

    def test_ending_list_only_shown_on_last_beat(self):
        """早把结局清单给模型，它会提前收尾。"""
        self.start()
        self.assertNotIn("这是最后一章", self.prompt())
        st = galgame._load_state()
        st[self.key]["beat"] = 3
        galgame._save_state(st)
        self.say("我们到了")
        self.assertIn("这是最后一章", self.prompt())


class CGTest(_Base):
    def test_first_beat_draws(self):
        turn = self.start()
        self.assertTrue(turn.scene)
        self.assertIn("corridor", turn.scene)

    def test_same_beat_draws_once(self):
        self.start()
        turn = self.say("我笑了笑")
        self.assertEqual(turn.scene, "")

    def test_advance_turn_does_not_draw(self):
        """翻章那一轮的正文还是上一章的收尾，图和文对不上。"""
        self.start()
        self.say("a")
        self.replies.append(_reply(ready=True))
        turn = self.say("b")            # 翻到 ch2
        self.assertEqual(self.beat(), 1)
        self.assertEqual(turn.scene, "")

    def test_new_beat_draws_on_next_turn(self):
        self.start()
        self.say("a")
        self.replies.append(_reply(ready=True))
        self.say("b")                   # 翻到 ch2，不出图
        turn = self.say("c")            # ch2 第一轮，出图
        self.assertIn("rooftop", turn.scene)


class DegradeTest(_Base):
    def test_llm_failure_falls_back_to_skeleton(self):
        """模型挂了也要说话：回退章节骨架，不能空回。"""
        self.start()
        self.replies.append(RuntimeError("model down"))
        turn = self.say("你好")
        self.assertTrue(turn.reply.strip())
        self.assertIn("走廊", turn.reply)
        self.assertTrue(galgame.active(self.key))
        self.assertEqual(self.beat(), 0)

    def test_empty_reply_falls_back(self):
        self.start()
        self.replies.append("")
        turn = self.say("你好")
        self.assertTrue(turn.reply.strip())

    def test_non_json_reply_is_used_as_text(self):
        """模型没交 JSON：当纯文本用，别把这段好文字丢了。"""
        self.start()
        self.replies.append("她低下头，把素描本往怀里紧了紧。「……谢谢。」")
        turn = self.say("给你")
        self.assertIn("谢谢", turn.reply)
        self.assertEqual(self.beat(), 0)     # 没状态可应用，不推进

    def test_json_in_code_fence_is_parsed(self):
        self.start()
        self.replies.append("```json\n" + _reply(aff=3) + "\n```")
        self.say("给你")
        self.assertEqual(self.ent().get("aff"), 3)

    def test_missing_script_ends_gracefully(self):
        self.start()
        st = galgame._load_state()
        st[self.key]["script"] = "gone"
        galgame._save_state(st)
        turn = self.say("a")
        self.assertTrue(turn.ended)
        self.assertIn("不见了", turn.reply)
        self.assertFalse(galgame.active(self.key))


class SaveLoadTest(_Base):
    def test_save_then_load_restores_beat(self):
        self.start()
        self.assertIn("存好了", galgame.save_slot(self.key, "s1"))
        self.say("a")
        self.replies.append(_reply(ready=True))
        self.say("b")
        self.assertEqual(self.beat(), 1)

        text = galgame.load_slot(self.key, "s1")
        self.assertIn("第 1 章", text)
        self.assertEqual(self.beat(), 0)

    def test_load_missing_slot(self):
        self.start()
        self.assertIn("没有第", galgame.load_slot(self.key, "nope"))

    def test_leave_drops_saves(self):
        self.start()
        galgame.save_slot(self.key, "s1")
        galgame.leave(self.key)
        self.assertIn("没有第", galgame.load_slot(self.key, "s1"))

    def test_old_format_save_is_rejected(self):
        """旧版存档是 nodes 结构，读回来要说清楚，不能猜。"""
        self.start()
        galgame.save_slot(self.key, "old")
        p = os.path.join(self.tmp, "saves", self.key, "old.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"script": "sakura", "node": "ch1"}, f)
        self.assertIn("旧格式", galgame.load_slot(self.key, "old"))


class ScopeTest(_Base):
    def test_group_and_private_are_separate(self):
        gk = "group_555"
        self.start()
        galgame.handle_command(gk, "gal 开局")
        self.say("a")
        self.replies.append(_reply(ready=True))
        self.say("b")
        self.assertEqual(self.beat(), 1)
        self.assertEqual((galgame._entry(gk) or {}).get("beat"), 0)

    def test_same_group_key_shares_progress(self):
        gk = "group_555"
        galgame.handle_command(gk, "gal 开局")
        galgame.run_turn(gk, "a", "group", "甲")
        self.replies.append(_reply(ready=True))
        galgame.run_turn(gk, "b", "group", "乙")
        self.assertEqual(galgame._entry(gk)["beat"], 1)

    def test_group_sender_reaches_prompt(self):
        gk = "group_555"
        galgame.handle_command(gk, "gal 开局")
        galgame.run_turn(gk, "你怎么了", "group", "小明")
        self.assertIn("小明说", self.calls[-1])


if __name__ == "__main__":
    unittest.main()
