"""小说模式（2026-11-11 建，2026-11-12 改回 1v1 内核）：玩家当主角的生存模拟小说。

## 它跟角色扮演是同一套内核，不是两套东西

2026-11-12 站主纠正：主角从「她」换成玩家自己，去掉数值状态条、正文改
第三人称，**表现形式变了，内核没变**。第一版错误地从 0 重写了 `_slice` /
`_recent` / 一本独立世界书，把 1v1 已经解决过的坑又踩了一遍——最严重的是
「把最重要的东西交给编剧 AI 压缩成 facts，再把这份二次加工品当写作 AI 的
全部输入」。

现在照搬 `app/persona.py::run_turn` 的装配结构（不修改 persona.py 本身）：

- `[system 头] + 完整历史 + [user：玩家原话 → RAG → 发展线 → 状态块 → facts → 尾注]`
- 状态块由**代码从世界书直接读出来**，不经过任何模型压缩；编剧只负责改状态。
- 稳定内容并进 system 头吃前缀缓存，逐轮变化的内容全放最后一条 user 消息。
- 长期记忆直接复用 `app/persona_rag.py`（检索/入库/回填/collection 零改动）。

直接调用的现成零件：`persona.cache_rows`（滞回历史窗口）、`persona_rag`
全套、`writing_style.LENGTH_ANCHOR`。不能用 `persona._render_hist`（它会把
第三人称正文里的「他」替换成「你」）和 `persona.build_head`（写死 1v1 契约），
这两处只在 novel 里按同结构写小说版。

## 三个 AI 各管一段

- 开场 AI：只跑一次，把玩法预设扩成一本世界书草稿并封样。
- 编剧 AI：把玩家的「意图」裁定成这一拍真实发生的事，交一个 JSON 改状态。
- 写作 AI：拿到**完整上下文**，只把已裁定的事实演成第三人称正文。

编剧和写作共享同一份原始状态块 / RAG / 历史，只是各自的指令不同。

## 代码只登记，判断全归模型

`_apply()` 只做类型规整、按名字合并、落盘。**不审核玩家意图合不合理**
（那正是编剧 AI 的活），也不拿词表卡模型交回来的内容。只有「没交作业」
（空返回 / JSON 解析失败）才由接口提示重来。

裁定不是拒绝：玩家说掏出手枪，编剧会写「宿舍里没有枪，你摸到的是晾衣杆」，
把意图落成能往下演的事实，而不是退回重填。

## 死亡与重开

人物进度归零，世界知识留下：`discovered` 里 `kind == "static"` 的（地图、
固定物资）跨周目继承，动态事件（尸潮、遭遇、NPC 生死）不继承、重开重掷。
RAG 的线也跟着换 epoch，免得上一周目的动态危险被检索回来。封样草稿复用，
重开不再调 LLM。
"""
import ast
import json
import logging
import os
import re
import threading
import time
import uuid

from app import llm, persona, persona_rag, writing_style
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

def _normalize_world(w):
    """老存档迁移（2026-11-12）：只在内存里归一化，下次落盘自然写回。

    第一版把 dict 形式的 item 用 str() 存成 Python 字面量，前端显示成
    「name from Name from」。这里把脏串解析回名字，并补上新字段。
    不删任何东西、不重跑封样，玩家那 5 轮进度原样保留。
    """
    if not isinstance(w, dict):
        return w
    p = w.get("player")
    if isinstance(p, dict) and isinstance(p.get("items"), list):
        p["items"] = _norm_items(p["items"])
    if not isinstance(w.get("restarts"), int):
        w["restarts"] = 0
    # 上架 5175（2026-11-12）：世界书原来是全局单文件，谁都能列出全部。
    # 老世界一律归站主（owner=""），群友世界写自己的 QQ；读写时严格比较。
    if not isinstance(w.get("owner"), str):
        w["owner"] = ""
    # 2026-11-12 真机：跨了几天，薯片可乐一份没少——世界书里根本没有时间。
    # 现在由编剧每拍登记一句「第 3 天 傍晚」，代码只存不判断。
    if not isinstance(w.get("clock"), str):
        w["clock"] = ""
    return w


