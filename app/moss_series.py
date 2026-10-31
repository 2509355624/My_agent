"""moss 小漫画 · 系列档案（连载的账本）。

## 为什么要有这一层

`moss_bot` 原来是「一句话进、一张图出」，编剧只看得见当次那句需求，
上一页演到哪、椅子摆在哪、人物长什么样，全都不知道。连载需要把这些
记住，而且不能塞 `spec.json`（几页之后英文长句会盖掉中文规范，
模型开始模仿那套腔调，输出就散了）——所以这里只存**中文摘要**。

一页存两份，各管一件事，**不能合并**：
  · `summary`  50~80 字，「这一页演了什么」→ 管下一话别割裂
  · `scene`    200~300 字，「这一页长什么样」→ 管画面别崩
    （地点、家具在左右哪侧、光源、人物姿态、末格定格）

前者给全部页，后者**只给上一页**。跨两三页场景本来就要变，
再往前给反而互相打架——用户 2026-10-31 拍的就是这条。

## 为什么是树，不是数组

群聊里允许任何人从任意一页重开（多人一起玩，不该拦），这会分叉：

    p01 → p02 → p03a → p04a
              ↘ p03b → p04b

私聊不允许分叉，回头就截断（后页标 `dead`，图留盘）。
**两种策略共用同一份数据**，差别只在「回头时删不删」，见 `truncate`。

## 系列是全局的，不是挂在会话上（2026-10-31 用户改的口径）

一开始这里是「一个会话一个系列」，用户否了：他要的是**一份数据、一对 N**——
群里玩到第 5 页，把那一页的回执分享出去，私聊里接着画；或者干脆在群里
把编号给朋友，让对方在他的群里继续。所以：

    state/moss_series/S7K3/series.json     剧情本体，只有一份
    state/moss_series/_index.json          各会话「当前在用哪个系列」

`S7K3` 是 4 位短码（去掉了 0/O/1/I 这些看错的），谁拿到都能续。
**安全边界**是「编号不外传」：群里所有人都看得到回执，所以群里开出来的
系列对全群可见；私聊里开出来的，只有拿到编号的人能接。用户认这个边界。

## 最新的那一页 = 下一个续集接的地方

`latest()` 取 `pages` 里**最后一条还没 dead 的**，不是取 n 最大的。
群聊里 A 从 5 跑到 6a/7a、B 从 5 跑到 6b，latest 是 6b——
谁刚跑完就接谁，这是用户认可的解法（「不断接最新的就行」）。
`active_chain()` 顺着 parent 回到根，就是 `moss合集` 要的那条线。

## 重画不是新页

换个种子重画同一页，`n` 不变、`parent` 不变，旧版本推进 `history`。
所以重画**不会**污染剧情链。但调用方要记得重算这一页的 `scene`：
画面换了场景卡还是旧的，下一页就会按一个不存在的画面往下接。

## 外部编号

对外只暴露 `S7K3-P05`（系列 + 页号），内部页 id 是 `p05a` 这种——
分支字母不进外部编号，因为引用靠 `message_id` 精确命中，编号只是
转发/截图之后认不出来的那条兜底路（见 `moss_msgs`）。
"""

import json
import logging
import os
import random
import re
import threading
import time

from .config import BASE_DIR

log = logging.getLogger("moss_series")

STATE_ROOT = os.path.join(BASE_DIR, "state", "moss_series")
INDEX_FILE = os.path.join(STATE_ROOT, "_index.json")

# 会看错的字符一律不进编号：0/O、1/I、5/S 里只留一个。
_ID_CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_ID_LEN = 4
_ID_RE = re.compile(r"\b([A-Z0-9]{%d})-[Pp](\d{1,3})\b" % _ID_LEN)

# 读改写必须整段在锁里：群聊里两张图前后脚跑完，都在回写同一个文件，
# 各自 load→改→save 会把对方的页吞掉。
_LOCK = threading.RLock()


# ─── 编号 ─────────────────────────────────────────────

