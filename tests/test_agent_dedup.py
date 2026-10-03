# -*- coding: utf-8 -*-
"""一次 run 内的「生图调用闸」（app/agent.py）。

两条规则（2026-10-04 用户要求，生图 agent 不是聊天机器人）：

  ① **一次请求只提交一张**：本 run 里第一次 generate_image 放行，之后**所有**
     生图调用一律拦下——**不管参数是否相同**。模型每轮重提都会改写几个字
     （实测「下面那格」→「下面那一格」），参数精确比对拦不住。
  ② **提交成功即掐断循环**：图一进队列就收工，不再让模型迭代。实测模型会
     连出 5 轮「收到，就一条…」，群里就是 5 段复读；图其实第一轮就提交了。
     模型本轮已吐出的正文照发（那几句「排上队了、前面还有 N 张」正是要留的）。

历史（2026-09-30，群 1041079621）：一句话出了 **2 张**图。证据链：
  · 日志里两条 `NAI 请求生成` 的 **seed 不同** ⇒ 两次独立的 _generate_image；
  · 会话文件里同一条工具块**连着发了两遍**，第二遍**一个字正文都没有**；
  · `[turn]` 行同一次 run 出现两条 `工具=generate_image`，且两条 **hash 不同**。
根因是**文本协议**没有「本轮已调过这个工具」的状态 + 循环里零去重。当时只补了
「参数完全相同才拦」的精确去重（`_DEDUP_TOOLS` / `_call_key`，仍是第一道闸），
但**挡不住改写措辞的重提**——所以 2026-10-04 又加了「本 run 一次」这道粗闸，
并让提交成功的轮次直接掐断循环。

这个文件钉住这几件事：
  · 提交成功 → 循环立刻停（LLM 不再被调第二次）；
  · 一次 run 只放行一张，第二个生图调用一律拦；
  · 拦下不等于禁言——模型本轮正文照发；
  · 别的工具（send_sticker 等）不受影响，可跨迭代重复；
  · 作用域是**一次 run**，下一条用户消息重新放行。
"""

import json
import unittest
from unittest import mock

import app.agent as agent
import app.comfy_status as comfy_status
import app.config as config

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
    """按脚本依次产出回复，模拟 call_llm_stream 的 (kind, text) 契约。"""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0

    def __call__(self, messages, **kwargs):
        self.calls += 1
        if not self.replies:
            yield "content", "（脚本已用尽）"
            return
        item = self.replies.pop(0)
        if isinstance(item, list):
            for pair in item:
                yield pair
        elif isinstance(item, tuple):
            yield item
        else:
            yield "content", item


def _block(prompt, **extra):
    """拼一个 generate_image 工具块（extra 里的 None 会真的序列化成 null）。"""
    args = {"prompt": prompt}
    args.update(extra)
    return "[[TOOL:generate_image]]" + json.dumps(args) + "[[/TOOL]]"


