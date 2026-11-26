# -*- coding: utf-8 -*-
"""工作台聊天记录的服务端正本（2026-11-24 用户要求）。

## 为什么要有它

用户原话：「网页端的聊天记录是怎么保存的，会持久化保存吗？我更换不同的
浏览器登陆之后，是否还会显示内容和之前的聊天记录」。

以前只存 localStorage——它按「浏览器 + 域名」隔离：换手机、换浏览器、换
隧道域名（Cloudflare 快擦除每次重连都换）就是一片空白。所以加一层服务端
存档，**登录的 QQ 号就是档位**，任何设备登录都拿到同一份。

## 形状

· 一条记录 = 前端 `store.js` 里那种消息对象（`k` 是类型：user / img /
  comic / ref / note）。
· 后端不认识这些字段，只做「按指纹去重 + 追加 + 截断」。字段怎么解释
  全在前端（`archive.js` 重放），这里不参与，改前端不会让老数据读不了。
· 每条存档额外带一个 `f`（内容指纹），追加时同指纹的直接跳过——前端
  每次只推最后十几条，重复的那些在这里被吃掉。

## 存储

`state/chat_archive/<qq>.json`，一档一个文件：

· 单文件而不是 SQLite：一条记录就是几百字节的 JSON，120 条撑死几十 KB，
  读写一次就是整个文件，不存在并发写同一行的复杂度；
· 写盘用「临时文件 + os.replace」：手机在中途刷新不会留下半截 JSON；
· 按 QQ 号一个文件：删一个人的记录就是删一个文件，不会误伤别人。

## 鉴权

只认 `qq` 参数（5175 转发时注入，见 `friend_app._forward` 的说明：qq 一律
由网关写死，不信客户端传的）。5174 本机直连（站主自己）按 `GALLERY_OWNER_QQ`
认——和画廊那边同一套口径。没登录就 401，不返回空列表：那会把「没登录」
伪装成「你还没有记录」。
"""

import io
import json
import logging
import os
import re
import threading
import time

from flask import Blueprint, jsonify, request

from app.config import AGENT_PORT, GALLERY_OWNER_QQ, state_path
from app.core.admin_access import LOCAL_ADDRS, is_lan_addr

log = logging.getLogger("chat_archive")

bp = Blueprint("chat_archive", __name__, url_prefix="/api/chat")

DIR = state_path("chat_archive")

# 一个档位最多留多少条。**这是用户能看到多长的历史**，别和「一次推几条」混起来。
# 早期是 240：一天正常画几十张，两条记录一张卡，两三天就滚掉了——用户体验上
# 等于「以前的图没了」。2026-11-25 提到 2000（约一千张图的量级）。一条记录
# 通常 200~600 字节，2000 条约 1MB，前端只拉尾部一页，不整份下发。
KEEP = 2000
# 一次推送最多收几条：前端现在**一产生就推**（单条 / 刚变化的几条），给到
# PUSH_MAX 是防一个改坏了的客户端一把塞几兆进来。
PUSH_MAX = 200
# GET 默认下发多少条（尾部）。前端要更早的再带 `?limit=` 要，最多给到 KEEP。
TAIL = 120
BODY_MAX = 4 << 20          # 单次请求体上限 4MB

_QQ_RE = re.compile(r"^\d{4,12}$")
_LOCK = threading.Lock()


def _file(qq):
    return os.path.join(DIR, "%s.json" % qq)


def _load(qq):
    return _load_box(qq)[0]


def _load_box(qq):
    """→ (msgs, 旧记录补 id 后有没有变)。

    老存档里的记录没有 id（升级前的前端写的），按内容认人一错位，图和提示
    行就会串。这里按固定顺序补一个 `old_<序号>`：同样的输入永远算出同样的
    id，多台设备、多次读取都一样，相当于给它们一个稳定身份。
    """
    p = _file(qq)
    if not os.path.isfile(p):
        return [], False
    try:
        with io.open(p, "r", encoding="utf-8") as f:
            d = json.load(f)
    except Exception:                        # noqa: BLE001
        # 文件坏了就当成空的，别让一个人的存档把整个接口 500 掉。
        # 备份一份现场，站主想捞还能捞（直接删掉会永久丢）。
        log.warning("聊天存档读不了，已留现场：%s", p, exc_info=True)
        try:
            os.replace(p, p + ".broken-%d" % int(time.time()))
        except OSError:
            pass
        return [], False
    if not isinstance(d, dict):
        return [], False
    msgs = d.get("msgs")
    if not isinstance(msgs, list):
        return [], False
    changed = False
    for i, m in enumerate(msgs):
        if isinstance(m, dict) and not _rec_id(m):
            m["id"] = "old_%d" % i
            changed = True
    return msgs, changed


