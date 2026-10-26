"""QQ 角色扮演：人设卡 + 会话级扮演态 + 一轮纯文字对话。

跟生图机器人是**两条互不干扰的路**：

    默认态 → direct_gen / agent 循环，不带人设、能用工具
    扮演态 → 本模块：**不给模型任何工具**，一轮只做两件事

        拼极简头（角色人设 + 输出契约 + 环境）
          → 调 1 次 LLM → 正文发回 QQ，顺手写的那行画面拿去出一张快图（见下）

## 为什么是 1 次调用

参考的那套角色扮演系统一轮要 3 次（情感分析 → 对白 → 生图 tag）。但那三样里，
「人物此刻的情绪」本来就是模型写对白时脑子里已经有的东西，拆成三次只是当年的
写法；同时省掉两次完整输入的 token（那两次的输入跟第一次一样大）。

## 配图（2026-10-16 二次拍板）

同一天早些时候拍的是「扮演态不出图」——理由是配一张图要十几秒，而聊天是几秒
一句话的节奏，图赶不上对话。**当天下午换成了每轮配一张快图**：用户原话
「角色扮演模式的话，就用这个来快速生图，这个只要 2 到 3 秒钟而已，非常的快」，
指 `skills/_persona_fast`（480×720 / 3 步的 silver 漫画变体）。

于是这里的分工是：

    一次 LLM 调用 → 正文（立刻发）+ 「画面：」那一行（拿去出图）
                  ↓ 后台线程，不占这一轮
                出一张 ≈2 秒的图 → 发回同一个会话

三条边界：

1. **正文先走，图后到**。出图在后台线程里等，这一轮不阻塞——聊天节奏一点不变，
   图只是慢两三秒跟上来。
2. **画面由角色自己写**（用户拍板「角色回话时顺手写」）。这一行是输出契约的一部分
   （见 `_CONTRACT`），代码只负责把它从正文里剥出来，**不猜、不兜底**：没这一行就
   这一轮不出图，绝不拿上一轮或角色卡去凑一张对不上剧情的图。
3. **角色长相由卡上的外观字段钉死**（`appearancePrompt` / `outfitPrompt`）。模型
   只写「在做什么」，不写「长什么样」——不然每轮的脸都不一样。

## 作用域

进出都是**会话线级**的（私聊按 QQ 号、群按群号各一份），所以 A 群在演纱雾的
时候，B 群照旧是生图机器人。状态落 `state/persona_sessions.json`，**不进
settings.json**：那是用户在管理页手配的长期配置，扮演态是运行时状态，两者
混在一起会让管理页显示出一堆看不懂的键。
"""

import collections
import json
import logging
import os
import random
import re
import threading
import time

from app import llm
from app.config import BASE_DIR, QQ_AGENT_ID, QQ_BOT_NAME

log = logging.getLogger("persona")

# 一轮的结果：`reply` 是要发回去的正文，`scene` 是这一轮要画的画面（可能为空）。
# 用命名元组而不是 `(reply, scene)`：调用方写 `turn.reply` 比 `[0]` 看得懂，
# 也不会在改动时把两个字段的顺序弄反。
Turn = collections.namedtuple("Turn", "reply scene")

# 历史：最近 20 条（AI 和用户合计），单条超 200 字截断。
#
# 200 字那条是防「对面一口气甩几千字」——真塞进来会把这一轮的输入撑爆，而
# 我们要的只是「他还记得刚才聊过什么」。正常人不会发 200 字以上的消息，
# 真发了，截断也不会丢掉那轮在说什么。
HISTORY_LIMIT = 20
MSG_MAX_CHARS = 200

# 小记：每 NOTE_EVERY_TURNS 轮在后台压一次，压到 NOTE_MAX_CHARS 字以内。
# 不做每轮压缩——那等于把「1 次调用」省下来的钱又还回去。
NOTE_MAX_CHARS = 500
NOTE_EVERY_TURNS = 10

# 扮演态的静默窗口（秒）：这之内的连发算同一条。用户 2026-10-16 定的 10 秒。
# 群聊默认那 1.5 秒是给「连发三句问同一件事」用的；聊天不一样——人打字是
# 断续的，1.5 秒会把一句话切成两轮，回两遍。
DEBOUNCE_SECONDS = 10.0

