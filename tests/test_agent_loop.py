# -*- coding: utf-8 -*-
"""Agent 主循环测试（app/agent.py::run_agent_stream）。

用"脚本化的假 LLM"替换真实网络调用，验证事件流顺序、工具执行、
pre_tool_results 注入、思考内容（reasoning）只出不进、异常兜底与
最大轮次保护。这些是前端渲染和用户体验直接依赖的行为，回归代价最高。
"""

import json
import os
import re
import tempfile
import unittest
from unittest import mock

import app.agent as agent
import app.agents as agent_store
import app.comfy_status as comfy_status
import app.config as config

# comfy_status.snapshot 一律吃内存快照，任何路径都不走真网络
# （状态栏已不再探测 ComfyUI，见 app/comfy_status 的模块注释）
_comfy_patch = None


def setUpModule():
    global _comfy_patch
    _comfy_patch = mock.patch.object(
        comfy_status, "snapshot",
        return_value={"online": True, "running": 0, "pending": 0, "ts": 0.0})
    _comfy_patch.start()


def tearDownModule():
    global _comfy_patch
    if _comfy_patch is not None:
        _comfy_patch.stop()
        _comfy_patch = None


class _ScriptedLLM:
    """按脚本依次产出回复，模拟 call_llm_stream 的 (kind, text) 契约。

    脚本元素：
      - str            → 作为一整块正文（"content"）
      - (kind, text)   → 原样产出，用于构造思考内容 / 分块正文
      - [(kind, text)] → 同上，一次产出多块
      - Exception      → 抛出（验证 agent 的异常兜底）
    """

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0
        self.seen_histories = []
        self.seen_kwargs = []

    def __call__(self, messages, **kwargs):
        self.calls += 1
        self.seen_histories.append(messages)
        self.seen_kwargs.append(kwargs)
        if not self.replies:
            yield "content", "（脚本已用尽）"
            return
        item = self.replies.pop(0)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, list):
            for pair in item:
                yield pair
        elif isinstance(item, tuple):
            yield item
        else:
            yield "content", item


class CompactWriteBackTest(unittest.TestCase):
    """压缩结果必须原地写回 history（2026-09-30）。

    不写回的话压缩只活在这一轮的局部变量里，落盘写的还是压缩前的胖历史，
    下一轮从盘上读回来重新压一遍 —— 实测群里每个「带工具的回合」都白烧一次
    13K~18K token 的**全价**摘要调用（0% 命中）+ 8 秒；09-29 共 94 次 ≥10K、
    合计 2.08M miss token。
    """

    def setUp(self):
        p = mock.patch.object(config, "MAX_TURNS", 10)
        p.start()
        self.addCleanup(p.stop)
        self.trim_kwargs = []

    def _patch_trim(self, make_result):
        def fake(history, agent_id=None, **kw):
            self.trim_kwargs.append(kw)
            return make_result(history)

        p = mock.patch.object(agent, "trim_history", fake)
        p.start()
        self.addCleanup(p.stop)

    def _fat_history(self, turns=6):
        h = [{"role": "system", "content": "sys"}]
        for i in range(turns):
            h.append({"role": "user", "content": "u%d" % i})
            h.append({"role": "assistant", "content": "a%d" % i})
        return h

    def _run(self, history, **kwargs):
        fake = _ScriptedLLM(["好了"])
        p = mock.patch.object(agent, "call_llm_stream", fake)
        p.start()
        self.addCleanup(p.stop)
        return list(agent.run_agent_stream("你好", history, **kwargs))

    def test_compaction_shrinks_the_caller_history_in_place(self):
        history = self._fat_history()

        # 模拟 _compact：system + 摘要 + 最近一轮（含刚 append 的这条用户消息）
        def make_result(h):
            return ([h[0],
                     {"role": "tool_result", "tool_name": "compact_summary",
                      "content": "[上文压缩摘要] 旧内容"}]
                    + h[-1:])

        self._patch_trim(make_result)
        self._run(history, agent_id="qq", session_key="group_9")

        # 6 轮胖历史被压成 system + 摘要 + 本轮 user，再追加本轮 assistant
        self.assertEqual(len(history), 4)
        self.assertEqual(history[0]["content"], "sys")
        self.assertIn("[上文压缩摘要]", history[1]["content"])
        self.assertEqual(history[2]["content"], "你好")
        self.assertEqual(history[-1]["content"], "好了")
        # 老的轮次确实没了（不是被复制到别处）
        self.assertNotIn("u0", str(history))

    def test_session_key_is_forwarded_to_trim_history(self):
        history = self._fat_history()
        self._patch_trim(lambda h: h)          # 不压缩，只看入参
        self._run(history, agent_id="qq", session_key="group_1103174141")
        self.assertEqual(self.trim_kwargs[-1]["session_key"], "group_1103174141")

    def test_no_compaction_leaves_history_alone(self):
        """trim_history 原样返回时（不压缩）history 不能被改。"""
        history = self._fat_history()
        before = list(history)
        self._patch_trim(lambda h: h)
        self._run(history, agent_id="qq")
        # 只多了本轮的两条（user + assistant），老的 13 条一条没动
        self.assertEqual(history[:len(before)], before)
        self.assertEqual(len(history), len(before) + 2)