def _save(qq, msgs):
    os.makedirs(DIR, exist_ok=True)
    p = _file(qq)
    tmp = "%s.%d.tmp" % (p, os.getpid())
    with io.open(tmp, "w", encoding="utf-8") as f:
        json.dump({"v": 1, "qq": qq, "msgs": msgs,
                   "ts": round(time.time(), 3)}, f, ensure_ascii=False)
    os.replace(tmp, p)


def _fingerprint(m):
    """记录指纹。前端推上来时带 `f`（它算好的），没带就自己算一遍。
    两者算法一致（都是紧凑 JSON），所以换端推同一条不会重复。"""
    try:
        return json.dumps(m, ensure_ascii=False, separators=(",", ":"),
                          sort_keys=True)
    except (TypeError, ValueError):
        return ""


def _rec_id(m):
    """记录自己的稳定 id（前端建的）。没有 id 的老记录返回 ""。

    为什么按 id 而不是按整条 JSON 去重（2026-11-25 实测）：一张生图卡从
    「排队中」走到「已出图」时整条 JSON 全变了，按内容去重就成了**同一张卡
    追加好几遍**——240 条里 165 条是卡，记录顺序变成「用户、结果、结果…」。
    id 不变，它才是「同一张卡」的凭据。
    """
    if not isinstance(m, dict):
        return ""
    return str(m.get("id") or "").strip()


def _who():
    """当前请求属于哪个档位。→ qq 或 ""。

    ⚠️ 2026-11-26 用户划死的边界：**访客模式只属于工作台**。这里曾经写过
    「访客一律返回空串」，结果凡是共用这套身份判定的路子（跑图台、角色
    扮演、小说、群友额度……）在访客 cookie 下一律被当成没登录，主业务被
    一个展示用的开关拖垮。现在访客的身份**只在 5175 的 `/workspace/` 那条
    路上**带 `X-Friend-Guest` 头，其余页面根本没有这个头，这里也就不必、
    也不该再判访客。
    """
    qq = str(request.args.get("qq") or "").strip()
    if not qq:
        body = request.get_json(silent=True) or {}
        qq = str(body.get("qq") or "").strip()
    if _QQ_RE.match(qq):
        return qq
    # 5174 本机直连（站主自己开管理页）：网关没注入 qq，用配置里的站主号当档位。
    host = str(request.host or "").lower()
    addr = request.remote_addr or ""
    if (not request.headers.get("X-Friend-Token")
            and not request.headers.get("CF-Connecting-IP")
            and host.endswith(":%s" % AGENT_PORT)
            and (addr in LOCAL_ADDRS or is_lan_addr(addr))
            and GALLERY_OWNER_QQ):
        return str(GALLERY_OWNER_QQ)
    return ""


@bp.route("/archive", methods=["GET"])
def api_archive_get():
    """取这一档的聊天记录。→ {ok, msgs, n, total, more}

    `total` 是这一档一共存了多少条，`more` 表示还有更早的没下发（前端据此
    决定「加载更早」要不要显示）。一次默认只给尾部 TAIL 条：手机端第一次
    进页面拉十兆 JSON 会白屏几秒，那是真实的体验问题。
    """
    qq = _who()
    if not qq:
        # 访客（2026-11-26）：不存服务端记录，前端按 //me 的 role 走本地缓存。
        return jsonify({"ok": False, "error": "没登录"}), 401
    msgs = _load(qq)
    total = len(msgs)
    limit = request.args.get("limit", type=int) or TAIL
    limit = max(1, min(limit, KEEP))
    if total > limit:
        msgs = msgs[-limit:]
    return jsonify({"ok": True, "msgs": msgs, "n": len(msgs),
                    "total": total, "more": total > len(msgs), "qq": qq})


