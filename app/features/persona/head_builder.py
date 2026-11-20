"""扮演态系统头拼装：人设、示例、房间、契约、档位、环境和动态块。"""

import json
import os

from app.config import BASE_DIR
from app.features.persona import writing_examples, writing_style
from app.features.persona.contract import _CONTRACT, _CONTRACT_NO_INTIMATE, _GROUP_NOTE
from app.features.persona.ledger_text import (
    _PRONOUN_HE, _ledger_fix_pronouns, _ledger_ok, drop_ledger_section)
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
    """「之前的事」小记块：旧账本可能带第三人称，注入前先纠成「你」。

    2026-11-19：注入副本去掉「约定与未完成」一节。账本新格式已经不再生成
    这一节；这里保留删除动作，只为兼容旧存档里残留的「约定」。写作 AI 不再
    每轮回收旧细节（用户报的「裙摆划痕」贯穿全文就是这一节把一次性细节
    写成了待办）。
    """
    if not str(note or "").strip():
        return ""
    body = _ledger_fix_pronouns(note)
    if not _ledger_ok(body):
        # 旧版散文小记不是分节账本：它不是「之前的事」的账，而是一段
        # 可以直接被演员扩写的场景散文。宁可这一轮不带旧事，也不把
        # 「晚饭 → 红酒 → 洗澡 → 第二天」这类旧线残片塞进新存档。
        return ""
    body = drop_ledger_section(body, "约定").strip()
    if not body:
        return ""
    return ("【之前的事】\n" + body
            + "\n\n（这些都是已经发生过的旧事，本轮只作背景；"
              "不要把它们当成本轮必须回收的待办。）")


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
    # 卡级声口（2026-11-18 试点）：卡面只有标签时，模型只能演「一个性格」；
    # 这块写她怎么断句、紧张时怎么办、哪些话她不会说。只放日常轮——
    # 亲密轮不注入，避免把「声口」变成性暗示的放大器。
    voice = str(card.get("voiceAnchor") or "").strip()
    if voice and not intimate:
        parts.append("## 她的声口（不是性格标签，是她说话的方式）\n\n" + voice)
    vexample = str(card.get("voiceExample") or "").strip()
    if vexample and not intimate and not str(session_key or "").startswith("group_"):
        parts.append("## 她说话的短示范（只学语气，不抄内容）\n\n"
                     + writing_style.format_examples(vexample))
    # 2026-11-19：范文段停用。写法全部落在 writing_guide.py 的「告诉 AI
    # 怎么做」里，不再注入本机存档回合 / 改前改后（那是已驳回的坏写法）。
    # prompt_example() 仍保留空实现，调用方 import 不用改。
    # 契约和档位照拼——它们才是每轮真正生效的写法规则。
    parts.append((_CONTRACT if intimate else _CONTRACT_NO_INTIMATE).replace(
        "{NAME}", cname))
    sty = writing_style.head_style(mode)
    if sty:
        parts.append(sty)
    # 契约末尾已经强调了人称；这里把同一句再钉一次，保证生成前最后读到的
    # 仍是「你 / 她」的指代规则。
    parts.append("## 生成前最后一句\n\n"
                     "对面那个人是「你」，她的念头里也一样；指代写具体；"
                     "主语能省就省。")
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
