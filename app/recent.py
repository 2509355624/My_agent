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
from app import longterm

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


def remember(agent_id, group_id, name, text, user_id="", when=None, image=""):
    """记一条群消息，返回是否写入成功。空正文直接跳过。

    正文里的换行会被压成空格：每行一条记录，正文带换行会让文件本身变成
    不可解析的（读回来是一堆半截 JSON）。纯图消息由调用方给 "[图片]" 之类
    的占位文案——群里的图也是群聊的一部分，缺了上下文会看起来断片。

    image 是这条消息带的图片地址（多张取第一张）：上下文里图只渲染成
    "[图片]" 占位符，但地址留在记录里，接话时要「看最近那张图」就靠它。
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
    if image:
        rec["m"] = str(image)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError as exc:
        # 缓存写失败不该影响聊天本身，记一条警告就放过去
        log.warning("写群聊缓存失败 %s：%s", path, exc)
        return False
    _trim(path, agent_id, group_id)
    return True


def _archive_path(path):
    """裁剪时丢弃行的去处：recent/archive/<key>_<日期>.jsonl。

    按天一个文件（文件名带日期，每条记录自带时间戳），攒下来是以后做长期
    记忆 / 训练数据的原料——滚动缓存只服务「刚才在聊什么」，历史在这里。
    """
    base = os.path.splitext(os.path.basename(path))[0]
    name = "%s_%s.jsonl" % (base, time.strftime("%Y-%m-%d"))
    return os.path.join(os.path.dirname(path), "archive", name)


def _trim(path, agent_id="", group_id=""):
    """行数超标时重写一次，只留最近 KEEP_LINES 条。

    丢掉的行先追加进 archive（追加失败只记警告，不影响缓存本身重写）——
    这些是真实群聊记录，留着以后翻旧账。

    agent_id/group_id 用来触发长期记忆摘要（longterm.digest_async，后台
    线程，不阻塞这里）——归档就是「这批消息从缓存毕业」的时刻，顺手让它
    变成一条记忆。
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return
    if len(lines) <= MAX_LINES:
        return
    dropped, keep = lines[:-KEEP_LINES], lines[-KEEP_LINES:]
    try:
        os.makedirs(os.path.dirname(_archive_path(path)), exist_ok=True)
        with open(_archive_path(path), "a", encoding="utf-8") as f:
            f.writelines(dropped)
    except OSError as exc:
        log.warning("归档群聊缓存失败 %s：%s", path, exc)
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.writelines(keep)
    except OSError as exc:
        log.warning("裁剪群聊缓存失败 %s：%s", path, exc)
    if dropped:
        try:
            longterm.digest_async(agent_id, group_id, dropped)
        except Exception:
            log.exception("长期记忆摘要任务启动失败")


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


def latest_image(agent_id, group_id, within=12):
    """最近 within 条里最新的一张图片地址；没有返回空串。

    给主动接话用：判断模型在上下文里只看得到「[图片]」占位符，判了「接」
    之后把真正的图捞出来给主模型看——不然它对着看不见的东西只能装懂。
    只往前翻 within 条：太老的图多半已经不是当前话题了。
    """
    for rec in reversed(load_recent(agent_id, group_id, within)):
        if rec.get("m"):
            return rec["m"]
    return ""


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