class AgentLoopTest(unittest.TestCase):
    def setUp(self):
        # 不做上下文压缩、不做真实工具调用、不做真实网络请求
        for target, value in (
            ("trim_history", lambda h, agent_id=None, **kw: h),
            ("execute_tool", lambda name, args: "工具结果:" + name),
        ):
            p = mock.patch.object(agent, target, value)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(config, "MAX_TURNS", 10)
        p.start()
        self.addCleanup(p.stop)
        self.history = []

    def _patch_llm(self, replies):
        fake = _ScriptedLLM(replies)
        p = mock.patch.object(agent, "call_llm_stream", fake)
        p.start()
        self.addCleanup(p.stop)
        return fake

    def _collect(self, user_input, **kwargs):
        return list(agent.run_agent_stream(user_input, self.history, **kwargs))

    def test_plain_answer_event_sequence(self):
        self._patch_llm(["你好，我是助理。"])
        events = self._collect("你好")
        self.assertEqual([e["type"] for e in events], ["user", "assistant"])
        self.assertEqual(events[0]["content"], "你好")
        self.assertEqual(events[1]["content"], "你好，我是助理。")
        self.assertEqual(self.history[0], {"role": "user", "content": "你好"})
        self.assertEqual(self.history[-1]["role"], "assistant")

    def test_single_tool_then_answer(self):
        self._patch_llm(['[[TOOL:get_time]][[/TOOL]]', '现在是 12:00。'])
        events = self._collect("几点了")
        self.assertEqual([e["type"] for e in events],
                         ["user", "tool_call", "tool_result", "assistant"])
        self.assertEqual(events[1]["name"], "get_time")
        self.assertEqual(events[2]["result"], "工具结果:get_time")
        self.assertEqual(events[3]["content"], "现在是 12:00。")
        # 工具结果必须落进历史，下一轮 LLM 才看得到
        self.assertIn("tool_result", [m["role"] for m in self.history])

    def test_tool_call_reply_emits_no_assistant_event(self):
        # 纯工具调用的那一轮，正文被剥空 → 不应产生 assistant 事件
        self._patch_llm(['[[TOOL:get_time]][[/TOOL]]', '好了'])
        events = self._collect("几点")
        self.assertEqual([e["type"] for e in events].count("assistant"), 1)

    def test_multiple_tools_in_one_reply(self):
        self._patch_llm([
            '[[TOOL:get_time]][[/TOOL]]\n[[TOOL:list_skills]][[/TOOL]]',
            '完成',
        ])
        events = self._collect("都查一下")
        self.assertEqual([e["type"] for e in events],
                         ["user", "tool_call", "tool_result",
                          "tool_call", "tool_result", "assistant"])
        self.assertEqual(events[1]["name"], "get_time")
        self.assertEqual(events[3]["name"], "list_skills")

    def test_pre_tool_results_injected_before_loop(self):
        self._patch_llm(["已知答案"])
        pre = [{"name": "get_time", "result": "2026-01-01 00:00:00"}]
        events = self._collect("现在几点", pre_tool_results=pre)
        self.assertEqual([e["type"] for e in events],
                         ["user", "tool_result", "assistant"])
        self.assertEqual(events[1]["name"], "get_time")
        self.assertEqual(events[1]["result"], "2026-01-01 00:00:00")
        # 注入的工具结果要排在 LLM 循环之前（history[1]）
        self.assertEqual(self.history[1]["role"], "tool_result")
        self.assertEqual(self.history[1]["tool_name"], "get_time")

    # ─── 空回复守卫 ───────────────────────────────────
    # 背景（2026-09-28）：deepseek 系开思维链后偶发「流正常结束但正文零字」，
    # 一轮就此静默，用户看到「收到了消息却不回」。守卫 = 换链上下一家重试一次。

    def test_empty_reply_retries_with_next_model(self):
        fake = self._patch_llm([("reasoning", "只想不说"), "在的"])
        p = mock.patch.object(agent, "candidates",
                              return_value=[("volc", "m1"), ("mimo", "m2")])
        p.start()
        self.addCleanup(p.stop)
        events = self._collect("在吗", provider="volc", model="m1")
        # 第一次空 → 换 mimo/m2 重跑 → 正常回复
        self.assertEqual(fake.calls, 2)
        self.assertEqual(fake.seen_kwargs[0].get("provider"), "volc")
        self.assertEqual(fake.seen_kwargs[0].get("model"), "m1")
        self.assertEqual(fake.seen_kwargs[1].get("provider"), "mimo")
        self.assertEqual(fake.seen_kwargs[1].get("model"), "m2")
        texts = [e["content"] for e in events if e["type"] == "assistant"]
        self.assertEqual(texts, ["在的"])

    def test_empty_reply_leaves_no_empty_assistant_in_history(self):
        self._patch_llm([("reasoning", "只想不说"), "在的"])
        p = mock.patch.object(agent, "candidates",
                              return_value=[("volc", "m1"), ("mimo", "m2")])
        p.start()
        self.addCleanup(p.stop)
        self._collect("在吗", provider="volc", model="m1")
        roles = [m["role"] for m in self.history]
        self.assertNotIn("", [m.get("content") for m in self.history])
        self.assertEqual(roles.count("assistant"), 1)
        self.assertEqual(self.history[-1]["content"], "在的")

    def test_empty_reply_twice_gives_up_silently(self):
        fake = self._patch_llm([("reasoning", "甲"), ("reasoning", "乙")])
        p = mock.patch.object(agent, "candidates",
                              return_value=[("volc", "m1"), ("mimo", "m2")])
        p.start()
        self.addCleanup(p.stop)
        events = self._collect("在吗", provider="volc", model="m1")
        self.assertEqual(fake.calls, 2)
        self.assertNotIn("assistant", [e["type"] for e in events])
        # 历史里也不留空 assistant（只有用户那条）
        self.assertEqual([m["role"] for m in self.history], ["user"])

    def test_single_candidate_empty_retries_same_model(self):
        fake = self._patch_llm([("reasoning", "嗯"), "好"])
        p = mock.patch.object(agent, "candidates",
                              return_value=[("volc", "m1")])
        p.start()
        self.addCleanup(p.stop)
        events = self._collect("在吗", provider="volc", model="m1")
        self.assertEqual(fake.calls, 2)
        self.assertEqual(fake.seen_kwargs[1].get("provider"), "volc")
        texts = [e["content"] for e in events if e["type"] == "assistant"]
        self.assertEqual(texts, ["好"])

    def test_status_bar_appended_at_tail_not_front(self):
        fake = self._patch_llm(["ok"])
        self.history.append({"role": "system", "content": "stable prompt"})
        self._collect("你好")
        sent = fake.seen_histories[0]
        self.assertEqual(sent[0]["role"], "system")
        # 动态状态栏必须追加在末尾；放在头部会毒化 prefix cache
        self.assertIn("<status_bar>", sent[-1]["content"])
        self.assertNotIn("<status_bar>", sent[0]["content"])

    def test_reasoning_is_streamed_but_never_stored(self):
        self._patch_llm([
            [("reasoning", "先看"), ("reasoning", "时间"), ("content", "现在 12 点。")],
        ])
        events = self._collect("几点了")
        self.assertEqual([e["type"] for e in events],
                         ["user", "reasoning", "reasoning", "assistant"])
        self.assertEqual("".join(e["content"] for e in events if e["type"] == "reasoning"),
                         "先看时间")
        # 思考内容"只出不进"：历史里只能有 user/assistant，且正文不含思考
        self.assertEqual([m["role"] for m in self.history], ["user", "assistant"])
        self.assertEqual(self.history[-1]["content"], "现在 12 点。")
        self.assertNotIn("先看时间", self.history[-1]["content"])

    # ─── 带图对话：图片只注入第 1 轮，且绝不进历史 ─────

    def test_image_attached_to_first_turn_only(self):
        fake = self._patch_llm(['[[TOOL:list_files]][[/TOOL]]', '看完了'])
        # 显式指定有视觉的模型：图片直发多模态。若走默认 provider（.env 里是
        # 火山，纯文本），会改走识图预处理——那是另一条路径，另有测试覆盖。
        list(agent.run_agent_stream("（用户上传了一张图片：看图）", self.history,
                                    provider="deepseek",
                                    image="data:image/jpeg;base64,ZZZ"))
        # 第 1 轮：本轮用户消息被升级成多模态
        first = [m for m in fake.seen_histories[0] if isinstance(m.get("content"), list)]
        self.assertEqual(len(first), 1)
        parts = first[0]["content"]
        self.assertEqual(parts[0],
                         {"type": "text", "text": "（用户上传了一张图片：看图）"})
        self.assertEqual(parts[1]["image_url"]["url"], "data:image/jpeg;base64,ZZZ")
        # 第 2 轮：不再重发图片——白烧输入 token，而且每轮都会掐断前缀缓存
        second = [m for m in fake.seen_histories[1] if isinstance(m.get("content"), list)]
        self.assertEqual(second, [])

    def test_image_never_leaks_into_history(self):
        self._patch_llm(["看图完毕。"])
        list(agent.run_agent_stream("看图", self.history, provider="deepseek",
                                    image="data:image/jpeg;base64,ZZZ"))
        # 图片本体绝不能进 history——整份历史会被原样落盘
        self.assertEqual(self.history[0], {"role": "user", "content": "看图"})

    # ─── 无视觉模型：先识图转文字再进正文 ─────────────

    def _patch_vision(self, text=None, exc=None):
        """替换识图调用：要么返回固定文字，要么抛错（验证降级路径）。"""
        self.vision_prompts = []

        def fake(_data_url, prompt=None, timeout=None):
            # prompt 是 2026-09-27 起新增的入参（识图要带上用户问题）。收下
            # 并留档，方便断言「问题确实传到了识图」——签名不跟着改的话，
            # 调用方一传 prompt 就会 TypeError，被识图的 try/except 吞成
            # 「识图失败」，测试仍会"通过"，只有断言能把它揪出来。
            self.vision_prompts.append(prompt)
            if exc is not None:
                raise exc
            return text

        p = mock.patch("app.vision.describe", fake)
        p.start()
        self.addCleanup(p.stop)

    def test_user_question_reaches_vision_prompt(self):
        """识图要看得见用户的问题。

        看不到问题，识图就只能对着一张图泛泛而谈，下游文本模型还得自己猜
        用户想问什么。这条盯的是整条链路：run_agent_stream → _with_vision
        → vision.describe 的 prompt 入参。
        """
        self._patch_vision("一只猫趴在键盘上。")
        self._patch_llm(["我看到一只猫。"])
        list(agent.run_agent_stream("这是什么", self.history, provider="volc",
                                    image="data:image/jpeg;base64,ZZZ"))
        self.assertTrue(self.vision_prompts)
        self.assertIn("这是什么", self.vision_prompts[0])

    def test_no_vision_model_gets_recognized_text(self):
        """火山的纯文本模型：图先被识成文字，拼在用户输入后面进正文。

        这条路径是必需的——把 base64 直接喂给纯文本模型不是"效果差"，
        而是整条请求挂死（实测 ReadTimeout 卡满 180 秒）。
        """
        self._patch_vision("一只猫趴在键盘上。")
        fake = self._patch_llm(["我看到一只猫。"])
        list(agent.run_agent_stream("这是什么", self.history, provider="volc",
                                    image="data:image/jpeg;base64,ZZZ"))

        # 发给模型的那条用户消息带上识别结果（第 1 轮）
        first_user = [m for m in fake.seen_histories[0]
                      if m.get("role") == "user"][0]
        self.assertIn("这是什么", first_user["content"])
        self.assertIn("一只猫趴在键盘上。", first_user["content"])
        self.assertIn("[用户发来图片，以下是识别结果]", first_user["content"])
        # 没有任何多模态结构，也没落下 base64
        self.assertFalse(any(isinstance(m.get("content"), list)
                             for m in fake.seen_histories[0]))
        self.assertNotIn("ZZZ", str(fake.seen_histories[0]))

    def test_recognized_text_is_what_enters_history(self):
        """进 history 的是识别文字，不是图片本体。"""
        self._patch_vision("图中写着：504 Gateway Timeout")
        self._patch_llm(["是网关超时。"])
        list(agent.run_agent_stream("这个报错什么意思", self.history,
                                    provider="volc",
                                    image="data:image/jpeg;base64,ZZZ"))
        self.assertEqual(self.history[0]["role"], "user")
        self.assertIn("504 Gateway Timeout", self.history[0]["content"])
        self.assertNotIn("ZZZ", self.history[0]["content"])

    def test_vision_failure_degrades_instead_of_breaking_turn(self):
        """识图挂掉不能让整轮对话失败：降级成"看不到这张图"，照常回答。"""
        self._patch_vision(exc=RuntimeError("识图请求失败：超时"))
        fake = self._patch_llm(["抱歉，我看不到这张图。"])
        list(agent.run_agent_stream("看看这个", self.history, provider="volc",
                                    image="data:image/jpeg;base64,ZZZ"))

        first_user = [m for m in fake.seen_histories[0]
                      if m.get("role") == "user"][0]
        self.assertIn("识别失败", first_user["content"])
        # 说的是"你看不到"，不能让模型以为图在那儿而顺着编
        self.assertIn("看不到它的内容", first_user["content"])
        # 正常出最终回复，没有变成错误事件
        self.assertEqual(self.history[-1]["content"], "抱歉，我看不到这张图。")

    def test_long_vision_text_is_truncated_before_entering_history(self):
        """识图文字块写进 history 前必须截断到 _VISION_NOTE_MAX_CHARS。

        它是历史里最大的一块可压缩脂肪（实测 19 条占群历史 28%，单条 300~1,712
        字）。不截就会把会话顶破 token 预算 → 压缩开始每轮触发 → 摘要插在系统头
        正后面 → 整段前缀缓存作废（签名 `命中 6144 / 26xxx = 23.3%`）。
        """
        long_text = "画面描述：" + "".join(
            "第%d处：银白色双马尾，发尾渐变薄荷绿。" % i for i in range(1, 40))
        self._patch_vision(long_text)
        self._patch_llm(["看到了。"])
        list(agent.run_agent_stream("这是什么", self.history, provider="volc",
                                    image="data:image/jpeg;base64,ZZZ"))

        body = self.history[0]["content"]
        self.assertIn("[用户发来图片，以下是识别结果]", body)
        self.assertTrue(body.endswith("…（描述已截断）"))
        self.assertIn("银白色双马尾", body)           # 头部保留
        self.assertNotIn("第39处", body)             # 尾部被切掉
        self.assertNotIn(long_text, body)
        self.assertLess(len(body), agent._VISION_NOTE_MAX_CHARS + 120)

    def test_short_vision_text_is_not_truncated(self):
        """没超上限的识图结果一个字节都不许动，也不能加截断标记。"""
        self._patch_vision("一只猫趴在键盘上。")
        self._patch_llm(["看到了。"])
        list(agent.run_agent_stream("这是什么", self.history, provider="volc",
                                    image="data:image/jpeg;base64,ZZZ"))
        self.assertIn("一只猫趴在键盘上。", self.history[0]["content"])
        self.assertNotIn("（描述已截断）", self.history[0]["content"])

    def test_blind_vision_result_is_replaced_by_failure_note(self):
        """识图模型「成功地没看图」时，它编的内容绝不能进 history。

        实测 deepseek-flash vision 会返回「我无法直接看到图片，但根据你提供的
        引用信息和需求，我可以帮你梳理关键点…」，然后自行编出一段画面描述，
        下游照样据此生图。这类返回**不抛异常**，原兜底完全接不住。
        """
        blind = ("我无法直接看到图片，但根据你提供的引用信息和需求，"
                 "我可以帮你梳理关键点：原图是粉色小熊连体泳衣。")
        self._patch_vision(blind)
        self._patch_llm(["看不到。"])
        list(agent.run_agent_stream("照着这张改", self.history, provider="volc",
                                    image="data:image/jpeg;base64,ZZZ"))

        body = self.history[0]["content"]
        self.assertIn("识别失败", body)
        self.assertIn("看不到它的内容", body)
        self.assertNotIn("粉色小熊连体泳衣", body)

    def test_wu_fa_inside_a_real_description_is_not_a_failure(self):
        """判据只看「模型自称看不到」——描述画面里的文字含「无法」不算失败。"""
        self._patch_vision("画面里的报错文字是「无法连接到服务器」。")
        self._patch_llm(["网关问题。"])
        list(agent.run_agent_stream("这个报错什么意思", self.history,
                                    provider="volc",
                                    image="data:image/jpeg;base64,ZZZ"))
        body = self.history[0]["content"]
        self.assertIn("无法连接到服务器", body)
        self.assertNotIn("识别失败", body)

    def test_multiple_images_all_attached_and_numbered(self):
        self._patch_vision("识别结果")
        fake = self._patch_llm(["看完了"])
        list(agent.run_agent_stream("两张", self.history, provider="deepseek",
                                    image=["data:image/jpeg;base64,AAA",
                                           "data:image/jpeg;base64,BBB"]))
        multi = [m for m in fake.seen_histories[0]
                 if isinstance(m.get("content"), list)]
        self.assertEqual(len(multi), 1)
        urls = [p["image_url"]["url"] for p in multi[0]["content"]
                if p.get("type") == "image_url"]
        self.assertEqual(urls, ["data:image/jpeg;base64,AAA",
                                "data:image/jpeg;base64,BBB"])

    def test_multiple_images_numbered_when_preprocessing(self):
        self._patch_vision("文字内容")
        fake = self._patch_llm(["好"])
        list(agent.run_agent_stream("两张", self.history, provider="volc",
                                    image=["data:image/jpeg;base64,AAA",
                                           "data:image/jpeg;base64,BBB"]))
        first_user = [m for m in fake.seen_histories[0]
                      if m.get("role") == "user"][0]
        self.assertIn("【第 1 张】", first_user["content"])
        self.assertIn("【第 2 张】", first_user["content"])

    def test_image_targets_user_message_not_tool_result(self):
        # 用户手打工具块时 pre_tool_results 排在本轮 user 之后（_history_for_llm
        # 会把它们转成 user role），倒序找必须跳过它们，否则图片挂到工具结果上
        fake = self._patch_llm(["好了"])
        list(agent.run_agent_stream(
            "看图", self.history, provider="deepseek",
            image="data:image/jpeg;base64,ZZZ",
            pre_tool_results=[{"name": "get_time", "result": "12:00"}]))
        multi = [m for m in fake.seen_histories[0] if isinstance(m.get("content"), list)]
        self.assertEqual(len(multi), 1)
        self.assertEqual(multi[0]["content"][0]["text"], "看图")

    def test_content_chunks_are_concatenated(self):
        self._patch_llm([[("content", "你"), ("content", "好"), ("content", "！")]])
        events = self._collect("你好")
        # 正文分块只能产生一条 assistant 事件（不是三条）
        self.assertEqual([e["type"] for e in events], ["user", "assistant"])
        self.assertEqual(events[-1]["content"], "你好！")
        self.assertEqual(self.history[-1]["content"], "你好！")

    def test_reasoning_resumes_after_tool_call(self):
        self._patch_llm([
            [("reasoning", "需要查时间"), ("content", "[[TOOL:get_time]][[/TOOL]]")],
            [("reasoning", "拿到结果了"), ("content", "现在 12:00。")],
        ])
        events = self._collect("几点了")
        self.assertEqual([e["type"] for e in events],
                         ["user", "reasoning", "tool_call", "tool_result",
                          "reasoning", "assistant"])
        self.assertEqual(events[1]["content"], "需要查时间")
        self.assertEqual(events[4]["content"], "拿到结果了")

    def test_llm_exception_is_reported_not_raised(self):
        self._patch_llm([RuntimeError("boom")])
        events = self._collect("你好")
        self.assertEqual(events[-1]["type"], "assistant")
        self.assertIn("执行出错", events[-1]["content"])
        self.assertIn("boom", events[-1]["content"])

    def test_max_turns_guard(self):
        p = mock.patch.object(config, "MAX_TURNS", 1)
        p.start()
        self.addCleanup(p.stop)
        self._patch_llm(['[[TOOL:get_time]][[/TOOL]]'])  # 每轮都要调工具
        events = self._collect("一直查")
        self.assertIn("最大轮次", events[-1]["content"])


