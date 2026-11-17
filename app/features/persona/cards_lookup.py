"""按 id / 名字找卡，以及玩家自建卡的读写。"""

import json
import logging
import os
import time
import uuid

from app.core.agents import _atomic_write_text
from app.features.persona.cards_store import (
    _as_int, _digits_only, _fresh_id, _level_names, _norm_card,
    _norm_state_behavior, load_cards, user_card_owner,
)

log = logging.getLogger("persona")


USER_CARD_MAX = 20                 # 每人最多几张
USER_CARD_NAME_MAX = 24            # 卡名长度（列表和 #命令都要用它）

# 玩家能写的字段 + 各自的长度上限。**没列在这里的字段一律不收**：
# `char_lora`（角色 LoRA 是站主的资产库 key，玩家乱填会把出图搞坏）、
# `referenceImage`（服务器上的图片路径）、`hidden`（下架开关）、`builtin`
# 都只对站主开放。`systemPrompt` 是玩家写给模型的演出指令，长度必须钉死——
# 它每轮都在 system 头上，一张卡写两万字会把整条链的预算吃掉。
USER_FIELD_MAX = {
    "name": USER_CARD_NAME_MAX,
    "charName": 20,
    "description": 3000,
    "personality": 1500,
    "systemPrompt": 4000,
    "exampleDialogue": 3000,
    "appearancePrompt": 800,
    "outfitPrompt": 400,
    "room": 1500,
    "opening": 1200,
    "avatar": 500,
}

_INT_FIELD_MAX = {
    # 玩家能写的起点只有 v2 那四条轴（2026-11-09 用户拍板）。
    # `start_level` / `start_arousal` 是 v1 老标尺的字段，新线建成 v2 之后
    # 它们不参与任何判定——留在表单里就是让人填一个空壳，所以**不在白名单**：
    # 手搓请求塞进来也会被 `_clean_user_fields` 丢掉，落回 `_norm_card` 的默认。
    "start_value": (-50, 100),          # 好感：v2 满分 100，50/75 两档要在一起
    "start_confess": (0, 50),           # 表白值：50 = 该她开口了
    "start_yandere": (0, 100),          # 病娇值：45 / 75 各出一个事件
    "start_arousal_tier": (0, 4),       # 冲动是 0~4 档，不是数值
}


def user_cards_path(qq):
    """这个人的自建卡文件；QQ 号不合法（非纯数字）→ None。

    目录跟着 `_STATE_PATH` 走（不是 `state_path()`）：生产上两者都落在
    `state/`，但测试用例是每个用例 patch 一份 `_STATE_PATH`，用它才能做到
    「这个用例建的卡不会串到下一个用例」——`state_path()` 是**整进程共享**
    一个临时目录，20 张上限和重名判断会被前面的用例污染。
    """
    s = _digits_only(qq)
    if not s:
        return None
    # 延迟读门面的 `_STATE_PATH`：测试会 patch `persona._STATE_PATH`，
    # 模块在导入时抄一份就会绕过 patch，把测试卡写进真实 state。
    from app.features.persona import service as persona_service
    return os.path.join(os.path.dirname(persona_service._STATE_PATH),
                        "persona_user_cards", s + ".json")


def load_user_cards(qq):
    """这个人的自建卡。文件不在 / 坏了 / 不是列表 → 空列表（同 `load_cards()`）。

    `owner` 一律按**文件名**覆写：卡里自己写谁都数，文件在谁名下就是谁的——
    否则往别人的文件里塞一张 owner=自己 的卡就能冒充。
    """
    path = user_cards_path(qq)
    if not path or not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        log.exception("读自建卡失败 %s", path)
        return []
    if not isinstance(data, list):
        log.warning("自建卡文件不是列表，忽略：%s", path)
        return []
    owner = _digits_only(qq)
    cards, seen = [], set()
    for c in data:
        if not isinstance(c, dict) or not str(c.get("name") or "").strip():
            continue
        card = _norm_card(c)
        card["owner"] = owner
        if card["id"] in seen:
            card["id"] = _fresh_id(seen)
        seen.add(card["id"])
        cards.append(card)
    return cards


