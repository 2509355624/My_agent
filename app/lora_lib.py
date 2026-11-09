# -*- coding: utf-8 -*-
"""角色库：向量召回 + 角色库 AI 判定（2026-11-06 用户定的方案）。

## 它只回答一个问题

「用户这句话里，**点名**的是不是一个我们库里有的角色？」

- 点名了 → `{"action": "use", "key": "hutao"}` → 挂 LoRA + 追加外观 tag
- 没点名（「绿头发的偶像」「画个白毛」）→ `{"action": "skip"}` → 生图 AI 自己发挥

**skip 必须由大模型给**，代码不许猜。理由（用户原话）：靠关键词硬匹配，
「绿发偶像」会被永久吸到某一个角色上，模型的创造性就没了。

## 为什么不复用 persona 那条 RAG daemon

试过：`RagClient` + `ingest/search` 能建库，但 `search` 在
`bge-small-zh` 上抛 `TextEncodeInput must be Union[...]`（Chroma 的 query 路径
和这个模型对不上）。角色库只有几十条，向量在小模型上是 512 维——本地算一遍
余弦就够了，不值得为它再养一个 daemon。

所以这里：`LocalEmbeddings`（同一个 bge-small-zh，CPU）+ numpy 余弦，
索引落盘到 `state/lora_vec.json`，`config/char_loras.json` 变了才重建。
"""
import hashlib
import json
import logging
import os
import threading
import time

from app.config import BASE_DIR, PERSONA_MODEL, PERSONA_PROVIDER
from app.config import PERSONA_RAG_MODEL
from app import llm

log = logging.getLogger("lora_lib")

INDEX_JSON = os.path.join(BASE_DIR, "config", "char_loras.json")
VEC_PATH = os.path.join(BASE_DIR, "state", "lora_vec.json")
CACHE_PATH = os.path.join(BASE_DIR, "state", "lora_match_cache.json")
TOP_K = 8
JUDGE_TIMEOUT = 20
MIN_SCORE = 0.35          # 余弦下限，低于它的候选直接不送判定

_LOCK = threading.Lock()
_EMB = None
_KEYS = []                # 与 _MAT 行对应的 key
_MAT = None               # numpy 数组 (N, D)
_STAMP = ""


def entries():
    try:
        with open(INDEX_JSON, encoding="utf-8") as f:
            return (json.load(f).get("characters") or [])
    except Exception:                                  # noqa: BLE001
        log.warning("角色库索引读不了", exc_info=True)
        return []


def _stamp():
    try:
        return "%d-%d" % (len(entries()), int(os.path.getmtime(INDEX_JSON)))
    except Exception:                                  # noqa: BLE001
        return ""


def _doc(c):
    """一条角色 → 一段可检索的文本。名字最高权重，外观只取前 120 字。"""
    return ("角色：%s（%s，%s）\n别名：%s\n外观：%s"
            % (c.get("name") or "", c.get("key") or "",
               c.get("category") or "",
               " / ".join(str(x) for x in (c.get("aliases") or [])[:6]),
               str(c.get("identity") or "")[:120]))


def _emb():
    global _EMB
    if _EMB is None:
        from app.rag.local_embeddings import LocalEmbeddings
        _EMB = LocalEmbeddings(model_name=PERSONA_RAG_MODEL, device="cpu")
        log.info("角色库向量模型就绪：%s", PERSONA_RAG_MODEL)
    return _EMB


def _build():
    """重算全部向量并落盘。返回 (keys, matrix)。"""
    import numpy as np
    cs = entries()
    keys = [str(c.get("key")) for c in cs]
    vecs = _emb().embed_documents([_doc(c) for c in cs])
    mat = np.asarray(vecs, dtype="float32")
    with open(VEC_PATH, "w", encoding="utf-8", newline="\n") as f:
        json.dump({"stamp": _stamp(), "keys": keys,
                   "vecs": [[round(float(x), 5) for x in row] for row in vecs]},
                  f)
    log.info("角色库向量索引重建：%d 条", len(keys))
    return keys, mat


