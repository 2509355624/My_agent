"""小说模式（2026-11-11）：以玩家为唯一主体的生存模拟小说。

## 它跟角色扮演是两套东西

`app/persona.py` 是「你 + 她」的一对一，写好感数值、跨天、服饰、四层记忆。
这里是第三套：一个世界一条线，多 NPC 只是推进剧情，**没有任何数值条**。
状态、key、prompt、前端页面全部独立，只共用 `app/llm.py` 的调用封装。

## 三个 AI 各管一段

- 开场 AI：只跑一次，把玩法预设扩成一本世界书草稿并封样。
- 编剧 AI：每轮一次，把玩家的「意图」裁定成这一拍真实发生的事，交一个 JSON。
- 写作 AI：每轮一次，只把已裁定的事实演成第三人称正文。

## 代码只登记，判断全归模型

`_apply()` 只做类型规整、按名字合并、落盘。**不审核玩家意图合不合理**
（那正是编剧 AI 的活），也不拿词表卡模型交回来的内容。只有「没交作业」
（空返回 / JSON 解析失败）才由接口提示重来。

裁定不是拒绝：玩家说掏出手枪，编剧会写「宿舍里没有枪，你摸到的是晾衣杆」，
把意图落成能往下演的事实，而不是退回重填。

## 死亡与重开

人物进度归零，世界知识留下：`discovered` 里 `kind == "static"` 的（地图、
固定物资）跨周目继承，动态事件（尸潮、遭遇、NPC 生死）不继承、重开重掷。
所以玩家能少走弯路，不能预知危险。封样草稿 `seed` 复用，重开不再调 LLM。
"""
import json
import logging
import os
import re
import threading
import time
import uuid

from app import llm
from app.config import (BASE_DIR, PERSONA_MODEL, PERSONA_PROVIDER,
                        state_path)

log = logging.getLogger("novel")

NOVELS_DIR = os.path.join(BASE_DIR, "novels")
_FILE = state_path("novel_worlds.json")
_LOCK = threading.RLock()
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")


class NoHomework(RuntimeError):
    """模型没交作业（空返回 / JSON 解析失败 / 正文为空）。

    只有这一类才提示玩家重来；参数不合法、链路不通是另外两种错误，
    不能混成「玩家再写一遍」。
    """


_HISTORY_MAX = 60
_LOG_MAX = 80
_SLICE_LOG = 6
_SLICE_DISCOVERED = 12

# 输出上限（2026-11-11 真机修）：开场封样要一次交出整本世界书草稿（7 个
# 字段、每个地点/角色还有好几项），2400 token 会被 finish_reason=length 砍断
# （实测正文 4359 字），残缺 JSON 解析失败后前端只说「没交作业，再点一次」，
# 再点还是截断。跟 1v1 的 REPLY_MAX_TOKENS 对齐，给足但不限定它写多少。
OPEN_MAX_TOKENS = 8192
DIRECTOR_MAX_TOKENS = 4096
WRITER_MAX_TOKENS = 4096


# ─── 玩法预设 ─────────────────────────────────────────

def list_plays():
    """扫 novels/*/preset.json。第一版只有「丧尸末日」一个玩法。"""
    out = []
    try:
        names = sorted(os.listdir(NOVELS_DIR))
    except OSError:
        return out
    for name in names:
        p = load_preset(name)
        if p:
            out.append({
                "id": p.get("id") or name,
                "name": p.get("name") or name,
                "tagline": p.get("tagline") or "",
                "brief": p.get("brief") or "",
                "identity_choices": p.get("identity_choices") or [],
                "opening_note": p.get("opening_note") or "",
            })
    return out