def _write_user_cards(qq, cards):
    path = user_cards_path(qq)
    if not path:
        return False
    from app.core.agents import _atomic_write_text
    owner = _digits_only(qq)
    out = []
    for c in cards or []:
        if not isinstance(c, dict):
            continue
        card = _norm_card(c)
        if not card["name"]:
            continue
        card["owner"] = owner
        out.append(card)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    _atomic_write_text(path, json.dumps(out, ensure_ascii=False, indent=2))
    return True


def get_user_card(qq, cid):
    cid = str(cid or "").strip()
    for c in load_user_cards(qq):
        if c["id"] == cid:
            return c
    return None


def _clean_user_fields(src):
    """按白名单 + 长度上限收字段；返回的 dict 只含玩家能写的那些。"""
    src = src if isinstance(src, dict) else {}
    out = {}
    for k, cap in USER_FIELD_MAX.items():
        text = " ".join(str(src.get(k) or "").split()) if k in (
            "name", "charName", "avatar") else str(src.get(k) or "").strip()
        out[k] = text[:cap]
    for k, (lo, hi) in _INT_FIELD_MAX.items():
        out[k] = _as_int(src.get(k), None, lo, hi)
    out["level_names"] = _level_names(src.get("level_names"))
    # 状态行为 / 怀孕开关是卡面表现，玩家可以自己定；其余见 USER_FIELD_MAX。
    out["stateBehavior"] = _norm_state_behavior(src.get("stateBehavior"))
    out["pregnancyEnabled"] = bool(src.get("pregnancyEnabled", True))
    # 是否在一起：v2 的 50/75 两档靠它解锁，是个布尔而不是数值。
    out["start_together"] = bool(src.get("start_together"))
    return out


def _name_taken_error(qq, name, char_name, skip_id=""):
    """重名就拒绝，不做优先级。

    「官方 > 站主 > 玩家」那套排序要改 `find_card` 的匹配顺序、还要让玩家猜
    为什么自己的卡搜不到；创建时就拒（错误文案直接说找谁）便宜得多。
    卡名和「她真正的名字」两个都算——它们都会进 `find_card`。
    """
    if not str(name or "").strip():
        return "卡名没填"
    want = {str(name).strip().lower(), str(char_name or "").strip().lower()}
    want.discard("")
    pool = ([(c, "官方角色") for c in load_cards()]
            + [(c, "你自己的角色") for c in load_user_cards(qq)])
    for c, where in pool:
        if c["id"] == skip_id:
            continue
        has = {str(c.get("name") or "").strip().lower(),
               str(c.get("charName") or "").strip().lower()}
        has.discard("")
        clash = sorted(want & has)
        if clash:
            return "名字「%s」和%s的卡撞了，换一个" % (clash[0], where)
    return ""


def make_user_card(qq, src):
    """新建一张自建卡 → `(卡, "")`；不合法 → `(None, 错误文案)`。

    id 由后端发（`u<qq>_<uuid8>`），前端传什么都不认——归属不能由客户端声明。
    """
    owner = _digits_only(qq)
    if not owner:
        return None, "没登录，建不了角色"
    card = _clean_user_fields(src)
    err = _name_taken_error(owner, card["name"], card["charName"])
    if err:
        return None, err
    mine = load_user_cards(owner)
    if len(mine) >= USER_CARD_MAX:
        return None, "你自己的角色已经有 %d 张了，先删一张再建" % USER_CARD_MAX
    import uuid
    card["id"] = "u%s_%s" % (owner, uuid.uuid4().hex[:8])
    card["owner"] = owner
    card["createdAt"] = int(time.time() * 1000)
    if not _write_user_cards(owner, mine + [card]):
        return None, "存不下来，稍后再试"
    log.info("自建卡新建：%s → %s（%s）", owner, card["name"], card["id"])
    return _norm_card(card), ""


