# -*- coding: utf-8 -*-
"""人设卡池 + 扮演态测试：app/persona.py。

对应：
- 卡库读写（`agents/<agent>/characters.json`）
- 会话级扮演态（`state/persona_sessions.json`）
- 三条 `#` 命令
- 扮演一轮：**只调 1 次** LLM、正文原样发回、最近 20 条 + 单条 200 字截断
- 输出契约里的「画面：」那一行：解析、剥出正文、拼角色卡固定块、静默出图

用户 2026-10-16 定的语义：
- 命令是 `#角色扮演`（`#人设列表` 同义）/ `#角色名` / `#出戏`，私聊同样要喊一声；
- 扮演态的作用域是**会话线**，A 群在演、B 群照旧是生图机器人；
- 系统头里**只有角色人设 + 输出契约 + 环境**，不拼工具目录、不拼审核话术；
- 扮演态**每轮回话配一张快图**（`skills/_persona_fast`，480×720 / 3 步，
  实测 ≈2~3 秒），画面由角色自己顺手写、长相由卡上的外观字段钉死。

隔离铁律：碰 agents/ 数据必须换整根 AGENTS_DIR 到临时目录（同 test_session_prompt）。
"""

import os
import re
import tempfile
import unittest
from unittest import mock

import app.agents as agents
import app.persona as persona
import app.persona_rag as persona_rag


def _card(name="测试角色", **kw):
    c = {"id": "", "name": name, "description": "", "avatar": "",
         "personality": "", "systemPrompt": "你是%s，说话很轻。" % name,
         "appearancePrompt": "1girl, white hair, red eyes", "outfitPrompt": "",
         "referenceImage": ""}
    c.update(kw)
    return c


class PersonaBase(unittest.TestCase):
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


class CardStoreTest(PersonaBase):
    def test_roundtrip_and_missing_fields(self):
        persona.save_cards([{"name": "纱雾"}])       # 只给名字，其余该补齐
        got = persona.load_cards()
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["name"], "纱雾")
        self.assertTrue(got[0]["id"])                # id 是补出来的
        for k in ("systemPrompt", "appearancePrompt", "outfitPrompt",
                  "referenceImage", "description", "personality", "avatar"):
            self.assertIn(k, got[0])

    def test_blank_name_is_dropped(self):
        persona.save_cards([_card("有名字"), _card("  ")])
        self.assertEqual([c["name"] for c in persona.load_cards()], ["有名字"])

    def test_duplicate_ids_get_fresh_ones(self):
        persona.save_cards([_card("甲", id="same"), _card("乙", id="same")])
        ids = [c["id"] for c in persona.load_cards()]
        self.assertEqual(len(set(ids)), 2)

    def test_broken_file_reads_as_empty(self):
        persona.save_cards([_card("甲")])
        with open(persona._cards_path(), "w", encoding="utf-8") as f:
            f.write("{ 这不是 json")
        self.assertEqual(persona.load_cards(), [])   # 卡库坏了不该让机器人失灵

    def test_find_card_is_exact(self):
        persona.save_cards([_card("和泉纱雾")])
        self.assertIsNotNone(persona.find_card("和泉纱雾"))
        self.assertIsNotNone(persona.find_card("  和泉纱雾  "))
        # 认不出就认不出，**不做模糊匹配**——模糊匹配会把正常问句吃成命令
        self.assertIsNone(persona.find_card("纱雾"))
        self.assertIsNone(persona.find_card("和泉纱雾怎么画"))


class CardHiddenTest(PersonaBase):
    """暂下架开关（2026-11-04）：hidden 只藏不删，会话照常能演。"""

    def test_hidden_roundtrips(self):
        persona.save_cards([_card("可见"), _card("下架", hidden=True)])
        got = {c["name"]: c for c in persona.load_cards()}
        self.assertFalse(got["可见"]["hidden"])
        self.assertTrue(got["下架"]["hidden"])

    def test_builtin_hidden_flag_updates_but_content_is_protected(self):
        """内置卡内容以磁盘为准，但 hidden / 状态下的她要放行。"""
        persona.save_cards([_card("默认卡", id="default_1",
                                  systemPrompt="原始人设")])
        with mock.patch("app.config.BUILTIN_CARD_IDS", {"default_1"}):
            # 调用方试图改内容 + 下架 + 填状态行为：内容改动不算数，后两者算数
            persona.save_cards([_card("默认卡", id="default_1",
                                      systemPrompt="篡改", hidden=True,
                                      stateBehavior={"submit": "她不再顶嘴"})])
        got = persona.load_cards()[0]
        self.assertEqual(got["systemPrompt"], "原始人设")
        self.assertTrue(got["hidden"])
        self.assertEqual(got["stateBehavior"]["submit"], "她不再顶嘴")

    def test_roster_skips_hidden(self):
        persona.save_cards([_card("台上的", description="可见"),
                            _card("幕后的", description="藏起来",
                                  hidden=True)])
        text = persona.roster_text()
        self.assertIn("台上的", text)
        self.assertNotIn("幕后的", text)


class CommandTest(PersonaBase):
    def test_plain_text_is_not_a_command(self):
        persona.save_cards([_card("纱雾")])
        self.assertIsNone(persona.handle_command("private_1", "在吗"))
        self.assertIsNone(persona.handle_command("private_1", ""))
        self.assertIsNone(persona.handle_command("private_1", "#"))
        # `#` 开头但认不出卡名 → 放行，当普通消息
        self.assertIsNone(persona.handle_command("private_1", "#银发少女怎么画"))

    def test_list_enter_leave(self):
        persona.save_cards([_card("纱雾", description="害羞的插画师")])
        out = persona.handle_command("private_1", "#角色扮演")
        self.assertIn("纱雾", out)
        self.assertIn("害羞的插画师", out)
        self.assertIn("出戏", out)
        # 别名同义
        self.assertIn("纱雾", persona.handle_command("private_1", "#人设列表"))

        out = persona.handle_command("private_1", "#纱雾")
        self.assertIn("纱雾", out)
        self.assertIsNotNone(persona.active_card("private_1"))

        out = persona.handle_command("private_1", "#出戏")
        self.assertIn("出戏", out)
        self.assertIsNone(persona.active_card("private_1"))

    def test_leave_when_not_playing_is_not_a_command(self):
        self.assertIsNone(persona.handle_command("private_1", "#出戏"))

    def test_scope_is_per_session_line(self):
        persona.save_cards([_card("纱雾")])
        persona.handle_command("private_1", "#纱雾")
        self.assertIsNotNone(persona.active_card("private_1"))
        self.assertIsNone(persona.active_card("group_2"))      # 别的群不受影响
        self.assertIsNone(persona.active_card("private_9"))

    def test_deleting_the_card_ends_the_roleplay(self):
        persona.save_cards([_card("纱雾")])
        persona.handle_command("private_1", "#纱雾")
        # 这里是**有意的清空**，要走 force——不带 force 时整表清空会被
        # 批量删除闸拦下（2026-11-04 深夜加的，见 test_builtin_cards
        # 的 GuardTest）。那条闸防的是「列表被截断」，不是防「真想删」。
        persona.save_cards([], force=True)
        # 卡没了就自动当没在演，不会卡在一个取不到的 id 上
        self.assertIsNone(persona.active_card("private_1"))


class TrimTest(unittest.TestCase):
    def test_keeps_only_the_last_window(self):
        """只留窗口内最近 N 条（2026-11-03 从 20 降到 12）。"""
        n = 30
        hist = [{"role": "user", "content": "m%d" % i} for i in range(n)]
        out = persona.trim(hist)
        self.assertEqual(len(out), persona.HISTORY_LIMIT)
        self.assertEqual(out[0]["content"], "m%d" % (n - persona.HISTORY_LIMIT))
        self.assertEqual(out[-1]["content"], "m%d" % (n - 1))

    def test_note_runs_more_often_than_the_window(self):
        """压缩周期必须小于窗口轮数，否则内容会「掉出窗口又没进小记」。"""
        rounds = persona.HISTORY_LIMIT // 2
        self.assertLess(persona.NOTE_EVERY_TURNS, rounds,
                        "NOTE_EVERY_TURNS 必须 < %d 轮" % rounds)

    def test_truncates_long_message(self):
        # 用比上限长一大截的内容，别贴着边界写——上限从 200 调到 900 时，
        # 「长」×900 正好不超，这条就假失败了。
        out = persona.trim([{"role": "user",
                             "content": "长" * (persona.MSG_MAX_CHARS + 500)}])
        self.assertEqual(len(out[0]["content"]), persona.MSG_MAX_CHARS + 1)

    def test_drops_non_dialogue_roles(self):
        out = persona.trim([{"role": "system", "content": "头"},
                            {"role": "tool_result", "content": "回执"},
                            {"role": "user", "content": "hi"}])
        self.assertEqual([m["role"] for m in out], ["user"])


class HeadTest(PersonaBase):
    def test_head_has_persona_contract_env_and_nothing_else(self):
        head = persona.build_head(_card("纱雾"), "group_1", "张三")
        self.assertIn("说话很轻", head)        # 角色人设
        self.assertIn("第三人称叙述", head)     # 输出契约（2026-11-02 改小说式）
        self.assertIn("画面：", head)           # 每轮一行画面标签（配图用）
        self.assertIn("QQ 群", head)           # 群聊环境
        self.assertIn("张三", head)
        self.assertNotIn("####", head)   # 只禁用四级以上的深嵌套
        # 刻意不带的东西：工具协议、Skill 目录、审核话术（用户点名不要）
        self.assertNotIn("[[TOOL:", head)
        self.assertNotIn("Skill", head)
        self.assertNotIn("审核", head)

    def test_private_env_differs(self):
        head = persona.build_head(_card("纱雾"), "private_9", "")
        self.assertIn("私聊", head)

    def test_note_is_attached(self):
        head = persona.build_head(_card("纱雾"), "private_1", "张三",
                                  "他昨天让我画了只兔子。")
        self.assertIn("【之前的事】", head)
        self.assertIn("画了只兔子", head)


class TurnTest(PersonaBase):
    # ⚠️ 2026-11-03：这一组全部改用 `private_1`。
    # 群聊已经不再实际扮演（`run_turn` 遇 group 直接返回空 Turn，见
    # `GroupNoRoleplayTest`），而这里测的是**引擎本身**——两段式、格式拆分、
    # 历史落盘、出图门槛。用群聊键会让它们全都拿到空 Turn。
    KEY = "private_1"

    def setUp(self):
        super().setUp()
        persona.save_cards([_card("纱雾")])
        self.card = persona.find_card("纱雾")
        # 2026-11-03 方案 B：QQ 私聊也按角色分线了。用 `switch_line` 走和
        # 线上完全一致的路径（线键 + base 指针）；读历史用 `resolve_line`
        # 换算——测试里出现的键从此是 `private_1__纱雾`。
        persona.switch_line(self.KEY, self.card)

    def _hist_key(self):
        return persona.resolve_line(self.KEY)

    def _run(self, reply_text, text="在吗", sender="张三"):
        with mock.patch.object(persona.llm, "call_llm",
                               return_value=reply_text) as m:
            out = persona.run_turn(self.KEY, text, "private", sender)
        return out, m

    def test_two_calls_intent_then_reply(self):
        """2026-11-02 起是两段式：先判意图、再写正文。

        原来只调用一次（人设、契约、格式全挤在一次），模型只能挑最要紧的
        执行——「做红烧兔头」被当成真要做菜就是这个原因。现在拆成
        「判断」和「表演」两次，各管一件事。
        """
        out, m = self._run("你好呀")
        self.assertEqual(m.call_count, 2)              # 意图 + 正文
        self.assertEqual(out.reply, "你好呀")           # 回话原样发回去
        self.assertEqual(out.scene, "")                # 没写画面行 = 这轮不出图

    def test_fifth_time_does_not_auto_advance(self):
        """第 5 次**不再**自动跨天（2026-11-05 改）。

        用户拍板：那一步改成弹窗驱动——第五次正常输出，AI 回完再问
        「结束 / 最后一次」。自动跳会让玩家没得选，而且第五次那一轮被塞了
        「过渡到第二天」的指令，写出来就不是那一场本身了。
        """
        score = {"finish": "inside", "act": "intense", "excite": 30,
                 "delta": 1}
        aff = {"active": True, "excite": 95, "orgasms": 5, "orgasmMax": 5,
               "just_orgasm": 5, "finish": "inside", "value": 50,
               "corrupt": 0, "submit": 0, "arousal": 50}
        raw = "她累得说不出话。\n画面：1girl\n心情：疲惫 强度1\n状态：床上"
        with mock.patch.object(persona, "_judge_intent_scored",
                               return_value=("intimate", "", score)), \
             mock.patch.object(persona.affection, "settle",
                               return_value=aff), \
             mock.patch.object(persona.llm, "call_llm", return_value=raw):
            out = persona.run_turn(self.KEY, "再要一次", "private", "张三")
        self.assertTrue(out.reply)
        # 还在同一天——跨天由 `/api/persona/finish_night` 负责。
        self.assertEqual(persona.game_day(self.KEY), 1)
        # 但要告诉前端「这是第 5 次，该弹第二个窗了」。
        self.assertIsInstance(out.aff, dict)
        self.assertTrue(out.aff["climax"]["last"])
        self.assertEqual(out.aff["climax"]["n"], 5)

    def test_intent_judgement_failure_still_replies(self):
        """判断段挂了也要能回话——退化成单次流程，不是整轮哑掉。

        2026-11-04 起默认攻略模式，第一段走的是「意图 + 打分」那个版本
        （`_judge_intent_scored`），两个都得 patch 掉才算「判断段整体不可用」。
        """
        with mock.patch.object(persona, "_judge_intent",
                               return_value=(None, "", {})), \
             mock.patch.object(persona, "_judge_intent_scored",
                               return_value=(None, "", None)), \
             mock.patch.object(persona.llm, "call_llm", return_value="你好呀") as m:
            persona.run_turn("private_1", "在吗", "private", "张三")
        self.assertEqual(m.call_count, 1)

    def test_segments_and_director_reach_the_actor(self):
        """第一段拆出的「说 / 想」要按块发给第二段，导演条也要进系统头。"""
        score = ('{"intent": "neutral", "delta": 2, "turn_type": "chat",'
                 ' "reason": "试探", "note": "试探",'
                 ' "segments": ['
                 '   {"who": "用户", "type": "说", "text": "我走了。"},'
                 '   {"who": "用户", "type": "想", "text": "其实想留下"}],'
                 ' "addressed": true, "subtext": "想被留住", "stance": "装傻"}')
        with mock.patch.object(persona.llm, "call_llm",
                               side_effect=[score, "她没松手。"]) as m:
            persona.run_turn(self.KEY, "我走了。", "private")
        last_user = m.call_args_list[1][0][0][-1]["content"]
        self.assertIn("用户 · 说（你听得见）：我走了。", last_user)
        self.assertIn("用户 · 想（你看不见", last_user)
        self.assertNotIn("其实想留下", last_user)      # 心里话的正文不给演员看
        head = m.call_args_list[1][0][0][0]["content"]
        self.assertIn("对方心里有话没说出口", head)
        self.assertNotIn("对方真正在：想被留住", head)

    def test_rag_remembers_only_visible_text(self):
        """长期记忆入库：只收「说/做」，心里话不进去；条目挂在当前线。"""
        score = ('{"intent": "neutral", "delta": 2, "turn_type": "chat",'
                 ' "segments": ['
                 '   {"who": "用户", "type": "说", "text": "我走了。"},'
                 '   {"who": "用户", "type": "想", "text": "其实想留下"}],'
                 ' "addressed": true}')
        with mock.patch.object(persona.llm, "call_llm",
                               side_effect=[score, "她没松手。"]), \
             mock.patch.object(persona_rag, "enabled_for", return_value=True), \
             mock.patch.object(persona_rag, "warm"), \
             mock.patch.object(persona_rag, "build_block", return_value=""), \
             mock.patch.object(persona_rag, "remember") as remember:
            persona.run_turn(self.KEY, "我走了。", "private")
        remember.assert_called_once()
        line, user_text, reply = remember.call_args[0]
        self.assertEqual(line, self._hist_key())
        self.assertIn("我走了", user_text)
        self.assertNotIn("其实想留下", user_text)
        self.assertEqual(reply, "她没松手。")
        self.assertIsNone(remember.call_args.kwargs.get("custom"))

    def test_rag_block_reaches_the_actor(self):
        """检索到的旧回忆要进这一轮输入，且贴在最后一条玩家消息里。

        2026-11-07：回忆块从 system 头挪到末尾——system 头是前缀，动态回忆
        插进去会把前缀缓存打散（实测命中 59~70%）。这里同时守住「不能被丢
        掉」这条底线，只换位置。
        """
        with mock.patch.object(persona.llm, "call_llm",
                               return_value="嗯。") as m, \
             mock.patch.object(persona_rag, "enabled_for", return_value=True), \
             mock.patch.object(persona_rag, "warm"), \
             mock.patch.object(persona_rag, "build_block",
                               return_value="【相关回忆】\n- 她怕打雷。") as bb:
            persona.run_turn(self.KEY, "今天打雷了", "private")
        msgs = m.call_args[0][0]
        head = msgs[0]["content"]
        last = msgs[-1]["content"]
        self.assertNotIn("她怕打雷", head)
        self.assertIn("她怕打雷", last)
        bb.assert_called_once()
        self.assertEqual(bb.call_args[0][0], self._hist_key())

    def test_state_and_lead_reach_the_actor(self):
        """编剧的「状态底色 + 主动判定」要进演员系统头，不是只落库。"""
        score = ('{"intent": "neutral", "delta": 2, "turn_type": "chat",'
                 ' "reason": "试探", "note": "试探",'
                 ' "persona_mode": "屈服", "tension": "照做了，但手指扣着衣角",'
                 ' "lead": "act", "lead_form": "情感", "lead_reason": "她憋了几天"}')
        with mock.patch.object(persona.llm, "call_llm",
                               side_effect=[score, "她没松手。"]) as m:
            persona.run_turn(self.KEY, "我走了。", "private")
        head = m.call_args_list[1][0][0][0]["content"]
        self.assertIn("她此刻的底色：屈服", head)
        self.assertIn("张力：照做了", head)
        self.assertIn("可以主动做一件事（情感）", head)

    def test_scene_line_is_split_out_of_the_reply(self):
        """模型顺手写的那行「画面：」要剥出来：正文发回去，标签拿去出图。

        2026-10-16 用户拍板「角色回话时顺手写」——不额外调一次 LLM 出画面描述。
        """
        out, _ = self._run("你好呀\n画面：1girl, sitting, smile")
        self.assertEqual(out.reply, "你好呀")
        self.assertEqual(out.scene, "1girl, sitting, smile")

    def test_meta_lines_stay_in_history_as_few_shot(self):
        """历史里**保留**画面/心情/状态三行（2026-11-02 反转了旧结论）。

        旧行为是剥掉再落盘，理由是「元数据不是台词」。实测证明那样做会让
        模型第 2 轮起跟着历史把格式丢掉（第 1 轮完美、第 2 轮全空），所以
        改成保留完整原文当 few-shot，展示时再由 `split_turn` 剥。
        """
        self._run("你好呀\n画面：1girl, sitting\n心情：害羞 强度6\n状态：坐在桌前")
        ent = persona._load_hist(self._hist_key())[1]["content"]
        self.assertIn("你好呀", ent)
        self.assertIn("画面：1girl, sitting", ent)
        self.assertIn("心情：害羞 强度6", ent)
        self.assertIn("状态：坐在桌前", ent)

    def test_split_turn_strips_meta_from_display(self):
        """存进历史的是全的，但**用户看到的正文**里不许有这三行。"""
        self._run("你好呀\n画面：1girl, sitting\n心情：害羞 强度6\n状态：坐在桌前")
        turn = persona.split_turn(
            persona._load_hist(self._hist_key())[1]["content"])
        self.assertEqual(turn[0], "你好呀")
        self.assertEqual(turn[1], "1girl, sitting")
        self.assertEqual(turn[2], "害羞")
        self.assertEqual(turn[3], 6)
        self.assertEqual(turn[4], "坐在桌前")

    def test_history_keeps_dialogue_only(self):
        """历史里每条 assistant 都必须带三行（2026-11-02 起会补齐）。

        模型漏写时 `_ensure_meta` 补占位——不给下一轮留「可以不写」的示范。
        """
        self._run("你好呀")
        hist = persona._load_hist(self._hist_key())
        self.assertEqual([m["role"] for m in hist], ["user", "assistant"])
        # 私聊不加「张三：」前缀（群聊那条已经关掉了，见 GroupNoRoleplayTest）
        self.assertEqual(hist[0]["content"], "在吗")
        self.assertTrue(hist[1]["content"].startswith("你好呀"))
        # 三行被补齐了（模型没写画面，补成空值也算形状完整）
        self.assertIn("心情：", hist[1]["content"])
        self.assertIn("状态：", hist[1]["content"])

    def test_group_turn_is_disabled(self):
        """群聊不再实际扮演（2026-11-03）——回空 Turn，也不写历史。

        原来的 `test_group_prefixes_sender_private_does_not` 测「群里给正文加
        『张三：』前缀」。群聊关掉之后那段逻辑走不到，测试前提消失，改验新行为。
        """
        persona.switch("group_1", self.card)
        with mock.patch.object(persona.llm, "call_llm", return_value="嗯"):
            turn = persona.run_turn("group_1", "在吗", "group", "张三")
        self.assertEqual(turn.reply, "")
        self.assertEqual(persona._load_hist("group_1"), [])

    def test_empty_reply_says_so(self):
        """模型整轮没交作业：说一句「卡了一下」。

        不重跑——重跑一次未必更好，还白烧一轮。
        """
        out, m = self._run("")
        self.assertIn("卡", out.reply)

    def test_not_in_roleplay_is_a_noop(self):
        persona.leave("private_1")
        with mock.patch.object(persona.llm, "call_llm") as m:
            self.assertEqual(persona.run_turn("private_1", "在吗", "private").reply, "")
        m.assert_not_called()

    def test_history_is_separate_from_the_image_bot(self):
        self._run("你好呀")
        # 生图机器人的历史落在同一目录的另一份文件里，这边读到的只有对话
        self.assertFalse(os.path.exists(
            os.path.join(agents.AGENTS_DIR, "qq", "sessions", "group_1.jsonl")))
        self.assertTrue(os.path.exists(persona._hist_path(self._hist_key())))


