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

    def test_intent_judgement_failure_still_replies(self):
        """判断段挂了也要能回话——退化成单次流程，不是整轮哑掉。

        2026-11-04 起默认攻略模式，第一段走的是「意图 + 打分」那个版本
        （`_judge_intent_scored`），两个都得 patch 掉才算「判断段整体不可用」。
        """
        with mock.patch.object(persona, "_judge_intent",
                               return_value=(None, "")), \
             mock.patch.object(persona, "_judge_intent_scored",
                               return_value=(None, "", None)), \
             mock.patch.object(persona.llm, "call_llm", return_value="你好呀") as m:
            persona.run_turn("private_1", "在吗", "private", "张三")
        self.assertEqual(m.call_count, 1)

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

    def test_no_workflow_is_silent(self):
        self._patch(wf=None)
        import app.image_jobs as ij
        self.assertEqual(persona.draw("x", {"appearancePrompt": "1girl"},
                                      "private_1"), "")
        ij.enqueue.assert_not_called()


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
        self.assertEqual(got, ("joke", "拿兔子开玩笑"))

    def test_reads_intimate(self):
        got = self._judge('{"intent": "intimate", "note": "求抱抱"}')
        self.assertEqual(got[0], "intimate")

    def test_unknown_intent_falls_back(self):
        """模型自创类别 → 当作没判断，走原流程。"""
        self.assertEqual(self._judge('{"intent": "tease", "note": "x"}'),
                         (None, ""))

    def test_non_json_falls_back(self):
        self.assertEqual(self._judge("我觉得他在开玩笑"), (None, ""))

    def test_exception_falls_back(self):
        from unittest import mock
        with mock.patch.object(persona.llm, "call_llm",
                               side_effect=RuntimeError("超时")):
            self.assertEqual(persona._judge_intent(self.CARD, "x"), (None, ""))

    def test_intent_block_tells_it_how_to_react(self):
        block = persona._intent_block("joke", "拿兔子开玩笑")
        self.assertIn("玩笑", block)
        self.assertIn("不要当真", block)
        self.assertIn("拿兔子开玩笑", block)

    def test_neutral_block_is_empty(self):
        self.assertEqual(persona._intent_block("neutral", ""), "")

    def test_unknown_intent_block_is_empty(self):
        self.assertEqual(persona._intent_block("", ""), "")


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


class RosterCardTest(PersonaBase):
    """`#角色扮演` 的规则用聊天记录卡片发（2026-11-03）。"""

    def setUp(self):
        super().setUp()
        persona.save_cards([_card("纱雾")])

    def test_roster_asks_for_a_card(self):
        for t in ("#角色扮演", "#人设列表", "@大大怪 #角色扮演"):
            self.assertTrue(persona.is_roster_request(t, addressed=True), t)

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
        self.assertIn("五格", text)

    def test_roster_says_hash_is_optional(self):
        """用户不爱打井号，规则里要明说「单独发就行」。

        2026-11-03 规则卡重写后，措辞从「不用打」统一成「不用加 `#`」，
        断言跟着认这两种写法，别再因为换词变红。
        """
        text = persona.roster_text()
        self.assertTrue("不用加 `#`" in text or "不用打" in text)
        self.assertNotIn("#妹妹", text)      # 名单里不再带井号前缀


