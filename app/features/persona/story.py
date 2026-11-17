"""【故事到现在】：把 gameday、账本和约定排成一条因果线。

纯文本重组，不新增信息、不调模型：时间来自 gameday，已经发生来自
`event` + 账本「你的事 / 她的事」，还没完成来自 `plan` +「约定」。
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
    """【故事到现在】——时间、已经发生的事、还没完成的事，排成一条线。

    任一部分缺失就跳过；全空返回空串。跨天时让模型对照事实自己判断
    约定做到没做到，代码只报「账本记着」。
    """
    gd = gd if isinstance(gd, dict) else {}
    day = _as_int(gd.get("day"), 0, 0, 99999)
    phase = str(gd.get("phase") or "").strip()
    place = str(gd.get("place") or "").strip()
    event = str(gd.get("event") or "").strip()
    plan = str(gd.get("plan") or "").strip()
    plan_day = _as_int(gd.get("plan_day"), 0, 0, 99999)

    ledger = _ledger_fix_pronouns(note) if note else ""
    done = (_clip_items(_ledger_section(ledger, "你的事"), 3, 60)
            + _clip_items(_ledger_section(ledger, "她的事"), 3, 60))
    todo = _clip_items(_ledger_section(ledger, "约定"), 4, 50)

    parts = []
    if day:
        stamp = "第 %d 天" % day
        if phase:
            stamp += " · " + phase
        if place:
            stamp += " · " + place
        parts.append("时间：" + stamp)
    if event:
        if event.startswith("已经办完："):
            parts.append("今天已经办完：" + event[len("已经办完："):])
        else:
            parts.append("今天：" + event)
    if done:
        parts.append("已经发生：" + "；".join(done))
    if plan:
        if plan_day and plan_day > day:
            when = "（约在第 %d 天）" % plan_day
        elif plan_day:
            when = "（第 %d 天约的，那天已经过了）" % plan_day
        else:
            when = ""
        todo = [t for t in todo if not _shares_phrase(plan, t)]
        todo = [plan + when] + todo
    if todo:
        parts.append("账本里还记着的约定：" + "；".join(todo[:5]))
    if not parts:
        return ""
    body = "\n".join(parts)
    if len(body) > STORY_MAX_CHARS:
        body = body[:STORY_MAX_CHARS].rstrip("；，、") + "…"
    return ("【故事到现在】\n" + body
            + "\n已经办完的事就是过去了；约定做到没做到，对照上面的事实和"
              "刚才的对话自己判断，做过的事留在昨天。"
              "这一拍接着眼前正在发生的事往下写。")
