"""送模型前的历史副本整理：分段渲染、人称替换、心声标签清理。"""

from app.features.persona.ledger_text import _PRONOUN_HE
from app.features.persona.text_utils import _strip_monologue_tag
from app.features.persona.user_block import render_user_block


def pronoun_to_name(content, name):
    """历史示范里的「她」换成卡名；只改送模型的副本，落盘不动。"""
    n = str(name or "").strip()
    if not n or n == "她":
        return str(content)
    text = str(content)
    text = text.replace("她们", n + "们")
    return text.replace("她", n)


def render_hist(hist, name="", alias=""):
    """历史里带 `seg` 的用户消息按分段渲染；老消息没有 `seg` 就用原文。"""
    out = []
    for m in hist or []:
        if not isinstance(m, dict):
            continue
        content = str(m.get("content") or "")
        if m.get("role") == "assistant":
            content = _strip_monologue_tag(content)
            content = _PRONOUN_HE.sub("你", content)
            if alias and name and alias != name:
                content = content.replace(alias, name)
            content = pronoun_to_name(content, name)
        if m.get("role") == "user" and m.get("seg"):
            content = render_user_block(m["seg"], m.get("addressed", True),
                                        fallback=content)
        out.append({"role": m.get("role"), "content": content})
    return out