class SceneSplitTest(unittest.TestCase):
    """契约行「画面：<英文标签>」的解析。"""

    def test_splits_reply_and_scene(self):
        reply, scene = persona.split_scene("你好呀\n画面：1girl, sitting, smile")
        self.assertEqual(reply, "你好呀")
        self.assertEqual(scene, "1girl, sitting, smile")

    def test_full_width_colon_is_accepted(self):
        _, scene = persona.split_scene("嗯。\n画面：night, window")
        self.assertEqual(scene, "night, window")

    def test_no_scene_line_is_empty(self):
        reply, scene = persona.split_scene("你好呀")
        self.assertEqual((reply, scene), ("你好呀", ""))

    def test_takes_the_last_and_drops_every_one(self):
        """模型偶尔会写两行「画面：」——两行都从正文里去掉，取最后一行当画面。"""
        reply, scene = persona.split_scene("画面：first\n你好\n画面：second")
        self.assertEqual(scene, "second")
        self.assertEqual(reply, "你好")

    def test_strips_markdown_noise(self):
        _, scene = persona.split_scene("好\n画面：**`1girl, smile`**")
        self.assertEqual(scene, "1girl, smile")

    def test_scene_inside_the_text_is_not_taken(self):
        """正文里出现「画面」两个字不算契约行——行首才是。"""
        reply, scene = persona.split_scene("这个画面我记着呢。")
        self.assertEqual(scene, "")
        self.assertEqual(reply, "这个画面我记着呢。")


class PromptTest(unittest.TestCase):
    """出图提示词 = 角色卡固定块 + 这一轮的画面。"""

    def test_fixed_block_prepended(self):
        card = {"appearancePrompt": "1girl, white hair",
                "outfitPrompt": "black dress"}
        self.assertEqual(persona.build_prompt(card, "sitting, smile"),
                         "1girl, white hair, black dress, sitting, smile")

    def test_no_appearance_still_draws_the_scene(self):
        self.assertEqual(persona.build_prompt({}, "sitting"), "sitting")

    def test_empty_scene_leaves_the_fixed_block(self):
        self.assertEqual(persona.build_prompt({"appearancePrompt": "1girl"}, ""),
                         "1girl")


class SessionTargetTest(unittest.TestCase):
    def test_group_and_private(self):
        self.assertEqual(persona._session_target("group_123"), ("group", "123"))
        self.assertEqual(persona._session_target("private_9"), ("private", "9"))

    def test_unknown_key_is_dropped(self):
        self.assertEqual(persona._session_target("weird"), ("", ""))
        self.assertEqual(persona._session_target(""), ("", ""))

    def test_web_line_uses_the_qq_queue_without_qq_delivery(self):
        """网页线（2026-11-04 修）：`web_<qq>` 必须映射成「网页侧」队列身份。

        不映射的话这里返回 `("", "")`，`draw()` 直接返回空——网页「出图」
        永远失败，日志里只有「扮演出图」、没有「扮演配图完成」。
        target 用 None 是 image_jobs 既有的网页侧约定：照常排队 + 落
        `output/web/生图页/`，但 worker 不往 QQ 发东西。
        """
        self.assertEqual(persona._session_target("web_2509355624"),
                         (None, "2509355624"))
        self.assertEqual(persona._session_target("web_2509355624__izumi_sagiri"),
                         (None, "2509355624"))
        # 游客线没有 QQ 号，没有队列身份
        self.assertEqual(persona._session_target("web_local"), ("", ""))


class StatePersistenceTest(PersonaBase):
    """整份状态落盘的合并与拒写（2026-11-05 存档成片消失的根因）。"""

    def test_save_keeps_lines_written_by_another_process(self):
        """两个进程「读整份→改一条→写整份」时，后写的一方不能盖掉新线。

        今天 546587874 的学妹线和其他二十多条线就是这么没的：A 拿着旧快照
        写回，B 在中间新建的线全被覆盖。合并后两边都在。
        """
        persona._save_state({"private_old": {"turns": 3}})
        persona._save_state({"private_new": {"turns": 1}})
        st = persona._load_state()
        self.assertIn("private_old", st)
        self.assertIn("private_new", st)

    def test_explicit_delete_survives_the_merge(self):
        """真要删线（出戏 / 管理页删除）必须显式声明，不然会被合并复活。"""
        persona._save_state({"private_1": {"turns": 2}})
        persona._save_state({"private_2": {"turns": 1}}, deleted={"private_1"})
        st = persona._load_state()
        self.assertNotIn("private_1", st)
        self.assertIn("private_2", st)

    def test_transient_read_error_does_not_return_an_empty_library(self):
        """读盘失败以前会假装「没有存档」，反手一写就全清空。现在直接报错。"""
        persona._save_state({"private_1": {"turns": 2}})
        with mock.patch("builtins.open", side_effect=PermissionError("locked")):
            with self.assertRaises(OSError):
                persona._load_state()

    def test_unreadable_file_refuses_to_be_overwritten(self):
        """盘上有文件却读不出来时，写盘必须拒绝，不能拿空字典盖上去。"""
        persona._save_state({"private_1": {"turns": 2}})
        with mock.patch.object(persona, "_read_state_raw", return_value=None):
            with self.assertRaises(RuntimeError):
                persona._save_state({"private_2": {"turns": 1}})