def _load():
    global _KEYS, _MAT, _STAMP
    import numpy as np
    st = _stamp()
    if _MAT is not None and _STAMP == st:
        return True
    if os.path.isfile(VEC_PATH):
        try:
            d = json.load(open(VEC_PATH, encoding="utf-8"))
            if d.get("stamp") == st and d.get("keys"):
                _KEYS = [str(k) for k in d["keys"]]
                _MAT = np.asarray(d["vecs"], dtype="float32")
                _STAMP = st
                return True
        except Exception:                              # noqa: BLE001
            log.warning("向量索引读不了，重建", exc_info=True)
    try:
        _KEYS, _MAT = _build()
        _STAMP = st
        return True
    except Exception:                                  # noqa: BLE001
        log.warning("角色库建索引失败", exc_info=True)
        return False


def _token_hit(c, text):
    """这条角色的名字/触发词/别名，有没有**原样**出现在 prompt 里。

    只用于**召回加权**（2026-11-06 修）：bge-small-zh 是中文模型，
    `robin_(honkai:_star_rail), honkai:_star_rail, ...` 这种 danbooru tag
    在它眼里和「罗宾」不沾边——实测 top-5 里根本没有 robin，最像的是
    rossi / hysilens / rin。于是候选里没有正确角色，大模型只能 skip，
    LoRA 没挂上，画出来就是模型自己想象的「知更鸟」。

    加权之后候选里一定包含它；**判定仍然交给大模型**，这里只负责别漏人。
    """
    low = text.lower()
    for k in (c.get("key"), c.get("trigger")):
        k = str(k or "").strip().lower()
        if k and len(k) >= 3 and k in low:
            return True
    for a in (c.get("aliases") or []):
        a = str(a or "").strip().lower()
        if a and len(a) >= 3 and a in low:
            return True
    return False


def search(text, k=TOP_K):
    """向量召回 + 名字原样命中的加权。返回 [{"key","name","score"}]。"""
    import numpy as np
    with _LOCK:
        if not _load():
            return []
        keys, mat = list(_KEYS), _MAT
    t = str(text or "").strip()
    if not t or mat is None or not len(keys):
        return []
    try:
        q = np.asarray(_emb().embed_query(t), dtype="float32")
    except Exception:                                  # noqa: BLE001
        log.warning("查询向量算失败", exc_info=True)
        return []
    q = q / (np.linalg.norm(q) + 1e-9)
    m = mat / (np.linalg.norm(mat, axis=1, keepdims=True) + 1e-9)
    sims = (m @ q).astype(float)

    by_key = {str(c.get("key")): c for c in entries()}
    BOOST = 0.45
    for i, key in enumerate(keys):
        c = by_key.get(key)
        if c and _token_hit(c, t):
            sims[i] = float(sims[i]) + BOOST

    order = np.argsort(-sims)[:max(1, int(k))]
    out = []
    for i in order:
        s = float(sims[i])
        if s < MIN_SCORE:
            continue
        key = keys[int(i)]
        out.append({"key": key,
                    "name": str((by_key.get(key) or {}).get("name") or ""),
                    "score": round(s, 4)})
    return out


# ── 候选块（2026-11-06 用户拍板）──────────────────────────────────────
# 中间那层「角色库 AI 判定」删掉了。检索结果直接当**参考资料**塞进大大怪
# 自己的上下文，由它一次调用同时做三件事：判渠道 / 决定用不用库里这个角色 /
# 写提示词。用不上就忽略，跟标签库资料一个待遇——**没有 skip 这个动作**。
#
# 为什么砍：多一次调用就多一个误判点，而且判定只能看见一张精简过的候选表
# （key/名字/别名），看不见身份 tag 的细节、也看不见用户这句话的语气。把
# 原始候选交给写提示词的那个模型，它自己就能对齐。
PREVIEW_K = 6              # 最多放几条
PREVIEW_IDENTITY = 120     # 每条 identity 截断
PREVIEW_LIMIT = 1500       # 整块上限（字符）
# 相关性门（2026-11-06 实测）：`search` 的 MIN_SCORE=0.35 太松——「帮我看看
# 这个 bug」top 0.438、「画个绿头发的偶像」0.397 都能召回 6 条，塞给模型只是
# 噪声。真点名（含名字原样命中的 0.45 加权）实测都在 0.57 以上：坎特蕾拉
# 1.095、千夏 0.576。取 0.45 当闸：低于它 = 这句跟角色库没关系，整块不拼。
PREVIEW_MIN = 0.45

