# -*- coding: utf-8 -*-
"""LLM 流式（SSE）解析测试（app/llm.py）。

覆盖三件容易出错、回归代价又很高的事：
1. reasoning / content 的分流与顺序（思考内容只展示、不进上下文）；
2. 噪声帧（空行、非 data 行、坏 JSON、[DONE]、非对象 JSON）不中断整条流；
3. UTF-8 增量解码——HTTP 分块可能把一个汉字劈成两半，逐块 decode 会解成
   乱码，必须走 incremental decoder。
另外钉住两件协议约定：
- thinking / stream_options 只发给火山方舟，不能塞给 DeepSeek 官方
  （未知字段可能被判 400）；
- 真撞上 400 时要去掉扩展字段重试一次。
"""

import json
import tempfile
import threading
import unittest
from unittest import mock

import app.llm as llm
from app import usage as usage_stats


def setUpModule():
    """本文件不少用例直接调真的 _record_usage（带假 usage 数据），
    它会归账到 app/usage——不把 BASE_DIR 挪走就会写脏真实的每日账本。"""
    p = mock.patch.object(usage_stats, "BASE_DIR",
                          tempfile.mkdtemp(prefix="wb_usage_test_"))
    p.start()
    unittest.addModuleCleanup(p.stop)

_URL = "https://ark.cn-beijing.volces.com/api/v3/chat/completions"


def _frame(obj):
    return "data: " + json.dumps(obj, ensure_ascii=False)


def _delta(**kw):
    return _frame({"choices": [{"delta": kw}]})


class ParseSseLineTest(unittest.TestCase):
    def test_reasoning_delta(self):
        self.assertEqual(llm._parse_sse_line(_delta(reasoning_content="先想")),
                         [("reasoning", "先想")])

    def test_content_delta(self):
        self.assertEqual(llm._parse_sse_line(_delta(content="答案")),
                         [("content", "答案")])

    def test_reasoning_comes_before_content_in_same_frame(self):
        line = _delta(reasoning_content="想", content="答")
        self.assertEqual(llm._parse_sse_line(line),
                         [("reasoning", "想"), ("content", "答")])

    def test_empty_delta_yields_nothing(self):
        self.assertEqual(llm._parse_sse_line(_delta()), [])

    def test_trailing_cr_is_tolerated(self):
        self.assertEqual(llm._parse_sse_line(_delta(content="x") + "\r"),
                         [("content", "x")])

    def test_noise_lines_return_empty(self):
        for line in ["", "   ", ": keep-alive", "event: message",
                     "data: ", "data: [DONE]", "data: {坏 json", "data: 12345"]:
            self.assertEqual(llm._parse_sse_line(line), [], repr(line))

    def test_usage_only_frame_updates_cache_stats(self):
        saved = dict(llm.LAST_USAGE)
        self.addCleanup(lambda: llm.LAST_USAGE.update(saved))
        line = _frame({"choices": [], "usage": {
            "prompt_cache_hit_tokens": 900, "prompt_cache_miss_tokens": 100}})
        # usage 帧不带 delta，但缓存统计必须更新（否则流式下命中率会断掉）
        self.assertEqual(llm._parse_sse_line(line), [])
        self.assertEqual(llm.LAST_USAGE["total_tokens"], 1000)
        self.assertEqual(llm.LAST_USAGE["hit_tokens"], 900)
        self.assertAlmostEqual(llm.LAST_USAGE["hit_rate"], 0.9)

    def test_openai_style_cached_tokens_is_understood(self):
        saved = dict(llm.LAST_USAGE)
        self.addCleanup(lambda: llm.LAST_USAGE.update(saved))
        line = _frame({"choices": [], "usage": {
            "prompt_tokens": 400, "prompt_tokens_details": {"cached_tokens": 300}}})
        llm._parse_sse_line(line)
        self.assertEqual(llm.LAST_USAGE["total_tokens"], 400)
        self.assertEqual(llm.LAST_USAGE["hit_tokens"], 300)


class IterSseLinesTest(unittest.TestCase):
    class _FakeResp:
        def __init__(self, chunks):
            self._chunks = chunks

        def iter_content(self, chunk_size=None):
            for c in self._chunks:
                yield c

    def _lines(self, chunks):
        return list(llm._iter_sse_lines(self._FakeResp(chunks)))

    def test_multibyte_char_split_across_chunks(self):
        # "你好\n" 的 UTF-8 是 7 字节，故意在不完整的位置切开
        raw = "你好\n".encode("utf-8")
        self.assertEqual(self._lines([raw[:1], raw[1:4], raw[4:]]), ["你好"])
        self.assertEqual("".join(self._lines([raw[:2], raw[2:]])), "你好")

    def test_multiple_lines_in_one_chunk(self):
        self.assertEqual(self._lines([b"a\nb\n"]), ["a", "b"])

    def test_trailing_line_without_newline_is_flushed(self):
        self.assertEqual(self._lines([b"data: [DONE]"]), ["data: [DONE]"])

    def test_empty_chunks_are_skipped(self):
        self.assertEqual(self._lines([b"", b"a\n", b""]), ["a"])


