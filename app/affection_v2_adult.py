# -*- coding: utf-8 -*-
"""v2 成人向状态条结算器（2026-11-08）。

九条轴的数值/档位存在存档记录的 `adult` 子字典里，**和 v1 的
`arousal/excite/corrupt/submit/dev` 完全隔开**——老档没有 `schema=2`
就不会走到这个模块，v1 那套一个字不动。

分工（用户拍板的）：
    · 冲动 arousal  —— 档位 0~4，编剧判，这里只做闸门（dev < 40 封到 2 档）
    · 高潮 excite   —— 数值，涨幅编剧给，到顶回落 reset_to
    · 堕落 corrupt  —— 正向恒 0，负好感 + 不愿意的事 + 身体有反应才涨
    · 屈服 submit   —— 正向恒 0，负好感 + 驯化行为才涨
    · 开发 dev      —— 五处，一直涨，只涨不掉
    · 敏感 sense    —— 五处，恒 >= dev，只从前戏/道具涨，高潮后回落
    · 羞耻 shame    —— 0~50 正常亲密，50~100 只能靠露出；重复行为递减
    · 主动权 control—— 0~100，50 对等
    · 接触 contact  —— 枚举，只记事实

**这个模块只算数，不判断。** 什么算「她不愿意的事」、什么算「驯化行为」、
什么算「露出」、什么算「前戏」，全部由编剧 AI 判完传进来（见
`_SCORE_PROMPT_V2` 的成人向字段）。这里只负责：clamp、闸门、回落、去重。
"""

import json
import logging
import os

from app.config import BASE_DIR

log = logging.getLogger("affection_adult")

_ADULT_CFG_FILE = "affection_v2_adult.json"

# 五处身体部位（顺序固定，前端按这个顺序画）
PARTS = ("mouth", "nipple", "pussy", "anus", "skin")

_CONTACT_VALUES = ("none", "mouth", "pussy", "anus", "hand", "breast", "leg")


# ─── 配置 ────────────────────────────────────────────

_FALLBACK = {
    "schema": 2,
    "arousal": {"type": "tier", "dev_gate": {"below": 40, "max_tier": 2}, "levels": []},
    "excite": {"type": "number", "min": 0, "max": 100, "initial": 0, "reset_to": 50, "levels": []},
    "corrupt": {"type": "number", "min": 0, "max": 100, "initial": 0,
                "gain_only": True, "gate": {"value_below": 0}, "levels": []},
    "submit": {"type": "number", "min": 0, "max": 100, "initial": 0,
               "gain_only": True, "gate": {"value_below": 0}, "levels": []},
    "dev": {"type": "map", "parts": list(PARTS), "min": 0, "max": 100,
            "initial": 0, "gain_only": True, "levels": []},
    "sense": {"type": "map", "parts": list(PARTS), "min": 0, "max": 100,
              "initial": 0, "floor_of": "dev", "decay": {"on_orgasm": 10}, "levels": []},
    "shame": {"type": "number", "min": 0, "max": 100, "initial": 20, "soft_cap": 50,
              "routine": {"decay_per_repeat": 5, "min_gain": 0}, "levels": []},
    "control": {"type": "number", "min": 0, "max": 100, "initial": 50, "neutral": 50, "levels": []},
    "contact": {"type": "enum", "values": list(_CONTACT_VALUES), "levels": []},
}


def _cfg():
    """每次重读配置文件（改了不用重启，和 v2 档位配置同口径）。"""
    try:
        path = os.path.join(str(BASE_DIR), "config", _ADULT_CFG_FILE)
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh)
        if isinstance(d, dict):
            # 缺轴就用兜底补齐，不让半份配置把整轮算崩
            for k, v in _FALLBACK.items():
                d.setdefault(k, v)
            return d
    except (OSError, ValueError):
        log.warning("成人向配置读不到，用内嵌兜底")
    return _FALLBACK


def _i(x, default=0):
    try:
        return int(x or 0)
    except (TypeError, ValueError):
        return default


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def _axis_range(cfg, key):
    a = cfg.get(key) or {}
    return _i(a.get("min", 0)), _i(a.get("max", 100))


# ─── 初始 / 读档 ─────────────────────────────────────