# 每组只留一个**代表渠道**（2026-11-06 用户拍板）：用户只报角色名、没点名
# 渠道时，用它填 `skill`。分组的事实源在 app/char_loras.py 的 _LORA_SKILLS_*，
# 这里懒导入，避免和 char_loras.match() 里的懒导入互相套。
_REP_BY_GROUP = (
    ("_LORA_SKILLS_ANIMA", "silver"),
    ("_LORA_SKILLS_SDXL", "miao"),
    ("_LORA_SKILLS_SD15", "image_gen_v1"),
    ("_LORA_SKILLS_MOSS", "moss"),
)


def rep_channels(entry):
    """这条角色 LoRA 能挂的**代表渠道**（按 anima→SDXL→SD1.5→moss 排）。

    一个角色跨架构有多条记录时会给出多个；候选块里第一个就是「只报名字」
    时该用的那个。代码只把事实摆出来，选哪个交给模型（2026-11-06 用户原则：
    代码只检测，判断权留给 AI）。
    """
    from app import char_loras as _cl
    chans = {str(x) for x in (entry.get("channels") or [])}
    out = []
    for attr, rep in _REP_BY_GROUP:
        if chans.intersection(getattr(_cl, attr, ())):
            out.append(rep)
    return out


PREVIEW_HEAD = (
    "\n【角色库 · 本次检索到的角色】\n"
    "下面这些是按用户这句话在角色库里搜出来的（**可能无关，自己判断**）。\n"
    "每行的「渠道」是这条角色 LoRA 能挂的底模渠道（按 anima→SDXL→SD1.5→moss 排）。\n"
    "用户**只报了角色名、没点名渠道** → `skill` 填该行**第一个渠道**，\n"
    "`character` 同时填它的 key；用户点名了渠道就照他说的传，别用这里的。\n"
    "用户要的就是其中某个角色才填 `character`，否则留空，一切照常。\n\n"
)


def preview_block(text, k=PREVIEW_K):
    """检索结果拼成一段参考资料，供 `direct_gen` 塞进提示词。空串 = 没候选。

    内部就用 `search()`（向量 + 名字原样命中加权），不调大模型：整条路
    只在本地跑，**不花 token**。返回的文本进的是**大大怪看到的上下文**，
    不是最终生图提示词——最终 prompt 仍由它自己写。
    """
    t = str(text or "").strip()
    if not t:
        return ""
    try:
        hits = search(t, k)
    except Exception:                                  # noqa: BLE001
        log.warning("角色库检索异常，候选块留空", exc_info=True)
        return ""
    if not hits:
        return ""
    # 最高分不过闸 = 这句跟角色库无关（见 PREVIEW_MIN 注释），整块不拼。
    if float(hits[0].get("score") or 0.0) < PREVIEW_MIN:
        return ""
    # 过了闸就只留同样过闸的那几条，别把 0.36 的凑数项带进去。
    hits = [h for h in hits if float(h.get("score") or 0.0) >= PREVIEW_MIN]
    # 同一个 key 可能有多条（跨架构各一份文件），按 key 聚合起来，渠道合并
    # 到一行里——同 key 拆两行的话，模型填 `character` 时两个 key 一模一样，
    # 反而分不清该用哪条。
    by_key = {}
    for c in entries():
        by_key.setdefault(str(c.get("key")), []).append(c)
    lines = []
    seen = set()
    for h in hits:
        k = str(h.get("key"))
        if k in seen:
            continue
        seen.add(k)
        items = by_key.get(k) or []
        c = items[0] if items else {}
        name = str(c.get("name") or h.get("name") or "").strip()
        trig = str(c.get("trigger") or "").strip()
        ident = str(c.get("identity") or "").strip()[:PREVIEW_IDENTITY]
        reps = []
        for it in items:
            for r in rep_channels(it):
                if r not in reps:
                    reps.append(r)
        head = "- %s｜%s｜渠道: %s" % (k, name or "-",
                                      " / ".join(reps) or "-")
        if trig:
            head += "｜trigger: %s" % trig
        lines.append(head)
        if ident:
            lines.append("  ｜%s" % ident)
    block = PREVIEW_HEAD + "\n".join(lines) + "\n"
    if len(block) > PREVIEW_LIMIT:
        block = block[:PREVIEW_LIMIT].rstrip() + "…\n"
    return block


