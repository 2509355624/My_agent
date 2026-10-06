"""人工二审队列：审核 AI 拦下的图留一份，管理员点「过审」才发回去。

## 它解决什么

审核是**最严白名单 + 拿不准就拦**（见 image_audit），误拦是这套口径的设计
代价。2026-10-06 用户报：不少图其实只是擦边、根本没沾违规的边，但拦下之后
**没有任何补救通道**——图已经画好了、字节就在手上，却只能回一句「没过审」，
连管理员都没看过一眼就作废了。

## 做法

审核闸门 `image_audit.allow_send` 判 False 的那一刻，把那份**真正要发出去的
字节**（`image_out.prepare_for_send` 的产物）从系统 temp 拷一份进
`state/review/`，连同会话、编号、caption、审核理由落一行
`state/image_review.jsonl`。管理页把它列出来，管理员看着图点「过审」→ 走
**正常发图那条路** `qq_api.send_image`，caption 一字不改，跟没被拦过一模一样。

## 关键取舍

- **只挂一个点**：三个发图口（worker 出图 / NAI / qq_bot 回发）共用
  `allow_send`，在它里头落队就够了，不用三处都改一遍。
- **图必须拷出 temp**：`image_out` 的产物在系统 temp、24 小时一清（`KEEP_
  SECONDS`）。二审是「回头再看」的活，图不能跟着没了。
- **追加写、后写的行盖先写的**（与 image_log 同款）：qq_bot 进程写、网页进程
  读+改，两个进程同时改一个 JSON 文件必然丢数据；JSONL 追加只在崩的那一行丢。
- **状态只有三种**：pending / approved / rejected。改状态也是追加一行（同一
  个 id），读的时候「后写的赢」。
- **过审后补记账本**：账本只在**真发出去**之后记（见 image_jobs），被拦的图
  没记。过审发出去了就该补上，否则「引用这张图问提示词」查不到。
- **失败一律不抛**：这是补救通道，它自己炸了不该把审核闸门也带下去——
  `record()` 写不进去只记一条 warning，图照旧按拦截处理。
- **瘦身先丢已处理的**：待审记录是「还没人看过的」，老的记录被随机口令重抽
  刷爆时也不该先把待审的挤掉。
"""

import json
import logging
import os
import shutil
import threading
import time

from app.config import state_path

log = logging.getLogger("image_review")

# 队列账本：一行一条。测试进程自动落到临时目录（见 config.state_path）。
PATH = state_path("image_review.jsonl")

# 留图的目录（**不是系统 temp**，见上面「图必须拷出 temp」）。
REVIEW_DIR = state_path("review")

_lock = threading.Lock()

# 队列里最多留多少条。超了先丢已处理的、再动最老的待审。
MAX_ITEMS = 200

# 记录保留多久（秒）。7 天：二审是「回头再看」，隔周再翻的基本没意义了。
KEEP_SECONDS = 7 * 86400

PENDING = "pending"
APPROVED = "approved"
REJECTED = "rejected"
STATES = (PENDING, APPROVED, REJECTED)


def new_id(ts=None):
    """生成一条队列记录的 id，形如 `RV-20261006-144800-123`。

    ⚠️ 前缀必须是 `RV`：**不能**跟生图编号 `HT` 撞——账本的正则 `image_log.
    TAG_RE` 只认 `HT-`，撞了以后「引用这条问提示词」会查到错的东西。另外
    沿用 image_log 那条约束：以字母开头、不含 `_` 和 `*`（Markdown 降级会吃）。
    """
    t = time.time() if ts is None else float(ts)
    st = time.localtime(t)
    ms = int(round((t - int(t)) * 1000)) % 1000
    return "RV-%s-%s-%03d" % (time.strftime("%Y%m%d", st),
                              time.strftime("%H%M%S", st), ms)


