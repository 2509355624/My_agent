"""角色卡库：官方卡读写、规范化、内置卡保护、批量删除闸。

自建卡仍留在 `service.py`：它按 `_STATE_PATH` 找文件，测试会直接 patch 那个
模块级路径；搬走会让 patch 落空。这里只收「不依赖会话状态」的官方卡库。
"""

import json
import logging
import os
import re
import time

from app.config import QQ_AGENT_ID

log = logging.getLogger("persona.cards")


def _digits_only(v):
    """只留数字。QQ 号进文件名前必过这一关（防 `..`、防路径分隔符）。"""
    return re.sub(r"\D", "", str(v or ""))


def _cards_path():
    """卡库落在 agent 自己目录下（`agents/` 整个被 gitignore，卡是私有数据）。"""
    from app.core import agents
    from app.core import agents as agent_store
    d = agents.agent_dir(QQ_AGENT_ID)
    if not d:
        return None
    return os.path.join(d, "characters.json")


def _as_int(v, default=None, lo=None, hi=None):
    """宽松取整数：空/坏值回退 default（可能是 None），可选夹区间。"""
    try:
        n = int(v)
    except (TypeError, ValueError):
        return default
    if lo is not None:
        n = max(lo, n)
    if hi is not None:
        n = min(hi, n)
    return n


def _level_names(v):
    """距离层级的自定义叫法（6 个词），坏值当没填。"""
    if not isinstance(v, list):
        return []
    return [str(x or "").strip() for x in v][:8]


# 状态下的她：四个轴各一段自由文本。英文键给程序用，中文键也认（手写卡
# 时更直观）。**不解析内容、不做阈值**——只是塞进编剧 AI 的上下文。
_STATE_BEHAVIOR_KEYS = ("submit", "corrupt", "affection", "suspicion")
_STATE_BEHAVIOR_ALIAS = {
    "submit": "submit", "屈服": "submit",
    "corrupt": "corrupt", "堕落": "corrupt",
    "affection": "affection", "好感": "affection",
    "suspicion": "suspicion", "戒备": "suspicion",
}


def _norm_state_behavior(v):
    """把卡上的 `stateBehavior` 洗成四段。字符串就当成一段总说明。"""
    out = {k: "" for k in _STATE_BEHAVIOR_KEYS}
    if isinstance(v, dict):
        for key, val in v.items():
            k = _STATE_BEHAVIOR_ALIAS.get(str(key or "").strip().lower())
            if k:
                out[k] = str(val or "").strip()[:400]
        if "note" in v:
            out["note"] = str(v.get("note") or "").strip()[:1600]
    elif isinstance(v, str) and v.strip():
        out["note"] = v.strip()[:1600]
    return out


def _state_behavior_text(card):
    """卡上的「状态下的她」拼成提示词里的一小段。没有就返回空串。"""
    v = (card or {}).get("stateBehavior") if isinstance(card, dict) else None
    if isinstance(v, str):
        return v.strip()
    if not isinstance(v, dict):
        return ""
    labels = (("submit", "屈服高时"), ("corrupt", "堕落高时"),
              ("affection", "好感高时"), ("suspicion", "戒备高时"))
    lines = []
    for key, label in labels:
        val = str(v.get(key) or "").strip()
        if val:
            lines.append("- %s：%s" % (label, val))
    note = str(v.get("note") or "").strip()
    if note:
        lines.append("- " + note)
    return "\n".join(lines)


