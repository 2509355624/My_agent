"""发图审核闸门（app/image_audit.py）的锁。

这个模块只有三条规则，但每条都是「错了会很痛」的那种，所以逐条钉住：

1. **fail-closed** —— 识图超时 / 报错 / 回复解析不出来 / 图读不出来，一律
   拦下不发。用户 2026-10-01 明确选的（改前是 fail-open）。代价是识图 provider
   一抖所有图都发不出去，所以失败路径回的是另一句提示（FAILED_NOTICE）。
2. **口径适中** —— 拦的是裸露 / 性暗示 / 暧昧动作，而**穿着本身不算**（泳装、
   内衣、浴巾都放行，配上性暗示动作或表情才算）。这条钉在 `_PROMPT` 的关键词上
   （PromptPolicyTest）——只测 parse_verdict 的话，把提示词改回从严版都不会有
   测试变红。
3. **开关没开时一次网络都不发** —— 三个发图点都挂了它，热路径上多一次
   识图调用就是每张图白等两秒。

另外钉住「拦下之后不再抛异常」：调用方（image_jobs / qq_bot）都靠返回值
分流，抛出去会被当成发送失败、进而退额度 —— 那等于给了一条「靠生成违规图
刷额度」的路。
"""

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, ".")

from app import agents, image_audit, main  # noqa: E402


def _write_png(data=b"\x89PNG\r\n\x1a\n" + b"x" * 64):
    """造一个本地文件当 prepare_for_send 的产物（内容不重要，都会被 mock 掉）。"""
    fd, path = tempfile.mkstemp(suffix=".jpg")
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    return path


class ParseVerdictTest(unittest.TestCase):
    """模型回复 → Verdict。宽容是必要的（模型偶尔加围栏或客套话）。"""

    def test_clean_json(self):
        v = image_audit.parse_verdict(
            '{"allow": true, "reason": "泳装展示", "category": "ok"}')
        self.assertTrue(v.allow)
        self.assertEqual(v.category, "ok")
        self.assertEqual(v.reason, "泳装展示")
        self.assertFalse(v.failed)

    def test_fenced_json(self):
        v = image_audit.parse_verdict(
            '```json\n{"allow": false, "reason": "全裸", "category": "nudity"}\n```')
        self.assertFalse(v.allow)
        self.assertEqual(v.category, "nudity")

    def test_json_with_chatter_around_it(self):
        v = image_audit.parse_verdict(
            '好的，结果如下：{"allow": false, "reason": "x", "category": "sexual"} 完毕')
        self.assertFalse(v.allow)
        self.assertEqual(v.category, "sexual")

    def test_string_bool_is_not_accepted(self):
        """`"allow": "true"` 是字符串不是布尔 —— 算解析失败，宁可放行也别拦人。"""
        self.assertIsNone(image_audit.parse_verdict('{"allow": "true"}'))

    def test_missing_allow_is_not_accepted(self):
        self.assertIsNone(image_audit.parse_verdict('{"reason": "没给 allow"}'))

    def test_non_json_and_empty(self):
        for text in ("完全不是 JSON", "", None, "   "):
            self.assertIsNone(image_audit.parse_verdict(text), repr(text))

    def test_unknown_category_falls_back_to_other(self):
        """野 category 只影响日志标签，**不该影响 allow**。"""
        v = image_audit.parse_verdict('{"allow": true, "category": "weird"}')
        self.assertTrue(v.allow)
        self.assertEqual(v.category, "other")

    def test_missing_reason_is_empty_string(self):
        v = image_audit.parse_verdict('{"allow": true}')
        self.assertTrue(v.allow)
        self.assertEqual(v.reason, "")


class CheckFailClosedTest(unittest.TestCase):
    """check() 的四条失败路径必须全部拦下（fail-closed），而且**不抛异常**。"""

    def test_missing_file_is_fail_closed(self):
        v = image_audit.check("D:/definitely/not/here.jpg")
        self.assertFalse(v.allow)
        self.assertTrue(v.failed)

    def test_empty_file_is_fail_closed(self):
        path = _write_png(b"")
        try:
            v = image_audit.check(path)
            self.assertFalse(v.allow)
            self.assertTrue(v.failed)
        finally:
            os.remove(path)

    def test_vision_error_is_fail_closed(self):
        path = _write_png()
        try:
            with mock.patch("app.vision.describe",
                            side_effect=RuntimeError("识图请求失败（耗时 30.0s）")):
                v = image_audit.check(path)
            self.assertFalse(v.allow)
            self.assertTrue(v.failed)
        finally:
            os.remove(path)

    def test_unparseable_reply_is_fail_closed(self):
        path = _write_png()
        try:
            with mock.patch("app.vision.describe",
                            return_value="我觉得这张图还行吧"):
                v = image_audit.check(path)
            self.assertFalse(v.allow)
            self.assertTrue(v.failed)
        finally:
            os.remove(path)


