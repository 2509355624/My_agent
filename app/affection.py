"""攻略模式：好感度数值层（2026-11-03）。

## 它是什么

自由模式（现状）完全不经由这个模块。攻略模式下，两段式扮演的第一段
（意图判断）会顺带输出一个打分 JSON，这里负责把分数**结算**成分线状态里
持久的好感值，并按档位给出要注入主对话的提示词。

设计原则（和整个项目一致）：

  - **判断交给 AI**：这轮算多少分、算聊天还是剧情，由第一段模型斟酌；
  - **代码只做检测和记账**：夹单轮上限、降档滞回、越线判结局；
  - **不设按天的时间闸**（2026-11-04 晚）：聊天和剧情同权，玩家一次会话里
    能一路推到底，不存在「今天涨够了明天再来」；
  - 档位**文案**全部来自 `config/affection_tiers.json`（用户自己配），
    机制层只认 key / 区间 / aftercare 标记，不掺和内容。

## 数值与档位

好感 -50 ~ +50，初始 0。9 个行为档 + 2 个结局（毕业 / 进局子）。
结局**越线即触发**（≥50 / ≤-50），触发后数值锁定，直到「清空存档」
（回到这张卡的起点）或切回自由模式（封存原值）。切自由模式会**顺手解除结局**
（2026-11-04）：不解除的话，切回攻略模式的第一轮结算会拿同一个越线值
再判一次结局，玩家等于被关在局子里出不来。

## 状态长什么样

存在 persona 分线条目的 `affection` 字段（没有这个字段 = 自由模式）：

    {"mode": "raid" | "free", "value": 12, "tier": "touch",
     "down": 0, "ending": "" | "graduated" | "jail",
     "day": "2026-11-03", "chat": 0, "acts": 0,
     "hist": [{"d": 3, "r": "原因", "ts": "..."}]}

`mode` 为 "free" 时数值**封存不删**——切回 raid 原值接着来。
"""
import datetime
import json
import logging
import os

from app.config import BASE_DIR

log = logging.getLogger("affection")

MIN_V = -50
MAX_V = 50
INITIAL_V = 0
ROUND_CAP = 15          # 单轮加分绝对值上限（防一句话刷满）

# ── 距离层级（2026-11-04）────────────────────────────────
# 「你们到哪一步了」。0~5，描述**亲疏**不描述**身份**——这样一张骨科卡
# 可以从「妹妹」这个起点开始，不用被「陌生人」框死。卡片可用
# `start_level` 指定起点、`level_names` 覆盖 6 级的叫法。
LEVEL_MAX = 5
AROUSAL_MAX = 100       # 冲动：此刻被激起的程度，一次性机会
AROUSAL_DECAY = 15      # 每轮回落多少（回合结束就凉）
# 越界被拒的代价，按当晚第几次递增（不冻结、不降级——自己哄回来）
CROSS_PENALTY = (3, 6, 10)
CROSS_HARD = 20         # 她说了不要还继续：一次就够狠
CROSS_JAIL_AT = 2       # 强行几次触发报警（这条线结束，只能重开）
# 2026-11-04 晚：**按天的额度全部取消**。原来剧情每天 5 次大额、纯聊天按
# 「当天第几次」递减（3/2/1/1 之后每 3 轮 +1），造成的实际效果是「今天补不完，
# 明天再来」——用户拍板去掉：「没有人喜欢一个限制你 24 小时的游戏」。
# 现在聊天和剧情同权，每轮按模型给的 delta 走，只留 ROUND_CAP 兜底。
# 想再调节奏就改 ROUND_CAP，不要再往回合数上按天加闸。
DOWN_CONFIRM = 2        # 降档要连续 N 轮确认（防阈值边缘反复横跳）
HIST_KEEP = 8           # 每条线留最近 N 条变化记录（给「好感」卡片看）

MODE_RAID = "raid"
MODE_FREE = "free"

# 结局 key → 触发线
ENDING_GRAD = "graduated"
ENDING_JAIL = "jail"

# 「再给我一次机会」（2026-11-04 用户定的）：进局子之后能真的重来一次，
# 但每人只给 MERCY_MAX 次，**按每条角色线各算**。
# 存在的理由不是宽容，是防人跑：只给「清空存档」的话，玩家在进局子那一刻
# 会以为存档要没了，索性不玩了。用完就只剩自由模式（封存）和重新开始。
MERCY_MAX = 3

# ── 默认配置（config 文件缺失/坏掉时的兜底；prompt 留空 = 该档只注入档位名）──
DEFAULT_CONFIG = {
    "min": MIN_V, "max": MAX_V, "initial": INITIAL_V,
    "tiers": [
        {"key": "full",   "min": 40,  "name": "随便凿",   "prompt": ""},
        {"key": "care",   "min": 30,  "name": "凿+关心",  "prompt": "",
         "aftercare": True},
        {"key": "start",  "min": 20,  "name": "开始凿",   "prompt": ""},
        {"key": "touch",  "min": 10,  "name": "肢体接触", "prompt": ""},
        {"key": "normal", "min": 0,   "name": "正常关系", "prompt": None},
        {"key": "miffed", "min": -10, "name": "小反感",   "prompt": ""},
        {"key": "cold",   "min": -20, "name": "爱答不理", "prompt": ""},
        {"key": "forced", "min": -30, "name": "强制爱",   "prompt": ""},
        {"key": "fallen", "min": -50, "name": "堕落模式", "prompt": ""},
    ],
    "endings": {
        "graduated": {"at": 50,  "name": "毕业",   "prompt": ""},
        "jail":      {"at": -50, "name": "进局子", "prompt": ""},
    },
    # 冲动档位兜底（2026-11-04）：给玩家看的是**词**，不是 0~100 的数字。
    # 数字一露，玩家就开始算「再来两杯酒就能过线」——那就不叫气氛了。
    "arousal_levels": [
        {"key": "open",  "min": 75, "name": "不设防", "short": "她这会儿没在拦。"},
        {"key": "messy", "min": 50, "name": "很乱",   "short": "她心里有点乱，话会绕。"},
        {"key": "warm",  "min": 25, "name": "有点乱", "short": "她比刚才软了一点。"},
        {"key": "calm",  "min": 0,  "name": "平静",   "short": "她跟平时一样。"},
    ],
    # 距离层级兜底：config 里没有 intimacy 时用这套中性名，机制不因缺配置停摆
    "intimacy": {"levels": [
        {"key": "l0", "name": "隔得很远",     "gate": 0,  "guard": 50, "prompt": ""},
        {"key": "l1", "name": "点头之交",     "gate": 5,  "guard": 46, "prompt": ""},
        {"key": "l2", "name": "说得上话",     "gate": 12, "guard": 40, "prompt": ""},
        {"key": "l3", "name": "说得进心里",   "gate": 20, "guard": 34, "prompt": ""},
        {"key": "l4", "name": "靠得很近",     "gate": 30, "guard": 25, "prompt": ""},
        {"key": "l5", "name": "不分彼此",     "gate": 40, "guard": 15, "prompt": ""},
    ]},
}


