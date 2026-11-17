"""扮演链路的 LLM 出口：固定链头、注入回复预算，再交给统一网关。"""

from app.config import PERSONA_PROVIDER
from app.integrations import llm

# 回复 token 上限：防止服务端默认值把话砍在半句上。32768 是上限不是配额。
REPLY_MAX_TOKENS = 32768


def call(messages, pick, **kw):
    """用角色扮演专用模型调用；`pick` 是本轮选中的 provider/model。"""
    kw.setdefault("max_tokens", REPLY_MAX_TOKENS)
    if pick.get("provider"):
        kw.setdefault("provider", pick["provider"])
    elif PERSONA_PROVIDER:
        kw.setdefault("provider", PERSONA_PROVIDER)
    if pick.get("id"):
        kw.setdefault("model", pick["id"])
    return llm.call_llm(messages, **kw)
