"""QQ galgame 模式：剧本骨架写死，台词交给模型，关键节点出一张 CG。

跟 persona 是同一个形状（会话级状态 + 一轮一次 LLM 调用 + 后台出图），但
解决的是另一件事：persona 是「随便聊」，这里是「有剧情、有选项、有结局」。

## 分工：谁决定什么

| | 谁定 | 为什么 |
| :--- | :--- | :--- |
| 走哪条分支（`to`） | **代码**，玩家选 | 让模型跳转 = 剧情随时跑偏，还复现不了 |
| 好感度 / 标志位 | **代码**，按剧本加减 | 同上；而且结局判定要能算 |
| 这一段的台词和描写 | **模型** | 每条分支都手写台词，改一次要动剧本，写不动 |
| 选项文案 | **代码**，直接从剧本取 | 见下 |

最后一条是刻意的。`0d00-Ciallo-0721/astrbot_plugin_QQgal` 的作者在 README 里
自己吐槽过：模型不够聪明时「选项会乱写」，甚至给出根本走不通的分支。我们不
把选项交给模型，它就只有一件事可做——把骨架演出来。模型再烂也毁不掉选项。

## 为什么群聊共享进度

进度按 `session_key` 存，而 `session_key` 本身已经是 `private_<QQ号>` 或
`group_<群号>`（见 qq_bot._session_key）。所以「群里一份、私聊各一份」不是我
写的规则，是存储键天然长这样。群里十个人共享一条线、投票推进；想各玩各的
就私聊。

## 为什么不存对话历史

persona 要存（聊过什么要记得），galgame 不用：**当前进度就是全部上下文**。
节点里写着这一段的骨架，角色卡写着人设，两者拼起来足够演好这一幕。存历史
反而会把「上个分支说过的话」带进新分支，串味。所以进度文件很小（几百字节），
存档、读档、复制给别群都只要挪一个 JSON。

## CG 什么时候出

节点写了 `cg` 字段才出，而且**同一个节点只出一次**（`seen` 记着）。回退到
走过的节点不再重复画——不然读档一次就重画一遍，几十秒白等。
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
from app.config import BASE_DIR

log = logging.getLogger("galgame")

# 一轮的结果：`reply` 是要发回去的整段（正文 + 选项已拼好），`scene` 是这一轮
# 要出的 CG 描述（可能为空），`ended` 表示这一轮之后游戏已经结束。
Turn = collections.namedtuple("Turn", "reply scene ended")

# 剧本目录。跟 comics/ 一样放项目根，不进 state/——那是运行时数据，剧本是内容。
SCRIPTS_DIR = os.path.join(BASE_DIR, "galgame")
_STATE_PATH = os.path.join(BASE_DIR, "state", "galgame_sessions.json")
_SAVE_DIR = os.path.join(BASE_DIR, "state", "galgame_saves")

# 出 CG 走这条渠道。用户 2026-11-02 拍板「用现有 anima 渠道，不新增」。
# 注意它输出 2456×3686（末尾带 2× 像素放大），一张要几十秒，所以只能按节点出、
# 不能每句出。想换成轻量版把这行改成 "_persona_fast" 即可（480×720，2~3 秒）。
GAL_SKILL = "anima-line"

# `gal 开局` 不带剧本名时用这个；不存在就退回剧本目录里的第一个。
DEFAULT_SCRIPT = "sakura"

# 正文上限。给选项留位置——一段 600 字的描写会把三条选项顶出屏幕。
BODY_MAX_CHARS = 260

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
                    "nodes": len(sc["nodes"])})
    out.sort(key=lambda s: s["title"])
    return out


def load_script(sid):
    """读一个剧本。不存在、不是 JSON、缺 nodes 一律返回 None。

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
    nodes = d.get("nodes")
    if not isinstance(nodes, dict) or not nodes:
        return None
    start = str(d.get("start") or "")
    if start not in nodes:
        start = next(iter(nodes))
    d["id"] = str(d.get("id") or sid)
    d["start"] = start
    d["nodes"] = nodes
    return d


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
    """读档 → 返回一句回执。**不留历史**，读回来直接停在那个节点。"""
    name = re.sub(r"[^A-Za-z0-9_-]", "", str(name or ""))[:24] or "slot"
    path = os.path.join(_save_dir(session_key), name + ".json")
    try:
        with open(path, encoding="utf-8") as f:
            ent = json.load(f)
    except (OSError, ValueError):
        return "没有第 %s 档。" % name
    if not isinstance(ent, dict) or not ent.get("script"):
        return "第 %s 档坏了。" % name
    sc = load_script(ent["script"])
    if sc is None or ent.get("node") not in sc["nodes"]:
        return "这档对应的剧本没了。"
    with _LOCK:
        st = _load_state()
        st[session_key] = ent
        _save_state(st)
    node = sc["nodes"][ent["node"]]
    return "%s\n\n%s" % (_node_text(sc, node, ent), _opt_text(node))