def edit_user_card(qq, cid, patch):
    """改自己的一张自建卡（只覆盖 patch 里点名的字段）→ `(卡, "")`。"""
    owner = _digits_only(qq)
    cid = str(cid or "").strip()
    if not owner or user_card_owner(cid) != owner:
        return None, "只能改你自己的角色"
    mine = load_user_cards(owner)
    hit = None
    for c in mine:
        if c["id"] == cid:
            hit = c
            break
    if hit is None:
        return None, "没有这张卡：%s" % cid
    upd = _clean_user_fields({**hit, **(patch or {})})
    err = _name_taken_error(owner, upd["name"], upd["charName"], skip_id=cid)
    if err:
        return None, err
    keep = {"id": hit["id"], "owner": owner, "createdAt": hit["createdAt"]}
    hit.clear()
    hit.update(keep)
    hit.update(upd)
    if not _write_user_cards(owner, mine):
        return None, "存不下来，稍后再试"
    return _norm_card(hit), ""


def remove_user_card(qq, cid):
    """删自己的一张自建卡。**只删这一张**，不做整表覆盖以外的任何事。

    正在演这张卡的会话不受影响：线键/历史/小记都按 id 存在 `persona_sessions.json`
    和 `sessions/` 里，卡没了 `active_card()` 就返回 None（沿用官方卡删除的既有行为）。
    """
    owner = _digits_only(qq)
    cid = str(cid or "").strip()
    if not owner or user_card_owner(cid) != owner:
        return False, "只能删你自己的角色"
    mine = load_user_cards(owner)
    left = [c for c in mine if c["id"] != cid]
    if len(left) == len(mine):
        return False, "没有这张卡：%s" % cid
    if not _write_user_cards(owner, left):
        return False, "删失败，稍后再试"
    log.info("自建卡删除：%s → %s", owner, cid)
    return True, ""




def get_card(card_id):
    """按 id 取卡。`u<qq>_` 前缀的走那个人的自建卡文件（2026-11-09）。

    这里认前缀而不是「先查官方库再查本人库」，是为了让**所有**按 id 取卡的调用点
    （`active_card` / `_erase` / 管理页名字 / 出图取卡面）不改一行就能演自建卡——
    线键里存的就是 id，归属从 id 本身就能读出来，不需要额外传「现在是谁」。
    """
    cid = str(card_id or "").strip()
    if not cid:
        return None
    owner = user_card_owner(cid)
    if owner:
        return get_user_card(owner, cid)
    for c in load_cards():
        if c["id"] == cid:
            return c
    return None


def find_card(word, qq=""):
    """按卡名找卡；找不到再按 id 找。**精确匹配**（忽略首尾空白与大小写）。

    不做模糊匹配是有意的：`#` 开头的消息认不出卡就**当普通消息放行**（见
    `handle_command`），所以认错比认不出更糟——模糊匹配会让「#银发少女怎么画」
    这种正常问句被吃成命令。

    `qq`（2026-11-09 玩家自建角色）：传了就在这个人的自建卡里也找一遍，
    **默认不传 = 只看官方库**。QQ 那边全程不传，所以别人的自建卡既不进群的名单、
    也不会被 `#命令` 认出来；只有网页端知道自己是谁，才传这个参数。
    """
    key = str(word or "").strip()
    if not key:
        return None
    low = key.lower()
    cards = load_cards()
    for c in cards:
        if c["name"].lower() == low:
            return c
    # 自定义名字（charName）也认：玩家记住的多半是「苏暖」，不是卡名「青梅竹马」。
    # 仍然精确匹配，不做模糊，理由同上。
    for c in cards:
        if str(c.get("charName") or "").strip().lower() == low:
            return c
    owner = _digits_only(qq)
    if owner:
        mine = load_user_cards(owner)
        for c in mine:
            if c["name"].lower() == low:
                return c
            if str(c.get("charName") or "").strip().lower() == low:
                return c
    for c in cards:
        if c["id"].lower() == low:
            return c
    card_owner = user_card_owner(key)
    if card_owner:                # 自建卡的 id 不在官方库里
        # 传了 qq（玩家自己找卡）→ **只认自己的**：别人的卡 id 猜到你头上也当没有。
        # 不传 qq 的是查看台 `_card_name()` 那类只读展示，站主必须能按 id 解析出
        # 是谁的卡，所以那一支放行。
        if owner and card_owner != owner:
            return None
        return get_card(key)
    return None