def config_path():
    return os.path.join(str(BASE_DIR), "config", "affection_tiers.json")


def load_config():
    """读档位配置。文件缺失或 JSON 坏了就用默认档位（机制不能因配置挂掉）。"""
    try:
        with open(config_path(), encoding="utf-8") as fh:
            d = json.load(fh)
    except (OSError, ValueError):
        return DEFAULT_CONFIG
    if not isinstance(d, dict) or not isinstance(d.get("tiers"), list):
        return DEFAULT_CONFIG
    return d


def tier_for(value, cfg=None):
    """数值 → 行为档（取 min ≤ value 的最大那一档）。不在区间返回最边缘档。"""
    cfg = cfg or load_config()
    tiers = sorted(cfg.get("tiers") or [], key=lambda t: t.get("min") or 0,
                   reverse=True)
    for t in tiers:
        try:
            lo = int(t.get("min") or 0)
        except (TypeError, ValueError):
            continue
        if value >= lo:
            return t
    return tiers[-1] if tiers else None


def bounds(cfg=None):
    """好感的上下限 `(hi, lo)`。config 可覆盖（越线判定和「退出结局」都用它）。"""
    cfg = cfg or load_config()
    try:
        hi = int(cfg.get("max") if cfg.get("max") is not None else MAX_V)
        lo = int(cfg.get("min") if cfg.get("min") is not None else MIN_V)
    except (TypeError, ValueError):
        hi, lo = MAX_V, MIN_V
    return hi, lo


def ending_for(value, cfg=None):
    """越线判定：≥max 毕业 / ≤min 进局子；没越线返回 None。"""
    hi, lo = bounds(cfg)
    if value >= hi:
        return ENDING_GRAD
    if value <= lo:
        return ENDING_JAIL
    return None


def ending_cfg(key, cfg=None):
    cfg = cfg or load_config()
    return (cfg.get("endings") or {}).get(key) or {}


# ── 距离层级 ──────────────────────────────────────────

def levels_of(cfg=None):
    """六级的配置列表，按 0→5 顺序。配置坏了回退默认表。"""
    cfg = cfg or load_config()
    lst = (cfg.get("intimacy") or {}).get("levels")
    if not isinstance(lst, list) or not lst:
        lst = (DEFAULT_CONFIG.get("intimacy") or {}).get("levels") or []
    return lst


def level_cfg(n, cfg=None):
    """第 n 级（0~5）的配置；越界取边缘。"""
    lst = levels_of(cfg)
    if not lst:
        return {}
    try:
        n = int(n)
    except (TypeError, ValueError):
        n = 0
    return lst[max(0, min(len(lst) - 1, n))]


def level_name(n, cfg=None, card=None):
    """这一级在**这张卡**里叫什么。卡片给了 level_names 就用卡片的
    （骨科卡第 1 级可能叫「妹妹」），没给就用通用名。"""
    names = (card or {}).get("level_names") if isinstance(card, dict) else None
    if isinstance(names, list) and 0 <= int(n or 0) < len(names):
        got = str(names[int(n or 0)] or "").strip()
        if got:
            return got
    return str(level_cfg(n, cfg).get("name") or "")


# ── 冲动档位（2026-11-04）─────────────────────────────

def arousal_levels(cfg=None):
    """冲动四档配置，按 min 从高到低。配置坏了回退默认表。"""
    cfg = cfg or load_config()
    lst = cfg.get("arousal_levels")
    if not isinstance(lst, list) or not lst:
        lst = DEFAULT_CONFIG.get("arousal_levels") or []
    return sorted(lst, key=lambda x: int(x.get("min") or 0), reverse=True)


def arousal_level(v, cfg=None):
    """冲动值 → 它落在哪一档（返回整条配置）。"""
    try:
        v = int(v or 0)
    except (TypeError, ValueError):
        v = 0
    v = max(0, min(AROUSAL_MAX, v))
    for l in arousal_levels(cfg):
        if v >= int(l.get("min") or 0):
            return l
    return {}


def arousal_name(v, cfg=None):
    """冲动值 → 给玩家看的那个词（平静 / 有点乱 / 很乱 / 不设防）。

    只给词不给数字：数字一摆出来，玩家就开始算「再喝两杯能不能过线」，
    那就不叫气氛了，叫刷条。
    """
    return str(arousal_level(v, cfg).get("name") or "")


def card_start(card):
    """角色卡给的起点：{value, level}。没配就从 0 / 第 0 级开始。"""
    card = card if isinstance(card, dict) else {}
    try:
        lv = int(card.get("start_level") or 0)
    except (TypeError, ValueError):
        lv = 0
    lv = max(0, min(LEVEL_MAX, lv))
    gate = int(level_cfg(lv).get("gate") or 0)
    try:
        val = int(card.get("start_value"))
    except (TypeError, ValueError):
        val = gate          # 没给分数：正好卡在这一级的门槛上
    return {"value": max(MIN_V, min(MAX_V, val)), "level": lv}


def _today():
    return datetime.date.today().isoformat()


def _blank(line_key="", value=None):
    """新建一份攻略记录。

    起点**不是**固定的 0：先看角色卡有没有配 `start_level` / `start_value`。
    骨科卡可以一上来就是「妹妹」（第 2 级），不用从陌生人爬。
    """
    card = {}
    if line_key:
        try:
            from app import persona
            card = persona.active_card(line_key) or {}
        except Exception:                                  # noqa: BLE001
            card = {}
    st = card_start(card)
    if value is not None:
        st["value"] = max(MIN_V, min(MAX_V, int(value)))
    t = tier_for(st["value"]) or {}
    return {"mode": MODE_RAID, "value": st["value"], "tier": str(t.get("key") or ""),
            "level": st["level"], "arousal": 0, "cross_n": 0,
            "down": 0, "ending": "", "day": _today(), "chat": 0, "acts": 0,
            "streak": 1, "turns": 0, "ach": [], "hist": [],
            "mercy": MERCY_MAX}


