"""
NapCat（OneBot 11）HTTP API 封装 + QQ 文本适配

职责边界：只解决「一段文本 / 一张图怎么变成 QQ 消息发出去」。不含任何
会话、触发、调度逻辑——那些在 app/qq_bot.py。

为什么需要这个文件：模型的输出是给网页端看的 Markdown，直接丢给 QQ 会
看到满屏 `**` 和 `#`，而且单条消息有长度上限会被截断。所以发送前必须
先降级成纯文本，再按段落切成若干条。
"""

import itertools
import logging
import os
import re
import threading
import time

import requests

from app.config import QQ_HTTP_URL, QQ_TOKEN, QQ_REPLY_MAX_CHARS

log = logging.getLogger("qq_api")

# 连发多条之间的间隔。切分后的消息如果瞬间连发，容易被 QQ 判定为异常
_SEND_INTERVAL = 0.3


# ─── 发送日志：把「发给了谁」打进适配层终端 ─────────────

# NapCat 自己的日志有发送行但没有正文，适配层这边只有接收没有发送，
# 排查「它到底回给了谁」得两边对——干脆在发送的必经之路（本文件）补一行，
# 带上群名/昵称和内容预览。名字懒加载一次缓存住，拿不到就显示号码，
# 绝不因为取名字失败而拦发送。
_name_lock = threading.Lock()
_names = {}                 # {"group_<id>": 群名, "private_<id>": 昵称}
_names_fetched_at = 0.0     # 上次尝试拉名单的时刻（失败也要隔一阵才重试）
_NAMES_RETRY_AFTER = 600


def _display_name(kind, target_id):
    """group_<id> → 群名，private_<id> → 昵称；拿不到返回空串。"""
    global _names_fetched_at
    key = "%s_%s" % (kind, int(target_id))
    with _name_lock:
        if key not in _names and time.time() - _names_fetched_at > _NAMES_RETRY_AFTER:
            _names_fetched_at = time.time()
            for fetch, k, id_field, name_field in (
                    (get_group_list, "group", "group_id", "group_name"),
                    (get_friend_list, "private", "user_id", "nickname")):
                try:
                    for item in fetch():
                        _names["%s_%s" % (k, int(item.get(id_field, 0)))] = (
                            item.get(name_field) or "")
                except Exception:
                    pass          # 名单拿不到无所谓，发送才是正事
        return _names.get(key, "")


def group_display_name(group_id):
    """群号 → 群名，拿不到返回空串。给接话判断等日志复用。"""
    return _display_name("group", group_id)


def _send_log(kind, target_id, chunk):
    """一条实际发出的 QQ 消息打一行日志。kind: group / private。"""
    name = _display_name(kind, target_id)
    label = "群聊" if kind == "group" else "私聊"
    who = "%s(%s)" % (name, target_id) if name else str(target_id)
    log.info("发送 -> %s [%s]: %s", label, who, _preview(chunk))


def _preview(chunk):
    """把一条待发消息压成单行预览：文本取正文，其余段落用占位符。

    吞下三种形态：整条是字符串、段列表（`[{...}]`）、以及**漏了外层的单个
    段**（`{...}`）。最后那种是调用方传参少套一层造成的（`[seg]` 被当成
    「一个 chunk」），遍历它拿到的是键名而不是段，会报
    `'str' object has no attribute 'get'`。预览只是日志，不该有把事情弄挂的
    能力——这里兜住，真正的发送照旧按原样走。
    """
    if isinstance(chunk, str):
        s = chunk
    else:
        if isinstance(chunk, dict):
            chunk = [chunk]
        parts = []
        for seg in chunk:
            t = seg.get("type")
            if t == "text":
                parts.append((seg.get("data") or {}).get("text", ""))
            elif t == "image":
                parts.append("[图片]")
            else:
                parts.append("[%s]" % t)
        s = "".join(parts)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:60] + ("…" if len(s) > 60 else "")


# ─── 当前会话上下文（供工具层读取）────────────────────

