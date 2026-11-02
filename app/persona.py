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
# 2026-11-02：加三个字段做「自由模式」的情绪与视觉状态。
# `mood` 是情绪名（十个之一），`intensity` 是 0~10 的整数，`state` 是她此刻
# 身上的客观状态（穿着、位置、有没有肢体接触）。用 defaults 而不是新加一个
# 类：老调用方读 `.reply` / `.scene` 完全不受影响。
Turn = collections.namedtuple(
    "Turn", "reply scene mood intensity state", defaults=("", 0, ""))

# 情绪词表（自由模式）。模型只能从这十个里挑，多一个字都不认——不限定的话
# 它会每轮自己发明一个词，代码就没法按强度做出图判断了。
MOODS = ("平静", "开心", "害羞", "紧张", "生气", "撒娇", "心动", "难过", "疲惫", "暧昧")

# 出图门槛：强度到这条线才配图，或者这一轮发生了服装变化。
# 每轮都出会把对话框刷成图册，而且白烧显卡（用户 2026-11-02 定）。
DRAW_INTENSITY = 7

# 「心情：<情绪> 强度<数字>」那一行。半角/全角冒号、全角数字都认。
_MOOD_RE = re.compile(r"^[ \t]*心情[：:][ \t]*(.+)$", re.M)
# 「状态：<一句话>」那一行。
_STATE_RE = re.compile(r"^[ \t]*状态[：:][ \t]*(.+)$", re.M)

# 服装变化判定用的关键词。**只用来触发配图，不用来改画面**——真正的穿着
# 描述仍由模型写在「画面：」里。
#
# 2026-11-02 收紧过一次：原来收了「衣 / 裙 / 衬衫」这些名词，结果状态里正常
# 写着「家居卫衣」也会命中，等于每轮都判成换衣、每轮都出图。现在只认
# **动作词**（脱、解开、褪、换…），名词交给「文案变化」那条判据。
_CLOTHES_ACTIONS = (
    "脱", "解开", "拉开", "褪", "滑落", "掀", "撩起", "换上", "换回", "穿上",
    "披上", "裹着", "只穿", "仅剩", "只剩", "浴巾", "裸",
)
# 服装名词：**只收「穿在身上」的那类**。手里抱着的外套、搭在椅背上的浴巾
# 都不算换装信号，混进来会让「接过外套」被误判成换衣（2026-11-02 实测）。
_CLOTHES_NOUNS = (
    "衬衫", "裙", "裤", "袜", "鞋", "内衣", "浴袍", "睡衣", "卫衣", "毛衣",
    "制服", "校服", "泳装", "浴巾",
)

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
    "\n"
    "## 输出格式\n"
    "\n"
    "每一轮都必须按下面四段输出，**缺一段都不行**：\n"
    "\n"
    "1. **正文** —— 以这个角色的口吻说想说的话，就像真的在跟他聊天。"
    "心里想的事写在括号里。正文可以多行。\n"
    "2. **`画面：`** —— 英文一行。\n"
    "3. **`心情：`** —— 情绪 + 强度。\n"
    "4. **`状态：`** —— 中文一句话。\n"
    "\n"
    "正文之后空一行，然后严格按 2→3→4 的顺序写三行，每行以行首的中文标签开头。"
    "这三行是格式要求，不是正文：不要用括号包、不要改标签名、不要写进对话里。\n"
    "\n"
    "## 画面\n"
    "\n"
    "用英文写一行，属性用标签（小写，多词用下划线），方位和互动用英文短语补，"
    "例如 `on the left side of the desk`、`holding a mug in her right hand`。\n"
    "\n"
    "要写清：在哪、在做什么、什么表情、什么东西在她哪一侧、什么镜头、什么光。\n"
    "\n"
    "**她长什么样不用写，我照角色卡上的样子画。**\n"
    "\n"
    "## 心情\n"
    "\n"
    "格式：`情绪 强度N`，N 是 0~10 的整数。\n"
    "\n"
    "情绪只能从这十个里挑**一个**，不许自己编：\n"
    + " / ".join(MOODS) + "\n"
    "\n"
    "强度说这一下来的有多猛：4 以下当作平常，7 以上我才会给她配一张图。\n"
    "\n"
    "## 状态\n"
    "\n"
    "她此刻身上的客观状态，用「·」分成几段，**三段都要有**：\n"
    "\n"
    "- 在哪\n"
    "- **穿什么**（具体到哪件、什么状态。这一项最重要，直接决定画面里她穿什么）\n"
    "- 跟他有没有肢体接触\n"
    "\n"
    "示例：`靠窗坐着 · 白衬衫解开两粒扣 · 你扶着她的肩`\n"
    "\n"
    "两条硬规则：\n"
    "\n"
    "- **穿着必须写清楚**。写「宽松白T恤和居家短裤」「浴袍」「只裹着浴巾」，"
    "不写空、不写「穿着衣服」这种含糊话。\n"
    "- **状态接着上一轮往下写**。上一轮脱掉的外套这一轮不会自己穿回去；"
    "上一轮已经全裸，这一轮就写全裸，不要凭空给她穿回衣服。\n"
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