@bp.route("/archive", methods=["POST"])
def api_archive_post():
    """追加记录。→ {ok, n, added}

    body: `{"msgs": [{"m": {...}, "f": "..."}]}`，也接受裸的对象数组
    （`{"msgs": [{...}]}`）。缺 `f` 的自己算。

    只追加、不覆盖：两台的记录会拼成时间序（各自追加的先后），不会因为
    后到的那台把先到的那台冲掉——这跟 localStorage 那个「整份覆盖」的
    行为正好相反，也正是「换浏览器能看到之前的记录」的关键。
    """
    qq = _who()
    if not qq:
        # 访客（2026-11-26）：不存服务端记录，前端按 //me 的 role 走本地缓存。
        return jsonify({"ok": False, "error": "没登录"}), 401
    if request.content_length and request.content_length > BODY_MAX:
        return jsonify({"ok": False, "error": "这次推的记录太大了"}), 413

    body = request.get_json(silent=True) or {}
    rows = body.get("msgs") if isinstance(body, dict) else body
    if not isinstance(rows, list):
        return jsonify({"ok": False, "error": "参数不对"}), 400
    rows = rows[:PUSH_MAX]

    incoming = []
    for r in rows:
        if isinstance(r, dict) and isinstance(r.get("m"), dict):
            m = r["m"]
            f = str(r.get("f") or "") or _fingerprint(m)
        elif isinstance(r, dict) and r.get("k"):
            m = r
            f = _fingerprint(m)
        else:
            continue
        incoming.append((f, m))
    if not incoming:
        return jsonify({"ok": True, "n": len(_load(qq)), "added": 0})

    with _LOCK:
        msgs, migrated = _load_box(qq)
        # 去重分两层：
        #   ① 有 id 的记录按 id **原位替换**（卡片状态更新，不追加）；
        #   ② 没有 id 的老记录按内容指纹比，且只比最近这一段。
        by_id = {}
        for i, m in enumerate(msgs):
            rid = _rec_id(m)
            if rid:
                by_id[rid] = i
        seen = {_fingerprint(m) for m in msgs[-PUSH_MAX * 2:]}
        added = 0
        updated = 0
        for f, m in incoming:
            rid = _rec_id(m)
            if rid and rid in by_id:
                msgs[by_id[rid]] = m        # 同一条记录的新状态：原地更新
                updated += 1
                continue
            if f in seen:
                continue
            seen.add(f)
            msgs.append(m)
            if rid:
                by_id[rid] = len(msgs) - 1
            added += 1
        # 老记录补了 id 也算「该落盘」：迁移一次之后就稳定了，不会再变。
        if added or updated or migrated:
            if added:
                msgs = msgs[-KEEP:]
            try:
                _save(qq, msgs)
            except OSError:
                log.warning("聊天存档写盘失败 qq=%s", qq, exc_info=True)
                return jsonify({"ok": False, "error": "存不下"}), 500
    return jsonify({"ok": True, "n": len(msgs), "added": added,
                    "updated": updated})


@bp.route("/archive", methods=["PATCH"])
def api_archive_patch():
    """就地改一条已有记录（按 `id` 认人，老记录按 `job`）。→ {ok, hit}

    前端推的是「追加」，改不了已经存下来的那条：出图卡片从「排队中…」
    变成「这张没了 / 已出图」时，服务端那份还停在旧状态，下次换设备登录
    读回来又是一张假卡片（2026-11-24 用户：「重新进入网页，又出现了那个
    排队中，我知道是假的」）。

    body: `{"job": "GP-...", "m": {...}}` 或带上裸字段：
    `{"job": "...", "note": "…", "err": true}`。带 `id` 时以 id 为准。

    只改 `k:"img"` 的那一条；找不到就 hit=0，不报错——卡片可能已经被
    截断掉了，不值得为它拦一次请求。
    """
    qq = _who()
    if not qq:
        # 访客（2026-11-26）：不存服务端记录，前端按 //me 的 role 走本地缓存。
        return jsonify({"ok": False, "error": "没登录"}), 401
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        return jsonify({"ok": False, "error": "参数不对"}), 400
    job = str(body.get("job") or "").strip()
    rid = str(body.get("id") or "").strip()
    if not job and not rid:
        return jsonify({"ok": False, "error": "没给 job 或 id"}), 400
    patch = body.get("m")
    if not isinstance(patch, dict):
        patch = {k: v for k, v in body.items() if k not in ("job", "id")}

    with _LOCK:
        msgs = _load(qq)
        hit = 0
        for m in reversed(msgs):
            if not isinstance(m, dict) or m.get("k") != "img":
                continue
            # 有 id 就认 id（id 是同一条记录的凭据）；老记录没有 id，退回按 job 认。
            if rid:
                if _rec_id(m) != rid:
                    continue
            elif str(m.get("job") or "") != job:
                continue
            m.update(patch)
            hit += 1
            break
        if hit:
            try:
                _save(qq, msgs)
            except OSError:
                log.warning("聊天存档改写失败 qq=%s", qq, exc_info=True)
                return jsonify({"ok": False, "error": "存不下"}), 500
    return jsonify({"ok": True, "hit": hit})


@bp.route("/archive", methods=["DELETE"])
def api_archive_clear():
    """清空这一档（用户在「我的」里点「清空聊天记录」时用）。

    **只删自己的**：qq 由网关注入，改参数也删不到别人。退出登录**不会**
    走这里——退出只是换人，记录还得留着给他换台设备登录时看。
    """
    qq = _who()
    if not qq:
        # 访客（2026-11-26）：不存服务端记录，前端按 //me 的 role 走本地缓存。
        return jsonify({"ok": False, "error": "没登录"}), 401
    with _LOCK:
        try:
            if os.path.isfile(_file(qq)):
                os.remove(_file(qq))
        except OSError:
            log.warning("清空聊天存档失败 qq=%s", qq, exc_info=True)
            return jsonify({"ok": False, "error": "删不掉"}), 500
    return jsonify({"ok": True})