_CONTRACT = (
    "【怎么回话】\n"
    "以这个角色的口吻说想说的话，就像真的在跟他聊天。\n"
    "想写心里想的事，就写在括号里。\n"
    "\n"
    "【画面】\n"
    "回完话，最后另起一行写「画面：」，后面跟这一轮该画出来的那一幕，\n"
    "用英文写一行：属性用标签（小写，多词用下划线），方位和互动用英文短语补\n"
    "（`on the left side of the desk`、`holding a mug in her right hand`）——\n"
    "在哪、在做什么、什么表情、什么东西在她哪一侧、什么镜头、什么光。\n"
    "她长什么样不用你写，我照角色卡上的样子画。\n"
    "每一轮都要写这一行。"
)

_NOTE_PROMPT = (
    "把下面的对话压成一段 %d 字以内的回忆，用「我」的第一人称写，"
    "只留以后还用得上的事：我跟对方是什么关系、聊过什么、答应过什么、"
    "发生过什么要紧的事。\n"
    "不要复述原话，不要写「对方说」「我说」这类标记，直接写回忆本身，"
    "不要标题，不要分点。\n\n"
    "【我原来记得的】\n%s\n\n【最近发生的】\n%s"
)

# 命令词。都带 `#` 前缀，且要求**整条消息只有这一个词**，免得误伤
# 「#角色扮演能不能画图」这种正常问句。
LIST_WORDS = ("角色扮演", "人设列表")
EXIT_WORDS = ("出戏",)

_SAFE_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

# 扮演态专用的配图渠道（`skills/_persona_fast/`，480×720 / 3 步，实测 ≈2~3 秒/张）。
# 它是个**内部渠道**（目录名 `_` 开头，AI 点不到、管理页也看不到），只由本模块
# 的 `draw()` 引用——扮演态的配图永远走这条路，不受「用户点名了什么渠道」影响。
FAST_SKILL = "_persona_fast"

# 「画面：」那一行。半角/全角冒号都认（模型两种都写过）。取**最后一处**匹配：
# 契约要求它写在正文末尾，而正文里偶尔也会出现「画面」这个词。
_SCENE_RE = re.compile(r"^[ \t]*画面[：:][ \t]*(.*)$", re.M)

# 这行里的修饰符：模型偶尔会把标签套进反引号或加粗星号里。
_SCENE_NOISE_RE = re.compile(r"[`*]+")


# ─── 卡库 ────────────────────────────────────────────

def _cards_path():
    """卡库落在 agent 自己目录下（`agents/` 整个被 gitignore，卡是私有数据）。"""
    from app import agents
    d = agents.agent_dir(QQ_AGENT_ID)
    if not d:
        return None
    return os.path.join(d, "characters.json")


