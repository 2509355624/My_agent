"""结束夜与次日：高潮、过夜、第二天开场。"""

import logging

log = logging.getLogger("persona")


def set_finish(session_key, spot):
    """记下玩家选的射精位置。下一轮 `run_turn` 取走并交给编剧。"""
    from app.features.persona import service as svc

    key = svc.resolve_line(session_key)
    spot = str(spot or "").strip().lower()
    if spot not in svc.FINISH_SPOTS:
        return False
    st = svc._load_state()
    ent = st.get(key)
    if not isinstance(ent, dict):
        return False
    ent["finish_pick"] = spot
    svc._save_state(st)
    return True


def take_finish(session_key):
    """取走并清空。没有就返回空串。"""
    from app.features.persona import service as svc

    key = svc.resolve_line(session_key)
    st = svc._load_state()
    ent = st.get(key)
    if not isinstance(ent, dict):
        return ""
    spot = str(ent.pop("finish_pick", "") or "").strip().lower()
    if not spot:
        return ""
    svc._save_state(st)
    return spot


def _set_pending_finish(key, info):
    """把「等着问射在哪里」这个待办记进存档（2026-11-06）。

    为什么需要它：高潮值是在**普通一轮**里结算的（`settle` 已经把它扣了），
    而「射在哪里」的弹窗是那一轮之后才弹的。如果玩家点确定之前页面刷新、
    断网、或者干脆关了标签，弹窗就没了——数值已经扣过，文本却没写。
    之前就是这么丢了那一段。

    所以把待办落盘：刷新后 `/api/persona/session` 会把它带回来，
    前端重新弹一次。玩家真写了（或者明确跳过）才清掉。
    """
    from app.features.persona import service as svc

    if not key:
        return
    try:
        ent = svc._entry(key) or {}
        if info:
            ent["pending_finish"] = dict(info)
        else:
            if "pending_finish" not in ent:
                return          # 本来就没有待办：不白写一次盘
            ent.pop("pending_finish", None)
        state = svc._load_state()
        state[key] = ent
        svc._save_state(state)
    except Exception:                                  # noqa: BLE001
        svc.log.exception("待办射精落盘失败 %s", key)


def pending_finish(key):
    """这条线有没有一个「还没回答」的射精弹窗。"""
    from app.features.persona import service as svc

    try:
        ent = svc._entry(key) or {}
        v = ent.get("pending_finish")
    except Exception:                                  # noqa: BLE001
        return None
    if not isinstance(v, dict):
        return None
    try:
        n = int(v.get("n") or 0)
    except (TypeError, ValueError):
        n = 0
    if n <= 0:
        return None
    return {"n": n, "of": int(v.get("of") or 0), "last": bool(v.get("last"))}


def _climax_info(res):
    """这一轮是不是刚到高潮 / 是不是第 5 次。没到返回 None。

    前端拿到 `{n, of, last}` 就弹「射在哪里」；`last=True` 时答完再弹
    「结束 / 最后一次」。判定只用 `just_orgasm`——它由 `settle` 写，
    是「这一轮真的到了一次」的唯一事实来源。
    """
    from app.features.persona import service as svc

    try:
        n = int((res or {}).get("just_orgasm") or 0)
    except (TypeError, ValueError):
        n = 0
    if n <= 0:
        return None
    try:
        omax = int((res or {}).get("orgasmMax") or 0)
    except (TypeError, ValueError):
        omax = 0
    return {"n": n, "of": omax, "last": bool(omax and n >= omax)}


def _clear_adult_night(key):
    """跨天把 v2 的高潮值和冷却清零（2026-11-10）。

    `affection.sleep()` 管的是时间跳跃 / 第二天早晨那两条显式跨天路径；
    编剧在聊天里判 `time=overnight` 走的是 `_apply_gameday`，以前**完全没
    收场**，第二天面板还挂着昨晚的高潮值。这里补上，和 sleep 同口径：
    只清 excite / recover，开发度那些「发生过的事实」不动。
    """
    from app.features.persona import service as svc

    try:
        st = svc._load_state()
        rec = st.get(key) or {}
        aff = rec.get("affection")
        adult = aff.get("adult") if isinstance(aff, dict) else None
        if not isinstance(adult, dict):
            return
        aff["adult"] = svc.adult_state.night_reset(adult)
        svc._save_state(st)
    except Exception:                                    # noqa: BLE001
        svc.log.exception("跨天清 v2 高潮值失败 %s", key)


