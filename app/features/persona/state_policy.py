# -*- coding: utf-8 -*-
"""状态原文的选择策略。

`state_rules.py` 是原文；这里只决定**当前这一轮该拿哪几段原文**。
不做摘要、不做截断、不设总字数闸门：现实里有多少条命中，就返回多少条。

优先级按原书第 12 节：组合规则 > 单轴档位 > 通用说明。
单轴只取**当前档**，不把前面低档再注入一遍——那正是之前冗余的来源。
"""

from __future__ import annotations

from app.features.persona import state_rules
from app.features.persona.game_time import normalize_phase

# 开发度五处，按原书第 8 节的顺序。
_DEV_PARTS = ("mouth", "nipple", "pussy", "anus", "skin")

_PHASE_KEYS = ("早晨", "上午", "中午", "下午", "傍晚", "深夜")

_CONTACT_KEYS = {
    "none": "contact.0",
    "mouth": "contact.1",
    "pussy": "contact.2",
    "anus": "contact.3",
    "hand": "contact.4",
    "breast": "contact.5",
    "leg": "contact.6",
    # 几个常见别名（历史存档/前端可能写中文）
    "无": "contact.0",
    "口": "contact.1",
    "小穴": "contact.2",
    "后庭": "contact.3",
    "手": "contact.4",
    "胸": "contact.5",
    "腿": "contact.6",
}


