"""
LLM 调用封装（支持多 Provider 动态路由）
"""

import codecs
import json
import threading
import requests
from app.cancel import is_cancelled
from app.config import (API_URL, API_KEY, MODEL, LLM_PROVIDER,
                        PROVIDERS, OLLAMA_BASE_URL, CONTEXT_BUDGET)

# 跨调用状态（缓存优化用）：记录最近一次请求的 token 用量与命中率。
#
# **按线程各存一份**。QQ 适配层会让多个群在各自线程里并发跑同一轮 LLM，若
# 共用一个字典，A 群刚写进去的 3 万 token 会被 B 群读去判断「我该不该压缩
# 历史」——上下文本来很短的 B 群会被误摘要，而 A 群真正该压的时候又可能读到
# B 群的小数字而漏压。会话之间只有这一点共享状态，隔离掉就没有串号问题了。
_USAGE_LOCAL = threading.local()

_EMPTY_USAGE = {"total_tokens": 0, "hit_tokens": 0, "miss_tokens": 0, "hit_rate": 0.0}


def _usage():
    """当前线程的用量记录（首次访问时惰性建一份）。"""
    d = getattr(_USAGE_LOCAL, "d", None)
    if d is None:
        d = dict(_EMPTY_USAGE)
        _USAGE_LOCAL.d = d
    return d


def current_usage():
    """当前线程最近一次 LLM 调用的用量快照（返回拷贝，改不到内部状态）。

    app/memory.trim_history 用它决定该不该压缩历史；agent 循环每轮取一次、
    传给下一轮，这样压缩判断用的始终是「这条会话线自己」的真实用量。
    """
    return dict(_usage())


# 主线程视图，只为兼容既有读取方式（含测试）而保留。多线程场景请改用
# current_usage()，它按线程取，才是准的。
LAST_USAGE = _usage()

# 当前生效的 provider（web 端切换后更新）。默认取 .env 的 LLM_PROVIDER
CURRENT_PROVIDER = LLM_PROVIDER


def get_effective_config(provider=None, model=None):
    """解析出一次请求要用的 (base_url, model, api_key, provider)"""
    provider = (provider or CURRENT_PROVIDER or LLM_PROVIDER).lower()
    if provider not in PROVIDERS:
        provider = LLM_PROVIDER
    cfg = PROVIDERS[provider]
    return {
        "provider": provider,
        "base_url": cfg["base_url"],
        "model": model or cfg["model"],
        "api_key": cfg["api_key"],
    }


def call_llm(messages, timeout=600, provider=None, model=None):
    """调用 LLM，返回回复文本。

    - provider: 'volc' / 'doubao' / 'deepseek' / 'ollama'；默认当前生效 provider
    - model: 覆盖该 provider 的默认模型
    兼容旧调用 call_llm(messages)：用当前生效配置。
    """
    eff = get_effective_config(provider, model)
    resp_body = {
        "messages": messages,
        "stream": False,
        "model": eff["model"],
    }
    return _call_provider(eff, resp_body, timeout)


def _extract_error(resp):
    """从失败响应里取出服务端返回的错误正文（优先 error.message）"""
    try:
        d = resp.json()
    except Exception:
        d = {}
    msg = d.get("error") or d.get("message") or d.get("detail") or ""
    if isinstance(msg, dict):
        msg = msg.get("message") or msg.get("code") or str(msg)
    if not msg:
        return ""
    return str(msg)


def _raise_with_detail(resp):
    detail = _extract_error(resp)
    note = ""
    if resp.status_code == 404:
        note = "（404：一般表示 model 的接入点ID/模型ID 未被该 API Key 开通或不存在。火山引擎请填控制台里的 ep-xxxx 接入点ID，或改为已开通的模型ID）"
    msg = f"LLM 请求失败 HTTP {resp.status_code} {resp.reason} @{resp.request.url}"
    if detail:
        msg += f" | {detail}"
    if note:
        msg += note
    raise RuntimeError(msg)


