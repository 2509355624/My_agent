# -*- coding: utf-8 -*-
"""同一次 run 内的「重复调用」去重（app/agent.py::_DEDUP_TOOLS / _call_key）。

现场（2026-09-30 群 1041079621）：一句话出了 **2 张**图。
证据链：
  · 日志里两条 `NAI 请求生成` 的 **seed 不同** ⇒ 两次独立的 _generate_image
    （seed 是 _generate_image 里现 random.randint 的）；
  · 会话文件里同一条工具块**连着发了两遍**，第二遍**一个字正文都没有**；
  · `[turn]` 行同一次 run 出现两条 `工具=generate_image`，且两条 **hash 不同**
    ⇒ 不是上游网关把同一份响应重放了两遍。

根因是**文本协议**没有「本轮已调过这个工具」的状态 + 循环里零去重，
模型发几次就真跑几次。这个文件钉住那道代码级硬闸的行为：

  · **跨迭代**的相同调用只执行一次（拦下的那次回一句终止性文案）；
  · **同一条消息里**的相同调用照旧放行——用户可能真要两张；
  · 同一个 prompt 换 **skill** 是合法用法，绝不能误伤；
  · 只对 _DEDUP_TOOLS 里的工具生效，别的工具不受影响；
  · 去重的作用域是**一次 run**，下一条用户消息重新放行。
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
        """被去重闸拦下的那些 tool_result。"""
        return [m for m in self._results()
                if "没有执行" in (m.get("content") or "")]

    def _names(self):
        return [n for n, _ in self.executed]


class CrossIterationRepeatTest(_DedupRunner):
    """核心用例：上一轮刚提交过，下一轮又发一遍同一个调用。"""

    def test_repeat_call_in_the_next_iteration_is_not_executed(self):
        """复刻现场：山田凉那张被提交了两遍，第二遍一个字正文都没有。"""
        self._patch_llm([
            "233，凉再来一张\n" + _block("yamada ryo", skill="nai"),
            _block("yamada ryo", skill="nai"),      # ← 重复提交
            "233，凉在排了 出来就发",
        ])
        events = self._collect()

        self.assertEqual(len(self.executed), 1)      # 只真跑了一次
        self.assertEqual(self._names(), ["generate_image"])
        self.assertEqual(len(self._blocked()), 1)    # 第二次被拦

        # 拦下也要出流：调用方靠「事件出流」落盘（qq_bot/main 的契约）
        self.assertIn("tool_result", [e["type"] for e in events])
        # 两次调用请求都如实出流了（拦的是执行，不是抹掉模型说过的话）
        self.assertEqual(
            [e["name"] for e in events if e["type"] == "tool_call"],
            ["generate_image", "generate_image"])

    def test_blocked_result_says_it_was_not_executed(self):
        self._patch_llm([
            _block("1girl"),
            _block("1girl"),
            "收尾",
        ])
        self._collect()
        note = self._blocked()[0]
        self.assertEqual(note["tool_name"], "generate_image")
        self.assertIn("没有执行", note["content"])

    def test_the_model_can_still_finish_after_being_blocked(self):
        """拦下不等于把这一轮弄死——模型照旧能收尾说话。"""
        self._patch_llm([
            _block("1girl"),
            _block("1girl"),
            "233，那张在跑，出来就发",
        ])
        events = self._collect()
        self.assertIn("233，那张在跑，出来就发",
                      [e["content"] for e in events
                       if e["type"] == "assistant"])

    def test_third_identical_call_is_blocked_too(self):
        self._patch_llm([
            _block("1girl"),
            _block("1girl"),
            _block("1girl"),
            "收尾",
        ])
        self._collect()
        self.assertEqual(len(self.executed), 1)
        self.assertEqual(len(self._blocked()), 2)


class SameMessageDuplicatesTest(_DedupRunner):
    """同一条 assistant 消息里发两个一模一样的调用 = 用户可能真要两张。"""

    def test_two_identical_calls_in_one_message_both_run(self):
        self._patch_llm([
            _block("a cat") + "\n" + _block("a cat"),
            "画好了",
        ])
        self._collect()
        self.assertEqual(len(self.executed), 2)
        self.assertEqual(self._blocked(), [])

    def test_in_message_duplicate_then_a_third_one_next_turn_is_blocked(self):
        """消息内放行 ≠ 下一轮还能再发一遍。"""
        self._patch_llm([
            _block("a cat") + "\n" + _block("a cat"),
            _block("a cat"),
            "收尾",
        ])
        self._collect()
        self.assertEqual(len(self.executed), 2)      # 消息内那两张跑了
        self.assertEqual(len(self._blocked()), 1)    # 跨迭代那张被拦


class LegitimateReuseIsNotBlockedTest(_DedupRunner):
    """别把合法用法一起拦掉——这是这道闸最容易误伤的地方。"""

    def test_same_prompt_with_a_different_skill_is_allowed(self):
        """实测模型会主动用同 prompt 换动漫渠道再跑一张，好跟 NAI 对比。"""
        self._patch_llm([
            _block("1girl, blue twin-tail", skill="nai"),
            _block("1girl, blue twin-tail"),      # 换默认渠道
            "两张对比着看",
        ])
        self._collect()
        self.assertEqual(len(self.executed), 2)
        self.assertEqual(self._blocked(), [])
        self.assertEqual([a.get("skill") for _, a in self.executed],
                         ["nai", None])

    def test_a_different_prompt_is_allowed(self):
        self._patch_llm([
            _block("a cat"),
            _block("a dog"),
            "两张都在跑",
        ])
        self._collect()
        self.assertEqual(len(self.executed), 2)
        self.assertEqual(self._blocked(), [])

    def test_same_prompt_different_lora_is_allowed(self):
        self._patch_llm([
            _block("1girl", lora="a.safetensors:0.8"),
            _block("1girl", lora="b.safetensors:0.5"),
            "收尾",
        ])
        self._collect()
        self.assertEqual(len(self.executed), 2)
        self.assertEqual(self._blocked(), [])


class KeyNormalisationTest(_DedupRunner):
    def test_key_order_and_empty_values_do_not_defeat_the_guard(self):
        """键序不同、显式 null / 空串 与「不传」等价——都得算同一个调用。

        判据来自 generate_image._generate_image：`if not skill:` 与
        `if str(source_image or "").strip()`，所以 null / "" 和不传是同一件事。
        """
        reversed_order = ('[[TOOL:generate_image]]'
                          '{"skill": "nai", "prompt": "1girl"}[[/TOOL]]')
        self._patch_llm([
            _block("1girl", skill="nai", denoise=None, source_image=""),
            reversed_order,
            "收尾",
        ])
        self._collect()
        self.assertEqual(len(self.executed), 1)
        self.assertEqual(len(self._blocked()), 1)

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


class ScopeIsOneRunTest(_DedupRunner):
    def test_a_new_user_message_starts_a_fresh_run(self):
        """下一条用户消息是全新一次 run —— 「再画一张」必须能画出来。"""
        block = _block("1girl")
        self._patch_llm([block, "第一张"])
        self._collect("画一个")
        self._patch_llm([block, "第二张"])
        self._collect("再画一张")
        self.assertEqual(len(self.executed), 2)
        self.assertEqual(self._blocked(), [])


class WhitelistInteractionTest(_DedupRunner):
    def test_a_rejected_call_is_not_marked_as_done(self):
        """白名单拒掉的调用**不算执行过**——否则下一轮会给一句
        「已经提交过了」的假话，而实际上一张都没提交。"""
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
        block = '[[TOOL:send_sticker]]{"index": 3}[[/TOOL]]'
        self._patch_llm([block, block, "好了"])
        self._collect()
        self.assertEqual(self._names(), ["send_sticker", "send_sticker"])
        self.assertEqual(self._blocked(), [])


class RepeatNoteWordingTest(unittest.TestCase):
    """文案必须**终止**——写「再试一次 / 下条补 / 重新调」等于给模型下新指令。

    这是项目里踩过两次的坑（见 skill「回执文案本身就是指令」）：
    tool_result 会被 _history_for_llm 改写成 role:"user" + "[工具结果] " 前缀，
    对模型来说等价于**环境刚说的一句话**，也就是一条指令。
    """

    def test_note_tells_the_model_to_stop_calling(self):
        note = agent._REPEAT_CALL_NOTE
        self.assertIn("不要再调 generate_image", note)
        for bad in ("再试一次", "下条", "重试", "重新调", "补一张"):
            self.assertNotIn(bad, note)

    def test_note_does_not_leak_jargon_to_the_user(self):
        """群里不该出现「去重 / 重复提交被拦」这种技术话。"""
        note = agent._REPEAT_CALL_NOTE
        self.assertIn("别跟对方解释", note)


if __name__ == "__main__":
    unittest.main()
