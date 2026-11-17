"""小记账本：轮次节拍、后台压缩与归档补压。"""

import logging
import threading

NOTE_MAX_ROWS = 40
_NOTE_LOCK = threading.Lock()
_NOTE_RUNNING = set()
log = logging.getLogger("persona")


def _start_note_compress(session_key):
    """起一次后台压缩；同一条线已经有线程在跑就不再起。

    `note_due` 压缩失败不清零，下一轮会重试。没有这把锁时，模型连续 503
    就会每轮叠一个线程，全部读同一份旧账本再抢着写回。
    """
    from app.features.persona import service as svc

    with _NOTE_LOCK:
        if session_key in _NOTE_RUNNING:
            return False
        _NOTE_RUNNING.add(session_key)

    def _run():
        try:
            compress_note(session_key)
        finally:
            with _NOTE_LOCK:
                _NOTE_RUNNING.discard(session_key)

    threading.Thread(target=_run, name="persona-note", daemon=True).start()
    return True


def _bump_turn(session_key):
    """轮次 +1；攒够 svc.NOTE_EVERY_TURNS 轮就在后台压一次小记。

    放后台：压缩要一次 LLM 调用，不该让第 10 轮那个人的回话凭空慢几秒。

    **节拍另记一份单调值（2026-11-07）**：`turns` 会被回退减 1，而玩家
    「聊一条退好几次」时净增长长期为 0，`svc.NOTE_EVERY_TURNS` 永远够不着，
    账本就停在前一天不动（实测 11-07 全天 94 次聊天 / 28 次回退，
    账本一次都没更新）。`note_due` 只增不减，回退不碰它——被撤掉的
    那轮不进历史，但「生成过一轮」是既成事实，该压的还是要压。
    """
    from app.features.persona import service as svc

    state = svc._load_state()
    st = state.get(session_key)
    if not isinstance(st, dict):
        return
    st["turns"] = int(st.get("turns") or 0) + 1
    if "note_due" not in st:
        # 老线接上已有的节拍，不从零重新攒（否则已经滞后的还得再等 5 轮）。
        st["note_due"] = max(0, int(st.get("turns") or 0) - 1)
    st["note_due"] = int(st.get("note_due") or 0) + 1
    # 累计轮次（2026-11-06 用户要看的「聊了多少轮」）：`turns` 是每 5 轮
    # 清零的压缩节拍，这里另记一份永不归零的总量。老线第一次补算时归档
    # 已经包含这一轮，所以直接取她的回复条数，不再 +1。
    if st.get("turn_total") is None:
        # 归档正常时它已经包含这一轮；归档缺失（老线/写失败）至少算这一轮。
        st["turn_total"] = max(1, svc._archive_assistant_count(session_key))
    else:
        st["turn_total"] = int(st.get("turn_total") or 0) + 1
    # 新的一轮已经落史：刚才那级回退用完了，允许再退一次。
    st.pop("rollback_done", None)
    svc._save_state(state)
    if st["note_due"] >= svc.NOTE_EVERY_TURNS:
        _start_note_compress(session_key)


def _note_convo(session_key, st):
    """取「上次压缩之后」的全部对话，返回 `(消息, 归档总行数)`。

    以前读的是窗口 12 条——和压缩周期（5 轮 ≈ 10 条）几乎是同一批内容。
    一旦因为回退或模型失败拖了几轮才压上，中间那几轮既不在窗口里、
    也没进过任何一版账本，剧情的发展线路就永久断了（2026-11-07 用户报
    「AI 根本不知道前面几个轮次聊的事情的发展线路」）。改读归档里
    `note_at` 之后的部分，滞后多少补多少；老线没归档或增量为空时退回窗口。
    """
    from app.features.persona import service as svc

    arch = svc._read_archive_file(session_key)
    arch_len = len(arch)
    rows = []
    if arch:
        at = max(0, int(st.get("note_at") or 0))
        rows = (arch[-NOTE_MAX_ROWS:] if at <= 0
                else arch[min(at, arch_len):][-NOTE_MAX_ROWS:])
    hist = svc._render_hist(rows) if rows else []
    if not hist:
        hist = svc._render_hist(svc.trim(svc._load_hist(session_key)))
    return hist, arch_len