class CheckVerdictTest(unittest.TestCase):
    """正常路径：违规就 allow=False，合规就 allow=True。"""

    def _check_with(self, reply):
        path = _write_png()
        try:
            with mock.patch("app.vision.describe", return_value=reply):
                return image_audit.check(path)
        finally:
            os.remove(path)

    def test_nudity_is_blocked(self):
        v = self._check_with(
            '{"allow": false, "reason": "全裸且露出乳头及下体", "category": "nudity"}')
        self.assertFalse(v.allow)
        self.assertFalse(v.failed)
        self.assertEqual(v.category, "nudity")

    def test_skin_verdict_is_still_honoured(self):
        """模型回了 `skin` 档拒绝，照样拦——判定语义没变，变的只是**什么算 skin**。

        2026-10-01 口径从「从严」回到「适中」后，泳装 / 内衣本身不再违规，
        `skin` 这一档只剩「露肩露背之类被模型判成性化」时才用得上。这里只锁
        「verdict 被原样转发」，不再断言泳装必须被拦（那是 PromptPolicyTest 的事）。
        """
        v = self._check_with(
            '{"allow": false, "reason": "露肩露背", "category": "skin"}')
        self.assertFalse(v.allow)
        self.assertFalse(v.failed)
        self.assertEqual(v.category, "skin")

    def test_fully_clothed_passes(self):
        v = self._check_with(
            '{"allow": true, "reason": "正常着装", "category": "ok"}')
        self.assertTrue(v.allow)
        self.assertFalse(v.failed)


class PromptPolicyTest(unittest.TestCase):
    """口径本身就是需求，得钉在提示词上。

    这条边界反转过三轮：适中（10-01）→ 从严 → 适中 → **最严白名单**
    （2026-10-05，agent 退场后审核是群里唯一闸门）。现在钉的是白名单口径的
    三个锚：只有 level 0 放行 / 泳装内衣温泉进拦侧 / confidence=low 拦。
    只测 `parse_verdict` 是钉不住的：把 `_PROMPT` 换成另一套口径，那些用例
    照样全绿。
    """

    def test_prompt_normal_clothing_stays_in_whitelist(self):
        """白名单里必须保留正常穿着的放行项（露肩露腿短裙等）——最严不等于
        连校服短裙都拦，否则日常立绘一张都发不出去。"""
        p = image_audit._PROMPT
        self.assertIn("短裙", p)
        self.assertIn("露肩", p)
        self.assertIn("正常穿着不算问题", p)

    def test_prompt_pass_line_is_zero_only(self):
        """放行线钉死在 level 0：1-3 一律拦——旧口径的「0-1 放行」必须消失。"""
        p = image_audit._PROMPT
        self.assertIn("只有 level 0 放行", p)
        self.assertNotIn("0-1 放行", p)
        # 旧口径的擦边放行清单现在必须出现在拦侧
        self.assertIn("泳装、比基尼", p)
        self.assertIn("温泉", p)

    def test_prompt_blocks_nudity_and_suggestive_acts(self):
        """真正要拦的档位仍在：2 的动作/视角/表情清单 + 3 的裸露。"""
        p = image_audit._PROMPT
        self.assertIn("性器官", p)
        self.assertIn("一字马", p)
        self.assertIn("高潮脸", p)

    def test_prompt_low_confidence_blocks(self):
        """拿不准 = 拦：confidence=low 直接拦写进提示词。"""
        p = image_audit._PROMPT
        self.assertIn("confidence=low 一律拦", p)

    def test_low_confidence_is_forced_to_block(self):
        """代码层兜底：模型自报 low 却 allow=true → 强制按拦算。"""
        v = image_audit.parse_verdict(
            '{"allow": true, "level": 1, "confidence": "low", '
            '"category": "skin", "reason": "轻微透视"}')
        self.assertFalse(v.allow)
        self.assertIn("拿不准", v.reason)

    def test_high_confidence_pass_is_untouched(self):
        v = image_audit.parse_verdict(
            '{"allow": true, "level": 0, "confidence": "high", '
            '"category": "ok", "reason": "日常立绘"}')
        self.assertTrue(v.allow)

    def test_missing_confidence_keeps_old_behaviour(self):
        """旧口径提示词没带 confidence 字段 → 不触发强制拦。"""
        v = image_audit.parse_verdict(
            '{"allow": true, "reason": "ok", "category": "ok"}')
        self.assertTrue(v.allow)

    def test_new_categories_are_legal(self):
        for c in ("pose", "clothing", "scene", "intimacy", "minor", "real"):
            with self.subTest(c=c):
                v = image_audit.parse_verdict(
                    '{"allow": false, "reason": "x", "category": "%s"}' % c)
                self.assertEqual(v.category, c)

    def test_skin_category_is_kept_in_logs(self):
        v = image_audit.parse_verdict(
            '{"allow": false, "reason": "露肩露背", "category": "skin"}')
        self.assertEqual(v.category, "skin")


