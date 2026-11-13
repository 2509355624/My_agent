# -*- coding: utf-8 -*-
"""QQ 指令 `/玩法`：把当前所有生图渠道列成一张聊天记录卡片（2026-11-12）。

用户要的：群里发 `/玩法` → 合并转发卡片，**画廊链接放最开头**，然后按家族
分组列出每个渠道，配一句「人话」的功效说明（画质 / 快慢 / 适合画什么）。

零 LLM、零生图：渠道清单和功效文案都是这里写死的常量，画廊地址读
`friend_site.url()`（隧道重启就换域名，不能缓存）。

为什么用合并转发：跟 `/角色库` 同一个理由——几十个渠道平铺就是几百字，一条
消息塞不下也刷屏；卡片点开才展开（见 app/lora_bot.py 顶部注释）。

跟 `/角色库` 的区别：`/角色库` 是「读 config/char_loras.json 动态列」，这里
是「写死的精选家族清单」。渠道底层目录有 50+ 个（含测试渠道、参数管理、写作
类），不能 `list_skills()` 全倒出来——那会把 `silverqaq`、`pose_library` 这类
不该给群友看的东西也甩出去。所以这里**手工圈定**用户真正会点名的家族，跟
`direct_gen._FIXED_CHAN_MAP` 的点名词保持一致口径，但只挑有代表性的。
"""
import logging
import re

from app import friend_site, qq_api

log = logging.getLogger("play_bot")

# 只要句子里出现「/玩法」就算，不要求顶在句首（与 /角色库 同口径：用户会
# 发「大大怪 /玩法」这种带名字的，锚行首的正则会漏给生图管道）。斜杠 + 两个字
# 已经够独特，不会在闲聊里误撞。
PLAY_RE = re.compile(r"[/／]\s*玩法")


def _bot_identity():
    """合并转发卡片里每个节点的「发言人」。与 lora_bot 同一套。"""
    try:
        from app.card_bot import _bot_identity as ident
        return ident()
    except Exception:                                  # noqa: BLE001
        return ("大大怪", "10000")


def _gallery_link():
    """画廊链接（隧道地址 + /gallery）。读不到就返回空串，卡片里照实说。"""
    try:
        return friend_site.url()
    except Exception:                                  # noqa: BLE001
        return ""


# ─── 家族清单（手工圈定，人话短句） ─────────────────────────────
# 顺序就是卡片里的展示顺序。每个家族 = (标题, [(点名词, 功效一句话), ...])
# 功效只写「画质 / 快慢 / 适合什么」，不堆像素、步数、LoRA 这些技术细节
# （用户 2026-11-12 拍板：人话短句）。
_FAMILIES = (
    ("默认", [
        ("不提渠道", "silver，厚涂质感、饱和高，出图稳又快，日常首选"),
    ]),
    ("Anima 族（高清三档 + 衍生）", [
        ("三档 / anima", "高清三档，末尾 2x 放大，最清晰，清透素净"),
        ("anima 线条 / 写实 / 肉感 / 鬼针草 / 2s",
         "三档的衍生配方，各自偏线条、写实、肉感等风格"),
        ("随心配", "底模随机选 2 + 画风 LoRA 随机选 5，随机抽卡，点角色照挂"),
    ]),
    ("Qwen 族（自然语言、写实向）", [
        ("qwen", "Qwen-Image 2.1，写实向，直接说白话就行，出图偏慢"),
        ("qwen真人", "qwen 挂真人 LoRA，人像更写实"),
        ("qwen-hd / qwen30步", "qwen 的超清版 / 30 步高步数版，更精细但更慢"),
        ("qwen修复", "引用一张图说「qwen修复」，图生图修图"),
        ("qwen角色设定表", "生成一张 7 分区的角色设定表"),
    ]),
    ("Krea2 族（风格化）", [
        ("米山舞 / 日系", "米山舞、Rella 日系画风"),
        ("亚洲真人 / 宝可梦 / 极光 / 鬼针草 / 铭音",
         "各自挂一条风格 LoRA，二次元或真人风格"),
    ]),
    ("其它", [
        ("miao", "realskin 皮肤质感，SDXL 两段 + 超分"),
        ("nffa", "Illustrious 插画，手和脸单独修复"),
        ("cunny", "cocoStyle 两段超分"),
        ("moss / moss光影", "本地 MOSS 5B，25 步；光影是多 LoRA 组合"),
        ("nai", "NAI 云端渠道"),
        ("半厚涂 / 完整版 / sd", "半厚涂、silver 完整版底模、SD/SDXL（可一次多张）"),
    ]),
)


def head_text():
    """标题节点：画廊链接 + 一句总览。"""
    url = _gallery_link()
    if not url:
        return ("🎮 玩法 · 生图渠道一览\n"
                "画廊在群友页里，隧道现在没开，所以没有地址可发"
                "（Quick Tunnel 每次重启都会换域名）。\n"
                "让群主双击「启动群友隧道.bat」，起来之后就有了。")
    return ("🎮 玩法 · 生图渠道一览\n"
            "画廊：%s/gallery?from=qq\n"
            "不提渠道就跑默认 silver；想换就说下面这些点名，"
            "认得的角色会自动挂 LoRA。" % url)


def build_nodes():
    """标题节点 + 每个家族一个节点。"""
    name, uin = _bot_identity()
    nodes = [qq_api.node_segment(head_text(), name, uin)]
    for title, items in _FAMILIES:
        lines = ["【%s】" % title]
        for kw, desc in items:
            lines.append("· %s —— %s" % (kw, desc))
        nodes.append(qq_api.node_segment("\n".join(lines), name, uin))
    return nodes


def plain_text():
    """发不出卡片时的纯文本兜底，分组规则与卡片一致。"""
    lines = [head_text()]
    for title, items in _FAMILIES:
        lines.append("【%s】" % title)
        for kw, desc in items:
            lines.append("· %s —— %s" % (kw, desc))
    return "\n".join(lines)


def send(target, target_id):
    """发卡片；失败退回纯文本。返回是否发出去了。"""
    nodes = build_nodes()
    try:
        qq_api.send_forward(target, target_id, nodes)
        return True
    except Exception:                                  # noqa: BLE001
        log.warning("玩法卡片发送失败，退纯文本", exc_info=True)
    try:
        sender = (qq_api.send_group if target == "group"
                  else qq_api.send_private)
        sender(target_id, plain_text())
        return True
    except Exception:                                  # noqa: BLE001
        log.exception("玩法纯文本也发不出去")
        return False