# agent 一轮固定在一个线程里跑完，而 execute_tool(name, args) 的签名不便
# 再加参数（会牵动全部工具与既有测试），所以用线程本地变量传递「此刻在为
# 哪个 QQ 会话服务」——与 app/cancel.py 传递取消事件是同一套做法。
_local = threading.local()

# 轮次序号：每 bind_context 一次 +1。工具层靠它判断「这是新一轮还是同一轮里
# 的第 N 次迭代」——`execute_tool(name, args)` 的签名不便再加参数，而
# bind_context 恰好**一轮只调一次**（qq_bot 开跑前），是现成的轮次起点。
# 进程内单调递增，重启归零；只用于相等性比较，不做时间运算。
_turn_seq = itertools.count(1)


def bind_context(session_key, target, target_id, quoted_images=None,
                 user_text=None):
    """绑定当前线程正在处理的 QQ 会话。target 取 "private" / "group"。

    quoted_images 是**本轮**消息引用（reply）到的图片直链，按被引消息里出现
    的先后排。图生图靠它才能落到「对方点名的那一张」：模型在群里看不见图片
    地址（上下文里只有 `[图片]` 占位符），报得出「第几张」却报不出链接，所以
    候选范围必须由 qq_bot 在开跑前圈死。

    user_text 是本轮**对方自己打字的那段话**（不含引用块——引用块里可能整段
    是上一次生图的提示词，拿它判「有没有明说要图生图」会自己骗自己）。
    生图工具靠它拦「一看见引用图就往改图上想」的误判，见
    `generate_image._i2i_gate`。不传 = 这一轮没有可判的原话（网页端）。
    """
    _local.session_key = session_key
    _local.target = target
    _local.target_id = target_id
    _local.quoted_images = list(quoted_images or [])
    _local.turn_id = next(_turn_seq)
    if user_text is not None:
        _local.user_text = str(user_text)


def clear_context():
    """摘掉绑定。worker 线程是复用的，不清理会把上一个会话带进下一轮。"""
    for attr in ("session_key", "target", "target_id", "quoted_images",
                 "turn_id", "user_text"):
        if hasattr(_local, attr):
            delattr(_local, attr)


def current_context():
    """返回 (target, target_id)；不在 QQ 会话里时返回 (None, None)。"""
    return (getattr(_local, "target", None),
            getattr(_local, "target_id", None))


def current_turn_id():
    """当前这一轮的编号；不在 QQ 会话里时返回 0。

    0 是个「永远不会和真编号相等」的哨兵：没绑定上下文时工具本来就发不出去，
    调用方按同一轮处理即可，不必额外分支。
    """
    return getattr(_local, "turn_id", 0)


def current_quoted_images():
    """本轮引用的消息里带的图片直链，按出现顺序；没绑定或没引用时为空表。"""
    return list(getattr(_local, "quoted_images", None) or [])


def current_turn_text():
    """本轮**对方自己打的那段话**；不在 QQ 轮里时返回 None（不是空串）。

    这个 None / "" 的区分是整条判据的地基：

    - `None` = 不在 QQ 会话轮里（网页端、单元测试里直接调工具）——没有原话
      可判，调用方**不该**拿它当「对方没说要图生图」；
    - `""` = 在 QQ 轮里，但对方这轮一个字没打（只发了图 / 只引用了图）——
      这恰恰是「没明说要图生图」，调用方要按没说要处理。
    """
    return getattr(_local, "user_text", None)


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

    if not isinstance(data, dict):
        # POST 已经完成：请求送达且 NapCat 已处理——发送类 action 走到这一步，
        # 消息多半已经发出去了。实测偶发响应不是预期对象（json() 解析成字符串），
        # 之前在这里 data.get 崩掉、被上层当「发送失败」上报，模型便以为图没发
        # 出去，下文接着编「发不出来」——比丢一次返回值严重得多。按成功处理，
        # 原文进日志留证。
        log.warning("OneBot %s 返回非对象响应，按成功处理：%s",
                    action, str(data)[:120])
        return {}

    if data.get("status") == "failed" or data.get("retcode") not in (0, None):
        raise RuntimeError("OneBot 调用失败 %s: %s" % (action, str(data)[:300]))
    return data.get("data") or {}


