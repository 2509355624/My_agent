"""
QQ 接入适配层（NapCat / OneBot 11）

独立进程运行：

    python -m app.qq_bot

不并进 app/main.py 的理由：QQ 掉线重连、协议端重启、扫码失效这些事不该
影响网页端；反过来改网页端代码要重启，也不该把 QQ 连接一起断掉。两边共享
同一套 app.* 代码，只是进程分开。

链路：

    QQ → NapCat(:3001 WS 推事件) → 本模块 → run_agent_stream
                                          → NapCat(:3000 HTTP) → QQ

几条设计取舍：

1. **一条会话线 = 一个 session_key**。私聊是 private_<QQ号>，群聊是
   group_<群号>，各自落 agents/qq/sessions/<key>.jsonl。这样群里张三说的话
   和李四的上下文不会串，私聊也不会串到群里去。
2. **同一条会话线永远串行**。模型回复有先后顺序，并发跑会让后一句先回、
   上下文还互相干扰；不同会话线之间才并行。
3. **排队期间的消息合并**。群里连发三句会攒成一条一起送进去，而不是白跑
   三轮 LLM（也避免机器人刷屏式地回三条）。
4. **本轮调用过 send_qq_message 就不再自动回发正文**。否则同一句话会被
   发两遍。
"""

import asyncio
import inspect
import json
import logging
import os
import re
from urllib.parse import quote

try:
    import msvcrt                      # Windows 文件锁，用于单实例保护
except ImportError:                    # 非 Windows 平台退化为不做检查
    msvcrt = None

from app import interject, longterm, qq_api, recent, stickers
from app.agent import run_agent_stream
from app.agent_prompt import build_stable_prompt, sync_session_system
from app.config import (
    BASE_DIR, COMFYUI_URL, QQ_AGENT_ID, QQ_BOT_NAME, QQ_BLACKLIST_USERS,
    QQ_CONTEXT_MAX_CHARS, QQ_CONTEXT_MESSAGES,
    QQ_DEBOUNCE_SECONDS, QQ_GROUP_AT_ONLY, QQ_GROUP_KEYWORDS,
    QQ_MAX_CONCURRENCY, QQ_MEMORY_INJECT_LIMIT, QQ_MEMORY_INJECT_MAX_CHARS,
    QQ_PENDING_MAX_CHARS, QQ_PENDING_MAX_ITEMS,
    QQ_PRIVATE_ENABLE, QQ_QUOTE_MAX_CHARS, QQ_TOKEN, QQ_WHITELIST_GROUPS,
    QQ_WHITELIST_USERS, QQ_WS_URL,
)
from app.memory import load_history, save_history

try:
    from websockets.asyncio.client import connect as ws_connect
except ImportError:          # 旧版 websockets 的兼容路径
    from websockets import connect as ws_connect

log = logging.getLogger("qq_bot")

# 首次连接失败后的重试间隔（秒），指数退避到上限。NapCat 启动后需要先扫码
# 登录，3001 要等登录成功才监听，所以适配层刚起来时连不上属于常态。
_RECONNECT_MIN = 5
_RECONNECT_MAX = 60

# websockets 用 proxy=None 显式关掉代理，旧版本没有这个参数。不关代理会被
# 本机的 Clash 之类劫持：连 ws://127.0.0.1 的握手会被送去代理，换回
# 502 InvalidProxyStatus 并让进程当场退出。
try:
    _WS_SUPPORTS_NO_PROXY = "proxy" in inspect.signature(ws_connect).parameters
except (TypeError, ValueError):
    _WS_SUPPORTS_NO_PROXY = False

# 与 app/main.py 的 SESSION_EVENTS 同义：这几类事件出流时消息已写进 history，
# 所以看到就要落盘（生图可能阻塞很久，不落盘的话进程被杀会丢整轮）
SESSION_EVENTS = ("user", "assistant", "tool_result", "aborted")

