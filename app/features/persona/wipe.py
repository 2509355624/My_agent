"""删档：抹掉一条线的记忆，保留角色卡本身。"""

import logging
import os

from app.features.persona import affection, pregnancy
from app.features.persona import rag as persona_rag

log = logging.getLogger("persona")


def erase(session_key):
    """把这条线的存档整份抹掉，**保留角色卡本身**。

    抹四样：
      1. 历史文件 `sessions/persona_<key>.jsonl`（整份删掉）；
      2. 归档 `sessions/persona_archive_<key>.jsonl`（同删，否则网页还能翻出来）；
      3. 状态里的 `note` / `turns` / `mood` / `intensity` / `state`；
      4. 好感记录整份换新（`affection.wipe_numbers` → 回到**这张卡的起点**，
         成就、结局、连续天数、机会次数一起清）。

    不动 `card`：要删的是这段关系，不是这个人。想换角色发另一个 `#角色名`，
    想退出扮演发 `#出戏`。

    历史文件删不掉也照常往下走，只在日志里记一笔 —— 命令失败不该让用户卡在
    那儿干等。

    返回角色名；这条线没在演任何人时返回 None。
    """
    from app.features.persona import service as svc

    state = svc._load_state()
    ent = state.get(session_key)
    if not isinstance(ent, dict):
        return None
    card = svc.get_card(str(ent.get("card") or ""))
    name = svc.char_name(card, key=session_key)

    # 1) 历史：整份删。删失败就清空成空文件，至少别让下一轮读到旧内容。
    #    QQ 私聊分线（2026-11-03）：传 base 键时先换算成当前角色线——
    #    删的是**现在演的这个角色**的存档，不碰别的线。
    line = svc.resolve_line(session_key)
    path = svc._hist_path(line)
    if path and os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            log.exception("清空历史失败 %s", path)
            try:
                from app.core.agents import _atomic_write_text
                _atomic_write_text(path, "")
            except Exception:
                log.exception("清空历史时连写空文件都失败 %s", path)

    # 2) 归档：**必须一起删**（2026-11-04）。网页回看历史改成读归档之后，
    #    只删窗口的话「清空存档」就变成假删——聊天记录还能整段翻出来。
    ap = svc._archive_path(line)
    if ap and os.path.exists(ap):
        try:
            os.remove(ap)
        except OSError:
            log.exception("清空归档失败 %s", ap)

    # 2.5) 长期记忆向量库（2026-11-05）：只删**这一条线**的 collection，
    #      别人的用户/角色不受影响。后台队列执行；这期间该线检索被 blocked，
    #      不会把刚清掉的旧回忆又翻出来。
    try:
        persona_rag.drop_line(line)
    except Exception:                                    # noqa: BLE001
        log.exception("清空回忆库失败 %s", line)

    # 3) 好感：交给 affection 整份换新，它自己读盘写盘。
    #    必须在下面读 `_entry` **之前**做 —— 反过来的话，这层拿的是旧记录，
    #    最后整份 `_save_state` 会把刚换新的好感又盖回旧值。
    affection.wipe_numbers(line)

    # 4) 扮演态：保留 card，其余归零
    ent = svc._entry(line)
    if not isinstance(ent, dict):
        return None
    ent.update({"note": "", "turns": 0, "mood": "", "intensity": 0, "state": "",
                "intro": ""})     # 开场导读一起清：下一屏要按新起点重铺
    # 只清 note/turns 是不够的：`turn_total` 还挂在旧线上，存档列表会
    # 继续显示「21 轮」；`note_due` 还停在 5，下一轮立刻触发一次旧账
    # 压缩；`gameday` 还停在旧日期。清空存档 = 这条线的记忆全部归零。
    for k in ("turn_total", "note_due", "note_at", "gameday", "gameday_prev",
              "mood_prev", "preg_prev", "outfit"):
        ent.pop(k, None)
    pregnancy.clear(ent)
    state = svc._load_state()
    state[line] = ent
    if line != session_key:
        # 输入是 base：base 上的副本也刷新，管理页/状态查询别显示旧小记
        state[session_key] = dict(ent)
    keys = [line] if line == session_key else [line, session_key]
    # `turn_total` 的合并规则是「只许前进」；不把旧键从盘上 drop 掉，
    # 刚归零的 0 会被旧值顶回去，存档列表还显示旧轮次。
    svc._save_state(state, deleted=keys)
    log.info("存档已删除：%s（保留角色 %s）", session_key, ent.get("card"))
    return name


