"""QQ galgame 模式：章节主线写死，章内随便走。

跟 persona 是同一个形状（会话级状态 + 一轮一次 LLM 调用 + 后台出图），但解决
的是另一件事：persona 是「随便聊」，这里是「有主线、有自由、有结局」。

## 分工：谁决定什么

| | 谁定 | 为什么 |
| :--- | :--- | :--- |
| 这一章到哪结束（翻章） | **模型判断 + 代码校验** | 「这件事算不算发生了」只有读懂文本的才好判 |
| 章内怎么演、你说的话算不算数 | **模型** | 这正是自由度所在 |
| 状态增量（好感、事实） | **模型提，代码钳** | 模型写「给你一千万」也好，代码只放行合法范围 |
| 结局命中哪一条 | **模型判，代码校验 id** | 校验不过就用第一个兜底，不会卡住 |
| 章节锚点、CG 挂载点 | **代码** | 剧本里写死，模型改不动 |

和旧版最大的差别：旧版每一轮都要过「整条消息必须等于 A/B」这道闸门，说话等于
没发生。现在闸门挪到了章节边界——章内怎么演都行，只有「这一章的必须发生的事」
成了才翻章。

## 为什么选项还在

它从「唯一通道」降级成了「偷懒的快捷方式」：模型每轮给三条建议动作，回 A/B/C
就是替玩家把那条打出来。不理它、直接说人话，一样能玩。这样既能照顾不爱打字的
人，又不会像旧版那样把不会打字的人锁在门外。

## 为什么只存最近几轮，不存完整历史

原本想的是「当前章节 + 事实表 + 好感」就够了，存历史会把上个话题的余温带进来。
实测证明这条路走不通（2026-11-02 用户反馈「剧情很乱」）：模型完全看不到自己
上一句说了什么，每轮都在从零重演这一幕，玩家接着问「搬去哪」它都不知道这个
「搬」从哪来。

现在的折中是**滚动记忆**（`log`，最近 `HISTORY_MAX` 轮）：只留「玩家说了什么 /
她回了什么」，旧的自动挤掉。够接上话，又不会越聊越长。完整历史仍然不存——
要复盘的是一章之内的事，不是整局。

## 为什么群聊共享进度

进度按 `session_key` 存，而它本身已经是 `private_<QQ号>` 或 `group_<群号>`。
所以「群里一份、私聊各一份」不是额外规则，是存储键天然长这样。

## CG 什么时候出

章节写了 `cg` 才出，且**同一章只出一次**（`seen` 记着）。翻章的那一轮不出——
那一轮的正文还是上一章的收尾，图和文对不上；等下一轮进了新章再出。
"""

import collections
import json
import logging
import os
import random
import re
import threading
import time

from app import llm, trigger
from app.config import BASE_DIR

log = logging.getLogger("galgame")

# 一轮的结果：`reply` 是要发回去的整段（正文 + 章节提示 + 建议动作，已拼好），
# `scene` 是这一轮要出的 CG 描述（可能为空），`ended` 表示这一轮之后游戏结束。
Turn = collections.namedtuple("Turn", "reply scene ended")

# 剧本目录。跟 comics/ 一样放项目根，不进 state/——那是运行时数据，剧本是内容。
SCRIPTS_DIR = os.path.join(BASE_DIR, "galgame")
_STATE_PATH = os.path.join(BASE_DIR, "state", "galgame_sessions.json")
_SAVE_DIR = os.path.join(BASE_DIR, "state", "galgame_saves")

# 出 CG 走这条渠道。用户 2026-11-02 拍板「用现有 anima 渠道，不新增」。
# 注意它输出 2456×3686（末尾带 2× 像素放大），一张要几十秒，所以只能按章出、
# 不能每句出。想换成轻量版把这行改成 "_persona_fast" 即可（480×720，2~3 秒）。
GAL_SKILL = "anima-line"

# `gal 开局` 不带剧本名时用这个；不存在就退回剧本目录里的第一个。
DEFAULT_SCRIPT = "sakura"

# 正文上限。给建议动作留位置——一段 600 字的描写会把三条选项顶出屏幕。
BODY_MAX_CHARS = 260

# 事实表：只留最近的，不然越滚越长，最后把提示词撑爆。
#
# 2026-11-02 从 12 降到 8：facts 原本是模型唯一的记忆载体，于是它把「她抱紧
# 了本子」这种一次性动作也塞进来凑数。现在对话本身有滚动记忆（见 HISTORY_*），
# facts 只留真正会影响后续剧情的客观事实，少而准比拼数量有用。
FACTS_MAX = 8
FACT_MAX_CHARS = 30

# 滚动对话记忆：最近几轮「玩家说了什么 / 她回了什么」原样带进下一轮。
#
# 之前刻意不存历史（怕上个话题的余温被带进来），代价是每轮模型都在**从零
# 重演这一幕**：它看不到自己上一句说「我下个月要搬走」，玩家接着问「搬去哪」
# 时它不知道这个「搬」从哪来，只能重新编场面。用户 2026-11-02 反馈「剧情很乱」
# 就是这儿。取最近 6 轮：够接上话，又不至于把整章背下来。
HISTORY_MAX = 6
HISTORY_SAY_CHARS = 150
HISTORY_IN_CHARS = 120