def _node_text(sc, node, ent):
    """节点骨架原文。读档回执和 LLM 失败兜底都用它——不能空着不说话。"""
    say = str(node.get("say") or "").strip()
    return say or "（这里该有一段剧情，但剧本里没写。）"


def _opt_text(node):
    """选项文本。**不走模型**，直接从剧本取。"""
    opts = node.get("opts") or []
    if not opts:
        return ""
    lines = []
    for o in opts:
        k = str(o.get("k") or "").strip()
        t = str(o.get("t") or "").strip()
        if k and t:
            lines.append("%s. %s" % (k, t))
    return "\n".join(lines)


# ─── 命令 ────────────────────────────────────────────

_HELP = """galgame 模式

  gal 开局 [剧本]   开始（不带名字就用默认剧本）
  gal 状态          看当前进度和好感度
  gal 存档 [名字]   存一个档
  gal 读档 [名字]   回到那个档
  gal 剧本          看有哪些剧本
  gal 结束          退出

玩的时候直接回选项字母（A / B / C）就行。"""


def handle_command(session_key, text):
    """认 `gal xxx` 命令。返回 `Turn`；**不是命令返回 None**（照常往下走）。

    只认「整条消息以 gal 开头」这一种形态，且必须有空格或到头——否则
    `galaxy`、`gallery` 会被当成命令吃掉。
    """
    t = (text or "").strip()
    if not re.match(r"^gal(?:\s|$)", t, re.I):
        return None
    body = t[3:].strip()
    if not body:
        return Turn(_HELP, "", False)

    parts = body.split(None, 1)
    sub = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""

    if sub in ("开局", "开始", "start", "新游戏"):
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
        lines.append("  %s —— %s（%d 个节点）" % (s["id"], s["title"], s["nodes"]))
    lines.append("\n发 gal 开局 剧本id 开始。")
    return "\n".join(lines)


def _status_text(session_key):
    ent = _entry(session_key)
    if not ent:
        return "现在没在玩。发 gal 开局 开始。"
    sc = load_script(ent["script"])
    if sc is None:
        return "进度还在，但剧本「%s」找不到了。" % ent["script"]
    node = sc["nodes"].get(ent["node"]) or {}
    aff = int((ent.get("vars") or {}).get("aff") or 0)
    lines = [
        "《%s》" % (sc.get("title") or sc["id"]),
        "当前：%s" % (node.get("title") or ent["node"]),
        "好感度：%d" % aff,
    ]
    n_seen = len(ent.get("seen") or [])
    if n_seen:
        lines.append("已过节点：%d 个" % n_seen)
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

    node_id = sc["start"]
    ent = {"script": sc["id"], "node": node_id, "vars": {},
           "seen": [], "started": int(time.time())}
    with _LOCK:
        st = _load_state()
        st[session_key] = ent
        _save_state(st)

    return _play(session_key, sc, ent, node_id, None, "",
                 _target_of(session_key), "", opening=True)


# ─── 一轮 ────────────────────────────────────────────