def _entry(line_key):
    """读分线条目（懒导入 persona，避免循环依赖）。

    2026-11-04：老存档（没 level 字段）在这里**迁移一次并落盘**。
    以前只在 `record_of` 里就地补内存，每次读都重算——用户改一次
    `affection_tiers.json` 的 gate，同一条线的层级就跟着漂。
    """
    from app import persona
    ent = persona._entry(line_key)                        # noqa: SLF001
    if not isinstance(ent, dict):
        return None
    rec = ent.get("affection")
    if isinstance(rec, dict) and "level" not in rec:
        _migrate(rec)
        try:
            _save_entry(line_key, ent)
        except Exception:                                 # noqa: BLE001
            # 落盘失败不能挡住这一轮——内存里已经迁好了，下次读再试
            log.warning("老存档迁移落盘失败 %s", line_key)
    return ent


def _save_entry(line_key, ent):
    from app import persona
    state = persona._load_state()                         # noqa: SLF001
    state[line_key] = ent
    persona._save_state(state)                            # noqa: SLF001


def _card_of(line_key):
    """这条线现在演的角色卡（取不到就空字典）。"""
    try:
        from app import persona
        card = persona.active_card(line_key)
        return card if isinstance(card, dict) else {}
    except Exception:                                      # noqa: BLE001
        return {}


def _migrate(rec):
    """老存档（只有 value/tier，没有 level/arousal/ach）就地补齐。

    2026-11-04 加距离层级之前的记录没有这两个字段，直接按 0 算会让老玩家
    一夜之间退回「隔得很远」。这里按现有好感反推一个相符的层级。

    ⚠️ 反推的是「你现在的好感够得上哪一级」，不是「你当年走过哪几级」——
    旧的单轴系统里没有距离这个概念，这个信息本来就不存在，猜不出来。
    """
    if not isinstance(rec, dict):
        return rec
    if "level" not in rec:
        v = int(rec.get("value") or 0)
        lv = 0
        for i, l in enumerate(levels_of()):
            if v >= int(l.get("gate") or 0):
                lv = i
        # 老玩家补偿（2026-11-04 用户拍板）：旧的单轴里根本没有「距离」这个
        # 概念，按好感反推出来的层级天然偏保守——好感 20 的老档在旧系统里
        # 身体早就到了（档位叫「开始凿」），新标尺一上却退回「还隔着一层」，
        # 等于把走过的路抹了。凡是反推出至少走过一级的，一律再补一级（到顶为止）。
        # 宁可让老玩家快一步，也不让他们觉得「我的进度没了」。
        if lv >= 1:
            lv = min(LEVEL_MAX, lv + 1)
            # 但**不许白送到顶格**：「不分彼此」是终点站，靠补偿走到那里
            # 就没有下一步可走了，往后聊什么都只是原地。要顶格得自己够到
            # 最高一级的门票。
            if lv >= LEVEL_MAX and v < int(level_cfg(LEVEL_MAX).get("gate") or 0):
                lv = LEVEL_MAX - 1
        rec["level"] = lv
    if "arousal" not in rec:
        rec["arousal"] = 0
    if "cross_n" not in rec:
        rec["cross_n"] = 0
    # 2026-11-04 加成就与连续天数：老存档没有这两个，按「从今天开始算」补
    if "ach" not in rec or not isinstance(rec.get("ach"), list):
        # 补发：已经走过的层级不能因为「升级系统才做」就没记录。
        # 距离层级只升不降，反推出第 N 级 = 前 N 级都走过，可以确定地补。
        # 档位**不补**——档位会涨会掉，没法确认当年是否真的首次进过。
        rec["ach"] = [{"k": "lv%d" % i, "ts": ""}
                      for i in range(1, int(rec.get("level") or 0) + 1)]
        # 已经定格在结局的线不会再走结算，这两个只能在这里补
        ended = str(rec.get("ending") or "")
        if ended == "graduated":
            rec["ach"].append({"k": "graduate", "ts": ""})
        elif ended == "jail":
            rec["ach"].append({"k": "jail", "ts": ""})
    if "streak" not in rec:
        rec["streak"] = 1
    if "turns" not in rec:
        rec["turns"] = 0
    # 「再给我一次机会」的次数（2026-11-04 加）：老档没有这个字段，
    # 按满额算——不能因为系统后加，就把已经进过局子的人判成「机会用完了」。
    if "mercy" not in rec:
        rec["mercy"] = MERCY_MAX
    return rec


def record_of(ent):
    """条目里的好感记录；没有（自由模式从没开过）返回 None。"""
    if not isinstance(ent, dict):
        return None
    rec = ent.get("affection")
    return _migrate(rec) if isinstance(rec, dict) else None


def is_raid(line_key):
    """这条线现在是否在攻略模式（raid 且有角色线）。"""
    ent = _entry(line_key)
    rec = record_of(ent)
    return bool(rec) and rec.get("mode") == MODE_RAID


def mercy_left(rec):
    """这条线还剩几次「再给我一次机会」。缺字段的老档按满额算。"""
    try:
        n = int(rec.get("mercy", MERCY_MAX))
    except (TypeError, ValueError):
        n = MERCY_MAX
    return max(0, min(MERCY_MAX, n))


def status(line_key):
    """给网页/管理页看的只读快照。没开过攻略返回 None。

    2026-11-04 加冲动与越界次数：网页右栏要画三轴，缺一个就得再请求一次。
    `levelMax` 一起给，前端不用把「5」写死在 JS 里。
    """
    rec = record_of(_entry(line_key))
    if not rec:
        return None
    cfg = load_config()
    value = int(rec.get("value") or 0)
    t = tier_for(value, cfg) or {}
    lv = int(rec.get("level") or 0)
    ar = int(rec.get("arousal") or 0)
    return {
        "mode": str(rec.get("mode") or MODE_FREE),
        "value": value,
        "tier": str(rec.get("tier") or ""),
        "tierName": _tier_view(t, lv).get("name") or "",
        "level": lv,
        "levelMax": LEVEL_MAX,
        "levelName": level_name(lv, cfg, _card_of(line_key)),
        "arousal": ar,
        "arousalName": arousal_name(ar, cfg),
        "arousalShort": str(arousal_level(ar, cfg).get("short") or ""),
        "crossN": int(rec.get("cross_n") or 0),
        "ending": str(rec.get("ending") or ""),
        "mercy": mercy_left(rec),
        "mercyMax": MERCY_MAX,
    }


