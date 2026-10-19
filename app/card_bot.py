"""QQ 造卡：`<渠道> 造卡 <需求>` → 一张合并转发的「角色卡」卡片。

## 为什么用合并转发

一套卡是「一张封面 + 五段设定 + 脚注」，逐条发就是七八条消息，群里直接刷屏。
合并成聊天记录卡片，点开才展开（2026-10-19 用户定）。漫画之后也走同一条路。

## 重新生成只重画图，不重写文本

用户引用这张卡片说「重新生成」时，人设是他已经认可的那一版——重新调 LLM
只会把它冲掉。所以发卡片时把 `message_id → (卡号, seed, 渠道)` 记进账本，
引用时按 id 反查，换个 seed 重画图，文案从卡库里原样取。

账本查不到（卡片不是这个进程发的、或者 state/ 被清过）就**不接管**，交给
下面的正常管道判——绝不自己猜一个 seed 糊出张不像的图。

## 渠道识别不另写一套

`direct_gen._parse_channel()` 已经处理了「位置优先」「复合 id」「中文名省前缀」
这些坑，这里直接复用。只额外做两件事：抠掉「造卡」两个字、把云端渠道挡掉。
"""

import json
import logging
import os
import random
import re
import threading
import time

from app import card_maker, direct_gen, image_jobs, qq_api
from app.config import QQ_AGENT_ID, state_path

log = logging.getLogger("card_bot")

WORD = "造卡"
DEFAULT_SKILL = "anima-line"
LEDGER_FILE = "card_msgs.json"
LEDGER_KEEP = 500

# 「重新生成」的几种叫法。**只是前置快筛**：真正的判定是"引用的那张卡片在不在
# 账本里"，否则「重新生成一张猫的图」这种普通生图会被误吞。
REDO_WORDS = ("重新生成", "重生成", "重画", "换一张", "再来一张", "重做")

_CARD_RE = re.compile(r"[#＃]?\s*造卡\s*[：:]?\s*")
# 「造卡怎么用」「造卡是什么」——问句不是需求。不挡的话「怎么用」会被当成
# 一句人物描述，最后画出一张主题是「怎么用」的卡。
_ASK_RE = re.compile(r"^(怎么|如何|咋|怎样|什么|啥|能不能|可不可以|可以|支持|是啥|是什)")

USAGE = ("造卡这样用：`<渠道> 造卡 <需求>`\n"
         "例：`anima线条 造卡 一个傲娇的女高中生`\n"
         "不写渠道就走 %s。" % DEFAULT_SKILL)


# ─── 识别 ─────────────────────────────────────────────

def parse(text):
    """这条消息是不是造卡指令。返回 `(是不是, skill, 需求或提示语)`。

    抠掉「造卡」两个字之后剩下的整句交给 `_parse_channel`：渠道词在不在开头、
    写的是中文名还是 id、后面跟了多少需求，都由它按既有规则判。
    """
    t = (text or "").strip()
    if WORD not in t:
        return False, "", ""
    body = _CARD_RE.sub(" ", t).strip()
    skill, rest = direct_gen._parse_channel(body)
    skill = skill or DEFAULT_SKILL
    if skill in image_jobs.NAI_SKILLS:
        return True, "", ("云端渠道（%s）还不支持造卡——它不走本机的 workflow，"
                          "换 `anima线条` / `krea2` 这类吧" % skill)
    rest = (rest or "").strip(" ，,。.:：、\n")
    # 光喊「造卡」、或者问它怎么用 → 回用法。需求至少得有两个字。
    if not rest or len(rest) < 2 or _ASK_RE.match(rest):
        return True, "", USAGE
    return True, skill, rest


def wants_redo(text):
    """这句像不像「把它重画一遍」。命中也还要账本对得上才算数。"""
    t = (text or "").strip()
    return any(w in t for w in REDO_WORDS)


# ─── 主流程 ───────────────────────────────────────────

def start(target, target_id, skill, brief, reply_to=""):
    """起后台线程造卡，**立刻返回**。

    文案 5 秒 + 出图 20~80 秒，堵在消息线程上等于这半分钟整个群都听不见
    机器人说话（同 `image_jobs` 那条口径）。
    """
    th = threading.Thread(target=_work, name="card-bot", daemon=True,
                          args=(target, target_id, skill, brief, str(reply_to or "")))
    th.start()
    return th


