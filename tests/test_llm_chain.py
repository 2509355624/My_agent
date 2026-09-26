# -*- coding: utf-8 -*-
"""主备模型降级链测试（app/llm.py）。

覆盖：链解析、候选顺序（管理页指定的排链头、链内去重）、失败后拉黑与到期
恢复、下次请求跳过拉黑项、流式/非流式的降级，以及两条**不该切换**的红线
——已经吐过正文、用户点了停止。这两条防的是「换个模型重来」把用户看到的
话劈成两段，或者为一个没人等的请求白花钱。
"""

import json
import threading
import unittest
from unittest import mock

import app.config as config
import app.llm as llm


def _sse(*texts):
    """构造 content 增量帧 + [DONE]。"""
    out = ["data: " + json.dumps({"choices": [{"delta": {"content": t}}]},
                                 ensure_ascii=False) for t in texts]
    out.append("data: [DONE]")
    return out


class _Resp:
    """假的 requests 响应。"""

    def __init__(self, status=200, lines=(), payload=None, reason="OK"):
        self.status_code = status
        self.reason = reason
        self.request = mock.Mock(url="https://example.invalid/chat/completions")
        self.elapsed = mock.Mock(total_seconds=lambda: 0.1)
        self._lines = list(lines)
        self._payload = payload if payload is not None else {
            "error": {"message": "余额不足"}}
        self.closed = False

    def iter_content(self, chunk_size=None):
        for line in self._lines:
            yield (line + "\n").encode("utf-8")

    def json(self):
        return self._payload

    def close(self):
        self.closed = True


class _CutResp(_Resp):
    """吐一块就断的流：模拟中途超时 / 连接被掐。"""

    def __init__(self, kind="content", text="半截"):
        super().__init__(lines=())
        self._key = "reasoning_content" if kind == "reasoning" else "content"
        self._text = text

    def iter_content(self, chunk_size=None):
        yield ("data: " + json.dumps(
            {"choices": [{"delta": {self._key: self._text}}]},
            ensure_ascii=False) + "\n").encode("utf-8")
        raise RuntimeError("连接被掐断")


class _ChainBase(unittest.TestCase):
    """把链固定成 volc:a → scnet2:b，并清掉跨用例的拉黑状态。"""

    CHAIN = "volc:a,scnet2:b"

    def setUp(self):
        llm.reset_chain_state()
        self.addCleanup(llm.reset_chain_state)
        p = mock.patch.object(llm, "LLM_FALLBACK_CHAIN", self.CHAIN)
        p.start()
        self.addCleanup(p.stop)

    def _patch_post(self, responses):
        """按顺序把响应发给每一次请求；用完最后一条就一直用它。"""
        calls = []

        def fake_post(url, **kwargs):
            calls.append({"url": url, "kwargs": kwargs})
            idx = min(len(calls) - 1, len(responses) - 1)
            return responses[idx]

        p = mock.patch.object(llm._session, "post", fake_post)
        p.start()
        self.addCleanup(p.stop)
        return calls


class OutboundProxyTest(unittest.TestCase):
    """出网口必须无视本机系统代理。

    本机常驻 Clash 类工具会把代理写进注册表；代理进程一旦换端口或被杀，
    requests 的默认行为就会去连那个没人监听的端口，于是**所有 provider 一起
    ProxyError**——表现成"模型全挂了"，换哪家都救不回来（2026-09-26 实撞：
    注册表指向 127.0.0.1:65532，该端口无监听）。所以 llm / vision 的出网必须
    显式 trust_env=False，与 qq_api / comfy_src / image_out / model_catalog 一致。
    """

    def test_llm_session_ignores_system_proxy(self):
        self.assertFalse(llm._session.trust_env)

    def test_vision_session_ignores_system_proxy(self):
        from app import vision
        self.assertFalse(vision._session.trust_env)