def empty_state():
    """一条新线第一次碰成人向轴时的初始存档。"""
    cfg = _cfg()
    dev0 = {p: _i((cfg.get("dev") or {}).get("initial", 0)) for p in PARTS}
    sense0 = {p: max(_i((cfg.get("sense") or {}).get("initial", 0)), dev0[p]) for p in PARTS}
    shame = cfg.get("shame") or {}
    control = cfg.get("control") or {}
    return {
        # 冲动是档位，用 key 存而不是数字——数值没意义，档名才是给演员看的
        "arousal": str(((cfg.get("arousal") or {}).get("levels") or [{}])[0].get("key") or "calm"),
        "excite": _i((cfg.get("excite") or {}).get("initial", 0)),
        "corrupt": _i((cfg.get("corrupt") or {}).get("initial", 0)),
        "submit": _i((cfg.get("submit") or {}).get("initial", 0)),
        "dev": dev0,
        "sense": sense0,
        "shame": _i(shame.get("initial", 20)),
        "control": _i(control.get("initial", 50)),
        "contact": "none",
        # 羞耻的「做过什么」台账：{事件类别: 次数}。涨幅按次数递减。
        "shame_log": {},
        # 发生过几次高潮，用来算回落之后还剩多少（暂时只做计数，不改算法）
        "orgasm_count": 0,
    }


def _pick_adult(rec):
    """从存档记录里定位成人向状态那一小块。

    **唯一位置是 `affection.adult`**（和 value/contacts 放一起，属于关系档的
    一部分）。顶层 `adult` 只是极早期副本的兼容读法——两边都认，就不会出现
    「写一份、读另一份」把起点数值盖成默认值的 bug（2026-11-08 实测踩过）。
    """
    if not isinstance(rec, dict):
        return None
    aff = rec.get("affection")
    if isinstance(aff, dict) and isinstance(aff.get("adult"), dict):
        return aff["adult"]
    if isinstance(rec.get("adult"), dict):
        return rec["adult"]
    return None


def load(rec):
    """从存档记录里取成人向状态；没有或字段残缺就补齐。**不改 rec**。"""
    src = _pick_adult(rec)
    st = empty_state()
    if not isinstance(src, dict):
        return st
    if isinstance(src.get("arousal"), str) and src["arousal"]:
        st["arousal"] = src["arousal"]
    for k in ("excite", "corrupt", "submit", "shame", "control", "orgasm_count"):
        if k in src:
            st[k] = _i(src.get(k), st[k])
    for k in ("dev", "sense"):
        if isinstance(src.get(k), dict):
            for p in PARTS:
                if p in src[k]:
                    st[k][p] = _i(src[k][p], st[k][p])
    if isinstance(src.get("shame_log"), dict):
        st["shame_log"] = {str(k): _i(v) for k, v in src["shame_log"].items()}
    if src.get("contact") in _CONTACT_VALUES:
        st["contact"] = src["contact"]
    return normalize(st)


def save(rec, st):
    """把状态写回 rec 的 `affection.adult`。返回 rec（调用方自己落盘）。

    唯一写入位置和 `_pick_adult` 的优先读法一致；顶层那份若存在就顺手清掉，
    免得以后有人读错位置。
    """
    if not isinstance(rec, dict):
        return rec
    rec.pop("adult", None)
    aff = rec.setdefault("affection", {})
    aff["adult"] = normalize(st)
    return rec


def normalize(st):
    """把所有值收进合法区间，并强制 sense >= dev / 闸门 / 枚举合法。"""
    cfg = _cfg()
    st = dict(st or {})

    # 冲动：档位 key 必须存在
    a_levels = (cfg.get("arousal") or {}).get("levels") or []
    keys = [x.get("key") for x in a_levels]
    if st.get("arousal") not in keys:
        st["arousal"] = keys[0] if keys else "calm"

    for k in ("excite", "corrupt", "submit", "shame", "control"):
        lo, hi = _axis_range(cfg, k)
        st[k] = _clamp(_i(st.get(k), _i((cfg.get(k) or {}).get("initial", 0))), lo, hi)

    dev = st.get("dev") or {}
    sense = st.get("sense") or {}
    lo, hi = _axis_range(cfg, "dev")
    for p in PARTS:
        dev[p] = _clamp(_i(dev.get(p)), lo, hi)
    lo2, hi2 = _axis_range(cfg, "sense")
    for p in PARTS:
        # 硬约束：敏感度不会低于开发度（开发 80 敏感 20 是不存在的）
        sense[p] = _clamp(max(_i(sense.get(p)), dev[p]), lo2, hi2)
    st["dev"], st["sense"] = dev, sense

    if st.get("contact") not in _CONTACT_VALUES:
        st["contact"] = "none"
    if not isinstance(st.get("shame_log"), dict):
        st["shame_log"] = {}
    st["orgasm_count"] = max(0, _i(st.get("orgasm_count")))
    return st


