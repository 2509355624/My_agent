# -*- coding: utf-8 -*-
"""会话 system 头同步测试（app/agent_prompt.py::sync_session_system）。

要解决的真实问题：会话的 system 头是建立时写进 JSONL 的，之后一直躺在那。
改 prompt.md 后，进程内的 build_stable_prompt 立刻返回新内容，但**已有会话
读到的还是旧的那条**——网页端靠启动时重建，QQ 端连启动都不重建。结果就是
改完机器人的人设，已经在聊的群纹丝不动，只有新会话才生效。

这里验证三件事：变了要替换、没变一个字节都不动（保住前缀缓存）、
多条会话线之间互不影响。
"""

import json
import os
import tempfile
import time
import unittest
from unittest import mock

import app.agents as agents
import app.memory as memory
import app.agent_prompt as prompt_mod


def _bump(path):
    """把 mtime 拨到未来，绕开文件系统精度导致的「看起来没变」。"""
    later = time.time() + 10
    os.utime(path, (later, later))


class SyncSessionSystemTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.join(self.tmp.name, "agents")
        os.makedirs(self.root, exist_ok=True)
        p = mock.patch.object(agents, "AGENTS_DIR", self.root)
        p.start()
        self.addCleanup(p.stop)
        # 配置 / 人设 / 已检查版本都是按目录或按 agent 缓存的，换目录必须清
        agents.clear_cache()
        self.addCleanup(agents.clear_cache)
        prompt_mod.clear_session_cache()
        self.addCleanup(prompt_mod.clear_session_cache)

    # ─── 辅助 ───────────────────────────────────────

    def _mk_agent(self, aid="a", persona="你是A"):
        d = os.path.join(self.root, aid)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "agent.json"), "w", encoding="utf-8") as f:
            json.dump({}, f)
        self._set_persona(aid, persona)

    def _set_persona(self, aid, text):
        path = os.path.join(self.root, aid, "prompt.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        _bump(path)

    def _seed(self, agent_id="a", session_key=None, system="旧的"):
        memory.save_history([{"role": "system", "content": system},
                             {"role": "user", "content": "你好"}],
                            agent_id, session_key)

    # ─── 用例 ───────────────────────────────────────

    def test_stale_header_is_replaced(self):
        self._mk_agent()
        self._seed()
        self.assertTrue(prompt_mod.sync_session_system("a"))
        h = memory.load_history("a")
        self.assertIn("你是A", h[0]["content"])
        # 非 system 消息原样保留
        self.assertEqual(h[1], {"role": "user", "content": "你好"})

    def test_persona_change_is_picked_up(self):
        self._mk_agent(persona="你是A")
        self._seed(system=prompt_mod.build_stable_prompt("a"))

        self._set_persona("a", "你是B")
        self.assertTrue(prompt_mod.sync_session_system("a"))

        first = memory.load_history("a")[0]["content"]
        self.assertIn("你是B", first)
        self.assertNotIn("你是A", first)

    def test_unchanged_header_leaves_the_file_untouched(self):
        """没变就一个字节都不写——system 在最前面，动它等于整段前缀缓存重算。"""
        self._mk_agent()
        self._seed(system=prompt_mod.build_stable_prompt("a"))
        path = agents.session_file("a")
        before = os.path.getmtime(path)

        self.assertFalse(prompt_mod.sync_session_system("a"))

        self.assertEqual(os.path.getmtime(path), before)

    def test_second_call_is_a_noop(self):
        self._mk_agent()
        self._seed()
        self.assertTrue(prompt_mod.sync_session_system("a"))
        self.assertFalse(prompt_mod.sync_session_system("a"))

    def test_session_without_system_header_gets_one(self):
        self._mk_agent()
        memory.save_history([{"role": "user", "content": "你好"}], "a")
        self.assertTrue(prompt_mod.sync_session_system("a"))
        self.assertEqual(memory.load_history("a")[0]["role"], "system")

    def test_missing_session_file_is_left_alone(self):
        """会话还没建过就不在这一处建头，交给 _ensure_system_prompt。"""
        self._mk_agent()
        self.assertFalse(prompt_mod.sync_session_system("a"))
        self.assertFalse(os.path.exists(agents.session_file("a")))

    def test_session_lines_are_independent(self):
        """一个 agent 挂多条会话线（QQ 的每个群各一条）：只动指定的那条。"""
        self._mk_agent(persona="你是A")
        stable = prompt_mod.build_stable_prompt("a")
        memory.save_history([{"role": "system", "content": stable}], "a", "group_1")
        memory.save_history([{"role": "system", "content": "过期的人设"}],
                            "a", "group_2")

        self.assertTrue(prompt_mod.sync_session_system("a", "group_2"))

        self.assertIn("你是A",
                      memory.load_history("a", "group_2")[0]["content"])
        # 另一个群没被动过
        self.assertEqual(len(memory.load_history("a", "group_1")), 1)


class ImageGuideTest(unittest.TestCase):
    """生图方法独立成段：**不随会话自定义人设一起被顶掉**。

    「四、通用生图方法」原先写在 prompt.md 里，而 prompt.md **整份**就是
    build_stable_prompt 的 Role 段；会话级自定义人设（persona_override）是
    整份顶替 Role 的 —— 于是给某条会话设了人设，生图方法就跟着没了，生图
    质量莫名其妙地掉，而且越"定制"的会话越容易中招，极难查。
    2026-10-03 抽成 agents/<id>/image_guide.md，进独立 section。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.join(self.tmp.name, "agents")
        os.makedirs(self.root, exist_ok=True)
        for obj, name in ((agents, "AGENTS_DIR"),):
            p = mock.patch.object(obj, name, self.root)
            p.start()
            self.addCleanup(p.stop)
        agents.clear_cache()
        self.addCleanup(agents.clear_cache)
        # 稳定层缓存不归 clear_session_cache 管，得手动清（key 是 agent id，
        # 跨用例会串）
        prompt_mod._stable_cache.clear()
        prompt_mod._stable_fp.clear()
        self.addCleanup(prompt_mod._stable_cache.clear)
        self.addCleanup(prompt_mod._stable_fp.clear)

    def _mk_agent(self, aid, persona="你是A"):
        d = os.path.join(self.root, aid)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "agent.json"), "w", encoding="utf-8") as f:
            json.dump({}, f)
        path = os.path.join(d, "prompt.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(persona)
        _bump(path)

    def _mk_guide(self, aid, text):
        path = os.path.join(self.root, aid, "image_guide.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        _bump(path)

    def test_guide_survives_persona_override(self):
        self._mk_agent("g")
        self._mk_guide("g", "画手要写 five fingers")
        self.assertIn("five fingers", prompt_mod.build_stable_prompt("g"))

        over = prompt_mod.build_stable_prompt("g", persona_override="你只会说喵")
        self.assertIn("你只会说喵", over)          # 人设确实被换了
        self.assertIn("five fingers", over)        # ← 关键：生图方法没被吃掉

    def test_no_guide_file_means_no_section(self):
        """没有 image_guide.md 的 agent（多数都没有）不该多出空段。"""
        self._mk_agent("g2")
        self.assertNotIn("生图方法", prompt_mod.build_stable_prompt("g2"))

    def test_editing_guide_bumps_revision(self):
        """改生图方法要能热加载 —— 它必须进 revision 指纹。"""
        self._mk_agent("g3")
        self._mk_guide("g3", "第一版")
        r1 = agents.revision("g3")
        self._mk_guide("g3", "第二版")
        self.assertNotEqual(r1, agents.revision("g3"))

    def test_guide_path_is_none_for_bad_agent_id(self):
        self.assertIsNone(agents.image_guide_path("../.."))
        self.assertEqual(agents.image_guide_text("../.."), "")


class ImageGuideContentTest(unittest.TestCase):
    """`agents/qq/image_guide.md` 里的两条硬判据（2026-10-04）。

    这段是系统头第二大块，进 prompt 就等于每轮都带。以下两条不是可选的
    文案，是实测踩出来的坑，删了模型立刻犯。
    """

    @staticmethod
    def _guide():
        import io
        with io.open(agents.image_guide_path("qq"), encoding="utf-8") as f:
            return f.read()

    def test_no_weight_syntax_teaching(self):
        """不许再教权重语法——本机工作流没有解析器。

        老版本有一整节「模型差异」，讲 SD `(tag:1.2)` / NAI `{tag}` /
        niji `::` 各家写法。那些渠道 09-30 已归档；现在所有渠道的
        `__MULTI_PROMPTS__` 都是**原样插入的纯文本**，写了权重只会被当成
        字面垃圾词送进底模。保留的必须是「别写」的警告。
        """
        g = self._guide()
        self.assertIn("权重语法一个都别写", g,
                      "权重语法警告不见了，模型会开始写 (masterpiece:1.2)")
        self.assertNotIn("SD / SDXL / Pony", g,
                         "「模型差异」那节已删，别把权重语法讲回来")

    def test_character_name_hallucination_guard(self):
        """必须有「不编角色名」的判据。

        实测：9B 画「神里绫华」写出 `Shiori Sakura`（不存在）还照画。
        GSND 标签库收录有限，`kamisato_ayaka` 也不在库里，所以
        「认不出就写外貌 + 作品名」是唯一稳的路子。
        """
        g = self._guide()
        self.assertIn("别编", g, "角色名幻觉判据不见了")

    def test_fresh_prompt_on_new_request(self):
        """必须有「新请求=全新提示词」的判据（2026-10-04 私聊实测）。

        实测：胡桃桃私聊里换「神里绫华」「香菱」，MiMo 只换角色名，
        把胡桃的棒棒糖/黑背心/JK 裙/喷泉回头整套抄进新角色的提示词。
        正向规则（重新填四槽位）比负向规则（别抄）9B 学得动。
        """
        g = self._guide()
        self.assertIn("全新提示词", g, "「新请求=全新提示词」判据不见了")
        self.assertIn("保持 / 同款 /", g,
                      "「只继承明确说的槽位」判据不见了")

    def test_operation_words_stay_out_of_prompt(self):
        """必须有「prompt 字段只放画面内容」的判据（2026-10-04 实测）。

        实测：「高清重绘」四个字被当画面词写进 prompt 送进底模。
        """
        g = self._guide()
        self.assertIn("绝不写进 prompt", g,
                      "「操作指令不进 prompt」判据不见了")
        self.assertIn("load_skill", g,
                      "要留一条查库的出路（anima-tags），别让模型只能瞎猜")
        # 拿真实踩坑的假 tag 当反例钉住
        self.assertIn("Shiori Sakura", g,
                      "保留实测反例（9B 给神里绫华编的假 tag）")



class BriefModeTest(unittest.TestCase):
    """简短模式（.env PROMPT_BRIEF）：给小上下文模型砍系统头。

    实测背景（2026-10-04）：本地 ollama 9B 的 num_ctx 是 16384，
    `physical_budget` 把它压到 11468；而 qq 的系统头有 22514 字 ≈ 预算的
    两倍 → 输入超预算近 3 倍，模型写 generate_image 的参数写到一半被物理
    窗口切断（`[tool-truncated]`），群里表现成「话说得漂亮但没调工具」
    连失败 3 次。所以砍系统头是治根，不是省 token。
    """

    def _sp(self, brief):
        from unittest import mock
        import app.agent_prompt as ap
        ap._stable_cache.clear()
        ap._stable_fp.clear()
        with mock.patch.dict(os.environ,
                             {"PROMPT_BRIEF": "1" if brief else "0"}):
            return ap.build_system_prompt(agent_id="qq")

    def test_brief_shrinks_system_head_hard(self):
        full, brief = self._sp(False), self._sp(True)
        self.assertLess(len(brief), len(full) * 0.5,
                        "简短模式没砍下来（%d -> %d）" % (len(full), len(brief)))
        # 关键判据：必须能装进 ollama 的压缩预算 11468，否则压缩照样触发
        self.assertLessEqual(
            len(brief), 11468,
            "砍完仍超 ollama 物理预算 11468 字，压缩闸门照样每轮触发")

    def test_brief_keeps_tool_names_and_params(self):
        """砍的是描述，不是工具本身——名字与参数签名必须留着。

        少了它 9B 根本没法把参数填对（它不会主动 load_skill）。
        2026-10-04 晚白名单砍成 4 个纯生图工具：send_qq_message / web_search
        等整体出局（不在列表里），断言反过来钉「确实不在」。
        """
        brief = self._sp(True)
        for name, param in (("generate_image", "prompt*"),
                            ("recall_image", "tag*"),
                            ("load_skill", "name*")):
            self.assertIn(name, brief, "工具 %s 被砍没了" % name)
            self.assertIn(param.split("*")[0], brief,
                          "工具 %s 的参数名不见了" % name)
        for gone in ("send_qq_message", "web_search", "memory_save",
                     "preset_save", "send_sticker"):
            self.assertNotIn("**%s**" % gone, brief,
                             "工具 %s 已出白名单，不该再列出" % gone)

    def test_brief_drops_hints_entirely(self):
        """_TOOL_HINTS 那 2170 字细则要整段消失——它是「怎么用」不是「有什么」。"""
        from app.agent_prompt import _build_tool_hints
        hints = _build_tool_hints("qq")
        self.assertIn("提示：", self._sp(False))
        self.assertNotIn(hints[:40], self._sp(True),
                         "简短模式仍塞着 hints 全文")

    def test_brief_skill_list_is_names_only(self):
        """Skill 目录在简短模式下只列名字（渠道名本身就是 skill 参数值）。

        这里必须单独钉住：改回去不一定让系统头超预算（13361 也超，但
        两种超法不一样），所以光靠 test_brief_shrinks_system_head_hard
        抓不到「skill 目录没压」这种回归。
        """
        from app.agent_prompt import _build_skill_list
        full = _build_skill_list("qq", brief=False)
        brief = _build_skill_list("qq", brief=True)
        self.assertIn("（", full, "完整模式应该有类型标注")
        self.assertNotIn("（", brief, "简短模式不该再有类型标注（没压）")
        self.assertLess(len(brief), len(full) * 0.5,
                        "skill 目录没压下来（%d -> %d）" % (len(full), len(brief)))
        # 名字必须还在——那是渠道参数值，砍掉模型就没法填 skill 了
        # （2026-10-07 起动漫族只剩 `hd_3_<画风>`；`anima_clear` 那 12 个
        #  旧渠道已屏蔽，不该再出现在任何对外列表里）
        self.assertIn("hd_3_clear", brief)
        self.assertNotIn("anima_clear", brief)

    def test_brief_keeps_tool_call_protocol(self):
        """协议段不能砍。砍了它模型就不认识 [[TOOL:...]] 了，
        会退化成 XML 标签写法（工具静默不执行）。"""
        brief = self._sp(True)
        self.assertIn("[[TOOL:", brief)
        self.assertIn("TOOL:", brief)

    def test_brief_off_by_default(self):
        """没设 PROMPT_BRIEF 时必须走完整模式——云端模型上下文充裕，
        不该被小模型的限制拖累。"""
        from unittest import mock
        import app.agent_prompt as ap
        ap._stable_cache.clear()
        ap._stable_fp.clear()
        env = {k: v for k, v in os.environ.items() if k != "PROMPT_BRIEF"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertFalse(ap._brief_mode())


class PersonaTrimmedTest(unittest.TestCase):
    """人设砍到只剩身份+说话方式（2026-10-04 用户要求）。

    起因是 9B 上下文超载：原 prompt.md 7165 字里 6771 字是审核话术、
    边界细则、拒绝话术——用户自己本地玩，这些纯占位置。
    **但「身份 + 说话方式 + 生图铁律」必须留着**：砍到只剩一句话的话，
    模型会既不认自己是谁，也不会调工具。
    """

    def _persona(self):
        from app import agents as agent_store
        return agent_store.persona_text("qq")

    def test_persona_is_small(self):
        p = self._persona()
        self.assertLess(len(p), 1500,
                        "人设还剩 %d 字，审核/边界细则没砍干净" % len(p))

    def test_keeps_identity_and_voice(self):
        """2026-10-04 晚再砍：说话方式整节删掉，只留一句分行短句的要求。

        「要图就直接画」那类性格铺陈也删了——生图 AI 只剩身份 + 铁律。
        """
        p = self._persona()
        self.assertIn("大大怪", p, "身份丢了")
        self.assertIn("## 一、身份", p)
        self.assertIn("分行短句", p, "说话方式压缩后至少留一句格式要求")
        self.assertNotIn("## 三、说话方式", p, "说话方式整节该删了")

    def test_keeps_image_rule(self):
        """生图铁律是「不调工具」这个 bug 的直接对策，必须留着。"""
        p = self._persona()
        self.assertIn("直接调 generate_image", p)
        self.assertIn("绝不把提示词写进正文", p)

    def test_audit_sections_gone(self):
        """审核/边界/拒绝话术整段删掉。"""
        p = self._persona()
        for gone in ("最高优先级 · 内容审核层", "群聊拦截清单",
                     "私聊拒绝话术", "群聊拒绝话术", "试探边界",
                     "群聊只答被问的（铁律）"):
            self.assertNotIn(gone, p, "「%s」还在人设里" % gone)


class GroupsMutedTest(unittest.TestCase):
    """群聊总开关：开了所有群都不回，私聊照常（当纯私人工具用）。"""

    def test_settings_true_mutes_every_group(self):
        from app import agents as agent_store
        with mock.patch.object(agent_store, "load_settings",
                               return_value={"groups_muted": True}):
            self.assertIs(agent_store.groups_muted("qq"), True)

    def test_settings_list_mutes_only_those(self):
        from app import agents as agent_store
        with mock.patch.object(agent_store, "load_settings",
                               return_value={"groups_muted": ["111", "222"]}):
            got = agent_store.groups_muted("qq")
            self.assertEqual(got, {"111", "222"})

    def test_absent_means_not_muted(self):
        from app import agents as agent_store
        with mock.patch.object(agent_store, "load_settings", return_value={}):
            self.assertFalse(agent_store.groups_muted("qq"))

    def test_bot_gate_blocks_even_at(self):
        """连 @ 都不回——直接验行为，别测源码位置。

        之前用 `src.index()` 比位置，两次都被假通过骗过去：① 找的是第一处
        `if at_me:` 提及，插它前面断言照样成立；② 插第二个"群聊已静音"副本
        到 @ 之后，原件还在前面，断言还是成立。位置断言在有重复文本的函数里
        根本不可靠。这里改成跑真函数：被 @ 的群在开关打开时必须不回。
        """
        import app.qq_bot as qb
        ev = {"user_id": "1"}
        for at in (True, False):
            with mock.patch.object(qb, "_groups_muted", return_value=True):
                ok, why = qb._should_reply(ev, "group", "999", "大大怪", at)
            self.assertFalse(ok, "at_me=%s 竟然回了（静音开关失效）" % at)
            self.assertIn("静音", why)

    def test_bot_gate_not_muted_still_replies(self):
        """反向：开关没开时行为必须跟以前一样（别把群聊功能整体关死）。"""
        import app.qq_bot as qb
        ev = {"user_id": "1"}
        with mock.patch.object(qb, "_groups_muted", return_value=False):
            ok, why = qb._should_reply(ev, "group", "999", "大大怪", True)
        self.assertTrue(ok, "没静音却被拦了：%s" % why)

    def test_private_unaffected_by_group_mute(self):
        """群静音**不能**波及私聊——这是它的全部意义。"""
        import app.qq_bot as qb
        ev = {"user_id": "1"}
        with mock.patch.object(qb, "_groups_muted", return_value=True), \
             mock.patch.object(qb, "_private_gate", return_value=(True, "")):
            ok, why = qb._should_reply(ev, "private", "1", "在吗", False)
        self.assertTrue(ok, "私聊被群静音误伤：%s" % why)

    def test_bot_gate_returns_reason(self):
        import app.qq_bot as qb
        from app import agents as agent_store
        ev = {"user_id": "1"}
        with mock.patch.object(qb, "_groups_muted", return_value=True):
            ok, why = qb._should_reply(ev, "group", "999", "大大怪", True)
        self.assertFalse(ok)
        self.assertIn("静音", why)

    def test_sessions_api_reports_groups_muted(self):
        """sessions 接口要给管理页 `groups_muted_on`——按钮的初始状态靠它。

        ⚠️ 值必须**直接取** `agent_store.groups_muted()`（qq_bot 判的那一份），
        别在前端另算：那份可能是 True 也可能是群号列表（只静音部分群），
        两种都算「开」，前端只判布尔。
        """
        from app import agents as agent_store
        with open("app/main.py", encoding="utf-8") as f:
            src = f.read()
        self.assertIn('"groups_muted_on"', src,
                      "sessions 接口没返回 groups_muted_on，"
                      "管理页按钮会一直显示「开」")
        self.assertIn("agent_store.groups_muted(aid)", src,
                      "groups_muted_on 没直接取生效值")
        # 列表形态也得算「开」——bool(['111']) 是 True，语义正好
        for stored, want in ((True, True), (["111", "222"], True),
                             ({"111"}, True), (False, False), (None, False)):
            with mock.patch.object(agent_store, "load_settings",
                                   return_value={"groups_muted": stored}):
                got = bool(agent_store.groups_muted("qq"))
            self.assertEqual(got, want, "settings 里 %r → %r" % (stored, got))


if __name__ == "__main__":
    unittest.main()