def finish_block(spot):
    """把「射在哪里」拼成演员层的导演条。没有选择就返回空串。"""
    from app.features.persona import service as svc

    spot = str(spot or "").strip().lower()
    hint = svc._FINISH_HINT.get(spot)
    if not hint:
        return ""
    return ("## 这一拍：射精（系统已经替你决定了位置）\n\n"
            + hint
            + "\n\n（这是**已经发生的事实**，不要改成别的位置，"
              "也不要问她「要射哪里」——她已经在高潮的余韵里，"
              "这一拍是他自己的事。写她怎么承受、怎么反应。）")


def _recap_block(session_key):
    """跨日/切时段的「前情」：只给跳跃前**最后一幕**的完整原话。

    2026-11-06 重写（用户：切换时间之后 AI 给的内容很差）：
    旧版把最近 6 轮按 90 字硬切成 12 条清单，每条都断在半句上；这 1199 字
    又和后面 12 条原始历史讲的是同一段事，一边是残缺摘要、一边是原文，
    互相打架。模型手里没有「昨晚停在哪」，只能另起一个头。

    现在只给一幕：完整、按句收尾、和历史里的原文一致。挂着的约定、所在地、
    昨晚的状态本来就在 `_gameday_block` / `build_head` 里，这里不再重复一遍。

    归档里「她先开口」的跳跃轮没有配对的 user（这是设计，不是断腿），
    所以按**消息序列**取尾部两条：孤儿轮只写她那一半，接着她那句往下写。
    """
    from app.features.persona import service as svc

    rows = [m for m in (svc.load_archive(session_key) or [])
            if isinstance(m, dict) and m.get("role") in ("user", "assistant")]
    tail = rows[-svc._RECAP_MSGS:]
    lines = ["【前情 · 跳跃前停在这里】",
             "（下面是刚才最后一幕的原话。镜头就从这里抬起来，接着往下写。）"]
    for m in tail:
        if m.get("role") == "user":
            body = svc._brief(svc._visible_user_text(m.get("seg"), m.get("content")))
            who = "他"
        else:
            body, _sc, _mo, _it, _st = svc.split_turn(str(m.get("content") or ""))
            body = svc._brief(body)
            who = "她"
        if body:
            lines.append("%s：%s" % (who, body))
    if len(lines) <= 2:
        return ""
    # 上一次跳跃是她先开的口：这一幕只有她的话，明说一句，读起来才不像漏了半轮。
    if not any(ln.startswith("他：") for ln in lines[2:]):
        lines.append("（上一轮是她先开的口，从她这句话接着写就行。）")
    return "\n".join(lines)


def finish_night(session_key, last=True, sender="", sender_uin=""):
    """第五次之后的两条出口（2026-11-05 用户拍板）。

    `last=True`  —— 再来最后一次，然后到第二天早晨（两段一个气泡）；
    `last=False` —— 就到这里，收场，留在当晚。

    玩家在弹窗里选完之后调这个：它**不再等玩家发消息**，直接生成。
    返回 `Turn`；没在演返回 None。
    """
    from app.features.persona import service as svc