ANIMA_CHANNELS = ["anima_clear", "anima-line", "anima-2s", "anima-real",
                  "hd_3_clear", "hd_3_curvy", "hd_3_gloss", "hd_3_soft",
                  "silver", "silver-hd"]
# 用户 2026-11-06 拍板：
#   · 没提渠道            → **silver**（anima 的默认档）
#   · 说了「anima」       → **anima_clear**
#   · 说了「线条 / line」 → anima-line（另一种画风，不是默认）
DEFAULT_CHANNEL = "silver"

JUDGE_SYS = """你在做一件很小的事：判断用户这句话里**有没有点名**一个角色，
以及他**有没有提到渠道**。

只输出一个 JSON 对象，不要解释。

命中要件（三条同时成立）：
1. 用户说了角色的名字（中文名、英文名、或明显的同义写法都算）；
   **英文 danbooru 形式的 tag 也算**——`robin_(honkai:_star_rail)`、
   `hutao_(genshin_impact)` 这种就是在点名角色，括号里是出处。
2. 这个名字对应下面候选里的某一个；
3. 用户是在**要求画这个角色**，不是在聊别的。

不命中的情况（一律 skip）：
- 用户只描述了外观：「绿头发的偶像」「白毛双马尾」「穿红衣服的女孩」；
- 用户说的是原创角色、或者候选里没有的人；
- 用户只是提到、讨论、或者问「你认识胡桃吗」这类，没让画。

渠道（channel）：用户可能把渠道名放在前面、后面、中间，也可能用简称
（「线条」「Anima」「高清」「silver」）。你自己读原文判断他指的是哪个，
**不要按固定位置切词**。

⚠️ 只认**下面列表里**的渠道。用户如果提到的是别的渠道（krea2 / qwen /
moss / nai / 漫画 …），说明他这次不想用角色库这几个档——
**channel 一律留空，不要替他改渠道**。把「用户点了 krea2」理解成
「改用 silver」是最严重的错误：那会把用户选的渠道直接吞掉。

只有当用户**明确点名**了列表里的渠道时才填它；没提、或提的是别的渠道，
都填空串。

输出格式：
  {"action": "use", "key": "候选里的 key", "channel": "渠道名或空串", "why": "一句话"}
  {"action": "skip", "channel": "", "why": "一句话"}

宁可 skip，也不要猜。猜错会让以后所有类似请求都被锁死成同一个角色。"""

JUDGE_TMPL = "候选：\n%s\n\n用户原话：%s"


def _norm_channel(word):
    """AI 说的渠道 → 我们真实存在的渠道名；认不出就空串（= 没提）。"""
    w = str(word or "").strip().lower().replace(" ", "")
    if not w:
        return ""
    if w in ANIMA_CHANNELS:
        return w
    alias = {"2s": "anima-2s", "anima2s": "anima-2s",
             "anima-2s": "anima-2s",
             "线条": "anima-line", "line": "anima-line",
             "anima": "anima_clear", "默认": "anima_clear",
             "clear": "anima_clear", "anima线条": "anima-line",
             "real": "anima-real", "写实": "anima-real",
             "高清": "hd_3_clear", "curvy": "hd_3_curvy",
             "gloss": "hd_3_gloss",
             "soft": "hd_3_soft", "柔": "hd_3_soft",
             "silverhd": "silver-hd", "高清银": "silver-hd",
             "银": "silver", "server": "silver"}
    return alias.get(w, "")


