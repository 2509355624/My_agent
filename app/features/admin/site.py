# -*- coding: utf-8 -*-
"""管理页网关（5176）的公网地址。

和 `friend_site.py` 同一套约定：隧道地址每次重启都换，代码不会知道，所以
`启动管理页隧道.bat`（→ `friend_tunnel.py`，FRIEND_TUNNEL_URL_FILE=admin_url.txt）
拿到地址那一刻写进 `state/admin_url.txt`，这里只负责「读 + 校验」。

和群友页地址**分开两个文件**是有意的：两条 Cloudflare 隧道各自重启、各自
换地址，共用一份文件会互相覆盖（见 c944d17）。读不到就是隧道没开，不猜、
不编、更不拿上一次的旧地址顶上——旧地址早就 1033 了。
"""

import logging
import re

from app.config import state_path

log = logging.getLogger("admin_site")

# 与 friend_site 同一条校验：只认 https 公网地址。文件可能被手改，
# 一条 file:/// 或局域网地址发给站主，只会得到一个打不开的链接。
_URL_RE = re.compile(r"^https://[A-Za-z0-9.\-]+(?::\d+)?(?:/[^\s]*)?$")


def url():
    """当前管理页公网地址；隧道没开 / 文件里不是合法地址 → ""。"""
    try:
        with open(state_path("admin_url.txt"), encoding="utf-8") as f:
            u = f.read().strip()
    except OSError:
        return ""
    if not u:
        return ""
    if not _URL_RE.match(u):
        log.warning("admin_url.txt 里不是 https 地址，忽略：%.80r", u)
        return ""
    return u.rstrip("/")
