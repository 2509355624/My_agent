"""QQ 事件解析：CQ 码、消息段、文件段图片、引用与转发。

只做「把 SnowLuma 推来的事件翻译成文本/图片/引用」，不发 HTTP、不碰会话。
`_resolve_file_ref` 例外：它把 `fileid://` 换成真实链接，必须在 worker
线程里调用（见 `_resolve_quote` 的调用方）。
"""

import logging
import re

from app.features.qq import api as qq_api

log = logging.getLogger("qq_bot.message_parse")

_CQ_RE = re.compile(r"\[CQ:([a-z_]+)((?:,[^\]]*)?)\]")

# ─── 事件解析 ────────────────────────────────────────

def _cq_arg(rest, key):
    """从 CQ 码的参数串里取一个值（`...,url=http://x,y=1` 这种形式）。"""
    for part in (rest or "").lstrip(",").split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            if k.strip() == key:
                return v.strip()
    return ""


# ─── 「文件」段里的图片（2026-11-15 用户要求）────────────────────
# QQ 有两条上传通道：「图片」和「文件」。走「文件」发上来的图，消息里是
# `{"type": "file"}` 段，而 `_parse_segments` 原先只认 text/at/image/mface/face
# ——整段被静默丢掉：群里 @ 了却「没有内容」，私聊更是连轮都不进，用户看到
# 的就是「大大怪识别不了我发的图」。现在按扩展名认图，当普通图片收。
#
# 拿不到 url 时退回 `fileid://` 伪地址，由 worker 线程的 `_prepare_images`
# 调 OneBot 的取文件链接接口解析——`_parse_segments` 跑在 WS 消息循环上，
# 在这儿发 HTTP 会把所有群一起卡住（与图片「只取地址不下载」同一条纪律）。
_IMAGE_FILE_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")
_FILE_REF_RE = re.compile(r"^fileid://(group|private)/([^/]+)/([^/]+)/(.+)$")


def _file_name(sdata):
    """文件段里的文件名。SnowLuma 把它放在 `file`，别的实现叫 `name`。"""
    return str(sdata.get("file") or sdata.get("name")
               or sdata.get("file_name") or "").strip()


def _is_image_file(name):
    """按扩展名判「这是图片文件」。名字取不到就不猜（当不是）。"""
    n = str(name or "").split("?")[0].split("#")[0].strip().lower()
    return any(n.endswith(ext) for ext in _IMAGE_FILE_EXTS)


def _file_ref(sdata, ev):
    """文件段 → 能交给 `_prepare_images` 的地址；不是图片 / 拿不到 → 空串。"""
    if not _is_image_file(_file_name(sdata)):
        return ""
    url = str(sdata.get("url") or "").strip()
    if url:
        return url
    fid = str(sdata.get("file_id") or "").strip()
    if not fid:
        return ""
    from urllib.parse import quote

    # 群/私聊按 message_type 分；`get_msg` 拉回来的被引用消息不一定带这个字段
    # （`_quote_body` 复用本函数），有 group_id 就按群算——群号不会误落到私聊。
    mt = str(ev.get("message_type") or "")
    if (mt == "group") or (mt != "private" and ev.get("group_id")):
        kind, tid = "group", str(ev.get("group_id") or "")
        busid = str(sdata.get("busid") or "0")
    else:
        kind, tid = "private", str(ev.get("user_id") or "")
        busid = "0"
    if not tid:
        return ""
    return "fileid://%s/%s/%s/%s" % (kind, tid, busid, quote(fid, safe=""))


def _resolve_file_ref(url):
    """`fileid://` 伪地址 → 真实下载链接。**只能在 worker 线程里调**（发 HTTP）。

    解析失败原样返回：`fetch_image` 会抛、`_prepare_images` 记一行 warning
    跳过——跟普通图片取不到的处理一致，不额外炸整轮。
    """
    m = _FILE_REF_RE.match(str(url or ""))
    if not m:
        return url
    from urllib.parse import unquote

    kind, tid, busid = m.group(1), m.group(2), m.group(3)
    fid = unquote(m.group(4))
    try:
        real = qq_api.file_url(kind, tid, fid, busid=busid)
    except Exception as exc:                     # noqa: BLE001
        log.warning("取文件链接失败（%s %s）：%s", kind, tid, exc)
        return url
    if not real:
        log.warning("取文件链接为空（%s %s / %s）", kind, tid, fid[:24])
        return url
    return real