def _keep_file(path, rid):
    """把图拷进 state/review/。拷不动就退回原路径——留不住图也得留住记录。"""
    try:
        os.makedirs(REVIEW_DIR, exist_ok=True)
        ext = os.path.splitext(str(path))[1] or ".jpg"
        dst = os.path.join(REVIEW_DIR, rid + ext)
        shutil.copyfile(path, dst)
        return dst
    except Exception as exc:
        log.warning("二审留图失败 %s：%s", path, exc)
        return path


def _rows():
    """读出全部行（坏行跳过）。"""
    out = []
    try:
        if not os.path.exists(PATH):
            return out
        with open(PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue        # 坏行跳过，别让一行坏了整本
                if isinstance(row, dict) and row.get("id"):
                    out.append(row)
    except Exception as exc:
        log.warning("二审队列读不出来 %s：%s", PATH, exc)
    return out


def _index():
    """id → 行，**后写的赢**（改状态靠追加同 id 的新行）。"""
    out = {}
    for row in _rows():
        out[row["id"]] = row
    return out


def _append(row):
    """追加一行。调用方持锁。"""
    d = os.path.dirname(PATH)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _drop_file(path):
    """只删**本目录里**的图。拷图失败退回的 temp 路径不是我们的，别动。"""
    if not path:
        return
    try:
        if (os.path.dirname(os.path.abspath(path))
                == os.path.abspath(REVIEW_DIR)):
            os.remove(path)
    except OSError:
        pass


def _sweep():
    """瘦身：超龄的 + 超量的先丢已处理的，最后才动最老的待审。调用方持锁。"""
    rows = _rows()
    idx = {}
    for r in rows:
        idx[r["id"]] = r
    alive = sorted(idx.values(), key=lambda r: r.get("ts") or 0)
    if not alive:
        return

    cutoff = time.time() - KEEP_SECONDS
    doomed = [r for r in alive if (r.get("ts") or 0) < cutoff]
    rest = [r for r in alive if (r.get("ts") or 0) >= cutoff]
    if len(rest) > MAX_ITEMS:
        # 先丢已处理的（approved/rejected），不够再动最老的待审。
        decided = [r for r in rest if r.get("state") != PENDING]
        pending = [r for r in rest if r.get("state") == PENDING]
        over = len(rest) - MAX_ITEMS
        doomed += (decided + pending)[:over]
        doomed_ids = {r["id"] for r in doomed}
        rest = [r for r in rest if r["id"] not in doomed_ids]
    if not doomed:
        return

    doomed_ids = {r["id"] for r in doomed}
    for r in doomed:
        _drop_file(r.get("path"))
    try:
        with open(PATH, "w", encoding="utf-8") as f:
            for r in rows:
                if r.get("id") not in doomed_ids:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
    except Exception as exc:
        log.warning("二审队列瘦身失败：%s", exc)


def record(path, target, target_id, agent_id="", verdict=None, meta=None,
           ts=None):
    """审核拦下一张图 → 落队。返回这条的 id；落不进去返回 ""。

    verdict 传 `image_audit.Verdict`（拿 reason / category / failed），没有就
    不传；meta 是发图口那边顺手带过来的上下文（编号、渠道、种子、caption、
    提示词、ComfyUI 原图名），缺哪一项就留空，记录照样成立。
    """
    meta = meta or {}
    now = time.time() if ts is None else float(ts)
    rid = new_id(now)
    # 拷图失败**不放弃这条记录**：图留在系统 temp 里（24 小时一清），管理员
    # 当天之内还是能看、能点过审。留不住图就不记，等于把这次误拦彻底作废。
    try:
        kept = _keep_file(path, rid)
    except Exception as exc:
        log.warning("二审留图失败，记录照记（图可能已过期）：%s", exc)
        kept = path
    try:
        # 组装 + 落盘整段包住：补救通道炸了不该把审核闸门带下去。
        row = {
            "id": rid,
            "ts": now,
            "agent": agent_id or "",
            "target": target or "",
            "target_id": str(target_id or ""),
            "path": kept,
            "reason": getattr(verdict, "reason", "") or "",
            "category": getattr(verdict, "category", "") or "other",
            "failed": bool(getattr(verdict, "failed", False)),
            "tag": meta.get("tag") or "",
            "skill": meta.get("skill") or "",
            "seed": meta.get("seed"),
            "caption": meta.get("caption") or "",
            "prompt": meta.get("prompt") or "",
            "src_file": meta.get("file") or "",
            "state": PENDING,
            "decided_ts": 0,
        }
        with _lock:
            _append(row)
            _sweep()
    except Exception as exc:
        log.warning("二审队列写不进去：%s", exc)
        return ""
    log.info("图被审核拦下，已进人工二审队列 %s（%s %s，%s）",
             rid, target, target_id, row["category"])
    return rid


def items(state=None, limit=100):
    """列出队列，**最新在前**。state 传 None = 全部。

    管理页默认只看 pending；要看历史就传 approved / rejected。
    """
    rows = sorted(_index().values(),
                  key=lambda r: r.get("ts") or 0, reverse=True)
    if state:
        rows = [r for r in rows if r.get("state") == state]
    return rows[:limit]


def get(rid):
    """按 id 取一条（取的是最新状态那行）；没有返回 None。"""
    return _index().get(rid)


def _set_state(row, state):
    """改状态 = 追加一行同 id 的新行（后写的赢）。"""
    new = dict(row)
    new["state"] = state
    new["decided_ts"] = time.time()
    with _lock:
        _append(new)
    return new


def approve(rid):
    """过审 → **按正常格式**重新发回原会话。返回 (ok, 说明)。

    走的就是 `qq_api.send_image` 那条路，caption 用拦下时存的那份原文——
    对方收到的跟一张没被拦过的图**一模一样**，看不出中间进过二审。
    """
    row = get(rid)
    if row is None:
        return False, "这条记录不在了（可能已被清理）"
    if row.get("state") != PENDING:
        return False, "这条已经处理过了"
    target = row.get("target") or ""
    tid = row.get("target_id") or ""
    path = row.get("path") or ""
    if not target or not tid:
        return False, "记录里没有接收方，发不回去"
    if not os.path.exists(path):
        return False, "图文件已经不在了（可能被清理）"
    from app import qq_api
    try:
        qq_api.send_image(target, tid, path, caption=row.get("caption") or "")
    except Exception as exc:
        log.warning("过审重发失败 %s：%s", rid, exc)
        return False, "发送失败：%s" % exc
    _set_state(row, APPROVED)
    # 补记账本：账本只在真发出去之后记，被拦的图当时没记上。现在它真的发出
    # 去了，不补的话「引用这张图问提示词」会查不到。
    if row.get("tag"):
        try:
            from app import image_log
            image_log.save(row["tag"], prompt=row.get("prompt") or "",
                           file=row.get("src_file") or "",
                           skill=row.get("skill") or "", target=target,
                           target_id=tid,
                           seed="" if row.get("seed") is None
                           else row["seed"])
        except Exception:
            log.exception("过审后补记账本失败 %s", rid)
    log.info("人工过审，已重发 %s → %s %s（编号 %s）",
             rid, target, tid, row.get("tag") or "-")
    return True, "已过审并重新发送"


def reject(rid):
    """确认违规 → 标记拒发，图留着不删（备查）。返回 (ok, 说明)。"""
    row = get(rid)
    if row is None:
        return False, "这条记录不在了（可能已被清理）"
    if row.get("state") != PENDING:
        return False, "这条已经处理过了"
    _set_state(row, REJECTED)
    log.info("人工二审判违规 %s（%s %s）", rid,
             row.get("target"), row.get("target_id"))
    return True, "已确认违规，不发送"


def pending_count():
    """待审条数。管理页角标用。"""
    return len([r for r in _index().values() if r.get("state") == PENDING])