class BuildStreamBodyTest(unittest.TestCase):
    @staticmethod
    def _eff(provider, model="m"):
        return {"provider": provider, "base_url": "https://x", "model": model, "api_key": "k"}

    def test_volc_gets_thinking_and_usage_reporting(self):
        body = llm._build_stream_body(self._eff("volc"), [], True)
        self.assertIs(body["stream"], True)
        self.assertEqual(body["stream_options"], {"include_usage": True})

    def test_volc_thinking_matrix_by_model(self):
        # deepseek 系：显式关（2026-10-03 起，推理 token 按 output ¥4/M 计费太贵）
        body = llm._build_stream_body(
            self._eff("volc", "deepseek-v4-flash-ga-260731"), [], True)
        self.assertEqual(body["thinking"], {"type": "disabled"})
        # 豆包系：默认带思考，显式关掉换速度
        body = llm._build_stream_body(
            self._eff("volc", "doubao-seed-2-1-turbo-260628"), [], True)
        self.assertEqual(body["thinking"], {"type": "disabled"})
        # 其他（glm 等）：字段习惯没验证过，不带，走模型默认
        body = llm._build_stream_body(
            self._eff("volc", "glm-5-2-260617"), [], True)
        self.assertNotIn("thinking", body)
        self.assertEqual(body["stream_options"], {"include_usage": True})

    def test_deepseek_official_gets_thinking_disabled(self):
        """DeepSeek 官方**默认开**思考，必须显式关（2026-10-04 实测）。

        现场：同一份真实系统头，不关思考那轮 completion 22036 token（其中
        21k 是推理）、耗时 99 秒；显式 disabled 后思考 0 字、completion 1 token。
        这条线原先被白名单挡在外面，注释写着"显式关掉"其实从没下发过。
        """
        body = llm._build_stream_body(self._eff("deepseek"), [], True)
        self.assertEqual(body["thinking"], {"type": "disabled"})
        self.assertEqual(body["stream_options"], {"include_usage": True})

    def test_scnet_is_still_not_in_the_whitelist(self):
        """scnet 的模型名里也含 "deepseek"，但它的字段习惯没实测过，别顺手加。

        _build_stream_body 的 400 降级重试是兜底，不是"加了也无妨"的许可。
        """
        body = llm._build_stream_body(
            self._eff("scnet", "DeepSeek-V4.1-Flash-Event"), [], True)
        self.assertNotIn("thinking", body)
        self.assertNotIn("stream_options", body)

    def test_extras_can_be_stripped_for_400_retry(self):
        body = llm._build_stream_body(self._eff("volc"), [], False)
        self.assertNotIn("thinking", body)
        self.assertNotIn("stream_options", body)


class CacheLogReasoningTest(unittest.TestCase):
    """`[cache]` 行末尾的 think= 字数（2026-10-04 加）。

    起因：一轮带图的私聊调用 out=22036、耗时 99 秒，日志里看不出这 22k 是
    「正文长」还是「想太久」——真因是关思考的白名单漏了 DeepSeek 官方。
    有这一项之后，「思考到底关没关」可以直接从日志读出来。
    """

    _USAGE = {"prompt_tokens": 100, "completion_tokens": 22036,
              "prompt_cache_hit_tokens": 80, "prompt_cache_miss_tokens": 20}

    def test_stream_call_reports_reasoning_chars(self):
        llm._stream_meta().update(finish_reason=None, reasoning_chars=21000,
                                  content_chars=900)
        with self.assertLogs("llm", level="INFO") as cm:
            llm._record_usage(dict(self._USAGE), elapsed=99.0,
                              provider="deepseek", model="deepseek-flash",
                              kind="stream")
        self.assertIn("think=21000", "\n".join(cm.output))

    def test_sync_call_does_not_borrow_leftover_reasoning(self):
        """同步调用不涉及思维链，不能被同一线程上一轮流式的残留值污染。

        （这就是"只在 kind == stream 时读 _stream_meta"那条判断存在的理由。）
        """
        llm._stream_meta().update(finish_reason=None, reasoning_chars=21000,
                                  content_chars=900)
        with self.assertLogs("llm", level="INFO") as cm:
            llm._record_usage(dict(self._USAGE), elapsed=1.0,
                              provider="deepseek", model="deepseek-flash",
                              kind="sync")
        self.assertNotIn("think=", "\n".join(cm.output))


