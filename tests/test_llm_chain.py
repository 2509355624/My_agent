# -*- coding: utf-8 -*-
"""主备模型降级链测试（app/llm.py）。

覆盖：链解析、候选顺序（管理页指定的排链头、链内去重）、失败后拉黑与到期
恢复、下次请求跳过拉黑项、流式/非流式的降级，以及两条**不该切换**的红线
——已经吐过正文、用户点了停止。这两条防的是「换个模型重来」把用户看到的
话劈成两段，或者为一个没人等的请求白花钱。
"""

import json
import threading
import time
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


class RateLimitBanTest(_ChainBase):
    """429（频率/额度到顶）要拉黑一整天，而不是默认的 600 秒。

    429 不是「这条请求失败」，是「这家这一阵都不给了」——免费额度按天重置，
    600 秒后重试纯属白撞。拉黑一整天，让降级链直接把请求交给还能用的模型。
    其余失败（网络抖动、模型退役）保持短 TTL，到期自愈，不用重启进程。
    """

    def test_rate_limit_ttl_defaults_to_a_full_day(self):
        self.assertEqual(config.LLM_RATE_LIMIT_TTL, 86400)

    def test_429_error_gets_the_long_ttl(self):
        err = RuntimeError("LLM 请求失败 HTTP 429 Too Many Requests @https://x")
        self.assertEqual(llm._ttl_for(err), llm.LLM_RATE_LIMIT_TTL)

    def test_other_error_gets_the_short_ttl(self):
        self.assertEqual(llm._ttl_for(RuntimeError("HTTP 500 Server Error")),
                         llm.LLM_FALLBACK_TTL)

    def test_explicit_ttl_overrides_the_default(self):
        before = time.time()
        llm._mark_dead(("volc", "a"), "x", ttl=123)
        remain = llm._DEAD[("volc", "a")] - before
        self.assertAlmostEqual(remain, 123, delta=5)

    def test_mark_dead_without_ttl_uses_the_short_default(self):
        before = time.time()
        llm._mark_dead(("volc", "a"), "额度不足")
        remain = llm._DEAD[("volc", "a")] - before
        self.assertAlmostEqual(remain, llm.LLM_FALLBACK_TTL, delta=5)

    def test_429_through_stream_bans_for_a_day(self):
        before = time.time()
        self._patch_post([_Resp(status=429, reason="Too Many Requests"),
                          _Resp(lines=_sse("ok"))])
        list(llm.call_llm_stream([{"role": "user", "content": "hi"}]))
        remain = llm._DEAD[("volc", "a")] - before
        self.assertGreater(remain, llm.LLM_RATE_LIMIT_TTL - 10)

    def test_429_through_sync_bans_for_a_day(self):
        before = time.time()
        self._patch_post([_Resp(status=429, reason="Too Many Requests"),
                          _Resp(payload={"choices": [
                              {"message": {"content": "ok"}}]})])
        llm.call_llm([{"role": "user", "content": "hi"}])
        remain = llm._DEAD[("volc", "a")] - before
        self.assertGreater(remain, llm.LLM_RATE_LIMIT_TTL - 10)

    def test_non_429_failure_keeps_the_short_ban(self):
        before = time.time()
        self._patch_post([_Resp(status=500, reason="Server Error"),
                          _Resp(lines=_sse("ok"))])
        list(llm.call_llm_stream([{"role": "user", "content": "hi"}]))
        remain = llm._DEAD[("volc", "a")] - before
        self.assertLess(remain, llm.LLM_FALLBACK_TTL + 10)


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
    """模型调用行走的是 logging（logger 名 `llm`），不是 print。

    这是刻意换的：print 落 stdout，输出一旦被重定向/管道接管就是块缓冲，
    `logging` 的行还在、这些行却要攒满 8KB 才吐——2026-09-27 用户报
    「看不到模型调用日志」就是这个原因（见 app/logsetup.py）。
    """

    def test_attempt_is_shown(self):
        eff = {"provider": "volc", "model": "m"}
        with self.assertLogs("llm", level="INFO") as cm:
            llm._log_effective(eff, stream=True, attempt=(2, 3))
        line = cm.output[0]
        self.assertIn("stream [2/3]", line)
        self.assertIn("volc / m", line)

    def test_without_attempt_has_no_index(self):
        """不带序号时不出现 [n/m]——链只有一项时别让日志看着像降级过。"""
        eff = {"provider": "volc", "model": "m"}
        with self.assertLogs("llm", level="INFO") as cm:
            llm._log_effective(eff, stream=False)
        line = cm.output[0]
        self.assertIn("sync", line)
        self.assertNotIn("[1/", line)


class RequireVisionTest(_ChainBase):
    """带图轮次：降级链只走**能读图**的候选（2026-10-02 修的坑）。

    背景：图片是以多模态（base64）塞进 messages 的。纯文本 provider 收到
    base64 不会报错，而是**整条请求挂死**（见 config.PROVIDERS 上方注释）。
    而 agent 判「本轮模型有没有视觉」用的是**配置**的模型，降级链却会切到链上
    别的 provider —— 配了有视觉的模型（deepseek）一旦失败降到纯文本（volc），
    就会把 base64 丢给纯文本模型，白等一个超时。所以带图时必须收窄链。
    """

    CHAIN = "volc:a,deepseek:d,mimo:m"

    def test_vision_only_keeps_capable_candidates(self):
        # volc（纯文本）被剔掉，deepseek / mimo（provider 声明有视觉）留下
        self.assertEqual(llm.candidates(None, None, require_vision=True),
                         [("deepseek", "d"), ("mimo", "m")])

    def test_vision_only_keeps_a_vision_head(self):
        # 管理页选了有视觉的模型 → 它照样排链头，只是后面不再挂 volc
        self.assertEqual(llm.candidates("deepseek", "d", require_vision=True),
                         [("deepseek", "d"), ("mimo", "m")])

    def test_default_keeps_everything(self):
        # 不带图（默认）→ 一个字都没变，volc 还在链上
        self.assertEqual(llm.candidates(None, None),
                         [("volc", "a"), ("deepseek", "d"), ("mimo", "m")])

    def test_falls_back_to_original_when_no_vision_candidate(self):
        # 链里一个能读图的都没有 → 退回原样，不返回空表（宁可照老路试）
        with mock.patch.object(llm, "LLM_FALLBACK_CHAIN", "volc:a,scnet2:b"):
            self.assertEqual(llm.candidates(None, None, require_vision=True),
                             [("volc", "a"), ("scnet2", "b")])

    def test_stream_skips_text_only_provider_when_vision_required(self):
        # 链头（deepseek）失败后应降到 mimo，而不是纯文本的 volc
        calls = self._patch_post([_Resp(status=500, reason="Server Error"),
                                  _Resp(lines=_sse("备胎的回复"))])
        out = list(llm.call_llm_stream([{"role": "user", "content": "hi"}],
                                       require_vision=True))
        self.assertEqual(out, [("content", "备胎的回复")])
        self.assertEqual(len(calls), 2)
        self.assertEqual(
            calls[0]["url"],
            config.PROVIDERS["deepseek"]["base_url"].rstrip("/")
            + "/chat/completions")
        self.assertEqual(
            calls[1]["url"],
            config.PROVIDERS["mimo"]["base_url"].rstrip("/")
            + "/chat/completions")
        self.assertNotIn(config.PROVIDERS["volc"]["base_url"],
                         [c["url"] for c in calls])


if __name__ == "__main__":
    unittest.main()
