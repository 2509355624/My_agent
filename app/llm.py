"""
LLM 调用封装
"""

import requests
from app.config import API_URL, API_KEY, MODEL

# 跨调用状态（缓存优化用）：记录最近一次请求的 token 用量与命中率
LAST_USAGE = {"total_tokens": 0, "hit_tokens": 0, "miss_tokens": 0, "hit_rate": 0.0}
# 估算上下文上限（与 deepseek-flash 对齐，DeepSeek 官方文档 1M）
CONTEXT_LIMIT = 1_000_000


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

    # 更新跨调用状态（供 memory 压缩决策）
    rate = (hit / total) if total > 0 else 0.0
    LAST_USAGE["total_tokens"] = total
    LAST_USAGE["hit_tokens"] = hit or 0
    LAST_USAGE["miss_tokens"] = miss or 0
    LAST_USAGE["hit_rate"] = rate

    if total > 0:
        print(f"[cache] 命中 {hit} / {total} tokens = {rate*100:.1f}% "
              f"(未命中 {miss})")

    return data["choices"][0]["message"]["content"]