# ── 模式切换 / 重开 ────────────────────────────────────

def _lift_ending(rec, cfg=None):
    """把结局这一页翻过去：清标记，好感往线内挪**一格**。

    为什么是挪一格而不是拉回起点：结局有两种触发方式——分数越线，或者
    越界次数到顶（`force_ending`）。后者可能停在 -30 这种远没到线的地方，
    把 -30 拉回起点等于抹掉玩家一晚上的账。「挪一格」两种情形都成立：
    进局子 -50 → -49、报警时 -30 → -29、毕业 +50 → +49。
    挪完必须在区间内，不然切回攻略模式的第一轮结算会拿越线值再判一次结局。

    2026-11-04 用户明确：这条和「再给我一次机会」是**同一个效果**，
    区别只在「再给我一次机会」是明面上的按钮、要扣次数。
    返回被解除的结局 key（本来没结局就是空串）。
    """
    if not isinstance(rec, dict):
        return ""
    ended = str(rec.get("ending") or "")
    if not ended:
        return ""
    hi, lo = bounds(cfg)
    v = int(rec.get("value") or 0)
    v += -1 if ended == ENDING_GRAD else 1
    rec["value"] = max(lo + 1, min(hi - 1, v))
    rec["ending"] = ""
    return ended


def set_mode(line_key, raid):
    """开/关攻略模式。返回给用户看的确认话术。

    开：有封存记录就原值解冻，没有就归零新开。需要已在扮演态（有角色线）。
    关：mode 改 free，数值**封存不删**——玩崩了缓一缓还能接着来。
    ⚠️ 首次开启必须在判断「是不是已经开着」**之前**落一次盘——
    `_blank()` 的默认 mode 就是 raid，先落盘再判断，不然第一次永远命中
    「已经是攻略模式」分支，记录根本不落（实测踩过）。
    """
    ent = _entry(line_key)
    if not ent or not str(ent.get("card") or "").strip():
        return None           # 没在演谁：当普通消息，不认
    from app import persona
    existing = record_of(ent)
    if raid:
        if existing is None:
            rec = _blank(line_key)
            ent["affection"] = rec
            _save_entry(line_key, ent)
            lv = int(rec.get("level") or 0)
            if lv > 0:
                return ("攻略模式开启。你们已经到【%s】这一步，从这里接着往下走。"
                        "发「好感」随时查。" % (level_name(lv, card=_card_of(line_key))
                                                or "认识"))
            return ("攻略模式开启，好感从 0 开始。聊天会慢慢涨，"
                    "剧情里的照顾和付出涨得快，把她说烦了会掉。发「好感」随时查。")
        rec = existing
        if rec.get("ending"):
            rec["mode"] = MODE_RAID
            ent["affection"] = rec
            _save_entry(line_key, ent)
            ename = str(ending_cfg(str(rec["ending"]), load_config())
                        .get("name") or "结局")
            return ("这条线还停在结局（%s），数值锁定，她留在那天的情绪里。"
                    "想从那页翻出来接着聊就发「自由模式」，想彻底删档发「清空存档」。"
                    % ename)
        if rec.get("mode") == MODE_RAID:
            return "现在已经是攻略模式了，当前好感 %d。" % int(
                rec.get("value") or 0)
        was = int(rec.get("value") or 0)
        rec["mode"] = MODE_RAID
        ent["affection"] = rec
        _save_entry(line_key, ent)
        return "攻略模式已恢复：好感 %d，接着上次来。" % was
    # → 自由模式：封存
    if existing is None:
        return "现在本来就是自由模式。发「攻略模式」开启好感玩法。"
    rec = existing
    # 结局不是死路（2026-11-04 用户提：报警结局之后想接着聊）。
    # 切自由模式 = 这一页翻过去：结局标记清掉、好感往线内挪一格，她回到平时的样子。
    # 排在 `mode == free` 判断**之前**：已经切成自由的旧线（free + ending=jail）
    # 结局还挂着，就是这个顺序卡住了玩家。
    if rec.get("ending"):
        ended = str(rec["ending"])
        ename = str(ending_cfg(ended, load_config()).get("name") or "结局")
        _lift_ending(rec)
        rec["mode"] = MODE_FREE
        ent["affection"] = rec
        _save_entry(line_key, ent)
        return ("【%s】这一页翻过去了，切回自由模式：她不再按结局那天的样子对你，"
                "数值封存着（好感 %d）。想接着攻略就发「攻略模式」，"
                "想彻底删档重来就发「清空存档」。"
                % (ename, int(rec.get("value") or 0)))
    if rec.get("mode") == MODE_FREE:
        return "现在就是自由模式（好感 %d 封存中）。发「攻略模式」接着玩。" % int(
            rec.get("value") or 0)
    rec["mode"] = MODE_FREE
    ent["affection"] = rec
    _save_entry(line_key, ent)
    return ("已切回自由模式（好感 %d 封存，她不再按档位演戏）。"
            "想接着攻略就发「攻略模式」，好感原样恢复。" % int(
                rec.get("value") or 0))


def wipe_numbers(line_key, keep_mode=True):
    """把好感记录整份换新，重置成**这张卡的起点**，供「清空存档」调用。

    2026-11-04 用户拍板：存档既然是真删，就没有「经历过的事」可言——
    好感、关系层级、结局、成就、连续天数、机会次数（mercy）全部回到初始，
    连 `wipe` 之前攒的勋章一起清掉。

    和改动前的 `restart()` 的区别：那个只删数值、留着成就、聊天记忆也不动。
    玩家删完发现她还记得上周聊的，惩罚感是假的。

    只负责好感这一层；聊天历史与扮演态由 `persona.wipe()` 删。
    返回新记录（`persona` 拿它写进状态），没开过攻略时返回 None。
    """
    ent = _entry(line_key)
    old = record_of(ent)
    if not ent or old is None:
        return None           # 从没开过攻略：当普通消息
    fresh = _blank(line_key)
    if keep_mode and old.get("mode"):
        fresh["mode"] = old["mode"]
    ent["affection"] = fresh
    _save_entry(line_key, ent)
    return fresh


