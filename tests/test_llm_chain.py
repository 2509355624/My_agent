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
        # 重试之间不真睡：用例只关心「试了几次、最后给谁」，不关心等待。
        q = mock.patch.object(llm, "LLM_RETRY_DELAY", 0)
        q.start()
        self.addCleanup(q.stop)

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
    """429 的拉黑时长（2026-11-09 用户重定：一整天 → 30 秒）。

    旧口径「429 = 额度到顶，黑一天」被 hfapi 打脸：它的并发上限只有 2，而
    角色扮演一轮要打编剧+写作两次调用，两个玩家同时在线必撞 429。11-09 实录
    连着黑 86400 秒——站明明活着（vision 同刻还在用），角色扮演却整天走付费
    的官方 DeepSeek。现在所有 429 最多黑 `LLM_RATE_LIMIT_TTL`（30 秒），
    到期自动回头试；限流措辞（too many requests 等）按瞬时故障走，先重试。
    """

    def test_rate_limit_ttl_is_thirty_seconds(self):
        self.assertEqual(config.LLM_RATE_LIMIT_TTL, 30)

    def test_bare_429_is_rate_limit_and_retried(self):
        """光秃秃的 429 Too Many Requests 按限流算：瞬时 → 重试 + 短拉黑。

        hfapi 的 429 就是这个措辞（并发撞上限），重试两下常常就好了。
        """
        err = RuntimeError("LLM 请求失败 HTTP 429 Too Many Requests @https://x")
        self.assertTrue(llm._is_transient(err))
        self.assertEqual(llm._ttl_for(err), llm.LLM_CONCURRENCY_TTL)

    def test_429_without_hint_words_not_retried(self):
        """没措辞的裸 429（比如「余额不足」）：不在同一家白撞，直接降级；
        拉黑仍是 30 秒——过会儿再试，不是今天别碰。"""
        err = RuntimeError("LLM 请求失败 HTTP 429 @https://x | insufficient balance")
        self.assertFalse(llm._is_transient(err))
        self.assertEqual(llm._ttl_for(err), llm.LLM_RATE_LIMIT_TTL)
        self.assertLessEqual(llm.LLM_RATE_LIMIT_TTL, 60)

    def test_concurrency_429_gets_the_short_ttl(self):
        """并发冲突也是 429，但它几十秒自愈，不能按额度到顶藏一整天。

        2026-10-26 实录（hfapi 中转站并发上限 2）：
        {"code": "concurrency", "message": "too many concurrent requests
         for this api key (2 in flight, limit 2)"}
        """
        err = RuntimeError(
            'LLM 请求失败 HTTP 429 {"code": "concurrency", "message": '
            '"too many concurrent requests for this api key '
            '(2 in flight, limit 2)"}')
        self.assertEqual(llm._ttl_for(err), llm.LLM_CONCURRENCY_TTL)
        # 2026-11-09 起两档同为 30 秒（用户统一口径），并发冲突照旧短
        self.assertLessEqual(llm.LLM_CONCURRENCY_TTL, llm.LLM_RATE_LIMIT_TTL)

    def test_other_error_gets_the_short_ttl(self):
        self.assertEqual(llm._ttl_for(RuntimeError("HTTP 500 Server Error")),
                         llm.LLM_FALLBACK_TTL)

    def test_transient_503_gets_the_short_ttl(self):
        """503/502/504 = 服务端临时扛不住，几十秒自愈，不该按硬故障黑一小时。

        2026-11-03 实录：hfapi 中转站并发占满直接回 503，而它落到
        `LLM_FALLBACK_TTL`（.env 里 3600 秒），一次过载把中转站黑了一小时。
        本质和 concurrency 是同一件事，只是措辞不同。
        """
        for err in (
            "LLM 请求失败 HTTP 503 Service Unavailable @https://hfapi.cloud/v1",
            "HTTP 502 Bad Gateway",
            "HTTP 504 Gateway Timeout",
            "service temporarily unavailable",
        ):
            self.assertEqual(llm._ttl_for(RuntimeError(err)),
                             llm.LLM_CONCURRENCY_TTL, err)

    def test_503_short_ttl_is_much_shorter_than_fallback(self):
        self.assertLess(llm.LLM_CONCURRENCY_TTL, llm.LLM_FALLBACK_TTL)

    def test_timeout_and_connection_errors_get_the_short_ttl(self):
        """读超时 / 连接被掐也是「等一下就好」，不能按硬故障黑一小时。

        2026-11-09 实录：当天 11 次 `Read timed out` 和 1 次
        `Connection aborted / RemoteDisconnected` 全落了 3600 秒拉黑，
        中转站一整天被反复关停，后面只能走付费的官方 DeepSeek。
        """
        for err in (
            "HTTPSConnectionPool(host='hfapi.cloud', port=443): "
            "Read timed out. (read timeout=60)",
            "('Connection aborted.', RemoteDisconnected('Remote end closed "
            "connection without response'))",
        ):
            self.assertEqual(llm._ttl_for(RuntimeError(err)),
                             llm.LLM_CONCURRENCY_TTL, err)
        # 中文系统上的错误原文一个英文关键词都没有，只能按异常类型认
        self.assertEqual(llm._ttl_for(ConnectionResetError(
            10054, "远程主机强迫关闭了一个现有的连接。")), llm.LLM_CONCURRENCY_TTL)

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

    def test_429_through_stream_bans_short(self):
        before = time.time()
        self._patch_post([_Resp(status=429, reason=""),
                          _Resp(lines=_sse("ok"))])
        list(llm.call_llm_stream([{"role": "user", "content": "hi"}]))
        remain = llm._DEAD[("volc", "a")] - before
        self.assertGreater(remain, llm.LLM_RATE_LIMIT_TTL - 10)

    def test_429_through_sync_bans_short(self):
        before = time.time()
        self._patch_post([_Resp(status=429, reason=""),
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


class RetrySameCandidateTest(_ChainBase):
    """瞬时故障先在**同一个候选**上重试，3 次都不过才降级（2026-11-09）。

    起因：中转站只是这一秒抽风（502 / 读超时 / 连接被掐），旧逻辑一失败就
    拉黑一小时，后面全走付费的官方 DeepSeek。用户拍板「重试 3 次之后再
    走降级链路」。
    """

    def test_transient_failure_retries_the_same_candidate_first(self):
        calls = self._patch_post([_Resp(status=502, reason="Bad Gateway"),
                                  _Resp(lines=_sse("重试成功"))])
        out = list(llm.call_llm_stream([{"role": "user", "content": "hi"}]))
        self.assertEqual(out, [("content", "重试成功")])
        self.assertEqual(len(calls), 2)
        # 两次都打同一家（链头），没有提前降级
        self.assertEqual(len({c["url"] for c in calls}), 1)
        self.assertEqual(llm._DEAD, {})

    def test_three_attempts_then_falls_through(self):
        calls = self._patch_post([_Resp(status=502, reason="Bad Gateway"),
                                  _Resp(status=502, reason="Bad Gateway"),
                                  _Resp(status=502, reason="Bad Gateway"),
                                  _Resp(lines=_sse("备胎的回复"))])
        out = list(llm.call_llm_stream([{"role": "user", "content": "hi"}]))
        self.assertEqual(out, [("content", "备胎的回复")])
        self.assertEqual(len(calls), 4)      # 3 次链头 + 1 次备胎
        self.assertEqual(len({c["url"] for c in calls[:3]}), 1)

    def test_retry_exhausted_ban_is_short(self):
        """重试也失败后只拉黑 60 秒——下一轮就能再用，不是一小时。"""
        before = time.time()
        self._patch_post([_Resp(status=503, reason="Service Unavailable"),
                          _Resp(status=503, reason="Service Unavailable"),
                          _Resp(status=503, reason="Service Unavailable"),
                          _Resp(lines=_sse("ok"))])
        list(llm.call_llm_stream([{"role": "user", "content": "hi"}]))
        remain = llm._DEAD[("volc", "a")] - before
        self.assertLess(remain, llm.LLM_CONCURRENCY_TTL + 10)
        self.assertLess(remain, llm.LLM_FALLBACK_TTL)

    def test_plain_429_is_not_retried(self):
        """裸 429（无限流措辞）重试多少次都一样，立刻降级（桩默认错误体「余额不足」）。"""
        calls = self._patch_post([_Resp(status=429, reason=""),
                                  _Resp(lines=_sse("ok"))])
        list(llm.call_llm_stream([{"role": "user", "content": "hi"}]))
        self.assertEqual(len(calls), 2)

    def test_over_budget_skips_the_retry(self):
        """读超时已经吃满 60 秒，再重试三次等于让用户干等三分钟 → 直接降级。"""
        calls = self._patch_post([_Resp(status=504, reason="Gateway Timeout"),
                                  _Resp(lines=_sse("ok"))])
        with mock.patch.object(llm, "LLM_RETRY_BUDGET", 0):
            out = list(llm.call_llm_stream([{"role": "user", "content": "hi"}]))
        self.assertEqual(out, [("content", "ok")])
        self.assertEqual(len(calls), 2)

    def test_sync_call_also_retries(self):
        calls = self._patch_post([
            _Resp(status=502, reason="Bad Gateway"),
            _Resp(payload={"choices": [{"message": {"content": "重试成功"}}]})])
        self.assertEqual(llm.call_llm([{"role": "user", "content": "hi"}]),
                         "重试成功")
        self.assertEqual(len(calls), 2)
        self.assertEqual(llm._DEAD, {})


class StreamFallbackTest(_ChainBase):
    def test_falls_through_to_next_candidate(self):
        self._patch_post([_Resp(status=429, reason=""),
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
        self._patch_post([_Resp(status=429, reason=""),
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


class StrictModeTest(_ChainBase):
    """strict=True：网页端「我选谁就是谁」——不降级、不兜底（2026-10-03）。

    背景：网页端模型面板选的那个原本只是**链头**，链尾会顶上。三处会让面板上
    的名字和真正答话的模型不是同一个：① 链头失败顺链降级 ② 空回复换家
    ③ 链头在拉黑期内被静默跳过。网页端要的是排他——失败就是失败，至少账单、
    日志和面板对得上。QQ 侧不用 strict，照旧靠链兜底。
    """

    def test_strict_keeps_only_the_head(self):
        self.assertEqual(llm.candidates("scnet2", "b", strict=True),
                         [("scnet2", "b")])

    def test_strict_ignores_the_rest_of_the_chain(self):
        self.assertEqual(llm.candidates("volc", "a", strict=True), [("volc", "a")])

    def test_strict_tries_even_a_banned_model(self):
        # 明确选了它就照样发，不被失效记忆拦下——拦下等于偷偷换人
        llm._mark_dead(("volc", "a"), "额度不足")
        self.assertEqual(llm.candidates("volc", "a", strict=True), [("volc", "a")])

    def test_strict_without_head_still_uses_the_chain(self):
        # 调用方压根没指定模型时 strict 不生效：否则返回空表=请求必然失败
        self.assertEqual(llm.candidates(None, None, strict=True),
                         [("volc", "a"), ("scnet2", "b")])

    def test_strict_stream_does_not_fall_back(self):
        calls = self._patch_post([_Resp(status=500, reason="Server Error")])
        with self.assertRaises(Exception):
            list(llm.call_llm_stream([{"role": "user", "content": "hi"}],
                                     provider="volc", model="a", strict=True))
        self.assertEqual(len(calls), 1)

    def test_non_strict_stream_still_falls_back(self):
        # 反向对照：同一个失败，不带 strict 就该顺链换家（QQ 侧的行为）
        calls = self._patch_post([_Resp(status=500, reason="Server Error"),
                                  _Resp(lines=_sse("备胎的回复"))])
        out = list(llm.call_llm_stream([{"role": "user", "content": "hi"}],
                                       provider="volc", model="a"))
        self.assertEqual(out, [("content", "备胎的回复")])
        self.assertEqual(len(calls), 2)

    def test_strict_sync_call_does_not_fall_back(self):
        calls = self._patch_post([_Resp(status=500, reason="Server Error")])
        with self.assertRaises(Exception):
            llm.call_llm([{"role": "user", "content": "hi"}],
                         provider="volc", model="a", strict=True)
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
