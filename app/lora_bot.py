# -*- coding: utf-8 -*-
"""QQ 指令 `/角色库`：把当前角色库列成一张聊天记录卡片（2026-11-06）。

用户要的：群里发 `/角色库` → 合并转发卡片，先写支持哪些渠道，再按游戏分组
把角色列出来。零 LLM、零生图，只读 `config/char_loras.json`。

为什么用合并转发：几十个名字平铺就是几百字，一条消息塞不下也刷屏；
卡片点开才展开（和 `card_bot` 造卡同一个理由）。

2026-11-06 改：角色库按底模架构分了组（anima / SDXL / SD1.5 / moss），同一个
角色可能有多条记录（各挂各的架构）。卡片里**同一个 key 合并成一行**，后面
标它覆盖哪几个架构——不然「千夏」会出现两次，看着像重复。
"""
import json
import logging
import os
import re

from app import char_loras, qq_api
from app.config import BASE_DIR

log = logging.getLogger("lora_bot")

INDEX = os.path.join(BASE_DIR, "config", "char_loras.json")

# 只要句子里出现「/角色库」就算，**不要求顶在句首**（2026-11-06 实测：
# 用户发的是「大大怪 /角色库」，前面带名字，锚定行首的正则直接漏给了生图管道，
# 机器人回了一句「这是个指令，我画不了」）。斜杠 + 四个字本身已经够独特，
# 不会在闲聊里误撞。
ROLELIB_RE = re.compile(r"[/／]\s*角色库")

# 分组顺序（索引里的 category 就是这些中文名）
ORDER = ["鸣潮", "异环", "星穹铁道", "终末地", "绝区零", "原神",
         "蔚蓝档案", "碧蓝航线", "Vocaloid", "AI拟人", "晴雅"]

# 老条目写的是英文 slug，新条目写中文——归一，否则「千夏」会同时出现在
# 「绝区零」和「zzz」两组里，同一张卡看着像两个人。
CATEGORY_ALIAS = {"genshin": "原神", "hsr": "星穹铁道", "wuthering": "鸣潮",
                  "endfield": "终末地", "nte": "异环", "zzz": "绝区零"}


def _cat_of(entry):
    raw = str(entry.get("category") or "其他")
    return CATEGORY_ALIAS.get(raw, raw)

# 架构标签：条目属于哪一组渠道
ARCH_GROUPS = (
    ("anima", char_loras._LORA_SKILLS_ANIMA),
    ("SDXL", char_loras._LORA_SKILLS_SDXL),
    ("SD1.5", char_loras._LORA_SKILLS_SD15),
    ("moss", char_loras._LORA_SKILLS_MOSS),
)


def chars():
    try:
        with open(INDEX, encoding="utf-8") as f:
            return (json.load(f).get("characters") or [])
    except Exception:                                  # noqa: BLE001
        log.warning("角色库索引读不了", exc_info=True)
        return []


def arch_of(entry):
    """这条 LoRA 属于哪个架构组。"""
    ch = set(entry.get("channels") or [])
    for label, skills in ARCH_GROUPS:
        if ch & set(skills):
            return label
    return "?"


def channels_text():
    """按架构列出支持哪些渠道。"""
    return " / ".join("%s %d 个" % (label, len(skills))
                      for label, skills in ARCH_GROUPS)


def head_text():
    cs = chars()
    keys = {str(c.get("key")) for c in cs}
    return ("📚 角色库 · %d 个角色（%d 条记录）\n"
            "按底模分渠道：%s\n"
            "用法：说「anima 千夏」「miao 今汐」「折枝」这样点名字；"
            "不提渠道就跑默认，认得的角色会自动挂 LoRA。"
            % (len(keys), len(cs), channels_text()))


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
    out = [qq_api.node_segment(head_text(), name, uin)]

    groups = {}
    for c in cs:
        cat = _cat_of(c)
        key = str(c.get("key") or c.get("name"))
        groups.setdefault(cat, {}).setdefault(key, []).append(c)

    for cat in ORDER + [c for c in groups if c not in ORDER]:
        acts = groups.get(cat) or {}
        if not acts:
            continue
        lines = ["【%s】%d 个" % (cat, len(acts))]
        for key, items in acts.items():
            c = items[0]
            nm = str(c.get("name") or key)
            tag = " / ".join(dict.fromkeys(arch_of(it) for it in items))
            lines.append("· %s[%s]%s" % (
                nm, tag,
                "（触发词 %s）" % c["trigger"] if c.get("trigger") else ""))
        out.append(qq_api.node_segment("\n".join(lines), name, uin))
    return out


def plain_text():
    """发不出卡片时的纯文本兜底。分组规则必须和卡片一致，否则同一个人在
    卡片上标 [anima / SDXL]、在纯文本里只标一个架构。"""
    cs = chars()
    groups = {}
    for c in cs:
        cat = _cat_of(c)
        key = str(c.get("key") or c.get("name"))
        groups.setdefault(cat, {}).setdefault(key, []).append(c)
    parts = ["📚 " + head_text().split("\n", 1)[0].lstrip("📚 ")]
    for cat in ORDER + [x for x in groups if x not in ORDER]:
        acts = groups.get(cat) or {}
        if not acts:
            continue
        names = []
        for key, items in acts.items():
            nm = str(items[0].get("name") or key)
            tag = "/".join(dict.fromkeys(arch_of(it) for it in items))
            names.append("%s[%s]" % (nm, tag))
        parts.append("%s：%s" % (cat, "、".join(names)))
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