class AllowSendTest(unittest.TestCase):
    """总闸 allow_send()：开关 + 判定 + 通知，三件事的正确顺序。"""

    def setUp(self):
        self.path = _write_png()
        self.addCleanup(lambda: os.path.exists(self.path) and os.remove(self.path))

    def _with_enabled(self, enabled):
        return mock.patch.object(agents, "image_audit_enabled",
                                 return_value=enabled)

    def test_disabled_never_calls_vision(self):
        """开关没开时**一次识图都不发** —— 这是热路径上的硬要求。"""
        with self._with_enabled(False), \
             mock.patch("app.vision.describe") as desc:
            self.assertTrue(image_audit.allow_send(
                self.path, "qq", "group", "123"))
        desc.assert_not_called()

    def test_enabled_and_clean_passes(self):
        with self._with_enabled(True), \
             mock.patch("app.vision.describe",
                        return_value='{"allow": true, "category": "ok"}'), \
             mock.patch.object(image_audit, "_notify_blocked") as notify:
            self.assertTrue(image_audit.allow_send(
                self.path, "qq", "group", "123"))
        notify.assert_not_called()

    def test_enabled_and_nudity_blocks_and_notifies(self):
        with self._with_enabled(True), \
             mock.patch("app.vision.describe",
                        return_value='{"allow": false, "category": "nudity"}'), \
             mock.patch.object(image_audit, "_notify_blocked") as notify:
            self.assertFalse(image_audit.allow_send(
                self.path, "qq", "group", "123"))
        self.assertEqual(notify.call_count, 1)

    def test_vision_failure_blocks(self):
        """fail-closed 的端到端验证：识图挂了，图发不出去，并且回一句提示。"""
        with self._with_enabled(True), \
             mock.patch("app.vision.describe", side_effect=RuntimeError("boom")), \
             mock.patch.object(image_audit, "_notify_blocked") as notify:
            self.assertFalse(image_audit.allow_send(
                self.path, "qq", "group", "123"))
        self.assertEqual(notify.call_count, 1)

    def test_web_target_always_passes(self):
        """target=None（网页端）直接放行，连开关都不读。"""
        with mock.patch.object(agents, "image_audit_enabled") as gate, \
             mock.patch("app.vision.describe") as desc:
            self.assertTrue(image_audit.allow_send(
                self.path, "qq", None, None))
        gate.assert_not_called()
        desc.assert_not_called()

    def test_settings_read_error_does_not_block(self):
        """读不到 settings 时放行 —— 这**不是** fail-open 的残留。

        它和「识图失败」是两码事：读不到 settings 意味着不知道开关是什么状态，
        而不是「知道要审但审不了」。settings 坏了就把所有图堵死，等于 bot 主
        功能停摆，代价远大于收益。要连这条也拦，得先把开关状态挪到别处存。
        """
        with mock.patch.object(agents, "image_audit_enabled",
                               side_effect=OSError("settings 读挂了")), \
             mock.patch("app.vision.describe") as desc:
            self.assertTrue(image_audit.allow_send(
                self.path, "qq", "group", "123"))
        desc.assert_not_called()


