"""主动发送 QQ 消息工具

两种用法：

1. 不带 target / target_id —— 发回当前正在对话的那个会话。模型想「补一句」
   或追发一张图时可以用（正常回复由适配层自动发，多数情况不需要调它）。
2. 带 target + target_id —— 发到任意指定的好友或群。这是「主动推送」场景：
   定时任务、告警、把结果同步到别的群。

注意：本轮一旦调用过本工具，适配层就不再自动回发正文（见 app/qq_bot.py），
否则同一句话会被发两遍。
"""

from app import qq_api


def _send_qq_message(message, target=None, target_id=None):
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
        # 只给了一半时，用当前会话补另一半（有上下文才能补）
        target = target or cur_target
        target_id = target_id if target_id is not None else cur_id

    if not target or target_id is None:
        return ("没有指定发送目标，且当前不在 QQ 会话中，无法发送。"
                "请一并提供 target（group / private）与 target_id。")
    if target not in ("group", "private"):
        return "target 只能是 group 或 private，收到：" + str(target)

    text = (message or "").strip()
    if not text:
        return "message 不能为空"

    try:
        if target == "group":
            sent = qq_api.send_group(target_id, text)
        else:
            sent = qq_api.send_private(target_id, text)
    except Exception as e:
        return "发送失败：" + str(e)
    return "已发送给 %s %s，共 %d 条" % (target, target_id, sent)


tool = {
    "name": "send_qq_message",
    "description": (
        "主动发送 QQ 消息。不填 target/target_id 时发回当前正在对话的会话；"
        "填了则发到指定的好友或群（用于主动推送、通知、把结果同步到别的群）。"
    ),
    "function": _send_qq_message,
    "parameters": {
        "type": "object",
        "properties": {
            "message": {
                "type": "string",
                "description": "要发送的消息内容",
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
        "required": ["message"],
    },
}
