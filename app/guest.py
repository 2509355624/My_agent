"""游客会话的存储与限额（2026-11-03）。

## 为什么单独一个模块

登录用户的历史走 `persona` 那套（`agents/qq/sessions/persona_*.jsonl`），
游客走这里——**两套完全分开**：

- 游客数据不该混进登录用户的目录，清理时没法一刀切
- 游客 24 小时后就该没了，登录用户要长期保留
- 游客上限 30 轮，这个计数只有游客需要

用户原话：「游客单独给我弄一个游客-角色扮演的文件夹，我有时间也会自己去
手动清洗」。所以这里**只负责写、不负责删**，最多在启动时点名超期的目录。

## 目录形态

    agents/qq/游客-角色扮演/
        2026-11-03/
            3f2a9c11-....jsonl      ← 一个游客一条线
            8b7e0d42-....jsonl
        2026-11-04/
            ...

按天分目录：想删旧的，整个目录删掉就行，不用一个个看时间。

## 身份

游客没有 QQ 号，用一个**服务端签名的 UUID** 认人：

    <uuid>.<exp>.<hmac>

签名复用 `friend_auth.secret()`——和登录 cookie 同一把密钥，改不了。
清 cookie / 换浏览器 / 无痕窗口会重置，这是**已知且接受**的：体验版的意义
是让人尝到甜头，不是防盗版。
"""
import hashlib
import hmac
import json
import logging
import os
import re
import time
import uuid as uuid_mod

from .config import BASE_DIR

log = logging.getLogger("guest")

# 游客能聊几轮。**按 AI 回复次数算**（用户 2026-11-03 定），不是按消息条数：
# 一问一答算 1 轮，发三句算 1 轮。
GUEST_TURNS = 30

# 游客对话留多久（秒）。**不做自动删除**——用户要手动清，这里只在启动时
# 点名超期的目录。留着这个常量是为了让「多久算过期」可查、可改。
GUEST_TTL = 24 * 3600

# 根目录
GUEST_ROOT = os.path.join(BASE_DIR, "agents", "qq", "游客-角色扮演")

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def _secret():
    """复用登录那套密钥。import 放在函数里，避免循环依赖。"""
    from . import friend_auth
    return friend_auth.secret()


def _sign(payload):
    return hmac.new(_secret().encode(), payload.encode(), hashlib.sha256).hexdigest()[:32]


def issue_id():
    """新的游客 id（带签名）。"""
    uid = str(uuid_mod.uuid4())
    return "%s.%s" % (uid, _sign(uid))


def check_id(value):
    """验签名并返回裸 uuid；无效返回 ""。"""
    raw = str(value or "").strip()
    if "." not in raw:
        return ""
    uid, sig = raw.rsplit(".", 1)
    if not _UUID_RE.match(uid):
        return ""
    if not hmac.compare_digest(_sign(uid), sig):
        return ""
    return uid


def _day_dir(day=None):
    return os.path.join(GUEST_ROOT, day or time.strftime("%Y-%m-%d"))


def hist_path(uid, day=None):
    """这个游客今天的历史文件。**按天分目录**，跨天就是新文件。"""
    return os.path.join(_day_dir(day), "%s.jsonl" % uid)


def load_hist(uid):
    """读这个游客的全部历史。

    跨天的情况：昨天的文件也要读，否则游客过了零点就「失忆」了。
    从新到旧扫，凑够 `HISTORY_LIMIT` 条就停。
    """
    if not uid:
        return []
    files = []
    if os.path.isdir(GUEST_ROOT):
        for d in sorted(os.listdir(GUEST_ROOT), reverse=True):
            p = os.path.join(GUEST_ROOT, d, "%s.jsonl" % uid)
            if os.path.isfile(p):
                files.append(p)
    out = []
    for p in files:                     # 新的在前
        try:
            with open(p, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        try:
                            out.append(json.loads(line))
                        except ValueError:
                            pass
        except OSError:
            log.exception("游客历史读不出来：%s", p)
    # files 是倒序拼的，需要还原成时间正序
    return out


def append_hist(uid, msgs):
    """追加几条消息到今天这个游客的文件里。"""
    if not uid or not msgs:
        return
    d = _day_dir()
    os.makedirs(d, exist_ok=True)
    p = hist_path(uid)
    try:
        with open(p, "a", encoding="utf-8") as fh:
            for m in msgs:
                fh.write(json.dumps(m, ensure_ascii=False) + "\n")
    except OSError:
        log.exception("游客历史写不进去：%s", p)


def turns_used(uid):
    """已经用掉几轮 = 历史里有几条 assistant 回复。"""
    return sum(1 for m in load_hist(uid) if m.get("role") == "assistant")


def remaining(uid):
    return max(0, GUEST_TURNS - turns_used(uid))


def expired_dirs(now=None):
    """列出超过 `GUEST_TTL` 的日期目录名（只列，不删）。

    用户要手动清，所以这里只给一个「哪些该清了」的清单，启动时打一行日志。
    """
    now = now or time.time()
    out = []
    if not os.path.isdir(GUEST_ROOT):
        return out
    for d in os.listdir(GUEST_ROOT):
        p = os.path.join(GUEST_ROOT, d)
        if not os.path.isdir(p):
            continue
        try:
            mtime = os.path.getmtime(p)
        except OSError:
            continue
        if now - mtime > GUEST_TTL:
            n = len(os.listdir(p))
            out.append((d, n, now - mtime))
    return sorted(out)


def log_status():
    """启动时打一行「游客目录现在什么样」。不删任何东西。"""
    if not os.path.isdir(GUEST_ROOT):
        return
    old = expired_dirs()
    total = 0
    for d in os.listdir(GUEST_ROOT):
        p = os.path.join(GUEST_ROOT, d)
        if os.path.isdir(p):
            total += len(os.listdir(p))
    if not total and not old:
        return
    log.info("[guest] 游客会话 %d 个文件，根目录 %s", total, GUEST_ROOT)
    for d, n, age in old:
        log.info("[guest] 超过 24 小时：%s（%d 个文件，%.1f 小时）——该目录可手动删除",
                 d, n, age / 3600.0)