class NotifyTest(unittest.TestCase):
    """被拦之后回的那句话。"""

    def test_notice_does_not_leak_model_reason(self):
        """**不回显模型的 reason** —— 它写得很直白（「全裸且露出乳头及下体」），
        原样转述等于把露骨描述甩回群里。用户要的是「回一句提示」。"""
        self.assertNotIn("乳", image_audit.BLOCKED_NOTICE)
        self.assertNotIn("下体", image_audit.BLOCKED_NOTICE)
        self.assertTrue(image_audit.BLOCKED_NOTICE.strip())

    def test_failed_verdict_gets_failed_notice(self):
        """审核没生效 → 说「服务没响应」，不能说成「没过审」。

        fail-closed 之后两种拦截都发得出去，但原因完全不同：混成一句的话，
        识图一抖用户就去改本来没问题的提示词。
        """
        v = image_audit.Verdict(False, reason="识图失败：timeout", failed=True)
        with mock.patch("app.qq_api.send_group") as sg:
            image_audit._notify_blocked("group", "123", v)
        sg.assert_called_once_with("123", image_audit.FAILED_NOTICE)
        self.assertNotEqual(image_audit.FAILED_NOTICE,
                            image_audit.BLOCKED_NOTICE)

    def test_notify_uses_group_and_private_channels(self):
        v = image_audit.Verdict(False, reason="x", category="nudity")
        with mock.patch("app.qq_api.send_group") as sg, \
             mock.patch("app.qq_api.send_private") as sp:
            image_audit._notify_blocked("group", "123", v)
            image_audit._notify_blocked("private", "456", v)
        sg.assert_called_once_with("123", image_audit.BLOCKED_NOTICE)
        sp.assert_called_once_with("456", image_audit.BLOCKED_NOTICE)

    def test_notify_swallows_send_failure(self):
        """通知发不出去也不能抛 —— 拦截本身已经生效了，不该再炸一层。"""
        v = image_audit.Verdict(False, reason="x", category="nudity")
        with mock.patch("app.qq_api.send_group",
                        side_effect=RuntimeError("发不出去")):
            image_audit._notify_blocked("group", "123", v)   # 不抛就算过


class AgentSettingTest(unittest.TestCase):
    """settings.json 三层取值：单会话覆盖 > 分类总开关 > False。

    总开关**按群聊 / 私聊分开**（2026-10-01 用户要求：只给群开、私聊不开是常见
    需求，合成一个的话每次还得按会话类型逐个点）。下面头三条就是钉这个的。
    """

    def _enabled(self, settings, target="group", tid="123"):
        with mock.patch.object(agents, "load_settings", return_value=settings):
            return agents.image_audit_enabled("qq", target, tid)

    def _globals(self, settings):
        with mock.patch.object(agents, "load_settings", return_value=settings):
            return agents.image_audit_globals("qq")

    def test_default_is_off(self):
        """**默认全关** —— 跟加这个功能之前的行为一致。"""
        self.assertFalse(self._enabled({}))

    def test_groups_switch_covers_every_group(self):
        """群聊总开关一开，**所有群**都生效，不用一个一个点。"""
        s = {"image_audit_groups": True}
        for gid in ("123", "456", "789"):
            self.assertTrue(self._enabled(s, target="group", tid=gid), gid)

    def test_groups_switch_does_not_touch_private(self):
        """**只给群开、私聊不开** —— 这条是这次改动的核心，别合并回一个开关。"""
        s = {"image_audit_groups": True}
        self.assertFalse(self._enabled(s, target="private", tid="456"))

    def test_private_switch_does_not_touch_groups(self):
        s = {"image_audit_private": True}
        self.assertTrue(self._enabled(s, target="private", tid="456"))
        self.assertFalse(self._enabled(s, target="group", tid="123"))

    def test_both_switches_on(self):
        s = {"image_audit_groups": True, "image_audit_private": True}
        self.assertTrue(self._enabled(s, target="group", tid="123"))
        self.assertTrue(self._enabled(s, target="private", tid="456"))

    def test_session_override_wins_over_scope_switch(self):
        s = {"image_audit_groups": True,
             "image_audit_overrides": {"123": False}}
        self.assertFalse(self._enabled(s))
        s = {"image_audit_groups": False,
             "image_audit_overrides": {"123": True}}
        self.assertTrue(self._enabled(s))

    def test_override_only_applies_to_that_session(self):
        s = {"image_audit_overrides": {"123": True}}
        self.assertTrue(self._enabled(s, tid="123"))
        self.assertFalse(self._enabled(s, tid="999"))

    def test_override_is_shared_between_group_and_private(self):
        """群和私聊共用一张覆盖表（跟 image_send_format 同口径）。"""
        s = {"image_audit_overrides": {"456": True}}
        self.assertTrue(self._enabled(s, target="private", tid="456"))

    def test_non_bool_override_is_ignored(self):
        """手改坏了 settings.json 时别把野值当 True 用。"""
        s = {"image_audit_groups": False,
             "image_audit_overrides": {"123": "yes"}}
        self.assertFalse(self._enabled(s))

    def test_non_bool_switch_is_ignored(self):
        self.assertFalse(self._enabled({"image_audit_groups": "yes"}))

    def test_globals_reports_both_scopes(self):
        """管理页顶部两个按钮的取值来源。"""
        self.assertEqual(self._globals({}),
                         {"group": False, "private": False})
        self.assertEqual(self._globals({"image_audit_groups": True}),
                         {"group": True, "private": False})
        self.assertEqual(self._globals({"image_audit_private": True}),
                         {"group": False, "private": True})
        self.assertEqual(
            self._globals({"image_audit_groups": True,
                           "image_audit_private": True}),
            {"group": True, "private": True})

    def test_globals_ignores_session_overrides(self):
        """总开关只反映全局，**不该被某个会话的覆盖带跑**。"""
        s = {"image_audit_overrides": {"123": True, "456": True}}
        self.assertEqual(self._globals(s),
                         {"group": False, "private": False})