class AgentToolWhitelistTest(unittest.TestCase):
    """执行层的工具白名单拦截。

    system prompt 里不列出只是「看不见」，这里保证「调不动」——
    少了这一道，「写作 agent 不能用生图」就只是名义上的隔离。
    """

    def setUp(self):
        self.history = []
        self.executed = []

        def fake_execute(name, args):
            self.executed.append(name)
            return "结果:" + name

        for target, value in (
            ("trim_history", lambda h, agent_id=None, **kw: h),
            ("execute_tool", fake_execute),
        ):
            p = mock.patch.object(agent, target, value)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(config, "MAX_TURNS", 10)
        p.start()
        self.addCleanup(p.stop)

    def _patch_llm(self, replies):
        fake = _ScriptedLLM(replies)
        p = mock.patch.object(agent, "call_llm_stream", fake)
        p.start()
        self.addCleanup(p.stop)
        return fake

    def _patch_whitelist(self, allowed):
        p = mock.patch.object(agent.agent_store, "allows_tool",
                              lambda aid, name: allowed)
        p.start()
        self.addCleanup(p.stop)

    def test_allowed_tool_runs(self):
        self._patch_whitelist(True)
        self._patch_llm(['[[TOOL:read_file]]{"path": "a.md"}[[/TOOL]]', '读完了。'])
        events = list(agent.run_agent_stream("读一下", self.history, agent_id="writing"))
        self.assertEqual(self.executed, ["read_file"])
        results = [e for e in events if e["type"] == "tool_result"]
        self.assertEqual(results[0]["result"], "结果:read_file")

    def test_blocked_tool_never_executes(self):
        self._patch_whitelist(False)
        self._patch_llm(['[[TOOL:generate_image]]{"prompt": "cat"}[[/TOOL]]', '好的。'])
        events = list(agent.run_agent_stream("画只猫", self.history, agent_id="writing"))
        self.assertEqual(self.executed, [])                      # 压根没被执行
        results = [e for e in events if e["type"] == "tool_result"]
        self.assertIn("不可用", results[0]["result"])
        # 拒绝结果同样要落进历史，模型下一轮才知道换条路
        blocked = [m for m in self.history if m.get("role") == "tool_result"]
        self.assertEqual(len(blocked), 1)
        self.assertIn("不可用", blocked[0]["content"])


