"""角色扮演的怀孕 / 生产状态（2026-11-05）。

## 只做三件事

1. **周期**：从「认识她的第 1 天」开始按 28 天排。默认没有经期
   （`period_days=0`）：第 1–9 天是安全期、10–16 天易孕、14 天排卵附近、
   17–28 天回到安全期。想加经期就把 `period_days` 调大。
2. **受孕**：只记「这一轮编剧判成 inside」的日子；不立刻判定，等**跨天**
   （点时间跳跃 / AI 判出过夜）时才掷一次。
3. **怀孕到生产**：按天数记。怀孕第 N 天、预产期第 D 天；跨过预产期就
   生产，孩子按出生后的天数显示年龄（岁/月/天）。

v1 只到「孩子出生 + 年龄显示」，不做养育、不做流产/堕胎、不碰未成年。
成年角色由卡上的 `pregnancyEnabled` 控制，默认开——现在这批卡全是成年
角色；以后新增非成年角色时在管理页关掉。
"""
import json
import logging
import os
import random

from app.config import BASE_DIR

log = logging.getLogger("pregnancy")

_CONFIG_PATH = os.path.join(BASE_DIR, "config", "pregnancy.json")

_DEFAULTS = {
    "enabled": True,
    "cycle_length": 28,
    "period_days": 0,
    "fertile_start": 10,
    "fertile_end": 16,
    "ovulation_day": 14,
    "chance_safe": 0.02,
    "chance_fertile": 0.15,
    "chance_ovulation": 0.22,
    "extra_per_exposure": 0.02,
    "max_chance": 0.35,
    "max_chance_safe": 0.08,
    "gestation_days": 280,
    "max_children": 1,
    "year_days": 365,
    "month_days": 30,
}

_STATUS = ("none", "pregnant", "postpartum")


