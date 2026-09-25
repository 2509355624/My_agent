"""删表情包工具：模型自己觉得哪张不好用，按编号删掉。

真人的图库是会清理的——重复收的、当时觉得好笑回头看没劲的、被群友吐槽
"这张别发了"的。让模型自己认，比我们定规则准。

删法（见 app/stickers.py 的 delete）：索引行打 deleted 标记 + 删本地图片
文件，**不删索引行**。编号 = 行号，删行会让后面的号全部前移，模型这轮
记住的号下轮就指错图了。删掉的号永久空着，清单里不再出现。

一次最多删 _MAX_PER_CALL 张：防模型一口气把库清空（删完还得重新攒图）。
"""

import re

from app import stickers
from app.config import QQ_AGENT_ID

# 一次调用最多删几张
_MAX_PER_CALL = 5


def _delete_sticker(nums):
    picked = re.findall(r"\d+", str(nums or ""))[:_MAX_PER_CALL]
    if not picked:
        return ("没认出表情包编号。看每轮上下文里的 [表情包库] 清单，"
                "填要删的编号（如 3，多张填 3,7）。")

    done, skipped = stickers.delete(QQ_AGENT_ID, ",".join(picked))
    if not done:
        return ("这几个编号删不掉（已经删过，或者号对不上）：%s。"
                "看 [表情包库] 清单里还在的编号。"
                % "、".join(str(n) for n in skipped))
    parts = ["已删 %s" % "、".join("%d号（%s）" % (n, d) for n, d in done)]
    if skipped:
        parts.append("没删成：%s" % "、".join(str(n) for n in skipped))
    return "；".join(parts)


tool = {
    "name": "delete_sticker",
    "description": (
        "删掉表情包库里不想要的图。每轮上下文的 [表情包库] 清单就是你的"
        "存货，看哪张没劲、重复了、或者发了被吐槽，直接报编号删掉"
        "（如 3，多张 3,7，一次最多 5 张）。删掉的号以后空着，"
        "清单里不会再出现，腾出的位置留给群里新收的图。"
    ),
    "function": _delete_sticker,
    "parameters": {
        "type": "object",
        "properties": {
            "nums": {
                "type": "string",
                "description":
                    "要删的表情包编号，来自 [表情包库] 清单；如「3」，多张「3,7」",
            },
        },
        "required": ["nums"],
    },
}