def _judge(text, hits):
    by_key = {c.get("key"): c for c in entries()}
    lines = []
    for h in hits:
        c = by_key.get(h["key"]) or {}
        lines.append("- key=%s | %s（%s） | 别名：%s"
                     % (h["key"], c.get("name") or h["name"],
                        c.get("category") or "",
                        "/".join(str(x) for x in (c.get("aliases") or [])[:5])))
    # 库里只有这几个渠道，一起告诉它，免得它编出一个不存在的
    ch_line = "可用渠道：%s\n" % " / ".join(ANIMA_CHANNELS)
    msgs = [{"role": "system", "content": JUDGE_SYS},
            {"role": "user",
             "content": ch_line + JUDGE_TMPL % ("\n".join(lines), text)}]
    try:
        raw = llm.call_llm(msgs, timeout=JUDGE_TIMEOUT,
                           provider=PERSONA_PROVIDER, model=PERSONA_MODEL)
    except Exception:                                  # noqa: BLE001
        log.warning("角色库判定调用失败，按 skip 处理", exc_info=True)
        return {"action": "skip", "channel": "", "why": "判定失败"}
    import re
    m = re.search(r"\{.*\}", str(raw or ""), re.S)
    if not m:
        return {"action": "skip", "channel": "", "why": "没交 JSON"}
    try:
        d = json.loads(m.group(0))
    except Exception:                                  # noqa: BLE001
        return {"action": "skip", "channel": "", "why": "JSON 解析失败"}
    chan = _norm_channel(d.get("channel"))
    if str(d.get("action") or "").lower() != "use":
        return {"action": "skip", "channel": chan,
                "why": str(d.get("why") or "")[:60]}
    key = str(d.get("key") or "")
    if key not in {h["key"] for h in hits}:
        return {"action": "skip", "channel": chan, "why": "key 不在候选里"}
    return {"action": "use", "key": key, "channel": chan,
            "why": str(d.get("why") or "")[:60]}


def _cache_load():
    try:
        with open(CACHE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:                                  # noqa: BLE001
        return {}


def _cache_save(d):
    try:
        os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
        with open(CACHE_PATH, "w", encoding="utf-8", newline="\n") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
    except Exception:                                  # noqa: BLE001
        pass


def decide(text, current_channel=""):
    """用户原话 → {"entry": 角色或 None, "channel": 渠道或空串, "why": ...}。

    渠道规则（2026-11-06 用户拍板）：**由 AI 自己读原文判断**，不按位置切词
    （「Anima 千夏」和「千夏用 anima 跑」都要认出来）。没提渠道就回空串，
    调用方自己决定用默认值还是沿用上下文。
    """
    t = str(text or "").strip()
    if not t:
        return {"entry": None, "channel": "", "why": "空"}
    ck = hashlib.sha256((t.lower() + "|" + str(current_channel))
                        .encode("utf-8")).hexdigest()[:16]
    cache = _cache_load()
    hit = cache.get(ck)
    if hit is not None:
        entry = None
        if hit.get("action") == "use":
            entry = next((c for c in entries()
                          if str(c.get("key")) == hit.get("key")), None)
        return {"entry": entry, "channel": hit.get("channel") or "",
                "why": hit.get("why") or ""}

    cands = search(t)
    if not cands:
        return {"entry": None, "channel": "", "why": "没有候选"}
    d = _judge(t, cands)
    cache[ck] = {"text": t[:120], "action": d["action"],
                 "key": d.get("key") or "", "channel": d.get("channel") or "",
                 "why": d.get("why") or "", "ts": int(time.time())}
    _cache_save(cache)
    log.info("[lora] %s → %s %s 渠道=%s", t[:30], d["action"],
             d.get("key") or "", d.get("channel") or "-")
    entry = None
    if d["action"] == "use":
        entry = next((c for c in entries() if str(c.get("key")) == d["key"]),
                     None)
    return {"entry": entry, "channel": d.get("channel") or "",
            "why": d.get("why") or ""}


def resolve(text, current_channel=""):
    """兼容旧调用：只要角色条目。"""
    return decide(text, current_channel).get("entry")
