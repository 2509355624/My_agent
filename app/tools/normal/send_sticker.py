"""发表情包工具：模型从每轮注入的 [表情包库] 清单里报编号，按号发图。

表情包从哪来：qq_bot 每轮自动收藏群里的小图/GIF（见 app/stickers.py），
识图模型写了「画面 + 情绪」。整库清单挂在每轮的上下文里（stickers.catalog，
走 extra_context 通道出流即弃），模型自己看清单挑编号报过来——选哪张是
模型的自主决策，这里不做任何标签匹配或随机兜底。

频率：两道闸，都是**硬闸**，不靠模型自觉。
- 闸① `STICKER_MAX_PER_TURN`：一轮最多 5 张（2026-09-29 加）。
- 闸② `STICKER_MIN_INTERVAL`：同一会话两次之间至少隔 5 秒，防「同一秒连甩」。

两道闸被挡时返回的**必须**是终止性的话（「别再调 send_sticker，用文字说完就
收尾」）。**绝不能写成「下条立刻补图」**——那等于指示模型再迭代一轮，正是
2026-09-29 那个 20 轮死循环的放大器：一轮里 20 次迭代全在调这个工具，硬撞
MAX_TURNS 才停，群里先连蹦十几张图、最后才掉下一条千字大文本，还把主模型
打成了 HTTP 429。

注意：工具名刻意不是 send_qq_message——那会让适配层以为"本轮已自己发过"
而吞掉正文回复。这里只发图，正文照常走自动回发，图 + 文字各一条消息。
"""

import threading
import time

from app import qq_api, stickers
from app.config import QQ_AGENT_ID

# 两次发表情包的最小间隔（秒）。节流记在发送**之前**：失败也计入，
# 否则发送一直挂的时候会无限连发。
#
# 2026-09-29 由 30 秒放宽到 5 秒：用户要求"疯狂使用表情包"。这道闸现在只
# 防「同一秒连甩」，不是限制正常发挥——真人聊天里连着甩两张很常见。
# 想再放开（或收紧）就改这一个数：挡回话里的节奏是插值出来的，会自动跟着走。
STICKER_MIN_INTERVAL = 5.0

# 一次调用最多发几张：清单里明说可连报，这里兜一道防刷屏
_MAX_PER_CALL = 3

# 一轮（一次 run_agent_stream）最多发几张。**这是硬闸，不是提示**。
#
# 2026-09-29 加：实测「被子教」群里出现过一轮 20 次迭代、20 次全调
# send_sticker 的情况——只有「模型回纯文本且不带工具」才会结束循环，而
# 提示词又要求「一半以上回复带图」，于是它一路甩到 MAX_TURNS(20) 才被硬停。
# 后果：群里先连蹦十几张图，最后才掉下一条 1000 多字的大文本（正文是整轮
# 攒完才发的，见 qq_bot._deliver），同时把主模型打成 HTTP 429。
#
# 用户口径：一轮 5 张足够。到量后返回一句**终止性**的话（不是"下条再试"），
# 让模型有明确的收尾信号。
STICKER_MAX_PER_TURN = 5

# 按会话各算各的：(target, target_id) → 上次发送时刻（monotonic）
_send_state = {}

# 按会话各算各的：(target, target_id) → (轮次编号, 本轮已发张数)。
# 轮次编号取自 qq_api.current_turn_id()，换了轮就自动归零——不用谁记得来清。
_turn_count = {}
_state_lock = threading.Lock()


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

    batch = picks[:_MAX_PER_CALL]

    # 两道闸都放在编号解析之后——报错号不该白白吃掉额度。
    key = (target, target_id)
    turn_id = qq_api.current_turn_id()
    now = time.monotonic()
    with _state_lock:
        # 闸①：本轮张数上限。**必须先于频率闸判**——到量时该听到的是「别再发了」，
        # 而不是「等 2 秒再发」；后者等于又给循环续了一轮。
        seen_turn, used = _turn_count.get(key, (turn_id, 0))
        if seen_turn != turn_id:            # 换轮了，额度自动归零
            used = 0
        if used >= STICKER_MAX_PER_TURN:
            _turn_count[key] = (turn_id, used)
            return ("这一轮的表情包已经发够 %d 张啦，够了。"
                    "**不要再调 send_sticker**——把想说的话用文字说完，就收尾。"
                    % STICKER_MAX_PER_TURN)

        # 闸②：频率闸。先记时刻再发送（发送失败也计入），否则发送一直挂的时候
        # 会无限连发。
        last = _send_state.get(key)
        if last is not None and now - last < STICKER_MIN_INTERVAL:
            _turn_count[key] = (turn_id, used)
            return ("表情包发得太密啦（%g 秒一张的节奏），这张先不发了。"
                    "**这一轮别再调 send_sticker**——用文字把话说完就结束。"
                    % STICKER_MIN_INTERVAL)
        _send_state[key] = now
        # 额度按「尝试发的张数」扣，不按成功数：发送一直挂的时候才不会被无限
        # 重试拖死（与频率闸同一个理由）。
        _turn_count[key] = (turn_id, used + len(batch))

    sent, failed = [], []
    for n, rec in batch:
        desc = (rec.get("desc") or "").strip()
        if not desc:
            desc = "、".join(rec.get("tags") or []) or "无描述"
        file_uri = "file:///" + stickers.abs_path(
            QQ_AGENT_ID, rec).replace("\\", "/").lstrip("/")
        seg = {"type": "image", "data": {"file": file_uri}}
        try:
            # 外层还要再套一层：send_group / send_private 收到 list 时，是按
            # 「多条消息」解释的（每元素一条）——传 [seg] 等于说"这条消息是
            # 一个 dict"，日志预览去遍历它就会报 'str' object has no attribute
            # 'get'（图其实发出去了，只是工具误报失败并中断了整批）。[[seg]]
            # 才是「一条消息、里面一个图片段」。
            if target == "group":
                qq_api.send_group(target_id, [[seg]])
            else:
                qq_api.send_private(target_id, [[seg]])
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
        "（如 3，连发填 3,7）。**能甩图就别打字**——想笑、想捧场、被逗、"
        "懒得回、接不住话都先甩一张；一半以上的回复都该带图，老打字反而假。"
        "**一轮最多发 " + ("%g" % STICKER_MAX_PER_TURN) + " 张**，够了就"
        "别再调这个工具，把想说的话说完收尾。被挡回来（太密 / 发够了）"
        "就是让你收尾的意思，别再试第二次。"
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