class AgentEventHistoryContractTest(unittest.TestCase):
    """契约：会话消息类事件出流时，那条消息已经写进 history。

    app/main.py 以「事件出流」作为落盘时机，所以这个顺序是不变式。
    反过来（先 yield 后 append）该条消息会赶不上落盘——客户端中断时尤其明显。
    """

    def setUp(self):
        self.history = []
        for target, value in (
            ("trim_history", lambda h, agent_id=None, **kw: h),
            ("execute_tool", lambda name, args: "工具结果:" + name),
        ):
            p = mock.patch.object(agent, target, value)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(config, "MAX_TURNS", 10)
        p.start()
        self.addCleanup(p.stop)

    def _patch_llm(self, replies):
        fake = _ScriptedLLM(replies)
        p = mock.patch.object(agent, "call_llm_stream", fake)
        p.start()
        self.addCleanup(p.stop)
        return fake

    def _histories_by_event(self, user_input, **kwargs):
        """手动迭代事件流，逐个记录事件出流那一刻的 history 内容。"""
        seen = {}
        gen = agent.run_agent_stream(user_input, self.history, **kwargs)
        for event in gen:
            seen.setdefault(event["type"], []).append(
                [m.get("content") for m in self.history])
        return seen

    def test_each_event_sees_its_own_message_in_history(self):
        self._patch_llm(['[[TOOL:get_time]][[/TOOL]]', '现在 12:00。'])
        seen = self._histories_by_event("几点了")

        # user 事件出流时，用户那句话已经在历史里
        self.assertEqual(seen["user"][0], ["几点了"])
        # tool_result 事件出流时，工具结果已经在历史里（原先这里是反的）
        self.assertIn("工具结果:get_time", seen["tool_result"][0])
        # assistant 事件出流时，回复正文已经在历史里
        self.assertIn("现在 12:00。", seen["assistant"][0])

    def test_mid_stream_close_keeps_emitted_messages(self):
        """客户端中途断开：已经推送出去的内容都留在 history 里。"""
        self._patch_llm(['[[TOOL:get_time]][[/TOOL]]', '现在 12:00。'])
        gen = agent.run_agent_stream("几点了", self.history)
        next(gen)                                  # user
        next(gen)                                  # tool_call（不入历史）
        gen.close()                                # 模拟断开

        contents = [m.get("content") for m in self.history]
        # 已推送的 user 在；那一轮的 assistant 原文（工具块）也在，
        # 因为它在解析工具调用之前就已入历史——只是没有单独出流。
        self.assertEqual(contents[0], "几点了")
        self.assertNotIn("现在 12:00。", contents)   # 还没生成的当然没有


