# -*- coding: utf-8 -*-
"""「看已隐藏」的 6 位密码门。

## 为什么单独一套，不复用 admin_auth

`admin_auth` 是管理页的固定账号密码 + 长期 cookie（登录一次保 7 天）。
这里要的是**每次点进隐藏墙都重新输一次**——面试时把手机递出去，
对方就算翻到「看已隐藏」也进不去；登录态留着就等于没设。所以这里
只校验一次、不发 cookie、不记会话，验完即弃。

## 密码绝不进仓库

仓库是 public。`state/gallery_pin.json` 在 .gitignore 里，由
`tools/gallery_pin.py` 生成，只存 PBKDF2 加盐哈希，不存明文。代码只读、
只校验，不内置任何密码。没生成过这个文件就谁也进不去（fail-closed）。

## 限流

6 位数字只有 100 万种，挡不住脚本狂试。连续错 5 次锁 10 分钟，
和 friend_auth 同一个量级。计数只在内存里：进程重启会清零，
但那要能重启 5174 才行，不是递手机的人做得到的。
"""

import hashlib
import hmac
import json
import logging
import os
import threading
import time

from app.config import state_path

log = logging.getLogger("gallery_pin")

_AUTH = state_path("gallery_pin.json")
_ROUNDS = 200000
_TRY_MAX = 5
_LOCK_SECONDS = 600

_lock = threading.Lock()
_cache = None                   # {"salt":..., "hash":...}，延迟加载
_fails = 0
_locked_until = 0.0


def _load():
    """读哈希文件。缺文件 / 坏 JSON 一律当「没设密码」。"""
    global _cache
    if _cache is not None:
        return _cache
    try:
        with open(_AUTH, encoding="utf-8") as f:
            d = json.load(f)
        if d.get("salt") and d.get("hash"):
            _cache = d
            return _cache
    except (OSError, ValueError):
        pass
    return None


def configured():
    """是否已经设过密码。没设 = 隐藏墙任何人进不去。"""
    return _load() is not None


def _hash(pin, salt):
    return hashlib.pbkdf2_hmac(
        "sha256", str(pin).encode("utf-8"), salt.encode("utf-8"),
        _ROUNDS).hex()


def set_pin(pin):
    """设 / 改密码。由 tools/gallery_pin.py 调用，只写哈希。"""
    global _cache, _fails, _locked_until
    import secrets
    salt = secrets.token_hex(16)
    data = {"salt": salt, "hash": _hash(pin, salt)}
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
    _cache = data
    # 改密码就清掉旧的错误计数，别让人刚改完还被锁着
    with _lock:
        _fails = 0
        _locked_until = 0.0
    return True


def locked_left():
    """被锁时还剩几秒。没被锁返回 0。"""
    with _lock:
        return max(0, int(_locked_until - time.time()))


def verify(pin):
    """校验一次密码。返回 (是否通过, 剩余锁定秒数)。

    通过就清错误计数；不通过累计，到 5 次锁 10 分钟。
    """
    global _fails, _locked_until
    left = locked_left()
    if left:
        return False, left
    data = _load()
    if not data:
        return False, 0                 # 没设密码，fail-closed
    good = hmac.compare_digest(_hash(pin, data["salt"]), data["hash"])
    with _lock:
        if good:
            _fails = 0
            _locked_until = 0.0
            return True, 0
        _fails += 1
        if _fails >= _TRY_MAX:
            _locked_until = time.time() + _LOCK_SECONDS
            log.warning("隐藏墙密码连续错 %d 次，锁 %d 分钟",
                        _TRY_MAX, _LOCK_SECONDS // 60)
            return False, _LOCK_SECONDS
        return False, 0
