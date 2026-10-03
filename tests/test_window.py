# -*- coding: utf-8 -*-
"""会话压缩（memory.trim_window）与其配套链路的测试。

2026-09-29 起判据由「轮数」改成「token 预算」：到 CONTEXT_BUDGET（默认 5 万）
就压。原来按 CONTEXT_MAX_TURNS=100 轮开窗，实测群会话能攒到 8 万 token
（534 条 / 16.6 万字），单轮又慢又贵。

四件事：
1. 压缩：群会话超出预算时只留装得下的最近若干轮，更老的转交 longterm
   （mock，不真调 API）；
2. 去重与攒批：save_history 一次回复内会被调用多次，同一批滚出窗口的
   消息只允许摘要一次；滚出消息攒够 _DIGEST_BATCH_MIN 才摘一次，
   不然记忆库全是 2 条消息的碎片；
3. 回退：非群会话（网页 / 私聊）没有记忆库，走 trim_history 老路，且
   **必须原地收缩**调用方手上的 list（压缩不幂等，见 trim_window 注释）；
4. 另外覆盖 longterm.digest_messages_async（窗口摘要落盘）与 web_search
   的结果截断（tool_result 永久占历史，必须掐）。

token 估算口径见 memory.estimate_tokens：汉字 ×0.6 + 其他 ×0.3，每条消息
另加 16 的角色开销。下面的 _body/_turn 就是按这个口径造的「稳定尺寸砖块」。

2026-09-30 起群会话走**滞回**（`memory.WINDOW_TRIM_HIGH/LOW`）：估算涨到
`budget×1.3` 才裁、一次裁到 `budget×0.6`。原来的「到预算就裁、裁到刚好
装得下」会让**每一轮都裁一次**，消息数组每轮重建 → 系统头之后的前缀每轮
都不一样 → 服务端前缀缓存只剩系统头能命中（实测签名
`[cache] 命中 6144 / 25xxx ≈ 24%`，占全部未命中 token 的 28%）。
下面的 `_KEEP` 就是「裁一次之后留几轮」，`_NO_TRIM` 是「还在高水位以下、
一个字节都不许动」的轮数。
"""

import json
import os
import tempfile
import threading
import unittest
from unittest import mock

import app.agents as agents
import app.memory as memory
import app.longterm as longterm
from app import usage
from app.tools.normal import web_search


# ── 造数据：每轮固定 270 token，方便把预算算清楚 ────────────────
# _CHARS 个汉字（末尾换成序号保证内容唯一——去重指纹靠 (role, content)，
# 内容全一样的话第二批消息会被当成重复全部吃掉）。
_CHARS = 200


def _body(i):
    """第 i 轮的正文：唯一、且长度稳定（序号占 1~2 字符，估算是定值）。"""
    return "字" * (_CHARS - len(str(i))) + str(i)


def _turn(i):
    """一轮 = user + assistant，各 135 token（119 内容 + 16 开销）。"""
    return [{"role": "user", "content": _body(i)},
            {"role": "assistant", "content": _body(i)}]


def _history(turns, system=True):
    h = []
    if system:
        h.append({"role": "system", "content": "人设"})     # 17 token
    for i in range(turns):
        h.extend(_turn(i))
    return h


# 每轮 270 token、系统 17 token：
#   17 + 270×11 = 2987 < 3000  → 连预算都没到，绝不该裁
#   17 + 270×14 = 3797 < 3900  → 过了预算但没到高水位（3000×1.3），仍不该裁
#   17 + 270×15 = 4067 ≥ 3900  → 到高水位，裁
#   17 + 270×6  = 1637 ≤ 1800  → 裁的目标（3000×0.6），留 6 轮
_BUDGET = 3000
_NO_TRIM = 11
_KEEP = 6
_HIGH_TURNS = 15
_HIGH = 1.3
_LOW = 0.6