def _norm_card(c):
    """补齐缺省字段；`id` 缺失时用建卡时间兜一个。"""
    out = {
        "id": str(c.get("id") or "").strip(),
        "name": str(c.get("name") or "").strip(),
        # 她真正的名字（2026-11-09 群友需求）：卡名可能是「青梅竹马」这种
        # 关系称谓，正文里读着像代号；填了 charName 就用它指代她，
        # 卡名仍然负责 `#角色名` 匹配、列表排序和 LoRA 找卡。
        "charName": str(c.get("charName") or "").strip()[:20],
        "description": str(c.get("description") or "").strip(),
        "avatar": str(c.get("avatar") or "").strip(),
        "personality": str(c.get("personality") or "").strip(),
        "systemPrompt": str(c.get("systemPrompt") or "").strip(),
        # 示例对话（2026-11-03）：酒馆里叫 `mes_example`，是影响文风最重的
        # 一块。抽象规则让模型「推导」怎么写，示例让它「照着学」——后者
        # 稳得多。**空着就跳过**，不强制每张卡都填。
        "exampleDialogue": str(c.get("exampleDialogue") or "").strip(),
        "appearancePrompt": str(c.get("appearancePrompt") or "").strip(),
        "outfitPrompt": str(c.get("outfitPrompt") or "").strip(),
        # 角色 LoRA（2026-11-08）：卡上写死的角色库 key。给了就硬挂，
        # 不再让角色库每轮向量召回 + 大模型判一遍「这是谁」。
        # 没加进这个白名单的话会被 `_norm_card` 丢掉——实测卡里写了
        # char_lora，`load_cards()` 读出来是 None。
        "char_lora": str(c.get("char_lora") or "").strip(),
        # 房间与私物（2026-11-03）：只在「你离开她视线、进她房间」时当素材用。
        # 留空也能玩——由她按人设临场编，这里只是给个锚，避免每次房间都不一样。
        "room": str(c.get("room") or "").strip(),
        # 卡级默认开场（2026-11-07）：游客第一屏要固定、可控，不能每次现生成。
        # 生成版靠 `_OPENING_PROMPT` 抽卡，角色可能今天在食堂、明天在图书馆，
        # 游客的判断窗口只有十几秒，必须一上来就抓住。留空 = 走生成那条老路。
        "opening": str(c.get("opening") or "").strip(),
        # 攻略起点（2026-11-04）：不同角色不该都从「陌生人」开始。
        # 骨科卡可以一上来就是「妹妹」，同事卡可以一上来就是「同事」。
        # start_level = 距离层级 0~5；start_value 留空就卡在该级门槛上；
        # level_names 可以覆盖那 6 级的叫法（不填就用通用名）。
        # ⚠ 这四组里 `start_level` / `start_arousal` 是 **v1 老标尺**的字段。
        # 从没聊过的新线一律建成 v2（`affection.V2_AUTO_NEW`），v2 的关系档
        # 由「好感 + 是否在一起」算、冲动是身体线里的 0~4 档——所以 v2 卡
        # 要看下面那组 v2 起点，别指望这两个。两边都留着是因为老档还在跑。
        "start_level": _as_int(c.get("start_level"), 0, 0, 5),
        # 好感起点：v1 的 MAX_V 和 v2 的 value 上限都是 100，以前这里钳在 50，
        # 等于「恋人档（75）」「在一起了（50）」都填不出来——2026-11-09 放开。
        "start_value": _as_int(c.get("start_value"), None, -50, 100),
        # 开局冲动（2026-11-05）：「她一开始就已经很想」的卡用得上——
        # 痴情师尊那种开局，默认的 0 会让模型按「还没起来」演，和卡面冲突。
        # 只影响新建这条线的那一刻。
        "start_arousal": _as_int(c.get("start_arousal"), 0, 0, 100),
        # ── v2 起点（2026-11-09）：新档真正生效的那四条轴 ──────────
        # v2 建档时这些值直接落进记录（见 `affection._blank`），只影响
        # **新建**这条线的那一刻，之后照常按每轮结算涨落。
        # 表白值 0~50：她心里那句话攒到哪一步（50 = 该她开口了）。
        "start_confess": _as_int(c.get("start_confess"), 0, 0, 50),
        # 病娇值 0~100：她已经认准你了、你却一直做违背她的事才涨；
        # 45 / 75 两档各出一个事件。开局给到 45 = 一上来就绷着。
        "start_yandere": _as_int(c.get("start_yandere"), 0, 0, 100),
        # 是否在一起：v2 的 50 / 75 两档（在一起了 / 恋人）要这个为真才解锁。
        # 想要「一上来就是恋人」= together 真 + 好感 ≥ 75，两个都得给。
        "start_together": bool(c.get("start_together")),
        # 冲动起点（v2 版）：身体线里是 0~4 档而不是数值
        # （0 平淡 / 1 有点想 / 2 想要 / 3 主动 / 4 逆推）。
        "start_arousal_tier": _as_int(c.get("start_arousal_tier"), 0, 0, 4),
        "level_names": _level_names(c.get("level_names")),
        # 状态下的她（2026-11-05）：屈服/堕落/好感/戒备高的时候，她会怎么变。
        # 四段自由文本，**不解析、不做阈值**；只给编剧 AI 当阅读材料。
        "stateBehavior": _norm_state_behavior(c.get("stateBehavior")),
        "referenceImage": str(c.get("referenceImage") or "").strip(),
        # 暂下架（2026-11-04）：不删卡、只是不在选人列表/名单里出现。
        # 正在演这张卡的会话不受影响（按 id 找卡的路径不过这个开关）。
        "hidden": bool(c.get("hidden")),
        # 怀孕玩法（2026-11-05）：现有卡默认开（全是成年角色）；以后新增
        # 非成年/不想玩这条线的卡，在管理页取消勾选即可。
        "pregnancyEnabled": bool(c.get("pregnancyEnabled", True)),
        # 归属（2026-11-09 玩家自建角色）：**QQ 号**，空 = 官方/站主的卡。
        # 官方库里这个字段恒为空（`_write_cards_raw` 会抹掉），它只是自建卡
        # 那份文件里的自我标记，真正定位靠 id 的 `u<qq>_` 前缀。
        "owner": _digits_only(c.get("owner"))[:20],
    }
    try:
        out["createdAt"] = int(c.get("createdAt"))
    except (TypeError, ValueError):
        out["createdAt"] = int(time.time() * 1000)
    if not out["id"]:
        out["id"] = str(out["createdAt"])
    return out


