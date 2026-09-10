"""
LLM 调用封装
"""

import requests
from app.config import API_URL, API_KEY, MODEL


def call_llm(messages, timeout=120):
    """调用豆包 DeepSeek API，返回回复文本"""
    resp = requests.post(API_URL, json={
        "model": MODEL,
        "messages": messages,
        "stream": False,
    }, headers={
        "Authorization": "Bearer " + API_KEY,
        "Content-Type": "application/json",
    }, timeout=timeout)
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]
