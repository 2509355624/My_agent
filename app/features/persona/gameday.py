"""游戏内时间推进：按打分或强制跳天落账。"""

import copy

from app.features.persona.game_text import _gameday, _roll_event
from app.features.persona.game_time import PHASES, normalize_phase


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


def game_day(session_key):
    from app.features.persona import service as svc
    """这条线游戏内第几天（面板/回包用）；没在演返回 None。"""
    ent = svc._entry(svc.resolve_line(session_key))
    if not isinstance(ent, dict):
        return None
    return _gameday(ent).get("day")


# 时段常量与 normalizing 见 game_time。


def game_phase(session_key):
    from app.features.persona import service as svc
    """这条线当前时段；没在演返回 ""。"""
    ent = svc._entry(svc.resolve_line(session_key))
    if not isinstance(ent, dict):
        return ""
    return str(_gameday(ent).get("phase") or "")


def set_phase(session_key, phase, sender_uin=""):
    from app.features.persona import service as svc
    """手动切换今天时段，返回 `{"day": N, "phase": "中午"}`；没在演返回 None。

    刻意不碰 `gameday_prev`：切时段不是一轮对话，回退上一轮不该把它撤掉。
    也不重置 `turns_today`，否则「这一天聊了很久」的收束节拍会被刷掉。
    """
    ph = normalize_phase(phase)
    if ph not in PHASES:
        return None
    key = svc.resolve_line(svc._persona_key(session_key, sender_uin))
    st = svc._load_state()
    ent = st.get(key)
    if not isinstance(ent, dict):
        return None
    gd = _gameday(ent)
    gd["phase"] = ph
    svc._save_state(st)
    return {"day": gd["day"], "phase": gd["phase"]}


def _time_boundary(gd, phase=None):
    """跳跃轮最后一条 user：一句纯场景事实，标出「新的一轮从这里开始」。

    2026-11-06 D：任务指令全部搬进 system 末尾，玩家语域只留这一句时间
    标记。原来那条 user 写满了「【这一轮】…按平常的格式输出」，模型会把它
    当成玩家说的话，角色语域立刻乱掉。

    保留一条 user 有两个原因：OpenAI 兼容端点普遍以 user 作为一轮的开始；
    也让模型明确「昨晚那一幕已经收了，下面写新的一段」，而不是顺着上一条
    assistant 把昨晚继续写下去。
    """
    from app.features.persona import service as svc

    day = svc._day_of(gd)
    ph = str(phase or (gd or {}).get("phase") or "").strip()
    return "（第 %d 天%s）" % (day, ("，" + ph) if ph else "")