def _settled_day_facts(gd):
    """给账本压缩器的「系统结算事实」清单：只报事实，不下结论。

    2026-11-10 用户报「昨天约好的事已经做完了，第二天她还当没做」。
    根子在压缩器只看得见旧账本 + 最近对话，看不到这里结算过的天数、
    已办完的事和约定的日期，于是把做过的约定一代代抄回待办节。
    """
    from app.features.persona import service as svc

    gd = gd if isinstance(gd, dict) else {}
    day = svc._as_int(gd.get("day"), 0, 0, 99999)
    facts = ["游戏内第 %d 天 · %s"
             % (day, str(gd.get("phase") or "时段未定"))]
    if str(gd.get("place") or "").strip():
        facts.append("所在地：" + str(gd["place"]).strip())
    ev = str(gd.get("event") or "").strip()
    if ev.startswith("已经办完"):
        facts.append("今天已经办完："
                     + ev[len("已经办完"):].lstrip("：:").strip())
    elif ev.startswith("约定："):
        # 23213a7 之前生成的老存档：那句「约定：「X」」本来就是兑现记录。
        facts.append("今天已经办完：" + ev[len("约定："):].strip("「」"))
    pl = str(gd.get("plan") or "").strip()
    if pl:
        pd = svc._as_int(gd.get("plan_day"), 0, 0, 99999)
        facts.append("系统登记过的约定：%s%s"
                     % (pl, ("（约在第 %d 天）" % pd) if pd else ""))
        if pd and pd <= day:
            facts.append("约的那天已经过了；做没做过以最近对话为准。")
    else:
        facts.append("系统里目前没有登记在册的约定。")
    facts.append("约定是否已经兑现，以【最近发生的】对话为准，系统登记只作参考。")
    return "\n".join("- " + f for f in facts)


def compress_note(session_key):
    """把旧账本 + 上次存档以来的对话合并成一份 ≤1000 字的 Markdown 账本。

    2026-11-05 改：从「一段散文」改成「六个固定小节」。原来每次压缩都是拿
    「上一版摘要 + 最近 12 条」重写，聊到一百轮等于把最早的事摘要了二十遍，
    每遍丢一点。现在给旧账本一个明确的形状，并要求「还成立的条目原样抄」，
    只让模型改变化的部分——代际退化会小很多。
    """
    from app.features.persona import service as svc

    card = svc.active_card(session_key)
    if card is None:
        return False
    st = svc._entry(session_key) or {}
    # 旧账本先纠人称再交给压缩器（2026-11-06）：不让「他」以「原样抄」的形式
    # 传给下一版，否则坏账本会永久自复制。
    old = svc._ledger_fix_pronouns(st.get("note") or "").strip()
    hist, arch_len = _note_convo(session_key, st)
    if not hist:
        return False
    convo = "\n".join(
        "%s：%s" % ("女主角" if m["role"] == "assistant" else "玩家", m["content"])
        for m in hist)
    # 跨天事实（2026-11-10）：压缩器以前只看得见「旧账本 + 最近对话」，
    # 看不到系统结算的「今天已经办完 / 约定约在第几天」，又要求旧条目原样抄，
    # 于是把已经做完的约定一代代抄回待办节。这里只把**事实**列给它，
    # 做没做完由它对照对话自己判断，代码不替它下结论。
    gd = st.get("gameday") if isinstance(st.get("gameday"), dict) else {}
    prompt = svc._NOTE_PROMPT % {
        "soft": svc.NOTE_SOFT,
        "name": svc.char_name(card, key=session_key),
        "day_facts": _settled_day_facts(gd),
        "old": old or "（还没有）",
        "convo": convo,
    }
    try:
        # 输出上限要显式给：1000 字中文约 1500 token，不给的话服务端
        # 默认上限可能截在半句话上（同 `moss_comic.write` 的坑）。
        note = str(svc._llm_call([{"role": "user", "content": prompt}],
                             max_tokens=2400) or "").strip()
    except Exception:
        log.exception("小记压缩失败 %s", session_key)
        return False
    if not note:
        return False
    # 模型没按账本格式交（少小节、整段散文、前后灌了废话）→ 保留旧账本。
    # 宁可记旧事，不能拿一份残的覆盖掉——这是「记忆越压越少」的根。
    if not svc._ledger_ok(note):
        log.info("小记没按账本格式交，保留旧的 %s：%r", session_key,
                 note[:80])
        return False
    state = svc._load_state()
    st = state.get(session_key)
    if not isinstance(st, dict):
        return False                          # 压缩期间出戏了，别把它写回去
    # 压缩期间玩家按了「回退」：归档尾部已经被删掉，这份摘要里含着被撤掉
    # 的那轮。写回去等于把没发生的事记成真事，放弃，下一轮重压。
    if len(svc._read_archive_file(session_key)) < arch_len:
        log.info("压缩期间发生回退，本次账本不落盘 %s", session_key)
        return False
    st["note"] = svc._clip_ledger(note, svc.NOTE_MAX_CHARS)
    st["turns"] = 0
    st["note_due"] = 0
    st["note_at"] = arch_len
    svc._save_state(state)
    log.info("账本已更新 %s（%d 字，原始 %d，补压 %d 条）", session_key,
             len(st["note"]), len(note), len(hist))
    return True
