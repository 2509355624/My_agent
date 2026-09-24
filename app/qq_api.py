"""
NapCat（OneBot 11）HTTP API 封装 + QQ 文本适配

职责边界：只解决「一段文本 / 一张图怎么变成 QQ 消息发出去」。不含任何
会话、触发、调度逻辑——那些在 app/qq_bot.py。

为什么需要这个文件：模型的输出是给网页端看的 Markdown，直接丢给 QQ 会
看到满屏 `**` 和 `#`，而且单条消息有长度上限会被截断。所以发送前必须
先降级成纯文本，再按段落切成若干条。
"""

import os
import re
import threading
import time

import requests

from app.config import QQ_HTTP_URL, QQ_TOKEN, QQ_REPLY_MAX_CHARS

# 连发多条之间的间隔。切分后的消息如果瞬间连发，容易被 QQ 判定为异常
_SEND_INTERVAL = 0.3


# ─── 当前会话上下文（供工具层读取）────────────────────

# agent 一轮固定在一个线程里跑完，而 execute_tool(name, args) 的签名不便
# 再加参数（会牵动全部工具与既有测试），所以用线程本地变量传递「此刻在为
# 哪个 QQ 会话服务」——与 app/cancel.py 传递取消事件是同一套做法。
_local = threading.local()


def bind_context(session_key, target, target_id):
    """绑定当前线程正在处理的 QQ 会话。target 取 "private" / "group"。"""
    _local.session_key = session_key
    _local.target = target
    _local.target_id = target_id


def clear_context():
    """摘掉绑定。worker 线程是复用的，不清理会把上一个会话带进下一轮。"""
    for attr in ("session_key", "target", "target_id"):
        if hasattr(_local, attr):
            delattr(_local, attr)


def current_context():
    """返回 (target, target_id)；不在 QQ 会话里时返回 (None, None)。"""
    return (getattr(_local, "target", None),
            getattr(_local, "target_id", None))


# ─── 底层调用 ────────────────────────────────────────

# NapCat 就在本机 127.0.0.1，请求必须绕开系统代理：本机装了 Clash 这类工具
# 时会把代理写进 Windows 注册表，而 requests 在环境变量为空时会 fallback
# 去读注册表，结果连 127.0.0.1 的请求也被送去代理、换回一个 502。
# trust_env=False 让这个 session 完全不理会环境变量与注册表里的代理设置。
_session = requests.Session()
_session.trust_env = False


def _call(action, payload=None, timeout=20):
    """调一个 OneBot action，返回 data 字段。

    OneBot 11 的返回约定是 {"status": "ok", "retcode": 0, "data": {...}}，
    失败时 retcode 非 0（也有实现只给 status="failed"）。两个都看。
    """
    url = QQ_HTTP_URL + "/" + action
    headers = {"Content-Type": "application/json"}
    if QQ_TOKEN:
        headers["Authorization"] = "Bearer " + QQ_TOKEN

    resp = _session.post(url, json=payload or {}, headers=headers, timeout=timeout)
    resp.raise_for_status()
    try:
        data = resp.json()
    except ValueError:
        raise RuntimeError("OneBot 返回不是 JSON：" + resp.text[:200])

    if data.get("status") == "failed" or data.get("retcode") not in (0, None):
        raise RuntimeError("OneBot 调用失败 %s: %s" % (action, str(data)[:300]))
    return data.get("data") or {}


def check_alive(timeout=5):
    """探活：能取到登录信息就说明协议端在线。"""
    info = _call("get_login_info", timeout=timeout)
    return info.get("user_id"), info.get("nickname")


# ─── Markdown → QQ 纯文本 ────────────────────────────