def _read_db():
    try:
        with open(_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {"worlds": {}}
    if not isinstance(data, dict) or not isinstance(data.get("worlds"), dict):
        return {"worlds": {}}
    for w in data["worlds"].values():
        _normalize_world(w)
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


def get_world(world_id, owner=""):
    """按 id 取一本，**必须校验归属**。

    owner="" 是站主；5175 转发来的群友请求传自己的 QQ。只看 id 不认人会让
    猜到编号的人直接读到、改写、重开别人的本子（2026-11-12 上架前修）。
    """
    wid = str(world_id or "").strip()
    if not _SAFE_ID.match(wid):
        return None
    w = _read_db().get("worlds", {}).get(wid)
    if not isinstance(w, dict):
        return None
    return w if str(w.get("owner") or "") == str(owner or "") else None


def list_worlds(owner=""):
    """只列自己的世界。站主传 ""，群友传 QQ。"""
    out = []
    for wid, w in _read_db().get("worlds", {}).items():
        if not isinstance(w, dict):
            continue
        if str(w.get("owner") or "") != str(owner or ""):
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


_DIRECTOR_PROMPT = """你是这部长篇生存小说的编剧，负责把玩家这一轮写下的剧情落成这一拍真实发生的事。
你只改状态、不写正文；写作的人会拿到**同一份**世界书状态和你的裁定。

【玩家写的是剧情，不是一句话】
玩家这一轮给你的是**一段剧情的骨架**：他要往哪走、要见谁、想做什么。这不是一句等你
回复的话，也不是让你缩写的摘要。你的任务是把它落成这一拍确实发生的事，**节点一个都
不能少**：他写「往高层走，在 402 碰到一个叫苏然的女生」，这一拍就必须有「上楼」、
「到 402」、「门里的苏然」这几件事，按时间顺序排好。少一个节点，写作的人就接不上。

【裁定只判可行不可行，不改写剧情走向】
玩家写下的是他的意图：能不能做到，由你判。手边没有的东西，就写他伸手摸到了什么；
做不到的事，就写他试过之后的结果。
主角的身份是真实条件：文弱书生翻不了窗，体育生能撞门，医学生看得懂伤口。
但**不许否决他写的走向，也不许替他换成别的事**——他写去 402 找人，你不能裁定成
「退回 402 门口防守、把门插上」；他没写撤退、没写丢下那个人，这些决定都不属于你。
他没要求的那一段，别自己加戏。

【`facts` 按时间顺序写过程】
`facts` 是这一拍按时间顺序发生的每一步，一样一条：怎么走过去的、路上什么情况、
看到什么、对方什么反应。**不要只写结果**——「他摸到 402 门口，推开门」是过程，
「他退回 402 防守」是结果，而结果通常意味着中间全被砍了。

【道具：交完整清单，不交增量】
`items` 是**这一拍结束时主角身上/手上的全部东西**，照状态块里的「手上拿着」写：
数量变了就写新的数量（「一袋薯片」→「两包薯片」）；吃掉、用掉、放下、
离开时没带走的长物件，直接从清单里去掉；还在手上的原样抄回，别漏也别改名
（「五十厘米开刃钢刀」别写成「长刀」）；没变化就把那一行原样抄回。
只有你发放的东西才会进主角的口袋。代码只做归一化和去重，不替你判断。
写作的人会照你登记的持有物写动作，所以这一行必须准。

【时间】
每一拍都要给 `clock`：现在第几天、什么时候（「第 3 天 傍晚」）。玩家写的剧情跨了
饭点、熬了夜、睡了一觉，就照实往后推，并把吃掉喝掉的份数从清单里扣掉——过了三天
薯片可乐一份没少，那是错的。没跨时间就把上一拍的 `clock` 原样抄回。

【路线】
玩家提出一个远处的目标，就把它拆成两三段能走的路，每段是一个具体地点。
玩家中途改了主意，就重新给一条路。已经走到的位置记在 `at` 上。

【角色】
新遇到的人由你编：名字、一句话身份、跟主角的关系、愿不愿意跟着走。
这一拍新出现的人写进 `cast_new`，已经出场过的人只用 `cast_update`，别重复造人。
`tier` 写「主体」或「路人甲」。世界以主角为中心，路人甲只负责把剧情推一下。

【已经演过的事】
状态块里「已经演过的事」是上一轮及更早已经写完的戏。**只裁定本轮新增**，
不要把试过的刀、检查过的柜子、走过的路再裁定一遍。

只输出一个 JSON 对象，字段：
{"clock":"","ruling":"这一轮裁定成的事实，一句话",
 "facts":["这一拍确实发生了什么，按时间顺序一样一条"],
 "items":[{"name":"","qty":"","note":""}],
 "place":"","condition":"",
 "cast_new":[{"name":"","identity":"","relation":"","willing":"","tier":"主体","present":true}],
 "cast_update":[{"name":"","relation":"","willing":"","present":true}],
 "events_add":[{"trigger":"","result":"","done":true,"kind":"dynamic"}],
 "events_done":[],
 "discovered":[{"fact":"","kind":"static"}],
 "route":{"goal":"","nodes":[],"at":0},
 "chapter_goal":"",
 "chapter_advance":false,
 "ending":{"cause":"","summary":""},
 "actions":["","",""]}

`route` / `chapter_goal` / `ending` 这一轮没变化就给 null，`chapter_advance` 给 false。
`ending` 只在主角这一拍死了才给，写清死因和当时发生了什么。只输出 JSON。"""


_NOVEL_CONTRACT = """你是这部长篇生存小说的叙述者，用第三人称写主角「{name}」。

【人称】
正文一律写主角的名字「{name}」，不写「你」来指主角，也不写成第一人称「我」。
给玩家看的引导语、动作选项和状态卡另有其文，那部分用「你」；这里只写小说正文。

【你要做的是扩写，不是回应】
玩家这一轮写下的是一段剧情的骨架：他去哪、碰到谁、对方什么状态、他想做什么。
他不是在跟你说话，也不指望你回他一句——他要你把这副骨架写成小说正文。
骨架里有几个节点，正文里就得有几个节点，顺序照它来。

【过程必须写出来，不许跳到结果】
他写「往高层走，在 402 碰到一个叫苏然的女生，她饿了很多天」，你就从他此刻站的地方
写起：怎么上的楼、楼梯间什么样、一路经过几层、402 的门是什么状态、推开门看见什么、
苏然是什么样子、两人怎么开口、她说话什么语气。中间每一段路、每一次停顿、每一个过渡，
都由你写。直接写「他退回 402 门口把门插上」这种结果、把相遇整段跳过去，就是没写完。

【不许替玩家做决定，也不许改他写的剧情】
他没写撤退，就不许写他撤退；他没写丢下那个人，就不许写他丢下她走掉。
去哪、见谁、做什么，由他定；路上的细节、环境、动作、对话、他的心理活动，由你补。
剧情是他的，血肉是你的。你不许把「去 402 找人」改成「回 402 门口防守」。

【接上上一段】
上一轮正文的最后一句，就是这一轮的起点。地方、时间、手上拿着的东西、在场的人，
都从那儿接上，别重新开场。已经演过的事只用来衔接，**不要重演**：试过的刀不要再试
一次，走过的楼梯不要再走一遍，遇见的人不要再重新遇见一次。

【照着状态写】
「此刻的世界」是代码从世界书直接读出来的原始状态，不是别人的摘要。
主角手上拿着什么，动作里就带着什么；手上没有的东西不许凭空出现。
他身上的状态、第几天、现在在哪、在场的人，都照它写。
「这一拍必须写到的节点」是这一拍确实发生了的事，一条都不能漏；节点之间怎么写、
写多细，是你的活。

写完这一段就停：把玩家给的剧情演完，别提前演下一轮，也别替下一轮收尾。"""


def _call(messages, max_tokens, temperature, opts=None):
    """跟扮演共用链头（公用中转站），失败时顺链降级，跟 1v1 一致。

    2026-11-11：原来这里用严格模式单目标直调，中转站余额归零时
    开场直接 502，前端还写「等一下再试」，充值前完全玩不了。站主拍板放开：
    链头仍是 PERSONA_PROVIDER/MODEL，402 之后交给 llm.candidates() 顺链
    试 deepseek-flash / mimo / volc；代价是可能落到付费 provider。

    2026-11-12 上架 5175：`opts` 是接口层已经定好的链策略，服务层只吃参数、
    不 import flask、不认 QQ。None = 站主现状；群友免费层走严格模式，只打
    公用中转站（绝不静默落到付费官方），中转站挂了才由接口层扣代充次数、
    传 `provider/model=None` 走全局链。
    """
    o = opts or {}
    return llm.call_llm(
        messages,
        provider=o.get("provider", PERSONA_PROVIDER or None),
        model=o.get("model", PERSONA_MODEL or None),
        strict=bool(o.get("strict", False)),
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

def _blank_world(play, name, identity, identity_desc, preset, owner=""):
    start = dict(preset.get("start") or {})
    return {
        "id": "nv_" + time.strftime("%m%d%H%M%S") + "_" + uuid.uuid4().hex[:4],
        "owner": str(owner or ""),
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
        "restarts": 0,
        "clock": "",
        "seed": {},
        "opening_reply": "",
    }


def _identity_desc(preset, identity):
    for row in preset.get("identity_choices") or []:
        if isinstance(row, dict) and row.get("name") == identity:
            return row.get("desc") or ""
    return ""


# ─── 物品规整（代码只归一化，不判断）──────────────────────

def _item_text(x):
    """一件持有物 → 一行人话。

    2026-11-12 真机：编剧把 items_gain 交成 {"name": ..., "from": ...}，
    旧 _apply 直接 str() 存成 Python 字典字面量，前端照原样显示成
    「name from Name from」。这里只做归一化和老存档清洗，不判断内容。
    """
    if isinstance(x, dict):
        name = _as_text(x.get("name"))
        qty = _as_text(x.get("qty"))
        return (qty + name) if (qty and name) else name
    s = str(x or "").strip()
    if s.startswith("{") and "'name'" in s:
        try:
            d = ast.literal_eval(s)
        except (ValueError, SyntaxError, TypeError):
            d = None
        if isinstance(d, dict):
            return _item_text(d)
    return s


def _norm_items(rows):
    out = []
    for x in _as_list(rows):
        t = _item_text(x)
        if t:
            out.append(t[:120])
    return list(dict.fromkeys(out))


# ─── 上下文装配：照搬 persona.run_turn 的结构 ─────────────

def _state_block(world):
    """完整世界状态：代码直接从世界书读出来，不经任何模型压缩。

    编剧和写作拿到的是**同一份**。人、地点、持有物、事件、已探明事实、
    已经演过的事全在这里，写作 AI 不需要靠编剧的 facts 去猜世界长什么样。
    """
    p = world.get("player") or {}
    items = _norm_items(p.get("items") or [])
    present = [c for c in world.get("cast", []) if c.get("present")]
    lines = [
        "## 此刻的世界（代码从世界书直接读出，未经任何人压缩）",
        "主角：%s，身份：%s（%s）" % (
            p.get("name") or "", p.get("identity") or "",
            p.get("identity_desc") or ""),
        "身体状态：%s" % (p.get("condition") or "完好"),
        "现在在：%s（%s），第 %s 章，本章目标：%s" % (
            p.get("place") or "", world.get("clock") or "时间还没记",
            world.get("chapter", 1), world.get("chapter_goal") or ""),
        "手上拿着：%s" % ("；".join(items) or "空手"),
        "【上面这行是主角此刻全部持有物：动作里只能用这些，不许凭空多出别的东西；"
        "数量、放下、没带走都以它为准】",
        "在场的人：%s" % (
            "；".join("%s（%s，%s，%s）" % (
                c.get("name") or "", c.get("identity") or "",
                c.get("relation") or "", c.get("willing") or "")
                for c in present) or "只有主角一个"),
        "遇到过的人（只是名录：路过的、已经分开的、已经死了的都在里面，"
        "不是「主角团」，不在这行的人此刻不在场）：%s" % (
            "、".join(c.get("name") or "" for c in world.get("cast", [])
                     if not c.get("present")) or "还没遇到过别人"),
    ]
    discovered = world.get("discovered") or []
    # 固定事实只带最近若干条，更早的交给 RAG 检索（控制每轮 token）。
    static = [d.get("fact") for d in discovered
              if d.get("kind") == "static" and d.get("fact")]
    if static:
        lines.append("已经探明的固定事实（static，重开会继承，更早的靠回忆检索）：%s"
                     % "；".join(str(x) for x in static[-_SLICE_DISCOVERED:]))
    dyn = [d.get("fact") for d in discovered
           if d.get("kind") != "static" and d.get("fact")]
    if dyn:
        lines.append("这一周目还遇到过（dynamic，本轮看得见，重开不继承）：%s"
                     % "；".join(str(x) for x in dyn[-8:]))
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
        lines.append("【已经演过的事（只用来衔接，禁止重写、禁止再演一遍）】\n"
                     + "\n".join("- " + str(x) for x in recent_log))
    deaths = world.get("death_log") or []
    if deaths:
        last = deaths[-1]
        lines.append("上一次是怎么死的：%s（在%s）" % (
            last.get("cause") or "", last.get("at") or ""))
    return "\n\n".join(lines)


def _head(world, for_director=False):
    """逐字节稳定的 system 头，照 persona.build_head(dynamic=False) 的结构。

    所有逐轮变化的内容都挪到最后一条 user 消息，只有这里吃前缀缓存。
    编剧和写作共用人设/规则，只是契约换一份：编剧拿裁定规则，写作拿叙述契约。
    主角改名会断一次缓存，那是真变了，可接受。
    """
    p = world.get("player") or {}
    parts = [_DIRECTOR_PROMPT if for_director
             else _NOVEL_CONTRACT.format(name=p.get("name") or "主角")]
    parts.append("【主角】\n名字：%s\n身份：%s（%s）\n"
                 "身份是真实条件：文弱书生翻不了窗，体育生能撞门，"
                 "医学生看得懂伤口。" % (
                     p.get("name") or "", p.get("identity") or "",
                     p.get("identity_desc") or ""))
    rules = world.get("rules") or []
    if rules:
        parts.append("【世界规则】\n" + "；".join(str(x) for x in rules))
    return "\n\n".join(parts)


def _hist(world):
    """完整历史（滞回窗口），只透传、不做 1v1 的人称替换。

    不能调 persona._render_hist：它无条件把 assistant 正文里的「他」换成
    「你」，会把第三人称小说改成第二人称。这里只给玩家意图加一行标签，
    让模型分清「意图」和「已经写过的正文」。
    """
    out = []
    for r in persona.cache_rows(world.get("history") or []):
        role = r.get("role")
        content = str(r.get("content") or "")
        if role == "user":
            content = ("【玩家当时写下的剧情骨架（不是跟你说话，是让你扩写）】\n"
                       + content)
        out.append({"role": role, "content": content})
    return out


def _story_line(world):
    """把较早的经过串成一条因果线（零 LLM），照 persona.story_line 的立意。"""
    parts = []
    log_rows = world.get("log") or []
    older = (log_rows[:-_SLICE_LOG] if len(log_rows) > _SLICE_LOG
             else log_rows)
    if older:
        parts.append("先前经过：" + "；".join(str(x) for x in older[-10:]))
    route = world.get("route") or {}
    if route.get("goal"):
        parts.append("他正打算去%s，路线 %s，已经走到第 %s 段。" % (
            route.get("goal"),
            " → ".join(str(x) for x in (route.get("nodes") or [])),
            route.get("at", 0)))
    open_events = [e for e in (world.get("events") or [])
                   if not e.get("done")]
    if open_events:
        parts.append("还没了结：" + "；".join(
            str(e.get("trigger") or "") for e in open_events[-5:]))
    deaths = world.get("death_log") or []
    if deaths:
        last = deaths[-1]
        parts.append("上一次是怎么死的：%s（在%s）" % (
            last.get("cause") or "", last.get("at") or ""))
    return ("【故事到现在】\n" + "\n".join(parts)) if parts else ""


def _tail(world):
    """近因位尾注，永远最后一句（照 writing_style.tail 的结构）。

    人称锚点必须换小说版：writing_style.ANCHOR 写的是「用名字叫她，玩家是
    你」，那是 1v1 的共用常量，不能改；这里自己写同位置的锚点。
    篇幅锚点直接复用 writing_style.LENGTH_ANCHOR。
    """
    p = world.get("player") or {}
    name = p.get("name") or "主角"
    bits = ["正文一律用「%s」叫主角，不写「你」指他；"
            "引导语和动作选项另有其文，用「你」" % name]
    last = ""
    for r in reversed(world.get("history") or []):
        if r.get("role") == "assistant":
            last = str(r.get("content") or "")
            break
    lo = " ".join(last.split())
    if lo:
        bits.append("上一段停在「…%s」，从这里往下接，别重新开场" % lo[-26:])
    bits.append(writing_style.LENGTH_ANCHOR)
    # 放在 LENGTH_ANCHOR 之后：那段是 1v1 的口径（没东西写就停在 300 字），
    # 小说要的是扩写，近因位这句压住它，别让模型把路上的事压成一句。
    bits.append("玩家给的节点有多少就写多少，600~1500 字正常："
                "宁可长一点把过程写透，也别跳到结果")
    return "（" + " · ".join(bits) + "）"


def _rag_line(world):
    """一条世界一条线；重开换 epoch，免得上一周目的动态危险被检索回来。"""
    return "%s#%s" % (world.get("id") or "",
                      int(world.get("restarts") or 0))


def _rag_context(world):
    rows = (world.get("history") or [])[-6:]
    return "\n".join(str(r.get("content") or "")[:200] for r in rows)


def _rag_block(world, query):
    line = _rag_line(world)
    if not persona_rag.enabled_for(line):
        return ""
    try:
        return persona_rag.build_block(line, query or "",
                                       context=_rag_context(world))
    except Exception:                                       # noqa: BLE001
        log.exception("[novel] RAG 检索失败，本轮不带长期记忆")
        return ""


def _user_block(world, facts=None, said="", for_director=False):
    """最后一条 user 消息：玩家写的剧情 → 回忆 → 发展线 → 状态 → 节点 → 尾注。"""
    parts = ["【玩家这一轮写下的剧情（这是这一段的骨架：去哪、遇到谁、做什么。"
             "照它扩写成正文，节点一个不许漏，不许跳过过程直接写结果，"
             "也不许换成别的事）】\n"
             + (said or "（开场）")]
    rag = _rag_block(world, said)
    if rag:
        parts.append(rag)
    story = _story_line(world)
    if story:
        parts.append(story)
    parts.append(_state_block(world))
    if for_director:
        parts.append("（这一轮只裁定新增事实，交 JSON，不写正文；"
                     "玩家写的每个节点都要落成事实，不许换成别的事；"
                     "已经演过的事不要再裁定一遍）")
    else:
        parts.append("【这一拍必须写到的节点（按时间顺序，一条都不许漏；"
                     "节点之间的过场、环境、动作、对话由你写）】\n"
                     + _facts_text(facts))
        parts.append(_tail(world))
    return "\n\n".join(parts)


def _messages(world, facts=None, said="", for_director=False):
    """[system 头] + 完整历史 + [user]。两个 AI 结构相同、契约不同。"""
    return ([{"role": "system", "content": _head(world, for_director)}]
            + _hist(world)
            + [{"role": "user",
                "content": _user_block(world, facts, said, for_director)}])


def warm_world(world):
    """打开一本老世界就把已有 history 排进 RAG 回填，不用等下一轮写完。

    2026-11-12：第一版小说模式没有 RAG，站主正在玩的那 5 轮只有 world
    里的 history。`schedule_backfill` 按 line 幂等、后台执行，所以这里可以
    在 `/api/novel/state` 里放手调——打开即排队，接着写第一轮就能检索到
    之前的正文。回填不改动 world，也不重跑封样。
    """
    line = _rag_line(world)
    if not persona_rag.enabled_for(line):
        return
    try:
        persona_rag.warm()
        persona_rag.schedule_backfill(
            line, lambda: list(world.get("history") or []))
    except Exception:                                       # noqa: BLE001
        log.exception("[novel] RAG 回填排队失败，正文照常")


def _remember(world, said, reply):
    """这一轮写进长期记忆（复用 persona_rag，检索/入库/回填零改动）。"""
    line = _rag_line(world)
    if not persona_rag.enabled_for(line):
        return
    warm_world(world)
    try:
        persona_rag.remember(
            line, said, reply, custom=llm.get_custom_override(),
            card_name=(world.get("player") or {}).get("name") or "主角")
    except Exception:                                       # noqa: BLE001
        log.exception("[novel] RAG 写入失败，本轮正文照常返回")


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
    if isinstance(d.get("items"), list):
        # 新协议：编剧交回「这一拍结束时的完整持有清单」。数量变化、放下、
        # 没带走、还在不在，全由编剧照状态块写；代码只归一化、去重、落盘。
        p["items"] = _norm_items(d["items"])
    else:
        # 兼容旧增量协议（老剧本 / 既有测试）：同样只归一化，不判断内容。
        gains = _norm_items(d.get("items_gain"))
        drop = set(_norm_items(d.get("items_use"))) | set(
            _norm_items(d.get("items_lose")))
        p["items"] = [x for x in dict.fromkeys(
            _norm_items(p.get("items")) + gains) if x not in drop]

    place = _as_text(d.get("place"), p.get("place") or "")
    if place:
        p["place"] = place
    condition = _as_text(d.get("condition"), p.get("condition") or "")
    if condition:
        p["condition"] = condition
    clock = _as_text(d.get("clock"))
    if clock:
        world["clock"] = clock

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

def _open_ai(world, preset, opts=None):
    p = world["player"]
    msg = _OPEN_PROMPT.format(
        preset=json.dumps(preset, ensure_ascii=False),
        doc=_opening_doc(world["play"]) or "（这个玩法没有额外的开场说明）",
        name=p.get("name") or "",
        identity=p.get("identity") or "",
        identity_desc=p.get("identity_desc") or "",
    )
    raw = _call([{"role": "user", "content": msg}], OPEN_MAX_TOKENS, 0.8,
                opts)
    return _parse_json(raw)


def _director(world, said, opts=None):
    """编剧：拿完整上下文，但 system 头是裁定规则（不是叙述契约）。"""
    raw = _call(_messages(world, said=said, for_director=True),
                DIRECTOR_MAX_TOKENS, 0.7, opts)
    return _parse_json(raw)


def _writer(world, facts, said, opts=None):
    """写作：拿完整上下文（人设 + 完整历史 + RAG + 状态 + 原话 + facts + 尾注）。

    2026-11-12 修正：原来只给一条 user 消息（切片 + facts），等于把世界
    交给编剧压缩后再转述给写作 AI —— 这正是 1v1 早就修掉的老问题。
    """
    out = _call(_messages(world, facts=facts, said=said),
                WRITER_MAX_TOKENS, 1.0, opts)
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

def open_world(play, name, identity, world_id=None, owner="", opts=None):
    """开场封样：一次开场 AI + 一次写作 AI。返回给前端的完整一屏。

    传了 world_id 且世界已存在时直接返回那一本，**不再跑 LLM**——站主怕
    「再点一次开场封样又重新封一遍」，身份一旦选好就固定，除非重开。
    `owner` 由接口层给（站主 ""、群友 QQ），幂等分支也要校验，别让群友
    拿站主那本的 id 直接套到自己的封样上。
    """
    if world_id:
        existing = get_world(world_id, owner)
        if existing:
            warm_world(existing)
            return {"ok": True, "world_id": existing["id"],
                    "reply": existing.get("opening_reply") or "",
                    "note": "", "sealed": True,
                    "state": state_card(existing)}
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
                         _identity_desc(preset, identity), preset, owner)
    seed = _open_ai(world, preset, opts)
    _merge_seed(world, seed)
    facts = _as_list(seed.get("opening_facts"))
    reply = _writer(world, facts, "", opts)
    world["opening_reply"] = reply
    world["history"].append({"role": "assistant", "content": reply})
    _save_world(world)
    _remember(world, "（开场）", reply)
    return {"ok": True, "world_id": world["id"], "reply": reply,
            "note": preset.get("opening_note") or "", "sealed": True,
            "state": state_card(world)}


def rename(world_id, name, owner=""):
    """主角名字随时可改；身份不在这里动（要改身份只能重开一本）。

    只改状态里的名字，已写过的正文和开场白留着原名——那是当时确实写下的文字。
    """
    w = get_world(world_id, owner)
    if not w:
        raise ValueError("没有这个世界")
    name = _as_text(name)
    if not name:
        raise ValueError("名字不能是空的")
    w["player"]["name"] = name[:24]
    _save_world(w)
    return {"ok": True, "state": state_card(w)}


def run_turn(world_id, said, owner="", opts=None, turn_token=None):
    """一轮：编剧裁定 → 登记 → 写作演出。

    `turn_token`（2026-11-12 上架 5175）：一轮要串行两次 LLM，网关那头可能
    先超时再被手点一次。同一个 token 直接把上一轮结果原样返回，不重跑模型、
    不重复扣代充次数。编剧/写作任一步抛异常时世界书还没落盘（`_apply` 只改
    内存副本），接口层整轮换一条链重跑是安全的。
    """
    said = _as_text(said)
    if not said:
        raise ValueError("这一轮没有写意图")
    world = get_world(world_id, owner)
    if not world:
        raise ValueError("没有这个世界")
    token = _as_text(turn_token)
    last = world.get("last_turn_result")
    if (token and token == world.get("last_turn_token")
            and isinstance(last, dict)):
        return dict(last)
    if world.get("ending"):
        return {"ok": False, "error": "这一本已经结束了，重开一本吧",
                "state": state_card(world)}

    d = _director(world, said, opts)
    ruling, facts = _apply(world, d, said)
    reply = _writer(world, [ruling] + facts if ruling else facts, said, opts)
    world["history"].append({"role": "assistant", "content": reply})
    world["history"] = world["history"][-_HISTORY_MAX:]
    out = {"ok": True, "reply": reply, "ruling": ruling,
           "ending": world.get("ending"), "state": state_card(world)}
    if token:
        world["last_turn_token"] = token
        world["last_turn_result"] = dict(out)
    _save_world(world)
    _remember(world, said, reply)
    return out


def restart(world_id, name=None, identity=None, owner=""):
    """死亡重开：人物归零，静态世界事实与死因留下，封样复用，不重跑 LLM。"""
    old = get_world(world_id, owner)
    if not old:
        raise ValueError("没有这个世界")
    preset = load_preset(old.get("play"))
    if not preset:
        raise ValueError("玩法预设不见了")
    p = old.get("player") or {}
    name = _as_text(name) or p.get("name") or "无名的人"
    identity = _as_text(identity) or p.get("identity") or ""
    fresh = _blank_world(old["play"], name, identity,
                         _identity_desc(preset, identity), preset, owner)
    fresh["owner"] = str(old.get("owner") or "")
    fresh["id"] = old["id"]                 # 同一个世界，同一条线
    fresh["created"] = old.get("created", fresh["created"])
    fresh["restarts"] = int(old.get("restarts") or 0) + 1  # RAG 换线，旧周目记忆不串
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
        "clock": world.get("clock") or "",
        "ending": world.get("ending"),
        "player": {
            "name": p.get("name") or "",
            "identity": p.get("identity") or "",
            "identity_desc": p.get("identity_desc") or "",
            "place": p.get("place") or "",
            "condition": p.get("condition") or "",
            "items": _norm_items(p.get("items") or []),
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