def check_alive(timeout=5):
    """探活：能取到登录信息就说明协议端在线。"""
    info = _call("get_login_info", timeout=timeout)
    return info.get("user_id"), info.get("nickname")


def get_message(message_id, timeout=10):
    """按 id 取一条历史消息，用于展开「引用」段。

    引用（reply）段里只有一个 message_id，被引用的正文不在这条消息里，
    得回头查。返回的字段与 WS 上报的消息事件**同构**（message /
    raw_message / sender / message_type），所以能直接交给 qq_bot 现有的
    解析逻辑复用，不必另写一套。
    """
    return _call("get_msg", {"message_id": message_id}, timeout=timeout)


def get_forward_msg(forward_id, timeout=10):
    """按 id 取一张「转发的聊天记录」卡片的全部节点。

    多数情况下 forward 段自带 content 数组，用不上这个接口；这里是兜底，
    以及万一 NapCat 某个版本不下发 content 时的退路。
    """
    return _call("get_forward_msg", {"id": forward_id}, timeout=timeout)


# 取名字用的两个接口超时给得很短：它们是纯锦上添花（把 group_123 显示成
# 群名），NapCat 没开时应该立刻放弃，而不是让整个管理页等二十秒。
_NAME_TIMEOUT = 3


def get_group_list(timeout=_NAME_TIMEOUT):
    """机器人加入的群列表，用于把 group_<群号> 显示成群名。"""
    data = _call("get_group_list", timeout=timeout)
    return data if isinstance(data, list) else []


def get_friend_list(timeout=_NAME_TIMEOUT):
    """好友列表，用于把 private_<QQ号> 显示成昵称。"""
    data = _call("get_friend_list", timeout=timeout)
    return data if isinstance(data, list) else []


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

# 协议端拒绝给「非好友」发私聊时的固定话术。QQ 的反骚扰策略：机器人账号不能给
# 陌生人发私聊，必须先加好友。**判据认这些话术、不认 retcode**——retcode 100 太
# 笼统，别的发送失败（风控、频率限制）也用它。
#
# 为什么是**一组**而不是一句（2026-10-01 03:44 实测）：同一秒、同一个非好友
# （胡桃桃 3985441738）、同一个进程，**图和文字拿到的措辞居然不一样**——
#   文字：`发送失败，请先添加对方为好友`（老措辞，result=16）
#   图片：`OIDB error 170019003 on 0x11c5_100: verify identify fail`（新措辞）
# 只认老措辞的代价很大：图这条被判成「真故障」，于是 `_call_private` 既不重试
# 也不探测，直接报失败——可图**明明已经画好了**（Anima_00276_.png，3.89MB），
# 胡桃桃收到的是「图没画出来（OneBot 调用失败 send_private_msg: ）」。
# 0x11c5_100 就是 NapCat 发私聊那条 OIDB 命令，170019003 是它的身份校验失败码，
# 与 result=16 同源：没有可用的私聊通道（不是好友、也没有活跃临时会话）。
FRIEND_REQUIRED_HINTS = (
    "请先添加对方为好友",
    "verify identify fail",
)


def friend_required_error(exc):
    """这个异常是不是「对方不是好友，所以私聊发不出去」？

    2026-09-30 用户报「小小怪无法回复私聊，后台闪一下然后就没有了」，根因就是
    它：机器人**正常处理**了那条私聊，最后一步投递被 QQ 拒了。调用方靠这个函数
    把「策略限制」和「真出故障」分开，前者不该打一整段堆栈。

    2026-10-01 补上第二种措辞（见 `FRIEND_REQUIRED_HINTS` 的注释）——漏认它
    会让「重试 + 群临时会话兜底」整条链路都不启动。
    """
    text = str(exc)
    return any(hint in text for hint in FRIEND_REQUIRED_HINTS)