# 生图工具返回的是相对地址 /api/image/<file>，这里把它换成协议端能直接拉的
# ComfyUI 地址。不用网页端的 /api/image 是因为适配层是独立进程，不该依赖
# 网页端 Flask 同时开着。
_IMAGE_PATH_RE = re.compile(r"/api/image/([^\s)\"'，。]+)")
_CQ_RE = re.compile(r"\[CQ:([a-z_]+)((?:,[^\]]*)?)\]")


# ─── 会话线的 system 头 ──────────────────────────────

def _ensure_system_prompt(session_key):
    """把这条会话线的首条固定为稳定的 system 消息（prefix cache 锚点）。

    与 app/main.py 里的同名函数同构，差别只在多传一个 session_key。
    """
    stable = build_stable_prompt(QQ_AGENT_ID)
    keep = [m for m in load_history(QQ_AGENT_ID, session_key)
            if m.get("role") != "system"]
    save_history([{"role": "system", "content": stable}] + keep,
                 QQ_AGENT_ID, session_key)


# ─── 事件解析 ────────────────────────────────────────

def _cq_arg(rest, key):
    """从 CQ 码的参数串里取一个值（`...,url=http://x,y=1` 这种形式）。"""
    for part in (rest or "").lstrip(",").split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            if k.strip() == key:
                return v.strip()
    return ""