# ─── 档位 / 视图 ─────────────────────────────────────

def _level_of(axis_key, value, cfg=None):
    """数值落在哪一档（min 升序取最后一个 <= value 的）。"""
    cfg = cfg or _cfg()
    levels = sorted((((cfg.get(axis_key) or {}).get("levels")) or []),
                    key=lambda x: _i(x.get("min")))
    hit = None
    for lv in levels:
        if value >= _i(lv.get("min")):
            hit = lv
    return hit or (levels[0] if levels else {})


def arousal_of(st, cfg=None):
    """当前冲动档位的那条配置（含 prompt）。"""
    cfg = cfg or _cfg()
    key = (st or {}).get("arousal")
    for lv in ((cfg.get("arousal") or {}).get("levels") or []):
        if lv.get("key") == key:
            return lv
    return {}


def dev_level(st, part, cfg=None):
    return _level_of("dev", _i(((st or {}).get("dev") or {}).get(part)), cfg)


def sense_level(st, part, cfg=None):
    return _level_of("sense", _i(((st or {}).get("sense") or {}).get(part)), cfg)


def dev_avg(st):
    """五处开发度的加权平均，只用来判冲动闸门。"""
    dev = (st or {}).get("dev") or {}
    if not dev:
        return 0
    return sum(_i(dev.get(p)) for p in PARTS) // len(PARTS)


# ─── 结算：编剧给的 delta 落成数值 ─────────────────────