def load_cards():
    """读卡库。文件不在 / 坏了 / 不是列表，一律当空卡库——卡库出问题不该让
    机器人在群里失灵。"""
    path = _cards_path()
    if not path or not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        log.exception("读卡库失败 %s", path)
        return []
    if not isinstance(data, list):
        log.warning("卡库不是列表，忽略：%s", path)
        return []
    cards, seen = [], set()
    for c in data:
        if not isinstance(c, dict) or not str(c.get("name") or "").strip():
            continue
        card = _norm_card(c)
        if card["id"] in seen:              # id 撞了就给后来的一枚新的
            card["id"] = _fresh_id(seen)
        seen.add(card["id"])
        cards.append(card)
    return cards


def is_builtin_card(card_or_id):
    """这张卡是不是内置的（不可改、不可删）。

    2026-11-03 用户要求「创建角色不能动我的默认角色」。名单在
    `config.BUILTIN_CARD_IDS`——`card_maker` 那边也认同一份，因为扮演态和
    造卡页是两个独立的卡库，各护各的会漏。
    """
    from app.config import BUILTIN_CARD_IDS
    if isinstance(card_or_id, dict):
        if card_or_id.get("builtin") is True:
            return True
        cid = str(card_or_id.get("id") or "").strip()
    else:
        cid = str(card_or_id or "").strip()
    return cid in BUILTIN_CARD_IDS


class SaveCardsRejected(Exception):
    """`save_cards()` 的批量删除闸（2026-11-04 深夜）。

    卡库是整表覆盖写的，任何一次「列表被截断」的提交都会静默删卡。这个异常
    在「提交的卡不到磁盘上一半」时抛出，调用方**必须**把它变成一条明确的
    错误回给用户，不能吞掉——吞掉就退化成原来的静默删卡。

    想真删一批：分几次删（每次少一半以内），或者显式 `force=True`。
    """

    def __init__(self, old_n, new_n, lost):
        self.old_n = old_n
        self.new_n = new_n
        self.lost = list(lost or [])
        super().__init__(
            "拒绝覆盖：卡库会从 %d 张变成 %d 张，疑似列表被截断（丢失 %d 张）"
            % (old_n, new_n, len(self.lost)))


def save_cards(cards, force=False):
    """原子写卡库（临时文件 + os.replace，见 agents._atomic_write_text）。

    写之前逐张规范化、丢掉没名字的、给撞了的 id 补一枚新的——卡池页送来的
    是浏览器里那份（可能少字段、也可能刚「复制一张」出来两张同 id 的），
    这些规矩不该让前端自己知道。

    **内置卡永远保留**（2026-11-03）：这个函数是整体覆盖写，传进来的列表里
    没有内置卡就等于把它们全删了。实测事故：一个没做隔离的测试调了
    `save_cards([一张卡])`，用户 11 张默认卡当场没了。
    现在无论传什么——
      · 传了内置卡的 id：用**现有版本**，内容不许改
      · 没传：从现有卡库里补回来，位置按原顺序

    **批量删除的闸**（2026-11-04 深夜）：这个函数是整份覆盖，所以任何一次
    「只带了半截列表」的提交都会把没带的卡静默删掉。实测事故（当天凌晨）：
    卡库从 25 张变成 12 张，丢了 14 张（含用户的原版伊蕾娜卡），而备份只
    幸运地留在 `state/` 里才捡回来。

    内置卡那道补丁只护得住内置卡，用户自己建的卡一张都没护住。所以再加
    一道：**提交的卡比磁盘上少超过一半时直接拒绝**，除非调用方明确
    `force=True`。想真删一批就分几次删，或者显式 force。

    返回 True 表示写了；抛 SaveCardsRejected 表示被闸拦下（调用方要告诉
    用户为什么，不能静默成功）。
    """
    path = _cards_path()
    if not path:
        return False

    # 先把现有卡库里的内置卡捞出来（内容以磁盘为准，不信调用方）
    old = load_cards()
    old_by_id = {str(c.get("id") or ""): c for c in old}
    builtins = [c for c in old if is_builtin_card(c)]

    from app.core.agents import _atomic_write_text
    out, seen = [], set()
    for c in (cards or []):
        if not isinstance(c, dict):
            continue
        card = _norm_card(c)
        if not card["name"]:
            continue
        if user_card_owner(card["id"]):
            # 自建卡的归宿是 `state/persona_user_cards/<qq>.json`，不是这份库。
            # 官方库整表覆盖时把它们剔掉，省得一份数据两处各存一半（真撞车时
            # `get_card()` 会先读官方库那份，玩家改完发现自己的卡没变）。
            continue
        card["owner"] = ""
        if is_builtin_card(card):
            # 内置卡：一律用磁盘上那份，调用方传的改动不算数。
            # 唯一例外是 hidden（2026-11-04）：下架/上架是个运营开关，
            # 不属于「不许动我的默认角色」保护的内容本身。
            keep = old_by_id.get(card["id"])
            if keep:
                hidden = card.get("hidden")
                # 状态下的她（2026-11-05）：这是「数值高了她怎么变」的调参，
                # 和 hidden 一样属于运营开关，不是改人设正文。允许内置卡保存。
                state_behavior = card.get("stateBehavior")
                preterm = card.get("pregnancyEnabled")
                card = _norm_card(keep)
                card["hidden"] = bool(hidden)
                card["stateBehavior"] = _norm_state_behavior(state_behavior)
                card["pregnancyEnabled"] = bool(preterm)
        if card["id"] in seen:
            card["id"] = _fresh_id(seen)
        seen.add(card["id"])
        out.append(card)

    # 内置卡：传进来的列表里漏了谁，就从磁盘补回来。
    # 这一条是那次事故的直接补丁——`save_cards` 是整体覆盖，前端只传半截
    # 就等于把默认卡删了。
    for b in builtins:
        if b["id"] not in seen:
            out.append(_norm_card(b))
            seen.add(b["id"])

    # 批量删除闸：补完内置卡之后还不到磁盘上的一半 → 判成「列表被截断了」
    if not force and old and len(out) * 2 < len(old):
        lost = [c.get("id") for c in old
                if str(c.get("id") or "") not in seen]
        log.error("拒绝整表覆盖：%d 张 → %d 张，疑似截断，丢失 %s",
                  len(old), len(out), lost[:20])
        raise SaveCardsRejected(len(old), len(out), lost)

    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    _atomic_write_text(path, json.dumps(out, ensure_ascii=False, indent=2))
    return True