class _ResetState:
    """清掉模块级去重/攒批状态，测试之间互不污染。"""

    def setUp(self):
        super().setUp()
        memory._digest_seen.clear()
        memory._digest_pending.clear()
        self.addCleanup(memory._digest_seen.clear)
        self.addCleanup(memory._digest_pending.clear)
        # usage._last_hit 是模块级的「这条会话线最近热不热」，跨用例会互相
        # 污染（trim_window 的非群分支现在读它）。不在这里清掉的话，
        # test_private_session_falls_back_to_trim_history 会读到别的用例
        # 留下的命中率，断言随机翻车。
        usage._last_hit.clear()
        self.addCleanup(usage._last_hit.clear)
        # ⚠️ 别让 usage.record 落盘：它写的是 BASE_DIR/usage/<今天>.json，
        # 也就是**生产用量账本**（test_usage.py 有 patch BASE_DIR，这里没有）。
        # 首次 record 距 _last_flush=0.0 已超 10 秒 → 必触发 flush。
        p = mock.patch.object(usage, "flush")
        p.start()
        self.addCleanup(p.stop)


class TrimWindowTest(_ResetState, unittest.TestCase):
    def test_turn_estimate_is_stable(self):
        """钉住估算口径：下面的轮数/预算换算全建立在这个前提上。"""
        self.assertEqual(memory.estimate_messages(_turn(3)), 270)
        self.assertEqual(memory.estimate_messages(_turn(99)), 270)
        self.assertEqual(memory.estimate_messages(_history(0)), 17)

    def test_high_water_keeps_recent_turns(self):
        # 35 轮 ≈ 9467 token，超了高水位 3900 → 从最新往回装到 1800，留 6 轮
        with mock.patch.object(memory, "_DIGEST_BATCH_MIN", 1), \
             mock.patch.object(longterm, "digest_messages_async") as dig:
            out = memory.trim_window(_history(35), "qq", "group_9",
                                     budget=_BUDGET)
        roles = [m["role"] for m in out]
        self.assertEqual(roles[0], "system")
        self.assertEqual(roles.count("user"), _KEEP)      # 只剩最近 6 轮
        self.assertEqual(out[1]["content"], _body(29))    # 前 29 轮被摘走
        self.assertEqual(out[-1]["content"], _body(34))
        dig.assert_called_once()
        args = dig.call_args[0]
        self.assertEqual(args[0], "qq")
        self.assertEqual(args[1], "9")
        self.assertEqual(len(args[2]), 58)                # 29 轮 = 58 条消息

    def test_result_fits_the_low_water(self):
        """裁完必须落到低水位——留出的余量就是「接下来多少轮是纯追加」。"""
        with mock.patch.object(memory, "_DIGEST_BATCH_MIN", 1), \
             mock.patch.object(longterm, "digest_messages_async"):
            out = memory.trim_window(_history(200), "qq", "group_9",
                                     budget=_BUDGET)
        self.assertLessEqual(memory.estimate_messages(out),
                             _BUDGET * memory.WINDOW_TRIM_LOW)

    def test_within_budget_untouched(self):
        # 11 轮 = 2987 < 3000：原样返回，连对象都不换（保住热前缀）
        h = _history(_NO_TRIM)
        with mock.patch.object(longterm, "digest_messages_async") as dig:
            out = memory.trim_window(h, "qq", "group_9", budget=_BUDGET)
        self.assertIs(out, h)
        self.assertEqual(len(out), 23)
        dig.assert_not_called()

    def test_just_over_budget_still_untouched(self):
        """过了预算但没到高水位 → 一个字节都不许动。

        这是本次修复的核心：原实现到预算就裁，而估算偏大 + 尾巴不计入，
        裁完真实 prompt 仍贴着预算 → **每轮都裁一次** → 消息数组每轮重建
        → 前缀缓存只剩系统头能命中（`命中 6144 / 25xxx ≈ 24%`）。
        """
        h = _history(14)                                  # 3797 < 3900
        self.assertGreater(memory.estimate_messages(h), _BUDGET)
        with mock.patch.object(longterm, "digest_messages_async") as dig:
            out = memory.trim_window(h, "qq", "group_9", budget=_BUDGET)
        self.assertIs(out, h)
        dig.assert_not_called()

    def test_at_high_water_trims(self):
        # 15 轮 = 4067 ≥ 3900：到高水位才裁，且一次裁到低水位
        with mock.patch.object(memory, "_DIGEST_BATCH_MIN", 1), \
             mock.patch.object(longterm, "digest_messages_async") as dig:
            out = memory.trim_window(_history(_HIGH_TURNS), "qq", "group_9",
                                     budget=_BUDGET)
        self.assertEqual(len(out), 1 + _KEEP * 2)
        self.assertEqual(out[1]["content"], _body(_HIGH_TURNS - _KEEP))
        self.assertEqual(len(dig.call_args[0][2]), (_HIGH_TURNS - _KEEP) * 2)

    def test_trim_is_logged(self):
        """裁剪动作必须留痕：它一次作废整段前缀缓存，`[cache]` 行看不出来。"""
        with mock.patch.object(memory, "_DIGEST_BATCH_MIN", 1), \
             mock.patch.object(longterm, "digest_messages_async"), \
             self.assertLogs("memory", level="INFO") as cap:
            memory.trim_window(_history(35), "qq", "group_9", budget=_BUDGET)
        hit = [r for r in cap.output if "裁剪历史" in r]
        self.assertEqual(len(hit), 1)
        self.assertIn("70 条 → 12 条", hit[0])

    def test_last_turn_always_kept(self):
        """单轮就超预算也得留一轮，否则窗口空掉、模型看不见刚说的话。"""
        h = [{"role": "system", "content": "人设"}] + _turn(0)
        h[-1] = {"role": "assistant", "content": "字" * 20000}
        with mock.patch.object(memory, "_DIGEST_BATCH_MIN", 1), \
             mock.patch.object(longterm, "digest_messages_async"):
            out = memory.trim_window(h, "qq", "group_9", budget=_BUDGET)
        self.assertEqual(out[-1]["content"], "字" * 20000)

    def test_zero_disables(self):
        with mock.patch.object(longterm, "digest_messages_async") as dig:
            out = memory.trim_window(_history(35), "qq", "group_9", max_turns=0)
        self.assertEqual(len(out), 71)
        dig.assert_not_called()

    def test_private_session_falls_back_to_trim_history(self):
        """私聊 / 网页没有记忆库：不摘要、走 trim_history 的 token 预算老路。"""
        with mock.patch.object(longterm, "digest_messages_async") as dig, \
             mock.patch.object(memory, "trim_history",
                               return_value=[{"role": "user", "content": "旧路"}]) as th:
            out = memory.trim_window(_history(35), "qq", "user_123",
                                     budget=_BUDGET)
        dig.assert_not_called()
        th.assert_called_once()
        self.assertEqual(out, [{"role": "user", "content": "旧路"}])
        # 这条会话线还没记过命中率 → 退回老行为（给 0 = 到警戒线就压）
        self.assertEqual(th.call_args[1]["usage"]["hit_rate"], 0.0)
        self.assertEqual(th.call_args[1]["budget"], _BUDGET)

    def test_private_session_passes_real_hit_rate_through(self):
        """会话线已知很热时，必须把**真实命中率**交给 trim_history。

        2026-10-03 之前这里硬塞 0.0，等于把 LOW_HIT_RATE=0.3 那个「命中率高
        就别压」的保护关掉：私聊实测命中 88~94%，却照样在 0.75×预算就压，
        而每次压缩都把摘要插在系统头正后面 → **整段前缀缓存全量作废**。
        群聊侧早就不裁了（滞回），私聊压缩是仅存的前缀重写源。
        """
        # 走真实路径：qq_bot 每轮用 scope(session_key) 包住调用，llm 记用量时
        # 顺手把命中率写进 usage._last_hit
        with usage.scope("user_123"):
            usage.record(910, 90)
        with mock.patch.object(memory, "trim_history", return_value=[]) as th:
            memory.trim_window(_history(35), "qq", "user_123", budget=_BUDGET)
        self.assertAlmostEqual(th.call_args[1]["usage"]["hit_rate"], 0.91)

    def test_web_session_without_key_falls_back(self):
        with mock.patch.object(longterm, "digest_messages_async") as dig, \
             mock.patch.object(memory, "trim_history", return_value=[]) as th:
            memory.trim_window(_history(35), "main", None, budget=_BUDGET)
        dig.assert_not_called()
        th.assert_called_once()

    def test_digest_failure_does_not_break_window(self):
        """longterm 起不来线程也不影响压缩——历史不能因为后台失败继续膨胀。"""
        with mock.patch.object(memory, "_DIGEST_BATCH_MIN", 1), \
             mock.patch.object(longterm, "digest_messages_async",
                               side_effect=OSError("boom")):
            out = memory.trim_window(_history(35), "qq", "group_9",
                                     budget=_BUDGET)
        self.assertEqual(out[1]["content"], _body(29))


