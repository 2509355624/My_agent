# -*- coding: utf-8 -*-
"""游客会话查看：管理员白名单 + 只读会话数据。

和群友页（`app/friend_auth.py`）的分工：

- **登录复用群友那套**：6 位验证码、5 分钟有效、60 秒冷却、每小时 5 次、
  连错 5 次锁 10 分钟、HMAC 签名 cookie。这里只多一道「这个 QQ 在不在
  管理员名单里」。
- **数据只读**：只列 `guest/` 目录下那批游客会话，不碰网页 / 私聊 / 群聊；
  没有删除、没有角色卡管理、没有背景图上传。

为什么单独一个模块：`friend_app.py`（5175，对外）和 `persona_admin_api.py`
（5174，管理台）都要读这份名单，放谁那儿都会让另一个反向依赖。
"""
import json
import logging
import os
import threading

from app.config import BASE_DIR

log = logging.getLogger("guest_view")

_PATH = os.path.join(BASE_DIR, "state", "guest_admins.json")
_lock = threading.Lock()
_cache = None


def _load():
    global _cache
    if _cache is not None:
        return _cache
    ids = []
    try:
        with open(_PATH, encoding="utf-8") as f:
            d = json.load(f) or {}
        ids = [str(x).strip() for x in (d.get("ids") or []) if str(x).strip()]
    except FileNotFoundError:
        pass
    except Exception:                                    # noqa: BLE001
        log.warning("管理员名单读不了，当作空名单", exc_info=True)
    _cache = set(ids)
    return _cache


def is_admin(qq):
    """这个 QQ 是不是管理员。空号 / 非数字一律 False。"""
    s = str(qq or "").strip()
    if not s or not s.isdigit():
        return False
    return s in _load()


def list_admins():
    return sorted(_load())


def save_admins(ids):
    """覆盖保存。只收纯数字、去重、保序。返回落盘后的列表。"""
    global _cache
    clean = []
    for x in ids or []:
        s = str(x).strip()
        if s and s.isdigit() and s not in clean:
            clean.append(s)
    with _lock:
        os.makedirs(os.path.dirname(_PATH), exist_ok=True)
        with open(_PATH, "w", encoding="utf-8") as f:
            json.dump({"ids": clean}, f, ensure_ascii=False, indent=1)
        _cache = set(clean)
    return clean


def add_admin(qq):
    cur = list_admins()
    s = str(qq or "").strip()
    if not s.isdigit():
        return cur, "QQ 号只能是数字"
    if s in cur:
        return cur, ""
    return save_admins(cur + [s]), ""


def remove_admin(qq):
    s = str(qq or "").strip()
    return save_admins([x for x in list_admins() if x != s])
