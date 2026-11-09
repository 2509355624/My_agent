"""请求级自定义模型（app/llm.py 的 _custom_ctx）测试。

2026-11-05 网页端「自定义模型」：用户自己的 OpenAI 兼容接口存浏览器
localStorage，每轮聊天随请求带给后端，llm.set_custom_override 临时挂到
当前线程、请求结束摘掉。

2026-11-09 语义变了：自定义模型不再是「整轮换人」，而是扮演降级链的
第二段（链头永远是公用中转站 V4 Pro）。核心约束：

- 配置是有序列表，数组顺序 = 降级顺序；
- 扮演链不接全局 LLM_FALLBACK_CHAIN（第一项是付费的官方接口）；
- 全挂之后抛 PersonaChainExhausted，交给前端弹窗引导；
- 空列表 [] 也表示「这是扮演请求」，不能当 None 放回去接全局链；
- 测试链接（test_custom）测的是草稿配置，不碰线程覆盖状态。
"""

import unittest
from unittest import mock

from app import config, llm

_CFG = {"base_url": "https://my.relay/v1", "model": "my-model",
        "api_key": "sk-test-1234"}


class _FakeResp:
    def __init__(self, content="pong", status=200):
        self.status_code = status
        self._content = content
        import json as _json
        self._body = {
            "choices": [{"message": {"content": content}}],
            "usage": {"total_tokens": 1, "prompt_tokens": 1,
                      "completion_tokens": 1},
        }
        from datetime import timedelta
        self.elapsed = timedelta(seconds=0.4)

    def json(self):
        return self._body


def _fake_post(content="pong", status=200):
    resp = _FakeResp(content, status)
    m = mock.MagicMock(return_value=resp)
    m.call_args_list = []
    return m, resp


class OverrideTest(unittest.TestCase):
    def tearDown(self):
        llm.set_custom_override(None)

    def test_no_override_by_default(self):
        llm.set_custom_override(None)
        eff = llm.get_effective_config()
        self.assertNotEqual(eff["provider"], "custom")

    def test_override_changes_effective_config(self):
        llm.set_custom_override(_CFG)
        eff = llm.get_effective_config()
        self.assertEqual(eff["provider"], "custom")
        self.assertEqual(eff["base_url"], "https://my.relay/v1")
        self.assertEqual(eff["model"], "my-model")
        self.assertEqual(eff["api_key"], "sk-test-1234")

    def test_bad_cfg_is_ignored(self):
        # 缺 key / 缺地址 / 不是 dict：一律当「没挂」，回退默认模型。
        for bad in (None, {}, {"base_url": "https://a/v1"},
                    {"api_key": "sk"}, "junk", 123):
            llm.set_custom_override(bad)
            self.assertIsNone(llm._custom_active(), repr(bad))

    def test_explicit_provider_beats_override(self):
        # 调用方点名 provider（审核、识图这类）时不被覆盖劫走。
        llm.set_custom_override(_CFG)
        eff = llm.get_effective_config("deepseek")
        self.assertEqual(eff["provider"], "deepseek")

    def test_clear_restores(self):
        llm.set_custom_override(_CFG)
        llm.set_custom_override(None)
        self.assertIsNone(llm._custom_active())
        self.assertNotEqual(llm.get_effective_config()["provider"], "custom")


class CallLlmTest(unittest.TestCase):
    def tearDown(self):
        llm.set_custom_override(None)

    def test_call_goes_to_custom_url(self):
        llm.set_custom_override(_CFG)
        m, _resp = _fake_post("好")
        with mock.patch.object(llm, "_session") as fake:
            fake.post = m
            out = llm.call_llm([{"role": "user", "content": "hi"}])
        self.assertEqual(out, "好")
        url = m.call_args[0][0]
        self.assertTrue(url.startswith("https://my.relay/v1/chat/completions"))
        headers = m.call_args[1]["headers"]
        self.assertEqual(headers["Authorization"], "Bearer sk-test-1234")
        body = m.call_args[1]["json"]
        self.assertEqual(body["model"], "my-model")

    def test_custom_failure_raises_no_fallback(self):
        # 自定义接口挂了 → 原样抛错；绝不允许顺着降级链烧默认那几家的额度。
        llm.set_custom_override(_CFG)
        boom = mock.MagicMock(
            side_effect=RuntimeError("LLM 请求失败 HTTP 401"))
        with mock.patch.object(llm, "_session") as fake:
            fake.post = boom
            with self.assertRaises(RuntimeError) as cm:
                llm.call_llm([{"role": "user", "content": "hi"}])
        self.assertIn("401", str(cm.exception))
        # 只打了一次：没有第二个候选。
        self.assertEqual(boom.call_count, 1)

    def test_no_override_uses_normal_chain(self):
        # 没挂覆盖时行为不变：这条路径由既有测试兜着，这里只确认不会误入
        # custom 分支（mock 打断即可，不真出网）。
        llm.set_custom_override(None)
        with mock.patch.object(llm, "_chain_targets",
                               return_value=[("deepseek", "deepseek-chat")]), \
             mock.patch.object(llm, "get_effective_config", return_value={
                 "provider": "deepseek", "base_url": "https://api.deepseek.com/v1",
                 "model": "deepseek-chat", "api_key": "k",
                 "reasoning_effort": ""}), \
             mock.patch.object(llm, "_call_provider",
                               return_value="ok") as call:
            out = llm.call_llm([{"role": "user", "content": "hi"}])
        self.assertEqual(out, "ok")
        self.assertEqual(call.call_args[0][0]["provider"], "deepseek")