def _record_usage(usage, elapsed=None):
    """把一次响应的 usage 折算成命中率写入**当前线程**的用量记录（流式/非流式共用口径）。

    - 火山/DeepSeek 口径：prompt_cache_hit_tokens / prompt_cache_miss_tokens
    - OpenAI 口径兜底：prompt_tokens_details.cached_tokens
    流式下多数服务端只在末帧带 usage，且需要在请求里声明
    stream_options.include_usage。
    """
    if not usage:
        return
    hit = usage.get("prompt_cache_hit_tokens")
    miss = usage.get("prompt_cache_miss_tokens")
    if hit is None:
        details = usage.get("prompt_tokens_details") or {}
        hit = details.get("cached_tokens", 0)
        miss = (usage.get("prompt_tokens") or 0) - (hit or 0)
    total = (hit or 0) + (miss or 0)
    if total <= 0:
        return
    rate = (hit or 0) / total
    u = _usage()                     # 写当前线程那一份，不与其他会话互串
    u["total_tokens"] = total
    u["hit_tokens"] = hit or 0
    u["miss_tokens"] = miss or 0
    u["hit_rate"] = rate

    tail = f"  {elapsed:.1f}s" if elapsed is not None else ""
    print(f"[cache] 命中 {hit} / {total} tokens = {rate*100:.1f}% "
          f"(未命中 {miss}){tail} @{_now()}")


def _log_effective(eff, stream):
    """每次真实请求打一行用了谁——管理页切了模型之后，这里就是「实际生效」的
    唯一铁证（配置链路对不对，看这行比看后台展示准）。

    两个入口都要打：call_llm（摘要/接话判断）走 _call_provider，主对话走
    call_llm_stream，后者不经过 _call_provider——只打一处会让主对话全程无声。
    """
    print(f"[llm] {eff['provider']} / {eff['model']} "
          f"{'stream' if stream else 'sync'} @{_now()}")


def _call_provider(eff, body, timeout):
    """按 provider 分派请求。返回回复文本。"""
    _log_effective(eff, stream=False)
    if eff["provider"] == "ollama":
        return _call_ollama(eff["base_url"], body, timeout)

    url = eff["base_url"].rstrip("/") + "/chat/completions"
    headers = {
        "Authorization": "Bearer " + eff["api_key"],
        "Content-Type": "application/json",
    }
    resp = requests.post(url, json=body, headers=headers, timeout=timeout)
    if resp.status_code >= 400:
        _raise_with_detail(resp)
    data = resp.json()

    _record_usage(data.get("usage"), resp.elapsed.total_seconds())

    message = (data.get("choices") or [{}])[0].get("message") or {}
    return message.get("content") or ""


def _call_ollama(base_url, body, timeout):
    """Ollama 原生 /api/chat 接口（OpenAI 兼容的 send 字段）"""
    url = base_url.rstrip("/") + "/api/chat"
    ollama_body = {
        "model": body.get("model"),
        "messages": body.get("messages", []),
        "stream": False,
    }
    resp = requests.post(url, json=ollama_body, timeout=timeout)
    if resp.status_code >= 400:
        _raise_with_detail(resp)
    data = resp.json()
    return data["message"]["content"]


# ─── 流式（SSE）──────────────────────────────────────
# thinking / stream_options 这两个扩展字段只在火山方舟侧确认支持；DeepSeek
# 官方的思考能力由模型自身决定，塞未知字段可能被判 400。所以按 provider
# 白名单下发，并在真撞上 400 时降级重试一次。
_EXTRA_FIELDS_PROVIDERS = ("volc", "doubao")


def _build_stream_body(eff, messages, extras=True):
    """构造流式请求体。extras=False 时只带最保守的字段（400 降级重试用）。"""
    body = {"messages": messages, "stream": True, "model": eff["model"]}
    if extras and eff["provider"] in _EXTRA_FIELDS_PROVIDERS:
        # 火山多个 DeepSeek 版本默认关闭思维链，必须显式开启
        body["thinking"] = {"type": "enabled"}
        # 末帧回传 usage，否则缓存命中统计在流式下会断掉
        body["stream_options"] = {"include_usage": True}
    return body


