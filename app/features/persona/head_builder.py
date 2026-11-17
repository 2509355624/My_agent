"""扮演态系统头拼装：人设、示例、房间、契约、档位、环境和动态块。"""

import json
import os

from app.config import BASE_DIR
from app.features.persona import writing_style
from app.features.persona.contract import _CONTRACT, _CONTRACT_NO_INTIMATE, _GROUP_NOTE
from app.features.persona.ledger_text import _PRONOUN_HE, _ledger_fix_pronouns
from app.features.persona.prompts import _GUARD_TEXT


def _guard_block():
    """要不要在系统头最前面拼这段框定。读 `config/persona.json`，缺省开。"""
    try:
        with open(os.path.join(BASE_DIR, "config", "persona.json"),
                  encoding="utf-8") as fh:
            cfg = json.load(fh)
        on = bool((cfg.get("guard") or {}).get("enabled", True))
    except Exception:                                  # noqa: BLE001
        on = True
    return _GUARD_TEXT if on else ""


def _note_block(note):
    """「之前的事」小记块：旧账本可能带第三人称，注入前先纠成「你」。"""
    if not str(note or "").strip():
        return ""
    return "【之前的事】\n" + _ledger_fix_pronouns(note).strip()


def _state_block(state):
    """「她此刻的样子」：上一轮结束时的状态，接着它往下演。"""
    state = str(state or "").strip()
    if not state:
        return ""
    return ("## 她此刻的样子\n\n" + _PRONOUN_HE.sub("你", state)
            + "\n\n（这是上一轮结束时的状态，接着它往下演，"
              "不要自己给她换回原来的衣服）")


def _env_note(session_key, sender):
    """告诉它此刻在私聊还是群里、是谁在跟它说话。"""
    target, _, tid = (session_key or "").partition("_")
    if target == "group":
        return ("## 当前环境\n\n你在一个 QQ 群里（群号 %s），群里有很多人。"
                "刚才跟你说话的是%s，回他一个就行，别管其他人。"
                % (tid, sender or "群里某个人"))
    return "## 当前环境\n\n你在跟一个人私聊（QQ %s）。" % tid


def build(card, session_key, sender, cname, note="", state="", intimate=True,
          mode="normal", dynamic=True):
    """按固定顺序拼系统头；`dynamic=False` 时小记和状态由调用方放末尾。"""
    parts = []
    g = _guard_block()
    if g:
        parts.append(g)
    persona = str(card.get("systemPrompt") or "").strip()
    parts.append(persona if persona else "你是%s。" % card["name"])
    sketch = writing_style.card_sketch(card)
    if sketch:
        parts.append(sketch)
    ex = str(card.get("exampleDialogue") or "").strip()
    if ex:
        parts.append("## 她说话的示范（**只学语气和节奏，不要照抄内容**）\n\n"
                     + writing_style.format_examples(ex))
    room = str(card.get("room") or "").strip()
    if room and not str(session_key or "").startswith("group_"):
        parts.append("## 她的房间（你离开她视线、进她房间时的素材，"
                     "可以自由发挥，不必全用）\n\n" + room)
    parts.append((_CONTRACT if intimate else _CONTRACT_NO_INTIMATE).replace(
        "{NAME}", cname))
    sty = writing_style.head_style(mode)
    if sty:
        parts.append(sty)
    if str(session_key or "").startswith("group_"):
        parts.append(_GROUP_NOTE)
    parts.append(_env_note(session_key, sender))
    if dynamic:
        nb = _note_block(note)
        if nb:
            parts.append(nb)
        sb = _state_block(state)
        if sb:
            parts.append(sb)
    return "\n\n".join(parts)
