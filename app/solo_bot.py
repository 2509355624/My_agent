# -*- coding: utf-8 -*-
"""QQ 指令 `#游客模式`：把独立游客体验页的地址发成一张聊天记录卡片。

2026-11-06 用户要一个「用于分享的、完全单独的游客界面」，群里或私聊发
`#游客模式` 就给链接。地址每次**现读** `friend_site.url()`——Quick Tunnel
重启就换域名，缓存或硬编旧地址发出去只会得到 530。

卡片沿用 `/角色库` 那套合并转发（点开才展开，收起来只有一行）；协议端偶发
不支持合并转发时退纯文本，不能让一句回话凭空消失。
"""
import logging
import re

from app import qq_api

log = logging.getLogger("solo_bot")

# 「#」+「游客模式」已经够独特，不锚定行首：「大大怪 #游客模式」也要认
# （/角色库 踩过锚定行首漏判、整轮漏给生图管道的坑，见 lora_bot.py）。
SOLO_RE = re.compile(r"[#＃]\s*游客模式")
SOLO_PATH = "/play"


def _bot_identity():
    try:
        from app.card_bot import _bot_identity as ident
        return ident()
    except Exception:                                  # noqa: BLE001
        from app.config import QQ_BOT_NAME
        return (QQ_BOT_NAME, "10000")


def link_text():
    """当前独立游客页地址；隧道没开 → ""（不猜旧域名）。"""
    from app import friend_site
    u = friend_site.url()
    return (u.rstrip("/") + SOLO_PATH) if u else ""


def card_parts():
    """卡片的几条「消息」。链接单独一条，点开后方便长按复制。"""
    link = link_text()
    head = "🎭 游客模式 · 独立体验页"
    if not link:
        return [head,
                "群友页的隧道现在没开，所以没有地址可发。\n"
                "让群主双击「启动群友隧道.bat」，起来之后再发一次「#游客模式」就行。"]
    return [head,
            link,
            "点开就是角色扮演，不用登录、不用验证码，直接说话就能聊。\n"
            "体验版 30 轮，固定一个角色，纯聊天；这一页里没有画廊和登录入口。\n"
            "Quick Tunnel 每次重启会换域名，链接打不开就再发一次「#游客模式」要新的。"]


def plain_text():
    return "\n".join(card_parts())


def build_nodes():
    name, uin = _bot_identity()
    return [qq_api.node_segment(p, name, uin) for p in card_parts()]


def send(target, target_id):
    """发卡片；失败退纯文本。返回是否发出去了。"""
    try:
        qq_api.send_forward(target, target_id, build_nodes())
        return True
    except Exception:                                  # noqa: BLE001
        log.warning("游客模式卡片发送失败，退纯文本", exc_info=True)
    try:
        sender = (qq_api.send_group if target == "group"
                  else qq_api.send_private)
        sender(target_id, plain_text())
        return True
    except Exception:                                  # noqa: BLE001
        log.exception("游客模式纯文本也发不出去")
        return False