def jump_phase(session_key, phase, sender="", sender_uin=""):
    """切到今天的某个时段，并由她先开口收尾上一段、带入新时段（2026-11-05）。

    和 `skip_day` 的区别：**不推进天数**、不掷事件、不结算怀孕；但会
    `svc.affection.sleep()` 把当前的亲密场景收掉（用户：跳转代表 do 结束），
    再把跳跃前最后一幕的完整原话拼进系统头，避免剧情断片。
    返回 `svc.Turn`；没在演返回 None。
    """
    from app.features.persona import service as svc

    ph = svc.normalize_phase(phase)
    if ph not in svc.PHASES:
        return None
    key = svc.resolve_line(svc._persona_key(session_key, sender_uin))
    card = svc.active_card(key)
    if card is None:
        return None
    ent = svc._entry(key)
    if not ent:
        return None

    gd = svc._gameday(ent)
    old_phase = str(gd.get("phase") or "").strip() or "刚才"
    got = svc.set_phase(key, ph)
    if not got:
        return None
    gd = svc._gameday(svc._entry(key) or ent)

    # 上一段 do 收场：场景结束、高潮值归零、冲动回落（好感/堕落不动）。
    try:
        svc.affection.sleep(key)
    except Exception:                                  # noqa: BLE001
        svc.log.exception("时段跳跃收场失败 %s", key)

    ent = svc._entry(key) or ent
    note = str(ent.get("note") or "")
    prev_state = str(ent.get("state") or "")

    head = svc.build_head(card, key, sender, note, prev_state, intimate=False)
    tblock = svc._gameday_block(gd)
    if tblock:
        head = head + "\n\n" + tblock
    pblock = svc.pregnancy_block(key)
    if pblock:
        head = head + "\n\n" + pblock
    recap = svc._recap_block(key)
    if recap:
        head = head + "\n\n" + recap
    task = svc._PHASE_TASK % (old_phase, ph, svc._day_of(gd), ph)
    # 2026-11-06 D：指令归 system；最后那条 user 只留一句时间事实。
    head = head + "\n\n" + task

    # 缓存滞回窗口（2026-11-10）：这三条旁路也要用整段窗口、并且照样只追加
    # 落盘，否则它们一跑就把主回复攒起来的前缀砍回 12 条，前功尽弃。
    win = svc.cache_rows(svc._load_hist(key))
    model_hist = svc._render_hist(
        svc.trim(win, limit=len(win)), name=svc.char_name(card, key=key),
        alias=str((card or {}).get("name") or ""))
    # 近因位跟普通轮同源：时间事实之后仍贴一句正向锚点，别让跳跃开场
    # 因为换了条组装路径就丢了视角。
    msgs = ([{"role": "system", "content": head}] + model_hist
            + [{"role": "user", "content": _time_boundary(gd, ph)
                + "\n\n" + svc.writing_style.tail(
                    name=svc.char_name(card, key=key))}])
    try:
        raw = svc._llm_call(msgs, temperature=svc.TEMPERATURE)
    except svc.llm.PersonaChainExhausted:
        raise                                   # 见 run_turn：链路全挂要弹引导窗
    except Exception:                                  # noqa: BLE001
        svc.log.exception("时段跳跃开场失败 %s", key)
        return None
    reply, scene, mood, intensity, state = svc.split_turn(
        raw, default_mood=str(ent.get("mood") or ""))
    # 服饰（2026-11-09）：时段跳跃 / 隔天 / 收尾也会换衣服，状态一样要更新，
    # 否则「第二天换了身衣服」下一轮又被注回昨晚那件。没交作业就沿用。
    outfit = svc.split_outfit(raw) or str((ent or {}).get("outfit") or "").strip()
    if not reply:
        return None
    # 只归档她的开场，不归档那行系统任务。
    win.append({"role": "assistant",
                 "content": svc._ensure_meta(raw, scene, mood, intensity, state, outfit)})
    try:
        svc._save_hist(key, svc.cache_rows(win))
        svc._append_archive(key, win[-1:], seed=svc.trim(win[-svc.HISTORY_LIMIT:]))
    except Exception:                                  # noqa: BLE001
        svc.log.exception("时段跳跃落史失败 %s", key)
    svc._save_mood(key, mood, intensity, state, prev_state, outfit=outfit)
    svc._bump_turn(key)
    return svc.Turn(reply, scene, mood, intensity, state, None, "")