# ─── 群临时会话（非好友唯一的回复通道）────────────────

# user_id -> 那个群的 group_id。非好友从群里发起临时会话时记下来，回复被
# 「非好友」拒了就带上它重发。**进程内内存即可**：临时会话本身也会过期，
# 没必要落盘。
_temp_group = {}
_temp_lock = threading.Lock()


def note_temp_session(user_id, group_id):
    """记下「这个人是通过哪个群的临时会话找过来的」。

    2026-09-30 实测（胡桃桃 3985441738）：它和机器人共同在 5 个群里，但对 4 个群
    发 `group_id` 都报 `no such temp session`，只有 **1103174141（233的粉丝群）**
    成功——说明临时会话是**按群**存在、**得由对方先发起**的。

    两种写入来源：入站事件里若带 `group_id`（`qq_bot._dispatch` 的钩子，实测
    **通常不带**）会直接记；记不上也没关系，`_probe_temp_group` 会挨个群试出来。
    """
    if not user_id or not group_id:
        return
    with _temp_lock:
        _temp_group[str(user_id)] = str(group_id)


def temp_group_of(user_id):
    """这个人上次是从哪个群发起的临时会话；没有就返回 None。"""
    with _temp_lock:
        return _temp_group.get(str(user_id))


def forget_temp_session(user_id):
    """忘掉记着的那个群。

    **临时会话会过期**（2026-10-01 00:0x 实测：23:5x 还通的 1103174141，过一会儿
    就回 `no such temp session` 了）。记住的群一旦失效，如果还拿它去发，就会
    **每次都失败、而且永远不去重新探测**——所以失效时必须主动忘掉它。
    """
    with _temp_lock:
        _temp_group.pop(str(user_id), None)


# 「这个群对这个用户没有活跃临时会话」——试错探测时靠它区分「换个群再试」和
# 「真出故障了」。
NO_TEMP_SESSION_HINT = "no such temp session"


def _is_no_temp_session(exc):
    return NO_TEMP_SESSION_HINT in str(exc)


# 机器人所在的群号，缓存 5 分钟。临时会话探测要挨个群试，每次现拉群列表太浪费。
_group_cache = {"ts": 0.0, "ids": []}


def _bot_groups():
    """机器人所在的群号列表；取不到返回空列表（探测就跳过，不影响正常发送）。"""
    now = time.time()
    if _group_cache["ids"] and now - _group_cache["ts"] < 300:
        return list(_group_cache["ids"])
    try:
        groups = get_group_list()
    except Exception:
        log.debug("取群列表失败，临时会话探测跳过", exc_info=True)
        return []
    ids = [str(g.get("group_id")) for g in groups if g.get("group_id")]
    _group_cache.update(ts=now, ids=ids)
    return list(ids)


def _probe_temp_group(user_id, message, timeout=30):
    """不知道对方是从哪个群来的，就**挨个群试**一遍。成功返回 True。

    为什么必须试：临时会话是**按群**存在的（2026-09-30 实测：胡桃桃和机器人共同在
    5 个群里，只有 233的粉丝群 那个通），而**入站事件里根本不带 group_id**——
    加了「收到就记下群号」的钩子之后跑了两轮真实私聊，日志里 `群临时会话` 一次都
    没出现过。所以指望入站事件告诉我们用哪个群是行不通的，只能试。

    试错是**安全**的：对没有会话的群，QQ 直接回 `no such temp session`，
    **消息不会真的发出去**（实测对 4 个群试都是这个错，没有任何人收到东西）。
    成功的那个记下来，下次直接用，不用再试。
    """
    for gid in _bot_groups():
        try:
            _call("send_private_msg",
                  {"user_id": int(user_id), "group_id": int(gid),
                   "message": message}, timeout=timeout)
        except RuntimeError as exc:
            # 两种措辞都等于「这个群没有会话」（见 FRIEND_REQUIRED_HINTS），
            # 换下一个；别的错（风控/超时）照抛，别吞。
            if _is_no_temp_session(exc) or friend_required_error(exc):
                continue
            raise
        note_temp_session(user_id, gid)
        log.info("试出群临时会话：%s 在群 %s，已记住", user_id, gid)
        return True
    return False


