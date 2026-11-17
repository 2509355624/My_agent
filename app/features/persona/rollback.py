"""回退上一轮：历史、归档、游戏日、状态、好感一起撤。"""

import json
import logging
import os

from app.features.persona import affection
from app.features.persona import rag as persona_rag

log = logging.getLogger("persona")


def _save_archive(session_key, rows):
    """整份重写归档（回退用）。只有 `rollback_last_turn` 会删归档条目，
    平时只增不删；写法和 `_save_hist` 一致，走原子替换防半截文件。"""
    from app.features.persona import service as svc

    p = svc._archive_path(session_key)
    if not p:
        return
    from app.core.agents import _atomic_write_text
    parent = svc.os.path.dirname(p)
    if parent:
        svc.os.makedirs(parent, exist_ok=True)
    _atomic_write_text(p,
                       "".join(svc.json.dumps(m, ensure_ascii=False) + "\n"
                               for m in rows))


def rollback_last_turn(session_key, sender_uin=""):
    """回退上一轮（2026-11-04）：发错消息的后悔药。

    只支持**一级**——「上一条」的上一条退不了，不是时光机。一轮要退的账
    有五处，这里一次清完：

      1. 历史窗口 + 归档：删掉最后一轮（她的回复 + 你的那句话；
         「睡到明天」的开场只有她一条，就只删一条）；
      2. 好感：`svc.affection.rollback_last` 按结算前快照整体还原；
      3. 游戏日：还原这轮推进前的 `gameday`（跨天/约定也随之退回）；
      4. 心情/状态：还原这轮覆盖前的那份（状态行是下一轮拼头用的）；
      5. 轮次计数 -1（小记压缩的节拍器）。

    返回 dict：`ok` / `error` / 还原后的 mood、intensity、state、day。
    没在演、没有可退的轮，`ok=False` 并带原因——调用方把原因带给用户。
    """
    from app.features.persona import service as svc

    key = svc.resolve_line(svc._persona_key(session_key, sender_uin))
    ent = svc._entry(key)
    if not ent:
        return {"ok": False, "error": "这条线还没有在演的角色"}
    # 只允许一级（2026-11-06 用户要求）：退过一次就必须先出新的一轮，
    # 否则窗口尾部还是「她的一条回复」，会一轮一轮把整条历史拆掉。
    if ent.get("rollback_done"):
        return {"ok": False,
                "error": "刚回退过一次了，先和她重新说一句，才能再回退"}

    # ── 历史 + 归档 ─────────────────────────────────
    hist = svc._load_hist(key)
    if not hist or str((hist[-1] or {}).get("role")) != "assistant":
        return {"ok": False, "error": "没有可回退的轮（最新一条不是她的回复）"}
    n = 2 if (len(hist) >= 2
              and str((hist[-2] or {}).get("role")) == "user") else 1
    tail_pat = [str((m or {}).get("role")) for m in hist[-n:]]
    del hist[-n:]
    svc._save_hist(key, hist)

    # 归档只在**文件真实存在**时动：`load_archive` 对缺失文件会退回窗口副本，
    # 拿副本当账本删会把窗口里更早的轮也误删掉、还落成一个假归档文件。
    # 另外尾部形状要和窗口删掉的那轮一致——不一致说明归档和窗口已经错位
    # （铺底/写失败），宁可不删这条也不能错删。
    arch = svc._read_archive_file(key)
    if arch and [str((m or {}).get("role")) for m in arch[-n:]] == tail_pat:
        del arch[-n:]
        _save_archive(key, arch)
    else:
        svc.log.warning("归档缺失或尾部和窗口对不上，回退只删了窗口 %s", key)

    # ── 游戏日 / 心情状态 / 轮次 ─────────────────────
    gd_prev = ent.pop("gameday_prev", None)
    if isinstance(gd_prev, dict):
        ent["gameday"] = gd_prev
    # 怀孕/孩子跟着这一轮一起退（2026-11-05）：快照里没有 preg = 回退前没怀。
    if "preg_prev" in ent:
        pp = ent.pop("preg_prev")
        if isinstance(pp, dict):
            ent["preg"] = pp
        else:
            ent.pop("preg", None)
    day = svc._gameday(ent).get("day")

    mp = ent.pop("mood_prev", None)
    if isinstance(mp, dict):
        ent["mood"] = str(mp.get("mood") or "")
        ent["intensity"] = mp.get("intensity") or 0
        ent["state"] = str(mp.get("state") or "")
        # 老快照没有这个键，别把新服饰误清成空。
        if "outfit" in mp:
            ent["outfit"] = str(mp.get("outfit") or "")
    mood = str(ent.get("mood") or "")
    intensity = ent.get("intensity") or 0
    state = str(ent.get("state") or "")

    state_map = svc._load_state()
    state_map[key] = ent
    st_turns = int(ent.get("turns") or 0)
    ent["turns"] = max(0, st_turns - 1)      # `_bump_turn` 的逆操作
    if ent.get("turn_total") is not None:
        ent["turn_total"] = max(0, int(ent.get("turn_total") or 0) - 1)
    ent["rollback_done"] = True              # 锁死二级回退，等下一轮解开
    # 放行这一把键的天数倒退（`svc._save_state` 的单调闸门），不然回退会被自己挡住。
    ent["_day_back_ok"] = True
    svc._save_state(state_map)

    # 回退也把这一轮从回忆缓冲/最近一批入库里撤掉（2026-11-05）。
    # 只有「你的话 + 她的回复」这种成对轮才进过 RAG；时段跳跃 / 高潮后那几段
    # 只有她一条（n=1），本来就没记，不能顺手去弹缓冲里更早的一轮。
    if n == 2:
        try:
            svc.persona_rag.forget_last(key)
        except Exception:                                # noqa: BLE001
            svc.log.exception("回退回忆失败 %s", key)

    # ── 好感（快照还原；自由模式没结算过 → None，不算失败）────
    aff = svc.affection.rollback_last(key)

    return {"ok": True, "dropped": n, "aff": aff, "day": day,
            "mood": mood, "intensity": intensity, "state": state}