class TestCustomEndpointTest(unittest.TestCase):
    def test_ok(self):
        m, _resp = _fake_post("pong!")
        with mock.patch.object(llm, "_session") as fake:
            fake.post = m
            ok, elapsed, info = llm.test_custom("https://my.relay/v1/",
                                                "my-model", "sk-1")
        self.assertTrue(ok)
        self.assertEqual(info, "pong!")
        self.assertTrue(url_ok(m.call_args[0][0]))

    def test_fail_returns_error_text(self):
        boom = mock.MagicMock(side_effect=RuntimeError("HTTP 401 无效密钥"))
        with mock.patch.object(llm, "_session") as fake:
            fake.post = boom
            ok, elapsed, info = llm.test_custom("https://a/v1", "m", "bad")
        self.assertFalse(ok)
        self.assertIn("401", info)

    def test_does_not_touch_thread_override(self):
        # 测试的是草稿；测完线程覆盖状态不能被带脏。
        llm.set_custom_override(None)
        m, _resp = _fake_post()
        with mock.patch.object(llm, "_session") as fake:
            fake.post = m
            llm.test_custom("https://a/v1", "m", "k")
        self.assertIsNone(llm._custom_active())


class PersonaChainTest(unittest.TestCase):
    """2026-11-09：扮演链 = 公用中转站链头 + 玩家自定义模型（按列表顺序）。

    站主的钱不能被烧：中转站闭站时，以前会自动落到全局降级链里付费的
    官方接口；现在链尾只有玩家自己填的模型，一条都没有就直接报错弹窗。
    """

    def setUp(self):
        # 别的测试失败时会往 _DEAD 里写拉黑，不清会漏掉候选。
        llm._DEAD.clear()

    def tearDown(self):
        llm.set_custom_override(None)
        llm._DEAD.clear()

    def test_head_is_public_relay_and_customs_follow_in_order(self):
        cfg = [
            {"base_url": "https://a.example/v1", "model": "m-a",
             "api_key": "k1"},
            {"base_url": "https://b.example/v1", "model": "m-b",
             "api_key": "k2"},
        ]
        llm.set_custom_override(cfg)
        c = llm.candidates(config.PERSONA_PROVIDER, config.PERSONA_MODEL)
        self.assertEqual(c[0], (config.PERSONA_PROVIDER, config.PERSONA_MODEL))
        self.assertEqual(len(c), 3)
        self.assertTrue(all(x[0] == llm._PERSONA_CUSTOM_PID for x in c[1:]))
        self.assertEqual([x[1] for x in c[1:]], ["0", "1"])

    def test_paid_global_chain_is_never_appended(self):
        llm.set_custom_override([_CFG])
        c = llm.candidates(config.PERSONA_PROVIDER, config.PERSONA_MODEL)
        for item in llm.parse_chain(config.LLM_FALLBACK_CHAIN):
            self.assertNotIn(item, c)

    def test_empty_list_still_marks_a_persona_request(self):
        # [] ≠ None：传 None 会被当成「非扮演请求」，付费全局链接回来。
        llm.set_custom_override([])
        self.assertTrue(llm.persona_chain_active())
        self.assertEqual(llm.persona_customs(), [])
        self.assertEqual(
            llm.candidates(config.PERSONA_PROVIDER, config.PERSONA_MODEL),
            [(config.PERSONA_PROVIDER, config.PERSONA_MODEL)])

    def test_custom_candidate_resolves_to_the_right_model(self):
        llm.set_custom_override([
            _CFG,
            {"base_url": "https://b.example/v1", "model": "m-b",
             "api_key": "k2"},
        ])
        eff = llm.get_effective_config(llm._PERSONA_CUSTOM_PID, "1")
        self.assertEqual(eff["provider"], "custom")
        self.assertEqual(eff["model"], "m-b")
        self.assertEqual(eff["base_url"], "https://b.example/v1")

    def test_exhaustion_is_a_typed_error(self):
        llm.set_custom_override([])
        with mock.patch.object(llm, "_call_provider",
                               side_effect=RuntimeError("boom")), \
             mock.patch.object(llm, "_retry_wait", return_value=None):
            with self.assertRaises(llm.PersonaChainExhausted) as cm:
                llm.call_llm([{"role": "user", "content": "hi"}],
                             provider=config.PERSONA_PROVIDER,
                             model=config.PERSONA_MODEL)
        self.assertFalse(cm.exception.has_custom)

    def test_successful_head_is_reported_as_public(self):
        llm.set_custom_override([_CFG])
        with mock.patch.object(llm, "_call_provider", return_value="ok"):
            out = llm.call_llm([{"role": "user", "content": "hi"}],
                               provider=config.PERSONA_PROVIDER,
                               model=config.PERSONA_MODEL)
        self.assertEqual(out, "ok")
        used = llm.persona_last_model()
        self.assertEqual(used["kind"], "public")
        self.assertEqual(used["name"], "公用 V4 Pro")

    def test_custom_success_is_reported_with_its_own_name(self):
        llm.set_custom_override([_CFG])
        llm._note_persona_used(llm._PERSONA_CUSTOM_PID, {"model": "m-a"})
        used = llm.persona_last_model()
        self.assertEqual(used["kind"], "custom")
        self.assertEqual(used["name"], "m-a")


def url_ok(u):
    return u.startswith("https://") and u.endswith("/chat/completions")


if __name__ == "__main__":
    unittest.main()
