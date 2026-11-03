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
        self.assertIsNone(persona.handle_command("group_1", "在吗"))
        self.assertIsNone(persona.handle_command("group_1", ""))
        self.assertIsNone(persona.handle_command("group_1", "#"))
        # `#` 开头但认不出卡名 → 放行，当普通消息
        self.assertIsNone(persona.handle_command("group_1", "#银发少女怎么画"))

    def test_list_enter_leave(self):
        persona.save_cards([_card("纱雾", description="害羞的插画师")])
        out = persona.handle_command("group_1", "#角色扮演")
        self.assertIn("纱雾", out)
        self.assertIn("害羞的插画师", out)
        self.assertIn("出戏", out)
        # 别名同义
        self.assertIn("纱雾", persona.handle_command("group_1", "#人设列表"))

        out = persona.handle_command("group_1", "#纱雾")
        self.assertIn("纱雾", out)
        self.assertIsNotNone(persona.active_card("group_1"))

        out = persona.handle_command("group_1", "#出戏")
        self.assertIn("出戏", out)
        self.assertIsNone(persona.active_card("group_1"))

    def test_leave_when_not_playing_is_not_a_command(self):
        self.assertIsNone(persona.handle_command("group_1", "#出戏"))

    def test_scope_is_per_session_line(self):
        persona.save_cards([_card("纱雾")])
        persona.handle_command("group_1", "#纱雾")
        self.assertIsNotNone(persona.active_card("group_1"))
        self.assertIsNone(persona.active_card("group_2"))      # 别的群不受影响
        self.assertIsNone(persona.active_card("private_9"))

    def test_deleting_the_card_ends_the_roleplay(self):
        persona.save_cards([_card("纱雾")])
        persona.handle_command("group_1", "#纱雾")
        persona.save_cards([])
        # 卡没了就自动当没在演，不会卡在一个取不到的 id 上
        self.assertIsNone(persona.active_card("group_1"))


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
        head = persona.build_head(_card("纱雾"), "group_1", "张三",
                                  "他昨天让我画了只兔子。")
        self.assertIn("【之前的事】", head)
        self.assertIn("画了只兔子", head)


class TurnTest(PersonaBase):
    def setUp(self):
        super().setUp()
        persona.save_cards([_card("纱雾")])
        self.card = persona.find_card("纱雾")
        persona.switch("group_1", self.card)

    def _run(self, reply_text, text="在吗", sender="张三"):
        with mock.patch.object(persona.llm, "call_llm",
                               return_value=reply_text) as m:
            out = persona.run_turn("group_1", text, "group", sender)
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
        """判断段挂了也要能回话——退化成单次流程，不是整轮哑掉。"""
        with mock.patch.object(persona, "_judge_intent",
                               return_value=(None, "")), \
             mock.patch.object(persona.llm, "call_llm", return_value="你好呀") as m:
            persona.run_turn("group_1", "在吗", "group", "张三")
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
        ent = persona._load_hist("group_1")[1]["content"]
        self.assertIn("你好呀", ent)
        self.assertIn("画面：1girl, sitting", ent)
        self.assertIn("心情：害羞 强度6", ent)
        self.assertIn("状态：坐在桌前", ent)

    def test_split_turn_strips_meta_from_display(self):
        """存进历史的是全的，但**用户看到的正文**里不许有这三行。"""
        self._run("你好呀\n画面：1girl, sitting\n心情：害羞 强度6\n状态：坐在桌前")
        turn = persona.split_turn(persona._load_hist("group_1")[1]["content"])
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
        hist = persona._load_hist("group_1")
        self.assertEqual([m["role"] for m in hist], ["user", "assistant"])
        self.assertEqual(hist[0]["content"], "张三：在吗")
        self.assertTrue(hist[1]["content"].startswith("你好呀"))
        # 三行被补齐了（模型没写画面，补成空值也算形状完整）
        self.assertIn("心情：", hist[1]["content"])
        self.assertIn("状态：", hist[1]["content"])

    def test_group_prefixes_sender_private_does_not(self):
        self._run("嗯", text="在吗", sender="张三")
        self.assertEqual(persona._load_hist("group_1")[0]["content"], "张三：在吗")
        persona.switch("private_9", self.card)
        with mock.patch.object(persona.llm, "call_llm", return_value="嗯"):
            persona.run_turn("private_9", "在吗", "private", "张三")
        self.assertEqual(persona._load_hist("private_9")[0]["content"], "在吗")

    def test_empty_reply_says_so(self):
        """模型整轮没交作业：说一句「卡了一下」。

        不重跑——重跑一次未必更好，还白烧一轮。
        """
        out, m = self._run("")
        self.assertIn("卡", out.reply)

    def test_not_in_roleplay_is_a_noop(self):
        persona.leave("group_1")
        with mock.patch.object(persona.llm, "call_llm") as m:
            self.assertEqual(persona.run_turn("group_1", "在吗", "group").reply, "")
        m.assert_not_called()

    def test_history_is_separate_from_the_image_bot(self):
        self._run("你好呀")
        # 生图机器人的历史落在同一目录的另一份文件里，这边读到的只有对话
        self.assertFalse(os.path.exists(
            os.path.join(agents.AGENTS_DIR, "qq", "sessions", "group_1.jsonl")))
        self.assertTrue(os.path.exists(persona._hist_path("group_1")))


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
        persona.draw("sitting, smile", {"appearancePrompt": "1girl"}, "group_1")
        self.assertEqual(gi.build_t2i_workflow.call_args[0][0],
                         persona.FAST_SKILL)
        self.assertEqual(gi.build_t2i_workflow.call_args[0][1],
                         "1girl, sitting, smile")

    def test_comfy_down_is_silent(self):
        self._patch(alive=False)
        import app.image_jobs as ij
        self.assertEqual(persona.draw("x", {}, "group_1"), "")
        ij.enqueue.assert_not_called()

    def test_empty_scene_does_not_enqueue(self):
        self._patch()
        import app.image_jobs as ij
        self.assertEqual(persona.draw("   ", {"appearancePrompt": "1girl"},
                                      "group_1"), "")
        ij.enqueue.assert_not_called()

    def test_queue_rejection_is_silent(self):
        self._patch(reason="现在排队的人太多了（前面还有 9 张），这一张先不画。")
        self.assertEqual(persona.draw("x", {"appearancePrompt": "1girl"},
                                      "group_1"), "")

    def test_draw_failure_is_silent(self):
        job = self._patch()
        job.wait.side_effect = RuntimeError("ComfyUI 掉线")
        self.assertEqual(persona.draw("x", {"appearancePrompt": "1girl"},
                                      "group_1"), "")

    def test_no_workflow_is_silent(self):
        self._patch(wf=None)
        import app.image_jobs as ij
        self.assertEqual(persona.draw("x", {"appearancePrompt": "1girl"},
                                      "group_1"), "")
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
        """用户不爱打井号，规则里要明说「单独发就行」。"""
        text = persona.roster_text()
        self.assertIn("不用打", text)
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
        card = {"name": "纱雾", "systemPrompt": "你是纱雾。"}
        head = persona.build_head(card, "group_1", "张三")
        self.assertIn("这是群聊", head)
        self.assertIn("点到为止", head)

    def test_private_head_has_no_restraint_note(self):
        card = {"name": "纱雾", "systemPrompt": "你是纱雾。"}
        head = persona.build_head(card, "private_1", "")
        self.assertNotIn("这是群聊", head)


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


if __name__ == "__main__":
    unittest.main()
