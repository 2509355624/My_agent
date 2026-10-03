"""
LLM 调用封装（支持多 Provider 动态路由）
"""

import codecs
import json
import logging
import threading
import time
import requests
from app.cancel import is_cancelled
from app.config import (API_URL, API_KEY, MODEL, LLM_PROVIDER,
                        PROVIDERS, OLLAMA_BASE_URL, CONTEXT_BUDGET,
                        LLM_FALLBACK_CHAIN, LLM_REQUEST_TIMEOUT,
                        LLM_FALLBACK_TTL, LLM_RATE_LIMIT_TTL,
                        provider_vision)

# 诊断行一律走 logging，不走 print——原因见 app/logsetup.py 的模块说明：
# print 落 stdout，被重定向/管道接管后是块缓冲，日志会「看起来丢了」。
log = logging.getLogger("llm")

# 跨调用状态（缓存优化用）：记录最近一次请求的 token 用量与命中率。
#
# **按线程各存一份**。QQ 适配层会让多个群在各自线程里并发跑同一轮 LLM，若
# 共用一个字典，A 群刚写进去的 3 万 token 会被 B 群读去判断「我该不该压缩
# 历史」——上下文本来很短的 B 群会被误摘要，而 A 群真正该压的时候又可能读到
# B 群的小数字而漏压。会话之间只有这一点共享状态，隔离掉就没有串号问题了。
_USAGE_LOCAL = threading.local()

_EMPTY_USAGE = {"total_tokens": 0, "hit_tokens": 0, "miss_tokens": 0, "hit_rate": 0.0}

# 出网不走本机系统代理。本机常驻 Clash 类工具会把代理写进注册表，代理进程
# 一旦换端口或被杀，requests 的默认行为就会去连那个没人监听的端口——于是
# 所有 provider 一起 ProxyError，换哪家模型都救不回来（2026-09-26 实撞）。
# 换成直连后 volc / scnet / deepseek 官方全部可达，本地 ollama 更不该走代理。
# 与 qq_api / comfy_src / image_out / model_catalog 同款。
_session = requests.Session()
_session.trust_env = False


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


# 流式调用的元信息（finish_reason / reasoning 与 content 的字数），同样按线程
# 隔离。agent 循环在「吐空回复」（流正常结束但正文零字）时拿它打诊断日志：
# reasoning 字数大 = 思维链把额度花完了、正文没动笔；finish_reason 能区分
# stop / length 等收尾方式。纯诊断用，不参与任何控制流。
_STREAM_LOCAL = threading.local()


def _stream_meta():
    d = getattr(_STREAM_LOCAL, "meta", None)
    if d is None:
        d = {"finish_reason": None, "reasoning_chars": 0, "content_chars": 0}
        _STREAM_LOCAL.meta = d
    return d


def current_stream_meta():
    """当前线程最近一次流式调用的元信息快照（返回拷贝）。"""
    return dict(_stream_meta())


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


def call_llm(messages, timeout=None, provider=None, model=None, strict=False):
    """调用 LLM，返回回复文本（失败时按降级链依次往下试）。

    - provider: 'volc' / 'doubao' / 'deepseek' / 'scnet' / 'scnet2' / 'mimo' / 'ollama'
    - model: 覆盖该 provider 的默认模型
    - timeout: 单次尝试的超时；不传用 LLM_REQUEST_TIMEOUT
    - strict: True 只打指定的那一个，失败不再顺链换家（见 candidates）
    兼容旧调用 call_llm(messages)：用当前生效配置。
    """
    timeout = timeout or LLM_REQUEST_TIMEOUT
    targets = _chain_targets(provider, model, strict=strict)
    last_err = None
    for i, (pid, mname) in enumerate(targets):
        eff = get_effective_config(pid, mname)
        _log_effective(eff, stream=False, attempt=(i + 1, len(targets)))
        body = {"messages": messages, "stream": False, "model": eff["model"]}
        try:
            return _call_provider(eff, body, timeout)
        except Exception as e:
            last_err = e
            _mark_dead((pid, mname), _brief(e), ttl=_ttl_for(e))
    raise last_err


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