def _parse_segments(ev):
    """从事件里取出文本、@ 标记、以及图片地址列表。

    OneBot 有两种上报格式：message 数组（结构化）和 raw_message 字符串
    （CQ 码）。NapCat 配的是哪种都要能认，所以两条路都走。

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
                # 商城表情包：NapCat 一般带 url，能当普通图收（表情库/识图都认）
                url = str(sdata.get("url") or "")
                if url:
                    images.append(url)
                else:
                    parts.append("[表情]")
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
    body = text or ("（图片）" if images else "（没有可读内容）")
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
    head = "[引用 %s 的消息] " % who if who else "[引用的消息] "
    return head + body, images


def _session_key(target, target_id):
    return "%s_%s" % (target, target_id)


# _should_reply 拒绝的原因之一，单独提出来是因为 _dispatch 要按它分流：
# 只有「没被 @、也没命中触发词」这一类才值得再问一句「那我要不要主动接
# 一句」——它意味着「这条不是冲机器人来的，但也许可以搭个话」。别的拒绝
# 理由（黑名单、群不在白名单、@ 了却什么都没发）都该照旧丢掉。
REASON_NO_MENTION = "未 @ 且未命中关键词"

# 主动接话时往前翻多少条消息找「最近一张图」。判断模型的上下文是
# QQ_INTERJECT_CONTEXT_MESSAGES(12) 条，图再老多半已经不在当前话题里了。
_INTERJECT_IMAGE_LOOKBACK = 12


def _should_reply(ev, target, target_id, text, at_me, has_image=False,
                  has_quote=False):
    """判定这条消息要不要回。返回 (bool, 原因)，原因只用于日志。

    has_image / has_quote 参与判定的理由：@ 了机器人却只发一张图、或者只
    引用一条消息不写字（"这句怎么回"），都是常见用法，以前只看 text 会把
    它们整个丢掉。注意「有内容」判据是 `text or has_image or has_quote`
    ——这些也算内容，但**不 @ 的群里仍然不看**，触发规则没放宽。
    """
    user_id = str(ev.get("user_id", ""))
    has_content = bool(text) or bool(has_image) or bool(has_quote)

    if user_id in QQ_BLACKLIST_USERS:
        return False, "在黑名单里"

    if target == "private":
        if not QQ_PRIVATE_ENABLE:
            return False, "私聊未开启"
        if QQ_WHITELIST_USERS and user_id not in QQ_WHITELIST_USERS:
            return False, "不在私聊白名单"
        return (has_content, "私聊")

    group_id = str(target_id)
    if QQ_WHITELIST_GROUPS and group_id not in QQ_WHITELIST_GROUPS:
        return False, "群不在白名单"

    if at_me:
        return (has_content, "被 @")
    for kw in QQ_GROUP_KEYWORDS:
        if kw in text:
            return True, "命中关键词 " + kw
    if not QQ_GROUP_AT_ONLY:
        return (has_content, "群全量模式")
    return False, REASON_NO_MENTION


# ─── 一批消息的合并 ──────────────────────────────────

def _merge_batch(batch, max_items=None, max_chars=None, prefix=True):
    """把攒下来的一批消息拼成一条文本，并施加两道闸。

    静默窗口只负责「等连发到齐」，它自己不限制攒下多少：群里被刷屏时
    _pending 会一直涨，直接 join 出来的那一条会长到离谱，一次全灌进模型
    （既撑爆上下文预算，也把真正有用的近期内容冲淡）。所以这里只取最近的：

    - 条数上限：超过就只保留最后 max_items 条；
    - 字数上限：从最新往前装，装不下就停，更旧的丢掉。

    **最新的那条永远保留**，哪怕它自己就超过字数上限——否则会把用户刚说
    的话整个吞掉，那比超长更糟。两个上限 <=0 表示该项不限制。

    prefix=True（群聊）时给消息署名：合并窗口里可能混着好几个人的消息，
    谁说的哪句必须跟着走——尤其纯图消息，不署名模型会把图安到正好在
    说话的那个人头上。dispatch 已给「要回的」文本加过前缀，startswith
    挡住重复；主动接话入队的（tentative）没加过，在这里补上。
    
    偶尔有一条解析不出发送者（协议端没给名片/昵称）。这时跟着上一条已知的
    人走——空行会被模型当成「不知道谁说的」，进而把话安错人。按时间正序过
    一遍才能补，所以署名先算完，再走下面的截断（截断是从新往旧取的）。
    """
    if max_items is None:
        max_items = QQ_PENDING_MAX_ITEMS
    if max_chars is None:
        max_chars = QQ_PENDING_MAX_CHARS

    # 只发图不打字、只引用不写字的消息也算一条，不能按 text 过滤掉，否则
    # 「@机器人 + 一张图」「引用一句 + 直接发送」会被整条丢弃。图的下载与
    # 张数上限在 _run_turn / _prepare_images 里管，引言在 _resolve_quote 里拉。
    items = [x for x in batch
             if x.get("text") or x.get("images") or x.get("quotes")]
    if max_items > 0 and len(items) > max_items:
        log.info("待处理 %d 条，只取最近的 %d 条", len(items), max_items)
        items = items[-max_items:]

    # 第一步：按时间正序算好每行的署名（无名行跟着上一条已知的人）
    staged, last_who = [], ""
    for it in items:
        t = it["text"]
        who = it.get("sender") or ""
        if who:
            last_who = who
        owner = who or (last_who if prefix else "")
        n_img = len(it.get("images") or [])
        if prefix and owner and t and not t.startswith(owner + "："):
            t = owner + "：" + t
        if n_img:
            # 图的署名跟着消息走：有字的在句尾标张数，纯图的给一行占位
            if t:
                t += "（发了%d张图）" % n_img
            elif prefix and owner:
                t = owner + "：[图片]"
            else:
                t = "[图片]"
        staged.append(t)

    # 第二步：从最新往回装，装不下就停（最新的那条一定在）
    lines, total = [], 0
    for t in reversed(staged):
        if lines and max_chars > 0 and total + len(t) > max_chars:
            break
        if t:
            lines.append(t)
        total += len(t)
    lines.reverse()
    return "\n".join(lines).strip()


# ─── 一条会话线的串行执行器 ──────────────────────────

class SessionRunner:
    """把同一条会话线上的消息排队、合并、串行交给 agent。

    _pending 攒消息，_loop 在静默窗口结束后一次性取走——这样群里连发三句
    只会跑一轮。取走时经 _merge_batch 施加条数与字数上限，防刷屏灌爆。
    不同 SessionRunner 之间互不影响，并发上限由外部信号量控制。
    """

    def __init__(self, bot, session_key, target, target_id):
        self.bot = bot
        self.session_key = session_key
        self.target = target
        self.target_id = target_id
        self._pending = []
        self._task = None

    def submit(self, text, sender_name="", images=None, quotes=None,
               tentative=False):
        """tentative=True 表示「这条没 @ 机器人、也没命中触发词」——它不是
        非回不可的消息，要不要开口得先问一次判断模型（见 _run_turn 开头）。
        """
        item = {"text": text, "sender": sender_name,
                "images": images or [], "quotes": quotes or [],
                "tentative": bool(tentative)}
        self._pending.append(item)
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    async def _loop(self):
        while self._pending:
            # 静默窗口：等连发的后续消息到齐再一起处理
            await asyncio.sleep(QQ_DEBOUNCE_SECONDS)
            batch, self._pending = self._pending[:], []
            if not batch:
                continue
            try:
                async with self.bot.sem:
                    await asyncio.to_thread(self._run_turn, batch)
            except Exception:
                log.exception("处理 %s 的会话时出错", self.session_key)

    @staticmethod
    def _prepare_images(urls):
        """下载图片并转成 data URL，返回 (可用列表, 被丢弃的张数)。

        单张失败只记日志、跳过——一张图挂了不该让整轮对话停住，这正是
        「看不到这张图」的降级路径（模型会如实说看不到，而不是编内容）。
        """
        from app.config import QQ_IMAGE_MAX_COUNT
        from app.vision import fetch_image, to_data_url

        if not urls:
            return [], 0
        picked = urls[:QQ_IMAGE_MAX_COUNT] if QQ_IMAGE_MAX_COUNT > 0 else urls
        out = []
        for i, url in enumerate(picked, 1):
            try:
                out.append(to_data_url(fetch_image(url)))
            except Exception as exc:
                log.warning("取图失败（第 %d 张）：%s", i, exc)
        return out, len(urls) - len(picked)

    def _should_interject(self):
        """这批消息没点名机器人，问一次判断模型：此刻值不值得主动开口。

        跑在 worker 线程里（调用方已经是 to_thread）。不放在 _dispatch 是有
        意的：模型首次加载要几十秒，堵在 WS 消息循环上会把所有群一起卡住。
        """
        verdict = interject.decide(QQ_AGENT_ID, self.target_id)
        if not verdict or not verdict["pass"]:
            return False
        return interject.speaking()

    def _run_turn(self, batch):
        """在 worker 线程里跑一轮（run_agent_stream 是同步生成器）。"""
        # 表情包收藏：群图趁链接活着赶紧落盘，跟「这轮回不回」无关——
        # 接话被拒的轮次里出现的图也照收。下载是阻塞 IO，好在已经在
        # worker 线程上。失败只记日志，别让它碰倒整轮对话。
        if self.target == "group":
            try:
                stickers.collect(QQ_AGENT_ID,
                                 [(u, it.get("sender") or "")
                                  for it in batch
                                  for u in (it.get("images") or [])])
            except Exception:
                log.exception("表情包收藏出错 %s", self.session_key)

        # 整批都是「没点名机器人」的消息时，先让判断模型决定要不要开口。只要
        # 混进一条 @ 或命中触发词的，就照常回，不必问。
        voluntary = all(it.get("tentative") for it in batch)
        if voluntary and not self._should_interject():
            return
        # 回复对象不在协议层指定——模型自己在正文里称呼人（"233，你说的
        # 我知道呀"），上下文里每条消息都带昵称，它天然知道在跟谁说话。
        if voluntary:
            # 主动开口不是「回复谁」——那些群消息也不是对它说的，所以不能当
            # 正文喂进去，否则模型会以为自己被问了。上下文由 extra_context
            # 的群聊背景负责，这里只留一句说明。
            batch = [{"text": interject.INTERJECT_PROMPT, "sender": "",
                      "images": [], "quotes": []}]
            # 判断模型看不见图（上下文里图只是 "[图片]" 占位符）。它判「接」
            # 往往就是好奇那张图——把最近两张真正捞出来给主模型看，不然只能
            # 对着看不见的东西装懂。两张是因为群里经常连着甩表情，光看最新
            # 一张常常不够。每张都带上「是谁发的」：不署名的话，模型会把它
            # 安到最近在发言的那个人头上。
            recs = recent.recent_image_records(QQ_AGENT_ID, self.target_id,
                                               _INTERJECT_IMAGE_LOOKBACK, 2)
            if recs:
                batch[0]["images"] = [r["m"] for r in recs]
                owners = []
                for r in recs:
                    who = r.get("n") or r.get("u") or ""
                    if who and who not in owners:
                        owners.append(who)
                if owners:
                    batch[0]["text"] += "\n（最近的 %d 张图片是 %s 发的）" \
                        % (len(recs), " 和 ".join(owners))

        text = _merge_batch(batch, prefix=(self.target == "group"))
        # 图片段单独收集：只发图不打字是合法用法（"帮我看下这个"），
        # 不能因为 text 为空就把整轮丢掉。owner 与 image_urls 一一对应，
        # 识图文字块靠它写清「谁发的图」，否则模型会把图安错人。
        image_urls = []
        image_owners = []
        for it in batch:
            who = it.get("sender") or ""
            for u in (it.get("images") or []):
                image_urls.append(u)
                image_owners.append(who)

        # 引用/转发的正文不在这条消息里，得回头问协议端。放在这里而不是
        # _dispatch 里，是因为那时还没判定「这条要不要回」——否则群里每来
        # 一条消息都要白跑一次 HTTP。这也是 IO，必须在 worker 线程上做。
        quote_blocks, quote_images = [], []
        for it in batch:
            who = it.get("sender") or ""
            for q in (it.get("quotes") or []):
                block, q_images = _resolve_quote(q, QQ_QUOTE_MAX_CHARS)
                if not block:
                    continue
                # 署名必须跟着「引用这条消息的人」，不能只写被引的人——
                # 否则模型看到一段没有主人的引用，只能猜是谁在说话。
                if who and not block.startswith(who + "："):
                    block = who + "：" + block
                quote_blocks.append(block)
                quote_images.extend(q_images)
        # 引言里的图是被引那条消息里的，主人不在合并窗口里，宁可留空也不乱安
        image_urls = quote_images + image_urls
        image_owners = [""] * len(quote_images) + image_owners

        # 点收来源：本轮见过的图（消息本体的 + 引用块里的）记进「最近图片」
        # 缓冲，模型调 collect_sticker 时按「最近第几张」取链。引用里的图
        # 只走识图、不进自动收藏，这条缓冲是它唯一的落点。
        for _u, _w in zip(image_urls, image_owners):
            try:
                stickers.note_image((self.target, self.target_id), _u, _w)
            except Exception:
                pass
        if quote_blocks:
            text = "\n\n".join(quote_blocks + ([text] if text else []))

        if not text and not image_urls:
            return

        data_urls, dropped = self._prepare_images(image_urls)
        if dropped > 0:
            text = (text + "\n（另有 %d 张图片超过单条上限，未读取）"
                    % dropped).strip()
        if not text and not data_urls:
            return

        history = load_history(QQ_AGENT_ID, self.session_key)
        if not history or history[0].get("role") != "system":
            _ensure_system_prompt(self.session_key)
            history = load_history(QQ_AGENT_ID, self.session_key)
        elif sync_session_system(QQ_AGENT_ID, self.session_key):
            # 人设/配置变了，首条 system 已被替换 → 重新读一份带新头的历史。
            # 没变时 sync 返回 False，一个字节都没动过，前缀缓存不受影响。
            history = load_history(QQ_AGENT_ID, self.session_key)

        # 群里垫一层「刚才在聊什么」的背景，让回复接得上话，而不是干巴巴地
        # 只答那一句。走 extra_context 而不是拼进 text：拼进 text 会写进会话
        # 历史，每轮重复堆一份，十几轮就把预算占满；这条通道每轮现取现用、
        # 出流即弃（与状态栏同一处理，见 app/agent.py 的 _status_message）。
        extra_context = ""
        if self.target == "group":
            extra_context = recent.format_recent(
                QQ_AGENT_ID, self.target_id,
                QQ_CONTEXT_MESSAGES, QQ_CONTEXT_MAX_CHARS)
            # 长期记忆：最近几条「以前聊过什么」的摘要跟在短背景后面。
            # 走同一条 extra_context 通道——不写回 history，出流即弃。
            mem = longterm.format_memories(
                QQ_AGENT_ID, self.target_id,
                QQ_MEMORY_INJECT_LIMIT, QQ_MEMORY_INJECT_MAX_CHARS)
            if mem:
                extra_context = (extra_context + "\n\n" + mem
                                 if extra_context else mem)
        # 表情包清单：把整库目录亮给模型，看图挑编号自己发。挂在同一条
        # extra_context 通道，出流即弃。私聊也注入——库是全 agent 共享的，
        # 私聊里照样可以甩群里收的表情。
        menu = stickers.catalog(QQ_AGENT_ID)
        if menu:
            extra_context = (extra_context + "\n\n" + menu
                             if extra_context else menu)

        # 工具层靠线程本地变量知道「此刻在为哪个会话服务」，
        # send_qq_message 不带参数时就发回这里
        qq_api.bind_context(self.session_key, self.target, self.target_id)
        sent_by_tool = False
        reply_parts, images = [], []

        try:
            for ev in run_agent_stream(text, history, agent_id=QQ_AGENT_ID,
                                       image=data_urls or None,
                                       image_owners=image_owners or None,
                                       extra_context=extra_context or None):
                etype = ev.get("type")
                # 先落盘再处理（与 main.py 的契约一致）
                if etype in SESSION_EVENTS:
                    try:
                        save_history(history, QQ_AGENT_ID, self.session_key)
                    except Exception:
                        log.exception("落盘失败 %s", self.session_key)
                if etype == "assistant":
                    reply_parts.append(ev.get("content", ""))
                elif etype == "tool_result":
                    if ev.get("name") == "send_qq_message":
                        sent_by_tool = True
                    elif ev.get("name") == "generate_image":
                        images.extend(
                            _IMAGE_PATH_RE.findall(str(ev.get("result") or "")))
        except Exception:
            log.exception("agent 循环异常 %s", self.session_key)
            reply_parts.append("（这边出了点问题，稍后再试）")
        finally:
            qq_api.clear_context()
            try:
                save_history(history, QQ_AGENT_ID, self.session_key)
            except Exception:
                log.exception("收尾落盘失败 %s", self.session_key)

        self._deliver(sent_by_tool, "".join(reply_parts), images)

    def _deliver(self, sent_by_tool, reply, images):
        """把结果发回 QQ。图片走 ComfyUI 的 /view 地址。"""
        send_text = (qq_api.send_group if self.target == "group"
                     else qq_api.send_private)
        spoke = False

        if not sent_by_tool and reply.strip():
            try:
                send_text(self.target_id, reply)
                spoke = True
            except Exception:
                log.exception("回发文字失败 %s", self.session_key)

        for name in images:
            url = COMFYUI_URL.rstrip("/") + "/view?filename=" + quote(name)
            try:
                qq_api.send_image(self.target, self.target_id, url)
                spoke = True
            except Exception:
                log.exception("回发图片失败 %s", self.session_key)

        # 只要它真的开了口，两件事跟着来（只对群聊）：
        # 1) 冷却重新计时——30 秒管的是这张嘴，被 @ 的回复也算说话，否则
        #    刚回完就接话，接出来的内容跟刚回的撞车；
        # 2) 发言进群聊缓存——背景里看不到自己刚说过什么，模型就会换个
        #    说法复读上一句（实测复读过）。
        if spoke and self.target == "group":
            interject.mark_spoke(QQ_AGENT_ID, self.target_id)
            recent.remember(QQ_AGENT_ID, self.target_id, QQ_BOT_NAME, reply)


# ─── 适配层主体 ──────────────────────────────────────

class QQBot:
    def __init__(self):
        self.runners = {}
        self.sem = None

    def _runner_for(self, session_key, target, target_id):
        runner = self.runners.get(session_key)
        if runner is None:
            runner = SessionRunner(self, session_key, target, target_id)
            self.runners[session_key] = runner
        return runner

    def _dispatch(self, raw):
        """处理一条 WS 事件。只认消息事件，其余（心跳、通知）一律忽略。"""
        try:
            ev = json.loads(raw)
        except (ValueError, TypeError):
            return
        if not isinstance(ev, dict) or ev.get("post_type") != "message":
            return

        mtype = ev.get("message_type")
        if mtype == "group":
            target, target_id = "group", str(ev.get("group_id", ""))
        elif mtype == "private":
            target, target_id = "private", str(ev.get("user_id", ""))
        else:
            return
        if not target_id or target_id == "0":
            return

        text, at_me, image_urls = _parse_segments(ev)
        quotes = _extract_quotes(ev)
        self_id = str(ev.get("self_id", ""))
        user_id = str(ev.get("user_id", ""))
        sender = ((ev.get("sender") or {}).get("card")
                  or (ev.get("sender") or {}).get("nickname") or "")

        # 群消息**一律**先记进「最近群聊」缓存，不管回不回、也不管是不是自己
        # 发的。原先不 @ 机器人的消息在这里就直接丢了，模型每轮只看得到「有人
        # 问了它一句」，所以只能一问一答。记在判定之前是有意的——要回的那条
        # 同样是群聊的一部分（它另外还会作为正文进会话历史）。
        if target == "group" and (text or image_urls):
            recent.remember(QQ_AGENT_ID, target_id, sender or user_id,
                            text or "[图片]", user_id,
                            image=(image_urls[0] if image_urls else ""))

        # 自己发的消息也会被上报（reportSelfMessage），要跳过，否则会自问自答。
        # 放在记缓存之前会把「自己说过的话」从上下文里挖掉，所以放它后面。
        if self_id and user_id == self_id:
            return

        ok, reason = _should_reply(ev, target, target_id, text, at_me,
                                   bool(image_urls), bool(quotes))
        if not ok:
            # 只有「没被 @、也没命中触发词」这一类才交给主动接话——它意味着
            # 「这条不是冲机器人来的，但也许能搭个话」。@ 了却什么都没发、
            # 在黑名单、群不在白名单这些照旧丢掉：前者说明对方还没说完或按错
            # 了，让机器人凭空开口很奇怪；后两者是用户明确划的界。
            #
            # 判断不放这儿——_dispatch 跑在 WS 消息循环上，而这里要调模型，
            # 会把所有群一起卡住。所以照常入队，由 worker 线程在解抖窗口之后
            # 决定要不要开口（见 SessionRunner._run_turn）。
            if reason == REASON_NO_MENTION and interject.enabled():
                self._runner_for(
                    _session_key(target, target_id), target, target_id
                ).submit(text, sender, image_urls, quotes, tentative=True)
            else:
                log.debug("跳过 %s %s：%s", target, target_id, reason)
            return

        session_key = _session_key(target, target_id)

        # 群里要带上说话人，否则模型不知道是谁在问；私聊不用。
        # 没有文字时不加——「引用一条 + 不写字」的正文本来就是空的，硬拼
        # 出一个「张三：」只会让模型看到一行没有内容的归属标记。
        if target == "group" and sender and text:
            text = sender + "：" + text

        extra = ""
        if image_urls:
            extra += " +%d 张图" % len(image_urls)
        if quotes:
            extra += " +%d 条引用" % len(quotes)
        log.info("← %s %s（%s）: %s%s", target, target_id, reason,
                 text[:60].replace("\n", " "), extra)
        self._runner_for(session_key, target, target_id).submit(
            text, sender, image_urls, quotes)

    async def _probe(self):
        """启动时探一下协议端在不在，不在就只警告、照常去连 WS。"""
        try:
            uid, nick = await asyncio.to_thread(qq_api.check_alive)
            log.info("协议端在线：%s（%s）", nick, uid)
        except Exception as e:
            log.warning("协议端探活失败：%s", e)

    async def run(self):
        self.sem = asyncio.Semaphore(QQ_MAX_CONCURRENCY)
        log.info("QQ 接入启动：agent=%s，事件源=%s", QQ_AGENT_ID, QQ_WS_URL)
        await self._probe()

        kwargs = {"proxy": None} if _WS_SUPPORTS_NO_PROXY else {}
        if QQ_TOKEN:
            kwargs["additional_headers"] = {"Authorization": "Bearer " + QQ_TOKEN}

        # 两层重连各管一件事，缺一不可：
        #   - 内层 async for 管「连上之后断开」，它自带指数退避重连；
        #   - 外层 while 管「第一次就没连上」—— 这种失败会让 connect 直接
        #     抛异常冒泡出去，内置重连根本轮不到，进程会当场退出。NapCat
        #     还没扫码登录时正是这种情况。
        delay = _RECONNECT_MIN
        while True:
            try:
                async for ws in ws_connect(QQ_WS_URL, **kwargs):
                    log.info("已连接 %s", QQ_WS_URL)
                    delay = _RECONNECT_MIN    # 连上了就把退避重置回去
                    try:
                        async for raw in ws:
                            self._dispatch(raw)
                    except Exception as e:
                        log.warning("连接断开：%s", e)
                log.warning("连接已结束，%d 秒后重连", delay)
            except Exception as e:
                log.warning("连接失败：%s，%d 秒后重试", e, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, _RECONNECT_MAX)


_SINGLETON_HANDLE = None


def _acquire_single_instance():
    """抢占单实例锁；已有实例在跑时返回 False。

    两个适配层同时连着 NapCat 的 WS 时，同一条 QQ 消息会被两个进程各处理
    一遍 —— 同一个会话里回两遍，而且两份上下文会互相覆盖。Windows 的文件
    锁随进程结束（包括被强杀）由内核自动释放，所以不用担心残留锁文件。

    拿不到锁文件本身（如目录只读）时不拦启动，只记一条警告。
    """
    global _SINGLETON_HANDLE
    if msvcrt is None:
        return True
    path = os.path.join(BASE_DIR, ".qq_bot.lock")
    try:
        handle = open(path, "a+b")
    except OSError as e:
        log.warning("无法创建锁文件 %s（%s），跳过单实例检查", path, e)
        return True
    try:
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        handle.close()
        return False
    _SINGLETON_HANDLE = handle          # 持有引用，避免被 GC 提前关掉
    return True


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if not QQ_AGENT_ID:
        log.error("QQ_AGENT_ID 为空，先配好 .env 再启动")
        return
    if not _acquire_single_instance():
        log.error("已有 QQ 适配层在运行，本次退出。"
                  "要重启请先结束旧进程，或改用 一键启动QQ机器人.bat")
        return
    try:
        asyncio.run(QQBot().run())
    except KeyboardInterrupt:
        log.info("已停止")


if __name__ == "__main__":
    main()
