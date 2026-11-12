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

## 回退（2026-11-12）

只回退**最近一轮**，而且只给一次：`world["undo"]` 里存着一轮开始前的那份
状态快照（`_UNDO_KEYS` + history/log 长度），回退时还原它、把 history/log 截回
那个长度、清掉 `undo`。**回退过一次就必须先往下写一轮才能再回退**，不许连着
回退两次——连着退会把整条历史拆碎，状态和正文对不上。

RAG 那边不自己造轮子：`persona_rag.forget_last(line)` 就是 1v1 早就做好、
专门处理「刚入库又回退」的那支。

## 死亡与重开

人物进度归零，世界知识留下：`discovered` 里 `kind == "static"` 的（地图、
固定物资）跨周目继承，动态事件（尸潮、遭遇、NPC 生死）不继承、重开重掷。
RAG 的线也跟着换 epoch，免得上一周目的动态危险被检索回来。封样草稿复用，
重开不再调 LLM。
"""
import ast
import copy
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

# 读超时要按输出量单独给（2026-11-12 真机修）：全局 LLM_REQUEST_TIMEOUT 是
# 60 秒，那是对话抢答的口径。开场要一次吐 3000+ 字的世界书草稿，实测缓存
# 命中时 22.5 秒，未命中或中转站抖动就到 50~70 秒，**正好卡在 60 秒线上**；
# 撞了超时还会顺降级链换家从头再生成一遍，用户等两分钟只等到一句 502。
# 漫画台 `silver_comic.write_script_detail` 早就是同样的处理（50 格给 120s
# 起步），这里照抄那个先例，按各段的输出上限分档。写作段 600~1500 字卡在
# 60 秒边缘的最多，所以给得比编剧宽。
OPEN_TIMEOUT = 150.0
WRITER_TIMEOUT = 120.0
DIRECTOR_TIMEOUT = 90.0


# ─── 玩法预设 ─────────────────────────────────────────

def _disk_plays():
    """扫 novels/*/preset.json。现在只有「丧尸末日」一份官方玩法。"""
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
                "custom": False,
            })
    return out


def play_key(world):
    """这本书属于哪个玩法。

    自建玩法的 `play` 一律是 `"custom"`（同一个驱动，世界观各不相同），真正
    区分它们的是生成时 AI 定下的那个玩法名，所以键写成 `custom:<玩法名>`。
    老存档没有 preset 的照旧用 `play`，行为和以前完全一样。
    """
    if not isinstance(world, dict):
        return ""
    p = world.get("preset")
    if isinstance(p, dict) and _as_text(p.get("name")):
        return "custom:" + _as_text(p.get("name"))
    return str(world.get("play") or "")


def list_plays(owner=""):
    """官方玩法（磁盘预设）+ 这个人自己的玩法（从世界书里推导）。

    自建玩法不落 `novels/` 目录——那里是全局的、不认 owner，写进去等于把
    私人设定发给所有人。它内嵌在各自的世界书里，所以「我的玩法」只能从
    「我有哪些书」反推：同一份预设名下的书算同一个玩法，删掉最后一本，
    这个玩法条目跟着消失（也正是不想让别人看到的那份设定）。
    """
    out = _disk_plays()
    mine = {}
    books = {}
    for w in _read_db().get("worlds", {}).values():
        if not isinstance(w, dict):
            continue
        if str(w.get("owner") or "") != str(owner or ""):
            continue
        key = play_key(w)
        books[key] = books.get(key, 0) + 1
        if not key.startswith("custom:"):
            continue
        row = mine.get(key)
        if row is None:
            preset = w.get("preset") or {}
            mine[key] = {
                "id": key,
                "name": key[len("custom:"):],
                "tagline": _as_text(preset.get("tagline")),
                "brief": _as_text(preset.get("brief")),
                "identity_choices": preset.get("identity_choices") or [],
                "opening_note": _as_text(preset.get("opening_note")),
                "custom": True,
                "books": 0,
            }
    # 「有几本」由代码数，别让前端再算一遍（两边算法一分叉就会出现
    # 「写着 2 本、点开只有 1 本」）。
    for p in out:
        p["books"] = books.get(p["id"], 0)
    for key, row in mine.items():
        row["books"] = books.get(key, 0)
    out.extend(sorted(mine.values(), key=lambda x: x["name"]))
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


def world_preset(world):
    """这一本用的是哪份玩法预设。

    自建玩法把整份预设内嵌在世界书里（`world["preset"]`），因为它不属于
    任何磁盘目录；普通玩法照旧读 `novels/<id>/preset.json`。重开、开场、
    世界书面板都走这里，才不会出现「自建世界重开后掉回丧尸末日」。
    """
    p = (world or {}).get("preset")
    if isinstance(p, dict) and _as_text(p.get("name")):
        return p
    return load_preset((world or {}).get("play"))


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
    # 自建玩法（2026-11-12）：玩家自己写的世界观内嵌在这一本里，不进
    # novels/ 目录——那里是全局的、不认 owner，写进去等于群友共享。
    # 老世界没有这份，回退读磁盘预设，行为和以前完全一样。
    if not isinstance(w.get("preset"), dict):
        w["preset"] = {}
    # 「世界书」弹窗要标出哪些地方去过。老存档没有这份事实，只能用它
    # 现在站的地方 + 挂过事的地方兜底，不猜没记录的脚程。
    if not isinstance(w.get("visited"), list):
        v = []
        here = (w.get("player") or {}).get("place")
        if isinstance(here, str) and here:
            v.append(here)
        for e in w.get("events") or []:
            at = (e or {}).get("place") if isinstance(e, dict) else ""
            if isinstance(at, str) and at and at not in v:
                v.append(at)
        w["visited"] = v
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
            "play_key": play_key(w),
            "play_name": w.get("play_name") or w.get("play") or "",
            "custom": bool(w.get("preset")),
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


# 自建玩法（2026-11-12）：玩家填一张表，AI 把它补成能长期写下去的世界书。
# 这里用 replace 而不是 format——JSON 里的花括号不用转义，也就不会重演
# 「{{」被原样喂给模型那种事故。
_CUSTOM_PROMPT = """你在为一部长篇互动小说搭世界书。玩家自己填了一份设定表：
填了的是硬事实，一条都不许改、不许换个说法；空着的才由你补。

补的方向只有一个：**能撑住长篇**。地点要够走动，人物要够反复打交道，
规则要能用来判断「这件事做不做得到」。

【玩家填的表】
<<TABLE>>

【主角】
名字：<<NAME>>

硬性要求：
1. rules 写世界的机制（什么会致命、什么稀缺、什么被禁止），不写剧情。
   给 3~6 条，每条一句话，将来要能回答「这样干行不行」。
2. places 给 6~10 个，每个写清它是什么样子、能翻到什么。kind 只有两个值：
   static 是地图结构和固定物资，dynamic 是会随每次重开变化的东西。
3. cast_seed 给 3~5 人，每个人有自己的打算和难处，不是围着主角转的工具人。
   tier 填「主体」（会反复出现）或「路人甲」（一面之缘）。
   willing 写他此刻愿不愿意帮主角，要具体。
4. identity_choices 给 3 个身份，每个都有长处也有短处，短处要具体到体力、
   知识、胆量或人脉——它们决定主角做得到什么、做不到什么。
   表里点了名的身份就按名单来，你只补 desc。
5. start 是开场时主角站的地方、身上有什么。起点要能立刻写出画面，
   别一上来就是绝境。
6. opening_facts 写开场这一屏必须写到的 3~5 件事，按时间顺序。
7. actions 给 3 个开场就能做的动作，具体、可执行，不要「观察四周」这种空话。

只输出一个 JSON 对象，不要任何解释：
{"name":"","tagline":"","brief":"",
 "rules":["","",""],
 "identity_choices":[{"name":"","desc":""}],
 "start":{"place":"","condition":"","items":[""]},
 "places":[{"name":"","detail":"","search":"","kind":"static"}],
 "item_pool":[""],
 "cast_seed":[{"name":"","identity":"","relation":"","willing":"","tier":"主体","present":false}],
 "chapter_goal":"",
 "opening_facts":["","",""],
 "actions":["","",""],
 "discovered":[{"fact":"","kind":"static"}]}"""


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
`kind` 写「人」或「威胁」：丧尸、怪物、野兽这类不是人的写「威胁」（它只记个名，
不进「遇到过的人」）；活人一律写「人」，哪怕他是要杀主角的敌人。

【换地方就要交代在场的人】
主角离开一处、走到另一处，就把「谁跟着走、谁留在原地」在 `cast_update` 里写清楚：
跟着走的人 `present` 报 true，留在原地的报 false。没在本拍点名的人，代码一律按
「已经分开」处理——他走开了，那个人不该还挂在「在场的人」里。

【事件要当场分清风声了结还是留了尾巴】
`events_add` 只登记**还没兑现的伏笔**：谁被惊动了会追来、什么动静会引来尸群、
哪扇门后面还有东西。这一拍当场就发生完、结果已经写进 `facts` 的事（砍断丧尸、
炸开一扇门、声音传出去），`done` 一律给 true，别把它们挂成未了结。
主角已经离开那个地方、那件事不会再推进的，从 `events_done` 里点名结掉。

【已经演过的事】
状态块里「已经演过的事」是上一轮及更早已经写完的戏。**只裁定本轮新增**，
不要把试过的刀、检查过的柜子、走过的路再裁定一遍。

只输出一个 JSON 对象，字段：
{"clock":"","ruling":"这一轮裁定成的事实，一句话",
 "facts":["这一拍确实发生了什么，按时间顺序一样一条"],
 "items":[{"name":"","qty":"","note":""}],
 "place":"","condition":"",
 "cast_new":[{"name":"","kind":"人","identity":"","relation":"","willing":"","tier":"主体","present":true}],
 "cast_update":[{"name":"","kind":"人","relation":"","willing":"","present":true}],
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


def _call(messages, max_tokens, temperature, opts=None, timeout=None):
    """跟扮演共用链头（公用中转站），失败时顺链降级，跟 1v1 一致。

    2026-11-11：原来这里用严格模式单目标直调，中转站余额归零时
    开场直接 502，前端还写「等一下再试」，充值前完全玩不了。站主拍板放开：
    链头仍是 PERSONA_PROVIDER/MODEL，402 之后交给 llm.candidates() 顺链
    试 deepseek-flash / mimo / volc；代价是可能落到付费 provider。

    2026-11-12 上架 5175：`opts` 是接口层已经定好的链策略，服务层只吃参数、
    不 import flask、不认 QQ。None = 站主现状；群友免费层走严格模式，只打
    公用中转站（绝不静默落到付费官方），中转站挂了才由接口层扣代充次数、
    传 `provider/model=None` 走全局链。

    `timeout` 由三个调用点各自给（`OPEN/WRITER/DIRECTOR_TIMEOUT`），不传才落到
    全局 60 秒。别在这里写死一个值：同一份代码要服务 3000 字的开场和 600 字
    的一轮，输出量差一倍，共用一个超时必然有一边被卡。
    """
    o = opts or {}
    return llm.call_llm(
        messages,
        provider=o.get("provider", PERSONA_PROVIDER or None),
        model=o.get("model", PERSONA_MODEL or None),
        strict=bool(o.get("strict", False)),
        max_tokens=max_tokens,
        temperature=temperature,
        timeout=timeout,
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


def _lines(v):
    """一行一条。自建玩法的表格字段在前端就是一个 textarea，玩家怎么断行
    就怎么算一条，不做顿号/分号拆词——那是替他理解自己写的东西。"""
    if isinstance(v, list):
        rows = [str(x) for x in v]
    else:
        rows = str(v or "").splitlines()
    return [x.strip() for x in rows if x.strip()]


def _pairs(v):
    """一行一个；行内用 `|` 分开名字和描述。没写描述的交给 AI 补。"""
    out = []
    for line in _lines(v):
        name, _, desc = line.partition("|")
        name = name.strip()
        if name:
            out.append({"name": name, "desc": desc.strip()})
    return out


def _table_text(table):
    """把玩家填的表渲染成给 AI 看的一屏事实。没填的明确标出来。"""
    t = table if isinstance(table, dict) else {}
    rows = [
        ("玩法名", t.get("name")),
        ("世界规则（一行一条）", t.get("rules")),
        ("地点（一行一个）", t.get("places")),
        ("开局在哪儿", t.get("start_place")),
        ("开局身上有什么", t.get("start_items")),
        ("我是谁（身份 · 长处短处）", t.get("identities")),
        ("起手的人（名字 · 是谁 · 和主角什么关系）", t.get("cast")),
        ("这一章要干什么", t.get("goal")),
    ]
    out = []
    for label, v in rows:
        vals = _lines(v)
        if not vals:
            out.append("%s：（没填，你定）" % label)
        elif len(vals) == 1:
            out.append("%s：%s" % (label, vals[0]))
        else:
            out.append("%s：\n%s" % (label, "\n".join("  - " + x for x in vals)))
    return "\n".join(out)


# ─── 世界书 ──────────────────────────────────────────

# 一轮会改动的状态字段。history / log 是追加的，只记「这一轮追加了几条」，
# 回退时从尾巴削掉——不整份复制，一本世界不用多背几十 KB 的快照。
_UNDO_KEYS = ("player", "cast", "events", "discovered", "route", "chapter",
              "chapter_goal", "ending", "death_log", "actions", "turns",
              "clock")


def _snapshot(world):
    """一轮开始前的状态快照，只留一份（存在 world["undo"]）。"""
    return {
        "state": {k: copy.deepcopy(world.get(k)) for k in _UNDO_KEYS},
        "hist_added": 0,
        "log_added": 0,
    }


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
        "undo": None,
        "seed": {},
        "preset": {},
        "visited": [start.get("place")] if start.get("place") else [],
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
                     if not c.get("present")
                     and c.get("kind") != "威胁") or "还没遇到过别人"),
    ]
    threats = [c.get("name") or "" for c in world.get("cast", [])
               if c.get("kind") == "威胁" and not c.get("present")]
    if threats:
        lines.append("打过交道的威胁（不是人，只是记得遇过；此刻不在场）：%s"
                     % "、".join(threats))
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
        here = p.get("place") or ""
        shown = []
        for e in open_events[-5:]:
            at = e.get("place") or ""
            tail = ("（挂在%s，你已经离开那儿了）" % at) if (at and at != here) else ""
            shown.append(str(e.get("trigger") or "") + tail)
        lines.append("还没了结的事：%s" % "；".join(shown))
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
        # kind：人是「人」，丧尸/怪物/动物这类是「威胁」。右栏按它分两栏，
        # 免得「遇到过的人」里蹲着一只丧尸（用户 2026-11-12 真机点名）。
        row = {"name": name, "kind": "人", "identity": "", "relation": "",
               "willing": "", "tier": "路人甲", "present": True}
        world["cast"].append(row)
    for key in ("kind", "identity", "relation", "willing", "tier", "present",
                "status"):
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

    old_place = p.get("place") or ""
    place = _as_text(d.get("place"), old_place)
    if place:
        p["place"] = place
    moved = bool(place) and place != old_place
    if moved:
        # 「世界书」弹窗要标出去过哪儿。只追加事实，不猜脚程。
        seen = world.setdefault("visited", [])
        if place not in seen:
            seen.append(place)
    condition = _as_text(d.get("condition"), p.get("condition") or "")
    if condition:
        p["condition"] = condition
    clock = _as_text(d.get("clock"))
    if clock:
        world["clock"] = clock

    named = []
    for item in _as_list(d.get("cast_new")) + _as_list(d.get("cast_update")):
        _merge_cast(world, item)
        if isinstance(item, dict) and _as_text(item.get("name")):
            named.append(_as_text(item.get("name")))
    # 换地方是事实，人不会跟着瞬移：换到别处之后，编剧没在本拍点名的人
    # 一律记为已经分开。原来 present 全靠编剧自己报，它漏一次，走掉的丧尸
    # 就会永远挂在「在场的人」里。跟着走的人由编剧 cast_update 报回来，
    # 状态块里也写了这条规则，所以这不是替模型判断，只是把「你走开了」
    # 这个事实落到它该落的地方。
    if moved:
        for c in world.get("cast", []):
            if c.get("present") and c.get("name") not in named:
                c["present"] = False

    for item in _as_list(d.get("events_add")):
        if isinstance(item, dict) and _as_text(item.get("trigger")):
            # 有 result 又没显式写 done 的，按「这一拍已经兑现」算：
            # 「砍断丧尸的动静在楼道里传开」这种当场就发生完的事，原来
            # 一直被挂成未了结，主角走了三个地图它还挂着。
            has_result = bool(_as_text(item.get("result")))
            done_flag = item.get("done")
            world["events"].append({
                "trigger": _as_text(item.get("trigger")),
                "result": _as_text(item.get("result")),
                "done": (done_flag if isinstance(done_flag, bool)
                         else has_result),
                "kind": _as_text(item.get("kind"), "dynamic"),
                "chapter": world.get("chapter", 1),
                "place": _as_text(item.get("place")) or (p.get("place") or ""),
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
    _log0 = len(world["log"])
    for line in [ruling] + facts:
        if line:
            world["log"].append(line[:200])
    # 回退要按「这一轮追加了几条」从尾巴削，不能用长度截断：log 满 80 条时
    # 新加的行会同时把最老的顶掉，按长度切等于一条都不删。
    if isinstance(world.get("undo"), dict):
        world["undo"]["log_added"] = len(world["log"]) - _log0
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
                opts, timeout=OPEN_TIMEOUT)
    return _parse_json(raw)


def _director(world, said, opts=None):
    """编剧：拿完整上下文，但 system 头是裁定规则（不是叙述契约）。"""
    raw = _call(_messages(world, said=said, for_director=True),
                DIRECTOR_MAX_TOKENS, 0.7, opts, timeout=DIRECTOR_TIMEOUT)
    return _parse_json(raw)


def _writer(world, facts, said, opts=None):
    """写作：拿完整上下文（人设 + 完整历史 + RAG + 状态 + 原话 + facts + 尾注）。

    2026-11-12 修正：原来只给一条 user 消息（切片 + facts），等于把世界
    交给编剧压缩后再转述给写作 AI —— 这正是 1v1 早就修掉的老问题。
    """
    out = _call(_messages(world, facts=facts, said=said),
                WRITER_MAX_TOKENS, 1.0, opts, timeout=WRITER_TIMEOUT)
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


# ─── 自建玩法 ────────────────────────────────────────

def _preset_from_draft(draft, table):
    """把开场 AI 交回来的草稿收成一份玩法预设。

    取值优先级只有一条：**玩家在表里填了的，以玩家为准**，AI 只负责补描述。
    这里不判断世界观合不合理，也不替玩家把空着的地方写成他的意思。
    """
    t = table if isinstance(table, dict) else {}
    d = draft if isinstance(draft, dict) else {}
    start = d.get("start") if isinstance(d.get("start"), dict) else {}
    name = _as_text(t.get("name")) or _as_text(d.get("name")) or "自定世界"

    rules = _lines(t.get("rules"))
    if not rules:
        rules = [str(x).strip() for x in _as_list(d.get("rules"))
                 if str(x).strip()]

    choices = []
    for row in _as_list(d.get("identity_choices")):
        if isinstance(row, dict) and _as_text(row.get("name")):
            choices.append({"name": _as_text(row.get("name")),
                            "desc": _as_text(row.get("desc"))})
    want = _pairs(t.get("identities"))
    if want:
        # 玩家点了名要哪些身份：描述优先用他自己写的，没写才用 AI 补的。
        # AI 漏掉某个名字也照样收进来，不能让玩家写下的设定凭空少一项。
        by = {c["name"]: c["desc"] for c in choices}
        choices = [{"name": x["name"], "desc": x["desc"] or by.get(x["name"], "")}
                   for x in want]

    goal = (_as_text(t.get("goal")) or _as_text(d.get("chapter_goal"))
            or _as_text(start.get("chapter_goal")))
    items = _lines(t.get("start_items")) or _norm_items(start.get("items"))
    return {
        "id": "custom",
        "name": name,
        "tagline": _as_text(d.get("tagline")),
        "version": 1,
        "brief": _as_text(d.get("brief")),
        "rules": rules,
        "identity_choices": choices,
        "start": {
            "place": _as_text(t.get("start_place")) or _as_text(start.get("place")),
            "condition": _as_text(start.get("condition")),
            "items": items,
            "chapter": 1,
            "chapter_goal": goal,
        },
        "places": _lines(t.get("places")),
        "opening_director": "",
        "opening_note": _as_text(d.get("opening_note")),
        "opening_actions": [str(x).strip() for x in _as_list(d.get("actions"))
                            if str(x).strip()][:3],
    }


def _custom_open_ai(table, name, opts=None):
    """表格 → 世界书草稿。一次调用同时交出玩法预设和开局世界。"""
    msg = (_CUSTOM_PROMPT
           .replace("<<TABLE>>", _table_text(table))
           .replace("<<NAME>>", name or "无名的人"))
    raw = _call([{"role": "user", "content": msg}], OPEN_MAX_TOKENS, 0.8, opts,
                timeout=OPEN_TIMEOUT)
    return _parse_json(raw)


def _seal_custom(world, identity, opts=None):
    """自建玩法的开场正文。身份定下来之后才写，不然正文会写错人。"""
    preset = world_preset(world) or {}
    p = world["player"]
    p["identity"] = identity
    desc = _identity_desc(preset, identity)
    if desc:
        p["identity_desc"] = desc
    seed = world.get("seed") or {}
    reply = _writer(world, _as_list(seed.get("opening_facts")), "", opts)
    world["opening_reply"] = reply
    world["history"].append({"role": "assistant", "content": reply})
    _save_world(world)
    _remember(world, "（开场）", reply)
    return {"ok": True, "world_id": world["id"], "reply": reply,
            "note": preset.get("opening_note") or "", "sealed": True,
            "state": state_card(world)}


def _open_custom(table, name, identity, owner="", opts=None):
    """自建玩法开场。表里写了身份就直接封完；没写就先交出候选。

    没写身份时返回 `need_identity=True` 和三个身份，前端挑一个再调回来
    （带 world_id），这时才写正文。世界书已经落盘，所以那一步不重跑 AI、
    也不会因为网关超时白烧一次整本世界观。
    """
    name = _as_text(name) or "无名的人"
    identity = _as_text(identity)
    draft = _custom_open_ai(table, name, opts)
    preset = _preset_from_draft(draft, table)
    choices = preset.get("identity_choices") or []
    pick = identity or (choices[0]["name"] if choices else "普通人")

    world = _blank_world("custom", name, pick,
                         _identity_desc(preset, pick), preset, owner)
    world["preset"] = preset
    _merge_seed(world, draft)
    if not identity and len(choices) > 1:
        # 只写了一个身份就是已经选好了，不用再问一遍；真的空着才给候选。
        _save_world(world)
        return {"ok": True, "world_id": world["id"], "need_identity": True,
                "play_name": preset.get("name") or "",
                "identity_choices": choices, "state": state_card(world)}
    return _seal_custom(world, pick, opts)


def _custom_preset(play, owner=""):
    """`custom:<玩法名>` → 世界书里那份内嵌预设（取最近生成的一本）。

    自建玩法没有磁盘文件，设定只活在世界书里，所以「用同一个玩法再开一本」
    只能从已有的书里反查。一本都没有就返回 None，由调用方报「没有这个玩法」。
    """
    key = str(play or "").strip()
    if not key.startswith("custom:"):
        return None
    want = key[len("custom:"):]
    best, best_at = None, None
    for w in _read_db().get("worlds", {}).values():
        if not isinstance(w, dict):
            continue
        if str(w.get("owner") or "") != str(owner or ""):
            continue
        p = w.get("preset")
        if not isinstance(p, dict) or _as_text(p.get("name")) != want:
            continue
        at = float(w.get("created") or 0)
        if best is None or at >= best_at:
            best, best_at = (w, p), at
    return best


def _open_replay(play, name, identity, owner="", opts=None):
    """用自己已有的玩法再开一本：世界观照抄，只重写开场正文。

    不重跑开场 AI —— 那份设定是玩家看过、认过的，重新生成等于偷偷换掉他
    的世界。地点表、固定物资、人物种子从源世界那份 `seed` 里来；动态的东西
    （走过的路、手上拿的、遇见过的人）一律不带，新书就是新书。
    """
    found = _custom_preset(play, owner)
    if not found:
        raise ValueError("没有这个玩法")
    src, preset = found
    name = _as_text(name) or "无名的人"
    choices = [c for c in (preset.get("identity_choices") or [])
               if isinstance(c, dict)]
    identity = _as_text(identity)
    if not identity and choices:
        identity = _as_text(choices[0].get("name"))
    identity = identity or "普通人"
    world = _blank_world("custom", name, identity,
                         _identity_desc(preset, identity), preset, owner)
    world["preset"] = preset
    world["play_name"] = _as_text(preset.get("name")) or world["play_name"]
    try:                                    # seed 来自 JSON，深拷一份免得两本共享
        seed = json.loads(json.dumps(src.get("seed") or {}, ensure_ascii=False))
    except (TypeError, ValueError):
        seed = {}
    _merge_seed(world, seed)
    # 只继承「不会随重开变化」的固定事实，跟 restart 一个口径。
    world["discovered"] = [d for d in (src.get("discovered") or [])
                           if isinstance(d, dict) and d.get("kind") == "static"]
    return _seal_custom(world, identity, opts)


# ─── 对外接口 ────────────────────────────────────────

def open_world(play, name, identity, world_id=None, owner="", opts=None,
               table=None):
    """开场封样：一次开场 AI + 一次写作 AI。返回给前端的完整一屏。

    传了 world_id 且世界已存在时直接返回那一本，**不再跑 LLM**——站主怕
    「再点一次开场封样又重新封一遍」，身份一旦选好就固定，除非重开。
    `owner` 由接口层给（站主 ""、群友 QQ），幂等分支也要校验，别让群友
    拿站主那本的 id 直接套到自己的封样上。

    `table` 非空 = 自建玩法（玩家填的那张表），走 `_open_custom`；此时
    `play` 没有意义，世界书里的玩法是 AI 现生成的那一份。
    """
    if world_id:
        existing = get_world(world_id, owner)
        if existing:
            # 半封样：世界观已经生成好、正文还没写，这次带着身份回来。
            # 接着封完，不重跑开场 AI（重跑会换掉玩家刚看过的世界）。
            if (not _as_text(existing.get("opening_reply"))
                    and isinstance(existing.get("preset"), dict)
                    and existing["preset"].get("name") and _as_text(identity)):
                existing["owner"] = str(owner or "")
                return _seal_custom(existing, _as_text(identity), opts)
            warm_world(existing)
            return {"ok": True, "world_id": existing["id"],
                    "reply": existing.get("opening_reply") or "",
                    "note": "", "sealed": True,
                    "state": state_card(existing)}
    if isinstance(table, dict):
        return _open_custom(table, name, identity, owner=owner, opts=opts)
    if str(play or "").strip().startswith("custom:"):
        return _open_replay(play, name, identity, owner=owner, opts=opts)
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


def delete_world(world_id, owner=""):
    """整本删掉：世界书 + 这条线在 RAG 里的长期记忆，一起消失。

    接口层必须先让玩家把确认句手打一遍，这里不再问第二遍，只做事实。
    **没有回收站**：`state/novel_worlds.json` 里那一项直接摘掉；RAG 每代
    （`id#0`、`id#1`…）也一起清，否则删了书，那份记忆还躺在检索池里。
    """
    wid = str(world_id or "").strip()
    w = get_world(wid, owner) if _SAFE_ID.match(wid) else None
    if not w:
        raise ValueError("没有这个世界")
    with _LOCK:
        db = _read_db()
        db.get("worlds", {}).pop(wid, None)
        _write_db(db)
    for n in range(int(w.get("restarts") or 0) + 1):
        try:
            persona_rag.drop_line("%s#%d" % (wid, n))
        except Exception:                                   # noqa: BLE001
            log.exception("[novel] 删 RAG 线失败 %s#%d", wid, n)
    return {"ok": True, "id": wid,
            "name": (w.get("player") or {}).get("name") or "",
            "play_name": w.get("play_name") or ""}


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

    # 回退点：这一轮开始前的状态。只在整轮成功后随 `_save_world` 落盘，
    # 所以中途抛异常不会留下一个假的回退点。
    world["undo"] = _snapshot(world)
    _hist0 = len(world["history"])

    d = _director(world, said, opts)
    ruling, facts = _apply(world, d, said)
    reply = _writer(world, [ruling] + facts if ruling else facts, said, opts)
    world["history"].append({"role": "assistant", "content": reply})
    if isinstance(world.get("undo"), dict):
        world["undo"]["hist_added"] = len(world["history"]) - _hist0
    world["history"] = world["history"][-_HISTORY_MAX:]
    out = {"ok": True, "reply": reply, "ruling": ruling,
           "ending": world.get("ending"), "state": state_card(world)}
    if token:
        world["last_turn_token"] = token
        world["last_turn_result"] = dict(out)
    _save_world(world)
    _remember(world, said, reply)
    return out


def rollback(world_id, owner=""):
    """回退最近一轮，只给一次。

    只还原这一轮改过的状态、把 history/log 从尾巴削掉这一轮追加的条数，
    然后**清空回退点**——想再回退必须先往下写一轮，否则连着退会把整条历史
    拆碎、状态和正文对不上（用户 2026-11-12 明确要求「无法回退两次」）。

    顺带把这一轮写进 RAG 的那份记忆撤掉：`persona_rag.forget_last()` 就是
    1v1 为「第 5 轮刚入库又回退」做好的那支，不另写一套。
    """
    w = get_world(world_id, owner)
    if not w:
        raise ValueError("没有这个世界")
    snap = w.get("undo")
    if not isinstance(snap, dict) or not isinstance(snap.get("state"), dict):
        raise ValueError("这一轮已经回退过了，先往下写一轮才能再回退")

    said = ""
    for r in reversed(w.get("history") or []):
        if r.get("role") == "user":
            said = str(r.get("content") or "")
            break

    cur_name = _as_text((w.get("player") or {}).get("name"))
    for k, v in snap["state"].items():
        w[k] = copy.deepcopy(v)
    # 名字是玩家随时改的偏好，不是剧情状态：回退不该把改名也退回去。
    if cur_name and isinstance(w.get("player"), dict):
        w["player"]["name"] = cur_name
    back_h = int(snap.get("hist_added") or 0)
    if back_h:
        w["history"] = (w.get("history") or [])[:-back_h]
    back_l = int(snap.get("log_added") or 0)
    if back_l:
        w["log"] = (w.get("log") or [])[:-back_l]
    w["undo"] = None
    # 幂等令牌也得撤：不然网关超时后重发同一个 token，会把刚回退掉的那一轮
    # 结果原样吐回来。
    w.pop("last_turn_token", None)
    w.pop("last_turn_result", None)
    _save_world(w)

    line = _rag_line(w)
    try:
        persona_rag.forget_last(line)
    except Exception:                                       # noqa: BLE001
        log.exception("[novel] RAG 回退失败，世界书已回退")
    return {"ok": True, "said": said, "state": state_card(w)}


def restart(world_id, name=None, identity=None, owner=""):
    """死亡重开：人物归零，静态世界事实与死因留下，封样复用，不重跑 LLM。"""
    old = get_world(world_id, owner)
    if not old:
        raise ValueError("没有这个世界")
    preset = world_preset(old)
    if not preset:
        raise ValueError("玩法预设不见了")
    p = old.get("player") or {}
    name = _as_text(name) or p.get("name") or "无名的人"
    identity = _as_text(identity) or p.get("identity") or ""
    fresh = _blank_world(old["play"], name, identity,
                         _identity_desc(preset, identity), preset, owner)
    fresh["owner"] = str(old.get("owner") or "")
    # 自建玩法的世界观内嵌在这本里，重开必须一起带走，否则会掉回磁盘预设。
    fresh["preset"] = old.get("preset") if isinstance(old.get("preset"), dict) else {}
    fresh["play_name"] = old.get("play_name") or (preset.get("name") or "")
    here = (fresh.get("player") or {}).get("place")
    fresh["visited"] = [here] if here else []
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


def _open_events(world, here_only=True):
    """还没了结的事。挂在一个已经离开的地方、又没写后续的，不再算在眼前。

    老存档的事件没有 place 字段，一律照旧显示（代码不猜它发生在哪）。
    这里只做「这条事挂在哪」的事实比较，不问它该不该算完。
    """
    here = (world.get("player") or {}).get("place") or ""
    out = []
    for e in world.get("events") or []:
        if e.get("done"):
            continue
        at = e.get("place") or ""
        if here_only and at and here and at != here:
            continue
        out.append(e)
    return out


def state_card(world):
    """右栏用的纯事实状态卡。没有任何数值条、好感、跨天、服饰。"""
    p = world.get("player") or {}
    deaths = world.get("death_log") or []
    return {
        "id": world.get("id"),
        "play": world.get("play"),
        "play_key": play_key(world),
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
        "events": _open_events(world)[-6:],
        "log": list(world.get("log") or [])[-8:],
        "discovered": [d for d in world.get("discovered", [])
                       if d.get("kind") == "static"],
        "last_death": deaths[-1] if deaths else None,
        "actions": list(world.get("actions") or []),
        "can_undo": isinstance(world.get("undo"), dict),
        # 自建玩法的身份选项内嵌在这本里（磁盘玩法前端自己从玩法列表拿）。
        # 重开面板要用它，半封样（世界好了还没选身份）也要用它。
        "identity_choices": ((world.get("preset") or {}).get("identity_choices")
                             if isinstance(world.get("preset"), dict) else []) or [],
        "history": list(world.get("history") or [])[-_HISTORY_MAX:],
    }


def world_book(world):
    """世界书全貌，**只读**。给「查看世界」弹窗用。

    状态卡只给眼前的东西（在场的人、没了结的事、包里有什么），看不到这本
    世界到底是怎么设的。这里把规则、地点全表、人物名录摆出来。

    出去过的地方只按 `visited` 这个事实标，不猜；老存档没有这份记录的，
    就只有它现在站的地方算去过。
    """
    p = world.get("player") or {}
    preset = world_preset(world) or {}
    went = [str(x) for x in (world.get("visited") or []) if str(x)]
    here = p.get("place") or ""

    def hit(name, rows):
        """这条地点和某个记录是不是同一处：同名，或者记录更细。

        玩家站在「旧维修巷二楼」而地点表里写的是「旧维修巷」，这是真机里
        最常见的情况。这里只做字符串前缀比较（记录以地点名开头就算同一处），
        不做语义判断，也绝不把没去过的地点算成去过。
        """
        return any(r == name or r.startswith(name) for r in rows)

    places = []
    for row in world.get("places") or []:
        if isinstance(row, dict):
            nm = _as_text(row.get("name"))
            detail = _as_text(row.get("detail"))
            search = _as_text(row.get("search"))
        else:
            nm, detail, search = _as_text(row), "", ""
        if not nm:
            continue
        places.append({"name": nm, "detail": detail, "search": search,
                       "here": hit(nm, [here]) if here else False,
                       "visited": hit(nm, went)})
    cast = []
    for c in world.get("cast") or []:
        if not isinstance(c, dict) or not _as_text(c.get("name")):
            continue
        cast.append({"name": _as_text(c.get("name")),
                     "kind": _as_text(c.get("kind"), "人"),
                     "identity": _as_text(c.get("identity")),
                     "relation": _as_text(c.get("relation")),
                     "willing": _as_text(c.get("willing")),
                     "tier": _as_text(c.get("tier"), "路人甲"),
                     "present": bool(c.get("present"))})
    # 设定里的人（cast_seed）和「已经打过交道的人」是两回事，不能混成一份
    # 名录：开场就列出来会让玩家以为已经见过面，真机里已经因为这个踩过一次
    # （「遇到过的人里怎么有丧尸」）。这里分开摆，各自标清楚。
    seeded = []
    for c in world.get("cast_seed") or []:
        if not isinstance(c, dict) or not _as_text(c.get("name")):
            continue
        seeded.append({"name": _as_text(c.get("name")),
                       "identity": _as_text(c.get("identity")),
                       "relation": _as_text(c.get("relation")),
                       "willing": _as_text(c.get("willing")),
                       "tier": _as_text(c.get("tier"), "路人甲")})
    deaths = world.get("death_log") or []
    return {
        "id": world.get("id"),
        "play": world.get("play"),
        "name": preset.get("name") or world.get("play_name") or "",
        "tagline": _as_text(preset.get("tagline")),
        "brief": _as_text(preset.get("brief")),
        "custom": bool(isinstance(world.get("preset"), dict)
                       and world["preset"].get("name")),
        "rules": [str(x) for x in (world.get("rules") or [])],
        "chapter": world.get("chapter", 1),
        "chapter_goal": world.get("chapter_goal") or "",
        "clock": world.get("clock") or "",
        "turns": world.get("turns", 0),
        "restarts": world.get("restarts", 0),
        "person": {"name": p.get("name") or "",
                   "identity": p.get("identity") or "",
                   "identity_desc": p.get("identity_desc") or "",
                   "place": here,
                   "condition": p.get("condition") or "",
                   "items": _norm_items(p.get("items") or [])},
        "places": places,
        "cast": cast,
        "cast_seed": seeded,
        "path": list(world.get("visited") or []),
        "deaths": [{"cause": d.get("cause") or "", "at": d.get("at") or ""}
                   for d in deaths[-5:] if isinstance(d, dict)],
    }


# ─── 管理页（只读，只在 5174 开）──────────────────────

def all_worlds():
    """所有世界书，不分 owner，按「谁写的」分组给管理页看。

    只读：这里不做 owner 过滤，因为调它的是站主的管理页；反过来说，**这个
    函数绝不能直接挂在群友能打到的接口上**（5175 会通配反代 /api/novel/*，
    接口层还得再挡一次）。
    """
    out = []
    for wid, w in _read_db().get("worlds", {}).items():
        if not isinstance(w, dict):
            continue
        p = w.get("player") or {}
        hist = w.get("history") or []
        last = ""
        for row in reversed(hist):
            if isinstance(row, dict) and str(row.get("content") or "").strip():
                last = str(row.get("content")).strip().replace("\n", " ")
                break
        out.append({
            "id": wid,
            "owner": str(w.get("owner") or ""),
            "name": p.get("name") or "",
            "play_name": w.get("play_name") or w.get("play") or "",
            "custom": bool(w.get("preset")),
            "chapter": w.get("chapter", 1),
            "place": p.get("place") or "",
            "clock": w.get("clock") or "",
            "turns": w.get("turns", 0),
            "created": w.get("created") or 0,
            "ending": w.get("ending"),
            "lines": len(hist),
            "last": last[:140],
        })
    return sorted(out, key=lambda x: (x["owner"], -float(x["created"] or 0)))


def admin_world(world_id):
    """管理页读某一本，跨 owner。只读，不改任何东西；调用方限定站主。"""
    wid = str(world_id or "").strip()
    if not _SAFE_ID.match(wid):
        return None
    w = _read_db().get("worlds", {}).get(wid)
    return w if isinstance(w, dict) else None


def admin_story(world):
    """一本书的正文，按轮次摆好。

    历史里 assistant 的每一段就是一轮的正文，user 那一条是玩家当时写下的
    剧情骨架。管理页要能看出「他写了什么 → AI 写成了什么」，所以两半都留。
    开场那一条没有对应的 user，单独标出来。
    """
    out = []
    said = ""
    for row in world.get("history") or []:
        if not isinstance(row, dict):
            continue
        text = str(row.get("content") or "").strip()
        if not text:
            continue
        if row.get("role") == "user":
            said = text
            continue
        out.append({"said": said, "text": text})
        said = ""
    if said:
        out.append({"said": said, "text": ""})
    p = world.get("player") or {}
    return {
        "id": world.get("id"),
        "owner": str(world.get("owner") or ""),
        "name": p.get("name") or "",
        "identity": p.get("identity") or "",
        "play_name": world.get("play_name") or world.get("play") or "",
        "chapter": world.get("chapter", 1),
        "chapter_goal": world.get("chapter_goal") or "",
        "clock": world.get("clock") or "",
        "place": p.get("place") or "",
        "turns": world.get("turns", 0),
        "restarts": world.get("restarts", 0),
        "ending": world.get("ending"),
        "turns_rows": out,
    }