class PromptOverrideTest(unittest.TestCase):
    """自定义审核提示词（2026-10-01 用户要求「提示词我要能自己改」）。

    它整份替换 `_PROMPT`，所以两件事都要钉：**存进去的能生效**，以及
    **没存 / 存坏了回落内置默认**——后者更要紧。配置写坏绝不能把闸门放松，
    这是 fail-closed 那条原则在配置层的延伸。
    """

    def _setting(self, settings):
        with mock.patch.object(agents, "load_settings", return_value=settings):
            return agents.image_audit_prompt("qq")

    def test_missing_is_empty(self):
        self.assertEqual(self._setting({}), "")

    def test_blank_is_empty(self):
        """全空白 = 没设（管理页留空保存走的就是这条路）。"""
        self.assertEqual(self._setting({"image_audit_prompt": "   \n  "}), "")

    def test_non_string_is_empty(self):
        """手改坏了存成数字 / null / 列表，一律当没设。"""
        for bad in (123, None, [], {"a": 1}, True):
            self.assertEqual(self._setting({"image_audit_prompt": bad}), "",
                             repr(bad))

    def test_string_is_stripped(self):
        self.assertEqual(self._setting({"image_audit_prompt": "  从严  "}), "从严")

    def test_resolve_prefers_custom(self):
        with mock.patch.object(agents, "load_settings",
                               return_value={"image_audit_prompt": "我的口径"}):
            self.assertEqual(image_audit.resolve_prompt("qq"), "我的口径")

    def test_resolve_falls_back_to_default(self):
        with mock.patch.object(agents, "load_settings", return_value={}):
            self.assertEqual(image_audit.resolve_prompt("qq"),
                             image_audit.default_prompt())

    def test_resolve_falls_back_when_settings_blow_up(self):
        """读配置出岔子也不能变成「用空提示词去审」——那等于不审。"""
        with mock.patch.object(agents, "load_settings",
                               side_effect=OSError("settings 读挂了")):
            self.assertEqual(image_audit.resolve_prompt("qq"),
                             image_audit.default_prompt())

    def test_check_defaults_to_builtin_prompt(self):
        """check() 不传 prompt（测试、或直接调用）时仍然是内置那份。"""
        path = _write_png()
        try:
            with mock.patch("app.vision.describe",
                            return_value='{"allow": true}') as desc:
                image_audit.check(path)
            self.assertEqual(desc.call_args[1]["prompt"], image_audit._PROMPT)
        finally:
            os.remove(path)

    def test_allow_send_uses_the_custom_prompt(self):
        """端到端：管理页存的那份真的被发去识图，而不是继续用内置的。"""
        path = _write_png()
        try:
            with mock.patch.object(agents, "image_audit_enabled",
                                   return_value=True), \
                 mock.patch.object(agents, "load_settings",
                                   return_value={"image_audit_prompt":
                                                 "我的口径"}), \
                 mock.patch("app.vision.describe",
                            return_value='{"allow": true}') as desc:
                self.assertTrue(image_audit.allow_send(
                    path, "qq", "group", "123"))
            self.assertEqual(desc.call_args[1]["prompt"], "我的口径")
        finally:
            os.remove(path)