def _parse_segments(ev):
    """从事件里取出文本、@ 标记、以及图片地址列表。

    OneBot 有两种上报格式：message 数组（结构化）和 raw_message 字符串
    （CQ 码）。SnowLuma 配的是哪种都要能认，所以两条路都走。

    图片在这里**只取地址，不下载**：下载是 IO，得放到 worker 线程里做
    （见 SessionRunner._prepare_images），事件循环上不能有阻塞操作。
    """
    self_id = str(ev.get("self_id", ""))
    segs = ev.get("message")

    if isinstance(segs, list):
        parts, at_me, images = [], False, []
        for seg in segs:
            if not isinstance(seg, dict):
                continue
            stype = seg.get("type")
            sdata = seg.get("data") or {}
            if stype == "text":
                parts.append(str(sdata.get("text", "")))
            elif stype == "at":
                if str(sdata.get("qq", "")) == self_id:
                    at_me = True
            elif stype == "image":
                url = str(sdata.get("url") or "")
                if url:
                    images.append(url)
                else:
                    # 只给了本地文件名（file）时拿不到图，退回占位说明
                    parts.append("[图片]")
            elif stype == "mface":
                # 商城表情包：SnowLuma 一般带 url，能当普通图收（表情库/识图都认）
                url = str(sdata.get("url") or "")
                if url:
                    images.append(url)
                else:
                    parts.append("[表情]")
            elif stype == "file":
                # 「文件」通道发上来的图（2026-11-15）：按扩展名认图，当普通图片
                # 收；不是图片 / 拿不到地址就留一条文字占位，至少让模型知道对方
                # 发过东西（否则整轮会是「有 @ 但没内容」直接被丢掉）。
                ref = _file_ref(sdata, ev)
                if ref:
                    images.append(ref)
                else:
                    parts.append("[文件：%s]" % (_file_name(sdata) or "未命名"))
            elif stype == "face":
                parts.append("[表情]")
        return "".join(parts).strip(), at_me, images

    raw = str(ev.get("raw_message") or "")
    images = []

    def _sub(m):
        name, rest = m.group(1), m.group(2)
        if name == "at" and ("qq=" + self_id) in rest:
            _sub.at_me = True
        elif name == "image":
            url = _cq_arg(rest, "url")
            if url:
                images.append(url)
        elif name == "mface":
            url = _cq_arg(rest, "url")
            if url:
                images.append(url)
        elif name == "file":
            sdata = {"file": _cq_arg(rest, "file"),
                     "file_id": _cq_arg(rest, "file_id"),
                     "busid": _cq_arg(rest, "busid"),
                     "url": _cq_arg(rest, "url")}
            ref = _file_ref(sdata, ev)
            if ref:
                images.append(ref)
            else:
                return "[文件：%s]" % (_file_name(sdata) or "未命名")
        return ""

    _sub.at_me = False
    return _CQ_RE.sub(_sub, raw).strip(), _sub.at_me, images