# 处理顺序有讲究：图片 ![alt](url) 必须在普通链接之前，否则会先被
# 链接规则吃掉一个 "!"；行内代码要在加粗之前，避免代码里的 * 被当强调。
_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\((https?://[^)\s]+)\)")
_LINK_RE = re.compile(r"\[([^\]]*)\]\((https?://[^)\s]+)\)")
_FENCE_RE = re.compile(r"^\s*```.*$", re.M)
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+", re.M)
_QUOTE_RE = re.compile(r"^\s{0,3}>\s?", re.M)
_HR_RE = re.compile(r"^\s{0,3}([-*_])\s*(\1\s*){2,}$", re.M)
_BULLET_RE = re.compile(r"^(\s*)[-*+]\s+", re.M)
_BOLD_RE = re.compile(r"\*\*([^*\n]+)\*\*")
_UNDERSCORE_BOLD_RE = re.compile(r"(?<![A-Za-z0-9_])__([^_\n]+)__(?![A-Za-z0-9_])")
_ITALIC_RE = re.compile(r"(?<![A-Za-z0-9_*])\*([^*\n]+)\*(?![A-Za-z0-9_*])")
_CODE_RE = re.compile(r"`([^`\n]+)`")


def to_qq_text(text):
    """把 Markdown 降级成适合 QQ 显示的纯文本。

    保留内容和换行结构，只剥掉标记符号——不做「智能改写」，避免动到
    正文本身（模型写的字不该在传输层被改）。
    """
    if not text:
        return ""

    out = text.replace("\r\n", "\n").replace("\r", "\n")

    out = _IMAGE_RE.sub(lambda m: "[图片] " + m.group(2), out)
    out = _LINK_RE.sub(lambda m: (m.group(1) + " " + m.group(2)).strip(), out)
    out = _FENCE_RE.sub("", out)
    out = _HEADING_RE.sub("", out)
    out = _QUOTE_RE.sub("", out)
    out = _HR_RE.sub("", out)
    out = _BULLET_RE.sub(lambda m: m.group(1) + "· ", out)

    out = _CODE_RE.sub(r"\1", out)
    out = _BOLD_RE.sub(r"\1", out)
    out = _UNDERSCORE_BOLD_RE.sub(r"\1", out)
    out = _ITALIC_RE.sub(r"\1", out)

    # 连续 3 个以上空行压成 1 个（去掉围栏/分隔线后常留下成片空行）
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


# ─── 长文切分 ────────────────────────────────────────

def _atomic_pieces(text, limit):
    """把文本拆成不超过 limit 字的原子块，优先在段落/行边界切。"""
    pieces = []
    for block in re.split(r"\n\s*\n", text):
        block = block.strip()
        if not block:
            continue
        if len(block) <= limit:
            pieces.append(block)
            continue
        for line in block.split("\n"):
            line = line.rstrip()
            if not line:
                continue
            # 单行本身超限（模型偶尔写出一整段没有换行的长文）：硬切
            while len(line) > limit:
                pieces.append(line[:limit])
                line = line[limit:]
            if line:
                pieces.append(line)
    return pieces


def split_message(text, limit=None):
    """切成若干条不超过 limit 字的消息，尽量不让一个段落被拆开。"""
    limit = limit or QQ_REPLY_MAX_CHARS
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]

    chunks, buf = [], ""
    for piece in _atomic_pieces(text, limit):
        if not buf:
            buf = piece
        elif len(buf) + len(piece) + 1 <= limit:
            buf = buf + "\n" + piece
        else:
            chunks.append(buf)
            buf = piece
    if buf:
        chunks.append(buf)
    return chunks


# ─── 消息段 ──────────────────────────────────────────

def text_segment(text):
    return {"type": "text", "data": {"text": text}}


def image_segment(path_or_url):
    """构造图片消息段。

    有两种来源：生图工具产出的本地文件路径，或模型给出的 http 图片地址。
    本地路径要转成 file:// URI（Windows 的反斜杠必须先换成正斜杠）。
    """
    src = str(path_or_url).strip()
    if src.startswith(("http://", "https://")):
        return {"type": "image", "data": {"file": src}}
    abs_path = os.path.abspath(src).replace("\\", "/")
    if not abs_path.startswith("/"):
        abs_path = "/" + abs_path
    return {"type": "image", "data": {"file": "file://" + abs_path}}


# ─── 发送 ────────────────────────────────────────────

def send_private(user_id, message, limit=None):
    """给某个好友发消息（自动分段）。返回发出的条数。"""
    chunks = message if isinstance(message, list) else split_message(
        to_qq_text(message), limit)
    for i, chunk in enumerate(chunks):
        if i:
            time.sleep(_SEND_INTERVAL)
        _call("send_private_msg",
              {"user_id": int(user_id), "message": chunk}, timeout=30)
    return len(chunks)


def send_group(group_id, message, limit=None):
    """往某个群发消息（自动分段）。返回发出的条数。"""
    chunks = message if isinstance(message, list) else split_message(
        to_qq_text(message), limit)
    for i, chunk in enumerate(chunks):
        if i:
            time.sleep(_SEND_INTERVAL)
        _call("send_group_msg",
              {"group_id": int(group_id), "message": chunk}, timeout=30)
    return len(chunks)


def send_image(target, target_id, image_path, caption=""):
    """发一张图（可选带一句文字）。target 取 "private" 或 "group"。"""
    segs = []
    if caption:
        segs.append(text_segment(to_qq_text(caption)))
    segs.append(image_segment(image_path))
    action = "send_%s_msg" % target
    key = "user_id" if target == "private" else "group_id"
    _call(action, {key: int(target_id), "message": segs}, timeout=60)
    return 1
