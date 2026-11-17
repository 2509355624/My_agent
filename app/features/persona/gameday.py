"""游戏内时间推进：按打分或强制跳天落账。"""

import copy

from app.features.persona.game_text import _gameday, _roll_event
from app.features.persona.game_time import normalize_phase


def apply(key, score=None, force_new_day=False, days=1,
          load_state=None, save_state=None):
    """把这一轮的时间推进落账，返回最新 gameday（会话不存在返回 None）。

    force_new_day（「睡到明天」/时间跳跃）：不看打分，直接 +days 天并掷事件。
    跳多天时不掷日常小事件——「台风天」这种一句话用在一个月跨度上很怪。
    """
    try:
        days = max(1, min(365, int(days or 1)))
    except (TypeError, ValueError):
        days = 1
    st = load_state()
    ent = st.get(key)
    if not isinstance(ent, dict):
        return None
    gd = _gameday(ent)
    # 回退快照（2026-11-04）：这轮把时间推进之前先留一份原样。「回退上一轮」
    # 时整个换回去——不然撤掉一条「说好明天见」的消息，游戏日却停在了第 2 天。
    # 只保一级：每次覆盖上一份（deepcopy 里不会嵌 gameday_prev，它挂在 ent 上）。
    ent["gameday_prev"] = copy.deepcopy(gd)
    # 怀孕状态跟着天数一起存快照（2026-11-05）：跨天那一轮可能刚掷中，
    # 「回退上一轮」只退天数不退怀孕的话，回退完她还怀着。
    ent["preg_prev"] = copy.deepcopy(ent.get("preg"))
    gd["turns_today"] = int(gd.get("turns_today") or 0) + 1
    if force_new_day:
        gd["day"] = int(gd["day"]) + days
        gd["phase"] = "早晨"
        gd["event"] = _roll_event(gd) if days == 1 else ""
        gd["turns_today"] = 0
        # 跳了好几天，挂着的约定多半已经过期（2026-11-06）：原来只在
        # 「正好跳到那一天」时销账，跳 7 天就把它落地成了「今天的事件」，
        # 于是人都到目的地了，模型还在按「前往某地」写行程。
        pday = int(gd.get("plan_day") or 0)
        if gd.get("plan") and pday and pday <= gd["day"]:
            gd["plan"] = ""
            gd["plan_day"] = 0
    elif isinstance(score, dict):
        t = str(score.get("time") or "none").strip().lower()
        if t in ("overnight", "skip_days"):
            gd["day"] = int(gd["day"]) + (3 if t == "skip_days" else 1)
            gd["phase"] = normalize_phase(score.get("phase"))[:12] or gd["phase"]
            gd["event"] = _roll_event(gd)
            gd["turns_today"] = 0
        else:
            # 2026-11-07 起第一段**不再要求**输出 phase（角色 AI 的状态行第四段
            # 是主来源）。这里保留读取只为两条路都通：旧版模型偶然还在给，
            # 给了就采纳；不给就保持状态行给的值，不会被写成空。
            ph = normalize_phase(score.get("phase"))
            if ph:
                gd["phase"] = ph[:12]
        # 2026-11-10：约定在**同一天**约、同一天做完时，跨天销账根本不触发，
        # 旧 plan 会一直挂着被角色当成还欠着提。裁判现在把 plan 写成「到这一拍
        # 仍然有效」的约定：做完了就交空串销账。代码只在它**确实交了字符串**
        # 时覆盖（含空串）；没交作业（None）就沿用，不替它猜。
        raw_plan = score.get("plan")
        if isinstance(raw_plan, str):
            plan = raw_plan.strip()
            gd["plan"] = plan[:80]
            gd["plan_day"] = int(gd["day"]) + 1 if plan else 0
        # 换地方了就更新（2026-11-06）：留空表示「还在原处」，别每轮都写。
        place = str(score.get("place") or "").strip()
        if place:
            gd["place"] = place[:40]
    save_state(st)
    return gd