# 好感：单轮最多动 5，总量钳在 ±50。给模型自由，但不让它一句话刷满分。
AFF_STEP = 5
AFF_MIN, AFF_MAX = -50, 50

# 建议动作最多三条（A/B/C）。
OPTS_MAX = 3

_SAFE_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

# 状态文件是「读—改—写」，不同群同时存档会互相覆盖。
_LOCK = threading.Lock()


# ─── 剧本 ────────────────────────────────────────────

def list_scripts():
    """列出剧本目录里能用的剧本，按标题排序。

    单个文件坏了只跳过它，不让一个坏 JSON 把整个目录拖垮——否则用户改剧本时
    手滑少个逗号，`gal 开局` 会连「有哪些剧本」都答不上来。
    """
    out = []
    if not os.path.isdir(SCRIPTS_DIR):
        return out
    for name in sorted(os.listdir(SCRIPTS_DIR)):
        if not name.endswith(".json"):
            continue
        sid = name[:-5]
        sc = load_script(sid)
        if sc is None:
            log.warning("剧本 %s 解析失败，跳过", name)
            continue
        out.append({"id": sc["id"], "title": sc.get("title") or sc["id"],
                    "beats": len(sc["beats"])})
    out.sort(key=lambda s: s["title"])
    return out


def load_script(sid):
    """读一个剧本。不存在、不是 JSON、缺 beats 一律返回 None。

    每次重新读盘（不缓存）：剧本是几 KB 的文本，热改马上生效比省那点 IO 划算
    ——调剧本的时候谁都不想为改一行字重启机器人。
    """
    sid = str(sid or "").strip()
    if not _SAFE_KEY.match(sid):
        return None
    path = os.path.join(SCRIPTS_DIR, sid + ".json")
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(d, dict):
        return None
    raw = d.get("beats")
    if not isinstance(raw, list) or not raw:
        return None

    # 章节按数组顺序走，id 缺了就补 c1/c2。重复 id 直接跳过——CG 的 seen 是按
    # id 记的，两个同 id 的章节会共用一次出图机会。
    beats, seen_ids = [], set()
    for i, b in enumerate(raw):
        if not isinstance(b, dict):
            continue
        bid = str(b.get("id") or "").strip() or ("c%d" % (i + 1))
        if bid in seen_ids or not _SAFE_KEY.match(bid):
            log.warning("剧本 %s 第 %d 章 id 重复或非法（%s），跳过", sid, i + 1, bid)
            continue
        b = dict(b)
        b["id"] = bid
        seen_ids.add(bid)
        beats.append(b)
    if not beats:
        return None

    eds = d.get("endings")
    endings = []
    if isinstance(eds, list):
        for e in eds:
            if isinstance(e, dict) and str(e.get("id") or "").strip():
                endings.append(e)

    d["id"] = str(d.get("id") or sid)
    d["beats"] = beats
    d["endings"] = endings
    return d


def _beat_at(sc, i):
    beats = sc.get("beats") or []
    try:
        i = int(i)
    except (TypeError, ValueError):
        return None
    if 0 <= i < len(beats):
        return beats[i]
    return None


def _beat_index(ent):
    try:
        i = int(ent.get("beat") or 0)
    except (TypeError, ValueError):
        i = 0
    return max(0, i)


# ─── 会话状态 ────────────────────────────────────────