def mercy(line_key):
    """「再给我一次机会」：结局之后明面上的重来按钮，每人 MERCY_MAX 次。

    效果和「切自由模式再切回攻略」**完全一样**——都是 `_lift_ending`：
    结局解除、好感往线内挪一格（-50 → -49，报警时 -30 → -29）。
    区别只有两点：
      - 它留在攻略模式里，点完就能接着玩，不用来回切两次；
      - 它扣次数；那 3 次用完，就只剩自由模式那条不扣次数的路（不对外宣传）。

    用户 2026-11-04 定的：进局子那一刻得有个看得见的出口，不然玩家会以为
    「存档要没了」，索性不玩了。
    """
    ent = _entry(line_key)
    rec = record_of(ent)
    if not ent or rec is None:
        return None           # 从没开过攻略：当普通消息
    if rec.get("mode") != MODE_RAID:
        return "现在是自由模式，没有被结局锁住。想接着攻略发「攻略模式」。"
    if not rec.get("ending"):
        return "现在还没到结局，用不上这个。"
    left = mercy_left(rec)
    if left <= 0:
        return ("三次机会都用完了，这次没有了。发「自由模式」能接着聊（数值封存），"
                "发「清空存档」是把这条线彻底删掉（会要你二次确认）。")
    _lift_ending(rec)
    rec["cross_n"] = 0        # 案子了结：再犯一次不会立刻又报警
    rec["arousal"] = 0
    rec["mercy"] = left - 1
    ent["affection"] = rec
    _save_entry(line_key, ent)
    return ("她留下了，接着攻略：好感 %d。这条机会还剩 %d 次。"
            % (int(rec.get("value") or 0), left - 1))


# ── 结算 ──────────────────────────────────────────────

def scoring_ctx(line_key):
    """给第一段打分提示词的「当前关系」上下文。

    返回 dict：value / tier（档位名）/ recent（最近几条变化，拼好的多行文本）。
    没在攻略时返回 None（调用方就不用打分版提示词）。
    """
    rec = record_of(_entry(line_key))
    if not rec or rec.get("mode") != MODE_RAID:
        return None
    cfg = load_config()
    value = int(rec.get("value") or 0)
    recent_rows = []
    hist = rec.get("hist") if isinstance(rec.get("hist"), list) else []
    for h in reversed(hist[-3:]):
        recent_rows.append("· %s（%+d）" % (h.get("r") or "", int(h.get("d") or 0)))
    recent = ("最近发生：\n" + "\n".join(recent_rows)) if recent_rows \
        else "（你们还没什么交集）"
    lv = int(rec.get("level") or 0)
    tv = _tier_view(tier_for(value, cfg), lv)
    return {"value": value,
            "tier": tv["name"] or "?",
            "level": level_name(lv, cfg, _card_of(line_key)) or "?",
            "levelN": lv,
            "arousal": int(rec.get("arousal") or 0),
            "recent": recent}


