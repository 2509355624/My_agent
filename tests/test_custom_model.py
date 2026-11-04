"""请求级自定义模型（app/llm.py 的 _custom_ctx）测试。

2026-11-05 网页端「自定义模型」：用户自己的 OpenAI 兼容接口存浏览器
localStorage，每轮聊天随请求带给后端，llm.set_custom_override 临时挂到
当前线程、请求结束摘掉。核心约束：

- 覆盖是整轮换人：挂上后 get_effective_config / call_llm 都走用户的接口；
- 不进降级链：自定义接口挂了直接抛，绝不偷偷换默认那几家；
- 测试链接（test_custom）测的是草稿配置，不碰线程覆盖状态。
"""

import unittest
from unittest import mock

from app import llm

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


def url_ok(u):
    return u.startswith("https://") and u.endswith("/chat/completions")


if __name__ == "__main__":
    unittest.main()
