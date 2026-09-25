"""发表情包工具：模型从每轮注入的 [表情包库] 清单里报编号，按号发图。

表情包从哪来：qq_bot 每轮自动收藏群里的小图/GIF（见 app/stickers.py），
识图模型写了「画面 + 情绪」。整库清单挂在每轮的上下文里（stickers.catalog，
走 extra_context 通道出流即弃），模型自己看清单挑编号报过来——选哪张是
模型的自主决策，这里不做任何标签匹配或随机兜底。

注意：工具名刻意不是 send_qq_message——那会让适配层以为"本轮已自己发过"
而吞掉正文回复。这里只发图，正文照常走自动回发，图 + 文字各一条消息。
"""

from app import qq_api, stickers
from app.config import QQ_AGENT_ID

# 一次调用最多发几张：清单里明说可连报，这里兜一道防刷屏
_MAX_PER_CALL = 3


def _send_sticker(nums, target=None, target_id=None):
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

    picks = stickers.records_by_numbers(QQ_AGENT_ID, nums)
    if not picks:
        return ("没认出有效的表情包编号。看每轮上下文里的 [表情包库] 清单，"
                "填编号数字（如 3，连发填 3,7）。")

    sent, failed = [], []
    for n, rec in picks[:_MAX_PER_CALL]:
        desc = (rec.get("desc") or "").strip()
        if not desc:
            desc = "、".join(rec.get("tags") or []) or "无描述"
        file_uri = "file:///" + stickers.abs_path(
            QQ_AGENT_ID, rec).replace("\\", "/").lstrip("/")
        seg = {"type": "image", "data": {"file": file_uri}}
        try:
            if target == "group":
                qq_api.send_group(target_id, [seg])
            else:
                qq_api.send_private(target_id, [seg])
            sent.append("%d号（%s）" % (n, desc))
        except Exception as e:
            failed.append("%d号：%s" % (n, e))

    if sent and not failed:
        return "已发表情包：%s" % "、".join(sent)
    if sent and failed:
        return "发了 %s；失败：%s" % ("、".join(sent), "；".join(failed))
    return "发送失败：" + "；".join(failed)


tool = {
    "name": "send_sticker",
    "description": (
        "发一个表情包。每轮上下文里的 [表情包库] 清单就是你的全部存货"
        "（上限 50 张，平时自动收藏群里的小图/GIF），看中哪张填哪张的编号"
        "（如 3，连发填 3,7）。很多话不用打字，直接甩一张就是回复；"
        "群友发了好笑的，回敬一张也很好接。"
    ),
    "function": _send_sticker,
    "parameters": {
        "type": "object",
        "properties": {
            "nums": {
                "type": "string",
                "description":
                    "表情包编号，来自 [表情包库] 清单；如「3」，连发「3,7」",
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
        "required": ["nums"],
    },
}