def settle(line_key, score):
    """把第一段的打分结算进状态。返回结果 dict（调用方拼注入和尾行）。

    `score`：第一段模型输出的 {"delta": int, "turn_type": "...", "reason": "..."}。
    没交作业 / 解析失败时传 None——按 +0 处理，**不让整轮回不了话**。
    """
    ent = _entry(line_key)
    rec = record_of(ent)
    if not ent or not rec or rec.get("mode") != MODE_RAID:
        # **mode 必须带上**：网页端拿这个回包直接渲染开关状态，
        # 缺了就会被判成「自由模式」——聊天一轮后控件自己跳到自由模式
        # （2026-11-03 群友实测：好感 +20 时突然变自由模式，其实数值没变）。
        return {"applied": 0, "active": False, "mode": MODE_FREE}
    value = int(rec.get("value") or 0)
    cfg = load_config()

    # 结局已触发：数值锁定，不再结算
    if rec.get("ending"):
        return {"applied": 0, "active": True, "mode": MODE_RAID, "value": value,
                "tier": rec.get("tier") or "",
                "tierName": str((tier_for(value, cfg) or {}).get("name") or ""),
                "ending": str(rec.get("ending")), "just_ended": False,
                "mercy": mercy_left(rec), "mercyMax": MERCY_MAX,
                "reason": ""}

    # 跨天：把隔夜会散掉的东西散掉。**不再有额度要重置**——聊天和剧情
    # 都不按天算次数（2026-11-04 晚），这里只管气氛和警惕。
    if str(rec.get("day") or "") != _today():
        prev_day = str(rec.get("day") or "")
        rec["day"] = _today()
        rec["cross_n"] = max(0, int(rec.get("cross_n") or 0) - 1)
        rec["arousal"] = 0
        # 连续陪伴（2026-11-04）：昨天刚来过就 +1，断过就从 1 重算。
        # 首聊当天不算跨天（day 就是今天），streak 在 `_blank` 里已经是 1。
        yday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
        rec["streak"] = (int(rec.get("streak") or 0) + 1) if prev_day == yday else 1
    rec["turns"] = int(rec.get("turns") or 0) + 1

    score = score if isinstance(score, dict) else {}
    try:
        delta = int(score.get("delta") or 0)
    except (TypeError, ValueError):
        delta = 0
    delta = max(-ROUND_CAP, min(ROUND_CAP, delta))
    ttype = str(score.get("turn_type") or "chat").strip().lower()
    reason = str(score.get("reason") or "").strip()[:40]

    # 聊天和剧情同权（2026-11-04 晚）：不再按「当天第几次」打折，也不再限制
    # 剧情型每天几次大额。单轮只受 ROUND_CAP 约束——那是防一句话刷满，
    # 不是时间闸。`ttype` 仍然带下来，只用于记录和展示，不参与额度计算。
    applied = delta

    prev_value = value
    value = max(MIN_V, min(MAX_V, value + applied))
    applied = value - prev_value      # 夹到边缘后以实际变动为准

    # ── 越界与冲动（2026-11-04）────────────────────────
    # 判定权在第一段模型：它说越界了就是越界了，说她不答应就是不答应。
    # 代码只按「越界了几次」施加代价——**不冻结、不降级**，扣完自己哄回来。
    cross = str(score.get("cross") or "").strip().lower()
    if cross not in ("mild", "hard"):
        cross = ""
    allow = str(score.get("allow") or "").strip().lower()

    # 冲动是一次性的机会：本轮场景给的增量 + 回合结束回落
    try:
        arose = int(score.get("arousal") or 0)
    except (TypeError, ValueError):
        arose = 0
    arose = max(-60, min(60, arose))
    rec["arousal"] = max(0, min(AROUSAL_MAX,
                                 int(rec.get("arousal") or 0) + arose - AROUSAL_DECAY))

    force_ending = ""
    if cross:
        if cross == "hard":
            rec["cross_n"] = int(rec.get("cross_n") or 0) + 1
            extra = -CROSS_HARD
        elif allow == "no":
            n = int(rec.get("cross_n") or 0)
            extra = -CROSS_PENALTY[min(n, len(CROSS_PENALTY) - 1)]
            rec["cross_n"] = n + 1
        else:
            extra = 0             # 半推半就 / 她答应了：这一步不罚
        if extra:
            prev_value = value
            value = max(MIN_V, min(MAX_V, value + extra))
            applied += value - prev_value
        if cross == "hard" and int(rec.get("cross_n") or 0) >= CROSS_JAIL_AT:
            force_ending = ENDING_JAIL

    # ── 距离升级 ─────────────────────────────────────
    # 升一级要两个条件：模型判定「这轮做成了那件事」+ 好感够这一级的门票。
    # 缺一个都不升，**只升不降**（身体和记忆不会因为分数掉回去而复原）。
    cfg_levels = levels_of(cfg)
    lv = int(rec.get("level") or 0)
    rose = 0
    if bool(score.get("beat")) and lv < LEVEL_MAX:
        nxt = lv + 1
        gate = int(level_cfg(nxt, cfg).get("gate") or 0)
        if value >= gate:
            lv = nxt
            rose = nxt
            rec["level"] = lv

    # 越线结局：一次就触发
    just_ended = ""
    ending = force_ending or ending_for(value, cfg)
    if ending:
        rec["ending"] = ending
        just_ended = ending

    # 档位：升档立即，降档连续 DOWN_CONFIRM 轮才认
    cur_key = str(rec.get("tier") or "")
    new_t = tier_for(value, cfg) or {}
    changed = False
    if new_t.get("key") != cur_key:
        cur_t = next((t for t in (cfg.get("tiers") or [])
                      if t.get("key") == cur_key), None)
        cur_min = int(cur_t.get("min") or 0) if cur_t else 0
        new_min = int(new_t.get("min") or 0)
        if new_min > cur_min:                 # 升档（或升回来）：立即
            rec["tier"] = str(new_t.get("key") or "")
            rec["down"] = 0
            changed = True
        else:                                 # 掉档：要连续确认
            rec["down"] = int(rec.get("down") or 0) + 1
            if rec["down"] >= DOWN_CONFIRM:
                rec["tier"] = str(new_t.get("key") or "")
                rec["down"] = 0
                changed = True
    else:
        rec["down"] = 0

    hist = rec.get("hist") if isinstance(rec.get("hist"), list) else []
    hist.append({"d": applied, "r": reason,
                 "ts": datetime.datetime.now().strftime("%m-%d %H:%M")})
    rec["hist"] = hist[-HIST_KEEP:]
    rec["value"] = value

    # 对外报**生效档位**（滞回扣着的那个），不是按值现算的——不然注入和
    # 尾行会先一步换档，玩家看到她态度跳来跳去。
    eff_key = str(rec.get("tier") or "")
    eff_t = next((t for t in (cfg.get("tiers") or [])
                  if t.get("key") == eff_key), None) or new_t
    tv = _tier_view(eff_t, lv)
    res = {"applied": applied, "active": True, "mode": MODE_RAID, "value": value,
           "tier": eff_key, "changed": changed,
           "tierName": tv["name"] or str(eff_t.get("name") or ""),
           "level": lv, "levelMax": LEVEL_MAX,
           "levelName": level_name(lv, cfg, _card_of(line_key)),
           "rose": rose, "arousal": int(rec.get("arousal") or 0),
           "arousalName": arousal_name(rec.get("arousal"), cfg),
           "cross": cross, "allow": allow, "crossN": int(rec.get("cross_n") or 0),
           "ending": str(rec.get("ending") or ""),
           "mercy": mercy_left(rec), "mercyMax": MERCY_MAX,
           "just_ended": just_ended, "reason": reason}

    # 成就（2026-11-04）：只认确定性事实，判定在 `achievements` 里。
    # 放在最后——升级、结局、越界次数这一刻才都定下来了。
    from app import achievements
    fresh = achievements.check(rec, res, cfg, _card_of(line_key))
    if fresh:
        achievements.mark(rec, fresh)
        res["ach"] = fresh

    ent["affection"] = rec
    _save_entry(line_key, ent)
    return res


# ── 注入与展示 ────────────────────────────────────────

# 场外能翻到多深（2026-11-03）：给主模型一个**事实**——她现在让你看到哪一层。
# 代码只报事实，写不写、写多深、翻到什么，全交给模型判断。
# 2026-11-04：从「绑好感档位」改成「绑距离层级」。以前好感一过 20 就说
# 「几乎不再藏了」，可她连你的手都没碰过——场外那段当场穿帮。
_DEPTH_BY_LEVEL = {0: "low", 1: "low", 2: "mid", 3: "mid", 4: "high", 5: "high"}
# 老调用（传档位 key）的映射，保留兼容
_DEPTH_LEVEL = {
    "miffed": "low", "cold": "low", "forced": "low", "fallen": "low",
    "normal": "mid", "touch": "mid",
    "start": "high", "care": "high", "full": "high",
}
_DEPTH_TEXT = {
    "low": "她**不打算让你看到什么**——你能翻到的只有她无所谓的东西"
           "（外卖单、没关的屏幕、随手扔的衣服）。往深里翻，她会当成冒犯。",
    "mid": "她**允许你看到一部分**——衣柜、书桌、写了一半的东西会自然落进"
           "你眼里；真正私密的还锁着。",
    "high": "她**几乎不再藏了**——速写本、旧照片、她偷偷留着的东西，"
            "都摆在你翻得到的地方。",
}