def new_id():
    """生成一个没被占用的系列编号。"""
    for _ in range(50):
        sid = "".join(random.choice(_ID_CHARS) for _ in range(_ID_LEN))
        if not os.path.isdir(os.path.join(STATE_ROOT, sid)):
            return sid
    raise RuntimeError("系列编号生成失败，state/moss_series 下占位太多")


def parse_code(text):
    """从一段文字里抠 `S7K3-P05`。返回 `(系列号, 页号)`，抠不到返回 `("", 0)`。

    转发、截图之后 `message_id` 变了，账本对不上——回执卡上印的这串编号
    就是那条路上的唯一凭据（用户说的「最重要是那个编号」）。
    """
    m = _ID_RE.search(str(text or "").upper())
    if not m:
        return "", 0
    return m.group(1), int(m.group(2))


def find_code(text):
    """从一段文字里抠出编号**原文**（原样返回，用来在报错里回显给用户）。

    跟 `parse_code` 分开是因为失败时要说人话：「没找到 `S7K3-P05` 这条线」
    比「认不出来」有用得多——用户第一反应是自己打错了字，
    把他打的那串原样贴回去，他一眼就能看出错在哪。
    """
    m = _ID_RE.search(str(text or "").upper())
    return m.group(0) if m else ""


def code_of(series_id, n):
    return "%s-P%02d" % (series_id, int(n))


def strip_code(text):
    """把 `S7K3-P05` 从一段文字里抹掉（用户手打编号时它混在剧情里）。

    编号是定位信息，不能变成剧情的一部分——不然编剧会收到
    「S7K3-P05 她推门进来」这种句子，画出个牌子来。
    """
    out = _ID_RE.sub(" ", str(text or ""))
    return re.sub(r"\s{2,}", " ", out).strip(" ，,。.:：、")


# ─── 落盘 ─────────────────────────────────────────────

def _dir(series_id):
    return os.path.join(STATE_ROOT, series_id)


def _path(series_id):
    return os.path.join(_dir(series_id), "series.json")


def _blank(series_id):
    return {"id": series_id, "title": "", "style": "", "cast": {},
            "pages": [], "created": time.strftime("%Y-%m-%d %H:%M:%S"),
            "updated": ""}


def _load_raw(series_id):
    p = _path(series_id)
    if not os.path.isfile(p):
        return None
    try:
        with open(p, encoding="utf-8") as fh:
            s = json.load(fh)
    except Exception:                           # noqa: BLE001
        log.exception("系列档案读坏了，按空档处理：%s", p)
        return None
    if not isinstance(s, dict) or not isinstance(s.get("pages"), list):
        log.warning("系列档案结构不对，忽略：%s", p)
        return None
    s.setdefault("id", series_id)
    s.setdefault("cast", {})
    s.setdefault("style", "")
    s.setdefault("title", "")
    for pg in s["pages"]:
        pg.setdefault("history", [])
        pg.setdefault("dead", False)
    return s


