# -*- coding: utf-8 -*-
"""只读探针：把某条扮演线"最近一轮实际送模型的完整 messages"逐段拆开、标 token。

用途：查清每轮未命中（miss）到底由哪段吃掉。不调模型、不改存档、不重启服务。
复制盘上的真实状态与归档，复现 turn.py 的拼装顺序；判断块（编剧意图/事实）
用 mock 提供中性值，因为它们本轮内容用户测试时才有。

用法：
    D:/AI/confyui_env/Scripts/python.exe tools/probe_message_tokens.py
    D:/AI/confyui_env/Scripts/python.exe tools/probe_message_tokens.py web_2509355624__izumi_sagiri

输出：打印 + 追加写入 docs/message_token_probe.log
"""

import json
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tiktoken

from app.config import BASE_DIR
from app.features.persona import service as svc
from app.features.persona import turn as turn_mod
from app.features.persona import writing_style
from app.core import agents

ENC = tiktoken.get_encoding("cl100k_base")
LOG = os.path.join(BASE_DIR, "docs", "message_token_probe.log")


def tok(s):
    return len(ENC.encode(str(s or "")))


def dump(key):
    sessions = os.path.join(BASE_DIR, "state", "persona_sessions.json")
    all_sessions = json.load(open(sessions, encoding="utf-8"))
    ent = all_sessions.get(key)
    if not ent:
        print("找不到线:", key)
        return

    card = svc._norm_card(svc.find_card(str(ent.get("card") or "")) or {})
    if not card.get("name"):
        card["name"] = str(ent.get("char_name") or "角色")
        card["systemPrompt"] = card.get("systemPrompt") or ("你是%s。" % card["name"])

    note = str(ent.get("note") or "")
    prev_state = str(ent.get("state") or "")
    prev_outfit = str(ent.get("outfit") or "")
    gd = ent.get("gameday") or {}
    aff = dict(ent.get("affection") or {})
    raid = bool(ent.get("affection"))
    v2 = aff.get("schema") == 2

    # 归档与历史：真实文件。线键本来就是 web_xxx，文件名直接接在前缀后。
    key_raw = key
    archive_path = os.path.join(BASE_DIR, "agents", "qq", "sessions",
                                "persona_archive_" + key_raw + ".jsonl")
    hist_path = os.path.join(BASE_DIR, "agents", "qq", "sessions",
                             "persona_" + key_raw + ".jsonl")

    def load_rows(p):
        if not os.path.exists(p):
            return []
        return [json.loads(l) for l in open(p, encoding="utf-8") if l.strip()]

    archive = load_rows(archive_path)
    raw_hist = load_rows(hist_path)
    if not raw_hist:
        raw_hist = archive
    model_hist = svc._render_hist(
        svc.trim(raw_hist), name=svc.char_name(card, key=key),
        alias=str(card.get("name") or ""))

    # ── 系统头（真实拼装，dynamic=False）────────────────────────
    # 亲密/日常按当前线判断：v2 的 together/数值不直接决定，这里取日常轮
    # （写日常时命中最高、最常见）。要测亲密轮把 intimate 改 True。
    intimate = False
    head = svc.build_head(card, key, "2509355624", note, prev_state,
                          intimate=intimate, mode="normal", dynamic=False)
    head = head + "\n\n" + "\n\n".join(svc._stable_note_blocks())

    # ── 末尾玩家消息（逐段，尽量复现真实顺序）────────────────────
    text = ""  # 用户从第10轮测试时才有本轮输入；探针只标结构
    segs = []
    user_block = svc.render_user_block([], True, fallback="（本轮玩家输入）",
                                       remind=False)
    segs.append(("① 本轮原话", user_block))

    # 2026-11-19「半删小记」：【之前的事】整块不再注入（和最近12条重复），
    # 探针跟随主链路去掉这一段。story_line / judge ledger 仍从账本抽，照旧。
    sb = svc._state_block(prev_state)
    if sb:
        segs.append(("③ 状态", "\n\n" + sb))

    # 数值块：v2 走 _v2_injection，v1 走 state_inject
    if raid and v2:
        a = dict(aff); a["active"] = True
        v2b = svc.affection._v2_injection(a)
        if v2b:
            segs.append(("④ 数值块(v2)", "\n\n" + v2b))
    elif raid:
        try:
            from app.features.persona import state_inject
            a = dict(aff); a["active"] = True
            v1b = state_inject.build(a, gd)
            if v1b:
                segs.append(("④ 数值块(v1)", "\n\n" + v1b))
        except Exception:
            pass

    ob = svc._outfit_block(prev_outfit)
    if ob:
        segs.append(("⑤ 穿着", "\n\n" + ob))
    pb = svc._place_block(gd)
    if pb:
        segs.append(("⑥ 地点", "\n\n" + pb))
    story = svc.story_line(gd, note)
    if story:
        segs.append(("⑦ 故事到现在", "\n\n" + story))
    tail = writing_style.tail(name=svc.char_name(card, key=key), mode="normal")
    segs.append(("⑧ 四行尾注+锚点", "\n\n" + tail))

    # ── 输出 ─────────────────────────────────────────────────────
    lines = []
    lines.append("=" * 72)
    lines.append("探针时间线 / 线: %s  归档%d条 送模型历史%d条 亲密=%s" % (
        key, len(archive), len(model_hist), intimate))
    lines.append("=" * 72)
    lines.append("【消息1】system 系统头 ........................ %6d token (%d字)" % (
        tok(head), len(head)))
    hist_tok = sum(tok(m.get("content")) for m in model_hist)
    lines.append("【消息2..n】历史 %d 条合计 ...................... %6d token" % (
        len(model_hist), hist_tok))
    for i, m in enumerate(model_hist):
        lines.append("    history[%02d] %-9s %5d token | %s" % (
            i, m.get("role"), tok(m.get("content")),
            " ".join(str(m.get("content") or "").split())[:40]))
    # 末条 = 开头 + 各动态段（累加后才等于真实末条 token）
    ub_full = user_block + "".join(p for _n, p in segs[1:])
    ub_tok = tok(ub_full)
    lines.append("【末条】user 全文 ............................... %6d token" % ub_tok)
    for name, part in segs:
        lines.append("    %-18s %5d token (%d字)" % (name, tok(part), len(part)))
    lines.append("-" * 72)
    total = tok(head) + hist_tok + ub_tok
    lines.append("合计送模型 ≈ %d token（系统头 %d + 历史 %d + 末条 %d）" % (
        total, tok(head), hist_tok, ub_tok))
    lines.append("对照截图：命中≈系统头 %d；未命中≈历史 %d + 末条 %d = %d" % (
        tok(head), hist_tok, ub_tok, hist_tok + ub_tok))
    lines.append("")

    out = "\n".join(lines)
    print(out)
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write(out)