# 扮演态专用的配图渠道。
#
# 2026-11-02 换成 `silver`（用户定）：默认生图渠道，两段采样（12 步 + 8 步）
# 能把提示词里的服装描述落实下来。之前的 `_persona_fast` 是单段 3 步的
# BatchPromptImageGenerator，同样 6 个 LoRA、同一个底模，但 3 步只抓最强的主语
# （`1girl, silver hair, solo`），「浅灰色宽松卫衣」这种后段弱语义直接丢，
# 实测画成了裸体。
#
# 代价是慢（两段采样 + 两级放大），但排队是正常现象、用户等得起（用户原话）。
# 走的是**正常 QQ 队列**，不插队、不特殊处理——单张图别把自己排成特例。
FAST_SKILL = "silver"

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
    """进扮演态。**换卡 = 清掉这条线的小记和轮次**：那是上一个角色的记忆。

    同时清掉情绪与状态（`mood` / `intensity` / `state`）：上一个角色害羞到 9，
    不该带进下一个角色。
    """
    state = _load_state()
    state[session_key] = {"card": card["id"], "note": "", "turns": 0,
                          "mood": "", "intensity": 0, "state": ""}
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

    2026-11-02：assistant 那几条**保留结构化尾巴**。截断只砍正文，最后三行
    （画面 / 心情 / 状态）原样留着——它们是 few-shot 示范，历史里全是纯对话
    的话，模型第 2 轮起就会跟着历史把格式丢掉（实测第 1 轮完美、第 2 轮全空）。
    """
    msgs = [m for m in (hist or [])
            if isinstance(m, dict) and m.get("role") in ("user", "assistant")]
    msgs = msgs[-HISTORY_LIMIT:]
    out = []
    for m in msgs:
        c = str(m.get("content") or "").strip()
        if len(c) > MSG_MAX_CHARS:
            if m["role"] == "assistant":
                body, tail = _split_meta(c)
                room = max(20, MSG_MAX_CHARS - len(tail) - 1)
                c = body[:room].rstrip() + "…" + tail
            else:
                c = c[:MSG_MAX_CHARS] + "…"
        out.append({"role": m["role"], "content": c})
    return out


def _split_meta(text):
    """把一条 assistant 回复切成「正文」和「结构化的三行」。

    返回 `(正文, "\\n画面：…\\n心情：…\\n状态：…")`；没有结构化行时尾巴为空串。
    用行首匹配而不是别处出现的词，避免正文里提到「画面」被误切。
    """
    lines = str(text or "").split("\n")
    keep, idx = [], len(lines)
    # 从尾巴往回找连续的三类元数据行（允许中间夹空行）
    metas, i = [], len(lines) - 1
    while i >= 0:
        ln = lines[i].strip()
        if not ln or any(rx.match(ln) for rx in (_SCENE_RE, _MOOD_RE, _STATE_RE)):
            if ln and any(rx.match(ln) for rx in (_SCENE_RE, _MOOD_RE, _STATE_RE)):
                metas.insert(0, lines[i])
                idx = i
            i -= 1
            continue
        break
    if not metas:
        return str(text or ""), ""
    keep = lines[:idx]
    return "\n".join(keep).rstrip() + "\n", "\n" + "\n".join(metas)


def build_head(card, session_key, sender, note="", state=""):
    """扮演态的系统头：角色人设 + 输出契约 + 环境（+ 小记 + 此刻状态）。

    **刻意不带别的东西**——不拼工具目录、不拼 Skill 目录、不拼表情包清单、
    不拼长期记忆，也不拼任何审核 / 限制条款。用户 2026-10-16 点名：
    「人设是什么，人设就是什么」；扮演态本来就不给模型工具，拼那些它也用
    不上、还白烧 token（他那套角色扮演系统里，一万字的提示词有五千字是
    审核话术，他明确不要这个）。

    `state`（2026-11-02）：上一轮她身上的状态（穿着 / 位置 / 接触）。不喂回去
    的话，模型每轮都从零猜「她现在穿什么」，几轮之后衣服会自己长回来。
    """
    parts = []
    persona = str(card.get("systemPrompt") or "").strip()
    parts.append(persona if persona else "你是%s。" % card["name"])
    parts.append(_CONTRACT)
    parts.append(_env_note(session_key, sender))
    if note.strip():
        parts.append("【之前的事】\n" + note.strip())
    if str(state or "").strip():
        parts.append("## 她此刻的样子\n\n" + str(state).strip()
                     + "\n\n（这是上一轮结束时的状态，接着它往下演，"
                       "不要自己给她换回原来的衣服）")
    return "\n\n".join(parts)


def _env_note(session_key, sender):
    """告诉它此刻在私聊还是群里、是谁在跟它说话。

    群里必须点出说话人：角色面对的是「一屋子人」，不点名它就会答成一对多；
    私聊没有这个问题，也不需要昵称。
    """
    target, _, tid = (session_key or "").partition("_")
    if target == "group":
        return ("## 当前环境\n\n你在一个 QQ 群里（群号 %s），群里有很多人。"
                "刚才跟你说话的是%s，回他一个就行，别管其他人。"
                % (tid, sender or "群里某个人"))
    return "## 当前环境\n\n你在跟一个人私聊（QQ %s）。" % tid


def run_turn(session_key, text, target, sender=""):
    """扮演态的一轮：**1 次** LLM 调用，返回 `Turn(正文, 画面)`。

    正文空 = 这轮什么都不发（调用方别开口）；画面空 = 这轮不出图（见 `split_scene`）。
    """
    card = active_card(session_key)
    if card is None:
        return Turn("", "")

    ent = _entry(session_key) or {}
    note = str(ent.get("note") or "")
    prev_state = str(ent.get("state") or "")
    head = build_head(card, session_key, sender, note, prev_state)
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
    reply, scene, mood, intensity, state = split_turn(
        raw, default_mood=str(ent.get("mood") or ""))
    if not reply:
        # 整轮空 = 模型没交作业。这属于可用性问题，值得说一句，
        # 但**不重跑**：重跑一次未必更好，还白烧一轮。
        log.warning("扮演轮空回复 %s", session_key)
        return Turn("（我这会儿有点卡，你再说一句？）", "")

    # 落历史：存**完整原文**（含「画面 / 心情 / 状态」三行）。2026-11-02 改：
    # 这三行是下一轮的 few-shot 示范，剥掉的话模型第 2 轮起就把格式丢了
    # （实测第 1 轮完美、第 2 轮全空）。展示时由 `split_turn` 再剥一次。
    hist.append({"role": "user", "content": said})
    hist.append({"role": "assistant", "content": str(raw or "").strip()})
    try:
        _save_hist(session_key, hist[-HISTORY_LIMIT:])
    except Exception:
        log.exception("扮演轮落史失败 %s", session_key)

    # 情绪与状态落盘（2026-11-02）：只覆盖不累加——情绪是当下的，
    # 状态是这一格的结果，下一轮读 `state` 续着演。
    _save_mood(session_key, mood, intensity, state, prev_state)

    _bump_turn(session_key)
    return Turn(reply, scene, mood, intensity, state)


def _save_mood(session_key, mood, intensity, state, prev_state=""):
    """把这一轮的情绪/状态写回会话条目。**已经出戏的不写。**

    2026-11-02：模型偶尔整轮不交三行（同一份契约、同一句话，实测有 1/6 概率
    全空）。这时候**保留上一轮的值**而不是写空——否则下一轮 `state` 变成空串，
    「她穿着什么」这条线就断了，出图又会回到裸体。
    """
    state_data = _load_state()
    st = state_data.get(session_key)
    if not isinstance(st, dict):
        return
    if mood:
        st["mood"] = mood
    try:
        val = int(intensity or 0)
    except (TypeError, ValueError):
        val = 0
    if val or not st.get("intensity"):
        st["intensity"] = val
    if state:
        st["state"] = str(state)[:200]
    _save_state(state_data)


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


def split_turn(text, default_mood=""):
    """把一轮输出切成 `(正文, 画面, 情绪, 强度, 状态)`。

    「画面：」「心情：」「状态：」三行都是给代码的元数据，**全部从正文里剥掉**
    ——用户不该在聊天里看到「状态：白衬衫解开两粒扣」这种东西。

    三条都取**最后一处**匹配（同 `_SCENE_RE` 的理由：正文里偶尔会提到这些词）。
    缺哪行就当没写：情绪空串、强度 0、状态空串，调用方不许兜底。
    """
    raw = str(text or "")
    body = raw
    out = {"scene": "", "mood": "", "intensity": 0, "state": ""}

    for key, rx in (("scene", _SCENE_RE), ("mood", _MOOD_RE), ("state", _STATE_RE)):
        hits = list(rx.finditer(body))
        if not hits:
            continue
        val = hits[-1].group(1).strip()
        body = rx.sub("", body)
        out[key] = val

    out["scene"] = _clean_scene(out["scene"])
    out["mood"], out["intensity"] = _parse_mood(out["mood"])
    out["state"] = _clean_scene(out["state"])
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    if not out["mood"] and default_mood:
        out["mood"] = default_mood
    return body, out["scene"], out["mood"], out["intensity"], out["state"]


def _parse_mood(raw):
    """「害羞 强度8」/「心动强度7」/「暧昧 强度 9」 → `("害羞", 8)`。

    情绪必须落在 `MOODS` 里，否则当没写——模型偶尔会自创「怅然若失」这种词，
    收录它等于让出图门槛失效。强度钳到 0~10。
    """
    s = str(raw or "").strip()
    if not s:
        return "", 0
    m = re.search(r"强度\s*[：:]?\s*(\d{1,2})", s)
    val = int(m.group(1)) if m else 0
    val = max(0, min(10, val))
    head = (s[:m.start()] if m else s).strip(" ，,·:：")
    for mood in MOODS:
        if mood in head:
            return mood, val
    return "", val


def clothes_changed(prev_state, state):
    """这一轮的「状态」里有没有服装变化。**只用来决定配不配图。**

    两条判据，任一成立才算：
      1. 新状态里出现**动作词**（脱 / 解开 / 褪 / 换上 …）；
      2. 两轮的服装名词集合不一样（多了「浴巾」、少了「外套」）。
    只是「穿着家居卫衣」这种静态描述**不算变化**——否则每轮都命中，
    每轮都出图，等于没有门槛（2026-11-02 实测踩过）。
    """
    cur = str(state or "").strip()
    if not cur or cur == str(prev_state or "").strip():
        return False
    if any(w in cur for w in _CLOTHES_ACTIONS):
        return True
    prev = str(prev_state or "").strip()
    now_nouns = {w for w in _CLOTHES_NOUNS if w in cur}
    old_nouns = {w for w in _CLOTHES_NOUNS if w in prev}
    return now_nouns != old_nouns


def should_draw(intensity, prev_state, state):
    """这一轮要不要配图：情绪够强，或者身上换了衣服。"""
    try:
        if int(intensity or 0) >= DRAW_INTENSITY:
            return True
    except (TypeError, ValueError):
        pass
    return clothes_changed(prev_state, state)


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


def _outfit_from_state(state):
    """从「状态」里抠出「穿什么」那一段，翻成英文标签。

    2026-11-02：**这是服装词唯一的来源**。模型写「画面」那一行时经常整段不写
    衣服（只写动作/表情/镜头），而它自己写的「状态」里明明有「穿着灰色家居服」。
    两行都由模型产出，但只有「状态」被要求写穿着，所以从这里取。

    状态是「·」分段的：`坐在房间书桌前 · 穿着宽松的灰色家居服 · 没有肢体接触`。
    取含衣着关键词的那一段，用 `_OUTFIT_MAP` 翻成英文；翻不出来就原样丢弃
    （中文塞进英文提示词没用，还会干扰）。
    """
    segs = [s.strip() for s in str(state or "").split("·") if s.strip()]
    out = []
    for seg in segs:
        # 先排除「没有接触 / 无接触」这类段——它们也含「没有」，若不先排除，
        # 后面判断衣着时会被误伤（2026-11-02 实测：这段写在中间，导致整句被跳）。
        if "没有肢体接触" in seg or "无接触" in seg or "没有接触" in seg:
            continue
        hit = False
        # 一段里常写着**两件**单品（「白T恤和居家短裤」），所以这里不是 break，
        # 而是把命中的都收下；同类别冲突交给 `_OUTFIT_GROUPS` 去重。
        for en, words in _OUTFIT_MAP.items():
            if any(w in seg for w in words):
                out.append(en)
                hit = True
        if not hit and any(w in seg for w in _CLOTHES_NOUNS + _CLOTHES_ACTIONS):
            # 认得出是衣着段但翻不出来：不硬塞中文，只提醒**这一轮她有穿**，
            # 让负面词 + `dressed` 一起兜住「不许裸」。
            out.append("fully dressed in casual clothes")
    # 去重但保序
    seen, uniq = set(), []
    for e in out:
        if e not in seen:
            seen.add(e)
            uniq.append(e)
    # 同类只留第一个：`skirt` 和 `pleated skirt` 选一个、`shirt` 和 `white blouse`
    # 选一个，否则提示词里会同时出现两条互相矛盾的描述。
    picked, used_groups = [], set()
    for e in uniq:
        grp = _OUTFIT_GROUPS.get(e, e)
        if grp in used_groups:
            continue
        used_groups.add(grp)
        picked.append(e)
    return ", ".join(picked)


# 同类别分组：组内只保留第一个命中的词。没列出的按自身当独立组。
_OUTFIT_GROUPS = {
    "white blouse": "shirt",
    "shirt": "shirt",
    "white t-shirt": "shirt",
    "grey hoodie": "top",
    "knit sweater": "top",
    "casual loungewear": "top",
    "pleated skirt": "skirt",
    "skirt": "skirt",
    "lounge shorts": "bottom",
    "lounge pants": "bottom",
}


# 中文衣着 → 英文提示词片段。
#
# 2026-11-02 补过一轮：头版只有 8 组，实测「宽松白T恤和居家短裤」整段只命中
# 一个 `裤`，拼出 `long-sleeved casual top and shorts` 这种和实物不符的词。
# 现在按**具体单品**铺开，顺序即优先级（`_outfit_from_state` 里一段只认第一个
# 命中的）。
_OUTFIT_MAP = {
    "white t-shirt": ("T恤", "T shirt", "t-shirt", "短袖"),
    "grey hoodie": ("卫衣", "连帽"),
    "lounge shorts": ("短裤",),
    "lounge pants": ("长裤",),
    "knit sweater": ("毛衣", "针织"),
    "white blouse": ("白衬衫",),
    "shirt": ("衬衫",),
    "pleated skirt": ("百褶裙",),
    "skirt": ("裙",),
    "school uniform": ("校服", "制服"),
    "pajamas": ("睡衣",),
    "bathrobe": ("浴袍", "浴巾"),
    "casual loungewear": ("家居服", "居家服"),
}


def build_prompt(card, scene, state=""):
    """出图提示词 = 角色卡固定块 + 这一轮的状态（衣着）+ 动态画面。

    `state`（2026-11-02）：模型在「状态」行里写了她此刻的穿着。不拼进来的话，
    模型写「画面」时漏写衣服，出来的就是裸体（实测两次）。
    """
    fixed, scene = _fixed_block(card), _clean_scene(scene)
    outfit = _outfit_from_state(state)
    body = ", ".join(p for p in (outfit, scene) if p)
    if fixed and body:
        return fixed + ", " + body
    return fixed or body


def image_caption(spoken, limit=20):
    """配图下面的那行字：`配图 ·「她说的话（前 limit 字）」`。

    为什么要有它：图是 2~3 秒后才出来的，那时候她可能已经又说了几句。
    光靠引用玩家那条消息，用户未必记得这张图对应她哪一句（2026-11-02 用户要的）。

    2026-11-02：**跳过开头的括号段**再取。扮演态的正文常常以
    「（听到门响，整个人缩了一下）」这种动作描写开场，直接截前 20 字会全是
    括号，看不出是哪句话。优先取第一句**不在括号里**的台词。
    """
    raw = re.sub(r"\s+", " ", str(spoken or "")).strip()
    if not raw:
        return ""
    # 把开头连续的括号段丢掉
    s = raw
    while s.startswith("（") or s.startswith("("):
        end = max(s.find("）"), s.find(")"))
        if end < 0:
            break
        s = s[end + 1:].strip()
    if not s:
        s = raw                      # 整段都是动作描写，那就退回原文
    if len(s) > limit:
        s = s[:limit].rstrip() + "…"
    return "配图 ·「%s」" % s


def _session_target(session_key):
    """会话线 → `(target, target_id)`（`group_123` → `("group", "123")`）。"""
    target, _, tid = str(session_key or "").partition("_")
    if target not in ("group", "private") or not tid:
        return "", ""
    return target, tid


# 2026-11-02 撤掉：原来这里有一段给「不许裸」追加负面词（`nude, naked,
# topless...`）的 `_apply_nude_neg`，理由是 silver 那套 LoRA 裸体倾向强。
#
# 实测推翻了那个判断：几轮出裸图的提示词里**一个服装词都没有**——模型只写了
# 动作、表情、镜头，没写穿什么，模型只能落回先验。补上 `grey hoodie,
# lounge shorts` 之后立刻就穿回去了，负面词根本没参与。
#
# 而且 cfg=1 的 turbo 档里负面词本就没多少效果，那段反而会把「她真的脱了」
# 这个场景一并压住——用户要的恰恰是「状态里写什么就是什么」。所以整段删掉，
# 服装完全交给正面提示词里 `状态` 行翻译出来的标签。


def draw(scene, card, session_key, reply_to="", state=""):
    """出一张配图，返回文件名（没画出来返回 ""）。**阻塞**——调用方放后台线程。

    走 `silver`（默认生图渠道，两段采样 12+8 步 → 1.2× 潜空间放大 → 2x 像素
    放大，出图约 2464×3680）。排队走**正常 QQ 队列**，不插队。
    入队带 `quiet=True`：worker 只跑图不发，图由调用方拿去 `_deliver`（要带
    caption 和引用，见 `qq_bot._draw_persona`）。

    `reply_to`（2026-11-02）：玩家触发这一轮的那条消息 id。**必须在消息线程上
    传进来**——`enqueue` 的 `reply_to=None` 会去读 `qq_api.current_message_id()`，
    而本函数跑在后台线程，那时上下文早不是本轮了，读出来是空或者别的轮次。
    这就是「图不引用消息」的根因。

    `state`（2026-11-02）：这一轮「状态」行里她穿什么。**必须传**——模型写
    「画面」时经常漏写衣服，只有这里才有服装信息，不拼进去就会画出裸体。

    也不扣私聊每日额度：扮演态配图是聊天的一部分，不是「点单生图」。队列满 /
    ComfyUI 没在线 / 被审核拦下，一律静默跳过这一轮，聊天照常。
    """
    from app import image_jobs
    from app.tools.normal import generate_image as gi

    prompt = build_prompt(card, scene, state)
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
            seed=seed, landscape=False, quiet=True,
            reply_to=str(reply_to or ""))
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