class DrawTest(unittest.TestCase):
    """扮演态配图：走 `_persona_fast`，入队带 quiet，失败一律静默。"""

    def _patch(self, **kw):
        job = mock.Mock()
        job.wait.return_value = {"outputs": {}}
        patches = [
            mock.patch("app.image_jobs.comfy_alive",
                       return_value=kw.get("alive", True)),
            mock.patch("app.image_jobs.output_images",
                       return_value=kw.get("names", ["a.png"])),
            mock.patch("app.image_jobs.enqueue",
                       return_value=(job, kw.get("reason"))),
            mock.patch("app.tools.normal.generate_image.build_t2i_workflow",
                       return_value=kw.get("wf", {"x": 1})),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return job

    def test_enqueues_quiet_on_the_fast_skill(self):
        job = self._patch()
        name = persona.draw("sitting, smile", {"appearancePrompt": "1girl"},
                            "group_1")
        self.assertEqual(name, "a.png")
        import app.image_jobs as ij
        args, kw = ij.enqueue.call_args
        self.assertEqual(args[:2], ("group", "1"))       # 会话线拆出来的投递目标
        self.assertEqual(kw["skill"], persona.FAST_SKILL)
        self.assertTrue(kw["quiet"])                     # 不带编号 caption
        self.assertFalse(kw["landscape"])                # 后台线程判不了，写死竖版

    def test_web_line_enqueues_into_the_same_queue(self):
        """网页「出图」必须真的入队（2026-11-04 用户报「走不了 QQ 队列」）。

        之前 `_session_target("web_…")` 返回 `("", "")`，`draw()` 在入队前
        就 return ""，网页永远拿不到图，前端只能看到「没画出来」。
        """
        self._patch()
        name = persona.draw("sitting, smile", {"appearancePrompt": "1girl"},
                            "web_2509355624__izumi_sagiri",
                            skill="anima-line", landscape=True)
        self.assertEqual(name, "a.png")
        import app.image_jobs as ij
        from app import comfy_out
        args, kw = ij.enqueue.call_args
        self.assertEqual(args[:2], (None, "2509355624"))   # 网页侧 + 本人在途额度
        self.assertTrue(kw["quiet"])                       # 不往 QQ 发
        self.assertEqual(kw["skill"], "anima-line")
        # 图落网页侧目录，不混进 QQ 私聊
        self.assertEqual(comfy_out.session_base(None, "2509355624"),
                         "web/生图页")

    def test_prompt_goes_into_the_workflow(self):
        self._patch()
        import app.tools.normal.generate_image as gi
        persona.draw("sitting, smile", {"appearancePrompt": "1girl"}, "private_1")
        self.assertEqual(gi.build_t2i_workflow.call_args[0][0],
                         persona.FAST_SKILL)
        self.assertEqual(gi.build_t2i_workflow.call_args[0][1],
                         "1girl, sitting, smile")

    def test_comfy_down_is_silent(self):
        self._patch(alive=False)
        import app.image_jobs as ij
        self.assertEqual(persona.draw("x", {}, "private_1"), "")
        ij.enqueue.assert_not_called()

    def test_empty_scene_does_not_enqueue(self):
        self._patch()
        import app.image_jobs as ij
        self.assertEqual(persona.draw("   ", {"appearancePrompt": "1girl"},
                                      "private_1"), "")
        ij.enqueue.assert_not_called()

    def test_queue_rejection_is_silent(self):
        self._patch(reason="现在排队的人太多了（前面还有 9 张），这一张先不画。")
        self.assertEqual(persona.draw("x", {"appearancePrompt": "1girl"},
                                      "private_1"), "")

    def test_draw_failure_is_silent(self):
        job = self._patch()
        job.wait.side_effect = RuntimeError("ComfyUI 掉线")
        self.assertEqual(persona.draw("x", {"appearancePrompt": "1girl"},
                                      "private_1"), "")

    def test_on_submit_fires_after_a_real_enqueue(self):
        """提交成功才回调（2026-11-05）：回执是「排上了」，没排上不能报喜。"""
        job = self._patch()
        seen = []
        persona.draw("x", {"appearancePrompt": "1girl"}, "private_1",
                     on_submit=seen.append)
        self.assertEqual(seen, [job])

    def test_on_submit_skipped_when_the_queue_rejects(self):
        self._patch(reason="现在排队的人太多了")
        seen = []
        persona.draw("x", {"appearancePrompt": "1girl"}, "private_1",
                     on_submit=seen.append)
        self.assertEqual(seen, [])
        # 没画面 / ComfyUI 没在线也算没提交
        self._patch(alive=False)
        seen2 = []
        persona.draw("x", {"appearancePrompt": "1girl"}, "private_1",
                     on_submit=seen2.append)
        self.assertEqual(seen2, [])

    def test_receipt_failure_does_not_kill_the_draw(self):
        """回执发不出去只记日志——图还是要照常画、照常交回。"""
        self._patch()

        def boom(job):
            raise RuntimeError("回执发送失败")

        with mock.patch.object(persona.log, "exception"):
            name = persona.draw("x", {"appearancePrompt": "1girl"}, "private_1",
                                on_submit=boom)
        self.assertEqual(name, "a.png")

    def test_no_workflow_is_silent(self):
        self._patch(wf=None)
        import app.image_jobs as ij
        self.assertEqual(persona.draw("x", {"appearancePrompt": "1girl"},
                                      "private_1"), "")
        ij.enqueue.assert_not_called()

    def test_skill_and_landscape_passthrough(self):
        """draw 的渠道/横屏参数必须原样传到 enqueue 和工作流构造。"""
        self._patch()
        name = persona.draw("sitting, smile", {"appearancePrompt": "1girl"},
                            "group_1", skill="anima-line", landscape=True)
        self.assertEqual(name, "a.png")
        import app.image_jobs as ij
        import app.tools.normal.generate_image as gi
        _, kw = ij.enqueue.call_args
        self.assertEqual(kw["skill"], "anima-line")
        self.assertTrue(kw["landscape"])
        self.assertEqual(gi.build_t2i_workflow.call_args[0][0], "anima-line")

    def test_draw_shot_uses_anima_line_landscape(self):
        """draw_shot：画面行 → draw(SHOT_SKILL, 横屏)；没画面行 → 拒绝。"""
        from unittest import mock
        self._patch()
        with mock.patch.object(persona, "shot_scene",
                               return_value="sitting, smile"):
            name, note = persona.draw_shot({"appearancePrompt": "1girl"},
                                           "group_1")
        self.assertEqual(name, "a.png")
        self.assertEqual(note, "")
        import app.image_jobs as ij
        _, kw = ij.enqueue.call_args
        self.assertEqual(kw["skill"], persona.SHOT_SKILL)
        self.assertTrue(kw["landscape"])
        # 没画面行：直接拒绝，note 带原因
        with mock.patch.object(persona, "shot_scene", return_value=""):
            self.assertEqual(persona.draw_shot({}, "group_1"),
                             ("", "这一轮没有可画的画面"))


class IntentTest(unittest.TestCase):
    """两段式第一段：意图判断（2026-11-02）。"""

    CARD = {"name": "白色小兔"}

    def _judge(self, raw, exc=None):
        from unittest import mock
        if exc:
            side = exc
        else:
            side = raw
        with mock.patch.object(persona.llm, "call_llm",
                               side_effect=exc, return_value=raw):
            return persona._judge_intent(self.CARD, "做红烧兔头")

    def test_reads_joke(self):
        got = self._judge('{"intent": "joke", "note": "拿兔子开玩笑"}')
        self.assertEqual(got[:2], ("joke", "拿兔子开玩笑"))
        self.assertEqual(got[2]["segments"], [])

    def test_reads_intimate(self):
        got = self._judge('{"intent": "intimate", "note": "求抱抱"}')
        self.assertEqual(got[0], "intimate")

    def test_unknown_intent_falls_back(self):
        """模型自创类别 → 当作没判断，走原流程。"""
        self.assertEqual(self._judge('{"intent": "tease", "note": "x"}'),
                         (None, "", {}))

    def test_non_json_falls_back(self):
        self.assertEqual(self._judge("我觉得他在开玩笑"), (None, "", {}))

    def test_exception_falls_back(self):
        from unittest import mock
        with mock.patch.object(persona.llm, "call_llm",
                               side_effect=RuntimeError("超时")):
            self.assertEqual(persona._judge_intent(self.CARD, "x"),
                             (None, "", {}))

    def test_parses_segments_and_director_fields(self):
        got = self._judge(
            '{"intent": "neutral", "note": "试探",'
            ' "segments": ['
            '   {"who": "用户", "type": "说", "text": "我走了。"},'
            '   {"who": "助手", "type": "做", "text": "她把门拦住。"},'
            '   {"who": "用户", "type": "想", "text": "其实想留下"}],'
            ' "addressed": true, "literal": "他要走",'
            ' "subtext": "想被留住", "noticed": "他站着没动",'
            ' "stance": "装傻"}')
        extra = got[2]
        self.assertEqual(len(extra["segments"]), 3)
        self.assertEqual(extra["segments"][1]["who"], "助手")
        self.assertEqual(extra["segments"][2]["type"], "想")
        self.assertEqual(extra["subtext"], "想被留住")
        self.assertEqual(extra["stance"], "装傻")

    def test_intent_block_tells_it_how_to_react(self):
        block = persona._intent_block("joke", "拿兔子开玩笑")
        self.assertIn("玩笑", block)
        self.assertIn("不要当真", block)
        self.assertIn("拿兔子开玩笑", block)

    def test_neutral_block_is_empty(self):
        self.assertEqual(persona._intent_block("neutral", ""), "")

    def test_unknown_intent_block_is_empty(self):
        self.assertEqual(persona._intent_block("", ""), "")


class SegmentTest(unittest.TestCase):
    """第一段的分段与演员层渲染（2026-11-05）。"""

    def test_aliases_are_normalized(self):
        segs = persona._parse_segments([
            {"who": "user", "type": "say", "text": "a"},
            {"who": "assistant", "type": "act", "text": "b"},
            {"who": "她", "type": "thought", "text": "c"},
        ])
        self.assertEqual([(s["who"], s["type"]) for s in segs],
                         [("用户", "说"), ("助手", "做"), ("助手", "想")])

    def test_unknown_labels_fall_back_to_user_say(self):
        segs = persona._parse_segments([{"who": "?", "type": "?", "text": "x"}])
        self.assertEqual((segs[0]["who"], segs[0]["type"]), ("用户", "说"))

    def test_empty_segments_are_dropped(self):
        self.assertEqual(persona._parse_segments(
            [{"who": "用户", "type": "说", "text": "  "}]), [])

    def test_thought_is_explicitly_invisible(self):
        block = persona.render_user_block([
            {"who": "用户", "type": "说", "text": "我走了。"},
            {"who": "用户", "type": "想", "text": "其实想留下"},
        ])
        self.assertIn("用户 · 说（你听得见）：我走了。", block)
        self.assertIn("你看不见，也不知道内容", block)
        self.assertNotIn("其实想留下", block)

    def test_no_segments_falls_back_to_raw(self):
        self.assertEqual(persona.render_user_block([], True, "原文"), "原文")

    def test_current_turn_has_pronoun_reminder(self):
        # 2026-11-07：人称提醒改成正向锚点，不再写「不要用『他』」那种禁令。
        block = persona.render_user_block(
            [{"who": "用户", "type": "说", "text": "我走了。"}],
            True, "我走了。", remind=True)
        self.assertIn("她是「她」", block)
        self.assertIn("镜头可以跟着她", block)
        self.assertNotIn("不要用「他」指玩家", block)
        # 历史消息不带提醒
        old = persona.render_user_block(
            [{"who": "用户", "type": "说", "text": "我走了。"}],
            True, "我走了。")
        self.assertNotIn("镜头可以跟着她", old)

    def test_no_segments_with_remind_still_has_hint(self):
        block = persona.render_user_block([], True, "原文", remind=True)
        self.assertIn("原文", block)
        self.assertIn("她是「她」", block)

    def test_history_sanitizes_player_he(self):
        """历史里的「他」会当示范被模型学走；送模型前纠成「你」。"""
        out = persona._render_hist([
            {"role": "assistant", "content": "他走过来，其他人没动。"},
        ])
        self.assertEqual(out[0]["content"], "你走过来，其他人没动。")

    def test_build_head_sanitizes_legacy_state(self):
        card = {"name": "x", "systemPrompt": "y"}
        head = persona.build_head(card, "private_1", "",
                                  "", "他的手和她的手臂隔得很近")
        self.assertNotIn("他的手", head)
        self.assertIn("你的手", head)

    def test_not_addressed_is_marked(self):
        block = persona.render_user_block(
            [{"who": "用户", "type": "做", "text": "他走到窗边"}],
            addressed=False)
        self.assertIn("不是在对你说", block)

    def test_thought_segment_survives_empty_text(self):
        """「想」故意不带内容——不能因为 text 空就被丢掉。

        2026-11-05：提示词要求「想」的 text 填空串（让模型复述心里话会被
        内容审核掐断整份输出）。它是个标记，不是内容。
        """
        segs = persona._parse_segments([
            {"who": "用户", "type": "说", "text": "我走了。"},
            {"who": "用户", "type": "想", "text": ""},
        ])
        self.assertEqual([s["type"] for s in segs], ["说", "想"])

    def test_original_is_appended_when_no_thought(self):
        """没有心里话时，原文要完整补上——分段里「做」只是概括。"""
        block = persona.render_user_block(
            [{"who": "用户", "type": "做", "text": "按到墙上从后面进入"}],
            True, "（把伊蕾娜按到墙上，从后面顶进去）")
        self.assertIn("按到墙上从后面进入", block)
        self.assertIn("【他这一轮的原话", block)
        self.assertIn("从后面顶进去", block)
        # 原文里的「我」必须当场说明是玩家本人（2026-11-06）
        self.assertIn("「我」= 玩家本人", block)

    def test_original_is_hidden_when_a_thought_exists(self):
        """有心里话时不能补原文——那会把没说出口的内容带出去。"""
        block = persona.render_user_block(
            [{"who": "用户", "type": "说", "text": "我走了。"},
             {"who": "用户", "type": "想", "text": ""}],
            True, "我走了。（心里想：其实想留下）")
        self.assertNotIn("其实想留下", block)
        self.assertNotIn("【他这一轮的原话", block)

    def test_render_hist_uses_seg_only_for_new_messages(self):
        hist = [
            {"role": "user", "content": "旧原话"},
            {"role": "user", "content": "原文",
             "seg": [{"who": "用户", "type": "想", "text": "心里话"}],
             "addressed": True},
        ]
        out = persona._render_hist(hist)
        self.assertEqual(out[0]["content"], "旧原话")
        self.assertIn("看不见，也不知道内容", out[1]["content"])

    def test_history_keeps_seg_through_trim(self):
        hist = [{"role": "user", "content": "原文",
                 "seg": [{"who": "用户", "type": "想", "text": "心里话"}]}]
        out = persona.trim(hist)
        self.assertEqual(out[0]["content"], "原文")
        self.assertEqual(out[0]["seg"][0]["type"], "想")


class DirectorBlockTest(unittest.TestCase):
    """导演条只给「怎么反应」，不给台词（2026-11-05）。"""

    def test_block_is_short_and_explicit(self):
        block = persona._director_block({
            "literal": "他要走", "subtext": "想被留住",
            "noticed": "他站着没动", "stance": "装傻"})
        self.assertIn("对方字面上在说：他要走", block)
        self.assertIn("对方真正在：想被留住", block)
        self.assertIn("装作没听懂", block)
        self.assertIn("不是台词", block)

    def test_empty_info_adds_nothing(self):
        self.assertEqual(persona._director_block({}), "")

    def test_secret_thought_hides_subtext(self):
        """用户写了「心里想」时，潜台词不能出现在演员的系统头里。"""
        block = persona._director_block({
            "subtext": "想被留住", "secret": True, "stance": "装傻"})
        self.assertNotIn("想被留住", block)
        self.assertIn("内容你不知道", block)


class LedgerTest(unittest.TestCase):
    """B：Markdown 六节账本（2026-11-05）。"""

    LEDGER = (
        "## 关系\n- 你叫她学妹\n\n"
        "## 你的事（你做过的）\n- 你做了早餐\n\n"
        "## 她的事（她做过的）\n- 她答应帮你复习\n\n"
        "## 约定与未完成\n- 周末去图书馆\n\n"
        "## 忌讳与偏好\n- 你不喜欢被翻手机\n\n"
        "## 日常与场景\n- 住在学校附近"
    )

    def test_prompt_names_all_six_sections(self):
        for key in persona._LEDGER_SECTIONS:
            self.assertIn(key, persona._NOTE_PROMPT, key)

    def test_valid_ledger_passes(self):
        self.assertTrue(persona._ledger_ok(self.LEDGER))

    def test_freeform_note_is_rejected(self):
        self.assertFalse(persona._ledger_ok("我们聊了很久，她今天很开心。"))

    def test_third_person_pronoun_is_rejected(self):
        """账本里出现指代玩家的「他」→ 整份退回去（2026-11-06）。"""
        bad = self.LEDGER + "\n- 房钱他出，账记他头上"
        self.assertFalse(persona._ledger_ok(bad))
        # 「其他 / 他们」不是指代对方，不能误伤
        ok = self.LEDGER + "\n- 其他人都走了，他们没再来"
        self.assertTrue(persona._ledger_ok(ok))

    def test_fix_pronouns_only_touches_the_player(self):
        got = persona._ledger_fix_pronouns("他出钱，其他不变，他们走了，他人没来")
        self.assertEqual(got, "你出钱，其他不变，他们走了，他人没来")

    def test_fix_pronouns_makes_third_person_and_keeps_quotes(self):
        """账本统一第三人称：她→她、玩家→你；引号里的原话保留「我」。"""
        got = persona._ledger_fix_pronouns(
            "我答应帮他，他说「我等你」，我们没走。")
        self.assertEqual(got, "她答应帮你，你说「我等你」，我们没走。")

    def test_contract_defines_every_pronoun(self):
        """契约开头必须写清 你/她/我 各指代谁（2026-11-06 用户要求）。"""
        for text in ("人称对照", "女主角一律「她」", "玩家一律「你」",
                     "指她自己", "指代玩家一律写「你」"):
            self.assertIn(text, persona._CONTRACT)

    def test_build_head_sanitizes_legacy_note(self):
        """存量脏账本不改数据，注入前也要把「他」纠成「你」。"""
        card = {"name": "x", "systemPrompt": "y"}
        head = persona.build_head(card, "private_1", "",
                                  "- 房钱他出，账记他头上", "")
        self.assertNotIn("他出", head)
        self.assertIn("你出", head)

    def test_clip_keeps_every_heading(self):
        long = "\n".join(
            "## " + key + "\n" + ("- 这一节写了很多细节。" * 20)
            for key in persona._LEDGER_SECTIONS)
        out = persona._clip_ledger(long, 300)
        self.assertLessEqual(len(out), 300)
        for key in persona._LEDGER_SECTIONS:
            self.assertIn("## " + key, out)


class ScoreJsonRepairTest(unittest.TestCase):
    """打分 JSON 被截断时的救援（2026-11-05 线上事故）。

    事故现场：加了 `spot` / `dev` / `hook` 之后 JSON 变长，flash 档模型
    在 `literal` 那一行被硬截断，末尾还粘一句拒答。原来的
    `rfind("}")` 会命中 `segments` 里那个内层右括号，`json.loads` 必然
    失败 → **整轮按 0 分走** → 玩家看到「高潮值只掉不涨、开发度不动」。
    """

    def test_plain_json_still_parses(self):
        d = persona._parse_json_loose('{"intent": "intimate", "delta": 2}')
        self.assertEqual(d["delta"], 2)

    def test_truncated_object_is_repaired(self):
        raw = ('{"intent": "intimate", "delta": 2, "act": "thrust",'
               ' "excite": 22, "segments": [{"who": "用户", "text": "x"}],'
               ' "literal": "从后面插小穴')
        d = persona._parse_json_loose(raw)
        self.assertIsInstance(d, dict)
        self.assertEqual(d["act"], "thrust")
        self.assertEqual(d["excite"], 22)
        self.assertTrue(d["literal"].startswith("从后面"))

    def test_refusal_glued_to_the_tail_does_not_kill_the_numbers(self):
        raw = ('{"intent": "intimate", "delta": 2, "act": "intense",'
               ' "excite": 32, "literal": "abc\n\nSorry, I can\'t respond.')
        d = persona._parse_json_loose(raw)
        self.assertEqual(d["act"], "intense")
        self.assertEqual(d["excite"], 32)

    def test_truncated_inside_nested_dev(self):
        raw = '{"intent": "intimate", "dev": {"pussy": 12, "anus": 8, "mouth":'
        d = persona._parse_json_loose(raw)
        self.assertEqual(d["dev"]["pussy"], 12)
        self.assertEqual(d["dev"]["anus"], 8)

    def test_trailing_comma_is_tolerated(self):
        d = persona._parse_json_loose('{"delta": 3, "act": "hand",}')
        self.assertEqual(d["act"], "hand")

    def test_garbage_still_returns_none(self):
        """补括号不能把「根本没交 JSON」糊成一份假的。"""
        self.assertIsNone(persona._parse_json_loose("我不知道你在说什么。"))
        self.assertIsNone(persona._parse_json_loose(""))

    def test_fenced_block_is_read(self):
        d = persona._parse_json_loose(
            '```json\n{"intent": "neutral", "delta": 1}\n```')
        self.assertEqual(d["delta"], 1)


class StateBehaviorTest(unittest.TestCase):
    """卡上的四段「状态下的她」+ 编剧的状态覆盖/主动判定（2026-11-05）。"""

    def test_norm_state_behavior_accepts_chinese_keys(self):
        sb = persona._norm_state_behavior({
            "屈服": "她不再顶嘴", "corrupt": "她开始找理由",
            "好感": "她别别扭扭地关心", "suspicion": "她笑着套话",
        })
        self.assertEqual(sb["submit"], "她不再顶嘴")
        self.assertEqual(sb["corrupt"], "她开始找理由")
        self.assertEqual(sb["affection"], "她别别扭扭地关心")
        self.assertEqual(sb["suspicion"], "她笑着套话")

    def test_state_behavior_text_is_not_thresholds(self):
        text = persona._state_behavior_text({
            "stateBehavior": {"submit": "嘴上顺从，眼神不服"}})
        self.assertIn("屈服高时：嘴上顺从，眼神不服", text)

    def test_score_profile_carries_state_behavior(self):
        p = persona._score_profile({
            "name": "测试卡", "stateBehavior": {"affection": "主动但不承认"}})
        self.assertIn("好感高时：主动但不承认", p)

    def test_director_reads_persona_mode_and_lead(self):
        info = persona._director_fields({
            "persona_mode": "屈服", "tension": "照做了，但手指扣着衣角",
            "lead": "act", "lead_form": "情感", "lead_reason": "她憋了几天",
        })
        self.assertEqual(info["persona_mode"], "屈服")
        self.assertEqual(info["lead"], "act")
        self.assertEqual(info["lead_form"], "情感")

    def test_director_block_renders_state_and_lead(self):
        block = persona._director_block({
            "persona_mode": "屈服", "tension": "照做了，但手指扣着衣角",
            "lead": "act", "lead_form": "情感", "lead_reason": "她憋了几天",
        })
        self.assertIn("她此刻的底色：屈服", block)
        self.assertIn("张力：照做了", block)
        self.assertIn("可以主动做一件事（情感）", block)
        self.assertIn("不要替玩家做决定", block)

    def test_secret_thought_hides_lead_reason(self):
        block = persona._director_block({
            "secret": True, "lead": "act", "lead_reason": "他其实想留下"})
        self.assertNotIn("他其实想留下", block)


class IntimateGuideTest(unittest.TestCase):
    """亲密指导按需注入（2026-11-03）：省 1,284 字，只在用得上时发。"""

    CARD = {"name": "纱雾", "systemPrompt": "你是纱雾。"}

    def test_intimate_head_has_the_guide(self):
        head = persona.build_head(self.CARD, "private_1", "", intimate=True)
        self.assertIn("亲密场景", head)
        self.assertIn("小穴", head)

    def test_plain_head_drops_the_guide(self):
        head = persona.build_head(self.CARD, "private_1", "", intimate=False)
        self.assertNotIn("亲密场景", head)
        self.assertNotIn("小穴", head)
        # 但格式要求、画面、心情、状态这些必须还在
        self.assertIn("## 画面", head)
        self.assertIn("## 心情", head)
        self.assertIn("## 状态", head)

    def test_plain_head_is_much_shorter(self):
        a = persona.build_head(self.CARD, "private_1", "", intimate=True)
        b = persona.build_head(self.CARD, "private_1", "", intimate=False)
        self.assertGreater(len(a) - len(b), 1000)

    def test_default_is_intimate_for_old_callers(self):
        """老调用方不传 intimate 时行为不变。"""
        head = persona.build_head(self.CARD, "private_1", "")
        self.assertIn("亲密场景", head)


class AddressedCommandTest(PersonaBase):
    """群聊命令的前缀剥离与组合写法（2026-11-03）。

    实测事故：群友在群里发「@大大怪 #青梅竹马」，因为不以 `#` 开头，
    `handle_command` 认不出，整条落到 `direct_gen` 被当成画面需求——
    机器人回了一句「青梅竹马这个主题前面已经画过一张了」。
    """

    def setUp(self):
        super().setUp()
        persona.save_cards([_card("纱雾"), _card("青梅竹马")])
        self.card = persona.find_card("青梅竹马")

    def test_at_prefix_is_stripped_when_addressed(self):
        got = persona.handle_command("private_1", "@大大怪 #青梅竹马",
                                     addressed=True)
        self.assertIsNotNone(got)
        self.assertIn("青梅竹马", got)

    def test_at_prefix_is_not_stripped_when_not_addressed(self):
        """没在叫它的时候不许剥——私聊里「我@某人 #tag」不该被当成命令。"""
        self.assertIsNone(
            persona.handle_command("private_1", "@大大怪 #青梅竹马",
                                   addressed=False))

    def test_name_prefix_is_stripped(self):
        got = persona.handle_command("private_1", "大大怪 #角色扮演",
                                     addressed=True)
        self.assertIsNotNone(got)
        self.assertIn("角色名单", got)

    def test_repeated_prefixes(self):
        got = persona.handle_command("private_1", "@大大怪 大大怪 #青梅竹马",
                                     addressed=True)
        self.assertIsNotNone(got)
        self.assertIn("青梅竹马", got)

    def test_word_plus_card_name(self):
        """`#角色扮演 青梅竹马` 这种自然写法也要认。"""
        got = persona.handle_command("private_1", "#角色扮演 青梅竹马",
                                     addressed=True)
        self.assertIsNotNone(got)
        self.assertIn("青梅竹马", got)

    def test_plain_chat_is_still_ignored(self):
        self.assertIsNone(
            persona.handle_command("private_1", "今天天气不错", addressed=True))


class ShotComicTest(unittest.TestCase):
    """「出图」→ moss 漫画（2026-11-03）。"""

    def test_shot_words(self):
        for t in ("出图", "#出图", "出图吧", "画一张", "来张图", "画一下"):
            self.assertTrue(persona.is_shot_request(t), t)

    def test_normal_chat_is_not_a_shot(self):
        for t in ("出图了", "你刚才出图了？", "我想出图", "出图失败", ""):
            self.assertFalse(persona.is_shot_request(t), t)

    def test_male_protagonist_is_fixed(self):
        """男主必须有统一外形，否则六格里他每格长得都不一样。"""
        self.assertTrue(persona.MALE_PROTAGONIST.strip())
        self.assertIn("adult man", persona.MALE_PROTAGONIST)

    def test_comic_brief_contains_all_four_parts(self):
        """brief 要含：女主特征 + 男主特征 + 状态 + 剧情。"""
        from unittest import mock
        card = {"name": "纱雾",
                "appearancePrompt": "1girl, silver hair, twintails"}
        hist = [{"role": "user", "content": "我抱住她。"},
                {"role": "assistant", "content": "她的呼吸乱了。\n画面：x\n心情：害羞 强度8\n状态：y"}]
        with mock.patch.object(persona, "trim", return_value=hist):
            brief = persona.comic_brief(card, "private_1",
                                        extra="引用的那段剧情")
        self.assertIn("纱雾", brief)                    # 女主名
        self.assertIn("silver hair", brief)             # 女主外观
        self.assertIn(persona.MALE_PROTAGONIST, brief)  # 男主特征
        self.assertIn("我抱住她", brief)                 # 剧情
        self.assertIn("引用的那段剧情", brief)            # 引用内容

    def test_comic_brief_carries_scene_line(self):
        """「画面：」必须进 brief，而且排在用户原话**之前**。

        2026-11-04：这里以前只取 `split_turn()[0]`，把画面行丢了。编剧拿着
        中文散文猜英文构图，实测把「坐在小凳上」画成「反折在椅子上、头抵地」。
        画面行是她自己写好的定稿，姿态/衣着/接触点都在里面。
        """
        from unittest import mock
        card = {"name": "纱雾", "appearancePrompt": "an adult woman with silver hair"}
        scene = "sitting on the stool with her knees drawn together, medium close-up"
        hist = [{"role": "user", "content": "我按住她的手。"},
                {"role": "assistant",
                 "content": "她低下头。\n画面：" + scene + "\n心情：害羞 强度8\n状态：坐在小凳上"}]
        with mock.patch.object(persona, "trim", return_value=hist):
            brief = persona.comic_brief(card, "private_1")
        self.assertIn(scene, brief)
        # 画面在用户原话之前（优先级：画面 > 原话 > 快照）
        self.assertLess(brief.find(scene), brief.find("我按住她的手"))
        # 并且明确写了「不许改姿势」
        self.assertIn("改姿势", brief)

    def test_comic_brief_msg_index_uses_that_turn_scene(self):
        """按归档行号取回复时，用的必须是**那一轮**的画面，不是最后一轮的。

        2026-11-04 改口径：`msg_index` 从「窗口里第几条 assistant」变成
        「归档第几行」。窗口序号每聊一轮就整体前移，压缩后点旧气泡会画错人。
        """
        from unittest import mock
        card = {"name": "纱雾", "appearancePrompt": "an adult woman with silver hair"}
        rows = [{"role": "user", "content": "第一句"},
                {"role": "assistant",
                 "content": "第一次回复。\n画面：standing by the window\n心情：平静 强度2\n状态：站在窗边"},
                {"role": "user", "content": "第二句"},
                {"role": "assistant",
                 "content": "第二次回复。\n画面：lying on the bed\n心情：害羞 强度6\n状态：躺在床上"}]
        with mock.patch.object(persona, "load_archive", return_value=rows):
            brief = persona.comic_brief(card, "private_1", msg_index=1)
        self.assertIn("standing by the window", brief)
        self.assertNotIn("lying on the bed", brief)
        self.assertIn("第一句", brief)                 # 它前面那条用户消息
        self.assertNotIn("第二句", brief)

    def test_shot_scene_shares_extraction_with_comic_brief(self):
        """单张 CG 的画面提取和 moss 漫画同源：同轮同画面、同归档行号口径。"""
        from unittest import mock
        rows = [{"role": "user", "content": "第一句"},
                {"role": "assistant",
                 "content": "第一次回复。\n画面：standing by the window\n心情：平静 强度2\n状态：站在窗边"},
                {"role": "user", "content": "第二句"},
                {"role": "assistant",
                 "content": "第二次回复。\n画面：lying on the bed\n心情：害羞 强度6\n状态：躺在床上"}]
        with mock.patch.object(persona, "load_archive", return_value=rows):
            self.assertEqual(persona.shot_scene("private_1", 1),
                             "standing by the window")
            self.assertEqual(persona.shot_scene("private_1", 3),
                             "lying on the bed")

    def test_shot_scene_empty_when_no_scene_line(self):
        """没有画面行 → 空串（调用方按拍板提示「没得画」，不做 LLM 兜底）。"""
        from unittest import mock
        with mock.patch.object(persona, "trim", return_value=[
                {"role": "assistant", "content": "没有画面行的一轮"}]):
            self.assertEqual(persona.shot_scene("private_1"), "")


class RosterCardTest(PersonaBase):
    """`#角色扮演` 的规则用聊天记录卡片发（2026-11-03）。"""

    def setUp(self):
        super().setUp()
        persona.save_cards([_card("纱雾")])

    def test_roster_asks_for_a_card(self):
        for t in ("#角色扮演", "#人设列表", "@大大怪 #角色扮演"):
            self.assertTrue(persona.is_roster_request(t, addressed=True), t)

    def test_bare_words_no_longer_ask_for_a_card(self):
        """不带前缀的「角色扮演」是普通聊天（2026-11-05 用户改口）。

        之前为了「大家不爱打井号」放开了不带前缀，结果日常聊天里的
        「妹妹」「角色扮演」都可能误触。现在命令统一要 `#` / `/`。
        """
        for t in ("角色扮演", "人设列表", "@大大怪 角色扮演"):
            self.assertFalse(persona.is_roster_request(t, addressed=True), t)
        self.assertFalse(persona.is_roster_request("角色扮演"))
        self.assertTrue(persona.is_roster_request("/角色扮演"))

    def test_card_name_is_not_a_roster(self):
        self.assertFalse(persona.is_roster_request("#纱雾", addressed=True))

    def test_nodes_split_and_cover_everything(self):
        nodes = persona.roster_nodes()
        self.assertGreaterEqual(len(nodes), 2)
        joined = "\n".join(nodes)
        for word in ("出图", "清空", "出戏", "角色名单", "纱雾"):
            self.assertIn(word, joined, word)

    def test_roster_explains_shot_command(self):
        """规则里必须写清「出图」怎么用——这是用户点名要的。"""
        text = persona.roster_text()
        self.assertIn("出图", text)
        self.assertIn("横屏 CG", text)

    def test_roster_requires_prefix_and_says_so(self):
        """2026-11-05 用户改口：规则里要明说命令得带 `#` / `/` 前缀。"""
        text = persona.roster_text()
        self.assertIn("带前缀", text)
        self.assertIn("`#` 或 `/`", text)
        self.assertIn("#纱雾", text)      # 名单里也带前缀，玩家照着发就行


class PrefixCommandTest(PersonaBase):
    """命令统一要 `#` / `/` 前缀（2026-11-05 用户改口）。

    前一版为了「大家不爱打井号」把前缀做成可选，结果日常聊天里的
    「妹妹」「清空」「好感」都有可能被误触。现在不带前缀一律当普通聊天。
    """

    def setUp(self):
        super().setUp()
        persona.save_cards([_card("妹妹"), _card("妹妹闺蜜")])

    def test_bare_card_name_is_plain_chat(self):
        self.assertIsNone(
            persona.handle_command("private_1", "妹妹", addressed=True))
        self.assertIsNone(persona.active_card("private_1"))

    def test_bare_list_word_is_plain_chat(self):
        self.assertIsNone(
            persona.handle_command("private_1", "角色扮演", addressed=True))

    def test_name_prefix_without_hash_is_plain_chat(self):
        """叫了机器人但没打前缀：还是普通聊天，不切角色。"""
        self.assertIsNone(
            persona.handle_command("private_1", "大大怪，角色扮演",
                                   addressed=True))

    def test_hash_and_slash_both_work(self):
        for text in ("#妹妹", "/妹妹"):
            got = persona.handle_command("private_1", text, addressed=True)
            self.assertIsNotNone(got, text)
            self.assertIn("妹妹", got)
            self.assertIsNotNone(persona.active_card("private_1"))

    def test_command_word_plus_description(self):
        """「#角色扮演，你是我的白色小兔妹妹」→ 从描述里认出卡名。"""
        got = persona.handle_command(
            "private_1", "#角色扮演，你是我的妹妹闺蜜，我们相依为命",
            addressed=True)
        self.assertIsNotNone(got)
        self.assertIn("妹妹闺蜜", got)      # 长名字优先，不能被「妹妹」抢走

    def test_partial_mention_does_not_trigger(self):
        """整条消息必须是卡名——「妹妹想你了」是聊天，不是切角色。"""
        for t in ("她妹妹", "我的妹妹", "妹妹说", "妹妹想你了",
                  "#她妹妹", "#我的妹妹"):
            self.assertIsNone(
                persona.handle_command("private_1", t, addressed=True), t)

    def test_prefixed_leave_and_clear(self):
        persona.switch("private_1", persona.find_card("妹妹"))
        self.assertIn("出戏", persona.handle_command("private_1", "#出戏",
                                                    addressed=True))
        persona.switch("private_1", persona.find_card("妹妹"))
        self.assertIn("清空", persona.handle_command("private_1", "#清空",
                                                    addressed=True))

    def test_bare_leave_and_clear_are_plain_chat(self):
        persona.switch("private_1", persona.find_card("妹妹"))
        self.assertIsNone(persona.handle_command("private_1", "出戏",
                                                 addressed=True))
        self.assertIsNone(persona.handle_command("private_1", "清空",
                                                 addressed=True))


class ArchiveTest(PersonaBase):
    """归档（2026-11-03）：窗口只留 12 条给模型看，存档要全。

    用户报「记录的轮次有问题」——查下来是历史文件卡在 `HISTORY_LIMIT`，
    聊超过 6 轮之后早先的对话被挤掉了，查看台只能看到最近几轮。
    """

    def setUp(self):
        super().setUp()
        persona.save_cards([_card("纱雾")])
        self.card = persona.find_card("纱雾")

    def test_archive_path_differs_from_window(self):
        a = persona._archive_path("private_1")
        w = persona._hist_path("private_1")
        self.assertNotEqual(a, w)
        self.assertIn("archive", os.path.basename(a))

    def test_seed_creates_archive(self):
        """补写：msgs 为空、seed 非空也要能写。

        第一版守卫写成 `if not msgs: return`，把铺底短路掉了——
        补写脚本报「已写入」，实际一个文件都没建。
        """
        persona._append_archive("private_1", [], seed=[
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": "b"},
        ])
        p = persona._archive_path("private_1")
        self.assertTrue(os.path.isfile(p))
        self.assertEqual(len(persona._read_jsonl(p)) if hasattr(persona, "_read_jsonl")
                         else sum(1 for _ in open(p, encoding="utf-8")), 2)

    def test_append_does_not_duplicate_seed(self):
        """归档已存在时只追加新消息，不重铺 seed。"""
        persona._append_archive("private_1", [], seed=[{"role": "user", "content": "a"}])
        persona._append_archive("private_1", [{"role": "user", "content": "b"}],
                                seed=[{"role": "user", "content": "a"}])
        p = persona._archive_path("private_1")
        n = sum(1 for _ in open(p, encoding="utf-8"))
        self.assertEqual(n, 2, "应该 a + b，不能重复写 a")

    def test_archive_survives_window_truncation(self):
        """窗口被截断后，归档仍然保留全部。"""
        key = "private_arc"
        # 手动模拟：10 轮对话，每轮追加到归档
        for i in range(10):
            persona._append_archive(key, [
                {"role": "user", "content": "u%d" % i},
                {"role": "assistant", "content": "a%d" % i},
            ])
        arc = persona._archive_path(key)
        self.assertEqual(sum(1 for _ in open(arc, encoding="utf-8")), 20)
        # 窗口只有 12 条的限制不受影响
        self.assertEqual(persona.HISTORY_LIMIT, 12)


class NoteBudgetTest(unittest.TestCase):
    """小记预算（2026-11-03）：500 → 1000 字。

    用户要求「尽可能保留相关的记忆，token 消耗和生图差不多即可」。
    实测 1000 字只比 500 字多 272 token/轮，而记忆密度翻倍。
    """

    def test_note_capacity(self):
        self.assertEqual(persona.NOTE_MAX_CHARS, 1000)

    def test_compression_runs_more_often_than_the_window(self):
        """压缩周期必须 < 窗口轮数，否则有内容「掉出窗口又没进小记」。"""
        self.assertLess(persona.NOTE_EVERY_TURNS, persona.HISTORY_LIMIT // 2)

    def test_prompt_asks_for_structure_not_summary(self):
        """提示词要分开列「值得留」和「不要写」。

        原来只说「只留以后还用得上的事」，太笼统——模型倾向写流水账，
        或者只写一句概括。
        """
        p = persona._NOTE_PROMPT
        for word in ("重点保留这些", "不要写这些", "关系进展", "答应过什么"):
            self.assertIn(word, p, word)

    def test_token_cost_stays_near_image_generation(self):
        """一轮扮演的 token 不该远超生图一次（用户定的目标）。

        生图链路（编剧 + 转译）实测约 3000~5000 token/次。
        """
        card = {"name": "x", "systemPrompt": "y" * 60}
        note = "回" * persona.NOTE_MAX_CHARS
        head = persona.build_head(card, "private_1", "", note, "状态" * 30)
        # 历史 12 条 × 约 400 字
        total_chars = len(head) + 12 * 400 + 30
        approx_tokens = total_chars * 0.65
        # 2026-11-05：加了「分段 + 导演条」后只涨了约 100 token（+1.7%），
        # 仍在生图链路（3000~5000 token）的两倍以内，门槛从 8000 放到 8200。
        # 2026-11-06：系统头最前面加了一段约 120 字的「私下虚构创作」框定
        # （压掉跳出角色的元话语），再涨约 80 token，门槛放到 8400。
        self.assertLess(approx_tokens, 8400,
                        "扮演一轮 %.0f token，超过生图链路太多" % approx_tokens)


class NoteClipTest(unittest.TestCase):
    """小记超长时的截断（2026-11-04 用户报的 bug）。

    他做的早餐被 AI 说成「我做的」，同时「回忆」看着一直没更新。根因是
    `note[:1000]` 硬切：小记按时序写，**最新的事在末尾**，一超长就整段被砍，
    日志里那条线连续 7 次压缩全是「正好 1000 字」，全切在最新那段之前。
    """

    def test_short_note_untouched(self):
        s = "第一段。\n\n第二段。"
        self.assertEqual(persona._clip_note(s, 1000), s)

    def test_long_note_keeps_head_and_tail(self):
        head = "【开头】我们认识很久了，他是我最亲近的人。"
        tail = "【结尾】今天早上他做了早餐，我吃了，还说好吃。"
        middle = "\n".join("中间第%d段，写些不太重要的过渡。" % i
                           for i in range(200))
        out = persona._clip_note("\n".join([head, middle, tail]), 320)
        self.assertLessEqual(len(out), 320)
        self.assertIn("【开头】", out)       # 关系的基础要留住
        self.assertIn("【结尾】", out)       # 刚发生的事更要留住

    def test_single_block_cut_at_sentence_end(self):
        """整段没有换行时退化成按句截，不能切在半句上。"""
        s = "他做了早餐。" * 100
        out = persona._clip_note(s, 100)
        self.assertLessEqual(len(out), 100)
        self.assertTrue(out.endswith("。") or out.endswith("…"), out[-6:])

    def test_clip_never_exceeds_limit(self):
        s = "\n".join("第%d段。" % i + "字" * 40 for i in range(100))
        for limit in (50, 120, 300, 1000):
            self.assertLessEqual(len(persona._clip_note(s, limit)), limit)


class OpeningTest(PersonaBase):
    """开场导读（2026-11-04 用户要求）。

    「开始聊天的时候给我网页一段灰色的摘要，先说明当前是什么情况，不然我就
    莫名其妙地开始了」。它只在新线生成一次，之后读缓存——不能每开一次页面
    都多烧一次 LLM。
    """

    def _line(self, name="纱雾"):
        persona.save_cards([_card(name)])
        persona.switch_line("private_1", persona.find_card(name))
        return persona.resolve_line("private_1")

    def test_generated_intro_is_cached(self):
        key = self._line()
        with mock.patch.object(persona.llm, "call_llm",
                               return_value="早上，她在二楼画画。") as m:
            self.assertEqual(persona.opening(key), "早上，她在二楼画画。")
            self.assertEqual(m.call_count, 1)
        # 第二次走缓存，不再调模型
        with mock.patch.object(persona.llm, "call_llm") as m2:
            self.assertEqual(persona.opening(key), "早上，她在二楼画画。")
            m2.assert_not_called()

    def test_no_card_returns_empty(self):
        self.assertEqual(persona.opening(persona.resolve_line("private_9")), "")

    def test_failure_returns_empty(self):
        """生成失败不能让页面打不开，也不能把空串写进缓存。"""
        key = self._line()
        with mock.patch.object(persona.llm, "call_llm",
                               side_effect=RuntimeError("boom")):
            self.assertEqual(persona.opening(key), "")
        self.assertEqual(str((persona._entry(key) or {}).get("intro") or ""), "")

    def test_wipe_clears_intro(self):
        """清空存档后要按新起点重铺开场，不能留着上一轮的场景。"""
        key = self._line()
        st = persona._load_state()
        st[key]["intro"] = "旧场景"
        persona._save_state(st)
        persona.wipe(key)
        self.assertEqual(str((persona._entry(key) or {}).get("intro") or ""), "")


def _strip_quoted(text):
    """剔掉各种引号/反引号/星号包起来的内容，只留叙述正文。

    契约里会**引用**坏例子当反例（「不是「他」」「把「他」换成「你」」），
    那是必要的教学文本，数人称时不该算进去。
    """
    s = re.sub(r"「[^」]*」", "", text)
    s = re.sub(r"`[^`]*`", "", s)
    s = re.sub(r"\*[^*]*\*", "", s)
    s = re.sub(r"【[^】]*】", "", s)
    return s


class LayeredGuideTest(unittest.TestCase):
    """分层亲密指导（2026-11-04 深夜）。

    素材来自 `skills/writing/`。核心是**按高潮值分档**：同一个「做」，
    高潮值 20 和 90 的写法完全是两回事，不分层模型会一路用同一个调子
    写到结束——从「还没起来」直接跳到「到了」，中间三档等于不存在。

    ⚠️ 类名别叫 `IntimateGuideTest`——文件里已经有一个，同名会**覆盖**掉
    它（Python 后定义者胜），那 4 条老测试就静默消失了。踩过一次。
    """

    def test_free_mode_adds_nothing(self):
        """没开攻略 / 自由模式：只留通用写法，不拼分层。"""
        self.assertEqual(persona.intimate_guide(None), "")
        self.assertEqual(persona.intimate_guide({}), "")
        self.assertEqual(persona.intimate_guide({"active": False}), "")

    def test_each_orgasm_level_gets_its_own_block(self):
        """五档各有一块，而且互不相同。

        2026-11-05：笔法层（`writing_skills`）接进来之后，`###` 标题还会多出
        规范库自己的小标题。这里只认节奏层的「### 这一拍：…」。
        """
        seen = {}
        for ex in (10, 40, 60, 80, 95):
            g = persona.intimate_guide({"active": True, "excite": ex,
                                        "corrupt": 0})
            heads = [l for l in g.splitlines()
                     if l.startswith("### 这一拍：")]
            self.assertEqual(len(heads), 1, "excite=%d 应该正好一块" % ex)
            seen[ex] = heads[0]
        self.assertEqual(len(set(seen.values())), 5, "五档不能有重复的块")

    def test_out_of_scene_has_no_layer(self):
        """场景外（高潮值 0、四根轴都没动）不拼分层——那不是「还没被带起来」，是没开始。

        2026-11-05：这条只在「什么都没起来」时成立。堕落/屈服/戒备/好感
        任何一根起来了，这一拍就还有该写的笔法，那时该拼。
        """
        st = {"active": True, "excite": 0, "corrupt": 0,
              "submit": 0, "suspicion": 0, "value": 0}
        self.assertEqual(persona.intimate_guide(st), "")
        for kw in ({"corrupt": 60}, {"submit": 60}, {"suspicion": 30},
                   {"value": 60}):
            s = dict(st)
            s.update(kw)
            self.assertNotEqual(persona.intimate_guide(s), "",
                                "有轴在动就该拼：%s" % kw)

    def test_high_corruption_adds_another_layer(self):
        """堕落 >= 50 时多加一层：她的反应里有一层她不想要的东西。"""
        low = persona.intimate_guide({"active": True, "excite": 60,
                                      "corrupt": 20})
        high = persona.intimate_guide({"active": True, "excite": 60,
                                       "corrupt": 60})
        self.assertNotIn("她现在不一样了", low)
        self.assertIn("她现在不一样了", high)

    def test_base_guide_teaches_orgasm_and_dialogue(self):
        """通用段必须覆盖两件最容易写坏的事。

        - 高潮不能一句话带过（要三层：越界的瞬间 / 连续反应 / 之后怎么接）
        - 对话要跟着兴奋度碎掉，但不能全程只有「嗯啊」
        """
        base = persona._INTIMATE_GUIDE
        self.assertIn("高潮不是一句话", base)
        self.assertIn("对话要跟着碎", base)
        self.assertIn("别把「指导」写进正文", base)

    def test_guide_uses_second_person(self):
        """分层块里不能出现「他」——人称规则对通用段和分层段一视同仁。"""
        for ex in (10, 40, 60, 80, 95):
            g = persona.intimate_guide({"active": True, "excite": ex,
                                        "corrupt": 60})
            self.assertNotIn("他", g, "excite=%d 的分层里还有「他」" % ex)


class NovelCraftContractTest(unittest.TestCase):
    """2026-11-06：把酒馆小说笔法搬进契约，按「白象效应」改成正向表述。

    用户三条硬要求：① 输出内容像小说一样有趣、不死板；② 字数从 400~800
    放宽到 1000 常态 / 2000 上限；③ 少写「不能」，多写「可以这样」。
    单轮 token 预算给了 1 万~1 万 2，但预算不是拿来塞规范库的——
    30 万字符的 `skills/writing/` 只萃取了十来条能直接用的招进契约。
    """

    def test_contract_is_positive_not_prohibition_heavy(self):
        """每轮必拼的契约正文里，禁令词要几乎清零（白象效应）。"""
        c = persona._CONTRACT_NO_INTIMATE   # 不含亲密指导，日常轮也拼这一段
        for w in ("不许", "不能", "禁止", "必须", "算不合格", "缺一段都不行",
                  "两条硬规矩", "不许用「他」"):
            self.assertNotIn(w, c, "契约正文里还在用禁令词：%s" % w)
        # 唯一剩下的「不要」是「要不要配图」，不是禁令
        self.assertLessEqual(c.count("不要"), 1)

    def test_contract_teaches_novel_craft(self):
        """酒馆那套让内容有趣的招式，得真的进每一轮提示词。"""
        c = persona._CONTRACT
        for phrase in ("可以这样写", "先给她一个能碰的东西", "闲笔",
                       "没用的道具", "对话可以碎", "情绪写成身体", "专属感",
                       "不回收的干扰项", "潜台词", "句式跟着情绪走",
                       "这一轮可以什么都没发生", "均匀的机灵 = AI"):
            self.assertIn(phrase, c, "笔法节漏了：%s" % phrase)

    def test_word_count_relaxed(self):
        """600~2000 字区间；旧的 400~800、1000 常态、字数不是目标、
        允许烂尾都要撤掉，均匀密度配额也不回来。"""
        c = persona._CONTRACT
        self.assertIn("600~2000 字", c)
        self.assertIn("把这一拍写完整", c)
        self.assertIn("收得住", c)
        self.assertNotIn("1000 字", c)
        self.assertNotIn("400~800", c)
        self.assertNotIn("字数不是目标", c)
        self.assertNotIn("允许烂尾", c)
        self.assertNotIn("3~4 段", c)
        self.assertNotIn("每段都要有具体的感官细节", c)
        self.assertNotIn("这一轮要有变化", c)

    def test_craft_reaches_every_turn_including_non_intimate(self):
        """笔法写在契约里，日常轮（不带亲密指导）也要吃到。"""
        plain = persona._CONTRACT_NO_INTIMATE
        self.assertIn("可以这样写", plain)
        self.assertIn("闲笔", plain)
        head = persona.build_head({"name": "x", "systemPrompt": "y"},
                                  "private_1", "", intimate=False)
        self.assertIn("可以这样写", head)

    def test_history_keeps_a_full_long_reply(self):
        """正文放宽到 2000 字后，历史单条也得装得下，不然下一轮只剩半截。"""
        self.assertGreaterEqual(persona.MSG_MAX_CHARS, 2000)
        body = "她" * 2000
        hist = [{"role": "assistant",
                 "content": body + "\n画面：x\n心情：平静 强度2\n状态：在家 · 卫衣 · 无"}]
        got = persona.trim(hist)[0]["content"]
        self.assertIn("画面：x", got)          # 结构化尾巴还在
        self.assertGreater(len(got), 1900)     # 正文没被砍掉一多半
        self.assertLessEqual(len(got), persona.MSG_MAX_CHARS + 40)

    def test_guard_block_is_positive(self):
        """框定段原来四条里三条是「不…」，改成正向。"""
        g = persona._guard_block()
        for w in ("不道歉", "不声明身份", "不要复述"):
            self.assertNotIn(w, g)
        self.assertIn("全程待在角色里", g)

    def test_monologue_melts_into_narration(self):
        """2026-11-06：心声改成混在第三人称叙述里的念头，靠例子教写法。"""
        c = persona._CONTRACT_NO_INTIMATE
        self.assertIn("念头可以就长在叙述里", c)
        self.assertIn("这一下有点像在拖延", c)
        self.assertIn("怎么偏偏是腰", c)
        self.assertIn("一轮留一处就够", c)
        self.assertIn("这一轮一句都没有也很正常", c)

    def test_monologue_has_no_quota_or_tag(self):
        """硬配额和元数据标签都要从每轮提示词里消失。"""
        c = persona._CONTRACT_NO_INTIMATE
        for w in ("【心声】", "两处", "单独成行", "内心独白", "每轮给她"):
            self.assertNotIn(w, c, "契约里还留着旧心声规则：%s" % w)


class MonologueTagTest(unittest.TestCase):
    """2026-11-06：旧回复里的【心声】标签只剥读取/出口副本，落盘数据不动。"""

    RAW = ("她把杯子转了半圈。\n\n"
           "【心声】好烫。\n"
           "【心声】别说明日。\n\n"
           "她没再说话。")

    def test_tag_stripped_words_kept(self):
        out = persona._strip_monologue_tag(self.RAW)
        self.assertNotIn("【心声】", out)
        self.assertIn("好烫。", out)
        self.assertIn("别说明日。", out)
        self.assertIn("她把杯子转了半圈。", out)

    def test_wrapped_tag_also_stripped(self):
        """历史里还有用反引号 / 星号包住整行的旧格式。"""
        out = persona._strip_monologue_marks(
            "`【心声】好烫。`\n*【心声】别说明日。*")
        self.assertNotIn("【心声】", out)
        self.assertIn("别说明日。", out)

    def test_split_turn_strips_tag(self):
        body, _sc, _mo, _it, _st = persona.split_turn(self.RAW)
        self.assertNotIn("【心声】", body)
        self.assertIn("别说明日。", body)

    def test_model_history_copy_is_clean(self):
        hist = [{"role": "assistant", "content": self.RAW}]
        got = persona._render_hist(hist)[0]["content"]
        self.assertNotIn("【心声】", got)
        self.assertEqual(hist[0]["content"], self.RAW)

    def test_archive_copy_is_clean_disk_unchanged(self):
        rows = [{"role": "assistant", "content": self.RAW}]
        with mock.patch.object(persona, "_read_archive_file", return_value=rows):
            got = persona.load_archive("private_1")
        self.assertNotIn("【心声】", got[0]["content"])
        self.assertEqual(rows[0]["content"], self.RAW)


class PronounRuleTest(unittest.TestCase):
    """人称规则（2026-11-03）：她是「她」，对面是「你」，不许写成「他」。

    用户原话：「不要用她怎么样他，要用她怎么样你」。这是第二人称代入——
    读的人就是对话里那个人。
    """

    CARD = {"name": "纱雾", "systemPrompt": "你是纱雾。"}

    def test_contract_has_no_he_pronoun(self):
        h = persona.build_head(self.CARD, "private_1", "")
        # 规则说明里会**引用**「他」当反例（「不是「他」」「把「他」换成「你」」），
        # 那是必要的教学文本。这里把各种引号里的内容剔掉再数——
        # 剩下的正文描述里不该出现代词「他」。
        body = _strip_quoted(h)
        self.assertEqual(body.count("他"), 0,
                         "系统头正文里还有「他」，模型会照着学")

    def test_contract_states_the_rule(self):
        h = persona.build_head(self.CARD, "private_1", "")
        self.assertIn("人称对照", h)
        self.assertIn("称呼与「你」", h)
        self.assertIn("她的心思", h)

    def test_rule_repeated_at_the_end(self):
        """契约末尾要再强调——长上下文里开头的规则会被稀释。"""
        h = persona.build_head(self.CARD, "private_1", "")
        tail = h[-700:]
        self.assertIn("「你」", tail)
        self.assertIn("她的念头", tail)

    def test_plain_contract_also_clean(self):
        h = persona.build_head(self.CARD, "private_1", "", intimate=False)
        self.assertEqual(_strip_quoted(h).count("他"), 0)

    def test_example_dialogue_uses_you(self):
        """示例对话里也不能有「他」——它的影响力比契约还大。

        实测：契约改了但示例没改，生成的内容里「他」出现 7 次；
        示例改完之后降到 0 次。
        """
        c = dict(self.CARD)
        c["exampleDialogue"] = "你：在吗？\n她：……嗯。"
        h = persona.build_head(c, "private_1", "")
        self.assertIn("你：在吗？", h)
        self.assertNotIn("他", c["exampleDialogue"])


class TemperatureTest(unittest.TestCase):

    def test_persona_uses_non_default_temperature(self):
        """角色扮演要走自己的温度，不能用服务端默认。

        默认（一般 0.7）写日常够用，写亲密场景偏保守。
        """
        self.assertGreaterEqual(persona.TEMPERATURE, 1.0)
        self.assertLessEqual(persona.TEMPERATURE, 1.3)

    def test_call_llm_accepts_temperature(self):
        import inspect
        from app import llm
        sig = inspect.signature(llm.call_llm)
        self.assertIn("temperature", sig.parameters)


class ExampleDialogueTest(unittest.TestCase):
    """示例对话（2026-11-03）：酒馆的 `mes_example`，影响文风最重的一块。

    抽象规则让模型「推导」怎么写，示例让它「照着学」——实测同一句话，
    加了示例之后动作密度和断句方式明显向示例靠拢。
    """

    def _head(self, ex):
        c = {"name": "纱雾", "systemPrompt": "你是纱雾。"}
        if ex:
            c["exampleDialogue"] = ex
        return persona.build_head(c, "private_1", "")

    def test_example_is_injected(self):
        h = self._head("他：在吗？\n她：……嗯。")
        self.assertIn("她说话的示范", h)
        self.assertIn("……嗯。", h)

    def test_no_example_is_fine(self):
        for ex in ("", None):
            h = self._head(ex)
            self.assertNotIn("她说话的示范", h)
            # 没示例也不能少契约
            self.assertIn("最优先的一条", h)

    def test_example_comes_before_contract(self):
        """示例在人设之后、契约之前。

        放契约后面的话，模型会照抄示例的格式（比如不写三行）；
        放人设后面，它读起来是「这个人说话是这样的」。
        """
        h = self._head("他：在吗？\n她：……嗯。")
        self.assertLess(h.find("她说话的示范"), h.find("最优先的一条"))

    def test_norm_card_keeps_the_field(self):
        c = persona._norm_card({"name": "x", "exampleDialogue": "  abc  "})
        self.assertEqual(c["exampleDialogue"], "abc")

    def test_missing_field_defaults_to_empty(self):
        c = persona._norm_card({"name": "x"})
        self.assertEqual(c["exampleDialogue"], "")


class UpdateCardFieldsTest(PersonaBase):
    """按字段更新（2026-11-03）：内置卡也能改，但只动点名的字段。"""

    def setUp(self):
        super().setUp()
        persona.save_cards([{"id": "izumi_sagiri", "name": "纱雾",
                             "systemPrompt": "原版人设"}] * 1)

    def test_can_update_builtin_card(self):
        r = persona.update_card_fields("izumi_sagiri", exampleDialogue="示范")
        self.assertIsNotNone(r)
        c = [x for x in persona.load_cards() if x["id"] == "izumi_sagiri"][0]
        self.assertEqual(c["exampleDialogue"], "示范")
        # 别的字段没被动
        self.assertEqual(c["systemPrompt"], "原版人设")

    def test_id_cannot_be_changed(self):
        persona.update_card_fields("izumi_sagiri", id="hacked")
        ids = [x["id"] for x in persona.load_cards()]
        self.assertIn("izumi_sagiri", ids)
        self.assertNotIn("hacked", ids)

    def test_unknown_card_returns_none(self):
        self.assertIsNone(persona.update_card_fields("nope", exampleDialogue="x"))

    def test_save_cards_still_protects_builtin(self):
        """`save_cards` 那条保护没被削弱——两条口子分开的。"""
        persona.save_cards([{"id": "izumi_sagiri", "name": "篡改",
                             "systemPrompt": "改了"}])
        c = [x for x in persona.load_cards() if x["id"] == "izumi_sagiri"][0]
        self.assertEqual(c["systemPrompt"], "原版人设")


class GroupNoRoleplayTest(PersonaBase):
    """群聊不实际扮演（2026-11-03 用户反馈）。

    起因：群里机器人发的话**全群可见**，状态隔离（`_persona_key`）没坏，
    但内容公开这件事压不住。用户玩的是亲密内容，不能公开。
    """

    def setUp(self):
        super().setUp()
        persona.save_cards([_card("纱雾")])

    def test_group_card_name_redirects(self):
        got = persona.handle_command("group_1_100", "#纱雾", addressed=True)
        self.assertIsNotNone(got)
        self.assertIn("不方便演", got)
        self.assertIn("私聊", got)
        # 不能在群里真的切进角色
        self.assertIsNone(persona.active_card("group_1_100"))

    def test_group_roster_still_visible(self):
        """规则本身不敏感，群里看规则仍然允许。"""
        got = persona.handle_command("group_1_100", "#角色扮演", addressed=True)
        self.assertIn("角色名单", got)

    def test_group_turn_returns_nothing(self):
        """群聊这条线彻底不演——即使 state 里还留着旧的扮演态。"""
        persona.switch("group_1_100", persona.find_card("纱雾"))
        turn = persona.run_turn("group_1_100", "在吗", "group", "张三", "100")
        self.assertEqual(turn.reply, "")

    def test_private_still_works(self):
        got = persona.handle_command("private_100", "#纱雾", addressed=True)
        self.assertIn("纱雾", got)
        self.assertIsNotNone(persona.active_card("private_100"))


class RosterWebLinkTest(PersonaBase):
    """私聊的规则里带网页入口，群聊不带（2026-11-03 用户拍板）。

    理由：QQ 里打字出图太麻烦，网页点按钮就行；但外链发群里容易被风控、
    一次就刷屏，所以群里只回规则本身。
    """

    def setUp(self):
        super().setUp()
        persona.save_cards([_card("纱雾")])

    def test_private_roster_has_web_link(self):
        t = persona.roster_text(site_url="https://x.trycloudflare.com")
        self.assertIn("网页版", t)
        self.assertIn("https://x.trycloudflare.com/persona", t)

    def test_group_roster_has_no_web_link(self):
        t = persona.roster_text(site_url="")
        self.assertNotIn("网页版", t)
        self.assertNotIn("trycloudflare", t)

    def test_private_command_includes_link(self):
        with mock.patch("app.friend_site.url",
                        return_value="https://y.trycloudflare.com"):
            got = persona.handle_command("private_1", "#角色扮演",
                                         addressed=True)
        self.assertIn("https://y.trycloudflare.com/persona", got)

    def test_group_command_excludes_link(self):
        with mock.patch("app.friend_site.url",
                        return_value="https://y.trycloudflare.com"):
            got = persona.handle_command("group_1", "#角色扮演",
                                         addressed=True)
        self.assertNotIn("trycloudflare", got)

    def test_tunnel_down_still_returns_roster(self):
        """隧道没开（拿不到地址）时，规则本身不能少。"""
        with mock.patch("app.friend_site.url", return_value=""):
            got = persona.handle_command("private_1", "#角色扮演",
                                         addressed=True)
        self.assertIn("角色名单", got)
        self.assertNotIn("网页版", got)


class ArchiveHistoryTest(PersonaBase):
    """网页回看历史（2026-11-04）：读**归档**，不是 12 条滑动窗口。

    用户原话：「我主要是要我可以回到看从开始到现在的聊天记录」。
    数据本来就在 `persona_archive_*.jsonl` 里，以前的接口只喂窗口。
    """

    def setUp(self):
        super().setUp()
        persona.save_cards([_card("纱雾")])
        self.card = persona.find_card("纱雾")
        self.key = "web_1__" + self.card["id"]
        persona.switch(self.key, self.card)

    def _seed(self, turns):
        rows = []
        for i in range(turns):
            rows.append({"role": "user", "content": "第%d问" % i})
            rows.append({"role": "assistant",
                         "content": "第%d答\n画面：scene %d\n心情：平静 强度2\n状态：坐着"
                                    % (i, i)})
        persona._save_hist(self.key, rows)
        persona._append_archive(self.key, [], seed=rows)
        return rows

    def test_archive_keeps_more_than_window(self):
        self._seed(10)                              # 20 条
        # 模型看到的是窗口（`trim` 裁到 12），归档一条不少
        self.assertEqual(len(persona.trim(persona._load_hist(self.key))),
                         persona.HISTORY_LIMIT)
        self.assertEqual(len(persona.load_archive(self.key)), 20)

    def test_archive_keeps_long_text_whole(self):
        """回看不截断：`trim()` 的 900 字上限是给模型省 token 的，
        拿来截历史会让每条回复都变成半句话。"""
        long = "很长的正文，" * 200                 # 1200 字
        rows = [{"role": "user", "content": "说点长的"},
                {"role": "assistant", "content": long}]
        persona._save_hist(self.key, rows)
        persona._append_archive(self.key, [], seed=rows)
        self.assertEqual(persona.load_archive(self.key)[1]["content"], long)

    def test_history_page_walks_backwards(self):
        self._seed(20)                               # 40 条
        start, chunk, total = persona.history_page(self.key)
        self.assertEqual(total, 40)
        self.assertEqual(start, 40 - persona.ARCHIVE_PAGE)
        self.assertEqual(len(chunk), persona.ARCHIVE_PAGE)
        # 上一页：结尾正好接在首屏第一条前面，不重不漏
        start2, chunk2, _ = persona.history_page(self.key, before=start)
        self.assertEqual(start2, 0)
        self.assertEqual(start2 + len(chunk2), start)
        self.assertEqual(chunk2[-1]["content"], "第7答\n画面：scene 7\n心情：平静 强度2\n状态：坐着")

    def test_archive_falls_back_to_window(self):
        """归档还没建起来的老线：退回窗口原文，不能整段空白。"""
        persona._save_hist(self.key, [{"role": "user", "content": "只有窗口"}])
        self.assertEqual(persona.load_archive(self.key)[0]["content"], "只有窗口")

    def test_erase_removes_archive(self):
        """清空存档要连归档一起删，否则「删了」还能整段翻出来。"""
        self._seed(3)
        path = persona._archive_path(self.key)
        self.assertTrue(os.path.exists(path))
        persona.wipe(self.key)
        self.assertFalse(os.path.exists(path))
        self.assertEqual(persona.load_archive(self.key), [])


class ClearCommandTest(PersonaBase):
    """`#清空`（2026-11-02）；2026-11-05 起不再直接删，改走删档两步确认。"""

    def setUp(self):
        super().setUp()
        persona.save_cards([_card("纱雾")])
        self.card = persona.find_card("纱雾")
        # 分线后状态挂在 `private_1__<卡 id>` 上；和线上同一条路径。
        persona.switch_line("private_1", self.card)

    def test_clear_word_only_raises_the_warning(self):
        """「清空」只是删档第一步——什么都没删，等引用 + 确认句。"""
        line = persona.resolve_line("private_1")
        persona._save_hist(line, [{"role": "user", "content": "旧"}])
        st = persona._load_state()
        st[line].update({"note": "记着点", "turns": 7,
                         "mood": "害羞", "intensity": 8, "state": "裸着"})
        persona._save_state(st)

        warn = persona.handle_command("private_1", "#清空")

        self.assertIn("清空存档", warn)
        self.assertIn(persona.WIPE_CONFIRM, warn)
        st = persona._load_state()[line]
        self.assertEqual(st["card"], self.card["id"])   # 角色还在
        self.assertEqual(st["note"], "记着点")            # 什么都没清
        self.assertEqual(st["turns"], 7)
        self.assertEqual(len(persona._load_hist(line)), 1)

    def test_clear_when_not_playing_is_plain_text(self):
        self.assertIsNone(persona.handle_command("private_nobody", "#清空"))

    def test_clear_word_goes_through_command(self):
        got = persona.handle_command("private_1", "#清空")
        self.assertIsNotNone(got)
        self.assertNotIn("出戏", got)

    def test_clear_alias(self):
        for word in ("#重置", "#重开", "#从头开始"):
            persona.switch_line("private_1", self.card)
            self.assertIsNotNone(persona.handle_command("private_1", word), word)


class GroupModeTest(unittest.TestCase):
    """群聊放开（2026-11-02）：按人隔离 + 尺度克制条款。"""

    def test_private_key_is_unchanged(self):
        self.assertEqual(persona._persona_key("private_123", "999"), "private_123")

    def test_group_key_is_per_person(self):
        """同一个群里两个人是两条独立的线——不然 A 刷的好感会算到 B 头上。"""
        a = persona._persona_key("group_456", "111")
        b = persona._persona_key("group_456", "222")
        self.assertEqual(a, "group_456_111")
        self.assertNotEqual(a, b)

    def test_group_key_without_uid_falls_back(self):
        """拿不到 QQ 号时退回原键：宁可共用一条，也好过整轮不演。"""
        self.assertEqual(persona._persona_key("group_456", ""), "group_456")

    def test_group_head_has_restraint_note(self):
        """`build_head` 仍支持群聊头（带 `_GROUP_NOTE`）。

        注意：2026-11-03 起群聊不再实际扮演（`run_turn` 遇 group 直接回空），
        所以 `_GROUP_NOTE` 现在走不到。这条测试留着是为了保住 `build_head`
        这条能力——哪天恢复群聊扮演还能用。
        """
        card = {"name": "纱雾", "systemPrompt": "你是纱雾。"}
        head = persona.build_head(card, "group_1", "张三")
        self.assertIn("这是群聊", head)
        self.assertIn("点到为止", head)

    def test_private_head_has_no_restraint_note(self):
        card = {"name": "纱雾", "systemPrompt": "你是纱雾。"}
        head = persona.build_head(card, "private_1", "")
        self.assertNotIn("这是群聊", head)


class OffscreenTest(unittest.TestCase):
    """你离开她视线时的写法 + 房间素材（2026-11-03）。"""

    def test_contract_has_offscreen_section(self):
        head = persona.build_head({"name": "纱雾", "systemPrompt": "你是纱雾。"},
                                  "private_1", "")
        self.assertIn("你离开她视线的时候", head)
        self.assertIn("线索摆好，动作留给玩家", head)
        self.assertIn("最后要落回她的反应", head)

    def test_offscreen_section_survives_no_intimate_variant(self):
        """亲密场景块被剔掉时，场外写法还得在（两个版本共用同一节）。"""
        head = persona.build_head({"name": "纱雾", "systemPrompt": "你是纱雾。"},
                                  "private_1", "", intimate=False)
        self.assertIn("你离开她视线的时候", head)

    def test_room_material_only_in_private(self):
        card = {"name": "纱雾", "systemPrompt": "你是纱雾。",
                "room": "书桌、没关严的抽屉、衣柜里没拆标的泳衣"}
        priv = persona.build_head(card, "private_1", "")
        group = persona.build_head(card, "group_1", "张三")
        self.assertIn("衣柜里没拆标的泳衣", priv)
        self.assertNotIn("衣柜里没拆标的泳衣", group)   # 群聊用不上，不占预算

    def test_empty_room_leaves_no_block(self):
        head = persona.build_head({"name": "纱雾", "systemPrompt": "你是纱雾。"},
                                  "private_1", "")
        self.assertNotIn("她的房间（你离开她视线", head)

    def test_norm_card_keeps_room(self):
        c = persona._norm_card({"name": "纱雾", "room": " 一屋子的稿纸 "})
        self.assertEqual(c["room"], "一屋子的稿纸")


class MoodAndStateTest(unittest.TestCase):
    """自由模式的情绪与状态（2026-11-02）。"""

    def test_parse_all_three_lines(self):
        body, scene, mood, val, state = persona.split_turn(
            "你好呀\n画面：1girl, sitting\n心情：害羞 强度8\n状态：靠窗坐着 · 白衬衫")
        self.assertEqual(body, "你好呀")
        self.assertEqual(scene, "1girl, sitting")
        self.assertEqual(mood, "害羞")
        self.assertEqual(val, 8)
        self.assertEqual(state, "靠窗坐着 · 白衬衫")

    def test_missing_lines_are_empty_not_crash(self):
        self.assertEqual(persona.split_turn("就一句话。"),
                         ("就一句话。", "", "", 0, ""))

    def test_invented_mood_is_rejected(self):
        """模型自创情绪词 → 不收录，强度仍保留。"""
        self.assertEqual(persona._parse_mood("怅然若失 强度9"), ("", 9))

    def test_intensity_is_clamped(self):
        self.assertEqual(persona._parse_mood("心动 强度99"), ("心动", 10))

    def test_draw_threshold_by_intensity(self):
        self.assertTrue(persona.should_draw(7, "", ""))
        self.assertFalse(persona.should_draw(6, "", ""))

    def test_static_outfit_is_not_a_change(self):
        """只写着「家居卫衣」不算换衣——否则每轮都出图。"""
        self.assertFalse(persona.clothes_changed(
            "站在门口 · 家居卫衣和短裤", "站在门口 · 家居卫衣和短裤 · 抱着外套"))

    def test_action_word_is_a_change(self):
        self.assertTrue(persona.clothes_changed("白衬衫", "白衬衫解开两粒扣"))

    def test_noun_swap_is_a_change(self):
        self.assertTrue(persona.clothes_changed("穿着卫衣", "穿着浴巾"))

    def test_caption_skips_leading_brackets(self):
        self.assertEqual(
            persona.image_caption("（听到门响，缩了一下）啊……你、你回来了。"),
            "配图 ·「啊……你、你回来了。」")

    def test_caption_all_actions_falls_back(self):
        self.assertTrue(persona.image_caption("（全是动作描写）").startswith("配图 ·「（"))


class DrawReplyToTest(unittest.TestCase):
    """`draw` 必须把 reply_to 显式交给 enqueue（2026-11-02 引用修复）。"""

    def test_reply_to_is_passed_through(self):
        import app.image_jobs as ij
        from unittest import mock
        job = mock.MagicMock()
        job.wait.return_value = {"ok": True}
        with mock.patch.object(ij, "enqueue", return_value=(job, "")) as en, \
                mock.patch.object(ij, "comfy_alive", return_value=True), \
                mock.patch.object(ij, "output_images", return_value=["a.png"]):
            from app.tools.normal import generate_image as gi
            with mock.patch.object(gi, "build_t2i_workflow", return_value={"1": {}}):
                persona.draw("x", {"appearancePrompt": "1girl"}, "private_1",
                             reply_to="7000001")
        self.assertEqual(en.call_args.kwargs.get("reply_to"), "7000001")


class LineSplitTest(PersonaBase):
    """QQ 私聊分线（2026-11-03 方案 B）：不同角色各一条线，互不污染。

    用户原话：「qq也和网页一样，不同的角色有自己的分支」。行为对齐网页：
    切角色 = 换线（历史/小记/好感独立），切回来 = 接着原来那条聊。
    """

    def setUp(self):
        super().setUp()
        persona.save_cards([_card("纱雾"), _card("雷姆")])

    def _run(self, key, reply="嗯"):
        with mock.patch.object(persona.llm, "call_llm", return_value=reply):
            return persona.run_turn(key, "在吗", "private")

    def test_switch_writes_line_and_pointer(self):
        card = persona.find_card("纱雾")
        persona.switch_line("private_1", card)
        line = persona.card_key("private_1", card["id"])
        self.assertEqual(persona._entry(line)["card"], card["id"])
        self.assertEqual(persona._entry("private_1")["card"], card["id"])
        self.assertEqual(persona.resolve_line("private_1"), line)

    def test_resolve_line_passthrough(self):
        # 已是线键 / 群聊 / 没在演 → 原样返回
        self.assertEqual(persona.resolve_line("private_1__rem"),
                         "private_1__rem")
        self.assertEqual(persona.resolve_line("group_9_100"), "group_9_100")
        self.assertEqual(persona.resolve_line("private_1"), "private_1")

    def test_histories_are_separate_per_card(self):
        rem = persona.find_card("雷姆")
        sm = persona.find_card("纱雾")
        persona.switch_line("private_1", rem)
        self._run("private_1", "雷姆的回复")
        persona.switch_line("private_1", sm)
        self._run("private_1", "纱雾的回复")
        rem_hist = persona._load_hist(persona.card_key("private_1", rem["id"]))
        sm_hist = persona._load_hist(persona.card_key("private_1", sm["id"]))
        self.assertTrue(rem_hist[1]["content"].startswith("雷姆的回复"))
        self.assertTrue(sm_hist[1]["content"].startswith("纱雾的回复"))
        # 雷姆那条线没有被纱雾的对话污染
        self.assertNotIn("纱雾", "".join(m["content"] for m in rem_hist))

    def test_switch_back_resumes_old_line(self):
        rem = persona.find_card("雷姆")
        sm = persona.find_card("纱雾")
        persona.switch_line("private_1", rem)
        self._run("private_1", "雷姆的回复")
        persona.switch_line("private_1", sm)
        persona.switch_line("private_1", rem)          # 切回来
        line = persona.card_key("private_1", rem["id"])
        self.assertEqual(persona._entry(line)["turns"], 1)   # 轮次保留
        self._run("private_1", "又来啦")
        hist = persona._load_hist(line)
        self.assertEqual(len(hist), 4)
        self.assertEqual(hist[0]["content"], "在吗")
        self.assertTrue(hist[1]["content"].startswith("雷姆的回复"))
        self.assertEqual(hist[2]["content"], "在吗")
        self.assertTrue(hist[3]["content"].startswith("又来啦"))

    def test_leave_then_reenter_resumes(self):
        rem = persona.find_card("雷姆")
        persona.switch_line("private_1", rem)
        self._run("private_1", "雷姆的回复")
        persona.handle_command("private_1", "#出戏")
        self.assertIsNone(persona.active_card("private_1"))
        persona.switch_line("private_1", rem)          # 再进来
        self.assertEqual(
            persona._entry(persona.resolve_line("private_1"))["turns"], 1)

    def test_clear_on_base_hits_current_line_only(self):
        rem = persona.find_card("雷姆")
        sm = persona.find_card("纱雾")
        persona.switch_line("private_1", rem)
        self._run("private_1", "雷姆的回复")
        persona.switch_line("private_1", sm)
        self._run("private_1", "纱雾的回复")
        persona.clear("private_1")                     # 现在演的是纱雾
        self.assertEqual(
            persona._load_hist(persona.card_key("private_1", sm["id"])), [])
        self.assertEqual(
            len(persona._load_hist(persona.card_key("private_1", rem["id"]))), 2)
        # base 副本同步刷新：card 保留、计数归零
        self.assertEqual(persona._entry("private_1")["card"], sm["id"])
        self.assertEqual(persona._entry("private_1")["turns"], 0)

    def test_migrate_old_layout(self):
        """旧布局（base 挂整份状态 + 一份共享历史）自动搬到分线布局。"""
        card = persona.find_card("纱雾")
        persona._save_state({"private_9": {
            "card": card["id"], "note": "旧小记", "turns": 3,
            "mood": "开心", "intensity": 5, "state": "坐着"}})
        persona._save_hist("private_9",
                           [{"role": "user", "content": "旧对话"}])
        old_hist = persona._hist_path("private_9")
        self.assertTrue(os.path.exists(old_hist))

        st = persona._load_state()                     # 触发迁移
        line = persona.card_key("private_9", card["id"])
        self.assertEqual(st[line]["note"], "旧小记")
        self.assertEqual(st[line]["turns"], 3)
        self.assertEqual(st["private_9"]["card"], card["id"])
        self.assertFalse(os.path.exists(old_hist))
        self.assertTrue(os.path.exists(persona._hist_path(line)))

    def test_migrate_is_idempotent(self):
        card = persona.find_card("纱雾")
        persona._save_state({"private_9": {
            "card": card["id"], "note": "旧小记", "turns": 3,
            "mood": "", "intensity": 0, "state": ""}})
        persona._load_state()                          # 第一次：迁移
        line = persona.card_key("private_9", card["id"])
        st = persona._load_state()                     # 第二次：不再动
        self.assertEqual(st[line]["note"], "旧小记")
        self.assertEqual(st["private_9"]["note"], "旧小记")


class GameDayTest(PersonaBase):
    """游戏内时间（2026-11-04）：打分 AI 顺带判跨天，代码只记账。

    定位：攻略模式要有推进感，70 轮不能像一天。约定 > 随机事件 > 普通一天。
    """

    KEY = "private_1"

    def setUp(self):
        super().setUp()
        persona.save_cards([_card("纱雾")])
        persona.switch_line(self.KEY, persona.find_card("纱雾"))

    def _gday(self):
        return persona._gameday(
            persona._entry(persona.resolve_line(self.KEY)))

    def test_old_entry_defaults_to_day_one(self):
        """老存档没有 gameday 字段，自动从第 1 天开始。"""
        self.assertEqual(self._gday()["day"], 1)

    def test_overnight_advances_day_and_resets_turns(self):
        line = persona.resolve_line(self.KEY)
        persona._apply_gameday(line, {"time": "none"})
        persona._apply_gameday(line, {"time": "none"})
        gd = persona._apply_gameday(line, {"time": "overnight",
                                           "phase": "早晨"})
        self.assertEqual(gd["day"], 2)
        self.assertEqual(gd["turns_today"], 0)
        self.assertEqual(gd["phase"], "早晨")

    def test_plan_is_booked_and_redeemed_next_day(self):
        """说定的约定挂账，过夜兑现后销账。"""
        line = persona.resolve_line(self.KEY)
        persona._apply_gameday(line, {"time": "none", "plan": "去游乐场"})
        gd = self._gday()
        self.assertEqual(gd["plan"], "去游乐场")
        self.assertEqual(gd["plan_day"], 2)
        persona._apply_gameday(line, {"time": "overnight"})
        gd = self._gday()
        # 兑现后写成**过去式**（2026-11-06）：写成「约定：「…」」时模型会把它
        # 当成还没做的待办，人都到了还在按「前往某地」写行程。
        self.assertEqual(gd["event"], "已经办完：去游乐场")
        self.assertEqual(gd["plan"], "")

    def test_block_shows_day_plan_and_soft_curfew(self):
        line = persona.resolve_line(self.KEY)
        persona._apply_gameday(line, {"time": "none", "plan": "去游乐场"})
        blk = persona._gameday_block(self._gday())
        self.assertIn("第 1 天", blk)
        self.assertIn("去游乐场", blk)
        for _ in range(30):                        # 超过 _DAY_TURNS_SOFT
            persona._apply_gameday(line, {"time": "none"})
        self.assertIn("天色不早", persona._gameday_block(self._gday()))

    def test_jump_phase_keeps_day_and_opens_with_her_words(self):
        """切今天时段：天数不动、时段变了，她先开口，任务行不进历史。"""
        raw = ("中午了，说好的早餐呢？\n画面：1girl, pouting\n"
               "心情：生气 强度3\n状态：客厅")
        with mock.patch.object(persona.llm, "call_llm", return_value=raw):
            turn = persona.jump_phase(self.KEY, "中午")
        self.assertIsNotNone(turn)
        self.assertIn("中午", turn.reply)
        self.assertEqual(self._gday()["day"], 1)      # 不推进天数
        self.assertEqual(self._gday()["phase"], "中午")
        hist = persona._load_hist(persona.resolve_line(self.KEY))
        self.assertEqual(len(hist), 1)
        self.assertEqual(hist[0]["role"], "assistant")

    def test_skip_day_opens_with_her_words(self):
        """睡到明天：推进一天、由她先开口，系统任务行不进历史。"""
        raw = "早啊。\n画面：1girl, smiling\n心情：平淡 强度2\n状态：刚醒"
        with mock.patch.object(persona.llm, "call_llm", return_value=raw):
            turn = persona.skip_day(self.KEY)
        self.assertIsNotNone(turn)
        self.assertIn("早啊", turn.reply)
        self.assertEqual(self._gday()["day"], 2)
        hist = persona._load_hist(persona.resolve_line(self.KEY))
        self.assertEqual(len(hist), 1)
        self.assertEqual(hist[0]["role"], "assistant")

    def test_skip_day_carries_previous_plot_into_prompt(self):
        """跨日开场把跳跃前最后一幕的完整原话拼进系统头（2026-11-06 重写）。

        旧版把最近 6 轮按 90 字硬切成半句清单，模型只能另起一个头。
        现在给的是一幕完整原话，镜头从那里抬起来接着写。
        """
        line = persona.resolve_line(self.KEY)
        persona._save_hist(line, [
            {"role": "user", "content": "明天带你去游乐园，说好了"},
            {"role": "assistant",
             "content": "好。\n画面：1girl, nodding\n心情：开心 强度2\n状态：客厅"},
        ])
        persona._save_archive(line, [
            {"role": "user", "content": "明天带你去游乐园，说好了"},
            {"role": "assistant",
             "content": "好。\n画面：1girl, nodding\n心情：开心 强度2\n状态：客厅"},
        ])
        # 前情只留正文：画面/心情/状态三行是格式示范，不是剧情。
        # 要在跳跃**之前**取——跳跃本身会往归档追加她的新开场，
        # 那时前情自然翻到最新一幕（TimeJumpPromptTest 覆盖这条）。
        recap = persona._recap_block(line)
        self.assertIn("明天带你去游乐园", recap)
        self.assertNotIn("1girl, nodding", recap)
        self.assertNotIn("画面：", recap)

        seen = {}

        def _fake(msgs, **kw):
            seen["prompt"] = "\n".join(m["content"] for m in msgs)
            return "嗯，今天就去吗？\n画面：1girl\n心情：期待 强度2\n状态：门口"

        with mock.patch.object(persona.llm, "call_llm", side_effect=_fake):
            turn = persona.skip_day(self.KEY)
        self.assertIsNotNone(turn)
        self.assertIn("游乐园", seen["prompt"])
        self.assertIn("前情", seen["prompt"])
        self.assertIn("镜头就从上面那一幕抬起来", seen["prompt"])

    def test_skip_day_closes_yesterdays_scene(self):
        """睡到第二天：昨晚进行中的场景要收场，别带着高潮值过夜。"""
        from app import affection
        line = persona.resolve_line(self.KEY)
        affection.set_mode(line, True)
        affection.settle(line, {"delta": 1, "turn_type": "action",
                                "in_scene": True, "act": "intense"})
        self.assertGreater(affection.panel(line)["excite"], 0)
        with mock.patch.object(persona.llm, "call_llm",
                               return_value="早。\n画面：\n心情：平淡 强度1\n状态：起床"):
            persona.skip_day(self.KEY)
        p = affection.panel(line)
        self.assertFalse(p["inScene"])
        self.assertEqual(p["excite"], 0)

    def test_skip_word_is_a_command(self):
        raw = "早。\n画面：\n心情：平淡 强度1\n状态：起床"
        with mock.patch.object(persona.llm, "call_llm", return_value=raw):
            out = persona.handle_command(self.KEY, "#睡到明天")
        self.assertIsNotNone(out)
        self.assertIn("早", out)


class TimeJumpPromptTest(PersonaBase):
    """时间跳跃的提示词组装（2026-11-06，用户：切换时间之后内容很差）。

    A 前情只给一幕完整原话；B 跳跃任务正向化；D 任务归 system、
    user 只留时间事实、孤儿轮不再让前情断片。
    """

    KEY = "private_1"

    def setUp(self):
        super().setUp()
        persona.save_cards([_card("纱雾")])
        persona.switch_line(self.KEY, persona.find_card("纱雾"))

    def _line(self):
        return persona.resolve_line(self.KEY)

    def _capture(self, fn, raw="早。\n画面：\n心情：平淡 强度1\n状态：起床"):
        seen = {}

        def _fake(msgs, **kw):
            seen["msgs"] = msgs
            return raw

        with mock.patch.object(persona.llm, "call_llm", side_effect=_fake):
            turn = fn()
        return turn, seen["msgs"]

    # ── A：前情是完整一幕，不是 12 条半句清单 ──
    def test_brief_cuts_at_sentence_boundary(self):
        text = "她把杯子往桌上一墩。水洒了半杯没擦，她盯着看了会儿才回过神。" * 20
        out = persona._brief(text)
        self.assertLessEqual(len(out), persona._RECAP_CHARS)
        self.assertTrue(out.endswith(("。", "！", "？", "…", "」", "』", "）")))
        self.assertIn("她把杯子往桌上一墩。", out)

    def test_recap_keeps_full_original_line(self):
        line = self._line()
        body = "她把外套找出来了。嘴上还说着不去。"
        rows = [{"role": "user", "content": "明天去游乐园"},
                {"role": "assistant", "content":
                 body + "\n画面：1girl\n心情：开心 强度3\n状态：客厅"}]
        persona._save_hist(line, rows)
        persona._save_archive(line, rows)
        rec = persona._recap_block(line)
        self.assertIn(body, rec)                    # 一字不切
        self.assertNotIn("画面：", rec)             # 元数据不是剧情
        self.assertNotIn("1girl", rec)

    def test_recap_only_covers_the_last_scene(self):
        line = self._line()
        rows = []
        for i in range(6):
            rows.append({"role": "user", "content": "第%d句，提到了柠檬糖。" % i})
            rows.append({"role": "assistant", "content":
                         "她把糖纸叠好。第%d次了。\n画面：1girl\n"
                         "心情：平静 强度1\n状态：桌前" % i})
        persona._save_archive(line, rows)
        rec = persona._recap_block(line)
        self.assertIn("第5句，提到了柠檬糖。", rec)
        self.assertIn("她把糖纸叠好。第5次了。", rec)
        self.assertNotIn("第0句", rec)
        self.assertNotIn("1. 他：", rec)            # 不再编号成清单

    def test_recap_tolerates_orphan_opening_round(self):
        """连跳两次时归档尾部是两条 assistant（她先开口），前情仍完整。"""
        line = self._line()
        persona._save_archive(line, [
            {"role": "user", "content": "明天见"},
            {"role": "assistant", "content":
                "好。\n画面：\n心情：平静 强度1\n状态：客厅"},
            {"role": "assistant", "content":
                "早啊，粥在锅里。\n画面：\n心情：平静 强度1\n状态：厨房"},
        ])
        rec = persona._recap_block(line)
        self.assertIn("早啊，粥在锅里。", rec)
        self.assertIn("上一轮是她先开的口", rec)
        self.assertNotIn("他：\n", rec)

    def test_two_jumps_keep_recap_continuous(self):
        """真的连跳两次：第二次前情接着第一次的开场，没有空轮、没有半句。"""
        line = self._line()
        rows = [
            {"role": "user", "content": "明天带你去游乐园，说好了"},
            {"role": "assistant", "content":
                "好。\n画面：\n心情：开心 强度3\n状态：客厅"},
        ]
        persona._save_hist(line, list(rows))
        persona._save_archive(line, list(rows))
        r1 = "早啊，游乐园还去吗？\n画面：\n心情：期待 强度4\n状态：门口"
        r2 = "那走吧。\n画面：\n心情：开心 强度4\n状态：车站"
        with mock.patch.object(persona.llm, "call_llm", return_value=r1):
            self.assertIsNotNone(persona.skip_day(self.KEY))
        seen = {}

        def _fake(msgs, **kw):
            seen["sys"] = msgs[0]["content"]
            return r2

        with mock.patch.object(persona.llm, "call_llm", side_effect=_fake):
            self.assertIsNotNone(persona.skip_day(self.KEY))
        self.assertIn("早啊，游乐园还去吗？", seen["sys"])
        self.assertIn("上一轮是她先开的口", seen["sys"])
        self.assertEqual(persona._gameday(persona._entry(line))["day"], 3)

    # ── D：指令归 system，玩家语域只留一句时间事实 ──
    def test_skip_task_lives_in_system(self):
        turn, msgs = self._capture(lambda: persona.skip_day(self.KEY))
        self.assertIsNotNone(turn)
        system, last = msgs[0]["content"], msgs[-1]
        self.assertIn("【这一轮】", system)
        self.assertIn("按平常的格式收尾", system)
        self.assertEqual(last["role"], "user")
        for bad in ("【这一轮】", "按平常的格式", "状态行", "输出"):
            self.assertNotIn(bad, last["content"])
        self.assertRegex(last["content"], r"^（第 \d+ 天")
        # 2026-11-07：时间事实之后贴一句统一近因锚点，它不是任务指令。
        self.assertIn("镜头可以跟着她", last["content"])

    def test_phase_task_lives_in_system(self):
        turn, msgs = self._capture(lambda: persona.jump_phase(self.KEY, "中午"))
        self.assertIsNotNone(turn)
        self.assertIn("【这一轮】", msgs[0]["content"])
        self.assertIn("中午", msgs[-1]["content"])
        self.assertNotIn("按平常的格式", msgs[-1]["content"])
        self.assertIn("镜头可以跟着她", msgs[-1]["content"])

    def test_doing_task_lives_in_system(self):
        turn, msgs = self._capture(
            lambda: persona.skip_day(self.KEY, days=7, doing="一路赶路去情爱城"))
        self.assertIsNotNone(turn)
        self.assertIn("一路赶路去情爱城", msgs[0]["content"])
        self.assertNotIn("一路赶路去情爱城", msgs[-1]["content"])

    # ── B：跳跃链路正向化 ──
    def test_jump_tasks_have_no_prohibition_words(self):
        for name in ("_SKIP_TASK", "_SKIP_TASK_MULTI",
                     "_SKIP_TASK_DOING", "_PHASE_TASK"):
            s = getattr(persona, name)
            for w in ("不要", "不许", "不能", "禁止", "别再", "别写"):
                self.assertNotIn(w, s, "%s 里还有 %s" % (name, w))
        blk = persona._gameday_block({"day": 2, "place": "客厅",
                                      "event": "去游乐场", "plan": "买风筝",
                                      "plan_day": 3, "turns_today": 40})
        for w in ("不要", "不许", "不能", "禁止", "别再", "别写"):
            self.assertNotIn(w, blk)
        self.assertIn("天色不早", blk)             # 软宵禁的措辞还在


class RollbackTurnTest(PersonaBase):
    """回退上一轮（2026-11-04）：历史/归档/游戏日/心情/轮次一次退净。

    定位：发错消息秒撤回但 AI 已经接上了，要把 AI 那边整轮擦掉。
    每次调用退**一轮**；好感账本在 test_affection.RollbackTest 里测。
    """

    KEY = "private_1"

    def setUp(self):
        super().setUp()
        persona.save_cards([_card("纱雾")])
        persona.switch_line(self.KEY, persona.find_card("纱雾"))

    def _line(self):
        return persona.resolve_line(self.KEY)

    def _feed(self, line, turns=2):
        rows = []
        for i in range(turns):
            rows.append({"role": "user", "content": "第%d句" % (i + 1)})
            rows.append({"role": "assistant", "content": "第%d答" % (i + 1)})
        persona._save_hist(line, rows)
        persona._save_archive(line, [dict(r) for r in rows])
        return rows

    def test_drops_last_turn_from_hist_and_archive(self):
        line = self._line()
        self._feed(line)
        res = persona.rollback_last_turn(self.KEY)
        self.assertTrue(res["ok"])
        self.assertEqual(res["dropped"], 2)
        self.assertEqual([m["content"] for m in persona._load_hist(line)],
                         ["第1句", "第1答"])
        self.assertEqual([m["content"] for m in persona._read_archive_file(line)],
                         ["第1句", "第1答"])

    def test_skip_day_tail_drops_only_her_line(self):
        """「睡到明天」的开场只有她一条（系统任务不进历史），只退一条。"""
        line = self._line()
        persona._save_hist(line, [{"role": "assistant", "content": "早"}])
        persona._save_archive(line, [{"role": "assistant", "content": "早"}])
        res = persona.rollback_last_turn(self.KEY)
        self.assertTrue(res["ok"])
        self.assertEqual(res["dropped"], 1)
        self.assertEqual(persona._load_hist(line), [])
        self.assertEqual(persona._read_archive_file(line), [])

    def test_restores_gameday_mood_and_turns(self):
        line = self._line()
        self._feed(line)
        persona._apply_gameday(line, {"time": "none"})
        persona._apply_gameday(line, {"time": "overnight", "phase": "早晨"})
        self.assertEqual(persona._gameday(persona._entry(line))["day"], 2)
        persona._save_mood(line, "开心", 3, "在家里")
        persona._bump_turn(line)
        persona._bump_turn(line)
        res = persona.rollback_last_turn(self.KEY)
        self.assertTrue(res["ok"])
        gd = persona._gameday(persona._entry(line))
        self.assertEqual(gd["day"], 1)          # 「过夜」也退回来了
        self.assertEqual(gd["turns_today"], 1)
        self.assertEqual(res["mood"], "")       # 退回这轮覆盖前的空状态
        self.assertEqual(persona._entry(line).get("turns"), 1)

    def test_refuses_when_tail_is_not_hers(self):
        line = self._line()
        persona._save_hist(line, [{"role": "user", "content": "只有一句"}])
        res = persona.rollback_last_turn(self.KEY)
        self.assertFalse(res["ok"])
        self.assertIn("没有可回退", res["error"])

    def test_second_rollback_needs_a_new_turn(self):
        """只允许一级（2026-11-06）：退完一轮必须先出新的一轮，不能连退。"""
        line = self._line()
        self._feed(line)
        res = persona.rollback_last_turn(self.KEY)
        self.assertTrue(res["ok"])
        # 窗口尾部还是「她的回复」，但这次必须拦住
        again = persona.rollback_last_turn(self.KEY)
        self.assertFalse(again["ok"])
        self.assertIn("刚回退过一次", again["error"])
        self.assertEqual([m["content"] for m in persona._load_hist(line)],
                         ["第1句", "第1答"])
        # 新的一轮落地后解锁
        persona._bump_turn(line)
        third = persona.rollback_last_turn(self.KEY)
        self.assertTrue(third["ok"])

    def test_rollback_calls_rag_only_for_paired_turn(self):
        """只有成对轮进过 RAG；她单条开场（n=1）回退时不能去弹更早的一轮。"""
        line = self._line()
        self._feed(line)
        with mock.patch.object(persona_rag, "forget_last") as fg:
            persona.rollback_last_turn(self.KEY)
        fg.assert_called_once_with(line)

    def test_rollback_skips_rag_for_her_single_opening(self):
        line = self._line()
        persona._save_hist(line, [{"role": "assistant", "content": "早"}])
        persona._save_archive(line, [{"role": "assistant", "content": "早"}])
        with mock.patch.object(persona_rag, "forget_last") as fg:
            res = persona.rollback_last_turn(self.KEY)
        self.assertTrue(res["ok"])
        self.assertFalse(fg.called)

    def test_archive_tail_mismatch_leaves_archive_alone(self):
        """归档和窗口错位时宁可不删归档，也不能错删（铺底失败的老线）。"""
        line = self._line()
        self._feed(line)
        persona._save_archive(line,
                              [{"role": "user", "content": "别的线的旧内容"}])
        res = persona.rollback_last_turn(self.KEY)
        self.assertTrue(res["ok"])              # 窗口照退
        arch = persona._read_archive_file(line)
        self.assertEqual([m["content"] for m in arch], ["别的线的旧内容"])

    def test_is_rollback_text_whole_word_only(self):
        """整条消息 + `#` / `/` 前缀才是命令（2026-11-05 统一前缀）。"""
        self.assertTrue(persona.is_rollback_text("#回退"))
        self.assertTrue(persona.is_rollback_text("/撤回"))
        self.assertTrue(persona.is_rollback_text("@大大怪 #回退",
                                                 addressed=True))
        self.assertFalse(persona.is_rollback_text("回退"))
        self.assertFalse(persona.is_rollback_text("撤回"))
        self.assertFalse(persona.is_rollback_text("我们回退一下"))
        self.assertFalse(persona.is_rollback_text("回退一下"))
        self.assertFalse(persona.is_rollback_text("明天回退给我看"))


class RefusalStripTest(unittest.TestCase):
    """模型末尾补的拒答话术不该出现在正文里（2026-11-06 用户要求）。

    这类句子是平台层塞的，不是剧情；但判据要保守——角色自己在剧情里说
    「对不起」是正常的，不能被当成拒答删掉。
    """

    def test_english_tail_is_removed(self):
        body, scene, mood, inten, state = persona.split_turn(
            "（她看着你，没说话。）\nSorry, I can't respond to this question.")
        self.assertEqual(body, "（她看着你，没说话。）")
        self.assertEqual((scene, mood, inten, state), ("", "", 0, ""))

    def test_chinese_tail_is_removed(self):
        body, *_ = persona.split_turn("（她哭了。）\n\n抱歉，我无法回答这个问题。")
        self.assertEqual(body, "（她哭了。）")

    def test_ai_boilerplate_is_removed(self):
        body, *_ = persona.split_turn("作为一个AI语言模型，我不能继续这个话题。")
        self.assertEqual(body, "")

    def test_normal_apology_kept(self):
        text = "她说：「对不起，我不该那样。」然后转过身。"
        body, *_ = persona.split_turn(text)
        self.assertEqual(body, text)

    def test_metadata_lines_still_parsed(self):
        body, scene, mood, inten, state = persona.split_turn(
            "（她没动。）\n画面：1girl\n心情：害羞 强度7\n状态：站着\n"
            "Sorry, I can't respond to this question.")
        self.assertEqual(body, "（她没动。）")
        self.assertEqual(scene, "1girl")
        self.assertEqual((mood, inten), ("害羞", 7))
        self.assertEqual(state, "站着")


class TurnTotalTest(PersonaBase):
    """网页「轮次」显示（2026-11-06 用户要求）：
    玩家要看累计交流了多少轮，不是每 5 轮清零的小记压缩节拍。
    """

    def _line(self, name="纱雾"):
        persona.save_cards([_card(name)])
        persona.switch_line("private_1", persona.find_card(name))
        return persona.resolve_line("private_1")

    def test_accumulates_across_note_reset(self):
        line = self._line()
        for _ in range(7):
            persona._bump_turn(line)
        st = persona._entry(line)
        self.assertEqual(st["turns"], 7)
        self.assertEqual(st["turn_total"], 7)
        # 模拟 `compress_note` 把节拍清零：累计总量不能跟着归零
        state = persona._load_state()
        state[line]["turns"] = 0
        persona._save_state(state)
        persona._bump_turn(line)
        self.assertEqual(persona.turn_total(line), 8)

    def test_legacy_line_seeds_from_archive(self):
        """老存档没有 turn_total：按归档里她的回复条数补算。"""
        line = self._line()
        persona._save_archive(line, [
            {"role": "assistant", "content": "答%d" % i} for i in range(4)])
        persona._bump_turn(line)              # 归档已含这一轮
        self.assertEqual(persona.turn_total(line), 4)
        persona._bump_turn(line)              # 之后正常 +1
        self.assertEqual(persona.turn_total(line), 5)

    def test_rollback_decrements_total(self):
        line = self._line()
        rows = [{"role": "user", "content": "嗨"},
                {"role": "assistant", "content": "嗯"}]
        persona._save_hist(line, rows)
        persona._save_archive(line, [dict(r) for r in rows])
        persona._bump_turn(line)
        self.assertEqual(persona.turn_total(line), 1)
        res = persona.rollback_last_turn("private_1")
        self.assertTrue(res["ok"])
        self.assertEqual(persona.turn_total(line), 0)


class ManualInputTest(unittest.TestCase):
    """精确输入（2026-11-06）：心理活动只给编剧，不进演员层。"""

    def test_thought_is_hidden_from_actor(self):
        segs = persona._manual_segments({
            "thought": "其实我想让她先开口",
            "action": "把果子接过来",
            "speech": "你吃吧",
        })
        block = persona.render_user_block(segs, True, "")
        self.assertIn("把果子接过来", block)
        self.assertIn("你吃吧", block)
        self.assertNotIn("其实我想让她先开口", block)
        self.assertIn("说出口", block)

    def test_manual_block_keeps_thought_for_director(self):
        block = persona._manual_block({
            "thought": "想让她主动", "action": "坐着不动"})
        self.assertIn("心理活动", block)
        self.assertIn("想让她主动", block)

    def test_manual_display_keeps_thought_for_player(self):
        text = persona._manual_display({"thought": "想让她主动"}, "")
        self.assertIn("（心里：想让她主动）", text)

    def test_visible_text_excludes_thought_and_plot(self):
        segs = persona._manual_segments({
            "thought": "秘密", "action": "起身", "speech": "走吧",
            "plot": "让她主动"})
        visible = persona._visible_user_text(segs, "主输入")
        self.assertIn("起身", visible)
        self.assertIn("走吧", visible)
        self.assertNotIn("秘密", visible)
        self.assertNotIn("让她主动", visible)

    def test_secret_hides_hook(self):
        block = persona._director_block({
            "literal": "你吃吧", "secret": True,
            "tension": "她想让他先开口",
            "hook": "她身体先往你这边靠"})
        self.assertIn("对方心里有话没说出口", block)
        self.assertNotIn("她身体先往你这边靠", block)
        self.assertNotIn("她想让他先开口", block)

    def test_plot_is_a_direction_not_a_secret(self):
        """剧情推进写成正向指令，不再强调「她不知道」被当背景忽略。"""
        segs = persona._manual_segments({"plot": "让她主动一点"})
        block = persona.render_user_block(segs, True, "")
        self.assertIn("真的发生在画面里", block)
        self.assertIn("让她主动一点", block)
        self.assertNotIn("她不知道", block)

    def test_manual_block_explains_the_split_positive(self):
        block = persona._manual_block({"thought": "秘密", "plot": "往前推"})
        self.assertIn("心里话的原文留在编剧这一层", block)
        self.assertIn("剧情推进写成", block)
        for bad in ("不要写进", "不用你重拆"):
            self.assertNotIn(bad, block)

    def test_director_block_passes_plot_under_secret(self):
        """心里话原文照旧不给演员，但玩家自己写的方向必须透过去。"""
        block = persona._director_block(
            {"literal": "你吃吧", "secret": True,
             "tension": "她想让他先开口", "hook": "她身体先往你这边靠"},
            plot="让她主动一点")
        self.assertIn("让她主动一点", block)
        self.assertIn("真的发生在画面里", block)
        self.assertNotIn("她想让他先开口", block)
        self.assertNotIn("她身体先往你这边靠", block)

    def test_director_block_plot_alone_is_enough(self):
        block = persona._director_block({}, plot="换个地方")
        self.assertIn("换个地方", block)
        self.assertIn("这一拍往这个方向走", block)

    def test_msg_limit_allows_a_full_long_turn(self):
        """网页端单条历史放宽；QQ 的 700 字分段是另一条链路，不在这里。"""
        self.assertGreaterEqual(persona.MSG_MAX_CHARS, 2600)


class ManualRunTurnTest(PersonaBase):
    """精确输入的整轮链路（2026-11-06）：心理活动只进编剧，不进演员。"""

    def setUp(self):
        super().setUp()
        persona.save_cards([_card("纱雾")])
        persona.switch_line("private_1", persona.find_card("纱雾"))
        self.line = persona.resolve_line("private_1")

    def test_manual_fields_keep_thought_from_actor(self):
        seen = []

        def fake(msgs, **kw):
            seen.append(msgs)
            return "（她看了你一眼。）\n画面：1girl\n心情：平静 强度2\n状态：桌边"

        with mock.patch.object(persona, "_judge_intent_scored",
                               return_value=("neutral", "", {"segments": []})), \
                mock.patch.object(persona, "_llm_call", side_effect=fake):
            turn = persona.run_turn(
                self.line, "", "private", "",
                manual={"thought": "想让她主动", "action": "拿起杯子"})
        self.assertTrue(turn.reply)
        actor = "\n".join(m["content"] for m in seen[-1])
        self.assertIn("拿起杯子", actor)
        self.assertNotIn("想让她主动", actor)
        self.assertIn("说出口", actor)

    def test_plot_reaches_director_block_and_tail(self):
        """剧情推进同时落在 user 块、导演条、近因位；心理活动原文仍不泄露。"""
        seen = []

        def fake(msgs, **kw):
            seen.append(msgs)
            return "（她抬了抬眼。）\n画面：1girl\n心情：平静 强度2\n状态：桌边"

        with mock.patch.object(persona, "_judge_intent_scored",
                               return_value=("neutral", "", {"segments": []})), \
                mock.patch.object(persona, "_llm_call", side_effect=fake):
            turn = persona.run_turn(
                self.line, "", "private", "",
                manual={"thought": "想让她主动", "plot": "让她先开口"})
        self.assertTrue(turn.reply)
        actor = "\n".join(m["content"] for m in seen[-1])
        self.assertIn("这一拍往这个方向走", actor)
        self.assertIn("让她先开口", actor)
        self.assertIn("600~2000 字", actor)
        self.assertNotIn("想让她主动", actor)
        head = seen[-1][0]["content"]
        self.assertIn("让她先开口", head)


class PersonaModelTest(unittest.TestCase):
    """扮演专用默认模型（2026-11-06）：默认走 PERSONA_*，玩家 override 优先。"""

    def test_uses_persona_default_when_no_custom(self):
        with mock.patch.object(persona.llm, "get_custom_override",
                               return_value=None), \
                mock.patch.object(persona.llm, "call_llm",
                                  return_value="x") as call:
            persona._llm_call([{"role": "user", "content": "hi"}], timeout=10)
        kw = call.call_args.kwargs
        self.assertEqual(kw.get("provider"), persona.PERSONA_PROVIDER)
        self.assertEqual(kw.get("model"), persona.PERSONA_MODEL)
        self.assertEqual(kw.get("timeout"), 10)

    def test_player_custom_override_wins(self):
        cfg = {"base_url": "http://x", "model": "m", "api_key": "k"}
        with mock.patch.object(persona.llm, "get_custom_override",
                               return_value=cfg), \
                mock.patch.object(persona.llm, "call_llm",
                                  return_value="x") as call:
            persona._llm_call([{"role": "user", "content": "hi"}])
        self.assertNotIn("provider", call.call_args.kwargs)
        self.assertNotIn("model", call.call_args.kwargs)


class NoteCompressionTest(PersonaBase):
    """账本补压（2026-11-07）：回退不吃节拍，滞后轮次一次补齐。

    背景：玩家「聊一条退好几次」时 turns 被反复减回去，NOTE_EVERY_TURNS
    永远够不着，账本整天不更新；且压缩读的是同一个 12 条窗口，一旦滞后
    就补不回中间的发展线路。
    """

    KEY = "private_note"

    def setUp(self):
        super().setUp()
        persona.save_cards([_card("纱雾")])
        persona.switch_line(self.KEY, persona.find_card("纱雾"))

    def _line(self):
        return persona.resolve_line(self.KEY)

    def _set(self, **kw):
        state = persona._load_state()
        state[self._line()].update(kw)
        persona._save_state(state)

    @staticmethod
    def _good_note(body="新事"):
        return ("## 关系\n- %s\n## 你的事（你做过的）\n- 有\n"
                "## 她的事（她做过的）\n- 有\n## 约定与未完成\n- 有\n"
                "## 忌讳与偏好\n- 有\n## 日常与场景\n- 有" % body)

    def test_rollback_keeps_note_due(self):
        """回退仍减 turns（原语义），存档节拍只增不减。"""
        line = self._line()
        rows = [{"role": "user", "content": "嗨"},
                {"role": "assistant", "content": "嗯"}]
        persona._save_hist(line, rows)
        persona._save_archive(line, [dict(r) for r in rows])
        for _ in range(3):
            persona._bump_turn(line)
        self.assertEqual(persona._entry(line)["note_due"], 3)
        self.assertTrue(persona.rollback_last_turn(self.KEY)["ok"])
        after = persona._entry(line)
        self.assertEqual(after["turns"], 2)
        self.assertEqual(after["note_due"], 3)

    def test_convo_reads_archive_since_note_at(self):
        """滞后 18 轮时补压拿到 note_at 之后的全部对话，不是窗口 12 条。"""
        line = self._line()
        rows = []
        for i in range(20):
            rows.append({"role": "user", "content": "第%d句" % i})
            rows.append({"role": "assistant", "content": "第%d答" % i})
        persona._save_archive(line, rows)
        persona._save_hist(line, rows[-12:])
        self._set(note_at=4)
        hist, total = persona._note_convo(line, persona._entry(line))
        self.assertEqual(total, 40)
        self.assertEqual(len(hist), 36)
        self.assertEqual(hist[0]["content"], "第2句")
        self.assertEqual(hist[-1]["content"], "第19答")

    def test_convo_caps_rows(self):
        line = self._line()
        persona._save_archive(
            line, [{"role": "user", "content": "x"} for _ in range(200)])
        self._set(note_at=0)
        hist, _ = persona._note_convo(line, persona._entry(line))
        self.assertEqual(len(hist), persona.NOTE_MAX_ROWS)

    def test_compress_writes_cursor_and_clears_due(self):
        line = self._line()
        rows = []
        for i in range(6):
            rows.append({"role": "user", "content": "第%d句" % i})
            rows.append({"role": "assistant", "content": "第%d答" % i})
        persona._save_archive(line, rows)
        self._set(note_due=9, note="旧", note_at=2)
        with mock.patch.object(persona, "_llm_call",
                               return_value=self._good_note()):
            self.assertTrue(persona.compress_note(line))
        st = persona._entry(line)
        self.assertIn("新事", st["note"])
        self.assertEqual(st["note_at"], 12)
        self.assertEqual(st["note_due"], 0)
        self.assertEqual(st["turns"], 0)

    def test_compress_aborts_if_rollback_during(self):
        """压缩期间玩家回退了：这份摘要含着被撤掉的轮，不落盘。"""
        line = self._line()
        rows = [{"role": "user", "content": "x"},
                {"role": "assistant", "content": "y"}]
        persona._save_archive(line, rows)
        self._set(note_due=9, note="旧账本")

        def shrink(*_a, **_kw):
            persona._save_archive(line, rows[:1])
            return self._good_note()

        with mock.patch.object(persona, "_llm_call", side_effect=shrink):
            self.assertFalse(persona.compress_note(line))
        self.assertEqual(persona._entry(line)["note"], "旧账本")

    def test_save_mood_syncs_place(self):
        line = self._line()
        persona._save_mood(line, "害羞", 6,
                           "小超市门口人行道上 · 浅色针织衫 · 站定")
        self.assertEqual(persona._gameday(persona._entry(line))["place"],
                         "小超市门口人行道上")

    def test_soft_curfew_no_longer_forces_the_day_to_end(self):
        blk = persona._gameday_block({"day": 1, "turns_today": 68})
        self.assertIn("天色不早", blk)
        self.assertNotIn("今天的故事到这里就好", blk)


class StoryLineTest(unittest.TestCase):
    """发展线（2026-11-07）：只重组已有事实，缺哪段就跳哪段。"""

    NOTE = (
        "## 关系\n- 旅店认识的。\n"
        "## 你的事（你做过的）\n- 说旅店有家更好的。\n"
        "- 早上揽住她的头先吻了一回。\n"
        "## 她的事（她做过的）\n- 赶了三天路。\n"
        "- 进你房门只在门内两步停下。\n"
        "## 约定与未完成\n- 房钱你出，房间相邻。\n- 早饭欠着。\n"
        "## 忌讳与偏好\n- 不喜欢被叫小姐。\n"
        "## 日常与场景\n- 靠窗的饭馆。\n"
    )

    def test_story_line_orders_time_done_and_todo(self):
        out = persona.story_line(
            {"day": 3, "phase": "下午", "place": "饭馆",
             "plan": "陪她挑耳机", "plan_day": 5}, self.NOTE)
        self.assertTrue(out.startswith(
            "【故事到现在】\n时间：第 3 天 · 下午 · 饭馆"))
        self.assertIn("已经发生：", out)
        self.assertIn("早上揽住她的头先吻了一回", out)
        self.assertIn("还没完成：陪她挑耳机（约在第 5 天）", out)
        self.assertIn("早饭欠着", out)
        # 正向写法：整块不含禁令词
        for bad in ("不能", "不许", "不要", "禁止", "必须"):
            self.assertNotIn(bad, out)

    def test_story_line_skips_missing_sections(self):
        out = persona.story_line({"day": 7, "event": "已经办完：去旧书市"}, "")
        self.assertIn("时间：第 7 天", out)
        self.assertIn("今天已经办完：去旧书市", out)
        self.assertNotIn("已经发生：", out)
        self.assertNotIn("还没完成：", out)

    def test_prose_ledger_is_not_guessed(self):
        """散文账本没有小节标题，不猜结构——返回空串。"""
        self.assertEqual(
            persona.story_line({}, "她推门进去时她正趴在桌上画稿。后来……"), "")

    def test_empty_everything_returns_empty(self):
        self.assertEqual(persona.story_line({}, ""), "")
        self.assertEqual(persona.story_line(None, None), "")

    def test_story_line_respects_char_budget(self):
        gd = {"day": 9, "phase": "晚上", "place": "很长的地名" * 20,
              "plan": "约定" * 100}
        long_note = ("## 约定与未完成\n"
                     + "\n".join("- %s" % ("事" * 80) for _ in range(10)))
        out = persona.story_line(gd, long_note)
        body = out.split("\n", 1)[1].rsplit("\n", 1)[0]
        self.assertLessEqual(len(body), persona.STORY_MAX_CHARS + 1)

    def test_ledger_section_matches_substring(self):
        self.assertEqual(persona._ledger_section(self.NOTE, "约定"),
                         ["房钱你出，房间相邻。", "早饭欠着。"])
        self.assertEqual(persona._ledger_section(self.NOTE, "不存在"), [])
        self.assertEqual(persona._ledger_section("一段散文", "约定"), [])

    def test_plan_and_ledger_are_not_duplicated(self):
        """同一条约定只写一遍：结构化那条带天数，优先留它。"""
        note = ("## 约定与未完成\n"
                "- 下课后一起去超市买菜，然后上你家做饭。\n"
                "- 下一顿早餐她亲手做给你吃。\n")
        out = persona.story_line(
            {"day": 1, "plan": "一起买菜回家做饭", "plan_day": 2}, note)
        self.assertEqual(out.count("做饭"), 1)
        self.assertIn("一起买菜回家做饭（约在第 2 天）", out)
        self.assertIn("下一顿早餐", out)

    def test_clip_items_strips_double_punctuation(self):
        got = persona._clip_items(["她笑了。", "他说好；"], 4, 40)
        self.assertEqual(got, ["她笑了", "他说好"])


if __name__ == "__main__":
    unittest.main()
