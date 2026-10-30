# -*- coding: utf-8 -*-
"""群友页登录：QQ 好友白名单 + 6 位验证码 + HMAC 签名 Cookie。

## 为什么先查好友列表，而不是「发不出去就说不是好友」

`qq_api.send_private` 有**群临时会话兜底**（见 `_call_private`）：非好友只要
曾经从某个群发起过临时会话，照样能收到消息。拿「发消息成功」当好友判据，
等于把陌生人放进来。所以这里走 `get_friend_list()`，不在列表里连码都不发。

## 为什么要落盘

发码和验码是两个独立 HTTP 请求，验证码必须跨请求存活；进程重启也不能把
正在输码的人踢出去。落在 `state/friend_auth.json`（`state/` 在 .gitignore 里）。

## 为什么 secret 单独一个文件

`app/friend_api.py` 跑在 5174 主进程里，登录发生在 5175（`friend_app.py`）。
两边要共用一个签名密钥才能互认，所以密钥单独放 `state/friend_secret`，
谁先启动谁生成，后来者只读。

## 绝不抛错

它在登录热路径上。磁盘满、JSON 被手改坏，都只能退回内存，不能让登录页 500。
"""

import hmac
import hashlib
import json
import logging
import os
import re
import secrets
import threading
import time

from app.config import state_path

log = logging.getLogger("friend_auth")

CODE_TTL = 300              # 验证码有效期：5 分钟
CODE_COOLDOWN = 60          # 同一个号两次发码的最小间隔
CODE_PER_HOUR = 5           # 同一个号每小时最多发几次（防拿别人号刷）
TRY_MAX = 5                 # 连续错几次就锁
LOCK_SECONDS = 600          # 锁多久
COOKIE_NAME = "fr_sess"
COOKIE_TTL = 7 * 86400      # 登录状态保 7 天

QQ_RE = re.compile(r"^[1-9][0-9]{4,11}$")   # QQ 号：5~12 位，不以 0 开头

_AUTH = state_path("friend_auth.json")
_SECRET = state_path("friend_secret")

_lock = threading.Lock()
_state = None               # 延迟加载，见 _data()
_secret_cache = None

# 好友列表缓存：NapCat 每次都拉一遍太慢，60 秒内复用。
# **缓存为空不代表「没有好友」**——拉取失败也是空，所以另外记 `ok` 位，
# 只有 ok 才允许用它做「不是好友」的判定（fail-closed）。
_friends = {"ts": 0.0, "ids": set(), "ok": False}
_friends_lock = threading.Lock()


# ─── 落盘 ────────────────────────────────────────────────

def _blank():
    return {"codes": {}, "sent": {}, "fails": {}}


def _data():
    """读一次缓存在内存里。坏文件当空账（绝不抛）。"""
    global _state
    if _state is not None:
        return _state
    try:
        with open(_AUTH, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        raw = None
    if not isinstance(raw, dict):
        raw = _blank()
    for key in ("codes", "sent", "fails"):
        if not isinstance(raw.get(key), dict):
            raw[key] = {}
    _state = raw
    return _state


def _save():
    """原子落盘。失败只警告——登录不能因为磁盘满就挂掉。"""
    try:
        d = os.path.dirname(_AUTH)
        if d:
            os.makedirs(d, exist_ok=True)
        tmp = _AUTH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_data(), f, ensure_ascii=False)
        os.replace(tmp, _AUTH)
        return True
    except OSError:
        log.warning("登录账本写不进去（内存计数仍生效）", exc_info=True)
        return False


def secret():
    """签名密钥。两个进程共用同一个文件，谁先到谁生成。"""
    global _secret_cache
    if _secret_cache:
        return _secret_cache
    try:
        with open(_SECRET, encoding="utf-8") as f:
            got = f.read().strip()
        if len(got) >= 32:
            _secret_cache = got
            return got
    except OSError:
        pass
    got = secrets.token_hex(32)
    try:
        d = os.path.dirname(_SECRET)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(_SECRET, "w", encoding="utf-8") as f:
            f.write(got)
        try:
            os.chmod(_SECRET, 0o600)
        except OSError:
            pass
    except OSError:
        log.warning("密钥落盘失败，本进程内临时生效", exc_info=True)
    _secret_cache = got
    return got


# ─── 好友白名单 ──────────────────────────────────────────

def _friend_ids(force=False):
    """→ (set, ok)。ok=False 表示这次没拿到（NapCat 不在线等），调用方必须拒绝。"""
    now = time.time()
    with _friends_lock:
        if not force and _friends["ok"] and now - _friends["ts"] < 60:
            return set(_friends["ids"]), True
    try:
        from app import qq_api
        rows = qq_api.get_friend_list()
    except Exception:
        log.warning("拉好友列表失败", exc_info=True)
        rows = None
    if rows is None:
        return set(), False
    ids = set()
    for r in rows:
        try:
            uid = r.get("user_id") if isinstance(r, dict) else None
            if uid is not None:
                ids.add(str(int(uid)))
        except (TypeError, ValueError):
            continue
    with _friends_lock:
        _friends.update({"ts": now, "ids": ids, "ok": True})
    log.info("好友白名单已刷新：%d 人", len(ids))
    return ids, True