def depth_hint(level):
    """按**距离层级**给出「她现在让你看到多深」。未知返回空串——
    多注入一句反而会被模型当成硬规则。"""
    if isinstance(level, str) and not level.strip().isdigit():
        return _DEPTH_TEXT.get(_DEPTH_LEVEL.get(level.strip(), ""), "")
    try:
        lv = int(level)
    except (TypeError, ValueError):
        return ""
    return _DEPTH_TEXT.get(_DEPTH_BY_LEVEL.get(lv, ""), "")


def _tier_view(t, level):
    """这一档在你现在这个距离上该怎么念。

    前四档的原文案写的是**身体进度**（「界限已经松了」「开始凿」），
    那是距离 4~5 级才成立的画面。距离还没到的时候只有感情，
    用 `prompt_soft`（用户随时可以改），没有 soft 版就退回原文。
    """
    t = t if isinstance(t, dict) else {}
    try:
        lv = int(level or 0)
    except (TypeError, ValueError):
        lv = 0
    if lv >= 4:
        return {"name": str(t.get("name") or ""),
                "prompt": str(t.get("prompt") or "")}
    soft_name = str(t.get("name_soft") or "").strip()
    soft_txt = str(t.get("prompt_soft") or "").strip()
    return {"name": soft_name or str(t.get("name") or ""),
            "prompt": soft_txt or str(t.get("prompt") or "")}


def _can_cross(value, level, arousal, cfg=None):
    """按「防线 − 冲动加成」算：以你们现在的距离，她接不接得住。

    这只是一个**概率性事实**，不是命令——所以注入里永远写「可能」，不写「一定」。
    """
    guard = int(level_cfg(level, cfg).get("guard") or 0)
    return value >= guard - int(arousal or 0) // 5


def injection_block(res, cfg=None):
    """主对话（第二段）系统头里追加的那段。自由模式返回空串。"""
    if not isinstance(res, dict) or not res.get("active"):
        return ""
    cfg = cfg or load_config()
    lines = ["## 你们现在到这一步了（系统按状态结算的事实，照这个演，不要自己改）", ""]
    value = int(res.get("value") or 0)
    if res.get("ending") and not res.get("just_ended"):
        # 结局已定格（切模式回来接着演的线）：维持结局情绪，不再结算
        ec = ending_cfg(res["ending"], cfg)
        lines.append("剧情已经停在结局【%s】，关系不会再变。" % (ec.get("name") or ""))
        txt = str(ec.get("prompt") or "").strip()
        if txt:
            lines += ["", txt]
        return "\n".join(lines)
    if res.get("just_ended"):
        ec = ending_cfg(res["just_ended"], cfg)
        lines.append("**剧情到达结局：【%s】**" % (ec.get("name") or ""))
        txt = str(ec.get("prompt") or "").strip()
        if txt:
            lines += ["", txt]
        lines += ["", "这是收场回合：把这段结局剧情演完、收住。"]
        return "\n".join(lines)

    lv = int(res.get("level") or 0)
    # 不报好感数字：好感和「距离」本来就可以不同步（好感够了但没做成事，
    # 你们还是原来那一步）。两个数字一起摆出来，模型会纠结该信哪个。
    lines.append("关系：【%s】  她对你的感觉：【%s】"
                 % (res.get("levelName") or "?", res.get("tierName") or "?"))
    lp = str(level_cfg(lv, cfg).get("prompt") or "").strip()
    if lp:
        lines += ["", lp]

    # 档位文案按**生效档位**取（res["tier"]），不能用 tier_for(value)——
    # 降档滞回期间两者不一致，会提前把新档位的语气漏给模型
    t = next((x for x in (cfg.get("tiers") or [])
              if x.get("key") == res.get("tier")), None) or tier_for(value, cfg) or {}
    tv = _tier_view(t, lv)
    txt = tv["prompt"].strip()
    if txt:
        lines += ["", txt]
    hint = depth_hint(lv)
    if hint:
        lines += ["", "（她现在让你看到多深：%s）" % hint]

    # 这一刻的气氛：冲动决定「她此刻想不想」，防线决定「接不接得住」
    ar = int(res.get("arousal") or 0)
    can = _can_cross(value, lv, ar, cfg)
    if ar >= 65 and can:
        lines += ["", "（她此刻几乎没在设防，而且以你们现在的距离，这一点她也清楚。"
                      "这个状态下的她可能主动——先开口、先靠近、先留下你。）"]
    elif ar >= 65:
        lines += ["", "（她此刻很乱，但你们之间的距离还撑不住这个。她会被自己的反应吓到，"
                      "然后退回去。别把她的乱当成同意。）"]
    elif ar >= 35:
        lines += ["", "（她此刻有点乱。她不会干脆说不，也不会干脆答应，她会绕。）"]
    elif can:
        lines += ["", "（她此刻是清醒的，但以你们现在的距离，她挡不住太认真的一句话。）"]
    else:
        lines += ["", "（她此刻是清醒的。这个状态下越界，她挡得住，也说得出口。）"]

    if res.get("cross"):
        if res.get("cross") == "hard":
            lines += ["", "（她刚才明确表示不愿意，你还是继续了。她现在的反应不是害羞，"
                          "是怕，或者恼。）"]
        elif str(res.get("allow") or "") == "no":
            lines += ["", "（她拒绝了。这一轮她不会松口——除非你先退回到安全的位置。）"]
        else:
            lines += ["", "（她没有明确答应，也没有走开。这个状态最难看也最好看。）"]

    # 一条硬约束：不许每轮都给干脆答案
    lines += ["", "（怎么反应由你演，但**别每轮都给干脆的答案**：她可以装没听懂、可以反问、"
                  "可以嘴上说不要但没躲、可以答应完自己后悔。真实的犹豫比干脆的拒绝好看。）"]

    if res.get("rose"):
        lines += ["", "（你们的关系刚刚往前跨了一步，到了【%s】。这一步要有实感——"
                      "她的某个反应、某句话，得和之前不一样。）"
                  % (res.get("levelName") or "")]
    if res.get("changed"):
        lines += ["", "（你们的关系刚刚跨进【%s】这一档——这一轮开始，"
                      "她对你的态度要和之前**明显不一样**，转折要自然。）"
                  % (res.get("tierName") or "")]
    reason = str(res.get("reason") or "").strip()
    applied = int(res.get("applied") or 0)
    if applied and reason:
        lines += ["", "（刚才那件事：%s。）" % reason]
    return "\n".join(lines)


