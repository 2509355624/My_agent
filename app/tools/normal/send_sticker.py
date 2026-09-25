"""发表情包工具：从收藏库里按情绪/场景挑一张，发到当前会话。

表情包从哪来：qq_bot 每轮自动收藏群里的小图/GIF（见 app/stickers.py），
识图模型打了情绪标签。模型想活跃气氛时用 query 说想要什么（"大笑""无语"），
这里挑最匹配的一张用本地文件直发——不经过外链，不怕图床链接过期。

注意：工具名刻意不是 send_qq_message——那会让适配层以为"本轮已自己发过"
而吞掉正文回复。这里只发图，正文照常走自动回发，图 + 文字各一条消息。
"""

import os

from app import qq_api, stickers
from app.config import QQ_AGENT_ID


def _send_sticker(query, target=None, target_id=None):
    cur_target, cur_id = qq_api.current_context()

    target = (target or "").strip().lower() or None
    if target_id not in (None, ""):
        try:
            target_id = int(target_id)
        except (TypeError, ValueError):
            return "target_id 必须是数字，收到：" + str(target_id)

    if target is None and target_id is None:
        target, target_id = cur_target, cur_id
    else:
        target = target or cur_target
        target_id = target_id if target_id is not None else cur_id

    if not target or target_id is None:
        return ("没有指定发送目标，且当前不在 QQ 会话中，无法发送。"
                "请一并提供 target（group / private）与 target_id。")
    if target not in ("group", "private"):
        return "target 只能是 group 或 private，收到：" + str(target)

    rec = stickers.pick(QQ_AGENT_ID, query or "")
    if not rec:
        return ("表情包库还是空的——平时群里有人发小图/GIF 会自动收藏，"
                "攒几张之后就能甩了。")

    file_uri = "file:///" + stickers.abs_path(
        QQ_AGENT_ID, rec).replace("\\", "/").lstrip("/")
    seg = {"type": "image", "data": {"file": file_uri}}
    try:
        if target == "group":
            qq_api.send_group(target_id, [seg])
        else:
            qq_api.send_private(target_id, [seg])
    except Exception as e:
        return "发送失败：" + str(e)
    tags = "、".join(rec.get("tags") or []) or "无标签"
    return "已发表情包（标签：%s）" % tags


tool = {
    "name": "send_sticker",
    "description": (
        "发一个表情包。表情包来自平时自动收藏的群里好图（库存上限 100 张），"
        "已按情绪打好标签。query 填想表达的情绪或场景（如 大笑/无语/摸鱼），"
        "填「随便」就随机来一张；标签没对上时也会随机兜底，当抽卡就好。"
        "一张图配一句短话最自然，别一口气连发三张以上。"
    ),
    "function": _send_sticker,
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "想要的情绪或场景关键词，如「大笑」「无语」；「随便」= 随机",
            },
            "target": {
                "type": "string",
                "enum": ["group", "private"],
                "description": "群聊用 group，私聊用 private；不填则用当前会话",
            },
            "target_id": {
                "type": "integer",
                "description": "群号或 QQ 号；不填则用当前会话",
            },
        },
        "required": ["query"],
    },
}
