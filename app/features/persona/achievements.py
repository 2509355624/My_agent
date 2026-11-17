"""成就系统（2026-11-04）。

## 为什么只挂确定性事件

打分模型每轮会回一个 `beat`（这轮有没有做成一件推进关系的事），但它是
**布尔值**——判不出「第一次牵手」「她第一次主动」这种细粒度。想靠它出
成就，就得再让模型多输出一个里程碑字段：每轮多花 token，还会误报（模型
把一句客套话判成「告白」），而误报的成就是一次性消耗品，玩家不会回来。

所以这里只认**代码已经确知的事实**：

  - 距离层级首次到达某一级
  - 档位首次升进某一档（只认正向四档）
  - 里程碑（在一起）/ 结局（进局子）
  - 越界被拒累计 3 次
  - 连续陪伴 7 天
  - 单线聊到 100 轮

零额外 token、零误判。等玩家普遍反映「不够」，再加模型侧那套。

## 名字为什么是算出来的

层级名和档位名都来自配置，而且**角色卡可以覆盖**（骨科卡第 2 级叫
「妹妹」）。成就名跟着一起动，不然玩家看到成就写「说得上话」，侧栏写
「妹妹」，像是两个系统。
"""
import datetime

# 只认这四档：「有点在意 / 动了心 / 放在心上 / 彻底信任」。
# normal 是一开始就在的地方，负向那几档不是成就，是事故。
_TIER_KEYS = ("touch", "start", "care", "full")

# 固定成就：key → (icon, 兜底名, 条件说明)
_FIXED = {
    # 2026-11-04 晚：「毕业」结局退休（50 不再是终点），这条成就改成里程碑
    # 事件「在一起」——同一个位置、同一种「走到这一步了」的意思，但不再是收场。
    "together": ("💞", "在一起", "把关系走到确定下来"),
    "jail": ("🚨", "进局子", "把这条线玩到结局破裂"),
    "refused3": ("🚪", "她说过三次不", "累计越界被拒 3 次"),
    "streak7": ("📅", "连着七天", "连续 7 天都来找她"),
    "turns100": ("🕰", "第一百轮", "和同一个人聊到 100 轮"),
}

_LV_ICON = {1: "🌱", 2: "💬", 3: "🫀", 4: "🔥", 5: "🏠"}
_TIER_ICON = {"touch": "💛", "start": "💗", "care": "🩹", "full": "🕯"}


def _keys():
    """全部成就的 key（顺序即展示顺序）。"""
    out = ["lv%d" % i for i in range(1, 6)]
    out += ["tier_" + k for k in _TIER_KEYS]
    out += list(_FIXED)
    return out


# ── 名字（跟着层级名 / 档位名走）────────────────────────

def _lname(n, cfg, card):
    from app import affection as A
    try:
        return A.level_name(n, cfg, card) or ""
    except Exception:                                     # noqa: BLE001
        return ""


def _tname(key, cfg, level):
    from app import affection as A
    try:
        t = next((x for x in ((cfg or {}).get("tiers") or [])
                  if x.get("key") == key), None)
        if not t:
            return ""
        return A._tier_view(t, level).get("name") or ""    # noqa: SLF001
    except Exception:                                     # noqa: BLE001
        return ""


def name(key, cfg=None, card=None, level=None):
    """这个成就叫什么。层级/档位相关的走动态名，其余用兜底名。"""
    key = str(key or "")
    if key.startswith("lv") and key[2:].isdigit():
        got = _lname(int(key[2:]), cfg, card)
        if got:
            return got
        return "第 %s 级" % key[2:]
    if key.startswith("tier_"):
        got = _tname(key[5:], cfg, level)
        if got:
            return got
        return str(_FIXED.get(key, (None, "关系进了一步"))[1])
    return str(_FIXED.get(key, (None, "成就"))[1])