def _num(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _by_key(key: str):
    return state_rules.by_key(key)


def _range_block(axis: str, value: int):
    """按 lo/hi 取当前档；没有区间元数据的枚举轴不要走这里。"""
    for b in state_rules.BLOCKS:
        if b.axis != axis or b.lo is None or b.hi is None:
            continue
        if b.lo <= value <= b.hi:
            return b
    return None


def _append(out: list, block, seen: set):
    if block is None:
        return
    if block.key in seen:
        return
    seen.add(block.key)
    out.append(block)


def _combo_blocks(out: list, seen: set, res: dict, gd: dict):
    """第 12 节：只注入当前真正命中的组合行。

    条件直接照原表写，不自己发明阈值；命中几行就返回几行。
    """
    phase = normalize_phase(gd.get("phase"))
    ar = _num(res.get("arousal"))
    ex = _num(res.get("excite"))
    val = _num(res.get("value"))
    sus = _num(res.get("suspicion"))
    sub = _num(res.get("submit"))
    cor = _num(res.get("corrupt"))
    dev = res.get("dev") if isinstance(res.get("dev"), dict) else {}
    con = str(gd.get("contact") or "").strip()
    cross = str(res.get("cross") or "").strip().lower()
    cross_n = _num(res.get("crossN") or res.get("cross_n"))

    # 12.1 时间 × 冲动
    if phase == "深夜" and ar < 60:
        _append(out, _by_key("combo.1.1"), seen)
    elif phase == "深夜" and 60 <= ar < 80:
        _append(out, _by_key("combo.1.2"), seen)
    elif phase == "深夜" and ar >= 80:
        _append(out, _by_key("combo.1.3"), seen)
    if phase == "早晨" and ar < 30:
        _append(out, _by_key("combo.1.4"), seen)
    if phase == "傍晚" and ar > 45:
        _append(out, _by_key("combo.1.5"), seen)
    if phase == "深夜" and ex > 75:
        _append(out, _by_key("combo.1.6"), seen)

    # 12.2 位置 × 高潮
    if con == "mouth" and ex > 75:
        _append(out, _by_key("combo.2.1"), seen)
    elif con == "pussy" and ex > 75 and sub < 40:
        _append(out, _by_key("combo.2.2"), seen)
    elif con == "pussy" and ex > 75 and sub > 60:
        _append(out, _by_key("combo.2.3"), seen)
    elif con == "anus" and ex > 75:
        _append(out, _by_key("combo.2.4"), seen)
    elif con == "none" and ex > 75:
        _append(out, _by_key("combo.2.5"), seen)

    # 12.3 戒备 × 好感
    if sus > 20 and val < 30:
        _append(out, _by_key("combo.3.1"), seen)
    elif sus > 20 and val > 60:
        _append(out, _by_key("combo.3.2"), seen)
    if sus < 10 and val > 60:
        _append(out, _by_key("combo.3.3"), seen)
    if sus > 30:
        _append(out, _by_key("combo.3.4"), seen)

    # 12.4 开发度 × 第一次/公共场合
    # 第一次进入由 `affection.mark_first()` 持久化在 `first_entry`，不是靠
    # `contact_prev` 猜；只有 pussy 这一条规则依赖「第一次」，所以只看它。
    first = res.get("first_entry") if isinstance(res.get("first_entry"), dict) else {}
    if (con == "pussy" and _num(dev.get("pussy")) < 30
            and not first.get("pussy")):
        _append(out, _by_key("combo.4.1"), seen)
    if con == "anus" and _num(dev.get("anus")) < 30:
        _append(out, _by_key("combo.4.2"), seen)
    if con == "mouth" and _num(dev.get("mouth")) < 30:
        _append(out, _by_key("combo.4.3"), seen)
    # 「dev 某处 > 80 × 公共场合」需要明确的 public 字段；没有就不猜。

    # 12.5 屈服 × 高潮表现
    if ex > 75:
        if sub < 30:
            _append(out, _by_key("combo.5.1"), seen)
        elif 30 <= sub <= 60:
            _append(out, _by_key("combo.5.2"), seen)
        elif sub > 70:
            _append(out, _by_key("combo.5.3"), seen)

    # 12.6 越界 × 后续
    if cross == "mild":
        _append(out, _by_key("combo.6.1"), seen)
    if cross == "hard":
        _append(out, _by_key("combo.6.2"), seen)
    if cross_n >= 2:
        _append(out, _by_key("combo.6.3"), seen)
    if val < 0 and cross:
        _append(out, _by_key("combo.6.4"), seen)

    # 12.7 堕落 × 屈服（两个都高时）
    if cor >= 70 and sub >= 70:
        _append(out, _by_key("combo.7"), seen)


def select_blocks(aff_res, gd=None) -> list:
    """返回这一轮该注入的原文块。空列表 = 这一轮没有攻略状态。

    **不设上限**。`aff_res` 是 `affection.settle()` 的返回值；`gd` 是
    gameday 字典。只有 `active` 的攻略轮才有轴，自由模式不猜。
    """
    if not isinstance(aff_res, dict) or not aff_res.get("active"):
        return []
    gd = gd if isinstance(gd, dict) else {}
    out: list = []
    seen: set = set()

    # 组合规则优先级最高，先选。
    _combo_blocks(out, seen, aff_res, gd)

    # 单轴：只取当前档，不取旧档。
    _append(out, _range_block("value", _num(aff_res.get("value"))), seen)
    _append(out, _range_block("suspicion", _num(aff_res.get("suspicion"))), seen)
    _append(out, _range_block("arousal", _num(aff_res.get("arousal"))), seen)
    _append(out, _range_block("excite", _num(aff_res.get("excite"))), seen)
    _append(out, _range_block("corrupt", _num(aff_res.get("corrupt"))), seen)
    _append(out, _range_block("submit", _num(aff_res.get("submit"))), seen)

    # 开发度：公共档每档只出现一次，五处各自的差异行全部保留。
    dev = aff_res.get("dev") if isinstance(aff_res.get("dev"), dict) else {}
    by_band: dict[str, list] = {}
    for part in _DEV_PARTS:
        val = _num(dev.get(part))
        band = _range_block("dev", val)
        if band is None:
            continue
        by_band.setdefault(band.key, [band, []])[1].append(part)
    for band_key, (band, parts) in by_band.items():
        _append(out, band, seen)
        for part in parts:
            _append(out, _by_key("dev.part.%s" % part), seen)

    # 身体进度：存档里的 `level` 就是原书第 9 节的 0~6。
    try:
        level = int(aff_res.get("level"))
    except (TypeError, ValueError):
        level = None
    if level is not None and 0 <= level <= 6:
        _append(out, _by_key("gauge.%d" % (level + 1)), seen)

    # 时段 / 接触：枚举，按当前值取一节。
    phase = normalize_phase(gd.get("phase"))
    if phase in _PHASE_KEYS:
        _append(out, _by_key("phase.%d" % (_PHASE_KEYS.index(phase) + 1)), seen)
    contact = str(gd.get("contact") or "").strip()
    key = _CONTACT_KEYS.get(contact)
    if key:
        _append(out, _by_key(key), seen)

    # 第 2 节「感情 level」在现有存档里没有独立字段（`level` 是第 9 节
    # 的身体进度），所以不拿它冒充；有真实字段时再接入。
    return out


def selected_text(aff_res, gd=None) -> str:
    """返回选中的原文，用空行分开；不截断。"""
    blocks = select_blocks(aff_res, gd)
    return "\n\n".join(b.text.strip() for b in blocks)
