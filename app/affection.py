"""攻略模式：好感度数值层（2026-11-03）。

## 它是什么

自由模式（现状）完全不经由这个模块。攻略模式下，两段式扮演的第一段
（意图判断）会顺带输出一个打分 JSON，这里负责把分数**结算**成分线状态里
持久的好感值，并按档位给出要注入主对话的提示词。

设计原则（和整个项目一致）：

  - **判断交给 AI**：这轮算多少分、算聊天还是剧情，由第一段模型斟酌；
  - **代码只做检测和记账**：夹上限、每日递减、降档滞回、越线判结局；
  - 档位**文案**全部来自 `config/affection_tiers.json`（用户自己配），
    机制层只认 key / 区间 / aftercare 标记，不掺和内容。

## 数值与档位

好感 -50 ~ +50，初始 0。9 个行为档 + 2 个结局（毕业 / 进局子）。
结局**越线即触发**（≥50 / ≤-50），触发后数值锁定，直到「重新开始」
（归零）或切回自由模式（封存，再切回来原值恢复）。

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
ACTION_DAILY = 5        # 每天剧情(action)型大额加分的次数上限，超了按聊天算
CHAT_SCALE = (3, 2, 1, 1)   # 纯聊天：当天第 1~4 次的加分上限，之后为 0
DOWN_CONFIRM = 2        # 降档要连续 N 轮确认（防阈值边缘反复横跳）
HIST_KEEP = 8           # 每条线留最近 N 条变化记录（给「好感」卡片看）

MODE_RAID = "raid"
MODE_FREE = "free"

# 结局 key → 触发线
ENDING_GRAD = "graduated"
ENDING_JAIL = "jail"

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


def ending_for(value, cfg=None):
    """越线判定：≥max 毕业 / ≤min 进局子；没越线返回 None。"""
    cfg = cfg or load_config()
    try:
        hi = int(cfg.get("max") if cfg.get("max") is not None else MAX_V)
        lo = int(cfg.get("min") if cfg.get("min") is not None else MIN_V)
    except (TypeError, ValueError):
        hi, lo = MAX_V, MIN_V
    if value >= hi:
        return ENDING_GRAD
    if value <= lo:
        return ENDING_JAIL
    return None


def ending_cfg(key, cfg=None):
    cfg = cfg or load_config()
    return (cfg.get("endings") or {}).get(key) or {}


def _today():
    return datetime.date.today().isoformat()


def _blank(value=INITIAL_V):
    t = tier_for(value) or {}
    return {"mode": MODE_RAID, "value": value, "tier": str(t.get("key") or ""),
            "down": 0, "ending": "", "day": _today(), "chat": 0, "acts": 0,
            "hist": []}


def _entry(line_key):
    """读分线条目（懒导入 persona，避免循环依赖）。"""
    from app import persona
    ent = persona._entry(line_key)                        # noqa: SLF001
    return ent if isinstance(ent, dict) else None


def _save_entry(line_key, ent):
    from app import persona
    state = persona._load_state()                         # noqa: SLF001
    state[line_key] = ent
    persona._save_state(state)                            # noqa: SLF001


def record_of(ent):
    """条目里的好感记录；没有（自由模式从没开过）返回 None。"""
    if not isinstance(ent, dict):
        return None
    rec = ent.get("affection")
    return rec if isinstance(rec, dict) else None


def is_raid(line_key):
    """这条线现在是否在攻略模式（raid 且有角色线）。"""
    ent = _entry(line_key)
    rec = record_of(ent)
    return bool(rec) and rec.get("mode") == MODE_RAID


def status(line_key):
    """给网页/管理页看的只读快照。没开过攻略返回 None。"""
    rec = record_of(_entry(line_key))
    if not rec:
        return None
    cfg = load_config()
    t = tier_for(int(rec.get("value") or 0), cfg) or {}
    return {
        "mode": str(rec.get("mode") or MODE_FREE),
        "value": int(rec.get("value") or 0),
        "tier": str(rec.get("tier") or ""),
        "tierName": str(t.get("name") or ""),
        "ending": str(rec.get("ending") or ""),
    }


# ── 模式切换 / 重开 ────────────────────────────────────

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
            ent["affection"] = _blank()
            _save_entry(line_key, ent)
            return ("攻略模式开启，好感从 0 开始。聊天会慢慢涨，"
                    "剧情里的照顾和付出涨得快，把她说烦了会掉。发「好感」随时查。")
        rec = existing
        if rec.get("ending"):
            rec["mode"] = MODE_RAID
            ent["affection"] = rec
            _save_entry(line_key, ent)
            ename = str(ending_cfg(str(rec["ending"]), load_config())
                        .get("name") or "结局")
            return ("还接着上次的结局（%s）：数值已锁定，她停在那天的情绪里。"
                    "想重新攻略就发「重新开始」。" % ename)
        if rec.get("mode") == MODE_RAID:
            return "现在已经是攻略模式了，当前好感 %+d。" % int(
                rec.get("value") or 0)
        was = int(rec.get("value") or 0)
        rec["mode"] = MODE_RAID
        ent["affection"] = rec
        _save_entry(line_key, ent)
        return "攻略模式已恢复：好感 %+d，接着上次来。" % was
    # → 自由模式：封存
    if existing is None:
        return "现在本来就是自由模式。发「攻略模式」开启好感玩法。"
    rec = existing
    if rec.get("mode") == MODE_FREE:
        return "现在就是自由模式（好感 %+d 封存中）。发「攻略模式」接着玩。" % int(
            rec.get("value") or 0)
    rec["mode"] = MODE_FREE
    ent["affection"] = rec
    _save_entry(line_key, ent)
    return ("已切回自由模式（好感 %+d 封存，她不再按档位演戏）。"
            "想接着攻略就发「攻略模式」，好感原样恢复。" % int(
                rec.get("value") or 0))


def restart(line_key):
    """「重新开始」：数值归零、变化记录清空、结局解除；**聊天记忆保留**。"""
    ent = _entry(line_key)
    if not ent or record_of(ent) is None:
        return None           # 从没开过攻略：当普通消息
    ent["affection"] = _blank()
    _save_entry(line_key, ent)
    return ("好感已归零，重新攻略。之前聊过的她还记得，"
            "但你们的关系从「初识」再来一遍。")


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
    return {"value": value,
            "tier": str((tier_for(value, cfg) or {}).get("name") or "?"),
            "recent": recent}


def _chat_allowance(rec):
    """纯聊天这轮还能加多少（按当天第几次）。用完就是 0。"""
    n = int(rec.get("chat") or 0)
    if n < len(CHAT_SCALE):
        return CHAT_SCALE[n]
    return 0


def settle(line_key, score):
    """把第一段的打分结算进状态。返回结果 dict（调用方拼注入和尾行）。

    `score`：第一段模型输出的 {"delta": int, "turn_type": "...", "reason": "..."}。
    没交作业 / 解析失败时传 None——按 +0 处理，**不让整轮回不了话**。
    """
    ent = _entry(line_key)
    rec = record_of(ent)
    if not ent or not rec or rec.get("mode") != MODE_RAID:
        return {"applied": 0, "active": False}
    value = int(rec.get("value") or 0)
    cfg = load_config()

    # 结局已触发：数值锁定，不再结算
    if rec.get("ending"):
        return {"applied": 0, "active": True, "value": value,
                "tier": rec.get("tier") or "",
                "tierName": str((tier_for(value, cfg) or {}).get("name") or ""),
                "ending": str(rec.get("ending")), "just_ended": False,
                "reason": ""}

    # 跨天：聊天/剧情计数清零
    if str(rec.get("day") or "") != _today():
        rec["day"] = _today()
        rec["chat"] = 0
        rec["acts"] = 0

    score = score if isinstance(score, dict) else {}
    try:
        delta = int(score.get("delta") or 0)
    except (TypeError, ValueError):
        delta = 0
    delta = max(-ROUND_CAP, min(ROUND_CAP, delta))
    ttype = str(score.get("turn_type") or "chat").strip().lower()
    reason = str(score.get("reason") or "").strip()[:40]

    # 防刷分：纯聊天每日递减；剧情型每天前 ACTION_DAILY 次不吃递减，超了同聊天
    # （额度按**加之前的次数**取——先加再取会整体错位一档）
    if ttype == "action" and int(rec.get("acts") or 0) < ACTION_DAILY:
        rec["acts"] = int(rec.get("acts") or 0) + 1
        applied = delta
    else:
        if ttype == "action":
            rec["acts"] = int(rec.get("acts") or 0) + 1
        if delta > 0:
            applied = min(delta, _chat_allowance(rec))
            rec["chat"] = int(rec.get("chat") or 0) + 1
        else:
            applied = delta           # 扣分不受递减管

    prev_value = value
    value = max(MIN_V, min(MAX_V, value + applied))
    applied = value - prev_value      # 夹到边缘后以实际变动为准

    # 越线结局：一次就触发
    just_ended = ""
    ending = ending_for(value, cfg)
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
    ent["affection"] = rec
    _save_entry(line_key, ent)

    # 对外报**生效档位**（滞回扣着的那个），不是按值现算的——不然注入和
    # 尾行会先一步换档，玩家看到她态度跳来跳去。
    eff_key = str(rec.get("tier") or "")
    eff_t = next((t for t in (cfg.get("tiers") or [])
                  if t.get("key") == eff_key), None) or new_t
    return {"applied": applied, "active": True, "value": value,
            "tier": eff_key, "changed": changed,
            "tierName": str(eff_t.get("name") or ""),
            "ending": str(rec.get("ending") or ""),
            "just_ended": just_ended, "reason": reason}


# ── 注入与展示 ────────────────────────────────────────

# 场外能翻到多深（2026-11-03）：给主模型一个**事实**——她现在让你看到哪一层。
# 代码只报事实，写不写、写多深、翻到什么，全交给模型判断。
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


def depth_hint(tier_key):
    """按**生效档位**给出「她现在让你看到多深」。未知档位返回空串——
    多注入一句反而会被模型当成硬规则。"""
    return _DEPTH_TEXT.get(_DEPTH_LEVEL.get(str(tier_key or ""), ""), "")


def injection_block(res, cfg=None):
    """主对话（第二段）系统头里追加的那段。自由模式返回空串。"""
    if not isinstance(res, dict) or not res.get("active"):
        return ""
    cfg = cfg or load_config()
    lines = ["## 你们现在的关系到这一步了（系统按好感结算，照这个状态演）", ""]
    value = int(res.get("value") or 0)
    if res.get("ending") and not res.get("just_ended"):
        # 结局已定格（切模式回来接着演的线）：维持结局情绪，不再结算
        ec = ending_cfg(res["ending"], cfg)
        lines.append("好感 %+d —— 剧情已经停在结局【%s】，关系不会再变。"
                     % (value, ec.get("name") or ""))
        txt = str(ec.get("prompt") or "").strip()
        if txt:
            lines += ["", txt]
        return "\n".join(lines)
    if res.get("just_ended"):
        ec = ending_cfg(res["just_ended"], cfg)
        lines.append("好感 %+d —— **剧情到达结局：【%s】**" % (value, ec.get("name") or ""))
        txt = str(ec.get("prompt") or "").strip()
        if txt:
            lines += ["", txt]
        lines += ["", "这是收场回合：把这段结局剧情演完、收住。"]
        return "\n".join(lines)
    lines.append("好感 %+d —— 当前阶段：【%s】"
                 % (value, res.get("tierName") or "?"))
    # 档位文案按**生效档位**取（res["tier"]），不能用 tier_for(value)——
    # 降档滞回期间两者不一致，会提前把新档位的语气漏给模型
    t = next((x for x in (cfg.get("tiers") or [])
              if x.get("key") == res.get("tier")), None) or tier_for(value, cfg) or {}
    txt = str(t.get("prompt") or "").strip()
    if txt:
        lines += ["", txt]
    hint = depth_hint(res.get("tier"))
    if hint:
        lines += ["", "（她现在让你看到多深：%s）" % hint]
    if res.get("changed"):
        # 跨档回合：让她语气有明显体感，不然数值在后台动、玩家感知不到
        lines += ["", "（你们的关系刚刚跨进【%s】这一档——这一轮开始，"
                      "她对你的态度要和之前**明显不一样**，转折要自然。）"
                  % (res.get("tierName") or "")]
    reason = str(res.get("reason") or "").strip()
    applied = int(res.get("applied") or 0)
    if applied and reason:
        lines += ["", "（刚才那件事：%s —— 好感 %+d。）" % (reason, applied)]
    return "\n".join(lines)


def tail_line(res):
    """QQ 回复尾部追加的那行状态。没在攻略 / 这轮没结算就空串。

    格式（2026-11-03 调整）：总数在前、本轮在后——原来的
    「【档位】（好感 +6）」容易被读成本轮加了 6，玩家问了才知道是总数。
    """
    if not isinstance(res, dict) or not res.get("active"):
        return ""
    value = int(res.get("value") or 0)
    ending = str(res.get("ending") or "")
    if ending:
        mark = {ENDING_GRAD: "🎓", ENDING_JAIL: "🚨"}.get(ending, "❤")
        name = (ending_cfg(ending, load_config()) or {}).get("name") or "结局"
        return "%s 好感 %+d · 已结局【%s】" % (mark, value, name)
    applied = int(res.get("applied") or 0)
    tier = res.get("tierName") or "?"
    if applied:
        return "❤ 好感 %+d（这轮 %+d）·【%s】" % (value, applied, tier)
    return "❤ 好感 %+d ·【%s】" % (value, tier)


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
        head.append("状态：自由模式（好感封存中）")
    elif ending:
        head.append("状态：已结局【%s】" % (ending_cfg(ending, cfg).get("name") or ""))
    else:
        head.append("状态：攻略中")
    head += ["当前好感：%+d（%s）" % (value, (tier_for(value, cfg) or {}).get("name") or "?")]
    if not ending:
        head += ["距离毕业（%+d）还差 %d 点好感" % (MAX_V, MAX_V - value),
                 "距离进局子（%+d）还有 %d 点好感" % (MIN_V, value - MIN_V)]
    head += ["", "纯聊天涨得慢（每天前几次有效），剧情里的照料、付出、解围涨得快。"]
    page1 = "\n".join(head)

    hist = rec.get("hist") if isinstance(rec.get("hist"), list) else []
    rows = ["📋 最近的变化", ""]
    if hist:
        for h in reversed(hist[-6:]):
            rows.append("%s  %+d  %s" % (h.get("ts") or "", int(h.get("d") or 0),
                                         h.get("r") or ""))
    else:
        rows.append("还没有记录，多陪她聊聊？")
    page2 = "\n".join(rows)
    return [page1, page2]


def detail_text(line_key):
    """「好感」指令的纯文本兜底（协议端发不了卡片时用）。没开过返回 None。"""
    nodes = detail_nodes(line_key)
    if nodes is None:
        return None
    return "\n\n".join(nodes)