def _call_private(user_id, message, timeout=30):
    """发一条私聊；被「非好友」拒了就改用**群临时会话**重发。

    QQ **不允许**给非好友直接发私聊（result=16「请先添加对方为好友」），但如果
    对方是从某个群发起的**临时会话**，带上那个群的 `group_id` 就能发出去——
    NapCat 支持这个参数（OneBot 11 的 `send_private_msg.group_id`）。

    **只在这一种错误上换路子**：别的失败照抛，免得把真故障掩盖成「对方不是好友」。

    记住的群**过期了要重新探测**：临时会话有有效期，缓存里的群可能已经失效；
    这时如果还傻乎乎拿它发，就会每次都失败、永远不探测（2026-10-01 修的就是这个）。
    """
    try:
        return _call("send_private_msg",
                     {"user_id": int(user_id), "message": message}, timeout=timeout)
    except RuntimeError as exc:
        if not friend_required_error(exc):
            raise
        gid = temp_group_of(user_id)
        if gid is not None:
            log.info("私聊被拒（%s 还不是好友），用已知的群临时会话重发（群 %s）",
                     user_id, gid)
            try:
                return _call("send_private_msg",
                             {"user_id": int(user_id), "group_id": int(gid),
                              "message": message}, timeout=timeout)
            except RuntimeError as exc2:
                # 带上 group_id 还报「不是好友」也是同一种「这个群没会话」——
                # 措辞可能是 `no such temp session`，也可能又是
                # `verify identify fail`（QQ 两种都用，见 FRIEND_REQUIRED_HINTS）。
                # 只认前一种的话，撞上后一种就会在这里直接抛出去，永远不再探测。
                if not (_is_no_temp_session(exc2)
                        or friend_required_error(exc2)):
                    raise
                # 记着的这个群会话过期了——忘掉它，落到下面重新探测
                forget_temp_session(user_id)
                log.info("记着的临时会话群 %s 已过期，忘掉它，改为重新逐群探测", gid)
        # 还不知道是哪个群（入站事件不带 group_id）——挨个试，试到就记住。
        log.info("私聊被拒（%s 还不是好友），不知道是哪个群，开始逐群探测", user_id)
        if _probe_temp_group(user_id, message, timeout):
            return {}
        raise


def send_private(user_id, message, limit=None):
    """给某个好友发消息（自动分段）。返回发出的条数。

    非好友直接私聊会被 QQ 拒；能救的情况由 `_call_private` 兜底（群临时会话）。
    **逐条兜底**而不是整段重发：这样已经发出去的段不会重复发一遍。
    """
    chunks = message if isinstance(message, list) else split_message(
        to_qq_text(message), limit)
    for i, chunk in enumerate(chunks):
        if i:
            time.sleep(_SEND_INTERVAL)
        _call_private(user_id, chunk)
        _send_log("private", user_id, chunk)
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
        _send_log("group", group_id, chunk)
    return len(chunks)


def send_image(target, target_id, image_path, caption=""):
    """发一张图（可选带一句文字）。target 取 "private" 或 "group"。

    私聊走 `_call_private` 同一套「非好友 → 群临时会话」兜底。**图这条尤其要兜**：
    对方看到「在画了」然后什么都没有，比文字发不出去更让人干等。
    """
    segs = []
    if caption:
        segs.append(text_segment(to_qq_text(caption)))
    segs.append(image_segment(image_path))
    if target == "private":
        _call_private(target_id, segs, timeout=60)
    else:
        _call("send_group_msg",
              {"group_id": int(target_id), "message": segs}, timeout=60)
    _send_log(target, target_id, segs)
    return 1
