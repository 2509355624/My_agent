"""群名 / 私聊人名解析缓存。

给状态后台把 `群 1041446471` 显示成 `群 我的群`，`私聊 546587874` 显示成
`私聊 某人`。本来每次显示都调 NapCat 一遍是浪费，而且 `get_group_info`
按群号单查更贵 —— 改用两个一次性接口：

- `get_group_list` ：机器人**所有**已加入的群，附带群名。一次拿全。
- `get_friend_list`：所有好友，附带昵称/备注。

私聊昵称额外走一条捷径：**消息事件里 sender.card/nickname 是免费的**，
非好友也能拿到，所以在 `QQBot._dispatch` 里直接 `note_private` 进缓存
—— 不依赖 NapCat。

## 设计要点
- **缓存持久化**到 `state/qq_names.json`，重启不丢（跟 `qq_status` 一样）。
- **自动限速**：后台每 2 秒调一次 `name_for`，但 `refresh_lists` 内部判断距上次
  刷新是否过 10 分钟，没过直接返回，NapCat 不会被刷爆。
- **失败保守**：NapCat 不在线时 `refresh_lists` 静默失败，原缓存保留 —— 状态
  后台继续用 ID 显示，不该因为「取不到名字」整个卡住。
"""

import json
import logging
import os
import threading
import time

from app.config import BASE_DIR

log = logging.getLogger("qq_names")

PATH = os.path.join(BASE_DIR, "state", "qq_names.json")

# 距上次刷新多久内不重拉。群/朋友列表变动极少，1 小时更稳；这里取 10 分钟
# 兼顾「刚加的新群尽快看得到」。
REFRESH_INTERVAL = 600

_lock = threading.Lock()
_group = {}
_private = {}
_last_refresh = 0.0


def _load():
    global _group, _private
    try:
        with open(PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
        _group = {str(k): v for k, v in (d.get("group") or {}).items()}
        _private = {str(k): v for k, v in (d.get("private") or {}).items()}
        if _group or _private:
            log.info("加载名字缓存：%d 群、%d 私聊",
                     len(_group), len(_private))
    except (OSError, ValueError):
        pass


def _persist():
    try:
        os.makedirs(os.path.dirname(PATH), exist_ok=True)
    except OSError:
        pass
    try:
        tmp = PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"group": _group, "private": _private}, f,
                      ensure_ascii=False)
        os.replace(tmp, PATH)
    except OSError as exc:
        log.debug("名字缓存写盘失败：%s", exc)


def note_private(user_id, name):
    """从消息事件记一个私聊名字。非好友也行——不需要 NapCat 介入。

    QQBot._dispatch 处理 private 消息时调一下。这条路径是**主入口**，
    get_friend_list 只是兜底（只覆盖好友）。
    """
    if not name or not user_id:
        return
    user_id = str(user_id)
    with _lock:
        if _private.get(user_id) == name:
            return
        _private[user_id] = name
    _persist()


def refresh_lists(force=False):
    """从 NapCat 拉一次全量列表。限速：默认 10 分钟一次，force=True 不限。

    失败保持原缓存不动；返回值告诉调用方这次是否真拉了。
    """
    global _last_refresh
    now = time.time()
    with _lock:
        if not force and (now - _last_refresh) < REFRESH_INTERVAL:
            return False
        _last_refresh = now

    new_group = {}
    new_private = {}
    try:
        from app import qq_api
        for g in qq_api.get_group_list():
            gid = str(g.get("group_id") or "")
            nm = (g.get("group_name") or "").strip()
            if gid and nm:
                new_group[gid] = nm
    except Exception as exc:
        log.warning("get_group_list 失败（保留原缓存）：%s", exc)
        return False
    try:
        from app import qq_api
        for u in qq_api.get_friend_list():
            uid = str(u.get("user_id") or "")
            nm = (u.get("remark") or u.get("nickname") or "").strip()
            if uid and nm:
                new_private[uid] = nm
    except Exception as exc:
        log.warning("get_friend_list 失败（保留原缓存）：%s", exc)
        return False

    with _lock:
        _group.update(new_group)
        _private.update(new_private)
    _persist()
    log.info("名字缓存刷新：%d 群、%d 私聊", len(_group), len(_private))
    return True


def name_for(target, tid):
    """查群名/私聊名。找不到返回 None，不触发网络（避免状态后台卡住）。

    刷新由调用方（qq_status 写盘循环）定期驱动的 refresh_lists 负责。
    """
    if not tid:
        return None
    tid = str(tid)
    with _lock:
        if target == "group":
            return _group.get(tid)
        if target == "private":
            return _private.get(tid)
    return None


# 模块一被 import 就把磁盘缓存读进来 —— qq_bot / qq_status 谁先起来谁触发。
_load()