def dump_judge(key):
    """拆编剧调用（第一段）的 prompt：单条 user，逐字段标 token。

    用户疑问「编剧只看状态变化，最近几轮就够了吧」——这里量出 history /
    ledger / recent 各占多少，判断能不能砍。
    """
    from app.features.persona import affection, judge
    from app.features.persona.prompts.scoring import _SCORE_PROMPT_V2

    sessions = os.path.join(BASE_DIR, "state", "persona_sessions.json")
    ent = json.load(open(sessions, encoding="utf-8")).get(key)
    if not ent:
        return
    ctx = affection.scoring_ctx(key) or {}
    gd = ent.get("gameday") or {}
    note = str(ent.get("note") or "")
    state = str(ent.get("state") or "")
    card = {"name": str(ent.get("char_name") or "角色"), "systemPrompt": ""}

    # 归档当历史，和 turn.py 一致（hist = 最近12条）
    key_raw = key
    archive_path = os.path.join(BASE_DIR, "agents", "qq", "sessions",
                                "persona_archive_" + key_raw + ".jsonl")
    rows = []
    if os.path.exists(archive_path):
        rows = [json.loads(l) for l in open(archive_path, encoding="utf-8") if l.strip()]
    hist = svc.trim(rows)
    recent = svc._judge_recent(hist)

    fields = {
        "facts_guide": judge._FACTS_GUIDE,
        "profile": svc._score_profile(card, "", key=key),
        "item_keys": judge._item_keys_text(),
        "state_line": ("她上一轮身上的状态：" + state + "\n") if state.strip() else "",
        "relation_name": ctx.get("relation_name") or "正常关系",
        "relation_desc": ctx.get("relation_desc") or "",
        "together": "还没在一起；没表白之前，关系档不会自己往前跳。",
        "contact_name": ctx.get("contact_name") or "还没碰过",
        "confess_name": ctx.get("confess_name") or "还没想过",
        "confess_desc": ctx.get("confess_desc") or "",
        "yandere_name": ctx.get("yandere_name") or "没有",
        "dev": ctx.get("devText") or "（五处都还没被开发过）",
        "gday": judge._day_of(gd),
        "gphase": str(gd.get("phase") or "还没定"),
        "gplan": judge._plan_fact(gd, str(gd.get("plan") or "")),
        "recent": ctx.get("recent") or "（你们还没什么交集）",
        "history": recent,
        "ledger": judge._judge_ledger(str(note or "")[:300]),
        "said": "（本轮玩家输入，测试时才有）",
    }
    # 不用 % 格式化整个模板：某些字段值里带 %(xxx)s 字面量会被二次解析。
    # 探针只关心 token 量，模板骨架 + 各字段分别标即可。
    prompt_tok = tok(_SCORE_PROMPT_V2) + sum(tok(v) for v in fields.values())

    lines = []
    lines.append("=" * 72)
    lines.append("编剧调用（第一段，单条 user）/ 线 %s / schema %s" % (
        key, ctx.get("schema")))
    lines.append("=" * 72)
    lines.append("【编剧 prompt 估算】 %6d token（模板骨架 + 各字段，未重复计 %s 字面）" % (
        prompt_tok, "%"))
    lines.append("  ├ 稳定模板骨架 %5d token（前缀缓存命中）" % tok(_SCORE_PROMPT_V2))
    # 逐字段：值本身的 token（模板里变量位）
    ranked = sorted(fields.items(), key=lambda kv: -tok(kv[1]))
    for name, val in ranked:
        lines.append("  │   %-14s %5d token (%d字)" % (name, tok(val), len(str(val))))
    lines.append("  注：history=_judge_recent 取最近6条×每条300字；"
                 "ledger=小记开头300字（2026-11-19 精简）")
    lines.append("")
    out = "\n".join(lines)
    print(out)
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write(out)


if __name__ == "__main__":
    key = sys.argv[1] if len(sys.argv) > 1 else "web_2509355624__izumi_sagiri"
    dump(key)
    dump_judge(key)
