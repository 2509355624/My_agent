# -*- coding: utf-8 -*-
"""管理页公网网关的登录鉴权（固定账号密码 + HMAC 签名 Cookie）。

## 为什么单独一套，不复用 friend_auth

`friend_auth` 是群友页那套：QQ 白名单 + 6 位验证码 + 发私聊。管理页是站主
自己看的，用户拍板「固定账号密码、最稳」，不走 QQ、不依赖机器人在线。
两者凭据不能混：群友的 cookie 打不开管理页，管理页的 cookie 也进不了群友页。

## 密码绝不进仓库

仓库是 public。`state/admin_auth.json` 在 .gitignore 里，由 `tools/admin_passwd.py`
生成，只存 PBKDF2 加盐哈希，不存明文。代码只读、只校验，不内置任何账号密码。
没生成过这个文件就无人能登录（fail-closed），必须先跑脚本建号。

## Cookie

照 `friend_auth` 的模式：`user.exp.sig`，签名 = HMAC-SHA256(secret, "user.exp")。
`secret` 落在 `state/admin_secret`，谁先启动谁生成，重启后仍认旧 cookie。
"""

import hashlib
import hmac
import json
import logging
import os
import secrets
import time

from app.config import state_path

log = logging.getLogger("admin_auth")

COOKIE_NAME = "ad_sess"
COOKIE_TTL = 12 * 3600          # 登录态保 12 小时（2026-11-27 从 7 天收紧）

_AUTH = state_path("admin_auth.json")
_SECRET = state_path("admin_secret")

_PBKDF2_ROUNDS = 200000

_secret_cache = None


def _now():
    return time.time()


def secret():
    """签名密钥。落盘 state/，谁先启动谁生成。"""
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


def _hash_password(password, salt):
    return hashlib.pbkdf2_hmac(
        "sha256", str(password).encode("utf-8"), salt.encode("utf-8"),
        _PBKDF2_ROUNDS).hex()


def set_password(user, password):
    """建号 / 改密。由 tools/admin_passwd.py 调用。"""
    salt = secrets.token_hex(16)
    data = {
        "user": str(user or "").strip(),
        "salt": salt,
        "hash": _hash_password(password, salt),
    }
    d = os.path.dirname(_AUTH)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = _AUTH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, _AUTH)
    try:
        os.chmod(_AUTH, 0o600)
    except OSError:
        pass
    # 改了密码就把旧 cookie 全废掉：换 secret，已发的签名立刻失效。
    global _secret_cache
    _secret_cache = None
    try:
        with open(_SECRET, "w", encoding="utf-8") as f:
            f.write(secrets.token_hex(32))
        try:
            os.chmod(_SECRET, 0o600)
        except OSError:
            pass
    except OSError:
        log.warning("换密钥失败，旧 cookie 仍可能有效", exc_info=True)
    return data["user"]


def configured():
    """是否已经建号。"""
    try:
        with open(_AUTH, encoding="utf-8") as f:
            data = json.load(f)
        return bool(data.get("user") and data.get("hash") and data.get("salt"))
    except (OSError, ValueError):
        return False


def verify_password(user, password):
    """→ (ok, message)。没建号 / 账号不符 / 密码错都拒绝。"""
    try:
        with open(_AUTH, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return False, "还没设置管理员账号，请先运行初始化脚本"
    if not (data.get("user") and data.get("hash") and data.get("salt")):
        return False, "还没设置管理员账号，请先运行初始化脚本"
    if not hmac.compare_digest(str(data["user"]), str(user or "").strip()):
        return False, "账号或密码不对"
    good = _hash_password(password, data["salt"])
    if not hmac.compare_digest(good, str(data["hash"])):
        return False, "账号或密码不对"
    return True, ""


def _sign(user, exp):
    msg = "%s.%d" % (user, exp)
    return hmac.new(secret().encode("utf-8"), msg.encode("utf-8"),
                    hashlib.sha256).hexdigest()[:32]


def issue_cookie(user):
    exp = int(_now() + COOKIE_TTL)
    return "%s.%d.%s" % (user, exp, _sign(user, exp))


def check_cookie(value):
    """→ 用户名；无效 / 过期一律 None。"""
    raw = str(value or "").strip()
    parts = raw.split(".")
    if len(parts) != 3:
        return None
    user, exp_s, sig = parts
    if not user:
        return None
    try:
        exp = int(exp_s)
    except ValueError:
        return None
    if exp < _now():
        return None
    try:
        good = _sign(user, exp)
    except Exception:
        return None
    return user if hmac.compare_digest(good, sig) else None
