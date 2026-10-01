"""发图审核闸门（app/image_audit.py）的锁。

这个模块只有两条规则，但两条都是「错了会很痛」的那种，所以逐条钉住：

1. **判定违规才拦**，其余一律放行 —— 识图超时 / 报错 / 回复解析不出来
   全部 fail-open。用户 2026-10-01 明确选的（理由见模块注释：识图 provider
   一抖就把所有图堵死，用户会以为自己的描述有问题）。
2. **开关没开时一次网络都不发** —— 三个发图点都挂了它，热路径上多一次
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

from app import agents, image_audit  # noqa: E402


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


class CheckFailOpenTest(unittest.TestCase):
    """check() 的三条失败路径必须全部放行，而且**不抛异常**。"""

    def test_missing_file_is_fail_open(self):
        v = image_audit.check("D:/definitely/not/here.jpg")
        self.assertTrue(v.allow)
        self.assertTrue(v.failed)

    def test_empty_file_is_fail_open(self):
        path = _write_png(b"")
        try:
            v = image_audit.check(path)
            self.assertTrue(v.allow)
            self.assertTrue(v.failed)
        finally:
            os.remove(path)

    def test_vision_error_is_fail_open(self):
        path = _write_png()
        try:
            with mock.patch("app.vision.describe",
                            side_effect=RuntimeError("识图请求失败（耗时 30.0s）")):
                v = image_audit.check(path)
            self.assertTrue(v.allow)
            self.assertTrue(v.failed)
        finally:
            os.remove(path)

    def test_unparseable_reply_is_fail_open(self):
        path = _write_png()
        try:
            with mock.patch("app.vision.describe",
                            return_value="我觉得这张图还行吧"):
                v = image_audit.check(path)
            self.assertTrue(v.allow)
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

    def test_swimsuit_passes(self):
        """用户定的边界：泳装 / 内衣一律放行。这条是需求本身。"""
        v = self._check_with(
            '{"allow": true, "reason": "泳装展示，未违规", "category": "ok"}')
        self.assertTrue(v.allow)
        self.assertFalse(v.failed)


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

    def test_vision_failure_still_passes(self):
        """fail-open 的端到端验证：识图挂了，图照样发。"""
        with self._with_enabled(True), \
             mock.patch("app.vision.describe", side_effect=RuntimeError("boom")), \
             mock.patch.object(image_audit, "_notify_blocked") as notify:
            self.assertTrue(image_audit.allow_send(
                self.path, "qq", "group", "123"))
        notify.assert_not_called()

    def test_web_target_always_passes(self):
        """target=None（网页端）直接放行，连开关都不读。"""
        with mock.patch.object(agents, "image_audit_enabled") as gate, \
             mock.patch("app.vision.describe") as desc:
            self.assertTrue(image_audit.allow_send(
                self.path, "qq", None, None))
        gate.assert_not_called()
        desc.assert_not_called()

    def test_settings_read_error_is_fail_open(self):
        """连 settings 都读不出来时别把图卡住。"""
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
