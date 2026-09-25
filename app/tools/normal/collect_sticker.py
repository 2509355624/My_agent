"""收表情包工具：别人点名「收这张」时，把最近消息里的图收进库。

图片从哪来：qq_bot 每轮把见过的图（消息本体的 + 引用块里的）按会话记进
stickers 的「最近图片」缓冲（最新在尾）。模型不传参 = 收最近一张；传
数字 n = 收从最新往回数的第 n 张。自动收藏（群图无条件入库）覆盖不到
的场景就是引用旧图——引用里的图只走识图、不进收藏通道，这个工具补上它。

返回文案必须能让模型如实转述：收好了报编号，没收成说原因（重复 / 库满 /
下载失败）。此前自动收藏被上限挡住时是静默的，模型无从得知，虚报过
「加进来了」——本工具的每个失败分支都给了它一句现成的实话。
"""

from app import qq_api, stickers
from app.config import QQ_AGENT_ID


def _collect_sticker(which=""):
    target, target_id = qq_api.current_context()
    if not target:
        return "当前不在 QQ 会话里，收不了表情包。"
    imgs = stickers.recent_images((target, target_id))
    if not imgs:
        return ("最近的消息里没有图，收不了——想收的表情先发出来或"
                "引用一下再说。")

    which = str(which or "").strip()
    if which in ("", "last", "最新", "最近", "这张"):
        idx = len(imgs) - 1
    else:
        try:
            n = int(which)
        except ValueError:
            return ("which 要填数字（从最新往回数：1 = 最近一张，"
                    "2 = 前一张），收到：%s" % which)
        if n < 1 or n > len(imgs):
            return ("最近只有 %d 张图，没有第 %s 张（从最新往回数）。"
                    % (len(imgs), which))
        idx = len(imgs) - n

    url, sender = imgs[idx]
    status, num, msg = stickers.ingest(QQ_AGENT_ID, url, sender)
    if status == "ok":
        return "%s；下轮 [表情包库] 清单里就能看到，想发报编号就行。" % msg
    if status == "dup":
        return "没再收：%s。" % msg
    if status == "cap":
        return "没收进来：%s。可以调 delete_sticker 删一张腾位置。" % msg
    return "没收成：%s" % msg


tool = {
    "name": "collect_sticker",
    "description": (
        "把最近消息里的图收进表情包库。别人发图或引用一张图并让你"
        "「收这张」「加进库里」时调它；不传 which 收最近一张，传 2 收"
        "倒数第二张。收好报编号，没收成如实说原因。"
    ),
    "function": _collect_sticker,
    "parameters": {
        "type": "object",
        "properties": {
            "which": {
                "type": "string",
                "description":
                    "收哪张：不填 = 最近一张；数字 n = 从最新往回数的第 n 张",
            },
        },
        "required": [],
    },
}