class NoHashCommandTest(PersonaBase):
    """`#` 可选（2026-11-03）。

    用户反馈：「大家玩角色扮演，不喜欢加前面的 # 来触发，他们喜欢单独的词
    一个气泡来触发」。日志实证：`妹妹`、`角色扮演`、`大大怪，角色扮演`
    全都没被认出来。
    """

    def setUp(self):
        super().setUp()
        persona.save_cards([_card("妹妹"), _card("妹妹闺蜜")])

    def test_bare_card_name_enters_role(self):
        got = persona.handle_command("private_1", "妹妹", addressed=True)
        self.assertIsNotNone(got)
        self.assertIn("妹妹", got)

    def test_bare_list_word(self):
        got = persona.handle_command("private_1", "角色扮演", addressed=True)
        self.assertIsNotNone(got)
        self.assertIn("角色名单", got)

    def test_name_prefix_then_bare_word(self):
        got = persona.handle_command("private_1", "大大怪，角色扮演",
                                     addressed=True)
        self.assertIsNotNone(got)
        self.assertIn("角色名单", got)

    def test_command_word_plus_description(self):
        """「角色扮演，你是我的白色小兔妹妹」→ 从描述里认出卡名。"""
        got = persona.handle_command(
            "private_1", "角色扮演，你是我的妹妹闺蜜，我们相依为命",
            addressed=True)
        self.assertIsNotNone(got)
        self.assertIn("妹妹闺蜜", got)      # 长名字优先，不能被「妹妹」抢走

    def test_partial_mention_does_not_trigger(self):
        """整条消息必须是卡名——「妹妹想你了」是聊天，不是切角色。"""
        for t in ("她妹妹", "我的妹妹", "妹妹说", "妹妹想你了"):
            self.assertIsNone(
                persona.handle_command("private_1", t, addressed=True), t)

    def test_leave_and_clear_without_hash(self):
        persona.switch("private_1", persona.find_card("妹妹"))
        self.assertIn("出戏", persona.handle_command("private_1", "出戏",
                                                    addressed=True))
        persona.switch("private_1", persona.find_card("妹妹"))
        self.assertIn("清空", persona.handle_command("private_1", "清空",
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
        self.assertLess(approx_tokens, 8000,
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
        self.assertIn("人称", h)
        self.assertIn("记得用「你」", h)
        self.assertIn("心理活动要写足", h)

    def test_rule_repeated_at_the_end(self):
        """契约末尾要再强调——长上下文里开头的规则会被稀释。"""
        h = persona.build_head(self.CARD, "private_1", "")
        tail = h[-700:]
        self.assertIn("「你」", tail)
        self.assertIn("内心独白", tail)

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
        got = persona.handle_command("group_1_100", "纱雾", addressed=True)
        self.assertIsNotNone(got)
        self.assertIn("不方便演", got)
        self.assertIn("私聊", got)
        # 不能在群里真的切进角色
        self.assertIsNone(persona.active_card("group_1_100"))

    def test_group_roster_still_visible(self):
        """规则本身不敏感，群里看规则仍然允许。"""
        got = persona.handle_command("group_1_100", "角色扮演", addressed=True)
        self.assertIn("角色名单", got)

    def test_group_turn_returns_nothing(self):
        """群聊这条线彻底不演——即使 state 里还留着旧的扮演态。"""
        persona.switch("group_1_100", persona.find_card("纱雾"))
        turn = persona.run_turn("group_1_100", "在吗", "group", "张三", "100")
        self.assertEqual(turn.reply, "")

    def test_private_still_works(self):
        got = persona.handle_command("private_100", "纱雾", addressed=True)
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
            got = persona.handle_command("private_1", "角色扮演",
                                         addressed=True)
        self.assertIn("https://y.trycloudflare.com/persona", got)

    def test_group_command_excludes_link(self):
        with mock.patch("app.friend_site.url",
                        return_value="https://y.trycloudflare.com"):
            got = persona.handle_command("group_1", "角色扮演",
                                         addressed=True)
        self.assertNotIn("trycloudflare", got)

    def test_tunnel_down_still_returns_roster(self):
        """隧道没开（拿不到地址）时，规则本身不能少。"""
        with mock.patch("app.friend_site.url", return_value=""):
            got = persona.handle_command("private_1", "角色扮演",
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
    """`#清空`（2026-11-02）：清记忆、保留角色。"""

    def setUp(self):
        super().setUp()
        persona.save_cards([_card("纱雾")])
        self.card = persona.find_card("纱雾")

    def test_clear_keeps_card_but_wipes_memory(self):
        persona.switch("private_1", self.card)
        persona._save_hist("private_1", [{"role": "user", "content": "旧"}])
        st = persona._load_state()
        st["private_1"].update({"note": "记着点", "turns": 7,
                                "mood": "害羞", "intensity": 8, "state": "裸着"})
        persona._save_state(st)

        msg = persona.clear("private_1")

        st = persona._load_state()["private_1"]
        self.assertEqual(st["card"], self.card["id"])   # 角色还在
        self.assertEqual(st["note"], "")
        self.assertEqual(st["turns"], 0)
        self.assertEqual(st["state"], "")
        self.assertEqual(persona._load_hist("private_1"), [])
        self.assertIn("清空", msg)

    def test_clear_when_not_playing(self):
        self.assertIn("没有在演", persona.clear("private_nobody"))

    def test_clear_word_goes_through_command(self):
        persona.switch("private_1", self.card)
        got = persona.handle_command("private_1", "#清空")
        self.assertIsNotNone(got)
        self.assertNotIn("出戏", got)

    def test_clear_alias(self):
        for word in ("#重置", "#重开", "#从头开始"):
            persona.switch("private_1", self.card)
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
        self.assertIn("不要替你动手", head)
        self.assertIn("发现必须回到她身上", head)

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
        persona.handle_command("private_1", "出戏")
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


if __name__ == "__main__":
    unittest.main()