def tail_line(res):
    """QQ 回复尾部追加的状态块。

    2026-11-04 早先的版本**不报数字**（怕玩家算账）；当天晚些时候用户拍板
    反过来要数字：「看到数值浮动才有成就感」。所以数值**一行一个**，
    正数不带加号、负数才带负号。

    格式（当日三次修订）：`第 3 级 / 共 5 级` 也是行话——玩家不懂「级」是量什么，
    要的是能直接读懂的词。关系行只留**关系叫什么**，进度靠叫法本身会变
    （点头之交 → 说得上话 → 靠得很近），不需要数字：

        ❤ 关系 家里的人
        气氛 平静
        好感 21
        冲动 0

    越界那一轮气氛让位给「她退开了」——那轮的信号是「她退开了」，不是气氛；
    数字照给，因为这一轮的掉分正是要看的东西。
    """
    if not isinstance(res, dict) or not res.get("active"):
        return ""
    ending = str(res.get("ending") or "")
    if ending:
        mark = {ENDING_GRAD: "🎓", ENDING_JAIL: "🚨"}.get(ending, "❤")
        name = (ending_cfg(ending, load_config()) or {}).get("name") or "结局"
        return "%s 已结局【%s】" % (mark, name)
    lname = res.get("levelName") or level_name(int(res.get("level") or 0)) or "?"
    head = "❤ 关系 %s" % lname
    if res.get("rose"):
        head += " ↑"
    # 第二行：气氛。越界那一轮不是重点——那轮要说的是「她退开了」。
    if res.get("cross") == "hard":
        mood = "她退开了"
    else:
        ar = arousal_name(res.get("arousal") or 0)
        mood = ("气氛 " + ar) if ar else ""
    if int(res.get("crossN") or 0) > 0:
        part = "被拒 %d 次" % int(res.get("crossN") or 0)
        mood = (mood + " · " + part) if mood else part
    rows = [head]
    if mood:
        rows.append(mood)
    rows.append("好感 %d" % int(res.get("value") or 0))
    rows.append("冲动 %d" % int(res.get("arousal") or 0))
    return "\n".join(rows)


def detail_nodes(line_key):
    """「好感」指令的聊天记录卡片：档案页 + 最近变化页。没开过返回 None。"""
    ent = _entry(line_key)
    rec = record_of(ent)
    if not rec:
        return None
    from app import persona
    cfg = load_config()
    card = persona.active_card(line_key) or {}
    name = card.get("name") or "她"
    value = int(rec.get("value") or 0)
    mode = str(rec.get("mode") or MODE_FREE)
    ending = str(rec.get("ending") or "")

    head = ["❤ 攻略档案 · %s" % name, ""]
    if mode == MODE_FREE:
        head.append("状态：自由模式（进度封存中）")
    elif ending:
        head.append("状态：已结局【%s】" % (ending_cfg(ending, cfg).get("name") or ""))
    else:
        head.append("状态：攻略中")
    lv = int(rec.get("level") or 0)
    # 只写关系叫什么（2026-11-04 三次修订：「第 3 级 / 共 5 级」是行话，
    # 玩家看不懂级是在量什么。叫法本身就会随关系往前而变）。
    head.append("你们：%s" % (level_name(lv, cfg, card) or "?"))
    head.append("她对你的感觉：%s"
                % (_tier_view(tier_for(value, cfg), lv).get("name") or "?"))
    # 气氛（2026-11-04）：冲动是一次性的，查档案这一刻看到的就是「此刻」。
    # 卡片第一页给词，第二页不重复——重复就变成刷状态了。
    ar = int(rec.get("arousal") or 0)
    an = arousal_name(ar, cfg)
    if an:
        head.append("此刻的气氛：%s" % an)
    if int(rec.get("cross_n") or 0) > 0:
        head.append("越界被拒：%d 次" % int(rec.get("cross_n") or 0))
    if not ending:
        head.append("好感 %d" % value)
        if lv < LEVEL_MAX:
            head += ["", "再往前一步不靠攒分——得有一件能让她愿意往前走的事。"]
        else:
            head += ["", "她已经不设防了。剩下的只看这一刻的气氛。"]
    page1 = "\n".join(head)

    hist = rec.get("hist") if isinstance(rec.get("hist"), list) else []
    rows = ["📋 最近的变化", ""]
    if hist:
        for h in reversed(hist[-6:]):
            rows.append("%s  %+d  %s" % (h.get("ts") or "", int(h.get("d") or 0),
                                         h.get("r") or ""))
    else:
        rows.append("还没有记录，多陪她聊聊？")
    # 成就（2026-11-04）：只在解到过东西时追问一句，没解到不提——
    # 每次都写「成就 0/12」等于提醒玩家「你还没解锁」，是负担不是奖励。
    got = [a for a in (rec.get("ach") or []) if isinstance(a, dict)]
    if got:
        from app import achievements
        rows += ["", "🏅 成就 %d 个（发「成就」看全部）" % len(got)]
        for a in got[-3:]:
            # 名字要跟着这张卡的叫法走——同名成就换张卡就不一样了
            rows.append("%s %s" % (achievements.icon(a.get("k")),
                                   achievements.name(a.get("k"), cfg, card, lv)))
    page2 = "\n".join(rows)
    return [page1, page2]


def detail_text(line_key):
    """「好感」指令的纯文本兜底（协议端发不了卡片时用）。没开过返回 None。"""
    nodes = detail_nodes(line_key)
    if nodes is None:
        return None
    return "\n\n".join(nodes)


def achievement_nodes(line_key):
    """「成就」指令的卡片。没开过攻略返回 None（当普通消息）。"""
    rec = record_of(_entry(line_key))
    if not rec:
        return None
    from app import achievements
    return [achievements.nodes(rec, load_config(), _card_of(line_key))]


def achievement_text(line_key):
    """「成就」的纯文本兜底。"""
    nodes = achievement_nodes(line_key)
    if nodes is None:
        return None
    return "\n\n".join(nodes)
