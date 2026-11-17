"""把这一轮的情绪、状态、当前服饰写回会话条目。"""

from app.features.persona.game_text import _gameday
from app.features.persona.game_time import PHASES, normalize_phase
from app.features.persona.prompt_blocks import normalize_contact


def save(session_key, mood, intensity, state, prev_state="", outfit="",
         load_state=None, save_state=None):
    """把这一轮的情绪/状态/当前服饰写回会话条目。**已经出戏的不写。**

    `outfit`（2026-11-09）：演员这一轮交的「服饰：」行。空串 = 没交作业，
    沿用它上一轮已经写在 `st["outfit"]` 的值，不清零——和 mood/state 同一套
    「漏写不改状态」的规矩。

    2026-11-02：模型偶尔整轮不交三行（同一份契约、同一句话，实测有 1/6 概率
    全空）。这时候**保留上一轮的值**而不是写空——否则下一轮 `state` 变成空串，
    「她穿着什么」这条线就断了，出图又会回到裸体。
    """
    state_data = load_state()
    st = state_data.get(session_key)
    if not isinstance(st, dict):
        return
    # 回退快照（2026-11-04）：这轮覆盖之前先留一份。回退上一轮时还原——
    # 状态行（她穿着什么/在哪）是下一轮拼系统头用的，留着一轮发错消息的
    # 状态，剧情会顺着错的方向演下去。
    st["mood_prev"] = {"mood": str(st.get("mood") or ""),
                       "intensity": st.get("intensity") or 0,
                       "state": str(st.get("state") or ""),
                       "outfit": str(st.get("outfit") or "")}
    if mood:
        st["mood"] = mood
    try:
        val = int(intensity or 0)
    except (TypeError, ValueError):
        val = 0
    if val or not st.get("intensity"):
        st["intensity"] = val
    if state:
        st["state"] = str(state)[:200]
        # 地点同步（2026-11-07）：状态行按契约是「地点 · 衣着 · 姿态」，
        # 第一段就是她此刻在哪，是模型每轮自己交的确定事实。以前 place
        # 只在打分器给了 score["place"] 时才更新，没给就一直空着，模型
        # 只能拿滞后账本里的旧地点猜——人在小超市，写的还是旧书市。
        spot = str(state).split("·")[0].strip()[:40]
        if spot:
            gd_now = _gameday(st)
            last = str(gd_now.get("place") or "").strip()
            # 同一地点连着写了几拍（2026-11-09）：只用来告诉模型「在原地转了
            # 几圈」，换不换场仍然它自己定。换地点归一，同地点累加；第一次
            # 有地点时从 1 起算。
            if last and last == spot:
                try:
                    gd_now["place_streak"] = int(gd_now.get("place_streak") or 1) + 1
                except (TypeError, ValueError):
                    gd_now["place_streak"] = 2
            elif last:
                gd_now["place_streak"] = 1
            gd_now["place"] = spot
        # 时段同步（2026-11-07）：状态行第四段是她**自己交的**「现在几点」。
        # 以前只有第一段（地点）被解析，时段永远是开场时的默认值——玩家看到
        # 深夜的正文、早晨的高亮，就是因为这一格没人写。
        # 兼容旧数据：老状态行只有三段，len < 4 时不动现有的 phase。
        seg = [x.strip() for x in str(state).split("·")]
        if len(seg) >= 4:
            ph = normalize_phase(seg[3])
            if ph in PHASES:
                gd_now = _gameday(st)
                gd_now["phase"] = ph
        # 接触位置（2026-11-07）：第五段，角色 AI 自己交的枚举。
        # 段数不足 5 = 这一轮没写，**保持上一拍不动**——空值不能当成「拔出来了」，
        # 那样一次漏写就会把状态机清零。
        if len(seg) >= 5:
            con = normalize_contact(seg[4])
            if con:
                gd_now = _gameday(st)
                gd_now["contact_prev"] = str(gd_now.get("contact") or "")
                gd_now["contact"] = con
    # 当前服饰（2026-11-09）：独立字段，别混进 `state` 那行中文里——
    # 出图要用的是这串英文定稿，也是下一轮回注给演员的事实。
    if str(outfit or "").strip():
        st["outfit"] = str(outfit).strip()[:300]
    save_state(state_data)