class InPlaceShrinkTest(_ResetState, unittest.TestCase):
    """非群会话压缩后必须**原地收缩**调用方手上的 list。

    压缩不幂等：save_history 一轮里被调很多次，副本式裁剪下第一次写了瘦文件、
    水位也记上了，同轮后续保存又被冷却挡住 → 把胖历史原样写回，压缩成果被
    冲掉（实测私聊压完 547 条回弹）。
    """

    def test_private_trim_shrinks_caller_list(self):
        h = _history(35)
        with mock.patch.object(memory, "trim_history",
                               return_value=[{"role": "user", "content": "摘要"}]):
            memory.trim_window(h, "qq", "user_123", budget=_BUDGET)
        self.assertEqual(h, [{"role": "user", "content": "摘要"}])

    def test_unchanged_result_keeps_the_same_object(self):
        h = _history(35)
        with mock.patch.object(memory, "trim_history", return_value=h):
            out = memory.trim_window(h, "qq", "user_123", budget=_BUDGET)
        self.assertIs(out, h)


class DigestBatchTest(_ResetState, unittest.TestCase):
    """滚出消息攒批：攒够阈值才摘要一次，避免碎片记忆。"""

    def test_below_threshold_buffers_without_digest(self):
        # 15 轮滚掉 9 轮 = 18 条 < 40，先攒着
        with mock.patch.object(longterm, "digest_messages_async") as dig:
            memory.trim_window(_history(_HIGH_TURNS), "qq", "group_9",
                               budget=_BUDGET)
        dig.assert_not_called()
        self.assertEqual(len(memory._digest_pending[("qq", "9")]), 18)

    def test_crossing_threshold_flushes_whole_batch(self):
        with mock.patch.object(longterm, "digest_messages_async") as dig:
            memory.trim_window(_history(_HIGH_TURNS), "qq", "group_9",
                               budget=_BUDGET)
            # 再滚 58 条，其中 18 条与上批重合被去重，攒批总数 58 → 触发
            memory.trim_window(_history(35), "qq", "group_9", budget=_BUDGET)
        dig.assert_called_once()
        batch = dig.call_args[0][2]
        self.assertEqual(len(batch), 58)                 # 18 + 40（去重后），一次整批
        self.assertEqual(batch[0]["content"], _body(0))  # 顺序保持旧→新
        self.assertEqual(batch[-1]["content"], _body(28))
        self.assertEqual(memory._digest_pending.get(("qq", "9")), [])

    def test_big_initial_roll_flushes_immediately(self):
        with mock.patch.object(longterm, "digest_messages_async") as dig:
            memory.trim_window(_history(60), "qq", "group_9", budget=_BUDGET)
        dig.assert_called_once()
        self.assertEqual(len(dig.call_args[0][2]), 108)  # 54 轮 = 108 条

    def test_groups_buffered_separately(self):
        with mock.patch.object(longterm, "digest_messages_async") as dig:
            memory.trim_window(_history(_HIGH_TURNS), "qq", "group_9",
                               budget=_BUDGET)
            memory.trim_window(_history(_HIGH_TURNS), "qq", "group_8",
                               budget=_BUDGET)
        dig.assert_not_called()
        self.assertEqual(len(memory._digest_pending[("qq", "9")]), 18)
        self.assertEqual(len(memory._digest_pending[("qq", "8")]), 18)