def _extract_quotes(ev):
    """取出这条消息里的引用来源（reply / forward 段）。

    这两类段只带一个 id，被引用的正文不在这条消息里，所以单独提取出来，
    留到 _run_turn 里再回头查——那时才确认了「这条消息确实要回」，否则
    群里每来一条消息都要白跑一次 HTTP。

    与 _parse_segments 分开，是因为两者性质不同：text / at / image 是消息
    本体（决定回不回、谁来问），引用只是本轮的附加上下文。

    返回 [{"kind": "reply", "id": ...}] 或
         [{"kind": "forward", "id": ..., "nodes": [...]}]。
    """
    out = []
    segs = ev.get("message")

    if isinstance(segs, list):
        for seg in segs:
            if not isinstance(seg, dict):
                continue
            stype, sdata = seg.get("type"), seg.get("data") or {}
            if stype == "reply":
                mid = str(sdata.get("id") or "")
                if mid:
                    out.append({"kind": "reply", "id": mid})
            elif stype == "forward":
                nodes = sdata.get("content")
                out.append({
                    "kind": "forward",
                    "id": str(sdata.get("id") or ""),
                    "nodes": nodes if isinstance(nodes, list) else None,
                })
        return out

    raw = str(ev.get("raw_message") or "")
    for m in _CQ_RE.finditer(raw):
        name, rest = m.group(1), m.group(2)
        if name == "reply":
            mid = _cq_arg(rest, "id")
            if mid:
                out.append({"kind": "reply", "id": mid})
        elif name == "forward":
            fid = _cq_arg(rest, "id")
            if fid:
                out.append({"kind": "forward", "id": fid, "nodes": None})
    return out


def _quote_body(ev, limit):
    """把一条被引用的消息渲染成 (正文, 说话人, 图片地址列表)。

    这里直接复用 _parse_segments：get_msg 返回的结构与 WS 上报的消息事件
    同构，同一套解析逻辑拿来就能用。注意**里层的引用不再展开**——只解析
    正文，它自己带的 reply 段直接忽略，否则「引用了引用」会一层层追下去。
    """
    text, _at, images = _parse_segments(ev)
    who = ((ev.get("sender") or {}).get("card")
           or (ev.get("sender") or {}).get("nickname") or "")
    # 2026-11-03：正文为空时**返回空串**，由调用方决定怎么处理。
    # 原来填「（没有可读内容）」，这个占位符会被当成真内容塞进扮演态 brief 的
    # 「用户点名要画的（最高优先）」段，把用户真正的需求挤掉。
    body = str(text or "").strip()
    if not body and images:
        body = "（图片）"
    if limit > 0 and len(body) > limit:
        body = body[:limit] + "……（过长已截断）"
    return body, who, images


def _resolve_quote(q, limit):
    """把一个引用来源拉取并渲染成 (文本块, 图片地址列表)。

    拉取失败一律降级成一句说明：引用只是本轮的补充上下文，取不到不该让
    整轮对话失败（与取图失败的处理一致）。返回空文本表示这条引用没内容。
    """
    if q.get("kind") == "forward":
        nodes = q.get("nodes")
        if not nodes and q.get("id"):
            try:                     # 兜底：forward 段没带 content 时再问一次
                nodes = qq_api.get_forward_msg(q["id"]).get("messages") or []
            except Exception as exc:
                log.warning("拉取转发的聊天记录失败 %s：%s", q.get("id"), exc)
                return "[转发的聊天记录无法读取]", []
        lines, images, total = [], [], 0
        for node in (nodes or []):
            if not isinstance(node, dict):
                continue
            body, who, node_images = _quote_body(node, limit)
            lines.append("%s：%s" % (who, body) if who else body)
            images.extend(node_images)
            total += len(lines[-1])
            if limit > 0 and total >= limit:
                lines.append("……（记录过长，已截取）")
                break
        if not lines:
            return "", []
        return "[转发的聊天记录]\n" + "\n".join(lines), images

    try:
        msg = qq_api.get_message(q.get("id"))
    except Exception as exc:
        log.warning("拉取引用的消息失败 %s：%s", q.get("id"), exc)
        return "[引用的消息无法读取]", []
    body, who, images = _quote_body(msg, limit)
    # 2026-11-03：引用了一条**取不到正文**的消息（常见于引用机器人自己发的图
    # 或已撤回的消息）时返回空串，让调用方跳过。
    # 原来返回 `"[引用 大大怪 的消息] "`（后面什么都没有），这段垃圾会被塞进
    # 扮演态的 brief「用户点名要画的（最高优先）」，把真需求挤掉。
    if not str(body or "").strip() and not images:
        return "", []
    head = "[引用 %s 的消息] " % who if who else "[引用的消息] "
    return head + body, images