def _record_usage(usage, elapsed=None, provider="", model="", kind="llm"):
    """把一次响应的 usage 折算成命中率写入**当前线程**的用量记录（流式/非流式共用口径）。

    kind 只进调用流水（stream = 主对话，sync = 摘要/判断这类隐形调用）。

    - 火山/DeepSeek 口径：prompt_cache_hit_tokens / prompt_cache_miss_tokens
    - OpenAI 口径兜底：prompt_tokens_details.cached_tokens
    流式下多数服务端只在末帧带 usage，且需要在请求里声明
    stream_options.include_usage。
    同时归账到 app/usage 的每日统计（归属 = 当前线程的 usage.scope）。
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
    # 两个标记，用来解释「这一轮为什么这么贵」：
    #   尾巴≈N  —— 状态栏 + extra_context 挂在消息数组末尾，每轮必 miss，
    #              它就是命中率的天花板（命中率 ≈ 1 − 尾巴/prompt）。
    #   ⚠冷调用 —— 命中 <50%，多半是压缩重写了历史或被服务端驱逐，前缀整个作废。
    # 没有这两项时，日志里只能看到一个孤零零的百分比，看不出钱花在哪。
    mark = ""
    tag = "-"
    try:
        from app import usage as usage_stats
        # 会话归属 + 实际模型都打进这一行（2026-10-03）。原来只有百分比，
        # 出了冷调用根本查不出是「哪个会话 / 哪家模型」——日志是多会话并发
        # 交错的，光靠上下文猜不准。这两个字段是后面做归因分析的唯一抓手。
        tag = usage_stats.current_tag()
        _n = usage_stats.current_tail()
        if _n:
            mark += "  尾巴≈%d" % _n
    except Exception:                # 标记失败不能影响主链路
        pass
    if rate < 0.5:
        mark += "  ⚠冷调用"
    out = int(usage.get("completion_tokens") or 0)
    # out= 是 2026-10-03 补的：output 按 ¥4/M 计费，是全天账单里最贵的一项，
    # 而这一行原来只报命中率——只看日志根本看不出钱花在哪。
    # think= 是 2026-10-04 补的（按**字数**）：思维链同样混在 completion 里按
    # output 计费，out= 大得离谱时看不出是「正文长」还是「想太久」。那天的
    # 现场就是 out=22036 / 耗时 99 秒，根因是关思考的白名单漏了 DeepSeek 官方。
    # 字数从流式的 reasoning delta 现数（_stream_meta），跨 provider 都准；
    # 同步调用不涉及思维链，这一项为空。
    think = ""
    if kind == "stream":
        try:
            think = " think=%d" % _stream_meta()["reasoning_chars"]
        except Exception:
            think = ""
    log.info("[cache] %s %s/%s 命中 %d / %d tokens = %.1f%% (未命中 %d) out=%d%s%s%s",
             tag, provider or "-", model or "-",
             hit, total, rate * 100, miss, out, think, tail, mark)

    try:
        from app import usage as usage_stats
        usage_stats.record(hit or 0, miss or 0, output=out,
                           provider=provider, model=model,
                           elapsed=elapsed, kind=kind)
    except Exception:                # 统计挂了不能影响主链路
        pass


def _log_effective(eff, stream, attempt=None):
    """每次真实请求打一行用了谁——管理页切了模型之后，这里就是「实际生效」的
    唯一铁证（配置链路对不对，看这行比看后台展示准）。

    两个入口都要打：call_llm（摘要/接话判断）走 _call_provider，主对话走
    call_llm_stream，后者不经过 _call_provider——只打一处会让主对话全程无声。

    attempt=(第几次, 共几次) 时带上序号，降级切换在日志里一眼可见：
    `[llm] volc / deepseek-v4-flash stream [1/3]` 后面紧跟一行 `[2/3]`，
    就说明主模型失败、已经切到备胎了。识图（app/vision.py）也打同一前缀，
    所以 grep 一下 "[llm]" 就能捞到本进程的**全部**模型调用。

    时间戳交给 logging 的 asctime，别再自己拼一份。
    """
    tag = "stream" if stream else "sync"
    if attempt:
        tag += " [%d/%d]" % attempt
    log.info("[llm] %s / %s %s", eff["provider"], eff["model"], tag)


# ─── 候选链与失效记忆 ────────────────────────────────
# 某个候选失败后短期不再试它，省掉「每条消息都先白撞一次」的等待。跨线程
# 共享（同一个模型对所有会话都是坏的），所以不能像 _USAGE_LOCAL 那样按线程
# 分开存。到期自动恢复，额度充值/服务恢复后不用重启。
_DEAD = {}
_DEAD_LOCK = threading.Lock()


def parse_chain(text):
    """解析 "provider:model,provider2:model2" → [(provider, model), ...]。

    容忍空格、空项、只写 provider（用它的默认模型）；未知 provider 直接丢
    （写错了就当作没这一项，总比把请求发给一个不存在的地址好）。
    """
    out = []
    for part in (text or "").split(","):
        part = part.strip()
        if not part:
            continue
        pid, _, mname = part.partition(":")
        pid = pid.strip().lower()
        cfg = PROVIDERS.get(pid)
        if not cfg:
            continue
        out.append((pid, mname.strip() or cfg["model"]))
    return out


def _brief(err, limit=60):
    """错误信息压成一行，够写进日志和拉黑理由就行。"""
    text = " ".join(str(err).split())
    return text[:limit] + ("…" if len(text) > limit else "")


def _ttl_for(err):
    """这个失败该拉黑多久：429（频率/额度到顶）→ 24 小时，其余用默认 TTL。

    429 不是「这条请求失败」，是「这家这一阵都不给了」——免费额度按天重置，
    600 秒后重试纯属白撞。拉黑一整天，让降级链直接把请求交给还能用的模型。
    其余失败（网络抖动、模型退役）保持短 TTL，到期自愈，不用重启。
    """
    if "429" in str(err):
        return LLM_RATE_LIMIT_TTL
    return LLM_FALLBACK_TTL


def _mark_dead(key, reason, ttl=None):
    """把这个候选拉黑一段时间。失败是常态（额度用完、模型退役），只记日志。"""
    ttl = LLM_FALLBACK_TTL if ttl is None else ttl
    with _DEAD_LOCK:
        _DEAD[key] = time.time() + ttl
    log.info("[chain] %s / %s 拉黑 %.0fs（%s）", key[0], key[1], ttl, reason)
    # 失败也落一行流水（2026-10-03 补）：账单按「请求数」算，失败同样占一次
    # 调用。只打日志不进账本的话，「账单 N 次 vs 日志 M 次」这种缺口永远查不出来。
    try:
        from app import usage as usage_stats
        usage_stats.log_fail(key[0], key[1], reason)
    except Exception:                # 统计挂了不能影响主链路
        pass


def reset_chain_state():
    """清空失效记忆（测试用，也可在管理页做「立刻重试主模型」）。"""
    with _DEAD_LOCK:
        _DEAD.clear()


def candidates(provider=None, model=None, require_vision=False, strict=False):
    """本次请求依次尝试的 (provider, model) 列表。

    链头是调用方指定的那个（agent 配置 / 管理页选择），后面接降级链里其余
    项，重复的去掉——所以管理页手动切换依然优先，链只负责兜底。

    strict=True：**只要链头，不兜底**（网页端「我选谁就是谁」，失败即失败）。
    此时降级链与失效记忆都不参与——模型之前失败过也照样发（用户明确选的它），
    失败原样抛给调用方。调用方没指定链头时不生效，退回下面的老路：否则会返回
    空表，等于把请求变成必然失败。

    require_vision=True：只留**能直接读图**的候选（config.provider_vision）。
    带图的轮次必须这么调——图片是以多模态（base64）塞进 messages 的，纯文本
    模型收到 base64 不报错、而是**整条请求挂死**（见 config.PROVIDERS 上方
    注释，实测 ReadTimeout）。降级链里有 volc 系纯文本 provider，带图时降到
    它就是白等一个超时。过滤后若一个都不剩（调用方没指定有视觉的模型），
    退回原样——宁可照老路试，也不返回空表。

    已被拉黑的直接跳过（省掉「每条消息都先白撞一次」的等待）；如果全都被拉黑
    了就照原样全试一遍：「全都不可用」意味着情况变了（比如额度刚到账），
    直接报错不如重试一轮。
    """
    head = None
    pid = (provider or "").strip().lower()
    if pid in PROVIDERS:
        head = (pid, (model or "").strip() or PROVIDERS[pid]["model"])

    # 严格模式：只有链头，一条链尾都不接。_DEAD 也不查——用户明确选了这个模型，
    # 之前失败过是上一轮的事，这一轮照样发，失败直接把异常抛上去。
    if strict and head:
        return [head]

    seen, ordered = set(), []
    for item in ([head] if head else []) + parse_chain(LLM_FALLBACK_CHAIN):
        if item in seen:
            continue
        seen.add(item)
        ordered.append(item)
    if not ordered:
        return []

    if require_vision:
        vision_ok = [c for c in ordered if provider_vision(c[0], c[1])]
        if vision_ok:
            ordered = vision_ok

    now = time.time()
    alive = [c for c in ordered if _DEAD.get(c, 0) <= now]
    return alive or ordered


def _chain_targets(provider, model, require_vision=False, strict=False):
    """把候选列表兜到「至少有一个」——链关掉且调用方也没指定时，仍按老路走
    get_effective_config 的默认 provider。"""
    cands = candidates(provider, model, require_vision, strict)
    if cands:
        return cands
    eff = get_effective_config(provider, model)
    return [(eff["provider"], eff["model"])]


def _call_provider(eff, body, timeout):
    """按 provider 分派请求。返回回复文本（用了谁由调用方打日志）。"""
    if eff["provider"] == "ollama":
        return _call_ollama(eff["base_url"], body, timeout)

    # 摘要/判断这类同步调用同样不需要思维链，显式关掉换速度。火山系统一发
    # disabled（见 _thinking_type）；deepseek 系以前靠「不加字段」依赖模型默认，
    # 现在也显式声明，免得哪次默认值一变就悄悄变贵。
    if eff["provider"] in _EXTRA_FIELDS_PROVIDERS:
        t = _thinking_type(eff["provider"], eff["model"])
        if t == "disabled":
            body["thinking"] = {"type": "disabled"}

    url = eff["base_url"].rstrip("/") + "/chat/completions"
    headers = {
        "Authorization": "Bearer " + eff["api_key"],
        "Content-Type": "application/json",
    }
    resp = _session.post(url, json=body, headers=headers, timeout=timeout)
    if resp.status_code >= 400:
        _raise_with_detail(resp)
    data = resp.json()

    _record_usage(data.get("usage"), resp.elapsed.total_seconds(),
                  provider=eff["provider"], model=eff["model"], kind="sync")

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
    resp = _session.post(url, json=ollama_body, timeout=timeout)
    if resp.status_code >= 400:
        _raise_with_detail(resp)
    data = resp.json()
    return data["message"]["content"]


# ─── 流式（SSE）──────────────────────────────────────
# thinking / stream_options 按 provider 白名单下发，真撞上 400 时降级重试一次
# （见 _stream_once）。
#
# 2026-10-04 实测（api.deepseek.com / deepseek-flash，流式与非流式都试了）：
# 这两个字段 DeepSeek 官方**全收**，HTTP 200，usage 照常回。原先只放火山，
# 注释里担心「DeepSeek 官方塞未知字段会被判 400」——实测不成立。
# 而 deepseek-flash 是**默认开思考**的：同一份真实系统头，不关思考那轮
# completion 22036 token（其中 reasoning 占 21k）、耗时 99 秒；显式 disabled
# 后思考 0 字、completion 1 token。这条线从 10-03 晚切过来就一直没关过思考，
# 是当时"贵在 output"那笔账的回潮。
_EXTRA_FIELDS_PROVIDERS = ("volc", "doubao", "deepseek")


def _thinking_type(provider, model):
    """模型 thinking 字段的取值（None = 不带这个字段）。

    2026-10-03 起**一律显式关思维链**，理由是钱：推理 token 按 output 计费
    （火山 ¥4/M，DeepSeek 官方 ¥4/M 同档），实测占过全天账单的 72%；同一份
    真实系统头只切这个开关，completion 537 → 6 token、9.0s → 1.6s。
    本机只跑生图，不需要思维链。2026-10-04 实测 DeepSeek 官方默认**开**思考，
    同样收 disabled（见 _EXTRA_FIELDS_PROVIDERS 上方注释）。

    - deepseek 系：默认开或默认关都显式 disabled，不赌默认值——之前正是靠
      "不加字段"赌了一次，白烧了一天的思考 token。
    - 豆包系：默认就带思考，显式 disabled 压掉（2026-09-27 用户实测「太慢了」）。
    - 其他（glm 等）：字段习惯没验证过，不带，走模型默认。
    """
    name = (model or "").lower()
    if provider in ("deepseek", "doubao") or "deepseek" in name or "doubao" in name:
        return "disabled"
    return None


def _build_stream_body(eff, messages, extras=True):
    """构造流式请求体。extras=False 时只带最保守的字段（400 降级重试用）。"""
    body = {"messages": messages, "stream": True, "model": eff["model"]}
    if extras and eff["provider"] in _EXTRA_FIELDS_PROVIDERS:
        # 各家默认值相反（火山托管版默认关、豆包/DeepSeek 官方默认带），统一按
        # _thinking_type 的结论下发，不赌默认值。
        t = _thinking_type(eff["provider"], eff["model"])
        if t:
            body["thinking"] = {"type": t}
        # 末帧回传 usage，否则缓存命中统计在流式下会断掉（DeepSeek 官方实测
        # 不带这个字段也回 usage，带着更稳）。
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


def _parse_sse_line(line, provider="", model=""):
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
    _record_usage(chunk.get("usage"), provider=provider, model=model,
                  kind="stream")

    meta = _stream_meta()
    out = []
    for choice in chunk.get("choices") or []:
        if choice.get("finish_reason"):
            meta["finish_reason"] = choice["finish_reason"]
        delta = choice.get("delta") or {}
        # 思考内容在前，正文在后（同一帧里可能同时有，保持这个顺序）
        if delta.get("reasoning_content"):
            meta["reasoning_chars"] += len(delta["reasoning_content"])
            out.append(("reasoning", delta["reasoning_content"]))
        if delta.get("content"):
            meta["content_chars"] += len(delta["content"])
            out.append(("content", delta["content"]))
    return out


def _stream_once(eff, messages, timeout, cancel_event):
    """对固定的 (provider, model) 发一次流式请求，逐块产出 (kind, text)。"""
    if eff["provider"] == "ollama":
        yield "content", _call_ollama(
            eff["base_url"], {"model": eff["model"], "messages": messages}, timeout)
        return

    url = eff["base_url"].rstrip("/") + "/chat/completions"
    headers = {
        "Authorization": "Bearer " + eff["api_key"],
        "Content-Type": "application/json",
    }
    resp = _session.post(url, json=_build_stream_body(eff, messages, True),
                         headers=headers, timeout=timeout, stream=True)
    if resp.status_code == 400:
        # 有的模型不认 thinking / stream_options，去掉扩展字段重试一次
        resp.close()
        resp = _session.post(url, json=_build_stream_body(eff, messages, False),
                             headers=headers, timeout=timeout, stream=True)
    if resp.status_code >= 400:
        _raise_with_detail(resp)

    try:
        for line in _iter_sse_lines(resp):
            # 逐块检查取消信号。用 return 而非抛异常结束：finally 里的
            # resp.close() 会断开上游，未生成的 token 不再产生也不再计费。
            if is_cancelled(cancel_event):
                return
            for kind, text in _parse_sse_line(line, eff["provider"],
                                              eff["model"]):
                yield kind, text
    finally:
        resp.close()


def call_llm_stream(messages, timeout=None, provider=None, model=None,
                    cancel_event=None, require_vision=False, strict=False):
    """流式调用 LLM，逐块产出 (kind, text)；失败时按降级链依次往下试。

    kind 只有两种：
      - "reasoning"：思考内容，**仅供展示，绝不能写回 messages**——
        模型侧要求思考内容不参与后续上下文，写回去还会毒化前缀缓存。
      - "content"：正文增量。

    cancel_event: 可选，threading.Event。置位即停止读取并关闭上游连接。
      **这是用户点「停止」后唯一能立刻生效的位置**——被中断时模型往往正在
      长篇思考，早一步断开就少生成一批 token（也就少计费）。半截正文由
      agent 循环按"已收到多少算多少"落盘，这里不负责收尾。

    换模型的边界是「正文」：一旦已经产出过 content 再失败，就不再往下切，
    直接把异常抛给上层——换个模型重来会让用户看到两段接不上的话。思考内容
    （reasoning）不算开工，它只是展示，切了重想不影响正确性。用户点了停止
    也不再尝试下一个（没人等着看，没必要花这笔钱）。

    Ollama 走非流式，整体作为单个 content 块产出（行为与 call_llm 一致），
    该分支无法中断。
    timeout 在流式下是"两次数据块之间的最大间隔"，而非整次响应上限。

    require_vision=True：降级链只走能直接读图的候选。**带图的轮次必须置位**——
    messages 里塞的是 base64 多模态内容，纯文本 provider 收到不会报错而是挂死
    （见 `candidates`）。

    strict=True：只打调用方指定的那一个模型，失败不换家（见 `candidates`）。
    注意换模型的边界（"已吐过 content 就不换"）在严格模式下已经用不上——压根
    没有下一家可换，异常直接抛给调用方。
    """
    timeout = timeout or LLM_REQUEST_TIMEOUT
    # 元信息按「一次 call_llm_stream」清零：降级链换模型重试后，读到的是
    # 最后一次（也就是最终成功那次）的数字，正是诊断想要的口径。
    _stream_meta().update(finish_reason=None, reasoning_chars=0, content_chars=0)
    targets = _chain_targets(provider, model, require_vision, strict)
    last_err = None
    for i, (pid, mname) in enumerate(targets):
        if is_cancelled(cancel_event):
            return
        eff = get_effective_config(pid, mname)
        _log_effective(eff, stream=True, attempt=(i + 1, len(targets)))
        spoke = False
        try:
            for kind, text in _stream_once(eff, messages, timeout, cancel_event):
                if kind == "content":
                    spoke = True
                yield kind, text
            return
        except Exception as e:
            if spoke:
                raise
            last_err = e
            _mark_dead((pid, mname), _brief(e), ttl=_ttl_for(e))
    raise last_err
