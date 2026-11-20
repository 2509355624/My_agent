"""游戏日：状态补默认、约定事实、跨天事件和动态块文本。

这里不读写会话存档；`_apply_gameday` 那类要落盘的逻辑留在 service。
"""

import random

from app.features.persona.cards_store import _as_int
from app.features.persona.game_time import _day_of

_DAY_TURNS_SOFT = 24

# 通用事件池（约定之外的日子随机撒）。只写**当天有效**的一句话场景约束，
# 不带数值后果——后果由打分 AI 按剧情自然判。
_EVENT_POOL = (
    "台风天，风大雨大，今天哪儿也去不了",
    "一整天都在下雨，气氛黏糊糊的",
    "周末，不用上班也不用上学",
    "她说不上来为什么，今天有点提不起劲",
    "傍晚突然停了一次电，几分钟后自己来了",
)


def _gameday(ent):
    """会话条目里的游戏日状态；缺字段补默认（老存档兼容）。

    gd 必须挂回 ent，否则调用方改完落盘的是没动过的 ent。
    """
    gd = ent.get("gameday")
    if not isinstance(gd, dict):
        gd = {}
    # 第 0 天 = 开场白那一晚：跨天才 +1。
    gd["day"] = max(0, _as_int(gd.get("day"), 0, 0, 99999))
    gd["phase"] = str(gd.get("phase") or "")[:12]
    gd["plan"] = str(gd.get("plan") or "")[:80]
    gd["plan_day"] = _as_int(gd.get("plan_day"), 0, 0, 99999)
    gd["event"] = str(gd.get("event") or "")[:60]
    gd["place"] = str(gd.get("place") or "")[:40]
    gd["turns_today"] = max(0, _as_int(gd.get("turns_today"), 0, 0, 99999))
    ent["gameday"] = gd
    return gd


def _plan_fact(gd, plan):
    """裁判看到的约定：只报登记事实，不替模型断言「还没兑现」。"""
    plan = str(plan or "").strip()
    if not plan:
        return "（系统里目前没有登记在册的约定）"
    day = _day_of(gd)
    pd = _as_int((gd or {}).get("plan_day"), 0, 0, 99999)
    when = "，约在第 %d 天" % pd if pd and pd != day else ""
    past = "；约的那天已经过了" if pd and pd <= day else ""
    return ("第 %d 天登记过一条约定：%s%s%s。做没做过以最近对话为准，"
            "已经做完就把 plan 写成空串销账。" % (day, plan, when, past))


def _roll_event(gd):
    """新的一天开什么：约定 > 随机事件（两成）> 普通的一天。"""
    plan = str(gd.get("plan") or "").strip()
    if plan and (gd.get("plan_day") or 0) <= _day_of(gd):
        gd["plan"] = ""                             # 兑现了，销账
        gd["plan_day"] = 0
        return "已经办完：%s" % plan
    if random.random() < 0.2:
        return random.choice(_EVENT_POOL)
    return ""


def _gameday_block(gd, new_day=False):
    """拼进动态块的游戏时间。没有状态（自由模式等）返回空。"""
    if not isinstance(gd, dict):
        return ""
    phase = str(gd.get("phase") or "").strip()
    day = _day_of(gd)
    lines = ["【游戏时间】第 %d 天%s" % (day,
                                      ("，" + phase) if phase else "")]
    if new_day:
        lines.append("上一拍收在第 %d 天，现在是第 %d 天 · %s。"
                     "中间怎么过来的按「状态」行写，"
                     "这一拍从眼前正在发生的动作起笔。"
                     % (max(0, day - 1), day, phase or "早晨"))
    place = str(gd.get("place") or "").strip()
    if place:
        lines.append("【所在地】%s。人已经在这里，这一轮就写她在这个地方"
                     "做什么、碰到了什么。" % place)
    event = str(gd.get("event") or "").strip()
    if event:
        done = ""
        if event.startswith("已经办完"):
            done = event[len("已经办完"):].lstrip("：:").strip()
        elif event.startswith("约定："):
            done = event[len("约定："):].strip("「」").strip()
        if done:
            lines.append("今天已经办完的事：%s。它已经过去了，"
                         "以「状态」里的地点为准，聊接下来自然发生的事。"
                         % done)
        else:
            lines.append("今天的事：%s。把它化进今天的动作和对白里。" % event)
    # 2026-11-19 用户拍板：未完成的事不再进写作提示词。`plan` 是系统/
    # 裁判的二次判断，写进去会被演员当成每轮必须回收的待办；让它留在
    # 存档里做跨天结算即可。这里不输出任何“登记过的约定”。
    if int(gd.get("turns_today") or 0) >= _DAY_TURNS_SOFT:
        lines.append("这一天已经过了很久，天色不早了。这一拍可以从容收住，"
                     "眼前正在发生的事也可以接着往下演。")
    return "\n".join(lines)
