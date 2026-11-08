# -*- coding: utf-8 -*-
"""道具系统（2026-11-08 用户拍板）。

**道具是「一段可复用的写法」，不是数值加成。**

玩家说「我用迷药」→ 查表 → 把这一条的 `prompt` 注入给演员 → 演员照着写。
代码只做三件事：查表、拼注入块、记信物。涨多少、怎么写细，全交给模型。

三条设计原则（用户原话）：
    · **无限、不记数量**。表里的东西就当随身带着，想用就用。
    · **表外的道具不注入**。玩家临时编的只记名字，让模型自由发挥——
      硬编一段效果反而会写死。
    · **涨多少仍由编剧判**。`affects` 只说明「这轮该往哪几轴看」，不给数值。

和 `affection_v2_adult` 的分工：那边管数值怎么变，这边管「为什么变」的写法。
"""

import json
import logging
import os
import re

from app.config import BASE_DIR

log = logging.getLogger("items")

_CFG_FILE = "items.json"

# kind 三种（见配置的 _note）
KINDS = ("consumable", "plot", "restraint")


def _cfg():
    """每次重读配置——改道具表不用重启（和其它 config 同口径）。"""
    try:
        path = os.path.join(str(BASE_DIR), "config", _CFG_FILE)
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh)
        if isinstance(d, dict) and isinstance(d.get("items"), dict):
            return d
    except (OSError, ValueError):
        log.warning("道具表读不到，这轮按没有道具处理")
    return {"items": {}}


def all_items():
    return _cfg().get("items") or {}


def get(key):
    """按 key 取一条；没有返回 None。"""
    if not key:
        return None
    return all_items().get(str(key))


def match(text):
    """从一段话里找出提到了哪些道具，返回 `[key, ...]`（按出现顺序去重）。

    **只认配置里的别名**——不猜、不模糊匹配。玩家说「我用那个东西」认不出来
    就是认不出来，交给编剧自己判，代码不替它编。
    """
    s = str(text or "")
    if not s:
        return []
    hits = []
    for key, it in all_items().items():
        if not isinstance(it, dict):
            continue
        for alias in (it.get("aliases") or []):
            a = str(alias or "").strip()
            if a and a in s:
                if key not in hits:
                    hits.append(key)
                break
    return hits


def kind_of(key):
    it = get(key) or {}
    return str(it.get("kind") or "")


def is_plot(key):
    """剧情道具（迷药、催眠手机）——这类改走向，不只是推数值。"""
    return kind_of(key) == "plot"


def is_restraint(key):
    """束缚类（眼罩、束缚带）——生效期间持续作用。"""
    return kind_of(key) == "restraint"


# ─── 注入块 ──────────────────────────────────────────

def list_block():
    """常态注入：只给**名字清单**。

    十几个道具全注 prompt 要 600+ 字，只给名字不到 100 字。
    用到哪一条时再单独注入那一条的 prompt（见 `use_block`）。
    这就是整个设计省 token 的关键。
    """
    items = all_items()
    if not items:
        return ""
    names = [str((it or {}).get("name") or k) for k, it in items.items()
             if isinstance(it, dict)]
    if not names:
        return ""
    return ("## 你能用的东西\n\n"
            + " · ".join(names)
            + "\n\n（想用哪样直接说，不限次数。她没防备的时候效果最好；"
              "用到哪样，写法会单独告诉你。）\n")


def use_block(keys):
    """这一轮用到的道具：注入它们的 prompt。

    `keys` 是 key 列表（编剧给的或从原话匹配出来的）。表外的一律跳过——
    那是玩家自己编的，自由发挥效果更好。
    """
    if not keys:
        return ""
    parts = []
    used = []
    for k in keys:
        it = get(k)
        if not it:
            continue
        prompt = str(it.get("prompt") or "").strip()
        if not prompt:
            continue
        used.append(str(it.get("name") or k))
        parts.append(prompt)
    if not parts:
        return ""
    head = "## 这一轮用到的：" + "、".join(used) + "\n\n"
    return head + "\n\n".join(parts) + "\n"


def unknown_block(names):
    """表外的道具：只记名字，不注入效果。"""
    names = [str(n).strip() for n in (names or []) if str(n).strip()]
    if not names:
        return ""
    return ("## 玩家自己拿出来的东西\n\n"
            + "、".join(names)
            + "\n\n（不在道具表里，按他说的那样写就行。）\n")


def keepsake_block(records):
    """信物：她给的私人物品。**只有这一类记来源和日期**。

    `records` 是存档里的 `items` 列表（每个 `{name, kind, from, day, note}`）。
    """
    keeps = [r for r in (records or [])
             if isinstance(r, dict) and str(r.get("kind") or "") == "keepsake"]
    if not keeps:
        return ""
    lines = []
    for r in keeps:
        name = str(r.get("name") or "").strip()
        if not name:
            continue
        who = str(r.get("from") or "").strip()
        day = r.get("day")
        tail = ""
        if who:
            tail = "（%s 给的" % who
            if day:
                tail += "，第 %s 天" % day
            tail += "）"
        elif day:
            tail = "（第 %s 天拿到）" % day
        note = str(r.get("note") or "").strip()
        lines.append("- %s%s%s" % (name, tail, ("：" + note) if note else ""))
    if not lines:
        return ""
    return ("## 她给过你的东西\n\n" + "\n".join(lines)
            + "\n\n（这些她认得出来。她看见的时候会想起那天。）\n")


# ─── 存档记录 ────────────────────────────────────────

def normalize(records):
    """清洗存档里的 `items` 列表：只留认得出的 dict，字段收敛。"""
    out = []
    seen = set()
    for r in (records or []):
        if not isinstance(r, dict):
            continue
        name = str(r.get("name") or "").strip()
        if not name:
            continue
        key = (name, str(r.get("from") or ""))
        if key in seen:
            continue
        seen.add(key)
        rec = {"name": name[:40], "kind": str(r.get("kind") or "tool")[:16]}
        if r.get("from"):
            rec["from"] = str(r["from"])[:24]
        if r.get("day") is not None:
            try:
                rec["day"] = int(r["day"])
            except (TypeError, ValueError):
                pass
        if r.get("note"):
            rec["note"] = str(r["note"])[:80]
        out.append(rec)
        if len(out) >= 60:          # 防御：别让存档无限长
            break
    return out


def apply_gain(records, gains):
    """把这一轮拿到的东西记进存档。返回新列表。

    `gains` 形如 `[{"name": "钥匙扣", "kind": "keepsake", "from": "她", "day": 3}]`。
    **同名同来源的不会重复记**——她已经给过的东西不会再给一次。
    """
    cur = normalize(records)
    have = {(r["name"], r.get("from") or "") for r in cur}
    for g in (gains or []):
        if not isinstance(g, dict):
            continue
        name = str(g.get("name") or "").strip()
        if not name:
            continue
        src = str(g.get("from") or "").strip()
        if (name, src) in have:
            continue
        have.add((name, src))
        cur.append(g)
    return normalize(cur)


def panel(records):
    """前端道具面板的只读视图。"""
    items = all_items()
    catalog = []
    for key, it in items.items():
        if not isinstance(it, dict):
            continue
        catalog.append({
            "key": key,
            "name": str(it.get("name") or key),
            "kind": str(it.get("kind") or ""),
            "help": it.get("help") or None,
        })
    return {
        "catalog": catalog,
        "keepsakes": [r for r in normalize(records)
                      if str(r.get("kind") or "") == "keepsake"],
    }
