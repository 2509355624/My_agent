"""这一轮的事实清单、地点拍数和她的穿着：贴近生成点的三个小提示块。

只做裁剪和措辞，不判断该不该换场、该不该换衣服。
"""

FACT_MAX = 12
FACT_TEXT_MAX = 40

_FACTS_GUIDE = (
    "【再列一张「这一幕已经有的东西」】\n"
    "facts：按他写的顺序，把这一轮他摆出来、做出来、说出口的具体东西各记一条，"
    "每条不超过 20 字，只写名词和动作，不写解释：\n"
    "  · 拿出来的、放下的、摆在某处的东西：一把小油菜、几个西红柿、豆干、"
    "几颗鸡蛋、两块方便面……\n"
    "  · 已经做完的一件事：中午和晚上的菜分好放进冰箱、冰箱塞满。\n"
    "  · 他台词里点名的东西：早餐煮西红柿鸡蛋汤面加豆干。\n"
    "并列的东西一样一条，写具体的名字和数量，越具体演员越好落地。\n"
    "这里只记她眼前看得见、此刻已经发生的事；心里想的、别处发生过的，"
    "交给别的字段。\n"
)


def _parse_facts(raw):
    """把编剧交的 `facts` 裁成短句列表。只裁剪，不补写、不合并。"""
    if not isinstance(raw, (list, tuple)):
        return []
    out = []
    for item in raw:
        if isinstance(item, dict):
            item = item.get("text") or item.get("name") or ""
        s = " ".join(str(item or "").split())[:FACT_TEXT_MAX]
        if s and s not in out:
            out.append(s)
        if len(out) >= FACT_MAX:
            break
    return out


def _facts_block(facts):
    """给演员的最后一张清单：这一轮已经在场的东西，按原顺序各落地一次。"""
    items = _parse_facts(facts)
    if not items:
        return ""
    lines = ["【这一幕已经有的东西（按他写的顺序）】"]
    lines.extend("- " + f for f in items)
    lines.append("这些是他这一轮摆到眼前、已经发生的事，就在这一幕里。")
    lines.append("让它们按这个顺序在画面里各落地一次：写清东西此刻在哪、被谁碰过、"
                 "成了什么样；中间接上她的动作、停顿、反应，再往下走一拍。"
                 "落地是让它出现在这一刻的画面里，由她的动作带过去。")
    return "\n".join(lines)


def _place_block(gd):
    """同一地点已经连着写了几拍。**只报数，不判换不换场。**"""
    try:
        n = int((gd or {}).get("place_streak") or 0)
    except (TypeError, ValueError):
        n = 0
    if n < 3:
        return ""
    return ("（这个场景已经连着写了 %d 拍。就地接着写没问题，只是再往下得有东西"
            "真的变——有人动身、有个决定定了、有件事办完了；或者这一拍就停在"
            "某个还没做完的动作上。）" % n)


def _outfit_block(outfit):
    """给演员的「她此刻穿着」：只递上一轮的事实，判断交给模型。"""
    cur = str(outfit or "").strip()
    if not cur:
        return ""
    return ("【她此刻穿着（上一轮留下的状态）】\n"
            "- " + cur + "\n"
            "这一轮她脱了一件、加了一件、换了一身，就在「服饰：」行里写出"
            "新的那一身；身上的东西没动，就把这串照抄过去。写具体到哪件、"
            "什么颜色、什么状态，一件一件数清楚。")