def _norm_card(c):
    """补齐缺省字段；`id` 缺失时用建卡时间兜一个。"""
    out = {
        "id": str(c.get("id") or "").strip(),
        "name": str(c.get("name") or "").strip(),
        "description": str(c.get("description") or "").strip(),
        "avatar": str(c.get("avatar") or "").strip(),
        "personality": str(c.get("personality") or "").strip(),
        "systemPrompt": str(c.get("systemPrompt") or "").strip(),
        "appearancePrompt": str(c.get("appearancePrompt") or "").strip(),
        "outfitPrompt": str(c.get("outfitPrompt") or "").strip(),
        "referenceImage": str(c.get("referenceImage") or "").strip(),
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
            card["id"] = str(int(time.time() * 1000))
        seen.add(card["id"])
        cards.append(card)
    return cards


def save_cards(cards):
    """原子写卡库（临时文件 + os.replace，见 agents._atomic_write_text）。

    写之前逐张规范化、丢掉没名字的、给撞了的 id 补一枚新的——卡池页送来的
    是浏览器里那份（可能少字段、也可能刚「复制一张」出来两张同 id 的），
    这些规矩不该让前端自己知道。
    """
    path = _cards_path()
    if not path:
        return False
    from app.agents import _atomic_write_text
    out, seen = [], set()
    for c in (cards or []):
        if not isinstance(c, dict):
            continue
        card = _norm_card(c)
        if not card["name"]:
            continue
        if card["id"] in seen:
            card["id"] = str(int(time.time() * 1000))
        seen.add(card["id"])
        out.append(card)
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    _atomic_write_text(path, json.dumps(out, ensure_ascii=False, indent=2))
    return True


def get_card(card_id):
    cid = str(card_id or "").strip()
    if not cid:
        return None
    for c in load_cards():
        if c["id"] == cid:
            return c
    return None


def find_card(word):
    """按卡名找卡；找不到再按 id 找。**精确匹配**（忽略首尾空白与大小写）。

    不做模糊匹配是有意的：`#` 开头的消息认不出卡就**当普通消息放行**（见
    `handle_command`），所以认错比认不出更糟——模糊匹配会让「#银发少女怎么画」
    这种正常问句被吃成命令。
    """
    key = str(word or "").strip()
    if not key:
        return None
    low = key.lower()
    cards = load_cards()
    for c in cards:
        if c["name"].lower() == low:
            return c
    for c in cards:
        if c["id"].lower() == low:
            return c
    return None


# ─── 会话状态 ────────────────────────────────────────

_STATE_PATH = os.path.join(BASE_DIR, "state", "persona_sessions.json")


def _load_state():
    try:
        with open(_STATE_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def _save_state(state):
    from app.agents import _atomic_write_text
    parent = os.path.dirname(_STATE_PATH)
    if parent:
        os.makedirs(parent, exist_ok=True)
    _atomic_write_text(_STATE_PATH, json.dumps(state, ensure_ascii=False, indent=2))


def _entry(session_key):
    st = _load_state().get(session_key)
    return st if isinstance(st, dict) else None


def active_card(session_key):
    """这条会话线正在演谁（返回卡 dict）；没在演、或卡已经被删了 → None。

    卡被删掉时自动当作没在演：不然会一直卡在一个取不到的卡 id 上，
    每轮都拼不出人设，还只能靠 `#出戏` 自救。
    """
    st = _entry(session_key)
    if not st:
        return None
    cid = str(st.get("card") or "").strip()
    if not cid:
        return None
    return get_card(cid)


def switch(session_key, card):
    """进扮演态。**换卡 = 清掉这条线的小记和轮次**：那是上一个角色的记忆。"""
    state = _load_state()
    state[session_key] = {"card": card["id"], "note": "", "turns": 0}
    _save_state(state)
    log.info("扮演态开启：%s → %s（%s）", session_key, card["name"], card["id"])


def leave(session_key):
    """退出扮演态。历史留在盘上不管——下次演同一个角色还能接着聊。"""
    state = _load_state()
    had = state.pop(session_key, None)
    if had is not None:
        _save_state(state)
        log.info("扮演态结束：%s（原 %s）", session_key, had.get("card"))


# ─── 命令 ────────────────────────────────────────────

def roster_text():
    """`#角色扮演` / `#人设列表` 的回话：清单 + 怎么用。"""
    cards = load_cards()
    if not cards:
        return "我这儿还没有人设卡。"
    lines = ["想让我演谁，就发「#角色名」，我会用那个角色的口气跟你聊。",
             "发「#出戏」我就变回生图机器人。",
             ""]
    for c in cards:
        tag = c.get("description") or c.get("personality") or ""
        lines.append("#%s%s" % (c["name"], ("　" + tag) if tag else ""))
    return "\n".join(lines)


def handle_command(session_key, text):
    """认 `#` 命令。返回要回的话；**不是命令返回 None**（照常往下走）。

    只认「整条消息正好是 `#命令词`」这一种形态，所以：

      - `#和泉纱雾`   → 进角色
      - `#出戏`       → 退出
      - `#角色扮演`   → 列清单（`#人设列表` 同义）
      - `#银发少女怎么画` → 认不出卡名 → **当普通消息**，不拦
    """
    t = (text or "").strip()
    if not t.startswith("#"):
        return None
    body = t[1:].strip()
    if not body:
        return None

    if body in LIST_WORDS:
        return roster_text()
    if body in EXIT_WORDS:
        if not _entry(session_key):
            return None                     # 本来就没在演，当普通消息
        leave(session_key)
        return "出戏了，我又变回%s了。" % QQ_BOT_NAME

    card = find_card(body)
    if card is None:
        return None
    was = active_card(session_key)
    switch(session_key, card)
    if was and was["id"] == card["id"]:
        return "我现在就是%s呀。" % card["name"]
    return "好，从现在起我是%s。想让我变回来就发「#出戏」。" % card["name"]


# ─── 一轮 ────────────────────────────────────────────

def _hist_path(session_key):
    """扮演态的历史单独一份，**不跟生图机器人共用**。

    共用的话，切进角色时会先看到一串「银发少女 1024×1536 anima多图」，
    角色开口就串味；切出去以后生图机器人又得读一遍角色说过的话。
    文件名带 `persona_` 前缀，管理页里能一眼分出来。
    """
    from app import agents
    if not _SAFE_KEY.match(session_key or ""):
        return None
    d = agents.agent_dir(QQ_AGENT_ID)
    if not d:
        return None
    return os.path.join(d, "sessions", "persona_" + session_key + ".jsonl")


def _load_hist(session_key):
    path = _hist_path(session_key)
    if not path or not os.path.exists(path):
        return []
    out = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue            # 单行坏了不该让整段历史读不出来
    except OSError:
        return []
    return out


def _save_hist(session_key, msgs):
    path = _hist_path(session_key)
    if not path:
        return
    from app.agents import _atomic_write_text
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    _atomic_write_text(path,
                       "".join(json.dumps(m, ensure_ascii=False) + "\n"
                               for m in msgs))


def trim(hist):
    """取最近 HISTORY_LIMIT 条，单条超 MSG_MAX_CHARS 截断。

    裁的是**送进模型的副本**；落盘那份也是裁过的（扮演态的历史只留窗口内的
    20 条，久了不聊自然就淡了，这也是用户要的「先试 20 条」）。
    """
    msgs = [m for m in (hist or [])
            if isinstance(m, dict) and m.get("role") in ("user", "assistant")]
    msgs = msgs[-HISTORY_LIMIT:]
    out = []
    for m in msgs:
        c = str(m.get("content") or "").strip()
        if len(c) > MSG_MAX_CHARS:
            c = c[:MSG_MAX_CHARS] + "…"
        out.append({"role": m["role"], "content": c})
    return out


def build_head(card, session_key, sender, note=""):
    """扮演态的系统头：角色人设 + 输出契约 + 环境（+ 小记）。

    **刻意不带别的东西**——不拼工具目录、不拼 Skill 目录、不拼表情包清单、
    不拼长期记忆，也不拼任何审核 / 限制条款。用户 2026-10-16 点名：
    「人设是什么，人设就是什么」；扮演态本来就不给模型工具，拼那些它也用
    不上、还白烧 token（他那套角色扮演系统里，一万字的提示词有五千字是
    审核话术，他明确不要这个）。

    头在会话内是稳定的（只有换卡或小记重写才变），所以能吃到前缀缓存。
    """
    parts = []
    persona = str(card.get("systemPrompt") or "").strip()
    parts.append(persona if persona else "你是%s。" % card["name"])
    parts.append(_CONTRACT)
    parts.append(_env_note(session_key, sender))
    if note.strip():
        parts.append("【之前的事】\n" + note.strip())
    return "\n\n".join(parts)


def _env_note(session_key, sender):
    """告诉它此刻在私聊还是群里、是谁在跟它说话。

    群里必须点出说话人：角色面对的是「一屋子人」，不点名它就会答成一对多；
    私聊没有这个问题，也不需要昵称。
    """
    target, _, tid = (session_key or "").partition("_")
    if target == "group":
        return ("【当前环境】你在一个 QQ 群里（群号 %s），群里有很多人。"
                "刚才跟你说话的是%s，回他一个就行，别管其他人。"
                % (tid, sender or "群里某个人"))
    return "【当前环境】你在跟一个人私聊（QQ %s）。" % tid


def run_turn(session_key, text, target, sender=""):
    """扮演态的一轮：**1 次** LLM 调用，返回 `Turn(正文, 画面)`。

    正文空 = 这轮什么都不发（调用方别开口）；画面空 = 这轮不出图（见 `split_scene`）。
    """
    card = active_card(session_key)
    if card is None:
        return Turn("", "")

    note = str((_entry(session_key) or {}).get("note") or "")
    head = build_head(card, session_key, sender, note)
    hist = trim(_load_hist(session_key))

    # 群里带上说话人，模型才知道是谁在说话（同 qq_bot 正文前缀的做法）；
    # 私聊和空正文都不加——「张三：」后面什么都没有比不写更糊涂。
    said = text
    if target == "group" and sender and text:
        said = sender + "：" + text

    msgs = ([{"role": "system", "content": head}] + hist
            + [{"role": "user", "content": said}])
    try:
        raw = llm.call_llm(msgs)
    except Exception:
        log.exception("扮演轮调用失败 %s", session_key)
        return Turn("", "")
    reply, scene = split_scene(raw)
    if not reply:
        # 整轮空 = 模型没交作业。这属于可用性问题，值得说一句，
        # 但**不重跑**：重跑一次未必更好，还白烧一轮。
        log.warning("扮演轮空回复 %s", session_key)
        return Turn("（我这会儿有点卡，你再说一句？）", "")

    # 落历史：只放对话本身，**不留「画面：」那一行**——它是给代码的元数据，
    # 不是角色说过的话。system 头和小记也是每轮重新拼的，同样不进历史。
    hist.append({"role": "user", "content": said})
    hist.append({"role": "assistant", "content": reply})
    try:
        _save_hist(session_key, hist[-HISTORY_LIMIT:])
    except Exception:
        log.exception("扮演轮落史失败 %s", session_key)

    _bump_turn(session_key)
    return Turn(reply, scene)


# ─── 画面与出图 ──────────────────────────────────────

def split_scene(text):
    """把模型这一轮的输出切成 `(正文, 画面标签)`。

    契约见 `_CONTRACT`：正文之后另起一行写「画面：<英文标签>」。这里只做**解析**：

      - 所有「画面：」行都从正文里去掉（模型偶尔会写两行），取**最后一行**当画面；
      - 没有这一行 → 画面为空串，调用方**不许兜底**。这一轮少一张图，好过拿
        上一轮或角色卡去凑一张对不上剧情的图（用户 2026-10-16：代码只做检测，
        判断留给 AI）。
    """
    raw = str(text or "")
    hits = list(_SCENE_RE.finditer(raw))
    if not hits:
        return raw.strip(), ""
    scene = _clean_scene(hits[-1].group(1))
    body = _SCENE_RE.sub("", raw)
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    return body, scene


def _clean_scene(raw):
    """画面标签的最小清理：去反引号/星号、去首尾逗号和空白。"""
    s = _SCENE_NOISE_RE.sub("", str(raw or "")).strip()
    return s.strip(",").strip()


def _fixed_block(card):
    """角色卡上的**外观固定块**：每轮都拼在画面标签前面，保证同一个角色不换脸。

    两个字段都收（`appearancePrompt` 是外观、`outfitPrompt` 是服饰），都没有就
    留空——空固定块也出得了图，只是角色长相交给模型自由发挥。
    """
    parts = [str(card.get("appearancePrompt") or "").strip(),
             str(card.get("outfitPrompt") or "").strip()]
    return ", ".join(p for p in parts if p).strip(", ").strip()


def build_prompt(card, scene):
    """出图提示词 = 角色卡固定块 + 这一轮的动态画面。"""
    fixed, scene = _fixed_block(card), _clean_scene(scene)
    if fixed and scene:
        return fixed + ", " + scene
    return fixed or scene


def _session_target(session_key):
    """会话线 → `(target, target_id)`（`group_123` → `("group", "123")`）。"""
    target, _, tid = str(session_key or "").partition("_")
    if target not in ("group", "private") or not tid:
        return "", ""
    return target, tid


def draw(scene, card, session_key):
    """出一张配图，返回文件名（没画出来返回 ""）。**阻塞**——调用方放后台线程。

    走 `skills/_persona_fast`（480×720 / 3 步的 silver 漫画变体，实测 ≈2~3 秒）。
    入队带 `quiet=True`：图直接发回会话、**不带编号 caption、失败也不刷话术**
    ——聊天里突然冒一句「图没画出来」比不画还糟。

    也不扣私聊每日额度：扮演态配图是聊天的一部分，不是「点单生图」。队列满 /
    ComfyUI 没在线 / 被审核拦下，一律静默跳过这一轮，聊天照常。
    """
    from app import image_jobs
    from app.tools.normal import generate_image as gi

    prompt = build_prompt(card, scene)
    target, target_id = _session_target(session_key)
    if not _clean_scene(scene) or not prompt or not target:
        return ""       # 没写画面行 = 这一轮不出图，调用方不用替它守
    seed = random.randint(0, 2 ** 32 - 1)
    wf = gi.build_t2i_workflow(FAST_SKILL, prompt, seed)
    if wf is None:
        log.warning("扮演配图渠道 %s 没有文生图工作流", FAST_SKILL)
        return ""
    if not image_jobs.comfy_alive():
        log.info("扮演配图：ComfyUI 没在线，%s 这一轮不出图", session_key)
        return ""
    try:
        # landscape=False 写死：这里跑在后台线程，读不到本轮原话（qq_api 的
        # 上下文是 threading.local），不能让 enqueue 自己去判——判不出会退回
        # 「本轮原话里有没有横屏」，而这里恒为 None。
        job, reason = image_jobs.enqueue(
            target, target_id, wf, skill=FAST_SKILL, prompt=prompt,
            seed=seed, landscape=False, quiet=True)
    except Exception:
        log.exception("扮演配图入队失败 %s", session_key)
        return ""
    if reason:
        log.info("扮演配图没排上（%s）：%s", session_key, reason)
        return ""
    try:
        entry = job.wait()
    except Exception as e:
        log.info("扮演配图没画出来（%s）：%s", session_key, e)
        return ""
    names = image_jobs.output_images(entry, job=job)
    if not names:
        log.warning("扮演配图跑完却没有图片（%s）", session_key)
        return ""
    log.info("扮演配图完成 %s：%s（%s）", session_key, names[0], prompt[:60])
    return names[0]


def _bump_turn(session_key):
    """轮次 +1；攒够 NOTE_EVERY_TURNS 轮就在后台压一次小记。

    放后台：压缩要一次 LLM 调用，不该让第 10 轮那个人的回话凭空慢几秒。
    """
    state = _load_state()
    st = state.get(session_key)
    if not isinstance(st, dict):
        return
    st["turns"] = int(st.get("turns") or 0) + 1
    _save_state(state)
    if st["turns"] >= NOTE_EVERY_TURNS:
        threading.Thread(target=compress_note, args=(session_key,),
                         name="persona-note", daemon=True).start()


def compress_note(session_key):
    """把旧小记 + 最近这 20 条压成一份 ≤500 字的新小记。失败就保持原样。

    读的是**刚写过的那份历史**，所以不必由调用方传进来。
    """
    card = active_card(session_key)
    if card is None:
        return False
    st = _entry(session_key) or {}
    old = str(st.get("note") or "").strip()
    hist = trim(_load_hist(session_key))
    if not hist:
        return False
    convo = "\n".join(
        "%s：%s" % ("我" if m["role"] == "assistant" else "对方", m["content"])
        for m in hist)
    prompt = _NOTE_PROMPT % (NOTE_MAX_CHARS, old or "（还没有）", convo)
    try:
        note = str(llm.call_llm([{"role": "user", "content": prompt}]) or "").strip()
    except Exception:
        log.exception("小记压缩失败 %s", session_key)
        return False
    if not note:
        return False
    state = _load_state()
    st = state.get(session_key)
    if not isinstance(st, dict):
        return False                          # 压缩期间出戏了，别把它写回去
    st["note"] = note[:NOTE_MAX_CHARS]
    st["turns"] = 0
    _save_state(state)
    log.info("小记已更新 %s（%d 字）", session_key, len(st["note"]))
    return True