def skip_day(session_key, sender="", sender_uin="", days=1, doing=""):
    """时间跳跃（2026-11-04；2026-11-05 支持多天）：推进 days 天，**由她先开口**。

    `doing`（2026-11-06）：玩家写的一句「这段时间在干什么」，比如
    「一路赶路去情爱城」。给了就按它写**过程**，而不是写成压缩后的结论——
    跳七天不是想省七天，是想把那七天的路走完。

    事件优先级在 `_roll_event`：约定 > 随机事件 > 普通的一天；跳多天时不掷
    日常小事件。跨天时先结算怀孕，再拼开场提示词。
    返回 `svc.Turn`；没在演（没有会话条目）返回 None，调用方当普通消息处理。
    """
    from app.features.persona import service as svc

    try:
        days = max(1, min(365, int(days or 1)))
    except (TypeError, ValueError):
        days = 1
    doing = str(doing or "").strip()[:200]
    # 落一条日志（2026-11-06）：用户报「跳七天没按我写的过程走」，但从结果
    # 分不清是前端没把内容带上来、还是模型没照做。把入参记下来就能一眼判断。
    svc.log.info("[skip] days=%s doing=%r", days, doing[:60])
    key = svc.resolve_line(svc._persona_key(session_key, sender_uin))
    card = svc.active_card(key)
    if card is None:
        return None
    ent = svc._entry(key)
    if not ent:
        return None

    old_day = svc._day_of(svc._gameday(ent))
    gd = svc._apply_gameday(key, force_new_day=True, days=days)
    new_day = int((gd or {}).get("day") or old_day)
    preg_event = None
    if svc.pregnancy.card_enabled(card):
        try:
            preg_event = svc._preg_advance(key, old_day, new_day)
        except Exception:                              # noqa: BLE001
            svc.log.exception("怀孕结算失败 %s", key)
    # `svc._preg_advance` 会重新读盘写盘，拿最新的条目，别用旧的 mood/note。
    ent = svc._entry(key) or ent
    note = str(ent.get("note") or "")
    prev_state = str(ent.get("state") or "")

    # 过夜收场（2026-11-05）：场景结束、高潮值归零、冲动回落。
    # 不这么做的话，第二天早上面板还挂着昨晚的高潮值，状态行也还是床上的
    # 衣着和位置，模型只能接着昨晚写——「睡了一觉什么都没变」。
    try:
        svc.affection.sleep(key)
    except Exception:                                  # noqa: BLE001
        svc.log.exception("跨夜状态收场失败 %s", key)

    head = svc.build_head(card, key, sender, note, prev_state, intimate=False)
    tblock = svc._gameday_block(gd)
    if tblock:
        head = head + "\n\n" + tblock
    # 怀孕/孩子：跨天时先把结果告诉她，再让她开口。
    pblock = svc.pregnancy_block(key, preg_event)
    if pblock:
        head = head + "\n\n" + pblock
    # 前情（2026-11-06）：只给跳跃前最后一幕的完整原话。挂着的约定/所在地
    # 已经在时间块里，这里不再重复一份半句清单。
    recap = svc._recap_block(key)
    if recap:
        head = head + "\n\n" + recap
    event = str((gd or {}).get("event") or "").strip()
    if doing:
        # 玩家交代了过程：按他给的主线写这一段，不写「压缩摘要」。
        task = svc._SKIP_TASK_DOING % (days, new_day, doing, new_day)
    elif days == 1:
        task = svc._SKIP_TASK % (new_day,
                             ("（今天：%s）" % event) if event else "")
    else:
        task = svc._SKIP_TASK_MULTI % (days, new_day,
                                   ("（这段时间里：%s）" % event) if event else "")
    # 2026-11-06 D：这一轮该怎么写，全在 system 里说清；玩家语域只留一句
    # 时间事实，免得「按格式输出」这种系统腔被当成他说的话。
    head = head + "\n\n" + task
    # 缓存滞回窗口（2026-11-10）：这三条旁路也要用整段窗口、并且照样只追加
    # 落盘，否则它们一跑就把主回复攒起来的前缀砍回 12 条，前功尽弃。
    win = svc.cache_rows(svc._load_hist(key))
    model_hist = svc._render_hist(
        svc.trim(win, limit=len(win)), name=svc.char_name(card, key=key),
        alias=str((card or {}).get("name") or ""))
    # 睡到明天同样贴近因位，和普通轮、时段跳跃共用一句。
    msgs = ([{"role": "system", "content": head}] + model_hist
            + [{"role": "user", "content": _time_boundary(gd)
                + "\n\n" + svc.writing_style.tail(
                    name=svc.char_name(card, key=key))}])
    try:
        raw = svc._llm_call(msgs, temperature=svc.TEMPERATURE)
    except svc.llm.PersonaChainExhausted:
        raise                                   # 见 run_turn：链路全挂要弹引导窗
    except Exception:                                  # noqa: BLE001
        svc.log.exception("时间跳跃开场失败 %s", key)
        return None
    reply, scene, mood, intensity, state = svc.split_turn(
        raw, default_mood=str(ent.get("mood") or ""))
    # 服饰（2026-11-09）：时段跳跃 / 隔天 / 收尾也会换衣服，状态一样要更新，
    # 否则「第二天换了身衣服」下一轮又被注回昨晚那件。没交作业就沿用。
    outfit = svc.split_outfit(raw) or str((ent or {}).get("outfit") or "").strip()
    if not reply:
        return None
    # 只归档她的开场，不归档那行系统任务（见 svc._SKIP_TASK 注释）。
    win.append({"role": "assistant",
                 "content": svc._ensure_meta(raw, scene, mood, intensity, state, outfit)})
    try:
        svc._save_hist(key, svc.cache_rows(win))
        svc._append_archive(key, win[-1:], seed=svc.trim(win[-svc.HISTORY_LIMIT:]))
    except Exception:
        svc.log.exception("时间跳跃落史失败 %s", key)
    svc._save_mood(key, mood, intensity, state, prev_state, outfit=outfit)
    svc._bump_turn(key)
    return svc.Turn(reply, scene, mood, intensity, state, None)
