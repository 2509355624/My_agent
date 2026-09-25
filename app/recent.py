"""群消息滚动缓存：让机器人在被 @ 时看得见群里刚才在聊什么。

为什么需要它：qq_bot 原先收到群消息、不 @ 机器人就整个丢掉（`_should_reply`
判定不通过直接 return），于是模型每轮的信息量只有「有人问了它一句」。它不
知道前因后果，只能一问一答，看起来就是个问答助手而不是群里的一个人。
这里把群消息一律记下来，被叫到时把最近一段当背景一起送去。

存哪：agents/<agent_id>/recent/<key>.jsonl，每行一条
    {"t": 时间戳, "u": QQ号, "n": 昵称, "x": 正文}

为什么落盘而不是纯内存：进程重启（改代码、掉线重连）后上下文不至于全丢，
不必等群消息重新攒起来。

怎么读：一直按行 append，读的时候取末尾若干行；文件长到 MAX_LINES 行时整体
重写一次、只留最近 KEEP_LINES 条。不是每写一条就重写整个文件——那点 IO 在
群聊刷屏时不便宜。
"""

import json
import logging
import os
import time

from app import agents as agent_store

log = logging.getLogger("recent")

# 触发裁剪的行数 / 裁剪后保留的行数
MAX_LINES = 400
KEEP_LINES = 200


def _dir(agent_id):
    aid = agent_store.safe_agent_id(agent_id) or "main"
    return os.path.join(agent_store.AGENTS_DIR, aid, "recent")


def _path(agent_id, group_id):
    """缓存文件路径；群号非法时返回空串。

    群号是外部输入（来自 QQ 事件）且会被拼进文件名，所以复用会话 key 那套
    字符白名单，穿越不了目录。
    """
    key = agent_store.safe_session_key("group_" + str(group_id))
    if not key:
        return ""
    return os.path.join(_dir(agent_id), key + ".jsonl")


def remember(agent_id, group_id, name, text, user_id="", when=None):
    """记一条群消息，返回是否写入成功。空正文直接跳过。

    正文里的换行会被压成空格：每行一条记录，正文带换行会让文件本身变成
    不可解析的（读回来是一堆半截 JSON）。纯图消息由调用方给 "[图片]" 之类
    的占位文案——群里的图也是群聊的一部分，缺了上下文会看起来断片。
    """
    body = " ".join((text or "").split())
    if not body:
        return False
    path = _path(agent_id, group_id)
    if not path:
        log.debug("群号非法，不记群聊缓存：%s", group_id)
        return False

    rec = {
        "t": int(when if when is not None else time.time()),
        "u": str(user_id or ""),
        "n": str(name or ""),
        "x": body,
    }
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError as exc:
        # 缓存写失败不该影响聊天本身，记一条警告就放过去
        log.warning("写群聊缓存失败 %s：%s", path, exc)
        return False
    _trim(path)
    return True


def _trim(path):
    """行数超标时重写一次，只留最近 KEEP_LINES 条。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return
    if len(lines) <= MAX_LINES:
        return
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.writelines(lines[-KEEP_LINES:])
    except OSError as exc:
        log.warning("裁剪群聊缓存失败 %s：%s", path, exc)


def load_recent(agent_id, group_id, limit):
    """取最近 limit 条，按时间正序返回。读不到返回空列表。"""
    path = _path(agent_id, group_id)
    if not path or limit <= 0:
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return []

    out = []
    for line in lines[-limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue        # 半截行（上次写入被打断）跳过，别带崩整段背景
        if isinstance(rec, dict) and rec.get("x"):
            out.append(rec)
    return out


def _render(rec):
    who = rec.get("n") or rec.get("u") or "某人"
    return "%s：%s" % (who, rec.get("x", ""))


def format_recent(agent_id, group_id, limit, max_chars):
    """渲染成给模型看的群聊背景；没有可用内容时返回空串（调用方据此跳过）。

    max_chars 是从最新往前累计的字数闸：群聊刷屏时不封顶会把预算吃光。装不
    下就丢掉更旧的——近的比远的要紧，与 _merge_batch 同一个取舍。
    """
    if limit <= 0 or max_chars <= 0:
        return ""
    msgs = load_recent(agent_id, group_id, limit)
    if not msgs:
        return ""

    picked, total = [], 0
    for rec in reversed(msgs):
        line = _render(rec)
        if picked and total + len(line) > max_chars:
            break
        picked.append(line)
        total += len(line)
    picked.reverse()
    return "[群里最近的对话]\n" + "\n".join(picked)
