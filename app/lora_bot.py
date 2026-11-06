# -*- coding: utf-8 -*-
"""QQ 指令 `/角色库`：把当前角色库列成一张聊天记录卡片（2026-11-06）。

用户要的：群里发 `/角色库` → 合并转发卡片，先写支持哪些渠道，再按游戏分组
把 60 个角色列出来。零 LLM、零生图，只读 `config/char_loras.json`。

为什么用合并转发：60 个名字平铺就是几百字，一条消息塞不下也刷屏；
卡片点开才展开（和 `card_bot` 造卡同一个理由）。
"""
import json
import logging
import os
import re

from app import qq_api
from app.config import BASE_DIR

log = logging.getLogger("lora_bot")

INDEX = os.path.join(BASE_DIR, "config", "char_loras.json")

# 必须带斜杠、顶在句首（和 `/画廊` 同理：光「角色库」三个字群里随时会出现）
ROLELIB_RE = re.compile(r"^\s*[/／]\s*角色库\s*$")

CATEGORY_NAME = {"genshin": "原神", "hsr": "星穹铁道", "wuthering": "鸣潮",
                 "endfield": "终末地", "nte": "异环", "zzz": "绝区零"}


def chars():
    try:
        with open(INDEX, encoding="utf-8") as f:
            return (json.load(f).get("characters") or [])
    except Exception:                                  # noqa: BLE001
        log.warning("角色库索引读不了", exc_info=True)
        return []


def channels_text():
    """支持哪些渠道 —— 从条目里取（所有条目同一套），取不到就给个兜底。"""
    cs = chars()
    ch = (cs[0].get("channels") if cs else None) or []
    return ch or ["silver"]


def _bot_identity():
    try:
        from app.card_bot import _bot_identity as ident
        return ident()
    except Exception:                                  # noqa: BLE001
        return ("大大怪", "10000")


def build_nodes():
    """标题节点 + 每个游戏一个节点。"""
    name, uin = _bot_identity()
    cs = chars()
    chans = channels_text()
    head = ("📚 角色库 · 共 %d 个角色\n"
            "支持渠道：%s\n"
            "用法：说「anima 千夏」「silver 胡桃」这样点名字；不提渠道就跑 silver。"
            % (len(cs), " / ".join(chans)))
    out = [qq_api.node_segment(head, name, uin)]

    groups = {}
    for c in cs:
        groups.setdefault(str(c.get("category") or "其他"), []).append(c)
    order = ["genshin", "hsr", "wuthering", "endfield", "nte", "zzz"]
    for key in order + [k for k in groups if k not in order]:
        items = groups.get(key) or []
        if not items:
            continue
        title = CATEGORY_NAME.get(key, key)
        lines = ["【%s】%d 个" % (title, len(items))]
        for c in items:
            nm = str(c.get("name") or c.get("key"))
            extra = ""
            if c.get("trigger"):
                extra = "（触发词 %s）" % c["trigger"]
            lines.append("· %s%s" % (nm, extra))
        out.append(qq_api.node_segment("\n".join(lines), name, uin))
    return out


def plain_text():
    """发不出卡片时的纯文本兜底。"""
    cs = chars()
    by = {}
    for c in cs:
        by.setdefault(str(c.get("category") or "其他"), []).append(
            str(c.get("name") or c.get("key")))
    parts = ["📚 角色库 · 共 %d 个（渠道：%s）"
             % (len(cs), " / ".join(channels_text()))]
    for k, names in by.items():
        parts.append("%s：%s" % (CATEGORY_NAME.get(k, k), "、".join(names)))
    return "\n".join(parts)


def send(target, target_id):
    """发卡片；失败退回纯文本。返回是否发出去了。"""
    nodes = build_nodes()
    try:
        qq_api.send_forward(target, target_id, nodes)
        return True
    except Exception:                                  # noqa: BLE001
        log.warning("角色库卡片发送失败，退纯文本", exc_info=True)
    try:
        sender = (qq_api.send_group if target == "group"
                  else qq_api.send_private)
        sender(target_id, plain_text())
        return True
    except Exception:                                  # noqa: BLE001
        log.exception("角色库纯文本也发不出去")
        return False
