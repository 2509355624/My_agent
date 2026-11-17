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
    pregnancy.clear(ent)
    state = svc._load_state()
    state[line] = ent
    if line != session_key:
        # 输入是 base：base 上的副本也刷新，管理页/状态查询别显示旧小记
        state[session_key] = dict(ent)
    svc._save_state(state)
    log.info("存档已删除：%s（保留角色 %s）", session_key, ent.get("card"))
    return name