def apply(st, delta, value=None):
    """把编剧判出来的一轮变化落进状态。

    `delta` 是编剧交的一个 dict，**字段全都可缺**，缺了就不动：

        {
          "arousal": "want",              # 档位 key
          "excite": 25,                   # 本轮涨幅（负数是回落）
          "corrupt": 8,                   # 本轮涨幅（正向好感会被闸门吃掉）
          "submit": 5,
          "dev":  {"pussy": 10, "mouth": 5},
          "sense": {"pussy": 8},
          "shame": 6,
          "shame_event": "pussy_sex",     # 这一轮算哪一类羞耻事件（重复递减用）
          "control": -8,
          "contact": "pussy",
          "orgasm": true                  # 这一轮她到了
        }

    `value` 是**主好感值**（affection.value）。堕落和屈服靠它判闸门：
    正向好感时这两条恒定不涨（用户拍板）。
    """
    cfg = _cfg()
    st = normalize(dict(st or empty_state()))
    notes = []          # 给调用方记日志用：这一轮被闸门拦掉了什么

    # 1) 冲动：编剧给 key。开发度不够时封顶。
    a = (delta or {}).get("arousal")
    if isinstance(a, str) and a:
        a_levels = (cfg.get("arousal") or {}).get("levels") or []
        by_key = {lv.get("key"): lv for lv in a_levels}
        if a in by_key:
            gate = (cfg.get("arousal") or {}).get("dev_gate") or {}
            cap = _i(gate.get("max_tier"), 99)
            target = _i(by_key[a].get("tier"))
            if dev_avg(st) < _i(gate.get("below"), 40) and target > cap:
                for lv in a_levels:
                    if _i(lv.get("tier")) == cap:
                        a = lv.get("key")
                        notes.append("冲动被开发度闸门压到 %s（dev 均值 %d < %d）"
                                     % (a, dev_avg(st), _i(gate.get("below"), 40)))
                        break
            st["arousal"] = a

    # 2) 高潮：涨幅 + 到顶回落
    ex = _i((delta or {}).get("excite"))
    if ex:
        lo, hi = _axis_range(cfg, "excite")
        st["excite"] = _clamp(st["excite"] + ex, lo, hi)
    if st["excite"] >= _axis_range(cfg, "excite")[1] or (delta or {}).get("orgasm"):
        reset_to = _i((cfg.get("excite") or {}).get("reset_to"), 50)
        st["excite"] = _clamp(reset_to, *_axis_range(cfg, "excite"))
        st["orgasm_count"] += 1
        # 高潮后敏感度整片回落
        drop = _i(((cfg.get("sense") or {}).get("decay") or {}).get("on_orgasm"), 10)
        for p in PARTS:
            st["sense"][p] = max(st["dev"][p], st["sense"][p] - drop)

    # 3) 堕落 / 屈服：正向好感时恒定不涨（用户拍板）
    positive = value is not None and _i(value) >= 0
    for axis in ("corrupt", "submit"):
        d = _i((delta or {}).get(axis))
        if not d:
            continue
        gate = (cfg.get(axis) or {}).get("gate") or {}
        need_below = _i(gate.get("value_below"), 0)
        if positive or (value is not None and _i(value) >= need_below):
            notes.append("%s 被好感闸门吃掉（好感 %s 不为负）" % (axis, value))
            continue
        lo, hi = _axis_range(cfg, axis)
        st[axis] = _clamp(st[axis] + d, lo, hi)

    # 4) 开发度：只涨不掉
    for part, d in (((delta or {}).get("dev")) or {}).items():
        if part not in PARTS:
            continue
        lo, hi = _axis_range(cfg, "dev")
        v = st["dev"][part] + _i(d)
        st["dev"][part] = _clamp(v if v > st["dev"][part] else st["dev"][part], lo, hi)

    # 5) 敏感度：涨幅只来自前戏/道具（编剧判），高潮后的回落已在上面做过
    for part, d in (((delta or {}).get("sense")) or {}).items():
        if part not in PARTS:
            continue
        lo, hi = _axis_range(cfg, "sense")
        v = st["sense"][part] + _i(d)
        st["sense"][part] = _clamp(max(v, st["dev"][part]), lo, hi)

    # 6) 羞耻：0~50 软顶，50 以上只能靠「露出类」事件继续涨；重复事件递减
    sd = _i((delta or {}).get("shame"))
    if sd:
        ev = str((delta or {}).get("shame_event") or "").strip()
        soft = _i((cfg.get("shame") or {}).get("soft_cap"), 50)
        lo, hi = _axis_range(cfg, "shame")
        exposure = _is_exposure(ev)
        if sd > 0 and not exposure:
            # 普通亲密事件：**涨完之后不能越过软顶**（2026-11-08 实测踩过：
            # 只判「涨之前是否已过 50」，48 + 6 就直接冲到 54 了）。
            room = soft - st["shame"]
            if room <= 0:
                notes.append("羞耻已到软顶 %d，事件「%s」不再推进" % (soft, ev or "未分类"))
                sd = 0
            elif sd > room:
                notes.append("羞耻事件「%s」被软顶截断到 %d" % (ev or "未分类", soft))
                sd = room
        if sd > 0 and ev:
            log_tbl = st["shame_log"]
            times = _i(log_tbl.get(ev))
            decay = _i(((cfg.get("shame") or {}).get("routine") or {}).get("decay_per_repeat"), 5)
            min_gain = _i(((cfg.get("shame") or {}).get("routine") or {}).get("min_gain"), 0)
            # 同一类事件做第 n 次时，涨幅 = sd - (n-1)*decay，最低 0。
            # 到 0 就彻底不涨了——已经习惯的事不该再让她羞（用户原话）。
            effective = sd - times * decay
            if effective <= min_gain:
                notes.append("羞耻事件「%s」已做过 %d 次，不再增加羞耻" % (ev, times))
                effective = min_gain
            sd = effective
            log_tbl[ev] = times + 1
        st["shame"] = _clamp(st["shame"] + sd, lo, hi)

    # 7) 主动权：0~100，50 对等
    cd = _i((delta or {}).get("control"))
    if cd:
        lo, hi = _axis_range(cfg, "control")
        st["control"] = _clamp(st["control"] + cd, lo, hi)

    # 8) 接触位置：枚举，只记事实
    c = (delta or {}).get("contact")
    if isinstance(c, str) and c in _CONTACT_VALUES:
        st["contact"] = c

    return normalize(st), notes


def _is_exposure(ev):
    """这一轮算不算「露出 / 被看见」类事件——过了羞耻软顶只有这类还能涨。"""
    ev = str(ev or "").lower()
    return any(w in ev for w in ("expos", "public", "outdoor", "seen", "caught", "露出", "户外", "被看"))


# ─── 给演员的中文注入 ────────────────────────────────