class DigestDedupTest(_ResetState, unittest.TestCase):
    """save_history 一次回复内多次落盘，同一批消息只许摘要一次。"""

    def test_same_batch_digests_once(self):
        h = _history(35)
        with mock.patch.object(memory, "_DIGEST_BATCH_MIN", 1), \
             mock.patch.object(longterm, "digest_messages_async") as dig:
            memory.trim_window(h, "qq", "group_9", budget=_BUDGET)
            memory.trim_window(h, "qq", "group_9", budget=_BUDGET)
            memory.trim_window(h, "qq", "group_9", budget=_BUDGET)
        dig.assert_called_once()
        self.assertEqual(dig.call_args[0][2][0]["content"], _body(0))

    def test_growth_inside_last_turn_only_digests_fresh(self):
        """最后一轮在轮尾继续长（assistant/tool 续在后面），不重复摘要。"""
        h = _history(35)
        with mock.patch.object(memory, "_DIGEST_BATCH_MIN", 1), \
             mock.patch.object(longterm, "digest_messages_async") as dig:
            memory.trim_window(h, "qq", "group_9", budget=_BUDGET)
            self.assertEqual(dig.call_count, 1)
            self.assertEqual(dig.call_args_list[0][0][2][0]["content"], _body(0))
            # 轮尾继续长：最后一轮胖到把 keep 从 6 挤到 5
            h.append({"role": "assistant", "content": "字" * 300})
            memory.trim_window(h, "qq", "group_9", budget=_BUDGET)
            memory.trim_window(h, "qq", "group_9", budget=_BUDGET)
        # 轮尾变长只把更老的轮挤出去：第二批只含新滚出的 turn29，已摘过的
        # 消息（turn0 起）一条都不重摘。
        self.assertEqual(dig.call_count, 2)
        self.assertEqual(dig.call_args_list[1][0][2], _turn(29))

    def test_slide_digests_only_newly_rolled(self):
        """预算滑动：新一轮把更老的挤出去时，只摘新滚出的，不重摘旧的。"""
        h = _history(20)                      # 20 轮 → 滚 turn0..13
        with mock.patch.object(memory, "_DIGEST_BATCH_MIN", 1), \
             mock.patch.object(longterm, "digest_messages_async") as dig:
            memory.trim_window(h, "qq", "group_9", budget=_BUDGET)
            h.extend(_turn(20))               # 21 轮 → 滚 turn0..14
            memory.trim_window(h, "qq", "group_9", budget=_BUDGET)
        self.assertEqual(dig.call_count, 2)
        self.assertEqual(dig.call_args[0][2], _turn(14))


