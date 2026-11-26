# -*- coding: utf-8 -*-
"""访客模式：5175 登录页点「先逛逛」直接进来，能生图但每天 5 张。

## 为什么不是「第二种登录」

访客没有 QQ 号、不进好友白名单、不发验证码。5175 在 `/login/visitor` 里
直接签一个随机 id 塞进 `fr_visit` cookie，**证明它自己就是 5175 签的**
（HMAC，密钥就是 `friend_auth.secret()`）——客户端自己编一个 id 是不行的，
否则清空 cookie 换个新 id 就能无限刷额度这事变成「改一个字符」而不是
「清一次缓存」。

## 额度记在哪

`state/visitor.json` 里按 **cookie 里的身份串**（`id` 那一段，不是整条
cookie——整条含过期时间，续签一次就换 key，额度会跟着重置）记
`{"day": "2026-11-26", "used": 3}`。

按天（**北京时间零点**）重置。用户 2026-11-26 拍板：「5 张够了，换浏览器
就随便他换呗」——所以不锁 IP、不做设备指纹。

## 5174 怎么信这个身份

5175 转发访客请求时带两个头：

    X-Friend-Token   内部令牌（和 `comic_quota.friend_qq` 同一把密钥）
    X-Friend-Guest   访客身份串（`id.exp.sig`）

5174 侧 `is_guest()` **两者都验**：只有令牌对、没有访客头 = 站主直连或群友
登录，一律按原样放行；令牌对但访客头签名不对 = 伪造，**当没带**（不报错，
免得给了探测口径）。这个顺序和 `comic_quota.friend_qq` 完全一致，418 那一侧
不存在第二条鉴权规则。

## 绝不抛错

它挂在转发热路径上：账本坏了、磁盘满了，只能当作「额度照算、落盘失败」
继续走，不能让访客点一下「画」就 500。
"""

import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time

from app.config import state_path

log = logging.getLogger("visitor")

COOKIE_NAME = "fr_visit"
COOKIE_TTL = 24 * 3600      # 用户 2026-11-26 定的 24 小时（隧道会重启，长了没意义）
IMG_PER_DAY = 5             # 每天几张免费图（用户定的 5 张）
# 一次连发最多几张（工作台「直达」按钮，2026-11-26）。用户在 233 粉丝群
# 看到的现象是「同一提示词连跑 5 张，出来 5 张都不一样」——直达就是让他
# 一次把 5 张排满，每张不同 seed。数字跟 QQ/工作台的 MAX_INFLIGHT 对齐。
BATCH_MAX = 5

# 访客能调哪些接口：**不在这里判**。
#
# 2026-11-26 用户把边界划死了：「访客模式只局限于工作台，其他的东西不要给
# 我变」。原先这里有一张路径白名单，5175 见到访客 cookie 就把白名单外的
# 一律 403——结果画廊、漫画台、角色扮演、小说、跑图台全被这条挡成「访客
# 模式用不了这个功能」，主业务跟着塌。白名单是「按哪个路径」判，判不出
# 「这是谁在调」；真正能判的是**发起方**，所以现在只有 5175 的工作台入口
# 才把访客身份带过去（`friend_app._guest_here()`），其余页面这里根本没
# 人来问。

_FILE = state_path("visitor.json")
_LOCK = threading.RLock()
_CACHE = None               # 延迟加载的账本镜像
_SECRET_CACHE = None


# ─── 身份串 ──────────────────────────────────────────────

def _secret():
    """复用登录那份密钥：两个进程已经共用它，不必再落一份。"""
    from app.gateways import friend_auth
    return friend_auth.secret()


def _sign(gid, exp):
    msg = "visit.%s.%s" % (gid, exp)
    return hmac.new(_secret().encode("utf-8"), msg.encode("utf-8"),
                    hashlib.sha256).hexdigest()[:32]


def new_id():
    """签一个新访客身份：`<随机>.<过期时间>.<签名>`。"""
    gid = secrets.token_hex(8)
    exp = int(time.time() + COOKIE_TTL)
    return "%s.%d.%s" % (gid, exp, _sign(gid, exp))


def check_id(value):
    """→ 身份串里的 `id` 段；无效/过期/签名不对一律 None。"""
    raw = str(value or "").strip()
    parts = raw.split(".")
    if len(parts) != 3:
        return None
    gid, exp_s, sig = parts
    if not gid or len(gid) < 8:
        return None
    try:
        exp = int(exp_s)
    except (TypeError, ValueError):
        return None
    if exp < time.time():
        return None
    try:
        good = _sign(gid, exp)
    except Exception:                                   # noqa: BLE001
        return None
    return gid if hmac.compare_digest(good, sig) else None


# ─── 账本 ────────────────────────────────────────────────

def _today():
    """北京时间的那一天。额度按这个日期翻页，不跟机器时区走。"""
    try:
        from zoneinfo import ZoneInfo
        from datetime import datetime
        return datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")
    except Exception:                                   # noqa: BLE001
        # 没装 tzdata 的机器退回本地时区。差 8 小时只影响重置时刻，不影响限额。
        return time.strftime("%Y-%m-%d")