class UsageThreadIsolationTest(unittest.TestCase):
    """用量记录按线程隔离。

    QQ 适配层会让多个群在各自线程里并发跑 LLM。要是共用一份记录，A 群刚写
    进去的 3 万 token 会被 B 群拿去判断「我该不该压缩历史」——上下文本来很
    短的群被误摘要，真正该压的群又可能读到别的小数字而漏压。
    """

    def test_main_thread_usage_still_readable(self):
        """兼容路径：直接读 llm.LAST_USAGE 的老写法在主线程里必须照旧可用。"""
        llm._record_usage({"prompt_cache_hit_tokens": 900,
                           "prompt_cache_miss_tokens": 100})
        self.assertEqual(llm.LAST_USAGE["total_tokens"], 1000)
        self.assertAlmostEqual(llm.LAST_USAGE["hit_rate"], 0.9)

    def test_worker_thread_does_not_leak_into_main(self):
        llm._record_usage({"prompt_cache_hit_tokens": 10,
                           "prompt_cache_miss_tokens": 0})

        def worker():
            llm._record_usage({"prompt_cache_hit_tokens": 5000,
                               "prompt_cache_miss_tokens": 5000})

        t = threading.Thread(target=worker)
        t.start()
        t.join()

        # 子线程写的 10000 绝不能污染主线程那一份
        self.assertEqual(llm.current_usage()["total_tokens"], 10)

    def test_each_worker_sees_only_its_own(self):
        seen = []

        def worker(tag, hit, miss):
            llm._record_usage({"prompt_cache_hit_tokens": hit,
                               "prompt_cache_miss_tokens": miss})
            seen.append((tag, llm.current_usage()["total_tokens"]))

        threads = [threading.Thread(target=worker, args=("a", 0, 7000)),
                   threading.Thread(target=worker, args=("b", 0, 9000))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(sorted(seen), [("a", 7000), ("b", 9000)])

    def test_current_usage_returns_copy(self):
        llm._record_usage({"prompt_cache_hit_tokens": 100,
                           "prompt_cache_miss_tokens": 0})
        snap = llm.current_usage()
        snap["total_tokens"] = 999999
        self.assertEqual(llm.current_usage()["total_tokens"], 100)


class CallLlmStreamTest(unittest.TestCase):
    class _Resp:
        def __init__(self, status_code, lines=()):
            self.status_code = status_code
            self._lines = lines

        @property
        def request(self):
            return mock.Mock(url=_URL)

        def close(self):
            pass

        def iter_content(self, chunk_size=None):
            for line in self._lines:
                yield (line + "\n").encode("utf-8")

    def test_ollama_returns_single_content_chunk(self):
        with mock.patch.object(llm, "_call_ollama", lambda base, body, timeout: "本地回复"):
            out = list(llm.call_llm_stream([{"role": "user", "content": "hi"}],
                                           provider="ollama"))
        self.assertEqual(out, [("content", "本地回复")])

    def test_400_triggers_retry_without_extra_fields(self):
        bodies = []

        # 注意用 **kwargs：requests.post 的形参名就叫 json，直接声明会遮蔽 json 模块
        def fake_post(url, **kwargs):
            bodies.append(kwargs.get("json"))
            if len(bodies) == 1:
                return self._Resp(400)
            return self._Resp(200, ["data: " + json.dumps(
                {"choices": [{"delta": {"content": "ok"}}]}, ensure_ascii=False)])

        with mock.patch.object(llm._session, "post", fake_post):
            out = list(llm.call_llm_stream([{"role": "user", "content": "hi"}],
                                           provider="volc"))

        self.assertEqual(len(bodies), 2)
        self.assertIn("thinking", bodies[0])
        self.assertNotIn("thinking", bodies[1])
        self.assertEqual(out, [("content", "ok")])

    def test_reasoning_and_content_are_yielded_in_stream_order(self):
        lines = [
            "data: " + json.dumps({"choices": [{"delta": {"reasoning_content": "想"}}]},
                                  ensure_ascii=False),
            "data: " + json.dumps({"choices": [{"delta": {"content": "答"}}]},
                                  ensure_ascii=False),
            "data: [DONE]",
        ]
        with mock.patch.object(llm._session, "post",
                               lambda *a, **kw: self._Resp(200, lines)):
            out = list(llm.call_llm_stream([{"role": "user", "content": "hi"}],
                                           provider="deepseek"))
        self.assertEqual(out, [("reasoning", "想"), ("content", "答")])


if __name__ == "__main__":
    unittest.main()