def _iter_sse_lines(resp):
    """把 SSE 响应切成行。

    用增量解码器按 UTF-8 解码：SSE 响应头常不带 charset，交给 requests 猜
    编码容易把中文解成乱码；而 HTTP 分块又可能把一个多字节汉字劈成两半，
    所以必须用 incremental decoder 而不是逐块 decode。
    """
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    buf = ""
    for chunk in resp.iter_content(chunk_size=None):
        if not chunk:
            continue
        buf += decoder.decode(chunk)
        while "\n" in buf:
            line, buf = buf.split("\n", 1)
            yield line
    buf += decoder.decode(b"", final=True)
    if buf:
        yield buf


def _parse_sse_line(line):
    """解析一行 SSE，返回 [(kind, text)]，kind ∈ {"reasoning", "content"}。

    非 data 行、坏 JSON、空 delta、[DONE] 一律返回空列表——流里出现噪声
    不应该中断整次回答。
    """
    line = (line or "").strip()
    if not line or not line.startswith("data:"):
        return []
    payload = line[5:].strip()
    if not payload or payload == "[DONE]":
        return []
    try:
        chunk = json.loads(payload)
    except json.JSONDecodeError:
        return []
    if not isinstance(chunk, dict):
        return []
    _record_usage(chunk.get("usage"))

    out = []
    for choice in chunk.get("choices") or []:
        delta = choice.get("delta") or {}
        # 思考内容在前，正文在后（同一帧里可能同时有，保持这个顺序）
        if delta.get("reasoning_content"):
            out.append(("reasoning", delta["reasoning_content"]))
        if delta.get("content"):
            out.append(("content", delta["content"]))
    return out


def call_llm_stream(messages, timeout=600, provider=None, model=None,
                    cancel_event=None):
    """流式调用 LLM，逐块产出 (kind, text)。

    kind 只有两种：
      - "reasoning"：思考内容，**仅供展示，绝不能写回 messages**——
        模型侧要求思考内容不参与后续上下文，写回去还会毒化前缀缓存。
      - "content"：正文增量。

    cancel_event: 可选，threading.Event。置位即停止读取并关闭上游连接。
      **这是用户点「停止」后唯一能立刻生效的位置**——被中断时模型往往正在
      长篇思考，早一步断开就少生成一批 token（也就少计费）。半截正文由
      agent 循环按"已收到多少算多少"落盘，这里不负责收尾。

    Ollama 走非流式，整体作为单个 content 块产出（行为与 call_llm 一致），
    该分支无法中断。
    timeout 在流式下是"两次数据块之间的最大间隔"，而非整次响应上限。
    """
    eff = get_effective_config(provider, model)
    _log_effective(eff, stream=True)
    if eff["provider"] == "ollama":
        yield "content", _call_ollama(
            eff["base_url"], {"model": eff["model"], "messages": messages}, timeout)
        return

    url = eff["base_url"].rstrip("/") + "/chat/completions"
    headers = {
        "Authorization": "Bearer " + eff["api_key"],
        "Content-Type": "application/json",
    }
    resp = requests.post(url, json=_build_stream_body(eff, messages, True),
                         headers=headers, timeout=timeout, stream=True)
    if resp.status_code == 400:
        # 有的模型不认 thinking / stream_options，去掉扩展字段重试一次
        resp.close()
        resp = requests.post(url, json=_build_stream_body(eff, messages, False),
                             headers=headers, timeout=timeout, stream=True)
    if resp.status_code >= 400:
        _raise_with_detail(resp)

    try:
        for line in _iter_sse_lines(resp):
            # 逐块检查取消信号。用 return 而非抛异常结束：finally 里的
            # resp.close() 会断开上游，未生成的 token 不再产生也不再计费。
            if is_cancelled(cancel_event):
                return
            for kind, text in _parse_sse_line(line):
                yield kind, text
    finally:
        resp.close()


def _now():
    import time
    return time.strftime("%H:%M:%S")