class AgentModelConfigTest(unittest.TestCase):
    """agent.json 里配的 provider / model 要透传到 LLM 调用。

    这是「后台给单个 agent 换模型」生效的最后一环：网页端 /api/chat 与 QQ
    适配层都只调 run_agent_stream，且都可能不传 provider/model，所以回退
    必须发生在循环内部，而不是各调用点各写一遍。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.join(self.tmp.name, "agents")
        os.makedirs(self.root, exist_ok=True)
        for module, attr, value in (
            (agent_store, "AGENTS_DIR", self.root),
            (agent, "trim_history", lambda h, agent_id=None, **kw: h),
            (agent, "execute_tool", lambda name, args: ""),
        ):
            p = mock.patch.object(module, attr, value)
            p.start()
            self.addCleanup(p.stop)
        agent_store.clear_cache()
        self.addCleanup(agent_store.clear_cache)
        self.seen = []

    def _write_agent(self, aid, cfg):
        d = os.path.join(self.root, aid)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "agent.json"), "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False)

    def _patch_llm(self):
        def fake(messages, **kwargs):
            self.seen.append(kwargs)
            yield "content", "好"
        p = mock.patch.object(agent, "call_llm_stream", fake)
        p.start()
        self.addCleanup(p.stop)

    def _run(self, **kwargs):
        return list(agent.run_agent_stream("你好", [], **kwargs))

    def test_agent_config_used_when_caller_silent(self):
        self._write_agent("a", {"provider": "volc", "model": "m1"})
        self._patch_llm()
        self._run(agent_id="a")
        self.assertEqual(self.seen[0]["provider"], "volc")
        self.assertEqual(self.seen[0]["model"], "m1")

    def test_caller_value_wins(self):
        """网页端下拉显式选了模型，就不该再被 agent 配置覆盖。"""
        self._write_agent("a", {"provider": "volc", "model": "m1"})
        self._patch_llm()
        self._run(agent_id="a", provider="deepseek", model="m2")
        self.assertEqual(self.seen[0]["provider"], "deepseek")
        self.assertEqual(self.seen[0]["model"], "m2")

    def test_unconfigured_agent_passes_none(self):
        """老 agent.json 没有这两个字段时必须原样传 None，行为与从前一致。"""
        self._write_agent("a", {"tools": ["read_file"]})
        self._patch_llm()
        self._run(agent_id="a")
        self.assertIsNone(self.seen[0]["provider"])
        self.assertIsNone(self.seen[0]["model"])

    def test_empty_string_counts_as_unset(self):
        """「跟随默认」传的是空串，不能因为不是 None 就跳过 agent 配置。"""
        self._write_agent("a", {"provider": "volc", "model": "m1"})
        self._patch_llm()
        self._run(agent_id="a", provider="", model="")
        self.assertEqual(self.seen[0]["provider"], "volc")

    def test_only_provider_configured(self):
        """只配了 provider 没配 model：model 留 None，交给 llm 层用该家的默认模型。"""
        self._write_agent("a", {"provider": "deepseek"})
        self._patch_llm()
        self._run(agent_id="a")
        self.assertEqual(self.seen[0]["provider"], "deepseek")
        self.assertIsNone(self.seen[0]["model"])

    def test_no_agent_id_is_safe(self):
        self._patch_llm()
        self._run()
        self.assertIsNone(self.seen[0]["provider"])


class TurnFingerprintLogTest(unittest.TestCase):
    """每次迭代都要留一行指纹日志。

    同一轮里出现重复正文时，光看落盘数据分不清是「模型自己抄了上文」还是
    「上游网关把同一份响应重放了两遍」——两个日志行的输出 hash 相同才是模型
    复读，输入规模也一致才谈得上重放。这行是事后唯一能定性的证据。
    """

    def setUp(self):
        for target, value in (
            ("trim_history", lambda h, agent_id=None, **kw: h),
            ("execute_tool", lambda name, args: "工具结果:" + name),
        ):
            p = mock.patch.object(agent, target, value)
            p.start()
            self.addCleanup(p.stop)

    def _logs(self, replies):
        fake = _ScriptedLLM(replies)
        with mock.patch.object(agent, "call_llm_stream", fake):
            with self.assertLogs("agent", level="INFO") as cm:
                list(agent.run_agent_stream("你好", []))
        return [l for l in cm.output if "[turn]" in l]

    def test_one_line_per_iteration(self):
        # 第一轮带工具调用才会继续迭代，第二轮才是收尾的正文
        lines = self._logs(["[[TOOL:get_time]][[/TOOL]]", "好了"])
        self.assertEqual(len(lines), 2)
        self.assertIn("#1", lines[0])
        self.assertIn("#2", lines[1])

    def test_line_carries_input_size_output_size_and_hash(self):
        lines = self._logs(["一段话"])
        self.assertRegex(lines[0], r"输入=\d+条/\d+字")
        self.assertRegex(lines[0], r"输出=\d+字")
        self.assertRegex(lines[0], r"hash=[0-9a-f]{8}")

    def test_same_text_same_hash_other_text_other_hash(self):
        same = "重复的话[[TOOL:get_time]][[/TOOL]]"
        lines = self._logs([same, same, "换了说法"])
        hashes = [re.search(r"hash=([0-9a-f]{8})", l).group(1)
                  for l in lines]
        self.assertEqual(hashes[0], hashes[1])
        self.assertNotEqual(hashes[0], hashes[2])

    def test_tool_names_are_listed(self):
        lines = self._logs(["[[TOOL:get_time]][[/TOOL]]", "好了"])
        self.assertIn("get_time", lines[0])
        self.assertRegex(lines[1], r"工具=-$")


if __name__ == "__main__":
    unittest.main()
