"""
LLM 调用封装（支持多 Provider 动态路由）
"""

import json
import requests
from app.config import (API_URL, API_KEY, MODEL, LLM_PROVIDER,
                        PROVIDERS, OLLAMA_BASE_URL)

# 跨调用状态（缓存优化用）：记录最近一次请求的 token 用量与命中率
LAST_USAGE = {"total_tokens": 0, "hit_tokens": 0, "miss_tokens": 0, "hit_rate": 0.0}
# 估算上下文上限（与 deepseek-flash 对齐，DeepSeek 官方文档 1M）
CONTEXT_LIMIT = 1_000_000

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


def call_llm(messages, timeout=120, provider=None, model=None):
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


def _call_provider(eff, body, timeout):
    """按 provider 分派请求。返回回复文本。"""
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

    usage = data.get("usage") or {}
    hit = usage.get("prompt_cache_hit_tokens")
    miss = usage.get("prompt_cache_miss_tokens")
    if hit is None:
        details = usage.get("prompt_tokens_details") or {}
        hit = details.get("cached_tokens", 0)
        miss = usage.get("prompt_tokens", 0) - hit
    total = (hit or 0) + (miss or 0)

    rate = (hit / total) if total > 0 else 0.0
    LAST_USAGE["total_tokens"] = total
    LAST_USAGE["hit_tokens"] = hit or 0
    LAST_USAGE["miss_tokens"] = miss or 0
    LAST_USAGE["hit_rate"] = rate

    if total > 0:
        elapsed = resp.elapsed.total_seconds()
        print(f"[cache] 命中 {hit} / {total} tokens = {rate*100:.1f}% "
              f"(未命中 {miss})  {elapsed:.1f}s @{_now()}")

    return data["choices"][0]["message"]["content"]


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


def _now():
    import time
    return time.strftime("%H:%M:%S")