def is_friend(qq):
    """→ (真/假, 错误说明)。拿不到名单时一律 (False, ...)，fail-closed。"""
    qq = str(qq or "").strip()
    if not QQ_RE.match(qq):
        return False, "QQ 号格式不对"
    ids, ok = _friend_ids()
    if not ok:
        return False, "暂时连不上机器人，稍后再试"
    if qq not in ids:
        return False, "这个号不在大大怪的好友列表里"
    return True, ""


# ─── 发码 / 验码 ─────────────────────────────────────────

def _now():
    return time.time()


def _locked(qq):
    """这个号现在是不是被锁着？→ 剩余秒数。"""
    row = _data()["fails"].get(qq) or {}
    until = float(row.get("until") or 0)
    left = until - _now()
    return int(left) if left > 0 else 0


def _bump_fail(qq):
    row = _data()["fails"].setdefault(qq, {})
    n = int(row.get("n") or 0) + 1
    row["n"] = n
    if n >= TRY_MAX:
        row["until"] = _now() + LOCK_SECONDS
        row["n"] = 0
        log.warning("群友登录：%s 连续错 %d 次，锁 %d 分钟", qq, TRY_MAX,
                    LOCK_SECONDS // 60)
    _save()


def _clear_fail(qq):
    if _data()["fails"].pop(qq, None) is not None:
        _save()


def send_code(qq):
    """给这个 QQ 发 6 位码。→ (ok, 给人看的一句话)。

    顺序刻意是「查锁 → 查好友 → 查冷却 → 发」，不是「先生成码再校验」：
    被锁的号不该触发任何一次 QQ 发信。
    """
    qq = str(qq or "").strip()
    if not QQ_RE.match(qq):
        return False, "QQ 号格式不对"
    with _lock:
        left = _locked(qq)
        if left:
            return False, "试错太多次，%d 分钟后再来" % (left // 60 + 1)
    ok, why = is_friend(qq)
    if not ok:
        return False, why
    now = _now()
    with _lock:
        st = _data()
        sent = [t for t in (st["sent"].get(qq) or [])
                if isinstance(t, (int, float)) and now - t < 3600]
        if sent and now - sent[-1] < CODE_COOLDOWN:
            return False, "%d 秒后才能再发一次" % int(CODE_COOLDOWN - (now - sent[-1]))
        if len(sent) >= CODE_PER_HOUR:
            return False, "这一小时发太多次了，歇一会儿"
        code = "%06d" % secrets.randbelow(1000000)
        st["codes"][qq] = {"code": code, "exp": now + CODE_TTL}
        sent.append(now)
        st["sent"][qq] = sent
        _save()
    text = ("【生图页登录】验证码 %s，5 分钟内有效。\n"
            "不是你自己在登录就直接忽略，别把码给别人。" % code)
    try:
        from app import qq_api
        qq_api.send_private(int(qq), text)
    except Exception as e:
        log.warning("验证码私聊发送失败 %s：%s", qq, e)
        with _lock:
            _data()["codes"].pop(qq, None)
            _save()
        return False, "验证码发不出去，可能是机器人掉线了"
    log.info("群友登录：已给 %s 发验证码", qq)
    return True, "验证码已发到你的 QQ"


def verify(qq, code):
    """校验码。→ (ok, 说明, cookie 值)。成功才给 cookie。"""
    qq = str(qq or "").strip()
    code = str(code or "").strip()
    if not QQ_RE.match(qq) or not code.isdigit():
        return False, "填的东西不对", None
    with _lock:
        left = _locked(qq)
        if left:
            return False, "试错太多次，%d 分钟后再来" % (left // 60 + 1), None
        row = _data()["codes"].get(qq)
        if not row:
            return False, "先点「获取验证码」", None
        if float(row.get("exp") or 0) < _now():
            _data()["codes"].pop(qq, None)
            _save()
            return False, "验证码过期了，重新获取一个", None
        if not hmac.compare_digest(str(row.get("code") or ""), code):
            _bump_fail(qq)
            return False, "验证码不对", None
        _data()["codes"].pop(qq, None)
        _save()
    _clear_fail(qq)
    log.info("群友登录：%s 验证通过", qq)
    return True, "", issue_cookie(qq)


# ─── Cookie ─────────────────────────────────────────────

def _sign(qq, exp):
    msg = "%s.%s" % (qq, exp)
    return hmac.new(secret().encode("utf-8"), msg.encode("utf-8"),
                    hashlib.sha256).hexdigest()[:32]


def issue_cookie(qq):
    exp = int(_now() + COOKIE_TTL)
    return "%s.%d.%s" % (qq, exp, _sign(qq, exp))


def check_cookie(value):
    """→ QQ 号；无效/过期一律 None。"""
    raw = str(value or "").strip()
    parts = raw.split(".")
    if len(parts) != 3:
        return None
    qq, exp_s, sig = parts
    if not QQ_RE.match(qq):
        return None
    try:
        exp = int(exp_s)
    except ValueError:
        return None
    if exp < _now():
        return None
    try:
        good = _sign(qq, exp)
    except Exception:
        return None
    return qq if hmac.compare_digest(good, sig) else None


def reset():
    """清空内存镜像（测试用；不动磁盘）。"""
    global _state, _secret_cache
    with _lock:
        _state = None
    with _friends_lock:
        _friends.update({"ts": 0.0, "ids": set(), "ok": False})
    _secret_cache = None