def block(st, cfg=None):
    """把当前九条轴翻译成中文自然语言，拼进演员的系统头。

    **只描述她此刻是什么样**，不给字段名、不给数字——数值是后台的事，
    她只知道自己身体怎么了（同 v1 状态值全书的口径）。
    """
    cfg = cfg or _cfg()
    st = normalize(st or empty_state())
    out = []

    # 冲动（档位）
    a = arousal_of(st, cfg)
    if a.get("prompt"):
        out.append("【她这会儿想不想】%s" % a["prompt"])

    # 高潮（数值 → 档位文案）
    e = _level_of("excite", st["excite"], cfg)
    if e.get("prompt"):
        out.append("【她身体走到哪了】%s" % e["prompt"])

    # 开发 / 敏感：只说被碰过的那几处，没动过的就不占篇幅
    touched = [p for p in PARTS if st["dev"][p] > 0]
    if touched:
        lines = []
        for p in touched:
            dl = dev_level(st, p, cfg)
            sl = sense_level(st, p, cfg)
            name = ((cfg.get("dev") or {}).get("part_names") or {}).get(p, p)
            piece = "%s：%s" % (name, dl.get("prompt") or dl.get("name") or "")
            if sl.get("key") != "dull":
                piece += "（现在%s）" % (sl.get("name") or "")
            lines.append("- " + piece)
        out.append("【几处身体的记性】\n" + "\n".join(lines))

    # 堕落 / 屈服：只有涨过才说
    if st["corrupt"] > 0:
        c = _level_of("corrupt", st["corrupt"], cfg)
        if c.get("prompt"):
            out.append("【她身体比嘴先服到哪一步】%s" % c["prompt"])
    if st["submit"] > 0:
        s = _level_of("submit", st["submit"], cfg)
        if s.get("prompt"):
            out.append("【她认不认这个位置】%s" % s["prompt"])

    # 羞耻
    sh = _level_of("shame", st["shame"], cfg)
    if sh.get("prompt"):
        out.append("【她羞不羞】%s" % sh["prompt"])

    # 主动权
    ct = _level_of("control", st["control"], cfg)
    if ct.get("prompt"):
        out.append("【这一拍谁在带】%s" % ct["prompt"])

    # 接触位置
    for lv in ((cfg.get("contact") or {}).get("levels") or []):
        if lv.get("key") == st["contact"]:
            out.append("【身体连在哪】%s" % lv.get("prompt"))
            break

    body = "\n".join(out)
    if not body:
        return ""
    return "## 她身上这会儿\n\n" + body + "\n"


# ─── 给前端的状态面板（只读） ─────────────────────────

def panel(st, cfg=None):
    """前端画条用的只读视图。**不重算、不写档**。"""
    cfg = cfg or _cfg()
    st = normalize(st or empty_state())
    rows = []

    def row(axis, key, value, extra=None):
        lo, hi = _axis_range(cfg, key)
        lv = _level_of(key, value, cfg)
        r = {"axis": axis, "value": value, "min": lo, "max": hi,
             "level": lv.get("key") or "", "name": lv.get("name") or ""}
        if extra:
            r.update(extra)
        rows.append(r)

    a = arousal_of(st, cfg)
    rows.append({"axis": "arousal", "type": "tier",
                 "level": a.get("key") or "", "name": a.get("name") or "",
                 "tier": _i(a.get("tier")),
                 "maxTier": _i(((cfg.get("arousal") or {}).get("dev_gate") or {}).get("max_tier"), 4),
                 "gateActive": dev_avg(st) < _i(((cfg.get("arousal") or {}).get("dev_gate") or {}).get("below"), 40)})
    row("excite", "excite", st["excite"])
    row("corrupt", "corrupt", st["corrupt"], {"gated": True})
    row("submit", "submit", st["submit"], {"gated": True})
    row("shame", "shame", st["shame"],
        {"softCap": _i((cfg.get("shame") or {}).get("soft_cap"), 50)})
    row("control", "control", st["control"],
        {"neutral": _i((cfg.get("control") or {}).get("neutral"), 50)})

    parts = []
    names = (cfg.get("dev") or {}).get("part_names") or {}
    for p in PARTS:
        parts.append({"part": p, "name": names.get(p, p),
                      "dev": st["dev"][p], "sense": st["sense"][p]})
    contact = st["contact"]
    return {
        "rows": rows,
        "parts": parts,
        "contact": contact,
        "contactName": ((cfg.get("contact") or {}).get("names") or {}).get(contact, contact),
        "orgasmCount": st["orgasm_count"],
    }
