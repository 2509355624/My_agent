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

from app import qq_api
from app.agent import run_agent_stream
from app.agent_prompt import build_stable_prompt, sync_session_system
from app.config import (
    BASE_DIR, COMFYUI_URL, QQ_AGENT_ID, QQ_BLACKLIST_USERS,
    QQ_DEBOUNCE_SECONDS, QQ_GROUP_AT_ONLY, QQ_GROUP_KEYWORDS,
    QQ_MAX_CONCURRENCY, QQ_PENDING_MAX_CHARS, QQ_PENDING_MAX_ITEMS,
    QQ_PRIVATE_ENABLE, QQ_TOKEN, QQ_WHITELIST_GROUPS,
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
        return ""

    _sub.at_me = False
    return _CQ_RE.sub(_sub, raw).strip(), _sub.at_me, images


def _session_key(target, target_id):
    return "%s_%s" % (target, target_id)


def _should_reply(ev, target, target_id, text, at_me, has_image=False):
    """判定这条消息要不要回。返回 (bool, 原因)，原因只用于日志。

    has_image 参与判定的理由：@ 了机器人却只发一张图（"看看这个"）是常见
    用法，以前只看 text 会把它整个丢掉。注意「有内容」判据是 `text or
    has_image`——图也算内容，但**不 @ 的群里仍然不看图**，触发规则没变宽。
    """
    user_id = str(ev.get("user_id", ""))
    has_content = bool(text) or bool(has_image)

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
    return False, "未 @ 且未命中关键词"


# ─── 一批消息的合并 ──────────────────────────────────

def _merge_batch(batch, max_items=None, max_chars=None):
    """把攒下来的一批消息拼成一条文本，并施加两道闸。

    静默窗口只负责「等连发到齐」，它自己不限制攒下多少：群里被刷屏时
    _pending 会一直涨，直接 join 出来的那一条会长到离谱，一次全灌进模型
    （既撑爆上下文预算，也把真正有用的近期内容冲淡）。所以这里只取最近的：

    - 条数上限：超过就只保留最后 max_items 条；
    - 字数上限：从最新往前装，装不下就停，更旧的丢掉。

    **最新的那条永远保留**，哪怕它自己就超过字数上限——否则会把用户刚说
    的话整个吞掉，那比超长更糟。两个上限 <=0 表示该项不限制。
    """
    if max_items is None:
        max_items = QQ_PENDING_MAX_ITEMS
    if max_chars is None:
        max_chars = QQ_PENDING_MAX_CHARS

    # 只发图不打字的消息也算一条，不能按 text 过滤掉（否则「@机器人 + 一张图」
    # 会被整条丢弃）。图的下载与张数上限在 _run_turn / _prepare_images 里管。
    items = [x for x in batch if x.get("text") or x.get("images")]
    if max_items > 0 and len(items) > max_items:
        log.info("待处理 %d 条，只取最近的 %d 条", len(items), max_items)
        items = items[-max_items:]

    lines, total = [], 0
    for it in reversed(items):
        t = it["text"]
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

    def submit(self, text, sender_name="", images=None):
        item = {"text": text, "sender": sender_name, "images": images or []}
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

    def _run_turn(self, batch):
        """在 worker 线程里跑一轮（run_agent_stream 是同步生成器）。"""
        text = _merge_batch(batch)
        # 图片段单独收集：只发图不打字是合法用法（"帮我看下这个"），
        # 不能因为 text 为空就把整轮丢掉
        image_urls = [u for it in batch for u in (it.get("images") or [])]
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

        # 工具层靠线程本地变量知道「此刻在为哪个会话服务」，
        # send_qq_message 不带参数时就发回这里
        qq_api.bind_context(self.session_key, self.target, self.target_id)
        sent_by_tool = False
        reply_parts, images = [], []

        try:
            for ev in run_agent_stream(text, history, agent_id=QQ_AGENT_ID,
                                       image=data_urls or None):
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

        if not sent_by_tool and reply.strip():
            try:
                send_text(self.target_id, reply)
            except Exception:
                log.exception("回发文字失败 %s", self.session_key)

        for name in images:
            url = COMFYUI_URL.rstrip("/") + "/view?filename=" + quote(name)
            try:
                qq_api.send_image(self.target, self.target_id, url)
            except Exception:
                log.exception("回发图片失败 %s", self.session_key)


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

        # 自己发的消息也会被上报（reportSelfMessage），要跳过，否则会自问自答
        self_id = str(ev.get("self_id", ""))
        if self_id and str(ev.get("user_id", "")) == self_id:
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
        ok, reason = _should_reply(ev, target, target_id, text, at_me,
                                   bool(image_urls))
        if not ok:
            log.debug("跳过 %s %s：%s", target, target_id, reason)
            return

        sender = ((ev.get("sender") or {}).get("card")
                  or (ev.get("sender") or {}).get("nickname") or "")
        session_key = _session_key(target, target_id)

        # 群里要带上说话人，否则模型不知道是谁在问；私聊不用
        if target == "group" and sender:
            text = sender + "：" + text

        log.info("← %s %s（%s）: %s%s", target, target_id, reason,
                 text[:60].replace("\n", " "),
                 (" +%d 张图" % len(image_urls)) if image_urls else "")
        self._runner_for(session_key, target, target_id).submit(
            text, sender, image_urls)

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