def _match_option(node, text):
    """玩家这句话是不是在选选项。是就返回那个选项 dict，不是返回 None。

    **只认纯选项**：`A` / `a` / `选A` / `A.` / `1`。刻意不做模糊匹配——
    「我想跟她打招呼」这种交给模型当闲聊演，不猜他想选哪条。猜错一次就把人
    带到别的分支上，比不推进更糟。
    """
    opts = node.get("opts") or []
    if not opts:
        return None
    t = str(text or "").strip().upper()
    t = re.sub(r"^(?:选择|选|OPTION)\s*", "", t)
    m = re.match(r"^([A-Z]|[1-9])\s*[.、。:：)]?\s*$", t)
    if not m:
        return None
    key = m.group(1)
    for o in opts:
        if str(o.get("k") or "").strip().upper() == key:
            return o
    # 没写 abc、改用序号的情况：A 位就是 1
    if key.isdigit():
        idx = int(key) - 1
        if 0 <= idx < len(opts):
            return opts[idx]
    return None


def _first_option(node, text):
    """从（可能合并了多条的）文本里取**第一个**对得上选项的。

    群聊里两个人前后脚各发一个 `A`，`_dispatch` 会把它们合并成一条 `A\\nA`，
    整体拿去匹配会失败——于是这一轮变成闲聊，两人都以为没说清。逐行试一遍
    就对了，取到第一个就停：先到先得，跟多数投票相比少一次来回，也少一次
    因为「两人选得不一样」把剧情卡住。
    """
    for line in str(text or "").splitlines():
        p = _match_option(node, line)
        if p is not None:
            return p
    return None


def run_turn(session_key, text, target="private", sender=""):
    """galgame 态的一轮：**1 次** LLM 调用，返回 `Turn(正文+选项, CG描述, 是否结束)`。

    `reply` 空 = 这轮什么都不发（调用方别开口）。正常不会空——模型失败会回退
    到剧本骨架原文，宁可干一点也不能让人对着空气等。
    """
    ent = _entry(session_key)
    if not ent:
        return Turn("", "", False)
    sc = load_script(ent["script"])
    if sc is None:
        leave(session_key)
        return Turn("剧本「%s」不见了，游戏到这里结束。" % ent["script"], "", True)
    node = sc["nodes"].get(ent["node"])
    if node is None:
        leave(session_key)
        return Turn("剧情节点丢了，游戏到这里结束。", "", True)

    pick = _first_option(node, text)

    # ── 推进：变量加减 + 换节点，全在这一个锁里做完 ──
    if pick is not None:
        nxt = str(pick.get("to") or "").strip()
        aff = int(pick.get("aff") or 0)
        with _LOCK:
            st = _load_state()
            e = st.get(session_key)
            if e:
                if aff:
                    v = e.setdefault("vars", {})
                    v["aff"] = int(v.get("aff") or 0) + aff
                if nxt and nxt in sc["nodes"]:
                    e["node"] = nxt
                _save_state(st)
                ent = e
        if nxt and nxt not in sc["nodes"]:
            # 剧本写错了：指向一个不存在的节点。停在原地，把话说清楚，
            # 别把人卡在一个点不动的循环里。
            log.warning("剧本 %s 从 %s 指向了不存在的节点 %s",
                        sc["id"], ent.get("node"), nxt)
            return Turn("（剧本这里断掉了：%s 指向的 %s 不存在）\n\n%s"
                        % (ent.get("node"), nxt, _opt_text(node)), "", False)
        node = sc["nodes"].get(ent["node"]) or node

    # ── 结局 ──
    ended = not (node.get("opts") or [])
    reply = _play(session_key, sc, ent, ent["node"], pick, text, target, sender)
    if ended:
        leave(session_key)
        reply = Turn(reply.reply + "\n\n【%s】" % (node.get("end") or "结局"),
                     reply.scene, True)
    return reply


def _play(session_key, sc, ent, node_id, pick, said, target, sender,
          opening=False):
    """把这一幕演出来：调一次模型 + 拼选项 + 判断要不要出 CG。"""
    node = sc["nodes"].get(node_id) or {}
    body = _compose(sc, node, ent, pick, said, target, sender, opening)

    opts = _opt_text(node)
    reply = body
    if opts:
        reply = (body + "\n\n" + opts) if body else opts

    # CG：节点写了才出，且同一个节点只出一次——读档回到走过的节点不该重画，
    # 一张几十秒，重画一次人就走了。
    scene = ""
    cg = str(node.get("cg") or "").strip()
    if cg and node_id not in set(ent.get("seen") or []):
        scene = cg
        _mark_seen(session_key, node_id)
    return Turn(reply, scene, False)


