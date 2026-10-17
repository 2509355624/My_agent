"""最小探针：直接问火山方舟这几个模型还能不能用。

只发 1 个 token 的请求，判断 200 / 429 / 404。不经过 agent 链路。
用来确认「把 v4-pro / glm-5.2 加回降级链」到底有没有用。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
    os.environ.pop(k, None)

import requests
from app import config

MODELS = [
    "deepseek-v4-flash-ga-260731",
    "deepseek-v4-pro-ga-260813",
    "glm-5-2-260617",
    "doubao-seed-2-1-turbo-260628",
]

url = config.VOLC_BASE_URL.rstrip("/") + "/chat/completions"
headers = {"Authorization": "Bearer " + config.VOLC_API_KEY,
           "Content-Type": "application/json"}

for m in MODELS:
    body = {"model": m, "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 1}
    try:
        r = requests.post(url, json=body, headers=headers, timeout=30)
        note = ""
        if r.status_code >= 400:
            try:
                note = str(r.json().get("error", {}))[:150]
            except Exception:
                note = r.text[:150]
        print("%-34s -> %s  %s" % (m, r.status_code, note))
    except Exception as e:
        print("%-34s -> EXC %s" % (m, type(e).__name__ + ": " + str(e)[:100]))