def _gen_and_save(key, ent, card, head, task, sender="", prev_state="",
                  tag="收尾"):
    from app.features.persona import service as svc

    """公共尾巴：拿 head + task 调一次模型，剥格式、落史、落心情。

    `finish_action` / `finish_night` / `next_morning` 三处只差提示词，
    其余完全一样，抽出来免得三份各写一遍（写岔一次就有一处不落史）。
    """
    # 缓存滞回窗口（2026-11-10）：这三条旁路也要用整段窗口、并且照样只追加
    # 落盘，否则它们一跑就把主回复攒起来的前缀砍回 12 条，前功尽弃。
    win = svc.cache_rows(svc._load_hist(key))
    model_hist = svc._render_hist(
        svc.trim(win, limit=len(win)), name=svc.char_name(card, key=key),
        alias=str((card or {}).get("name") or ""))
    # 收尾 / 第二天 / 第五次收场：这一轮的最后一条是任务指令，同样在它
    # 末尾贴统一锚点，让视角要求离生成点最近。
    msgs = ([{"role": "system", "content": head}] + model_hist
            + [{"role": "user", "content": task
                + "\n\n" + svc.writing_style.tail(
                    name=svc.char_name(card, key=key))}])
    try:
        raw = svc._llm_call(msgs, temperature=svc.TEMPERATURE)
    except svc.llm.PersonaChainExhausted:
        raise                                   # 见 run_turn：链路全挂要弹引导窗
    except Exception:                                  # noqa: BLE001
        svc.log.exception("%s生成失败 %s", tag, key)
        return None
    reply, scene, mood, intensity, state = svc.split_turn(
        raw, default_mood=str((ent or {}).get("mood") or ""))
    # 服饰（2026-11-09）：时段跳跃 / 隔天 / 收尾也会换衣服，状态一样要更新，
    # 否则「第二天换了身衣服」下一轮又被注回昨晚那件。没交作业就沿用。
    outfit = svc.split_outfit(raw) or str((ent or {}).get("outfit") or "").strip()
    if not reply:
        return None
    win.append({"role": "assistant",
                 "content": svc._ensure_meta(raw, scene, mood, intensity, state, outfit)})
    try:
        svc._save_hist(key, svc.cache_rows(win))
        svc._append_archive(key, win[-1:], seed=svc.trim(win[-svc.HISTORY_LIMIT:]))
    except Exception:
        svc.log.exception("%s落史失败 %s", tag, key)
    svc._save_mood(key, mood, intensity, state, prev_state, outfit=outfit)
    svc._bump_turn(key)
    return svc.Turn(reply, scene, mood, intensity, state, None)


def finish_action(session_key, spot="", text="", sender="", sender_uin=""):
    """她高潮之后，按玩家选的位置 / 自己写的做法，**立刻**写这一下。

    2026-11-05 晚改：以前是「选完等玩家再发一句话」，用户说那样很别扭——
    他得猜什么时候该写射精。现在选完直接生成一轮，玩家只负责推动剧情。

    `spot` 是预设位置（见 `svc.FINISH_SPOTS`），`text` 是玩家自己写的做法；
    两个都给时**以 text 为准**（自定义比预设更具体）。
    返回 `Turn`；没在演返回 None。
    """
    from app.features.persona import service as svc

    key = svc.resolve_line(svc._persona_key(session_key, sender_uin))
    card = svc.active_card(key)
    if card is None:
        return None
    ent = svc._entry(key)
    if not ent:
        return None

    text = str(text or "").strip()[:120]
    spot = str(spot or "").strip().lower()
    if spot not in svc.FINISH_SPOTS:
        spot = ""
    if not spot and not text:
        return None

    # 位置决定内射：怀孕只认这个，不靠编剧猜。
    finish = "none"
    if spot in ("pussy", "anus"):
        finish = "inside"
    elif spot in ("mouth", "body"):
        finish = "outside"
    day = svc._day_of(svc._gameday(ent))
    if finish == "inside":
        try:
            svc._preg_record(key, card, day, finish)
        except Exception:                              # noqa: BLE001
            svc.log.exception("怀孕记录失败 %s", key)

    prev_state = str(ent.get("state") or "")
    head = svc.build_head(card, key, sender, str(ent.get("note") or ""),
                      prev_state, intimate=True)
    # 自定义优先，其次才是预设位置。
    hint = (svc._FINISH_CUSTOM_HINT % text) if text else finish_block(spot)
    if hint:
        head = head + "\n\n" + hint
    turn = _gen_and_save(key, ent, card, head, svc._FINISH_ACT_TASK,
                         sender=sender, prev_state=prev_state, tag="射精")
    # 写成功了才把待办清掉（2026-11-06）：失败时留着，页面刷新还能再问一次，
    # 不然就变成「数值扣了、文本没了」那种断层。
    if turn is not None:
        _set_pending_finish(key, None)
    return turn