def icon(key):
    key = str(key or "")
    if key.startswith("lv") and key[2:].isdigit():
        return _LV_ICON.get(int(key[2:]), "❤")
    if key.startswith("tier_"):
        return _TIER_ICON.get(key[5:], "💛")
    return str(_FIXED.get(key, ("🏅", "", ""))[0])


def exists(key):
    return str(key or "") in _keys()


# ── 判定 ──────────────────────────────────────────────

def check(rec, res, cfg=None, card=None):
    """结算之后调用：返回**这轮新解锁**的成就（不写 rec，由调用方落盘）。

    只看事实，不看 delta。返回的每一项都带好了文案，调用方直接发就行。
    """
    rec = rec if isinstance(rec, dict) else {}
    res = res if isinstance(res, dict) else {}
    got = {str(a.get("k")) for a in (rec.get("ach") or []) if isinstance(a, dict)}

    cand = []
    lv = int(res.get("level") or 0)
    if 1 <= lv <= 5:
        cand.append("lv%d" % lv)
    tier = str(res.get("tier") or "")
    if tier in _TIER_KEYS:
        cand.append("tier_" + tier)
    ended = str(res.get("just_ended") or "")
    if ended == "jail":
        cand.append("jail")
    # 里程碑「在一起」：2026-11-04 晚替代原来的「毕业」。
    # 只认**这一轮刚触发**（just_milestone），老档在 `_migrate` 里补发。
    if str(res.get("just_milestone") or "") == "together":
        cand.append("together")
    if int(rec.get("cross_n") or 0) >= 3:
        cand.append("refused3")
    if int(rec.get("streak") or 0) >= 7:
        cand.append("streak7")
    if int(rec.get("turns") or 0) >= 100:
        cand.append("turns100")

    ts = datetime.datetime.now().strftime("%m-%d %H:%M")
    out = []
    for k in cand:
        if k in got or not exists(k):
            continue
        got.add(k)
        out.append({"k": k, "name": name(k, cfg, card, lv),
                    "icon": icon(k), "ts": ts})
    return out


def mark(rec, fresh):
    """把新解锁的写进记录（就地改 rec），返回同样一份。"""
    if not isinstance(rec, dict) or not fresh:
        return []
    lst = rec.get("ach")
    lst = lst if isinstance(lst, list) else []
    lst += [{"k": a["k"], "ts": a.get("ts") or ""} for a in fresh]
    rec["ach"] = lst
    return fresh


# ── 展示 ──────────────────────────────────────────────

def _group(key):
    """未解锁时给个类别前缀：14 行一模一样的「🔒？？？」像一堵墙，
    分个组至少能让玩家知道「关系还有 5 级没走」。"""
    key = str(key or "")
    if key.startswith("lv"):
        return "关系"
    if key.startswith("tier_"):
        return "感觉"
    return "经历"


def nodes(rec, cfg=None, card=None):
    """「成就」指令的卡片：解锁的列出来，没解锁的只留锁。

    没解锁的不剧透名字——玩家不该先看到一张「你还没拿到」的清单，
    那等于把探索变成打卡。
    """
    rec = rec if isinstance(rec, dict) else {}
    got = [a for a in (rec.get("ach") or []) if isinstance(a, dict)]
    got_map = {str(a.get("k")): a for a in got}
    lv = int(rec.get("level") or 0)

    lines = ["🏅 成就 · %d / %d" % (len(got), len(_keys())), ""]
    for k in _keys():
        if k in got_map:
            lines.append("%s %s" % (icon(k), name(k, cfg, card, lv)))
        else:
            lines.append("🔒 %s · ？？？" % _group(k))
    if not got:
        lines += ["", "（还没有解锁的。慢慢来。）"]
    else:
        lines += ["", "（发「好感」能看她现在到哪一步。）"]
    return "\n".join(lines)


def text(rec, cfg=None, card=None):
    """纯文本兜底（协议端发不了卡片时用）。"""
    return nodes(rec, cfg, card)
