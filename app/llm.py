"""
LLM 调用封装
"""

import requests
from app.config import API_URL, API_KEY, MODEL


def call_llm(messages, timeout=120):
    """调用豆包 DeepSeek API，返回回复文本

    同时解析 usage 中的缓存命中统计并打印，
    用于验证 prefix cache 命中率（目标 90%+）。
    """
    resp = requests.post(API_URL, json={
        "model": MODEL,
        "messages": messages,
        "stream": False,
    }, headers={
        "Authorization": "Bearer " + API_KEY,
        "Content-Type": "application/json",
    }, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()

    usage = data.get("usage") or {}
    hit = usage.get("prompt_cache_hit_tokens")
    miss = usage.get("prompt_cache_miss_tokens")
    if hit is None:
        # 兼容 OpenAI 风格的返回结构
        details = usage.get("prompt_tokens_details") or {}
        hit = details.get("cached_tokens", 0)
        miss = usage.get("prompt_tokens", 0) - hit
    total = (hit or 0) + (miss or 0)
    if total > 0:
        rate = (hit or 0) / total * 100
        print(f"[cache] 命中 {hit} / {total} tokens = {rate:.1f}% "
              f"(未命中 {miss})")

    return data["choices"][0]["message"]["content"]