def update_card_fields(card_id, **fields):
    """**只改点名的几个字段**，其余原样保留。内置卡也允许改。

    2026-11-03：`save_cards()` 是「前端整卡覆盖」的口径，内置卡一律拒绝——
    那条保护是为了防误删（真出过事故：一个没隔离的测试把 11 张卡写成了 1 张）。

    但保护做过头了：连「给内置卡加一段示例对话」都改不了。所以拆开——

      · `save_cards()`      前端提交整张卡 → 内置卡拒绝（保护）
      · `update_card_fields()`  脚本点名字段 → 允许（维护）

    返回改好的卡；找不到返回 None。
    """
    want = str(card_id or "").strip()
    if not want:
        return None
    cards = load_cards()
    hit = None
    for c in cards:
        if str(c.get("id")) == want:
            hit = c
            break
    if hit is None:
        return None
    for k, v in (fields or {}).items():
        if k in ("id", "createdAt"):
            continue                      # id / 建卡时间不许改
        hit[k] = v
    # 缓存里的旧对象也要跟着变，不然同一进程里读到的是旧值
    for i, c in enumerate(cards):
        if str(c.get("id")) == want:
            cards[i] = hit
            break
    _write_cards_raw(cards)
    return hit


def _write_cards_raw(cards):
    """原样写入，**不做内置卡保护**。只给 `update_card_fields` 用。"""
    path = _cards_path()
    if not path:
        return False
    from app.core.agents import _atomic_write_text
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    out = [_norm_card(c) for c in (cards or []) if isinstance(c, dict)]
    out = [c for c in out if c["name"] and not user_card_owner(c["id"])]
    for c in out:
        c["owner"] = ""               # 官方库里不存归属，归属只活在自建卡文件里
    _atomic_write_text(path, json.dumps(out, ensure_ascii=False, indent=2))
    return True


def _fresh_id(seen):
    """给撞了 id 的卡发一枚**绝不重复**的新 id。

    2026-11-03：原来这里用 `str(int(time.time() * 1000))`——同一毫秒内
    连发的第二张卡拿到**一模一样**的 id，去重等于没去。实测：`save_cards`
    两张无 id 的卡落盘共用一个 id，之后 `load_cards` 每次给第二张重新生成
    （每次加载都漂移），`get_card` 永远找不到它——扮演态切到那张卡直接
    空 Turn。uuid 短哈希彻底根治。
    """
    import uuid
    while True:
        i = uuid.uuid4().hex[:12]
        if i not in seen:
            return i


def user_card_owner(cid):
    """`u2509355624_ab12cd34` → `"2509355624"`；官方卡 → 空串。"""
    m = re.match(r"^u(\d{4,20})_", str(cid or "").strip())
    return m.group(1) if m else ""