class PromptAdminEndpointTest(unittest.TestCase):
    """管理页那个「审核提示词」按钮走的后端接口。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = os.path.join(self.tmp.name, "agents")
        os.makedirs(root, exist_ok=True)
        p = mock.patch.object(agents, "AGENTS_DIR", root)
        p.start()
        self.addCleanup(p.stop)
        agents.clear_cache()
        self.addCleanup(agents.clear_cache)
        p = mock.patch.object(main, "ADMIN_ALLOW_REMOTE", True)
        p.start()
        self.addCleanup(p.stop)
        self.client = main.app.test_client()

    def url(self):
        return "/api/agent/qq/image_audit_prompt"

    def test_save_then_read_back(self):
        r = self.client.put(self.url(), json={"prompt": "我的口径"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["prompt"], "我的口径")
        self.assertFalse(r.get_json()["using_default"])
        self.assertEqual(agents.image_audit_prompt("qq"), "我的口径")

    def test_blank_restores_the_builtin_default(self):
        self.client.put(self.url(), json={"prompt": "我的口径"})
        r = self.client.put(self.url(), json={"prompt": "   "})
        self.assertTrue(r.get_json()["using_default"])
        self.assertEqual(agents.image_audit_prompt("qq"), "")
        self.assertEqual(r.get_json()["effective"], image_audit.default_prompt())

    def test_missing_field_is_rejected(self):
        self.assertEqual(self.client.put(self.url(), json={}).status_code, 400)

    def test_non_string_is_rejected(self):
        self.assertEqual(
            self.client.put(self.url(), json={"prompt": 123}).status_code, 400)

    def test_too_long_is_rejected(self):
        r = self.client.put(self.url(), json={"prompt": "长" * 5000})
        self.assertEqual(r.status_code, 400)

    def test_sessions_payload_carries_both_prompt_fields(self):
        """编辑器靠 default 填初始内容，所以两个字段都得出现在列表接口里。"""
        d = self.client.get("/api/agent/qq/sessions").get_json()
        self.assertEqual(d["image_audit_prompt"], "")
        self.assertEqual(d["image_audit_prompt_default"],
                         image_audit.default_prompt())


class SendHookTest(unittest.TestCase):
    """挂钩点：image_jobs._send_image 被拦下时**不能真发**，也不抛异常。"""

    def _send(self, allow):
        from app import image_jobs
        with mock.patch("app.image_out.prepare_for_send",
                        return_value="D:/fake/out.jpg"), \
             mock.patch.object(image_jobs, "QQ_AGENT_ID", "qq"), \
             mock.patch("app.agents.image_send_format", return_value="jpg"), \
             mock.patch("app.image_audit.allow_send", return_value=allow), \
             mock.patch("app.qq_api.send_image") as send:
            rv = image_jobs._send_image("group", "123", "Anima_1_.png")
        return rv, send

    def test_blocked_does_not_send(self):
        rv, send = self._send(False)
        self.assertFalse(rv)
        send.assert_not_called()

    def test_allowed_sends(self):
        rv, send = self._send(True)
        self.assertTrue(rv)
        # 编号是贴在图上的 caption，_send_image **总是**带上这个 kwargs（没编号
        # 时是空串，等于不加那行字）。别写成不带 caption 的断言——那会假装
        # 「编号和图片在同一条消息」这件事不存在。
        send.assert_called_once_with("group", "123", "D:/fake/out.jpg",
                                     caption="")

    def test_send_image_returns_bool(self):
        """返回值必须是布尔 —— 调用方（process）靠它数「发出去几张」。"""
        rv, _ = self._send(True)
        self.assertIsInstance(rv, bool)


if __name__ == "__main__":
    unittest.main()
