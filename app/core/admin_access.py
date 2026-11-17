"""管理面访问控制：本地直达 / 内网放行 / 公网拒绝。

`main.py` 和各个管理 Blueprint 共用同一套判断，避免拆模块后各自复制一份。
"""

import ipaddress

from flask import request

from app.config import ADMIN_ALLOW_REMOTE, ADMIN_ALLOW_REMOTE_LAN

LOCAL_ADDRS = ("127.0.0.1", "::1", "::ffff:127.0.0.1", "localhost")


def is_lan_addr(addr):
    """内网地址判定，用于把 `ADMIN_ALLOW_REMOTE=lan` 的放行范围收在子网内。"""
    if not addr:
        return False
    a = addr[7:] if addr.startswith("::ffff:") else addr
    try:
        ip = ipaddress.ip_address(a)
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback or ip.is_link_local


def admin_allowed():
    if request.remote_addr in LOCAL_ADDRS:
        return True
    if not ADMIN_ALLOW_REMOTE:
        return False
    if ADMIN_ALLOW_REMOTE_LAN:
        return is_lan_addr(request.remote_addr)
    return True