def load_config():
    """读全局参数；缺文件/坏值都退回默认。"""
    cfg = dict(_DEFAULTS)
    try:
        with open(_CONFIG_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            for k in _DEFAULTS:
                if k in data:
                    cfg[k] = data[k]
    except FileNotFoundError:
        pass
    except Exception:                                     # noqa: BLE001
        log.warning("怀孕配置读不了，用默认：%s", _CONFIG_PATH, exc_info=True)
    # 防呆：周期不能小于 2，窗口不能越界
    cfg["cycle_length"] = max(2, int(cfg.get("cycle_length") or 28))
    cfg["period_days"] = max(0, int(cfg.get("period_days") or 0))
    cfg["fertile_start"] = max(1, int(cfg.get("fertile_start") or 10))
    cfg["fertile_end"] = max(cfg["fertile_start"],
                             int(cfg.get("fertile_end") or 16))
    cfg["ovulation_day"] = max(cfg["fertile_start"],
                               min(cfg["fertile_end"],
                                   int(cfg.get("ovulation_day") or 14)))
    cfg["gestation_days"] = max(30, int(cfg.get("gestation_days") or 280))
    cfg["max_children"] = max(1, int(cfg.get("max_children") or 1))
    cfg["year_days"] = max(1, int(cfg.get("year_days") or 365))
    cfg["month_days"] = max(1, int(cfg.get("month_days") or 30))
    return cfg


def card_enabled(card):
    """这张卡开不开怀孕玩法。字段缺省按开启（现有卡都是成年角色）。"""
    cfg = load_config()
    if not cfg.get("enabled", True):
        return False
    return bool((card or {}).get("pregnancyEnabled", True))


def _state(ent):
    """取/补这条线的怀孕状态并挂回 ent。"""
    p = ent.get("preg")
    if not isinstance(p, dict):
        p = {}
    if str(p.get("status") or "") not in _STATUS:
        p["status"] = "none"
    ex = p.get("exposures")
    if not isinstance(ex, list):
        ex = []
    clean = []
    for e in ex:
        if not isinstance(e, dict):
            continue
        try:
            day = int(e.get("day") or 0)
            n = max(1, int(e.get("n") or 1))
        except (TypeError, ValueError):
            continue
        if day > 0:
            clean.append({"day": day, "n": min(5, n)})
    p["exposures"] = clean[-30:]
    for k in ("conceive_day", "due_day", "born_day", "last_checked_day"):
        try:
            p[k] = int(p.get(k) or 0)
        except (TypeError, ValueError):
            p[k] = 0
    if not isinstance(p.get("children"), list):
        p["children"] = []
    ent["preg"] = p
    return p


def cycle_info(day, cfg=None):
    """第 day 天处于周期的哪一段，以及一次内射的基础风险。"""
    cfg = cfg or load_config()
    cycle_day = ((int(day or 1) - 1) % cfg["cycle_length"]) + 1
    if cycle_day <= cfg["period_days"]:
        phase, name, risk = "period", "经期", cfg["chance_safe"]
    elif cycle_day < cfg["fertile_start"]:
        phase, name, risk = "safe", "安全期", cfg["chance_safe"]
    elif cycle_day <= cfg["fertile_end"]:
        if abs(cycle_day - cfg["ovulation_day"]) <= 1:
            phase, name, risk = "ovulation", "排卵期", cfg["chance_ovulation"]
        else:
            phase, name, risk = "fertile", "易孕期", cfg["chance_fertile"]
    else:
        phase, name, risk = "safe", "安全期", cfg["chance_safe"]
    return {"cycle_day": cycle_day, "phase": phase,
            "phase_name": name, "risk": float(risk)}


def record_inside(ent, day):
    """记一次「射在里面」的暴露。同一天多次只累计次数。"""
    p = _state(ent)
    if p["status"] != "none":
        return False
    try:
        day = int(day or 0)
    except (TypeError, ValueError):
        return False
    if day <= 0:
        return False
    for e in p["exposures"]:
        if e["day"] == day:
            e["n"] = min(5, int(e.get("n") or 1) + 1)
            ent["preg"] = p
            return True
    p["exposures"].append({"day": day, "n": 1})
    p["exposures"] = p["exposures"][-30:]
    ent["preg"] = p
    return True


def _birth(p, day, cfg):
    due = int(p.get("due_day") or 0)
    born = due or int(day or 0)
    p["status"] = "postpartum"
    p["born_day"] = born
    p["exposures"] = []
    children = p.get("children") if isinstance(p.get("children"), list) else []
    if len(children) < cfg["max_children"]:
        children.append({"born_day": born})
    p["children"] = children
    return "birth"


def advance(ent, old_day, new_day, cfg=None):
    """跨天结算。返回事件：`conceived` / `birth` / None。"""
    cfg = cfg or load_config()
    p = _state(ent)
    try:
        new_day = int(new_day or old_day or 1)
    except (TypeError, ValueError):
        new_day = int(old_day or 1)

    if p["status"] == "pregnant":
        if new_day >= int(p.get("due_day") or 0) > 0:
            return _birth(p, new_day, cfg)
        return None
    if p["status"] == "postpartum":
        return None

    exposures = list(p.get("exposures") or [])
    p["last_checked_day"] = new_day
    if not exposures:
        ent["preg"] = p
        return None

    # 一次跳跃只掷一次：取暴露日里最高风险的那天，再按次数小幅加权。
    top = 0.0
    total = 0
    for e in exposures:
        info = cycle_info(e["day"], cfg)
        top = max(top, info["risk"])
        total += int(e.get("n") or 1)
    chance = top + cfg["extra_per_exposure"] * max(0, total - 1)
    cap = (cfg["max_chance"] if top >= cfg["chance_fertile"]
           else cfg["max_chance_safe"])
    chance = min(cap, max(0.0, chance))
    p["exposures"] = []
    ent["preg"] = p

    if total <= 0 or random.random() >= chance:
        return None

    # 怀上了：从最后一次暴露那天算。跳跃跨过预产期就直接生产。
    conceive = max(int(e["day"]) for e in exposures)
    p["status"] = "pregnant"
    p["conceive_day"] = conceive
    p["due_day"] = conceive + cfg["gestation_days"]
    ent["preg"] = p
    if new_day >= p["due_day"]:
        return _birth(p, new_day, cfg)
    return "conceived"


def clear(ent):
    """清空存档：连怀孕/孩子和回退快照一起清掉。"""
    ent.pop("preg", None)
    ent.pop("preg_prev", None)


def child_age_text(days, cfg=None):
    """孩子的年龄：按天算，再拆成岁/月/天（用户要「具体到天」）。

    365 → 「1 岁」，400 → 「1 岁 1 个月 5 天」，45 → 「1 个月 15 天」，
    不满一个月 → 「15 天」。
    """
    cfg = cfg or load_config()
    days = max(0, int(days or 0))
    years = days // cfg["year_days"]
    rem = days % cfg["year_days"]
    months = rem // cfg["month_days"]
    rem_days = rem % cfg["month_days"]
    parts = []
    if years:
        parts.append("%d 岁" % years)
    if months:
        parts.append("%d 个月" % months)
    if rem_days or not parts:
        parts.append("%d 天" % rem_days)
    return " ".join(parts)


def view(ent, day, enabled=True):
    """给前端看的状态。没怀孕时不暴露周期细节。"""
    if not enabled:
        return {"enabled": False, "status": "disabled"}
    p = _state(ent)
    try:
        day = int(day or 1)
    except (TypeError, ValueError):
        day = 1
    if p["status"] == "pregnant":
        days = max(1, day - int(p.get("conceive_day") or day) + 1)
        due = int(p.get("due_day") or 0)
        return {
            "enabled": True,
            "status": "pregnant",
            "pregnant_days": days,
            "due_day": due,
            "days_left": max(0, due - day),
        }
    if p["status"] == "postpartum":
        born = int(p.get("born_day") or day)
        child_days = max(0, day - born)
        cfg = load_config()
        return {
            "enabled": True,
            "status": "postpartum",
            "born_day": born,
            "child_days": child_days,
            "child_text": child_age_text(child_days, cfg),
        }
    return {"enabled": True, "status": "none"}


def stage_key(ent, day, cfg=None):
    """当前处在哪个身体阶段，给别的模块做判断用（2026-11-06）。

    返回 `"early" / "mid" / "late" / "postpartum" / ""`。
    `view()` 是给前端看的（天数、预产期），这里是给代码分支用的**稳定键**——
    文案会改，键不会。开发度满格时「一碰就出水」的判断就靠它。
    """
    p = _state(ent)
    try:
        day = int(day or 1)
    except (TypeError, ValueError):
        day = 1
    if p["status"] == "postpartum":
        return "postpartum"
    if p["status"] != "pregnant":
        return ""
    days = max(1, day - int(p.get("conceive_day") or day) + 1)
    total = max(1, int((cfg or load_config()).get("gestation_days") or 280))
    if days <= total * 0.3:
        return "early"
    if days <= total * 0.675:
        return "mid"
    return "late"


def _pregnant_body(days, total):
    """按怀孕天数给编剧的身体素材：阶段、肚子、激素、她会主动做的事。

    不存「肚子尺寸」这种字段——它完全由天数推导，减少一个会写坏的账。
    文案是给演员的自由素材，不是体检报告；调用方只挑贴合的写。
    """
    total = max(1, int(total or 280))
    if days <= total * 0.3:                         # 约前 12 周
        return ("早孕",
                "肚子还看不出来，只有她自己觉得小腹发胀、裤腰变紧。",
                "晨起恶心、闻到油烟或香水会反胃、容易累、乳房胀痛、"
                "情绪忽上忽下；性欲可能下降，也可能因为激素变得敏感。",
                "突然犯恶心要去吐、闻到味道皱眉、说累想睡、护着胸口说胀；"
                "情绪来得快，会突然要抱或不想说话；想吃什么会直接点。")
    if days <= total * 0.675:                       # 约 13–27 周
        kick = ("已经能感觉到胎动，安静下来时肚皮会鼓一下。"
                if days >= int(total * 0.45)
                else "胎动还没出现，她可能还不确定自己是不是真的怀了。")
        return ("孕中期",
                "小腹已经明显隆起，侧面看得出来；" + kick,
                "精力比早孕回升，性欲可能变强；乳房变大、皮肤变敏感，"
                "腰开始有点酸。",
                "她会拉着你的手放到肚子上让你摸胎动；腰酸时靠过来让你揉；"
                "精力回来了，想要亲近时会自己贴上来，不舒服也会直接说。")
    return ("孕晚期",
            "肚子已经很显，动作变慢，弯腰、趴着会难受；胎动明显。",
            "腰酸、水肿、假性宫缩、呼吸短、尿频；性欲起伏大，"
            "临近预产期容易焦虑、黏人。",
            "走两步就停下扶着腰；假性宫缩时抓着你不放；脚肿了不想穿鞋；"
            "夜里睡不好会推醒你；临近预产期会反复确认你在不在。")


def _postpartum_body(days, month_days=30):
    """产后恢复按天数分段：刚生 / 月子恢复 / 已恢复。"""
    days = max(0, int(days or 0))
    month_days = max(1, int(month_days or 30))
    if days <= 7:
        return ("刚生产",
                "恶露、伤口还在疼、涨奶；走路慢，抱孩子都费劲。",
                "睡眠被切碎，情绪很脆弱，容易一件小事就掉眼泪。",
                "涨奶疼得皱眉、让孩子吸或让你帮忙热敷；伤口牵一下会倒吸气；"
                "累到不想说话；情绪上来会突然哭。")
    if days <= month_days * 1.5:                    # 约 45 天
        return ("月子里",
                "体力慢慢回来，肚子还没收回去，抱孩子久了腰酸。",
                "哺乳、涨奶、睡眠不足、情绪起伏；性欲通常很低，"
                "身体还在恢复。",
                "抱孩子久了会让你接手；涨奶或哺乳时会说不方便；"
                "睡眠不够脾气变差；想要你陪但嘴上不说。")
    return ("已恢复",
            "体力基本回来，小腹可能还松；伤口偶尔发紧。",
            "激素慢慢回稳，情绪比月子里稳定；性欲开始回来，但可能还怕。",
            "体力回来了，开始想出门、想亲近；肚子松会下意识遮一下；"
            "伤口偶尔发紧会提一句。")


def prompt_block(ent, day, event=None):
    """跨天/每轮拼进系统头的身体状态。没有状态返回空串。"""
    cfg = load_config()
    v = view(ent, day, enabled=True)
    if v["status"] == "pregnant":
        stage, belly, hormone, active = _pregnant_body(
            v["pregnant_days"], cfg["gestation_days"])
        lines = ["【她的身体 · 已孕 · %s】" % stage,
                 "怀孕第 %d 天，预产期第 %d 天（还有 %d 天）。"
                 % (v["pregnant_days"], v["due_day"], v["days_left"]),
                 "肚子：%s" % belly,
                 "激素与身体：%s" % hormone,
                 "她会主动：%s" % active,
                 "按这个阶段写她的身体和情绪；她会自己表现出来，不用等玩家问。"
                 "但别每轮都播报——挑 1–2 个贴合这一拍的细节，不要写成体检报告。"]
        if event == "conceived":
            lines.insert(1, "【新情况】她可能刚发现：月经迟了、早上犯恶心、"
                            "验孕棒两条线。让她自己察觉并说出来，不要替她宣布。")
        return "\n".join(lines)
    if v["status"] == "postpartum":
        stage, belly, hormone, active = _postpartum_body(
            v["child_days"], cfg["month_days"])
        return "\n".join([
            "【孩子 · 已出生 · 她%s】" % stage,
            "孩子已经出生第 %d 天（%s）。" % (v["child_days"], v["child_text"]),
            "她的身体：%s" % belly,
            "激素与情绪：%s" % hormone,
            "她会主动：%s" % active,
            "孩子现在多大、需要什么，按这个天数写；不要把刚出生的孩子写得"
            "像会说话的大人。",
        ])
    if event == "conceived":
        return ("【新情况】她可能刚发现怀孕：月经迟了、早上犯恶心、验孕棒"
                "两条线。让她自己察觉并说出来，不要替她宣布。")
    return ""