def load_preset(play):
    """读一个玩法的预设。非法 id / 缺文件都返回 None，调用方自己判。"""
    pid = str(play or "").strip()
    if not _SAFE_ID.match(pid):
        return None
    path = os.path.join(NOVELS_DIR, pid, "preset.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _opening_doc(play):
    pid = str(play or "").strip()
    if not _SAFE_ID.match(pid):
        return ""
    path = os.path.join(NOVELS_DIR, pid, "opening.md")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


# ─── 落盘 ────────────────────────────────────────────

def _read_db():
    try:
        with open(_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {"worlds": {}}
    if not isinstance(data, dict) or not isinstance(data.get("worlds"), dict):
        return {"worlds": {}}
    return data


def _write_db(db):
    tmp = _FILE + ".tmp"
    os.makedirs(os.path.dirname(_FILE) or ".", exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)
    os.replace(tmp, _FILE)


def _save_world(world):
    with _LOCK:
        db = _read_db()
        db["worlds"][world["id"]] = world
        _write_db(db)


def get_world(world_id):
    wid = str(world_id or "").strip()
    if not _SAFE_ID.match(wid):
        return None
    return _read_db().get("worlds", {}).get(wid)


def list_worlds():
    out = []
    for wid, w in _read_db().get("worlds", {}).items():
        if not isinstance(w, dict):
            continue
        out.append({
            "id": wid,
            "play": w.get("play"),
            "name": (w.get("player") or {}).get("name") or "",
            "chapter": w.get("chapter", 1),
            "place": (w.get("player") or {}).get("place") or "",
            "ending": w.get("ending"),
            "turns": w.get("turns", 0),
        })
    return sorted(out, key=lambda x: x["id"])


# ─── 提示词 ──────────────────────────────────────────

_OPEN_PROMPT = """你是这部长篇生存小说的开场编剧，现在要把一句题材扩成一本能往后写很多轮的世界书。

【玩法预设】
{preset}

【开场说明】
{doc}

【主角】
名字：{name}
身份：{identity}（{identity_desc}）

只输出一个 JSON 对象，字段：
{{"places":[{{"name":"","detail":"","search":"","kind":"static"}}],
 "item_pool":[""],
 "cast_seed":[{{"name":"","identity":"","relation":"","willing":"","tier":"主体","present":false}}],
 "chapter_goal":"",
 "opening_facts":[""],
 "actions":["","",""],
 "discovered":[{{"fact":"","kind":"static"}}]}}

每个地点写清它是什么样、能翻到什么；`kind` 由你标：地图结构和固定物资是
static，会随每次重开变化的是 dynamic。只输出 JSON。"""


_DIRECTOR_PROMPT = """你是这部长篇生存小说的编剧，负责把玩家这一轮的意图落成这一拍真实发生的事。

【裁定的意思】
玩家写的是意图，不是既成事实。手边没有的东西，就写他伸手摸到了什么；做不到的事，
就写他试过之后的结果。主角的身份是真实条件：文弱书生翻不了窗，体育生能撞门，
医学生看得懂伤口。你交回去的事实会写进世界书，下一轮所有人都照它演。

【道具】
只有你发放的东西才会进主角的口袋。玩家写了某样东西，就写他是怎么拿到、或是
在哪里摸了个空；使用过的东西从口袋里划掉。写作的人会照你登记的持有物来写动作。

【路线】
玩家提出一个远处的目标，就把它拆成两三段能走的路，每段是一个具体地点。
玩家中途改了主意，就重新给一条路。已经走到的位置记在 `at` 上。

【角色】
新遇到的人由你编：名字、一句话身份、跟主角的关系、愿不愿意跟着走。
`tier` 写「主体」或「路人甲」。世界以主角为中心，路人甲只负责把剧情推一下。

【世界书 · 当前场景切片】
{slice}

【最近几轮】
{recent}

【玩家这一轮】
{said}

只输出一个 JSON 对象，字段：
{{"ruling":"这一轮裁定成的事实，一句话",
 "facts":["这一拍确实发生了什么，一样一条"],
 "items_gain":[],"items_use":[],"items_lose":[],
 "place":"","condition":"",
 "cast_new":[{{"name":"","identity":"","relation":"","willing":"","tier":"主体","present":true}}],
 "cast_update":[{{"name":"","relation":"","willing":"","present":true}}],
 "events_add":[{{"trigger":"","result":"","done":true,"kind":"dynamic"}}],
 "events_done":[],
 "discovered":[{{"fact":"","kind":"static"}}],
 "route":{{"goal":"","nodes":[],"at":0}},
 "chapter_goal":"",
 "chapter_advance":false,
 "ending":{{"cause":"","summary":""}},
 "actions":["","",""]}}

`route` / `chapter_goal` / `ending` 这一轮没变化就给 null，`chapter_advance` 给 false。
`ending` 只在主角这一拍死了才给，写清死因和当时发生了什么。只输出 JSON。"""


_WRITER_CONTRACT = """你是这部长篇生存小说的叙述者，用第三人称写主角「{name}」。

给玩家看的引导语、动作选项和状态卡另有其文，用「你」；这里的正文一律写主角的名字。

你写的是已经发生过的事：下面的事实清单就是这一拍的全部内容，把它写成有过程、
有阻力的一段戏。主角手上拿着什么，动作里就带着什么；他身上的状态、所在的地方、
在场的人，都照切片写。写 400 到 700 字，结尾留一点还没解决的东西。

【世界书 · 当前场景切片】
{slice}

【这一拍的事实】
{facts}

【玩家这一轮】
{said}

输出从第一句开始就是小说正文。"""


def _call(messages, max_tokens, temperature):
    """跟扮演共用链头（公用中转站），失败时顺链降级，跟 1v1 一致。

    2026-11-11：原来这里用严格模式单目标直调，中转站余额归零时
    开场直接 502，前端还写「等一下再试」，充值前完全玩不了。站主拍板放开：
    链头仍是 PERSONA_PROVIDER/MODEL，402 之后交给 llm.candidates() 顺链
    试 deepseek-flash / mimo / volc；代价是可能落到付费 provider。
    """
    return llm.call_llm(
        messages,
        provider=PERSONA_PROVIDER or None,
        model=PERSONA_MODEL or None,
        strict=False,
        max_tokens=max_tokens,
        temperature=temperature,
    )


def _parse_json(text):
    """模型没交作业（空返回 / 不是 JSON）就抛，交给接口提示重来。"""
    raw = str(text or "").strip()
    if not raw:
        raise NoHomework("模型没有返回内容")
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    i, j = raw.find("{"), raw.rfind("}")
    if i < 0 or j <= i:
        raise NoHomework("没有返回 JSON 对象")
    try:
        data = json.loads(raw[i:j + 1])
    except ValueError as e:
        # 真机 2026-11-11：输出被 max_tokens 砍断时，rfind("}") 会抓到中间
        # 某个对象的右括号，报出来的「line 124 column 6」完全看不出是截断。
        # 这里只登记事实（多长、结尾长什么样），判断交给人，不做自动补 JSON。
        tail = raw[-24:].replace("\n", " ").replace("\r", " ")
        raise NoHomework("返回的 JSON 解析不了：%s（原文 %d 字，结尾「%s」）"
                         % (e, len(raw), tail)) from e
    if not isinstance(data, dict):
        raise NoHomework("返回的不是 JSON 对象")
    return data


def _as_list(v):
    if isinstance(v, list):
        return v
    if isinstance(v, str) and v.strip():
        return [v]
    return []


def _as_text(v, default=""):
    if isinstance(v, str):
        return v.strip()
    return default


# ─── 世界书 ──────────────────────────────────────────

def _blank_world(play, name, identity, identity_desc, preset):
    start = dict(preset.get("start") or {})
    return {
        "id": "nv_" + time.strftime("%m%d%H%M%S") + "_" + uuid.uuid4().hex[:4],
        "play": play,
        "play_name": preset.get("name") or play,
        "created": time.time(),
        "chapter": int(start.get("chapter") or 1),
        "chapter_goal": start.get("chapter_goal") or "",
        "ending": None,
        "player": {
            "name": name,
            "identity": identity,
            "identity_desc": identity_desc,
            "place": start.get("place") or "",
            "condition": start.get("condition") or "",
            "items": list(start.get("items") or []),
        },
        "rules": list(preset.get("rules") or []),
        "places": [],
        "item_pool": [],
        "cast": [],
        "cast_seed": [],
        "route": {"goal": "", "nodes": [], "at": 0},
        "events": [],
        "log": [],
        "discovered": [],
        "death_log": [],
        "history": [],
        "actions": list(preset.get("opening_actions") or []),
        "turns": 0,
        "seed": {},
        "opening_reply": "",
    }


def _identity_desc(preset, identity):
    for row in preset.get("identity_choices") or []:
        if isinstance(row, dict) and row.get("name") == identity:
            return row.get("desc") or ""
    return ""


def _slice(world):
    """每轮只注入当前场景切片；世界书全量留在盘上，不进 prompt。"""
    p = world.get("player") or {}
    present = [c for c in world.get("cast", []) if c.get("present")]
    lines = [
        "主角：%s，身份：%s（%s），%s" % (
            p.get("name") or "", p.get("identity") or "",
            p.get("identity_desc") or "", p.get("condition") or ""),
        "现在在：%s，第 %s 章，本章目标：%s" % (
            p.get("place") or "", world.get("chapter", 1),
            world.get("chapter_goal") or ""),
        "手上拿着：%s" % ("、".join(p.get("items") or []) or "空手"),
        "在场的人：%s" % (
            "；".join("%s（%s，%s，%s）" % (
                c.get("name") or "", c.get("identity") or "",
                c.get("relation") or "", c.get("willing") or "")
                for c in present) or "只有主角一个"),
        "主角团：%s" % (
            "、".join(c.get("name") or "" for c in world.get("cast", [])) or "还没有别人"),
    ]
    static = [d.get("fact") for d in world.get("discovered", [])
              if d.get("kind") == "static" and d.get("fact")]
    if static:
        lines.append("已经探明的固定事实：%s" % "；".join(static[-_SLICE_DISCOVERED:]))
    route = world.get("route") or {}
    if route.get("goal"):
        nodes = route.get("nodes") or []
        lines.append("当前路线：去%s → %s，已经走到第 %s 段" % (
            route.get("goal"), " → ".join(str(x) for x in nodes),
            route.get("at", 0)))
    open_events = [e for e in world.get("events", []) if not e.get("done")]
    if open_events:
        lines.append("还没了结的事：%s" % "；".join(
            str(e.get("trigger") or "") for e in open_events[-5:]))
    rules = world.get("rules") or []
    if rules:
        lines.append("世界规则：%s" % "；".join(str(x) for x in rules))
    recent_log = (world.get("log") or [])[-_SLICE_LOG:]
    if recent_log:
        lines.append("最近做过的事：%s" % "；".join(str(x) for x in recent_log))
    deaths = world.get("death_log") or []
    if deaths:
        last = deaths[-1]
        lines.append("上一次是怎么死的：%s（在%s）" % (
            last.get("cause") or "", last.get("at") or ""))
    return "\n".join(lines)


def _recent(world, n=4):
    rows = (world.get("history") or [])[-n * 2:]
    return "\n".join("%s：%s" % (
        "玩家" if r.get("role") == "user" else "正文",
        str(r.get("content") or "")[:400]) for r in rows) or "（这是开头）"


def _facts_text(facts):
    rows = [str(x).strip() for x in _as_list(facts) if str(x).strip()]
    return "\n".join("- " + x for x in rows) or "（这一拍没有新事实）"


# ─── 登记（只做数据合并，不判断内容）────────────────────

def _merge_cast(world, item):
    if not isinstance(item, dict) or not _as_text(item.get("name")):
        return
    name = _as_text(item.get("name"))
    row = next((c for c in world["cast"] if c.get("name") == name), None)
    if row is None:
        row = {"name": name, "identity": "", "relation": "",
               "willing": "", "tier": "路人甲", "present": True}
        world["cast"].append(row)
    for key in ("identity", "relation", "willing", "tier", "present", "status"):
        if key in item:
            row[key] = item[key]


def _merge_discovered(world, items):
    seen = {d.get("fact") for d in world["discovered"]}
    for item in _as_list(items):
        if isinstance(item, str):
            item = {"fact": item, "kind": "dynamic"}
        if not isinstance(item, dict):
            continue
        fact = _as_text(item.get("fact"))
        if not fact or fact in seen:
            continue
        # 没标 kind 的一律按动态算：重开时不继承，避免把剧情预知带进下一周目。
        world["discovered"].append({
            "fact": fact,
            "kind": _as_text(item.get("kind"), "dynamic"),
            "chapter": world.get("chapter", 1),
        })
        seen.add(fact)


def _apply(world, d, said):
    """把编剧交回来的 JSON 落成世界书。这里没有一处判断「合不合理」。"""
    p = world["player"]
    gains = [str(x).strip() for x in _as_list(d.get("items_gain")) if str(x).strip()]
    used = [str(x).strip() for x in _as_list(d.get("items_use")) if str(x).strip()]
    lost = [str(x).strip() for x in _as_list(d.get("items_lose")) if str(x).strip()]
    items = list(p.get("items") or []) + gains
    drop = set(used) | set(lost)
    p["items"] = [x for x in dict.fromkeys(items) if x not in drop]

    place = _as_text(d.get("place"), p.get("place") or "")
    if place:
        p["place"] = place
    condition = _as_text(d.get("condition"), p.get("condition") or "")
    if condition:
        p["condition"] = condition

    for item in _as_list(d.get("cast_new")) + _as_list(d.get("cast_update")):
        _merge_cast(world, item)

    for item in _as_list(d.get("events_add")):
        if isinstance(item, dict) and _as_text(item.get("trigger")):
            world["events"].append({
                "trigger": _as_text(item.get("trigger")),
                "result": _as_text(item.get("result")),
                "done": bool(item.get("done")),
                "kind": _as_text(item.get("kind"), "dynamic"),
                "chapter": world.get("chapter", 1),
            })
    done = {str(x) for x in _as_list(d.get("events_done"))}
    if done:
        for e in world["events"]:
            if e.get("trigger") in done:
                e["done"] = True
    world["events"] = world["events"][-_LOG_MAX:]

    _merge_discovered(world, d.get("discovered"))

    route = d.get("route")
    if isinstance(route, dict) and _as_text(route.get("goal")):
        nodes = [str(x) for x in _as_list(route.get("nodes"))]
        try:
            at = max(0, int(route.get("at") or 0))
        except (TypeError, ValueError):
            at = 0
        world["route"] = {"goal": _as_text(route.get("goal")),
                          "nodes": nodes, "at": at}
    goal = _as_text(d.get("chapter_goal"))
    if goal:
        world["chapter_goal"] = goal
    if d.get("chapter_advance") is True:
        world["chapter"] = int(world.get("chapter", 1)) + 1

    ruling = _as_text(d.get("ruling"))
    facts = [str(x).strip() for x in _as_list(d.get("facts")) if str(x).strip()]
    for line in [ruling] + facts:
        if line:
            world["log"].append(line[:200])
    world["log"] = world["log"][-_LOG_MAX:]

    actions = [str(x).strip() for x in _as_list(d.get("actions")) if str(x).strip()]
    if actions:
        world["actions"] = actions[:6]

    ending = d.get("ending")
    if isinstance(ending, dict) and _as_text(ending.get("cause")):
        world["ending"] = {
            "cause": _as_text(ending.get("cause")),
            "summary": _as_text(ending.get("summary")),
            "at": p.get("place") or "",
            "chapter": world.get("chapter", 1),
        }
        world["death_log"].append({
            "cause": world["ending"]["cause"],
            "at": world["ending"]["at"],
            "identity": p.get("identity") or "",
            "items": list(p.get("items") or []),
            "chapter": world.get("chapter", 1),
            "turns": world.get("turns", 0),
        })
        world["death_log"] = world["death_log"][-20:]

    world["history"].append({"role": "user", "content": said})
    world["turns"] = int(world.get("turns", 0)) + 1
    return ruling, facts


# ─── 三个 AI ─────────────────────────────────────────

def _open_ai(world, preset):
    p = world["player"]
    msg = _OPEN_PROMPT.format(
        preset=json.dumps(preset, ensure_ascii=False),
        doc=_opening_doc(world["play"]) or "（这个玩法没有额外的开场说明）",
        name=p.get("name") or "",
        identity=p.get("identity") or "",
        identity_desc=p.get("identity_desc") or "",
    )
    raw = _call([{"role": "user", "content": msg}], OPEN_MAX_TOKENS, 0.8)
    return _parse_json(raw)


def _director(world, said):
    msg = _DIRECTOR_PROMPT.format(slice=_slice(world),
                                  recent=_recent(world), said=said)
    raw = _call([{"role": "user", "content": msg}], DIRECTOR_MAX_TOKENS, 0.7)
    return _parse_json(raw)


def _writer(world, facts, said):
    p = world["player"]
    msg = _WRITER_CONTRACT.format(
        name=p.get("name") or "主角",
        slice=_slice(world),
        facts=_facts_text(facts),
        said=said or "（开场）",
    )
    out = _call([{"role": "user", "content": msg}], WRITER_MAX_TOKENS, 1.0)
    text = str(out or "").strip()
    if not text:
        raise NoHomework("写作 AI 没有返回正文")
    return text


def _merge_seed(world, seed):
    """开场 AI 的草稿并进世界书。重开时复用同一份 seed。"""
    world["seed"] = seed
    world["places"] = _as_list(seed.get("places"))
    world["item_pool"] = [str(x) for x in _as_list(seed.get("item_pool"))]
    world["cast_seed"] = [x for x in _as_list(seed.get("cast_seed"))
                          if isinstance(x, dict)]
    goal = _as_text(seed.get("chapter_goal"), world["chapter_goal"])
    if goal:
        world["chapter_goal"] = goal
    actions = [str(x).strip() for x in _as_list(seed.get("actions")) if str(x).strip()]
    if actions:
        world["actions"] = actions[:6]
    _merge_discovered(world, seed.get("discovered"))


# ─── 对外接口 ────────────────────────────────────────

def open_world(play, name, identity):
    """开场封样：一次开场 AI + 一次写作 AI。返回给前端的完整一屏。"""
    preset = load_preset(play)
    if not preset:
        raise ValueError("没有这个玩法")
    name = _as_text(name) or "无名的人"
    identity = _as_text(identity)
    if not identity:
        choices = preset.get("identity_choices") or []
        identity = (choices[0].get("name") if choices and isinstance(choices[0], dict)
                    else "普通人")
    world = _blank_world(play, name, identity,
                         _identity_desc(preset, identity), preset)
    seed = _open_ai(world, preset)
    _merge_seed(world, seed)
    facts = _as_list(seed.get("opening_facts"))
    reply = _writer(world, facts, "")
    world["opening_reply"] = reply
    world["history"].append({"role": "assistant", "content": reply})
    _save_world(world)
    return {"ok": True, "world_id": world["id"], "reply": reply,
            "note": preset.get("opening_note") or "",
            "state": state_card(world)}


def run_turn(world_id, said):
    """一轮：编剧裁定 → 登记 → 写作演出。"""
    said = _as_text(said)
    if not said:
        raise ValueError("这一轮没有写意图")
    world = get_world(world_id)
    if not world:
        raise ValueError("没有这个世界")
    if world.get("ending"):
        return {"ok": False, "error": "这一本已经结束了，重开一本吧",
                "state": state_card(world)}

    d = _director(world, said)
    ruling, facts = _apply(world, d, said)
    reply = _writer(world, [ruling] + facts if ruling else facts, said)
    world["history"].append({"role": "assistant", "content": reply})
    world["history"] = world["history"][-_HISTORY_MAX:]
    _save_world(world)
    return {"ok": True, "reply": reply, "ruling": ruling,
            "ending": world.get("ending"), "state": state_card(world)}


def restart(world_id, name=None, identity=None):
    """死亡重开：人物归零，静态世界事实与死因留下，封样复用，不重跑 LLM。"""
    old = get_world(world_id)
    if not old:
        raise ValueError("没有这个世界")
    preset = load_preset(old.get("play"))
    if not preset:
        raise ValueError("玩法预设不见了")
    p = old.get("player") or {}
    name = _as_text(name) or p.get("name") or "无名的人"
    identity = _as_text(identity) or p.get("identity") or ""
    fresh = _blank_world(old["play"], name, identity,
                         _identity_desc(preset, identity), preset)
    fresh["id"] = old["id"]                 # 同一个世界，同一条线
    fresh["created"] = old.get("created", fresh["created"])
    fresh["seed"] = old.get("seed") or {}
    _merge_seed(fresh, fresh["seed"])
    fresh["discovered"] = [d for d in old.get("discovered", [])
                           if d.get("kind") == "static"]
    fresh["death_log"] = list(old.get("death_log") or [])[-20:]
    fresh["opening_reply"] = old.get("opening_reply") or ""
    if fresh["opening_reply"]:
        fresh["history"].append({"role": "assistant",
                                 "content": fresh["opening_reply"]})
    _save_world(fresh)
    return {"ok": True, "reply": fresh["opening_reply"],
            "note": preset.get("opening_note") or "",
            "state": state_card(fresh)}


def state_card(world):
    """右栏用的纯事实状态卡。没有任何数值条、好感、跨天、服饰。"""
    p = world.get("player") or {}
    deaths = world.get("death_log") or []
    return {
        "id": world.get("id"),
        "play": world.get("play"),
        "play_name": world.get("play_name") or world.get("play"),
        "chapter": world.get("chapter", 1),
        "chapter_goal": world.get("chapter_goal") or "",
        "ending": world.get("ending"),
        "player": {
            "name": p.get("name") or "",
            "identity": p.get("identity") or "",
            "identity_desc": p.get("identity_desc") or "",
            "place": p.get("place") or "",
            "condition": p.get("condition") or "",
            "items": list(p.get("items") or []),
        },
        "present": [c for c in world.get("cast", []) if c.get("present")],
        "cast": list(world.get("cast") or []),
        "route": dict(world.get("route") or {}),
        "events": [e for e in world.get("events", []) if not e.get("done")][-6:],
        "log": list(world.get("log") or [])[-8:],
        "discovered": [d for d in world.get("discovered", [])
                       if d.get("kind") == "static"],
        "last_death": deaths[-1] if deaths else None,
        "actions": list(world.get("actions") or []),
        "history": list(world.get("history") or [])[-_HISTORY_MAX:],
    }