def start_redo(target, target_id, quoted_id, reply_to=""):
    """引用一张角色卡卡片说「重新生成」→ 只重画封面。

    返回 `(接没接管, 提示语)`。返回 False 表示那张 id 不在账本里，调用方
    应当继续走正常管道（可能只是句普通聊天）。
    """
    rec = recall(quoted_id)
    if not rec:
        return False, ""
    cid = str(rec.get("cid") or "")
    card = card_maker.get_card(cid)
    if not card:
        return True, "那张卡不在库里了（卡号 %s），重新造一张吧" % (cid or "?")
    skill = rec.get("skill") or DEFAULT_SKILL
    seed = int(rec.get("seed") or 0)
    th = threading.Thread(target=_redo_work, name="card-redo", daemon=True,
                          args=(target, target_id, cid, card, skill, seed,
                                str(reply_to or "")))
    th.start()
    return True, ""


def _work(target, target_id, skill, brief, reply_to):
    say = _say
    say(target, target_id, "造卡中（%s），大概半分钟" % skill, reply_to)
    try:
        card = card_maker.draft(brief)
    except Exception as exc:
        log.exception("造卡失败：%s", brief[:60])
        say(target, target_id, "造卡没成：%s\n换个说法再试一次？" % exc, reply_to)
        return
    card["brief"] = brief
    rec = card_maker.save_card(card)
    cid = rec.get("id") or ""
    seed = _new_seed()
    _draw_and_send(target, target_id, cid, card, skill, seed, reply_to)


def _redo_work(target, target_id, cid, card, skill, old_seed, reply_to):
    """重画：**换一个 seed**，其余一切照旧（文案不碰、卡号不变）。"""
    seed = _new_seed()
    if seed == old_seed:
        seed = (seed + 1) % (2 ** 31)
    log.info("角色卡重画 %s：%s seed %s → %s", cid, skill, old_seed, seed)
    _say(target, target_id, "重画中（%s，新 seed %d）" % (skill, seed), reply_to)
    _draw_and_send(target, target_id, cid, card, skill, seed, reply_to, full=False)


def _draw_and_send(target, target_id, cid, card, skill, seed, reply_to, full=True):
    """出封面 → 过审核 → 发一张合并转发卡片 → 记账。

    `full=False` 是「引用卡片重画」那一轮：人设他已经看过一遍了，再刷一遍
    五段明细纯属噪音（用户 2026-10-19 原话「不需要再重复发送这个角色卡的
    明细」）。所以卡片里只留标题、图、一行脚注。
    """
    image_path = None
    try:
        image_path = card_maker.render_cover(cid, card, skill, seed)
        card_maker.update_card(cid, {"cover_state": "done", "cover_seed": seed,
                                     "cover_skill": skill})
    except Exception as exc:
        log.exception("角色卡封面没画出来 %s（%s）", cid, skill)
        card_maker.update_card(cid, {"cover_state": "failed"})
        _say(target, target_id, "封面没画出来：%s\n文字还在，先给你这张。" % exc,
             reply_to)

    if image_path and not _audit(image_path, target, target_id, cid, skill, seed):
        image_path = None        # 拦下了就只发文字；审核自己会回一句提示

    nodes = build_nodes(card, image_path, seed, skill, cid, full=full)
    try:
        data = qq_api.send_forward(target, target_id, nodes)
    except Exception as exc:
        log.exception("角色卡卡片发送失败 %s", cid)
        _say(target, target_id, "卡片发不出去：%s" % exc, reply_to)
        return
    remember((data or {}).get("message_id"),
             {"cid": cid, "seed": seed, "skill": skill})