def _save_raw(s):
    """整份覆盖写。先写 .tmp 再 replace：进程被杀不会留半截 JSON。"""
    d = _dir(s["id"])
    os.makedirs(d, exist_ok=True)
    s["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
    tmp = _path(s["id"]) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(s, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, _path(s["id"]))


# ─── 会话索引 ─────────────────────────────────────────

def _sess(target, target_id):
    """会话 → 索引键。只留数字/字母，挡住路径分隔符。"""
    tid = "".join(ch for ch in str(target_id or "") if ch.isalnum())
    if not tid:
        raise ValueError("会话 id 不合法：%r" % (target_id,))
    return ("g" if target == "group" else "p") + tid


def _load_index():
    try:
        with open(INDEX_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:                           # noqa: BLE001
        return {}


def _save_index(idx):
    os.makedirs(STATE_ROOT, exist_ok=True)
    tmp = INDEX_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(idx, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, INDEX_FILE)


def bind(target, target_id, series_id):
    """把某个会话的「当前系列」指过去。

    跨群/跨私聊共用同一份剧情靠的就是这一步：群友在私聊里引用一张群里的
    回执，代码把他这个会话的指针也指到同一个系列，之后他自己说「续集」
    就接上了。
    """
    key = _sess(target, target_id)
    with _LOCK:
        idx = _load_index()
        idx[key] = {"series": series_id,
                    "ts": time.strftime("%Y-%m-%d %H:%M:%S")}
        _save_index(idx)
    return series_id


def current(target, target_id):
    """这个会话当前在玩哪个系列。没有返回 ""。"""
    key = _sess(target, target_id)
    with _LOCK:
        rec = _load_index().get(key) or {}
    sid = str(rec.get("series") or "")
    return sid if sid and _load_raw(sid) else ""


# ─── 对外接口 ─────────────────────────────────────────

def load(series_id):
    with _LOCK:
        return _load_raw(str(series_id or ""))


def mutate(series_id, fn, create=False):
    """在锁里 load → `fn(series)` → save，返回 `fn` 的返回值。

    调用方只拿返回值判断结果，不要自己再 load 一遍去改——那又回到
    「读改写裸奔」了。`fn` 返回 None 时本函数返回 series 本身。

    `create=False` 且档案不存在时返回 None（`fn` 不会被调用）。
    """
    sid = str(series_id or "")
    if not sid:
        return None
    with _LOCK:
        s = _load_raw(sid)
        if s is None:
            if not create:
                return None
            s = _blank(sid)
        out = fn(s)
        _save_raw(s)
        return s if out is None else out


def open_new(target, target_id, title="", style="", cast=None):
    """开一个新系列并把这个会话绑过去。返回 series dict。"""
    sid = new_id()
    series = _blank(sid)
    series["title"] = title or ""
    series["style"] = style or ""
    series["cast"] = dict(cast or {})
    with _LOCK:
        _save_raw(series)
    bind(target, target_id, sid)
    log.info("moss 新系列 %s（%s %s）", sid, target, target_id)
    return series


# ─── 页查询 ───────────────────────────────────────────

def alive(series):
    """没被截断的页，按创建顺序（= `pages` 数组顺序）。"""
    return [p for p in series.get("pages") or [] if not p.get("dead")]


def latest(series):
    """下一个续集该接的那一页 = 最后一条还活着的页。

    取「创建顺序的最后一条」而不是「n 最大的」：群聊里 B 从第 5 页重开
    跑出 6b，6b 在数组末尾，但它和 6a 同号。按 n 取会拿回 6a，接错分支。
    """
    ok = alive(series)
    return ok[-1] if ok else None


def by_id(series, page_id):
    for p in series.get("pages") or []:
        if p.get("id") == page_id:
            return p
    return None


def by_n(series, n, include_dead=False):
    """第 n 页。同号有多个分支时取**最新创建的**那个。"""
    hit = [p for p in (series.get("pages") or [])
           if p.get("n") == int(n) and (include_dead or not p.get("dead"))]
    return hit[-1] if hit else None


def siblings(series, n):
    """第 n 页有几个分支（用来在回执里标 A/B，只在 >1 时提示）。"""
    return [p for p in (series.get("pages") or [])
            if p.get("n") == int(n) and not p.get("dead")]


def chain(series, page_id):
    """从某一页顺着 parent 回到根，正序返回（根在前）。

    遇到断链（parent 指向不存在的页）就停在那儿，不抛——档案被手改过
    也只该少几张，不该整条线都发不出来。
    """
    out, seen, pid = [], set(), page_id
    while pid and pid not in seen:
        seen.add(pid)
        pg = by_id(series, pid)
        if pg is None:
            break
        out.append(pg)
        pid = pg.get("parent") or ""
    out.reverse()
    return out


def active_chain(series):
    """当前活跃那条线（最新页所在的链）——`moss合集` 就发这一条。"""
    last = latest(series)
    return chain(series, last["id"]) if last else []


def page_of_task(series, task):
    """按任务目录名找页。重画/补摘要要用。"""
    for p in series.get("pages") or []:
        if p.get("task") == task:
            return p
    return None


# ─── 页写入 ───────────────────────────────────────────

def _next_id(series, n):
    """同号多分支的页 id：p06a / p06b / p06c…

    id 唯一是硬要求——群聊里同号分支是常态，用 n 当钥匙必然串。
    """
    used = {p.get("id") for p in series.get("pages") or [] if p.get("n") == n}
    for ch in "abcdefghijklmnopqrstuvwxyz":
        pid = "p%02d%s" % (n, ch)
        if pid not in used:
            return pid
    i = 2
    while True:                                 # 一页 27 个分支，够荒诞了
        pid = "p%02d_%d" % (n, i)
        if pid not in used:
            return pid
        i += 1


def add_page(series, *, parent_id, task, brief, seed=None,
             summary="", scene="", title=""):
    """追一页。返回新页节点。

    `parent_id` 为空 = 系列第一页。`n` 由 parent 推：parent 的 n + 1，
    没有 parent 就是 1。私聊截断后从第 5 页重开，新页照样是 6。
    """
    parent = by_id(series, parent_id) if parent_id else None
    n = (parent["n"] + 1) if parent else 1
    pg = {
        "id": _next_id(series, n),
        "n": n,
        "parent": parent_id or "",
        "task": task,
        "brief": brief,
        "seed": seed,
        "summary": summary,
        "scene": scene,
        "dead": False,
        "history": [],
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    series.setdefault("pages", []).append(pg)
    if title and not series.get("title"):
        series["title"] = title
    return pg


def fill(series, page_id, **kw):
    """回填一页的字段（摘要、场景卡、seed…）。只认已存在的键。"""
    pg = by_id(series, page_id)
    if pg is None:
        return None
    for k, v in kw.items():
        if k in ("id", "n", "parent", "history"):
            continue
        pg[k] = v
    return pg


def truncate(series, page_id):
    """私聊回头：把这一页**之后**的页标 dead，返回被杀掉的那批。

    「之后」按 n 判，不按链判——私聊不允许分支，n 一定单调递增，
    所以 n > 本页 n 的就是要作废的。图不删，只标 dead，
    用户回头想捞还捞得回来（`moss合集` 不会带它们）。
    """
    pg = by_id(series, page_id)
    if pg is None:
        return []
    killed = []
    for p in series.get("pages") or []:
        if p is pg or p.get("dead"):
            continue
        if p.get("n", 0) > pg.get("n", 0):
            p["dead"] = True
            killed.append(p)
    return killed


def bump(series, page_id, *, task, seed):
    """重画：换一张图顶替同一页，旧版本推进 history。

    **不新建页**——重画不推进剧情，只换画面。旧数据和它的 seed 都留着，
    因为重画经常画得比原来还差，得能说「还是原来那张好」。

    返回 (页节点, 旧版本 dict)。
    """
    pg = by_id(series, page_id)
    if pg is None:
        return None, None
    old = {"task": pg.get("task") or "", "seed": pg.get("seed"),
           "scene": pg.get("scene") or "", "ts": pg.get("ts") or ""}
    pg.setdefault("history", []).append(old)
    pg["task"] = task
    pg["seed"] = seed
    pg["ts"] = time.strftime("%Y-%m-%d %H:%M:%S")
    return pg, old


def restore(series, page_id):
    """回退到重画前的版本（history 最后一个）。返回是否成功。"""
    pg = by_id(series, page_id)
    if pg is None or not pg.get("history"):
        return False
    old = pg["history"].pop()
    pg["task"] = old.get("task") or pg.get("task")
    pg["seed"] = old.get("seed")
    pg["scene"] = old.get("scene") or pg.get("scene") or ""
    return True


# ─── 给编剧用的前情 ───────────────────────────────────

def prev_lines(series, upto_id):
    """前情摘要：从根到 `upto_id` 这条链上每页一行。

    只给 50~80 字的 summary，**不给 scene**——场景卡只给上一页那一份。
    50 页 × 60 字 = 3000 字，DeepSeek 毫无压力，也不用搞二级压缩
    （用户 2026-10-31 拍板：先做简单的那版）。
    """
    out = []
    for pg in chain(series, upto_id):
        s = str(pg.get("summary") or "").strip()
        if s:
            out.append("第 %02d 页：%s" % (pg.get("n") or 0, s))
    return "\n".join(out)


def prev_scene(series, upto_id):
    """上一页场景卡，全文。这是「画面不崩」的那一半。"""
    pg = by_id(series, upto_id)
    return str((pg or {}).get("scene") or "").strip()