def _data():
    global _CACHE
    with _LOCK:
        if _CACHE is not None:
            return _CACHE
        try:
            with open(_FILE, encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, ValueError):
            raw = None
        if not isinstance(raw, dict) or not isinstance(raw.get("guests"), dict):
            raw = {"guests": {}}
        _CACHE = raw
        return _CACHE


def _save():
    """原子落盘；失败只警告（额度在内存里照样生效）。"""
    with _LOCK:
        try:
            d = os.path.dirname(_FILE)
            if d:
                os.makedirs(d, exist_ok=True)
            tmp = _FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(_data(), f, ensure_ascii=False, indent=1)
            os.replace(tmp, _FILE)
            return True
        except OSError:
            log.warning("访客账本写不进去（内存计数仍生效）", exc_info=True)
            return False


def _row(gid, day):
    """取这个访客今天的计数行；跨天就开新的一行（旧行直接丢，账本不会长）。"""
    g = _data()["guests"]
    row = g.get(gid)
    if not isinstance(row, dict) or row.get("day") != day:
        row = {"day": day, "used": 0}
        g[gid] = row
    try:
        row["used"] = int(row.get("used") or 0)
    except (TypeError, ValueError):
        row["used"] = 0
    return row


def remaining(gid):
    """今天还剩几张。认不出身份就按 0 算（宁可少给，不让它白跑）。"""
    gid = str(gid or "")
    if not gid:
        return 0
    with _LOCK:
        used = _row(gid, _today())["used"]
    return max(0, IMG_PER_DAY - used)


def consume(gid, n=1):
    """扣 `n` 张。→ (扣成功?, 剩余)。不够就**一张都不扣**，避免扣到负数。"""
    gid = str(gid or "")
    if not gid:
        return False, 0
    with _LOCK:
        row = _row(gid, _today())
        if row["used"] + n > IMG_PER_DAY:
            return False, max(0, IMG_PER_DAY - row["used"])
        row["used"] += n
        left = IMG_PER_DAY - row["used"]
        _save()
    return True, left


def refund(gid, n=1):
    """退还（出图失败时用）。只减不增到负数。"""
    gid = str(gid or "")
    if not gid:
        return
    with _LOCK:
        row = _row(gid, _today())
        row["used"] = max(0, row["used"] - n)
        _save()


def snapshot(gid):
    """给前端看的一份状态。"""
    gid = str(gid or "")
    left = remaining(gid)
    return {"limit": IMG_PER_DAY, "left": left, "used": IMG_PER_DAY - left}


def reset():
    """清空内存镜像（测试用；不动磁盘）。"""
    global _CACHE
    with _LOCK:
        _CACHE = None


# ─── 5174 侧的识别 ───────────────────────────────────────

def current():
    """这次请求是不是访客转发来的 → 身份串 `id`；不是/伪造 → ""。

    必须先验内部令牌：`X-Friend-Guest` 这个头的签名密钥虽然只有本机
    两个进程知道，但「知道密钥」这件事和「知道令牌」是同一件事——不先卡
    令牌，就等于把签名校验当成了鉴权，口径和 `comic_quota.friend_qq` 不齐。

    `raw`（2026-11-26 补）：没登录时聊天记录不存服务端（用户：「访客模式
    不需要给他存聊天记录」），前端本地那份缓存要换个键，不能跟这台设备上
    上一个真登录的人共用一份——所以把整条身份串回给前端当键用。
    """
    try:
        from flask import request
        good = _secret()
        got = str(request.headers.get("X-Friend-Token") or "")
        if not got or not hmac.compare_digest(got, good):
            return _NONE
        raw = str(request.headers.get("X-Friend-Guest") or "")
    except Exception:                                   # noqa: BLE001
        return _NONE
    return {"id": check_id(raw) or "", "raw": raw}


# 不是访客时返回的那个（只读，别改它）。用 dict 而不是空串：`current()["id"]`
# 在「不是访客」这条最常见的路上不该抛 TypeError。
_NONE = {"id": "", "raw": ""}


# ─── 5175 侧：已经不在这里判了 ───────────────────────────
#
# 原先这里有 `path_allowed()` / `comic_allowed()` 两张白名单，5175 一见访客
# 就把名单外的请求 403 掉。问题是它们只看路径、不看是谁在调：工作台和画廊
# 用的是同一批 `/api/gal/*`，跑图台和角色扮演各有自己的前缀——按路径判，
# 结果就是「把工作台的规则扣到了所有页面头上」，主业务被一个展示用的开关
# 拖塌（2026-11-26 用户报的原话：「我群聊艾特大大怪没有任何的回应」）。
#
# 现在改为在 5175 判**发起方**（`friend_app._guest_here()`）：只有工作台页面
# 发出来的请求才带 `X-Friend-Guest`，其余页面这里连问都不会被问。所以这两
# 个函数整个删掉，别让以后又有人拿它当「访客能不能用 X」的总闸。