def _audit(path, target, target_id, cid, skill, seed):
    """封面过不过审**跟现有图片审核开关走**（用户 2026-10-19 拍板）。

    `allow_send` 是唯一闸门：按群/私聊三层开关判，没开就直接放行（一次
    settings 读、不发网络请求），拦下时它会自己回一句并进人工二审队列。
    这里只负责「不给它图」。
    """
    from app import image_audit
    try:
        return image_audit.allow_send(
            path, QQ_AGENT_ID, target, target_id,
            meta={"kind": "card", "cid": cid, "skill": skill,
                  "seed": seed, "caption": "角色卡封面"})
    except Exception as exc:
        log.warning("角色卡审核异常（按不放图处理）：%s", exc)
        return False


def build_nodes(card, image_path, seed, skill, cid, full=True):
    """组装聊天记录卡片的节点：标题 / 封面 / 五段 / 补了什么 / 脚注。

    `full=False`（重画那一轮）只出 标题 + 封面 + 脚注 —— 明细不重发。
    """
    name, uin = _bot_identity()
    title = "📇 角色卡 · %s" % (card.get("name") or "未命名")
    out = [qq_api.node_segment(title if full else title + "（重画）", name, uin)]
    if image_path:
        out.append(qq_api.node_segment([qq_api.image_segment(image_path)], name, uin))
    else:
        out.append(qq_api.node_segment("（这张封面没过审核，只有文字）", name, uin))
    if full:
        for key, label, _hint, _rng in card_maker.FIELDS:
            body = str(card.get(key) or "").strip()
            if body:
                out.append(qq_api.node_segment("【%s】\n%s" % (label, body), name, uin))
        note = str(card.get("note") or "").strip()
        if note:
            out.append(qq_api.node_segment("补了什么：%s" % note, name, uin))
    out.append(qq_api.node_segment(
        "渠道 %s · seed %d · %s%s"
        % (skill, seed, cid,
           "\n不满意就引用这张卡片说「重新生成」" if full else ""), name, uin))
    return out


# ─── 账本 ─────────────────────────────────────────────
#
# 键 = **发出去的那张合并转发卡片自己的 message_id**。用户引用卡片时，
# reply 段给的就是它。卡片被 QQ 回收或本进程重启都不会丢记录（落盘）。

def _ledger_path():
    return state_path(LEDGER_FILE)


def _load_ledger():
    fp = _ledger_path()
    if not os.path.isfile(fp):
        return {}
    try:
        with open(fp, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError) as exc:
        # 手改坏 / 写一半断电：重画功能失效，但造卡本身不受影响。
        log.warning("造卡账本读不出来（%s），按空处理", exc)
        return {}


def remember(message_id, rec):
    """记一笔 `message_id → 卡号/seed/渠道`。拿不到 id 就只打日志。"""
    mid = str(message_id or "").strip()
    if not mid:
        log.warning("造卡卡片没拿到 message_id，这张卡之后引用重画认不出来")
        return
    data = _load_ledger()
    data[mid] = dict(rec or {}, ts=time.strftime("%Y-%m-%d %H:%M:%S"))
    if len(data) > LEDGER_KEEP:
        for k in sorted(data, key=lambda x: data[x].get("ts") or "")[:-LEDGER_KEEP]:
            data.pop(k, None)
    try:
        os.makedirs(os.path.dirname(_ledger_path()), exist_ok=True)
        with open(_ledger_path(), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
    except OSError as exc:
        log.warning("造卡账本写不进去（%s），重画功能对这张卡不可用", exc)


def recall(message_id):
    return _load_ledger().get(str(message_id or "").strip())


# ─── 杂项 ─────────────────────────────────────────────

_identity = {"cached": None}


def _bot_identity():
    """合并转发节点要的「谁说的」：机器人自己的昵称 + QQ 号。取不到就留空。"""
    if _identity["cached"] is None:
        try:
            uid, nick = qq_api.check_alive()
            _identity["cached"] = (nick or "造卡", int(uid or 0))
        except Exception:
            _identity["cached"] = ("造卡", 0)
    return _identity["cached"]


def _new_seed():
    return random.randint(0, 2 ** 31 - 1)


def _say(target, target_id, text, reply_to=""):
    try:
        if target == "group":
            qq_api.send_group(target_id, text, reply_to=reply_to or None)
        else:
            qq_api.send_private(target_id, text, reply_to=reply_to or None)
    except Exception:
        log.exception("造卡回话失败：%s", str(text)[:60])