def finish_night(session_key, last=True, sender="", sender_uin=""):
    """第五次之后的出口（2026-11-05）。

    `last=True`  —— 再来最后一次（只写这一夜，**不跨天**；天亮留给
                    `next_morning`，中间要插一个「第二天」标识）；
    `last=False` —— 就到这里，收场，留在当晚。

    玩家在弹窗里选完之后调这个，它不等玩家发消息，直接生成。
    """
    from app.features.persona import service as svc

    key = svc.resolve_line(svc._persona_key(session_key, sender_uin))
    card = svc.active_card(key)
    if card is None:
        return None
    ent = svc._entry(key)
    if not ent:
        return None

    # 玩家在弹窗里选的射精位置还没被用掉（第五次没有下一轮去消费它）。
    spot = take_finish(key)
    fblk = finish_block(spot)
    if last:
        # 只收场，不跨天——跨天由 `next_morning` 单独一次请求做。
        try:
            svc.affection.sleep(key)
        except Exception:                              # noqa: BLE001
            svc.log.exception("第五次收场失败 %s", key)

    ent = svc._entry(key) or ent
    prev_state = str(ent.get("state") or "")
    head = svc.build_head(card, key, sender, str(ent.get("note") or ""),
                      prev_state, intimate=True)
    if fblk:
        head = head + "\n\n" + fblk
    recap = _recap_block(key)
    if recap:
        head = head + "\n\n" + recap
    task = svc._FINISH_LAST_TASK if last else svc._FINISH_END_TASK
    return _gen_and_save(key, ent, card, head, task, sender=sender,
                         prev_state=prev_state, tag="第五次收尾")


def next_morning(session_key, sender="", sender_uin=""):
    """第二天早晨（2026-11-05）：跨天 + 一次开场。

    单独一个函数是因为前端要在**两次请求之间**浮出「第二天」那个标识：
    先写完夜里，再显示跨天，再写早上。合成一次就插不进去了。
    """
    from app.features.persona import service as svc

    key = svc.resolve_line(svc._persona_key(session_key, sender_uin))
    card = svc.active_card(key)
    if card is None:
        return None
    ent = svc._entry(key)
    if not ent:
        return None

    old_day = svc._day_of(svc._gameday(ent))
    gd = svc._apply_gameday(key, force_new_day=True, days=1) or svc._gameday(ent)
    new_day = int((gd or {}).get("day") or old_day)
    preg_event = None
    if svc.pregnancy.card_enabled(card):
        try:
            preg_event = svc._preg_advance(key, old_day, new_day)
        except Exception:                              # noqa: BLE001
            svc.log.exception("怀孕结算失败 %s", key)
    # 跨天之后这一场必然结束了，再收一次场（幂等）。
    try:
        svc.affection.sleep(key)
    except Exception:                                  # noqa: BLE001
        svc.log.exception("跨天收场失败 %s", key)

    ent = svc._entry(key) or ent
    prev_state = str(ent.get("state") or "")
    head = svc.build_head(card, key, sender, str(ent.get("note") or ""),
                      prev_state, intimate=False)
    tblock = svc._gameday_block(gd)
    if tblock:
        head = head + "\n\n" + tblock
    pblock = svc.pregnancy_block(key, preg_event)
    if pblock:
        head = head + "\n\n" + pblock
    recap = _recap_block(key)
    if recap:
        head = head + "\n\n" + recap
    return _gen_and_save(key, ent, card, head, svc._FINISH_MORNING_TASK,
                         sender=sender, prev_state=prev_state, tag="第二天")