def _load_state():
    try:
        with open(_STATE_PATH, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def _save_state(state):
    os.makedirs(os.path.dirname(_STATE_PATH), exist_ok=True)
    tmp = _STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    os.replace(tmp, _STATE_PATH)


def _entry(session_key):
    """当前会话的进度；不在游戏里返回 None。"""
    if not session_key:
        return None
    with _LOCK:
        return _load_state().get(session_key)


def active(session_key):
    """在不在 galgame 态。`qq_bot` 拿它做分流，`persona` 的判据同款。"""
    return _entry(session_key) is not None


def leave(session_key):
    """退出。已经不在里面也不报错（`gal 结束` 可以随手发）。"""
    with _LOCK:
        st = _load_state()
        if st.pop(session_key, None) is not None:
            _save_state(st)
    _drop_slot(session_key)


def _save_dir(session_key):
    return os.path.join(_SAVE_DIR, session_key)


def _drop_slot(session_key):
    """退出时清掉手动存档槽。剧本换了槽里的东西就对不上了。"""
    d = _save_dir(session_key)
    try:
        for name in os.listdir(d):
            os.remove(os.path.join(d, name))
        os.rmdir(d)
    except OSError:
        pass


def save_slot(session_key, name="slot"):
    """手动存档 → 返回一句回执。进度本来每轮就落盘了，这个是给「想回头重玩」用。"""
    ent = _entry(session_key)
    if not ent:
        return "现在没在玩呀。"
    name = re.sub(r"[^A-Za-z0-9_-]", "", str(name or ""))[:24] or "slot"
    d = _save_dir(session_key)
    os.makedirs(d, exist_ok=True)
    try:
        with open(os.path.join(d, name + ".json"), "w", encoding="utf-8") as f:
            json.dump(ent, f, ensure_ascii=False, indent=1)
    except OSError:
        log.exception("存档失败 %s", session_key)
        return "存档没写成，磁盘可能出问题了。"
    return "存好了（第 %s 档）。" % name


def load_slot(session_key, name="slot"):
    """读档 → 返回一句回执。**不留历史**，读回来直接停在那一章的起点。"""
    name = re.sub(r"[^A-Za-z0-9_-]", "", str(name or ""))[:24] or "slot"
    path = os.path.join(_save_dir(session_key), name + ".json")
    try:
        with open(path, encoding="utf-8") as f:
            ent = json.load(f)
    except (OSError, ValueError):
        return "没有第 %s 档。" % name
    if not isinstance(ent, dict) or not ent.get("script"):
        return "第 %s 档坏了。" % name
    if "beat" not in ent:
        # 旧版存档是 nodes 结构，没有 beat。**不去猜**它原来在第几幕——猜错
        # 比说清楚更难收拾，玩家会以为回到了存档的地方，其实在别处。
        return "这档是旧格式，已经用不了了。重新 gal 开局 吧。"
    sc = load_script(ent["script"])
    beat = _beat_at(sc, _beat_index(ent)) if sc else None
    if beat is None:
        return "这档对应的剧本没了，或者剧本改过格式。"
    with _LOCK:
        st = _load_state()
        st[session_key] = ent
        _save_state(st)
    return "回到第 %d 章 · %s。\n\n%s" % (
        _beat_index(ent) + 1, beat.get("name") or "",
        _beat_text(beat))


def _beat_text(beat):
    """章节骨架原文。读档回执和 LLM 失败兜底都用它——不能空着不说话。"""
    say = str(beat.get("scene") or beat.get("goal") or "").strip()
    return say or "（这里该有一段剧情，但剧本里没写。）"


# ─── 命令 ────────────────────────────────────────────

_HELP = """galgame 模式

  gal 开局 [剧本]   开始（不带名字就用默认剧本）
  gal 状态          看当前进度和好感度
  gal 存档 [名字]   存一个档
  gal 读档 [名字]   回到那个档
  gal 剧本          看有哪些剧本
  gal 结束          退出

玩的时候想说什么就说什么，回 A / B / C 是快捷选择。"""

# 命令词：`gal` 后面必须是空格、句尾，或者**直接接中文**（`gal模式` 这种
# 连写也认，群里打字经常不带空格）。后面接英文一律不认——`galgame`、
# `galaxy`、`gallery` 是别人的词，不能因为前缀相同就被吃掉。
_CMD_RE = re.compile(r"^gal(?=\s|$|[\u4e00-\u9fff])", re.I)

# 「叫了机器人」时允许出现在 `gal` 前面的前缀：真 @，或者名字本身。
#
# 2026-11-02 用户报的问题就在这儿：群里发「@大大怪 gal 开局」（有些协议端
# 把 at 段转成文本，或者用户干脆手打了昵称），消息正文是「大大怪 gal 开局」，
# 而命令只认整条消息以 gal 开头 —— 于是关键词把消息放行了，命令却没认出来，
# 落到普通聊天链，机器人回一句「gal 有两条路子」。对用户来说就是「我喊了
# 名字、说了指令，它装没听见」。
#
# 只在 addressed 为真时才剥这个前缀，所以私聊里「我不想玩 gal」这种句子
# 不会被误判成命令。
_PREFIX_RE = re.compile(
    r"^\s*(?:@[^\s，,、:：]+|" + re.escape(trigger.NAME) + r")\s*[，,、:：]?\s*")


def handle_command(session_key, text, addressed=False):
    """认 `gal xxx` 命令。返回 `Turn`；**不是命令返回 None**（照常往下走）。

    `addressed` 是「这条消息确实在叫机器人」（真 @，或者句子里喊了名字）。
    为真时先把开头的 `@某人` / 名字剥掉，再认 `gal`——群里手打昵称也能用。
    """
    t = (text or "").strip()
    if addressed:
        # 最多剥三次：「@大大怪 大大怪 gal 开局」这种叠着喊的也认。
        for _ in range(3):
            stripped = _PREFIX_RE.sub("", t, count=1).strip()
            if stripped == t:
                break
            t = stripped
    if not _CMD_RE.match(t):
        return None
    body = t[3:].strip()
    if not body:
        return Turn(_HELP, "", False)

    parts = body.split(None, 1)
    sub = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""

    if sub in ("开局", "开始", "模式", "start", "新游戏"):
        return _start(session_key, arg)
    if sub in ("结束", "退出", "end", "stop"):
        if not active(session_key):
            return Turn("现在没在玩呀。", "", False)
        leave(session_key)
        return Turn("游戏结束。想重新开始就发 gal 开局。", "", False)
    if sub in ("状态", "status", "进度"):
        return Turn(_status_text(session_key), "", False)
    if sub in ("剧本", "列表", "list"):
        return Turn(_scripts_text(), "", False)
    if sub in ("存档", "save"):
        return Turn(save_slot(session_key, arg or "slot"), "", False)
    if sub in ("读档", "load"):
        return Turn(load_slot(session_key, arg or "slot"), "", False)
    return Turn(_HELP, "", False)


def _scripts_text():
    scripts = list_scripts()
    if not scripts:
        return "还没有剧本。往 galgame/ 目录里放一个 .json 就能用。"
    lines = ["可用剧本："]
    for s in scripts:
        lines.append("  %s —— %s（%d 章）" % (s["id"], s["title"], s["beats"]))
    lines.append("\n发 gal 开局 剧本id 开始。")
    return "\n".join(lines)


def _status_text(session_key):
    ent = _entry(session_key)
    if not ent:
        return "现在没在玩。发 gal 开局 开始。"
    sc = load_script(ent["script"])
    if sc is None:
        return "进度还在，但剧本「%s」找不到了。" % ent["script"]
    i = _beat_index(ent)
    beat = _beat_at(sc, i) or {}
    lines = [
        "《%s》" % (sc.get("title") or sc["id"]),
        "第 %d / %d 章 · %s" % (i + 1, len(sc["beats"]), beat.get("name") or ""),
        "好感：%d" % int(ent.get("aff") or 0),
    ]
    facts = ent.get("facts") or []
    if facts:
        lines.append("记得的事：%d 条" % len(facts))
    return "\n".join(lines)


def _start(session_key, sid):
    scripts = list_scripts()
    if not scripts:
        return Turn("还没有剧本。往 galgame/ 目录里放一个 .json 就能用。", "", False)

    if not sid:
        sid = DEFAULT_SCRIPT
    sc = load_script(sid)
    if sc is None:
        # 也认中文标题：用户多半记不住 id，但记得住名字。
        for s in scripts:
            if s["title"] == sid:
                sc = load_script(s["id"])
                break
    if sc is None:
        return Turn("找不到剧本「%s」。\n\n%s" % (sid, _scripts_text()), "", False)

    ent = {"script": sc["id"], "beat": 0, "turn": 0, "aff": 0,
           "facts": [], "seen": [], "opts": [], "log": [],
           "started": int(time.time())}
    with _LOCK:
        st = _load_state()
        st[session_key] = ent
        _save_state(st)

    return run_turn(session_key, "", _target_of(session_key), "")


# ─── 输入解析 ────────────────────────────────────────

def _option_key(text):
    """这一行是不是在点建议动作。是就返回 0 基下标，不是返回 None。

    **只认纯字母/数字**：`A` / `a` / `选A` / `A.` / `1`。「A 好可爱」不算——
    带别的字就是玩家在说话，交给模型理解比正则去猜更准。
    """
    t = str(text or "").strip().upper()
    t = re.sub(r"^(?:选择|选|OPTION)\s*", "", t)
    m = re.match(r"^([A-Z]|[1-9])\s*[.、。:：)]?\s*$", t)
    if not m:
        return None
    g = m.group(1)
    if g.isdigit():
        return int(g) - 1
    return ord(g) - ord("A")


def _feed(ent, text):
    """把玩家这句交给模型之前，先看看是不是在偷懒点选项。

    返回 `(要交给模型的输入, 类型)`：
      `pick` 点了建议动作，输入换成那条的原文
      `out`  点了但编号越界
      `free` 自由发言
    """
    t = str(text or "").strip()
    if not t:
        return "", "free"
    opts = [str(o) for o in (ent.get("opts") or []) if str(o).strip()]
    if not opts:
        return t[:200], "free"
    for line in t.splitlines():
        k = _option_key(line)
        if k is None:
            continue
        if 0 <= k < len(opts):
            return opts[k], "pick"
        return "", "out"
    return t[:200], "free"


def _opt_text(opts):
    """建议动作 → `A. xxx`。这是快捷方式的入口，不是分支。"""
    lines = []
    for i, o in enumerate(opts[:OPTS_MAX]):
        s = str(o or "").strip()
        if s:
            lines.append("%s. %s" % (chr(ord("A") + i), s))
    return "\n".join(lines)


def _keys(ent):
    n = len(ent.get("opts") or [])
    return "/".join(chr(ord("A") + i) for i in range(max(1, n)))


# ─── 一轮 ────────────────────────────────────────────

def run_turn(session_key, text, target="private", sender=""):
    """galgame 态的一轮：**1 次** LLM 调用，返回 `Turn(正文+建议, CG描述, 是否结束)`。

    `reply` 空 = 这轮什么都不发（调用方别开口）。正常不会空——模型失败会回退
    到章节骨架原文，宁可干一点也不能让人对着空气等。

    `text` 为空 = 新开一局的开场。
    """
    ent = _entry(session_key)
    if not ent:
        return Turn("", "", False)
    sc = load_script(ent.get("script") or "")
    if sc is None:
        leave(session_key)
        return Turn("剧本「%s」不见了，游戏到这里结束。" % (ent.get("script") or ""),
                    "", True)

    i = _beat_index(ent)
    beat = _beat_at(sc, i)
    if beat is None:
        leave(session_key)
        return Turn("剧情章节丢了，游戏到这里结束。", "", True)

    opening = not str(text or "").strip()
    if opening:
        said, kind = "", "free"
    else:
        said, kind = _feed(ent, text)
    if kind == "out":
        return Turn("这一轮只有 %s 这几个建议。也可以直接说你想做什么。"
                    % _keys(ent), "", False)

    out = _ask(sc, beat, i, ent, said, opening, target, sender)
    # 留一行正文日志：用户报「剧情乱」的时候，没有逐轮原文就只能靠猜。
    log.info("galgame %s 第 %d 章第 %d 轮 | 玩家：%s | 她：%s",
             session_key, i + 1, int(ent.get("turn") or 0) + 1,
             str(said)[:60].replace("\n", " "),
             str(out.get("say") or "")[:60].replace("\n", " "))
    ent2, moved, ending, scene = _apply(sc, ent, out, opening, said)

    if ending:
        # 走到结局就收尾。这里沿用旧行为：退出会连存档一起清掉（用户没让改，
        # 别擅自改语义）。
        leave(session_key)
    else:
        with _LOCK:
            st = _load_state()
            st[session_key] = ent2
            _save_state(st)

    parts = []
    body = str(out.get("say") or "").strip()
    if body:
        parts.append(body)

    if moved:
        nb = _beat_at(sc, ent2["beat"]) or {}
        parts.append("\n——— 第 %d 章 · %s ———"
                     % (ent2["beat"] + 1, nb.get("name") or ""))

    if ending:
        parts.append("\n\n【%s】" % (ending.get("end") or "结局"))
        parts.append("\n（想再玩一次就发 gal 开局）")
    else:
        ot = _opt_text(out.get("opts") or [])
        if ot:
            parts.append("\n" + ot)
            if opening:
                parts.append("（选项只是偷懒用的，直接说你想做什么也行）")

    return Turn("\n".join(p for p in parts if p), scene, bool(ending))


def _apply(sc, ent, out, opening, said=""):
    """把模型这一轮的输出落到状态上。

    返回 `(新状态, 是否翻章, 结局 dict 或 None, 本轮 CG 描述)`。

    这里是**唯一**能改状态的地方：模型的输出一律被钳过范围再写进去。
    """
    beats = sc["beats"]
    i = _beat_index(ent)
    beat = beats[i] if 0 <= i < len(beats) else {}

    aff = int(ent.get("aff") or 0)
    facts = [str(x)[:FACT_MAX_CHARS] for x in (ent.get("facts") or [])]
    seen = [str(x) for x in (ent.get("seen") or [])]
    turn_now = int(ent.get("turn") or 0)

    # 对话记忆在这里追加：`ch` 记这一句属于第几章，翻章后提示词要标明
    # 「上面那些是上一章的事」。翻章判定在下面，所以这里取到的还是旧章号。
    hist = [h for h in (ent.get("log") or []) if isinstance(h, dict)]
    if not opening:
        body = str(out.get("say") or "").strip()
        if body or said:
            hist.append({"ch": i,
                         "u": str(said or "").strip()[:HISTORY_IN_CHARS],
                         "a": body[:HISTORY_SAY_CHARS]})
            hist = hist[-HISTORY_MAX:]

    if not opening:
        turn_now += 1
        # 事实：只收客观发生的事，去重，只留最近的。模型给多了也只取两条，
        # 不然一轮就能把 12 个格子全填满。
        for f in (out.get("facts") or [])[:2]:
            f = str(f or "").strip()[:FACT_MAX_CHARS]
            if f and f not in facts:
                facts.append(f)
        facts = facts[-FACTS_MAX:]
        # 好感：单轮钳在 ±AFF_STEP，总量钳在 ±AFF_MAX。
        try:
            d = int(out.get("aff") or 0)
        except (TypeError, ValueError):
            d = 0
        d = max(-AFF_STEP, min(AFF_STEP, d))
        aff = max(AFF_MIN, min(AFF_MAX, aff + d))

    min_t = max(1, int(beat.get("min_turns") or 1))
    max_t = max(min_t, int(beat.get("max_turns") or 6))

    ready = bool(out.get("ready")) and not opening
    if turn_now < min_t:
        # 代码硬挡：不到最少轮数，模型说到了也不算。它最容易犯的错就是一上来
        # 就把整章的事干完，然后剩下几十轮没内容。
        ready = False
    if turn_now >= max_t:
        # 反过来，到上限必须放行。不然模型一直不肯判定，玩家会在同一章转到烦。
        ready = True

    ending = None
    moved = False
    if ready:
        nxt = i + 1
        if nxt < len(beats):
            i = nxt
            turn_now = 0
            moved = True
        else:
            ending = _pick_ending(sc, out.get("ending"))

    # CG：翻章那一轮不出——那一轮正文还是上一章的收尾，图和文对不上。
    scene = ""
    cur = beats[i] if 0 <= i < len(beats) else {}
    cg = str(cur.get("cg") or "").strip()
    if cg and not moved and not ending and cur.get("id") not in seen:
        scene = cg
        seen.append(cur.get("id"))

    ent2 = dict(ent)
    ent2.update({
        "beat": i, "turn": turn_now, "aff": aff,
        "facts": facts, "seen": seen, "log": hist,
        "opts": [str(o).strip()[:40] for o in (out.get("opts") or [])
                 if str(o).strip()][:OPTS_MAX],
    })
    return ent2, moved, ending, scene


def _pick_ending(sc, raw):
    """模型说的结局 id 必须在剧本里存在，否则用第一个兜底。

    兜底而不是「不进结局」：已经是最后一章且到了轮数上限，不结束就会卡在
    判定循环里出不来。剧本把最中性的那个结局放第一条即可。
    """
    eds = sc.get("endings") or []
    rid = str(raw or "").strip().lower()
    for e in eds:
        if str(e.get("id") or "").strip().lower() == rid:
            return e
    if eds:
        log.warning("剧本 %s 的结局 id「%s」不合法，用第一条兜底", sc["id"], raw)
        return eds[0]
    log.warning("剧本 %s 没有 endings，按通用结局收尾", sc["id"])
    return {"id": "end", "end": "故事到这里结束"}


def _target_of(session_key):
    return "group" if str(session_key).startswith("group_") else "private"


# ─── 生成 ────────────────────────────────────────────

_CONTRACT = """【只输出一个 JSON 对象，不要解释、不要代码块围栏】
{
  "say": "这一幕的正文",
  "facts": ["这一轮值得记住的事"],
  "aff": 0,
  "opts": ["接下来玩家可能想做的三件事"],
  "ready": false,
  "ending": ""
}

【每个字段】
- say：先 1~2 句环境或动作（直接叙述，不要用括号包起来），再来角色的台词
  （台词用「」包住，不写成「角色名：」）。总共不超过 %(n)d 字。
- facts：这一轮新发生、以后还该记得的**客观事实**，0~2 条，每条不超过 %(f)d 字。
  判据只有一条：**把它单独拿出来，别人能核对真假**（她收下了伞 / 你知道她要搬走 /
  素描本在你手上）。写不进去的情绪、动作、修辞一律不要。
  ✅「她答应明天在站台等你」「你把伞留给她了」
  ❌「她有点害羞」「气氛变得温柔」「她抱紧了本子」「你心里一暖」
  已经在上面列过的事实不要重复写。
- aff：这一轮好感的变化，-5 到 5 的整数。被打动给正数，被冒犯给负数，平常给 0。
- opts：接下来玩家可能想做的三件事，每条不超过 14 字，不带 A/B 编号。
  写具体动作（「把伞递给她」），不要写「继续」「下一句」这种空话。
  要顺着**刚才的对话**往下接，不要给和上文无关的选项。
- ready：这一章「必须发生的事」是不是**被真的做到了**。
- ending：只有最后一章 ready 时才填结局 id，其他情况一律填空字符串。

【ready 的判断】这条最容易判错，看清再填。
- ready 只有一个条件：这一章「必须发生的事」**被真的做出来了**，有明确结果。
  「聊到了」「知道了」「她自己主动说了」都不算——那只是信息，不是行动。
  例：必须发生的事是「你帮她把散落的纸捡起来，还到她手里」
    → 玩家问「你怎么了」            → false（只是在问）
    → 玩家说「我蹲下去一张张收好，递给她」→ true（做到了）
- 「她心里悬着的事」被揭开，**不等于**必须发生的事做完了。这两件事分开算。
  她提前把秘密说出来了，也可能必须发生的事还没做——那就还是 false。
- 这一章才演了 %(turn)d 轮，最少 %(min_t)d 轮：%(hint)s
- 玩家岔开话题、这一轮什么都没推进 → false
- 已经到第 %(max_t)d 轮了 → 必须填 true，别再拖

【三条硬规则】
1. 不要替玩家决定他做了什么。玩家说「我走过去」，你写她怎么反应，
   不要写「你走过去抱住她」。
2. 玩家做的事和已有事实冲突时（明明没找到钥匙非要用钥匙），写她当场指出，
   不要凭空变出来。世界会回应，但不会迁就。
3. 不要总结刚才发生了什么，不要问「你要怎么做」，不要跳出角色。
   【刚才的对话】是给你接话用的，**不要复述、不要逐句回应**，就当它真的发生过。"""


def _contract(turn_now, min_t, max_t):
    if turn_now >= max_t:
        hint = "已经到上限了，这轮必须填 true。"
    elif turn_now < min_t:
        hint = "还不够，这轮必须填 false。"
    else:
        hint = "够了，可以填 true，也可以再演一轮。"
    return _CONTRACT % {"n": BODY_MAX_CHARS, "f": FACT_MAX_CHARS,
                        "turn": turn_now, "min_t": min_t,
                        "max_t": max_t, "hint": hint}


def _card_for(sc):
    cid = str(sc.get("cast") or "").strip()
    if not cid:
        return None
    try:
        from app import persona
        return persona.get_card(cid)
    except Exception:
        log.exception("取角色卡失败 %s", cid)
        return None


def _compose(sc, beat, idx, ent, said, opening, target, sender):
    """拼这一轮的提示词。"""
    card = _card_for(sc)
    name = (card or {}).get("name") or "旁白"
    beats = sc["beats"]

    lines = ["你在文字游戏《%s》里扮演%s。"
             % (sc.get("title") or sc["id"], name)]
    if card and card.get("persona"):
        lines.append("\n【角色】\n" + str(card["persona"])[:600])

    lines.append("\n【第 %d / %d 章 · %s】"
                 % (idx + 1, len(beats), beat.get("name") or ""))
    if beat.get("goal"):
        lines.append("必须发生的事：" + str(beat["goal"]))
    if beat.get("tension"):
        # 2026-11-02：光把 tension 写进提示词，模型会拿它当「本章要交代的信息」，
        # 一上来就把底牌摊开（实测第 1 章开场就说了「我下个月要搬走」）。
        # 它是**心里悬着的事**，不是台词内容——除非玩家做到 goal，否则只能漏破绽。
        lines.append("她心里悬着的事（她不会主动说出口，玩家没做到「必须发生的事」"
                     "之前最多漏半句、露出破绽，不许直接摊牌）："
                     + str(beat["tension"]))
    if beat.get("scene"):
        lines.append("这一幕大致是：" + str(beat["scene"]))

    turn_now = int(ent.get("turn") or 0) + (0 if opening else 1)
    min_t = max(1, int(beat.get("min_turns") or 1))
    max_t = max(min_t, int(beat.get("max_turns") or 6))
    lines.append("本章已经演了 %d 轮（最少 %d，最多 %d）。"
                 % (turn_now, min_t, max_t))

    # 状态：好感只影响语气，事实才是硬约束。
    aff = int(ent.get("aff") or 0)
    lines.append("\n【她对你的好感】%d（-50 到 50，只影响语气，不影响剧情走向）" % aff)
    facts = [str(f) for f in (ent.get("facts") or []) if str(f).strip()]
    if facts:
        lines.append("【已经发生的事】（这些是既成事实，不要和它们冲突）\n"
                     + "\n".join("- " + f for f in facts))

    # 刚才的对话：每轮都带上，否则模型从零重演这一幕（见 HISTORY_MAX 的注释）。
    hist = [h for h in (ent.get("log") or []) if isinstance(h, dict)]
    if hist and not opening:
        lines.append("\n【刚才的对话】（按时间顺序，最旧的在上面。接着往下说，"
                     "不要重演、不要复述这些内容）")
        for h in hist[-HISTORY_MAX:]:
            u = str(h.get("u") or "").strip()
            a = str(h.get("a") or "").strip()
            if u:
                lines.append("玩家：" + u)
            if a:
                lines.append("她：" + a)
        if int(hist[-1].get("ch") or 0) != idx:
            lines.append("（上面这些是上一章收尾时的对话，这一章已经换场了。）")

    # 这一轮玩家干了什么
    if not opening and idx > 0 and int(ent.get("turn") or 0) == 0:
        # 刚翻章：上一轮正文还是上一章的收尾，模型很容易顺手把那个场面续下去
        # （实测走廊里的本子被它带到了天台）。这里明说换场了，并要求写过渡，
        # 否则读起来像被硬切了一刀。
        lines.append("\n【这是新的一章开头】时间地点已经换了，按上面写的场景开始，"
                     "不要接着上一章的场面往下写。开头先用一两句话过渡一下"
                     "（时间/地点是怎么走到这里的），再进入这一章的戏。")
    if opening:
        lines.append("\n【这是开场】玩家刚进来，还没说话。把这一章的处境和她的状态"
                     "演出来，结尾留一个能接话的尾巴。")
    else:
        who = "玩家"
        if target == "group" and sender:
            who = str(sender)[:20]
        lines.append("\n【%s说】%s" % (who, str(said)[:200]))
        lines.append("（这是自由发言，不是选项。回应他，但剧情推不推进由你按"
                     "「必须发生的事」自己判断。）")

    # 最后一章才把结局清单给模型，早给会诱导它提前收尾。
    if idx + 1 >= len(beats) and sc.get("endings"):
        lines.append("\n【这是最后一章】如果这一章结束，ending 只能填下面之一：")
        for e in sc["endings"]:
            lines.append("  - %s：%s（%s）" % (e.get("id"), e.get("end") or "",
                                             e.get("when") or ""))

    lines.append("\n" + _contract(turn_now, min_t, max_t))
    return "\n".join(lines)


def _parse_json(raw):
    """从模型输出里抠出 JSON。抠不出来返回 None。

    有些模型爱包 ```json 围栏，有些爱在前后加一句「好的」。这两种都放过，
    但**不做语义修补**——解析不出来就说明它没按契约交作业，交给调用方降级。
    """
    s = str(raw or "").strip()
    if not s:
        return None
    m = re.search(r"```(?:json)?\s*(.+?)```", s, re.S)
    cands = [m.group(1).strip()] if m else []
    cands.append(s)
    a, b = s.find("{"), s.rfind("}")
    if a >= 0 and b > a:
        cands.append(s[a:b + 1])
    for c in cands:
        try:
            d = json.loads(c)
        except ValueError:
            continue
        if isinstance(d, dict):
            return d
    return None


def _fallback(beat):
    """模型挂了 / 没交作业时的兜底：用剧本里手写的那句，不推进。

    回退用 `scene` 而不是「我卡住了」：骨架是手写的，本来就通顺，只是干一点。
    空回或道歉话术会把「游戏」这件事当场戳破。
    """
    return {"say": _beat_text(beat), "facts": [], "aff": 0,
            "opts": [], "ready": False, "ending": ""}


def _ask(sc, beat, idx, ent, said, opening, target, sender):
    """调一次模型，返回规整过的 dict。**永远有 say**。"""
    prompt = _compose(sc, beat, idx, ent, said, opening, target, sender)
    try:
        raw = llm.call_llm([{"role": "user", "content": prompt}])
    except Exception:
        log.exception("galgame 调用失败 %s", sc["id"])
        return _fallback(beat)

    body = str(raw or "").strip()
    if not body:
        log.warning("galgame 空回复 %s", sc["id"])
        return _fallback(beat)

    d = _parse_json(body)
    if d is None:
        # 没交 JSON：当纯文本用。宁可少一次状态推进，也不能把这段好文字丢了。
        log.warning("galgame 输出不是 JSON，按纯文本处理：%s", body[:80])
        return {"say": body[:BODY_MAX_CHARS * 2], "facts": [], "aff": 0,
                "opts": [], "ready": False, "ending": ""}

    say = str(d.get("say") or "").strip()
    if not say:
        say = _beat_text(beat)
    if len(say) > BODY_MAX_CHARS * 2:
        say = say[:BODY_MAX_CHARS * 2].rstrip() + "…"
    opts = d.get("opts")
    if not isinstance(opts, list):
        opts = []
    facts = d.get("facts")
    if not isinstance(facts, list):
        facts = []
    return {"say": say, "facts": facts, "aff": d.get("aff") or 0,
            "opts": opts, "ready": bool(d.get("ready")),
            "ending": str(d.get("ending") or "")}


# ─── CG ──────────────────────────────────────────────

def draw_cg(cg, session_key, script_id=""):
    """出一张 CG，返回文件名（没画出来返回 ""）。**阻塞**——调用方放后台线程。

    跟 `persona.draw` 同一套：入队带 `quiet=True`（图直接回会话、失败不吭声），
    不扣每日额度。区别只有一个——这里用 `GAL_SKILL`（anima 渠道，出图慢但好看），
    所以只挂在关键章节上。
    """
    from app import image_jobs
    from app.tools.normal import generate_image as gi

    sc = None
    if not script_id:
        ent = _entry(session_key) or {}
        script_id = ent.get("script") or ""
    if script_id:
        sc = load_script(script_id)
    card = _card_for(sc) if sc else None
    scene = str(cg or "").strip()
    if not scene:
        return ""

    # 长相由角色卡钉死，这里只描述「这一幕发生什么」。不拼卡的话每张脸都不一样。
    prompt = scene
    if card:
        try:
            from app import persona
            prompt = persona.build_prompt(card, scene)
        except Exception:
            log.exception("拼 CG 提示词失败")

    seed = random.randint(0, 2 ** 32 - 1)
    wf = gi.build_t2i_workflow(GAL_SKILL, prompt, seed)
    if wf is None:
        log.warning("galgame CG 渠道 %s 没有文生图工作流", GAL_SKILL)
        return ""
    if not image_jobs.comfy_alive():
        log.info("galgame CG：ComfyUI 没在线，这次不出图")
        return ""

    target = "group" if session_key.startswith("group_") else "private"
    target_id = session_key.split("_", 1)[1] if "_" in session_key else ""
    if not target_id:
        return ""

    try:
        job, reason = image_jobs.enqueue(
            target, target_id, wf, skill=GAL_SKILL, prompt=prompt,
            seed=seed, landscape=False, quiet=True)
    except Exception:
        log.exception("galgame CG 入队失败 %s", session_key)
        return ""
    if reason:
        log.info("galgame CG 没排上（%s）：%s", session_key, reason)
        return ""
    try:
        entry = job.wait()
    except Exception as e:
        log.info("galgame CG 没画出来（%s）：%s", session_key, e)
        return ""
    names = image_jobs.output_images(entry, job=job)
    if not names:
        log.warning("galgame CG 跑完却没有图片（%s）", session_key)
        return ""
    return names[0]