class ParseChainTest(unittest.TestCase):
    def test_parses_pairs(self):
        self.assertEqual(llm.parse_chain("volc:a,scnet2:b"),
                         [("volc", "a"), ("scnet2", "b")])

    def test_tolerates_spaces_and_blank_items(self):
        self.assertEqual(llm.parse_chain(" volc : a ,, scnet2:b ,"),
                         [("volc", "a"), ("scnet2", "b")])

    def test_bare_provider_uses_its_default_model(self):
        self.assertEqual(llm.parse_chain("volc"),
                         [("volc", config.PROVIDERS["volc"]["model"])])

    def test_unknown_provider_is_dropped(self):
        self.assertEqual(llm.parse_chain("nope:x,volc:a"), [("volc", "a")])

    def test_empty_means_no_chain(self):
        self.assertEqual(llm.parse_chain(""), [])
        self.assertEqual(llm.parse_chain(None), [])


class CandidatesTest(_ChainBase):
    def test_configured_model_leads_the_chain(self):
        # 管理页配了 scnet2:b → 它排链头，链里重复的那项去掉
        self.assertEqual(llm.candidates("scnet2", "b"),
                         [("scnet2", "b"), ("volc", "a")])

    def test_without_provider_chain_starts_at_first_item(self):
        self.assertEqual(llm.candidates(), [("volc", "a"), ("scnet2", "b")])

    def test_unknown_provider_is_ignored(self):
        self.assertEqual(llm.candidates("nope", "x"),
                         [("volc", "a"), ("scnet2", "b")])

    def test_banned_candidate_is_skipped(self):
        llm._mark_dead(("volc", "a"), "额度不足")
        self.assertEqual(llm.candidates(), [("scnet2", "b")])

    def test_all_banned_falls_back_to_full_chain(self):
        for key in (("volc", "a"), ("scnet2", "b")):
            llm._mark_dead(key, "x")
        # 全拉黑说明情况变了（额度刚到账之类），照原样全试一遍比直接报错好
        self.assertEqual(llm.candidates(), [("volc", "a"), ("scnet2", "b")])

    def test_expired_ban_stops_counting(self):
        with mock.patch.object(llm, "LLM_FALLBACK_TTL", 0):
            llm._mark_dead(("volc", "a"), "x")
        self.assertEqual(llm.candidates(), [("volc", "a"), ("scnet2", "b")])

    def test_ban_is_shared_across_threads(self):
        t = threading.Thread(target=llm._mark_dead,
                             args=(("volc", "a"), "额度不足"))
        t.start()
        t.join()
        self.assertEqual(llm.candidates(), [("scnet2", "b")])


class StreamFallbackTest(_ChainBase):
    def test_falls_through_to_next_candidate(self):
        self._patch_post([_Resp(status=429, reason="Too Many Requests"),
                          _Resp(lines=_sse("备胎的回复"))])
        out = list(llm.call_llm_stream([{"role": "user", "content": "hi"}]))
        self.assertEqual(out, [("content", "备胎的回复")])

    def test_failed_candidate_is_banned(self):
        self._patch_post([_Resp(status=429), _Resp(lines=_sse("ok"))])
        list(llm.call_llm_stream([{"role": "user", "content": "hi"}]))
        self.assertGreater(llm._DEAD.get(("volc", "a"), 0), 0)

    def test_success_leaves_no_ban(self):
        self._patch_post([_Resp(lines=_sse("ok"))])
        list(llm.call_llm_stream([{"role": "user", "content": "hi"}]))
        self.assertEqual(llm._DEAD, {})

    def test_next_call_skips_the_banned_candidate(self):
        calls = self._patch_post([_Resp(status=429),
                                  _Resp(lines=_sse("ok"))])
        list(llm.call_llm_stream([{"role": "user", "content": "hi"}]))
        self.assertEqual(len(calls), 2)
        backup_url = calls[1]["url"]

        before = len(calls)
        list(llm.call_llm_stream([{"role": "user", "content": "再来一句"}]))
        # 主模型已被拉黑：这一次直接打备胎，不用先白撞一次
        self.assertEqual(len(calls) - before, 1)
        self.assertEqual(calls[-1]["url"], backup_url)

    def test_content_already_spoken_does_not_switch(self):
        # 正文都吐出来了，换模型重来会让群里看到两段接不上的话
        calls = self._patch_post([_CutResp(kind="content"),
                                  _Resp(lines=_sse("不该出现"))])
        with self.assertRaises(RuntimeError):
            list(llm.call_llm_stream([{"role": "user", "content": "hi"}]))
        self.assertEqual(len(calls), 1)

    def test_reasoning_alone_still_allows_switch(self):
        # 思考内容只是展示，不算开工——卡在这里更应该换一个模型重想
        self._patch_post([_CutResp(kind="reasoning"),
                          _Resp(lines=_sse("备胎的回复"))])
        out = list(llm.call_llm_stream([{"role": "user", "content": "hi"}]))
        self.assertEqual(out, [("reasoning", "半截"), ("content", "备胎的回复")])

    def test_all_failed_raises_the_last_error(self):
        self._patch_post([_Resp(status=500, reason="Server Error"),
                          _Resp(status=503, reason="Unavailable")])
        with self.assertRaises(RuntimeError) as cm:
            list(llm.call_llm_stream([{"role": "user", "content": "hi"}]))
        self.assertIn("503", str(cm.exception))

    def test_cancel_tries_nothing(self):
        calls = self._patch_post([_Resp(lines=_sse("x"))])
        ev = threading.Event()
        ev.set()
        out = list(llm.call_llm_stream([{"role": "user", "content": "hi"}],
                                       cancel_event=ev))
        self.assertEqual(out, [])
        self.assertEqual(calls, [])

    def test_timeout_is_passed_to_each_attempt(self):
        calls = self._patch_post([_Resp(status=429), _Resp(lines=_sse("ok"))])
        list(llm.call_llm_stream([{"role": "user", "content": "hi"}]))
        self.assertEqual(len(calls), 2)
        for c in calls:
            self.assertEqual(c["kwargs"]["timeout"], llm.LLM_REQUEST_TIMEOUT)

    def test_explicit_timeout_wins(self):
        calls = self._patch_post([_Resp(lines=_sse("ok"))])
        list(llm.call_llm_stream([{"role": "user", "content": "hi"}],
                                 timeout=7))
        self.assertEqual(calls[0]["kwargs"]["timeout"], 7)


