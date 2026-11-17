# -*- coding: utf-8 -*-
"""群友页（5175）的公网地址。

隧道是 Cloudflare Quick Tunnel：免费、免账号、免域名，代价是**地址每次重启
都换**，而且换的时候代码根本不知道——新地址只印在 cloudflared 自己的窗口里。

所以约定一个文件：`启动群友隧道.bat`（→ `friend_tunnel.py`）在拿到地址的
那一刻写进 `state/friend_url.txt`，这里只负责「读 + 校验」。读不到就是
「隧道没开」，不猜、不编、更不拿上一次的旧地址顶上——旧地址早就 1033 了，
发给群友只会得到一串打不开的链接。
"""

import logging
import re

from app.config import state_path

log = logging.getLogger("friend_site")

# 只认 https 的公网地址。文件是我们自己写的，但可能被手改过；
# 一条 `file:///` 或局域网地址发出去，对方只会以为页面坏了。
_URL_RE = re.compile(r"^https://[A-Za-z0-9.\-]+(?::\d+)?(?:/[^\s]*)?$")


def url():
    """当前隧道地址；隧道没开 / 文件里不是合法地址 → ""。"""
    try:
        with open(state_path("friend_url.txt"), encoding="utf-8") as f:
            u = f.read().strip()
    except OSError:
        return ""
    if not u:
        return ""
    if not _URL_RE.match(u):
        log.warning("friend_url.txt 里不是 https 地址，忽略：%.80r", u)
        return ""
    return u.rstrip("/")