class SaveHistoryIntegrationTest(unittest.TestCase):
    """save_history 落盘前真的会压缩——磁盘上的历史不再无限膨胀。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch.object(agents, "AGENTS_DIR",
                              os.path.join(self.tmp.name, "agents"))
        p.start()
        self.addCleanup(p.stop)
        agents.clear_cache()
        self.addCleanup(agents.clear_cache)
        memory._digest_seen.clear()
        memory._digest_pending.clear()
        self.addCleanup(memory._digest_seen.clear)
        self.addCleanup(memory._digest_pending.clear)

    def test_saved_file_stays_within_budget(self):
        # 显式钉住预算：不依赖 .env 的 CONTEXT_BUDGET
        h = _history(35)
        with mock.patch.object(memory, "CONTEXT_BUDGET", _BUDGET), \
             mock.patch.object(longterm, "digest_messages_async"):
            memory.save_history(h, "qq", "group_9")
        loaded = memory.load_history("qq", "group_9")
        self.assertEqual([m["role"] for m in loaded].count("user"), _KEEP)
        self.assertEqual(loaded[1]["content"], _body(29))
        self.assertLessEqual(memory.estimate_messages(loaded),
                             _BUDGET * memory.WINDOW_TRIM_LOW)

    def test_agent_budget_applies_to_the_window(self):
        """agent.json 的 context_budget 必须对 save_history 的滚窗生效。

        QQ 侧压缩走 save_history -> trim_window，那条路拿不到 agent 循环手上
        的 budget。不兜住的话 per-agent 预算是空头支票——09-29 实测群聊一直
        按全局 CONTEXT_BUDGET 滚窗，agent.json 里配了也没用。
        """
        d = os.path.join(agents.AGENTS_DIR, "qq")
        os.makedirs(d, exist_ok=True)
        # 8000 在 agents.MIN_CONTEXT_BUDGET 之上（低于下限会被归一成 0 = 继承全局）
        with open(os.path.join(d, "agent.json"), "w", encoding="utf-8") as f:
            json.dump({"context_budget": 8000}, f)
        agents.clear_cache()
        with mock.patch.object(longterm, "digest_messages_async"), \
             mock.patch.object(memory, "CONTEXT_BUDGET", 10 ** 6):
            memory.save_history(_history(39), "qq", "group_9")
        loaded = memory.load_history("qq", "group_9")
        # 全局预算被抬到 1e6；还能压到 17 轮，只可能是读了 agent.json 的 8000
        # （高水位 8000×1.3=10400，低水位 8000×0.6=4800 → 留 17 轮）
        self.assertEqual([m["role"] for m in loaded].count("user"), 17)


class ReserveTest(_ResetState, unittest.TestCase):
    """reserve：**不在 history 里、但每轮都进 prompt** 的尾部开销（状态栏 +
    群聊背景 / 长期记忆 / 表情包清单）。

    它不计入 estimate_messages，不预留的话压缩后真实 prompt 会超预算一整条
    尾巴——2026-09-29 实测群聊 prompt 37184 > 预算 30000。**当时把「每轮被
    驱逐」归因于「prompt 超出上游缓存容量」，2026-09-30 查下来是错的**：
    真正的原因是「到预算就裁、裁到刚好装得下」导致每轮都裁（见
    `WINDOW_TRIM_HIGH/LOW`）。reserve 本身仍然必须留——它决定裁完之后
    真实 prompt 还超不超预算。
    """

    def test_reserve_pushes_over_the_line(self):
        h = _history(14)                      # 3797：过了预算，但没到高水位 3900
        with mock.patch.object(memory, "_DIGEST_BATCH_MIN", 1), \
             mock.patch.object(longterm, "digest_messages_async") as dig:
            out = memory.trim_window(h, "qq", "group_9", budget=_BUDGET,
                                     reserve=200)          # 3997 > 3900 → 裁
        self.assertIsNot(out, h)
        dig.assert_called_once()

    def test_reserve_counts_toward_the_kept_window(self):
        # 尾部占 500 → 只剩 1300 装历史，最近 4 轮（1080）刚好装下，第 5 轮滚出
        with mock.patch.object(memory, "_DIGEST_BATCH_MIN", 1), \
             mock.patch.object(longterm, "digest_messages_async"):
            out = memory.trim_window(_history(35), "qq", "group_9",
                                     budget=_BUDGET, reserve=500)
        self.assertLessEqual(memory.estimate_messages(out),
                             _BUDGET * memory.WINDOW_TRIM_LOW - 500)
        self.assertEqual([m["role"] for m in out].count("user"), 4)

    def test_reserve_ignored_when_not_needed(self):
        h = _history(5)
        self.assertIs(memory.trim_window(h, "qq", "group_9",
                                         budget=_BUDGET, reserve=500), h)

    def test_negative_reserve_clamped(self):
        """传负数（调用方算错）不能把预算放大。"""
        h = _history(11)
        self.assertIs(memory.trim_window(h, "qq", "group_9",
                                         budget=_BUDGET, reserve=-10 ** 6), h)

    def test_save_history_forwards_reserve(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(memory, "_agent_session_file",
                                   return_value=os.path.join(d, "s.jsonl")), \
                 mock.patch.object(memory, "trim_window", return_value=[]) as tw:
                memory.save_history(_history(1), "qq", "group_9", reserve=777)
        self.assertEqual(tw.call_args[1]["reserve"], 777)


class GroupKeyTest(unittest.TestCase):
    def test_group_prefix_stripped(self):
        self.assertEqual(memory._group_id_from_key("group_885143276"), "885143276")

    def test_private_and_empty_return_blank(self):
        self.assertEqual(memory._group_id_from_key("user_123"), "")
        self.assertEqual(memory._group_id_from_key(None), "")


class DigestMessagesAsyncTest(unittest.TestCase):
    """longterm.digest_messages_async：窗口消息 → 150 字记忆落盘。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch.object(agents, "AGENTS_DIR",
                              os.path.join(self.tmp.name, "agents"))
        p.start()
        self.addCleanup(p.stop)
        agents.clear_cache()
        self.addCleanup(agents.clear_cache)

    def _run(self, msgs, llm_return="摘要内容"):
        with mock.patch.object(longterm, "call_llm", return_value=llm_return) as llm:
            longterm.digest_messages_async("qq", "9", msgs)
            # digest_messages_async 起后台线程；同步等它收尾再断言
            for t in threading.enumerate():
                if t.name == "memory-digest":
                    t.join(timeout=5)
        return llm

    def test_writes_short_record(self):
        msgs = [{"role": "user", "content": "小明：帮我看看报错"},
                {"role": "assistant", "content": "发来"}]
        llm = self._run(msgs)
        sys_content = llm.call_args[0][0][0]["content"]
        self.assertIn("150 字", sys_content)        # 用的是短摘要口径
        path = longterm._path("qq", "9")
        self.assertTrue(os.path.exists(path))
        import json
        rec = json.loads(open(path, encoding="utf-8").readlines()[-1])
        self.assertEqual(rec["s"], "摘要内容")
        self.assertEqual(rec["n"], 2)

    def test_skips_system_and_empty(self):
        msgs = [{"role": "system", "content": "人设"},
                {"role": "user", "content": "  "}]
        llm = self._run(msgs)
        llm.assert_not_called()

    def test_failure_writes_nothing(self):
        with mock.patch.object(longterm, "call_llm",
                               side_effect=RuntimeError("挂了")):
            longterm.digest_messages_async("qq", "9",
                                           [{"role": "user", "content": "hi"}])
            for t in threading.enumerate():
                if t.name == "memory-digest":
                    t.join(timeout=5)
        self.assertFalse(os.path.exists(longterm._path("qq", "9")))

    def test_body_capped_from_the_end(self):
        long_msgs = [{"role": "user", "content": "字" * 6000} for _ in range(3)]
        llm = self._run(long_msgs)
        body = llm.call_args[0][0][1]["content"]
        self.assertLessEqual(len(body), longterm._SOURCE_MAX_CHARS + 20)


class WebSearchTruncateTest(unittest.TestCase):
    def test_long_result_truncated(self):
        text = "x" * 3000
        out = web_search._truncate(text)
        self.assertLess(len(out), 1000)
        self.assertIn("截断", out)

    def test_short_result_untouched(self):
        self.assertEqual(web_search._truncate("短结果"), "短结果")

    def test_zero_disables(self):
        with mock.patch.object(web_search, "WEB_SEARCH_MAX_CHARS", 0):
            self.assertEqual(web_search._truncate("x" * 3000), "x" * 3000)

    def test_web_search_pipeline_truncates(self):
        with mock.patch.object(web_search, "_doubao_search",
                               return_value="y" * 3000):
            out = web_search._web_search("测试")
        self.assertLess(len(out), 1000)