class _DedupRunner(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(agent, "trim_history",
                              lambda h, agent_id=None, **kw: h)
        p.start()
        self.addCleanup(p.stop)
        # execute_tool 换成记录器：这个文件的核心断言就是「到底执行了几次」
        self.executed = []

        def _fake_exec(name, args):
            self.executed.append((name, args))
            return "工具结果:" + name

        p = mock.patch.object(agent, "execute_tool", _fake_exec)
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(config, "MAX_TURNS", 10)
        p.start()
        self.addCleanup(p.stop)
        # 默认：这个 agent 有生图工具
        p = mock.patch.object(agent.agent_store, "allows_tool",
                              lambda aid, name: True)
        p.start()
        self.addCleanup(p.stop)
        self.history = []

    def _patch_llm(self, replies):
        fake = _ScriptedLLM(replies)
        p = mock.patch.object(agent, "call_llm_stream", fake)
        p.start()
        self.addCleanup(p.stop)
        return fake

    def _collect(self, user_input="画一个"):
        return list(agent.run_agent_stream(user_input, self.history))

    def _results(self):
        return [m for m in self.history if m.get("role") == "tool_result"]

    def _blocked(self):
        """被生图闸拦下的那些 tool_result。"""
        return [m for m in self._results()
                if "没有执行" in (m.get("content") or "")]

    def _names(self):
        return [n for n, _ in self.executed]

    def _assistant_texts(self, events):
        return [e["content"] for e in events if e["type"] == "assistant"]


class SubmitStopsTheLoopTest(_DedupRunner):
    """规则②：生图提交成功 → 立刻掐断整个循环，LLM 不再被调。"""

    def test_llm_is_not_called_again_after_a_submission(self):
        """复刻现场：模型本来会在第二轮又提交一遍、又吐一段「收到」。"""
        fake = self._patch_llm([
            "233，排上了\n" + _block("yamada ryo", skill="nai"),
            _block("yamada ryo", skill="nai"),   # 旧现场：第二轮又提交
            "又一段",
        ])
        self._collect()

        self.assertEqual(len(self.executed), 1)      # 只真跑了一次
        self.assertEqual(fake.calls, 1)              # LLM 只调一次 → 没有第二轮复读
        self.assertEqual(self._blocked(), [])        # 后两段脚本根本没机会跑

    def test_the_reply_before_the_submission_still_goes_out(self):
        """掐断 ≠ 禁言：模型提交前说的那句（「排上队了、前面还有 N 张」）照发。"""
        self._patch_llm(["233，排上了，前面还有 1 张\n" + _block("1girl"),
                         "不该出现"])
        events = self._collect()
        texts = self._assistant_texts(events)
        self.assertIn("233，排上了，前面还有 1 张", texts)
        self.assertNotIn("不该出现", texts)

    def test_a_tool_only_turn_still_stops(self):
        """本轮只调工具、一个字正文都没有：照样掐断，且不出 assistant 事件。"""
        fake = self._patch_llm([_block("1girl"), "不该出现"])
        events = self._collect()
        self.assertEqual(fake.calls, 1)
        self.assertEqual(self._assistant_texts(events), [])

    def test_repeat_call_in_the_next_iteration_never_happens(self):
        """旧用例的意图仍然成立：第二轮绝不会再执行一次生图。"""
        self._patch_llm([
            _block("1girl"),
            _block("1girl"),
            "收尾",
        ])
        self._collect()
        self.assertEqual(len(self.executed), 1)
        self.assertEqual(self._names(), ["generate_image"])


class OneImagePerRunTest(_DedupRunner):
    """规则①：一次请求只出一张——同一条消息里第二个生图调用也拦。

    旧行为「同消息内两个相同调用都放行（理由是用户可能真要两张）」已按用户
    2026-10-04 的要求收紧。
    """

    def test_second_image_in_the_same_message_is_blocked(self):
        self._patch_llm([_block("a cat") + "\n" + _block("a cat"), "收尾"])
        self._collect()
        self.assertEqual(len(self.executed), 1)
        self.assertEqual(len(self._blocked()), 1)
        self.assertIn("一次请求只允许提交一张图",
                      self._blocked()[0]["content"])

    def test_second_image_with_a_different_prompt_is_blocked_too(self):
        """参数不同也拦——模型每次重提都会改写几个字，精确比对挡不住。"""
        self._patch_llm([_block("a cat") + "\n" + _block("a dog"), "收尾"])
        self._collect()
        self.assertEqual(len(self.executed), 1)
        self.assertEqual(len(self._blocked()), 1)

    def test_second_image_with_a_different_skill_is_blocked_too(self):
        """同 prompt 换渠道（旧行为当合法对比）现在也拦：一轮一张。"""
        self._patch_llm([
            _block("1girl", skill="nai") + "\n" + _block("1girl"),
            "收尾",
        ])
        self._collect()
        self.assertEqual(len(self.executed), 1)
        self.assertEqual(len(self._blocked()), 1)

    def test_blocked_call_is_still_emitted(self):
        """拦下也要出流：调用方靠「事件出流」落盘（qq_bot/main 的契约）。"""
        self._patch_llm([_block("a cat") + "\n" + _block("a cat"), "收尾"])
        events = self._collect()
        self.assertEqual(
            [e["name"] for e in events if e["type"] == "tool_call"],
            ["generate_image", "generate_image"])
        self.assertIn("tool_result", [e["type"] for e in events])


class ScopeIsOneRunTest(_DedupRunner):
    """作用域是一次 run：下一条用户消息是全新一次请求，重新放行。"""

    def test_a_new_user_message_starts_a_fresh_run(self):
        """「再画一张」必须能画出来——这是生图机器人最基本的用法。"""
        block = _block("1girl")
        self._patch_llm([block, "第一张"])
        self._collect("画一个")
        self._patch_llm([block, "第二张"])
        self._collect("再画一张")
        self.assertEqual(len(self.executed), 2)
        self.assertEqual(self._blocked(), [])

    def test_the_same_prompt_with_a_new_skill_is_allowed_in_a_new_run(self):
        """跨请求换渠道重画仍然放行（闸在 run 级重置）。"""
        self._patch_llm([_block("1girl", skill="nai"), "nai 版"])
        self._collect("画一个")
        self._patch_llm([_block("1girl"), "默认版"])
        self._collect("换默认渠道再来一张")
        self.assertEqual([a.get("skill") for _, a in self.executed],
                         ["nai", None])
        self.assertEqual(self._blocked(), [])


class KeyNormalisationTest(_DedupRunner):
    """`_call_key` 的归一化口径（第一道精确去重闸仍然用它）。"""

    def test_key_normalises_empty_values(self):
        """显式 null / 空串 与「不传」等价——都得算同一个调用。

        判据来自 generate_image._generate_image：`if not skill:` 与
        `if str(source_image or "").strip()`，所以 null / "" 和不传是同一件事。
        """
        a = agent._call_key("generate_image",
                            {"prompt": "x", "skill": "nai", "source_image": ""})
        b = agent._call_key("generate_image", {"skill": "nai", "prompt": "x"})
        self.assertEqual(a, b)

    def test_false_and_zero_are_meaningful_and_are_kept(self):
        """`False` / `0` 是**有意义的值**，只有 None / "" 才当空值丢掉。

        这是 `_call_key` 的通用口径（跟参数名无关）：`denoise=0` 是真实存在的
        合法值（0 表示强度为零），`False` 用任意键名演示同一条规则。
        （历史：原来这里用 `use_character=False` 举例，那个参数随角色底模机制
        在 2026-09-30 下线了。）
        """
        a = agent._call_key("generate_image", {"prompt": "x"})
        b = agent._call_key("generate_image", {"prompt": "x", "denoise": 0})
        c = agent._call_key("generate_image", {"prompt": "x", "flag": False})
        self.assertNotEqual(a, b)
        self.assertNotEqual(a, c)
        self.assertIn("denoise", b)
        self.assertIn("false", c.lower())

    def test_non_dedup_tools_have_no_key(self):
        for name in ("send_sticker", "collect_sticker", "read_file"):
            self.assertIsNone(agent._call_key(name, {"index": 1}), name)

    def test_unserialisable_args_do_not_raise(self):
        """参数里有 json 序列化不了的东西：宁可不拦，也不能抛错断掉整轮。"""
        self.assertIsNone(agent._call_key("generate_image", {"prompt": {1, 2}}))


class WhitelistInteractionTest(_DedupRunner):
    def test_a_rejected_call_is_not_marked_as_done(self):
        """白名单拒掉的调用**不算提交过**——否则后续会给一句
        「已经提交过了」的假话，而实际上一张都没提交，也不该掐断循环。"""
        with mock.patch.object(agent.agent_store, "allows_tool",
                               lambda aid, name: False):
            self._patch_llm([
                _block("1girl"),
                _block("1girl"),
                "画不了",
            ])
            self._collect()
        self.assertEqual(self.executed, [])
        self.assertEqual(self._blocked(), [])        # 不是「重复提交」的文案
        self.assertEqual(
            sum("不可用" in (m.get("content") or "") for m in self._results()),
            2)


class OtherToolsAreNotDedupedTest(_DedupRunner):
    def test_send_sticker_can_be_called_twice_across_iterations(self):
        """只有生图被限一次；别的工具照旧可以跨迭代重复调，循环也不会被掐断。"""
        block = '[[TOOL:send_sticker]]{"index": 3}[[/TOOL]]'
        fake = self._patch_llm([block, block, "好了"])
        self._collect()
        self.assertEqual(self._names(), ["send_sticker", "send_sticker"])
        self.assertEqual(self._blocked(), [])
        self.assertEqual(fake.calls, 3)     # 循环跑到脚本用尽，没被掐断


class RepeatNoteWordingTest(unittest.TestCase):
    """文案必须**终止**——写「再试一次 / 下条补 / 重新调」等于给模型下新指令。

    这是项目里踩过两次的坑（见 skill「回执文案本身就是指令」）：
    tool_result 会被 _history_for_llm 改写成 role:"user" + "[工具结果] " 前缀，
    对模型来说等价于**环境刚说的一句话**，也就是一条指令。
    """

    _TERMINATORS = ("再试一次", "下条", "重试", "重新调", "补一张")

    def test_note_tells_the_model_to_stop_calling(self):
        note = agent._REPEAT_CALL_NOTE
        self.assertIn("不要再调 generate_image", note)
        for bad in self._TERMINATORS:
            self.assertNotIn(bad, note)

    def test_note_does_not_leak_jargon_to_the_user(self):
        """群里不该出现「去重 / 重复提交被拦」这种技术话。"""
        note = agent._REPEAT_CALL_NOTE
        self.assertIn("别跟对方解释", note)

    def test_image_note_tells_the_model_to_stop_calling(self):
        note = agent._REPEAT_IMAGE_NOTE
        self.assertIn("别再调 generate_image", note)
        for bad in self._TERMINATORS:
            self.assertNotIn(bad, note)

    def test_image_note_states_the_one_image_rule(self):
        note = agent._REPEAT_IMAGE_NOTE
        self.assertIn("一次请求只允许提交一张图", note)


if __name__ == "__main__":
    unittest.main()
