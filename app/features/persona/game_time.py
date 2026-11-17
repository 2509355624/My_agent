"""游戏内时段与天数：只做字符串归一和整数读取，不碰存储。"""

# 游戏内时段（2026-11-05 四档 → 2026-11-07 六档）。
# 只改 gameday.phase：不推进天数、不掷事件、不触发怀孕结算。
#
# 为什么从四个变六个：打分器的提示词一直写的是「早晨 / 上午 / 中午 / 下午 /
# 傍晚 / 深夜」，它按这个交 phase；后端却只认四档，`傍晚` 被 `set_phase` 直接
# 拒掉，前端按钮里也没有这一档——于是正文写着傍晚、状态栏还停在早晨，玩家
# 点时间弹窗看不到任何高亮。三处词表必须同一张。
PHASES = ("早晨", "上午", "中午", "下午", "傍晚", "深夜")

# 旧数据、旧按钮、模型自由发挥的写法 → 归一。不改的话历史会话里的「晚上」
# 会永远卡在 `set_phase` 外面。
PHASE_ALIAS = {
    "早晨": "早晨", "清晨": "早晨", "早上": "早晨", "清早": "早晨",
    "上午": "上午", "接近中午": "上午",
    "中午": "中午", "正午": "中午", "午间": "中午",
    "下午": "下午", "午后": "下午",
    "傍晚": "傍晚", "黄昏": "傍晚", "晚上": "傍晚", "入夜": "傍晚",
    "深夜": "深夜", "夜里": "深夜", "夜晚": "深夜", "凌晨": "深夜",
    "半夜": "深夜",
}


def normalize_phase(value):
    """任意写法 → PHASES 里的那一档；认不出来原样返回（交给调用方判）。"""
    ph = str(value or "").strip()
    if not ph:
        return ""
    if ph in PHASE_ALIAS:
        return PHASE_ALIAS[ph]
    # 「傍晚前后」这类：取包含的**最长**那个已知档位（短词先命中会选错，
    # 「傍晚前后」里同时含「傍晚」和「下午」）。
    for word, norm in sorted(PHASE_ALIAS.items(), key=lambda kv: -len(kv[0])):
        if len(word) >= 2 and word in ph:
            return norm
    return ph


def phase_index(value):
    """当前时段在一天里的位置；认不出来返回 -1（前端灰显用）。"""
    ph = normalize_phase(value)
    try:
        return PHASES.index(ph)
    except ValueError:
        return -1


def _day_of(gd):
    """游戏日；缺字段按 0。

    2026-11-07 起第 0 天是合法的（= 开场白那一晚），所以**不能**再写
    `gd.get("day") or 1`——那个写法会把 0 当成「没值」，开场立刻跳成第 1 天，
    这正是玩家看到的「点一下切时段就多了一天」。
    """
    try:
        v = (gd or {}).get("day")
    except AttributeError:
        v = None
    try:
        return 0 if v is None else int(v)
    except (TypeError, ValueError):
        return 0