def clear(session_key):
    """把这条线的记忆清干净，**保留当前角色**（网页端「清空记忆」按钮走这里）。

    2026-11-05 起 QQ 侧**不再暴露成命令**（用户实测「清空」无确认直接删档，
    不可接受）。QQ 的命令入口统一走 `wipe_request` 的两步确认；这个函数
    只留给明确点了按钮的网页端。
    """
    name = erase(session_key)
    if name is None:
        return "现在没有在演谁，没什么可清的。发「#角色名」挑一个吧。"
    return "记忆清空了，就当今天第一次见面。还是%s，想换人就发「#角色名」。\n" % name


def wipe(session_key):
    """「清空存档」执行体：真删。

    2026-11-04 用户拍板：老版本这里叫「重新开始」，但只清数值、聊天记忆留着，
    玩家删完发现她还记得上周聊的，觉得「没惩罚」。所以改成真删，并且
    改名「清空存档」+ 二次确认（见 `wipe_request`）。
    """
    name = erase(session_key)
    if name is None:
        return None           # 没在演谁：当普通消息
    return ("🚨 存档已删除。\n"
            "你们的聊天记录、好感、关系进度、成就、连续天数全部清掉了，"
            "一切回到这张卡的最初。\n"
            "她还在这儿 —— 想继续就跟她说话，想换人发「#角色名」。")


def wipe_slot(session_key):
    """删一个存档槽：把这条线的东西**全部**清掉，不留旧记忆。

    2026-11-19 用户报：删存档后开新对话，第一轮里却出现旧存档的
    「晚饭 / 红酒 / 洗澡 / 第二天」。查到 `/api/persona/slot_wipe`
    只 `pop` 了 `persona_sessions.json` 里的状态条目，历史文件、归档
    文件和 RAG collection 都还在；新档复用同一个线键，旧内容就被
    重新读回来了。

    这里不依赖状态条目是否还在：文件按路径删，RAG 按线键 drop，
    好感按线键重置，最后再 `pop` 状态。三样都清，才算真的删档。
    """
    from app.features.persona import service as svc

    line = svc.resolve_line(session_key)

    # 1) 历史 + 归档：按路径直接删；删不掉就记日志，不让删档卡住。
    for maker in (svc._hist_path, svc._archive_path):
        try:
            path = maker(line)
            if path and os.path.exists(path):
                os.remove(path)
        except OSError:
            log.exception("删存档时清文件失败 %s", line)

    # 2) RAG：按线键删整个 collection；入队是异步的，route 这边不等待。
    try:
        persona_rag.drop_line(line)
    except Exception:                                    # noqa: BLE001
        log.exception("删存档时清回忆库失败 %s", line)

    # 3) 好感：回到这张卡的起点；万一这条线已经没了，wipe_numbers 自己会返回 None。
    try:
        affection.wipe_numbers(line)
    except Exception:                                    # noqa: BLE001
        log.exception("删存档时清好感失败 %s", line)

    # 4) 状态条目：整条 pop 掉，存档列表才会显示「空存档」。
    state = svc._load_state()
    state.pop(line, None)
    # 不传 deleted 的话，`_save_state` 会把盘上刚 pop 掉的键合并回来，
    # 删档等于没删（2026-11-19 实测：删完新开，旧 note 又回来）。
    svc._save_state(state, deleted=[line])
    log.info("存档槽已删除：%s", line)
    return line