class CallLlmFallbackTest(_ChainBase):
    def _ok(self, text):
        return _Resp(payload={"choices": [{"message": {"content": text}}]})

    def test_falls_through_and_returns_text(self):
        self._patch_post([_Resp(status=429, reason="Too Many Requests"),
                          self._ok("备胎回复")])
        self.assertEqual(llm.call_llm([{"role": "user", "content": "hi"}]),
                         "备胎回复")

    def test_all_failed_raises_the_last_error(self):
        self._patch_post([_Resp(status=500, reason="Server Error"),
                          _Resp(status=402, reason="Payment Required")])
        with self.assertRaises(RuntimeError) as cm:
            llm.call_llm([{"role": "user", "content": "hi"}])
        self.assertIn("402", str(cm.exception))

    def test_timeout_passed_to_each_attempt(self):
        calls = self._patch_post([_Resp(status=429), self._ok("ok")])
        llm.call_llm([{"role": "user", "content": "hi"}])
        self.assertEqual([c["kwargs"]["timeout"] for c in calls],
                         [llm.LLM_REQUEST_TIMEOUT, llm.LLM_REQUEST_TIMEOUT])

    def test_explicit_timeout_wins(self):
        calls = self._patch_post([self._ok("ok")])
        llm.call_llm([{"role": "user", "content": "hi"}], timeout=9)
        self.assertEqual(calls[0]["kwargs"]["timeout"], 9)


class EmptyChainTest(_ChainBase):
    CHAIN = ""

    def test_no_chain_and_no_provider_uses_env_default(self):
        eff = llm.get_effective_config(None, None)
        self.assertEqual(llm._chain_targets(None, None),
                         [(eff["provider"], eff["model"])])

    def test_explicit_provider_still_wins_when_chain_empty(self):
        self.assertEqual(llm.candidates("volc", "m"), [("volc", "m")])


class LogFormatTest(unittest.TestCase):
    def test_attempt_is_shown(self):
        eff = {"provider": "volc", "model": "m"}
        with mock.patch("builtins.print") as p:
            llm._log_effective(eff, stream=True, attempt=(2, 3))
        line = p.call_args[0][0]
        self.assertIn("stream [2/3]", line)
        self.assertIn("volc / m", line)

    def test_without_attempt_it_looks_like_before(self):
        eff = {"provider": "volc", "model": "m"}
        with mock.patch("builtins.print") as p:
            llm._log_effective(eff, stream=False)
        self.assertIn("sync @", p.call_args[0][0])


if __name__ == "__main__":
    unittest.main()
