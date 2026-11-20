"""【故事到现在】：把 gameday 和账本排成一条因果线。

纯文本重组，不新增信息、不调模型：时间来自 gameday，已经发生来自
`event` + 账本「你的事 / 她的事」。

2026-11-19：不再把「约定与未完成」当发展线注入。用户实测这一节会把
「裙摆上那道划痕」这种一次性细节写成待办，演员每轮都去回收，剧情变成
反复提同一件小事。明确承诺改由 RAG 批次叙事和最近 12 条原文承接。
"""

import re

from app.features.persona.cards_store import _as_int
from app.features.persona.ledger_text import _ledger_fix_pronouns

# 账本每 5 轮才压一次，中间那几轮两边都没有；这里留够空间一次说全。
STORY_MAX_CHARS = 600

_PUNCT_CHARS = "，。；、！？…—·「」“”‘’（）()[],.!?;: \t\n"


def _ledger_section(note, key):
    """从六小节账本里抽一节的条目；散文账本返回 []。

    key 用子串匹配，所以「约定」能对上「约定与未完成」。
    """
    text = str(note or "")
    if not text.strip() or "#" not in text:
        return []
    out, cur = [], ""
    for line in text.splitlines():
        s = line.strip()
        m = re.match(r"^#{1,3}\s*(.+?)\s*$", s)
        if m:
            cur = m.group(1)
            continue
        if not s or not cur or key not in cur:
            continue
        s = re.sub(r"^[-*·]\s*", "", s)
        if s:
            out.append(s)
    return out


def _clip_items(items, count, per):
    """取最后 count 条、每条压到 per 字，去重保序，剥掉句末标点。"""
    out, seen = [], set()
    for s in (items or [])[-max(1, int(count)):]:
        s = " ".join(str(s or "").split()).rstrip("。；，、")
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s if len(s) <= per else s[:per].rstrip("。；，、") + "…")
    return out


def _shares_phrase(a, b, thr=0.35):
    """两句话是不是在说同一件事（去标点后的字符集合 Jaccard）。"""
    sa = {ch for ch in str(a or "") if ch not in _PUNCT_CHARS}
    sb = {ch for ch in str(b or "") if ch not in _PUNCT_CHARS}
    if len(sa) < 4 or len(sb) < 4:
        return False
    return len(sa & sb) / len(sa | sb) >= thr


def story_line(gd, note=""):
    """【故事到现在】——时间 + 已经发生的事，排成一条线。

    任一部分缺失就跳过；全空返回空串。只报已经发生的事实，不再输出账本
    里的「约定与未完成」；当天的事件/约定由 `_gameday_block` 负责，这里
    不重复。这一拍怎么接由写作 AI 自己看最近原文判断。
    """
    gd = gd if isinstance(gd, dict) else {}
    day = _as_int(gd.get("day"), 0, 0, 99999)
    phase = str(gd.get("phase") or "").strip()
    place = str(gd.get("place") or "").strip()

    ledger = _ledger_fix_pronouns(note) if note else ""
    done = (_clip_items(_ledger_section(ledger, "你的事"), 3, 60)
            + _clip_items(_ledger_section(ledger, "她的事"), 3, 60))

    parts = []
    if day:
        stamp = "第 %d 天" % day
        if phase:
            stamp += " · " + phase
        if place:
            stamp += " · " + place
        parts.append("时间：" + stamp)
    if done:
        parts.append("已经发生：" + "；".join(done))
    if not parts:
        return ""
    body = "\n".join(parts)
    if len(body) > STORY_MAX_CHARS:
        body = body[:STORY_MAX_CHARS].rstrip("；，、") + "…"
    return ("【故事到现在】\n" + body
            + "\n已经发生的事就是过去了；这一拍接着眼前正在发生的事往下写。")