def _mark_seen(session_key, node_id):
    """记下这个节点的 CG 已经出过，防止读档回来重复画。"""
    with _LOCK:
        st = _load_state()
        e = st.get(session_key)
        if not e:
            return
        seen = e.setdefault("seen", [])
        if node_id not in seen:
            seen.append(node_id)
        _save_state(st)


def _target_of(session_key):
    return "group" if str(session_key).startswith("group_") else "private"


# ─── 生成 ────────────────────────────────────────────

_CONTRACT = """【输出要求】
- 先 1~2 句环境或动作，直接叙述，**不要用括号包起来**，再来角色的台词
- 台词用「」包住，不要写成剧本格式（不写「角色名：」）
- 总共不超过 {n} 字
- 不要输出选项、不要问「你要怎么做」、不要总结刚才发生了什么
- 不要复述骨架原文，要把它演出来"""


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


def _compose(sc, node, ent, pick, said, target, sender, opening=False):
    """一次 LLM 调用。失败回退骨架原文——**永远不返回空**。

    回退用 `say` 而不是「我卡住了」：骨架是手写的，本来就通顺，只是干一点。
    空回或道歉话术会把「游戏」这件事当场戳破。
    """
    card = _card_for(sc)
    name = (card or {}).get("name") or "旁白"

    lines = ["你在文字游戏《%s》里扮演%s。" % (sc.get("title") or sc["id"], name)]
    if card and card.get("persona"):
        lines.append("\n【角色】\n" + str(card["persona"])[:600])

    # 闲聊轮（没选选项、只是插话）不能整段喂骨架：那一幕上一轮刚演完，再喂
    # 一遍模型就会照着复述——实测「她慌忙蹲下去捡本子」连着出现两轮。这里只
    # 留一句锚点，并明确要求别复述。
    idle = (not opening) and pick is None and bool(said)
    if idle:
        anchor = _node_text(sc, node, ent)[:70]
        lines.append("\n【当前处境】这一幕上一轮已经演过了，大致是：「%s…」" % anchor)
        lines.append("不要重复上一轮的环境和动作描写，只回应玩家这一句话。")
    else:
        lines.append("\n【这一幕的骨架】\n" + _node_text(sc, node, ent))

    # 好感度给模型看，它才知道此刻该亲还是该冷——但只影响语气，不影响剧情。
    aff = int((ent.get("vars") or {}).get("aff") or 0)
    if aff:
        lines.append("\n【她对你的好感】%d（只影响语气，不改变剧情走向）" % aff)

    if opening:
        lines.append("\n【这是开场】玩家刚进入游戏，还没做任何选择。")
    elif pick is not None:
        lines.append("\n【玩家选择了】%s" % (pick.get("t") or pick.get("k")))
    elif idle:
        who = sender if (target == "group" and sender) else "玩家"
        lines.append("\n【玩家穿插说了一句】%s：%s" % (who, str(said)[:200]))

    lines.append("\n" + _CONTRACT.format(n=BODY_MAX_CHARS))

    try:
        raw = llm.call_llm([{"role": "user", "content": "\n".join(lines)}])
    except Exception:
        log.exception("galgame 调用失败 %s", ent.get("script"))
        return _node_text(sc, node, ent)

    body = (raw or "").strip()
    if not body:
        log.warning("galgame 空回复 %s", ent.get("script"))
        return _node_text(sc, node, ent)
    if len(body) > BODY_MAX_CHARS * 2:
        body = body[:BODY_MAX_CHARS * 2].rstrip() + "…"
    return body


# ─── CG ──────────────────────────────────────────────

def draw_cg(cg, session_key, script_id=""):
    """出一张 CG，返回文件名（没画出来返回 ""）。**阻塞**——调用方放后台线程。

    跟 `persona.draw` 同一套：入队带 `quiet=True`（图直接回会话、失败不吭声），
    不扣每日额度。区别只有一个——这里用 `GAL_SKILL`（anima 渠道，出图慢但好看），
    所以只挂在关键节点上。
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
