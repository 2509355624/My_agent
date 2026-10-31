"""QQ moss 漫画：一句话开一条线，引用上一页往下续。

## 为什么不用 comic_agent

`comic_agent` 那套是给 Anima 写的：编剧按 Danbooru 标签出分镜、逐格渲染、
后期再用 qwen 贴气泡。moss 的文本编码器是 Qwen3.5-9B，吃自然语言、不吃
`(tag:1.2)` 那套加权（工作流里连 LoRA 槽都空着），所以这边直接走
`moss_comic`：编剧模型写剧本 → 脚本拼整页提示词 → **一页一次生成**。

一页默认六格 `2t4b`（上排两格 512×768 建场 + 下排四格 512×384 连打反应）、
1024×1536 / 30 步 / cfg 3.5。
2026-10-31 起**用户可以在需求里点名格数**（「四格」「五格」「上面一大格」），
认到就钉死，不认才走 `LAYOUT` 默认值——见 `detect_layout`。

## 连载：四个词，**必须带 `moss` 前缀**（2026-10-31 用户拍板的形态）

一开始这里打算做七个指令（`moss续集 5 <剧情>` 之类），被用户否了。
中间试过砍成裸词 `续集` / `重画` / `合集`（靠「有没有引用」来判断），
**当晚又被用户推翻了**，理由很实在（原话）：

    你不能说续集、重画、合集，我不能这么说了，你必须得给我加上 moss。
    这 4 个字母你必须给我加上来，这样我才能够精确地带走代码闸门。

裸词的危险不在误触发，而在**闸门会去猜**：群里说「这个续集不错」，
代码得先翻账本看有没有引用才敢决定接不接——猜错了白跑一张图，
猜漏了用户说「我明明引用了」。带上前缀就没有猜的余地：

    moss漫画 <剧情>     新开一条线
    moss续集 <剧情>     从**引用那一页**往下接
    moss重画            引用那一页，换个种子重画
    moss合集            把这条线整串拼成聊天记录卡片
    moss漫画说明书       完整玩法（常量直回，零调用）

## 触发门槛：群里得先叫一声（2026-10-31 用户报的误触发）

回执和菜单本身就把 `moss续集` / `moss合集` / `更多渠道` / `使用指南` 这些词
印在群聊里，谁复述一句「moss合集 挺好用」就被当成指令。所以**四个词统一
要求**：真 @，或者句子里带「大大怪」。判据统一放在 `app/trigger.py`
（`direct_gen` 和 `qq_bot` 用的是同一套，不许各写一份）；私聊没有 @ 这回事，
由调用方传 `at_me=True` 放行。

宁可漏一次「他明明在跟我说话」，也不要因为别人闲聊白跑一张 100~250 秒的图。

## 说明书

`moss漫画说明书` 回一份 `MANUAL` 常量（同样要喊名字）。写成常量不交给 AI：
格数、尺寸、耗时、存盘路径都是硬事实，模型每次复述都会不一样，还白吃一次
调用。**它必须排在 `_CMD_RE` 前面判断**，否则「moss漫画说明书」会被
`moss漫画` 吃掉，「说明书」当成剧情真去画一张图。

**定位靠编号，且只靠编号。** 引用段带回来的 `reply.id` 对应发图时的
`message_id`，账本一查就知道「哪一系列第几页」（见 `moss_msgs`）；
`quote.id` 查不到时（转发之后 id 变了）就从**被引消息正文**里正则抠
`S7K3-P05`。两条都不通就**明确报错**，绝不猜一个系列往下画——
接错线比不画糟糕得多。

## 转发到私聊自己玩

群友想把群里的线搬到自己私聊接着画，做法是把那一页的**回执转发给
大大怪**——转发段（`forward`）的正文会被 `qq_bot._resolve_quote` 展开，
卡片上的编号就在里面，照样能定位（`quote_id` 是新的 forward id，
账本查不到，走编号兜底）。所以**第一页的回执后面会多一句用法提示**。

这里绝对不做「扫到编号就主动搭话」：纯转发（不说指令）走普通聊天管道，
进了代码闸门也不会自己开口——否则机器人回一句带编号的话，
又被自己扫到，就是死循环（用户 2026-10-31 点出来的）。

## 群聊和私聊**故意不一样**

  · **群聊**：谁都能从任意一页重开，**不拦、不截断**，新页挂在同一个
    parent 下变成分支。多人一起玩本来就会分叉，拦住只会把有意思的部分
    拦掉（用户 2026-10-31 的判断：「你敢在群聊玩，就是大家一起玩」）。
    换来的代价是「最新创建的那一页」才是下一个续集接的地方。
  · **私聊**：一对一，用户要的是唯一主线。从中间某页重开时**先弹确认**，
    确认后把后面的页标 `dead`（图留盘，只是移出主线）。

两条都用同一份数据（`moss_series` 的树），差别只在「回头时删不删」。

## 并发：同系列同时只跑一张

群里十个人同时点续集，就是十张图一起跑、接在同一页上，最后炸出十个
几乎一样的分支。所以开跑前占位，已经在跑就直接告诉他「第 06 页还在画」。

## 回执（2026-10-22 用户点名要的）

分三条，一条都不能省：
1. 编剧出来先报**标题 + 每格台词**——让人在等的 80 秒里就能发现「它理解跑偏了」；
2. 出图完，图下面带**编号** / 标题 / 步数 / 尺寸 / 耗时 / seed；
3. 任何一步失败都得有话说，绝不能让用户对着「在画了」干等。

编号印在卡上还有个副作用是好的：转发/截图之后 `message_id` 会变，
账本查不到，这时靠卡上的 `S7K3-P05` 手打也能接上（`moss_msgs.locate`）。

## 需求只取关键词后面那段

和造卡同一个坑：群消息进来前面带着「昵称：」，对方还常先喊一句机器人名字。
早期造卡把这两样当成了需求，结果卡名就叫「大大怪」。所以前面一律丢掉。
"""

import json
import logging
import os
import random
import re
import threading
import time

from app import (image_out, moss_api, moss_comic, moss_msgs,
                 moss_series as series_mod, qq_api, trigger)
from app.config import QQ_AGENT_ID

log = logging.getLogger("moss_bot")

# 默认六格 `2t4b`（2026-10-31 晚用户决定：默认六格，但不要等权平铺）。
LAYOUT = "2t4b"
PAGES = 1

NAME = trigger.NAME


# ─── 指令关键词 ───────────────────────────────────────
#
# 四个词**都带 `moss` 前缀**（2026-10-31 用户拍板，见模块开头）。
# 裸「续集」「重画」「合集」一律不接管——群里天天有人用这几个词，
# 一旦要靠「有没有引用」去猜，就必然在这两个错之间来回：
# 猜宽了白跑一张 100 秒的图，猜窄了用户明明引用了却不理他。
#
# `小\s*` 挂在 `moss` 后面：用户口头就说「moss小漫画」，三种写法落一套逻辑。
_CMD_RE = re.compile(r"moss\s*(?:小\s*)?(漫画|续\s*集|重\s*画|合\s*集)", re.I)
_ACTS = {"漫画": "new", "续集": "continue", "重画": "redo", "合集": "album"}

# 说明书（2026-10-31 用户要的）。**必须排在 `_CMD_RE` 前面判断**——否则
# 「moss漫画说明书」会先被 `moss漫画` 吃掉，剩下的「说明书」当成剧情，
# 真去画一张叫说明书的图。
# `(?:漫画\s*)?` 可有可无：「moss说明书」「moss漫画说明书」「moss漫画 说明书」
# 三种写法都认（用户实际怎么打就怎么认，不要求他记格式）。
_MANUAL_RE = re.compile(
    r"moss\s*(?:小\s*)?(?:漫画\s*)?(?:说明书|使用说明|玩法说明|怎么用)", re.I)

_ASK_RE = re.compile(r"^(怎么|如何|咋|怎样|什么|啥|能不能|可不可以|可以|支持|是啥|是什)")


# ─── 说明书正文 ───────────────────────────────────────
#
# 2026-10-31 用户点名要的：「给我一个 moss漫画说明书，直接关键词触发」。
# 写成**常量**而不是交给 AI 现编：说明书里全是硬事实（几条命令、多少格、
# 多长、存哪），让模型每次复述一遍就会每次不一样，还费 token。
# 文案里出现的 `moss续集` 这些词**不会**被自己触发——`parse` 只读用户发来的
# 消息，回执不会再灌回来（死循环那个坑，见模块开头）。
MANUAL = (
    "📖 moss 小漫画 · 说明书\n"
    "\n"
    "【开一条新线】\n"
    "moss漫画 雨夜便利店，两个陌生人共用一把伞\n"
    "一页六格 · 1024×1536 · 30 步 · cfg 3.5 · 约 100~250 秒\n"
    "\n"
    "【往下续 · 先引用那一页的图】\n"
    "引用 → moss续集 她发现伞被人拿走了\n"
    "引用哪页就从哪页接，页号不用自己记\n"
    "\n"
    "【画崩了重画 · 先引用那一页的图】\n"
    "引用 → moss重画\n"
    "换个随机种子重画同一页，剧本不动；旧图留盘不删\n"
    "\n"
    "【打包整条线 · 引用任意一页】\n"
    "引用 → moss合集        （1-10 页写成 moss合集 1-10）\n"
    "顺着引用那页往回，按顺序合成一张聊天记录卡片\n"
    "\n"
    "【编号：转发之后靠它定位】\n"
    "图下面印着 S7K3 · 第 05 页。转发/截图之后引用会失联，\n"
    "直接打编号：moss续集 S7K3-P05 <接下来的剧情>\n"
    "想搬到自己私聊玩：把那张回执转发给我，再说 moss续集 <剧情>\n"
    "\n"
    "【群聊和私聊不一样】\n"
    "群聊：任意一页都能重开，新页成为分支，谁的页都不删\n"
    "私聊：只有一条主线，从中间重开先问你一句，确认后\n"
    "　　　后面几页移出主线（图还在磁盘，不删）\n"
    "同一条线同时只画一张：第 06 页还在画时，续集会让你等它出来\n"
    "\n"
    "【版式】\n"
    "默认六格：上排两格大画面建场，下排四格连打反应\n"
    "想换直接说：四格 / 五格 / 等分六格 / 上面一大格\n"
    "\n"
    "【触发规矩】\n"
    "群里要 @大大怪，或句子里带「大大怪」；私聊直接说\n"
    "\n"
    "【存在哪】\n"
    "ComfyUI/output/qq-moss漫画/<任务目录>/"
)

USAGE = ("moss小漫画这样用：\n"
         "① 新开一条线（不用引用）：\n"
         "   `moss漫画 <一句话剧情>`\n"
         "② 接着往下画（**先引用上一页那张图**）：\n"
         "   引用 → 回 `moss续集 <接下来发生什么>`\n"
         "③ 这一页画崩了（**先引用那一页**）：\n"
         "   引用 → 回 `moss重画`\n"
         "④ 把整条线打包成一张卡片（**先引用任意一页**）：\n"
         "   引用 → 回 `moss合集`（也可以 `moss合集 1-10` 只取一段）\n"
         "\n"
         "每张图上都印着编号，像 `S7K3 · 第 05 页`。转发出去以后\n"
         "引用就认不出来了，那时直接打编号：`moss续集 S7K3-P05 <剧情>`。\n"
         "\n"
         "**这四个词必须要带 moss**：「续集」「重画」这些词群里天天有人说，\n"
         "不带前缀我分不清你是在跟我说话还是在聊天。\n"
         "**默认六格**（上排两格大画面建场，下排四格连打反应），\n"
         "想要别的直接说 `四格` / `五格` / `等分六格`。约 100~250 秒。\n"
         "完整玩法看 `moss漫画说明书`。")


# ─── 指令解析 ─────────────────────────────────────────

def parse(text, at_me=False, quote_id="", quote_text=""):
    """这条消息是不是 moss 指令。返回 `(是不是, 动作, 载荷, 提示语)`。

    动作只有四个：`"new"` / `"continue"` / `"redo"` / `"album"`。
    动作给空串、提示语非空 = 接管了但只回一段说明（不跑图）。

    **`parse` 只认词，不认引用**：带 `moss` 前缀就归这边管，能不能定位
    到具体一页是 `start` 的事（`parse` 不做 IO，测试也好写）。
    `quote_id` / `quote_text` 收在这里只是为了让调用方少写一遍转发。
    """
    t = (text or "").strip()
    if not t:
        return False, "", "", ""

    # 触发门槛（2026-10-31 用户拍板，两处口径一致）：
    # **群里必须真 @，或者句子里带「大大怪」**；私聊没有 @ 这回事，调用方
    # 传 at_me=True 放行。判据统一在 `app/trigger.py`。
    #
    # 原来只有 `moss漫画` 有这道门槛，续集/重画/合集「带了前缀就接管」——
    # 用户实测报的就是这个：回执本身把这四个词印在群聊里，谁复述一句
    # `moss合集` 就被当成指令，白记一次「认不出引用」的报错。
    if not trigger.called(t, at_me):
        return False, "", "", ""

    # 说明书：必须排在 `_CMD_RE` 前面（见 `_MANUAL_RE` 处注释）
    if _MANUAL_RE.search(t):
        return True, "", "", MANUAL

    m = _CMD_RE.search(t)
    if not m:
        return False, "", "", ""
    act = _ACTS[re.sub(r"\s+", "", m.group(1))]
    rest = _clean(t[m.end():])

    if act == "new":
        if not rest or len(rest) < 2 or _ASK_RE.match(rest):
            return True, "", "", USAGE
        return True, "new", rest, ""

    # 续集 / 重画 / 合集：有前缀就归这边管，问用法的回说明。
    # 能不能定位到具体一页是 `start` 的事（`parse` 不做 IO，测试也好写）。
    if rest and _ASK_RE.match(rest):
        return True, "", "", USAGE
    return True, act, rest, ""


def _clean(s):
    s = (s or "").lstrip(" #＃，,。.:：、\n")
    return s.strip(" ，,。.:：、\n")


def _span(rest, total):
    """`moss合集 1-10` 里的范围。认 `3-8` / `3~8` / `3到8`，抠不到就是全篇。"""
    m = re.search(r"(\d{1,3})\s*[-~～到至]\s*(\d{1,3})", rest or "")
    if not m:
        return 1, total
    a, b = int(m.group(1)), int(m.group(2))
    if a > b:
        a, b = b, a
    return max(1, a), min(total, b)


# ─── 版式识别 ─────────────────────────────────────────
#
# 用户说「四格」「上面一大格」就照办（2026-10-31 用户原话：用户会要求自己想要
# 什么格式，我们就得按用户的要求来）。所以这是**硬指定**，不是给编剧的建议。
#
# 顺序有意义：**先具体、后笼统**。显式键名放最前（跑图台和文档里就是这个写法），
# 然后是「上排三个小格」这类描述性说法，最后才是裸的「五格」。
_LAYOUT_ALIASES = (
    (r"2t4b", "2t4b"),
    (r"1t2m2b", "1t2m2b"),
    (r"3t2b", "3t2b"),
    (r"3\s*[x×]\s*2", "3x2"),
    (r"2\s*[x×]\s*2", "2x2"),
    # 「等分六格」「六个一样大」是 3x2；裸说「六格」给 2t4b（默认不是等分）。
    # 这条必须排在裸格数前面，否则「等分六格」会被「六格」抢走。
    (r"(?:等分六格|六格等分|六格一样大|六个一样大|上三下三)", "3x2"),
    (r"(?:上面|上排|顶上)[^。，,]{0,4}(?:三|3)\s*[个小][^。，,]{0,2}格", "3t2b"),
    (r"(?:上面|上排|顶上)[^。，,]{0,6}(?:两|二|2)\s*[个张]?\s*[大宽满]", "2t4b"),
    (r"(?:上面|上排|顶上)[^。，,]{0,6}(?:一|1)\s*[个张]?\s*[大宽满]", "1t2m2b"),
    # 「一格」不能裸认：「他一格一格地爬上楼梯」会被当成单格版式。
    # 只认三种明确写法——单格/单幅、句首的「一格：」、带指令词的「画成一格」。
    (r"(?:单格|单幅|整页一格)", "single"),
    (r"^\s*一格(?:\s|[：:，,、])", "single"),
    (r"(?:画成|画做|做成|改成|换成|用|按|来|要)\s*一格", "single"),
    (r"(?:两格|二格|2\s*格)", "2h"),
    (r"(?:三格|3\s*格)", "3h"),
    (r"(?:四格|4\s*格)", "2x2"),
    (r"(?:五格|5\s*格)", "1t2m2b"),
    (r"(?:六格|6\s*格)", "2t4b"),
)

# 剥离版式词，分两档：
#   ① 强模式：带指令词（「画成四格」「要六格」），剥掉不会伤剧情；
#   ② 首模式：只在开头且后面跟分隔符（「四格：剧情」）。
# 不做一条通吃的原因：群里真有人会说「他一格一格地爬楼梯」，通吃会把剧情删坏。
_LAYOUT_KEYS_PAT = (r"2t4b|1t2m2b|3t2b|3x2|2x2|single|2h|3h|3t|"
                    r"等分六格|六格等分|六格一样大|上三下三|"
                    r"单格|一格|两格|二格|三格|四格|五格|六格|[1-6]\s*格")
_LAYOUT_STRONG_RE = re.compile(
    r"(?:画成|画做|做成|改成|换成|用|按|来|要)\s*(?:%s)\s*(?:版式|格式|漫画|分镜|的)?"
    % _LAYOUT_KEYS_PAT)
_LAYOUT_LEAD_RE = re.compile(r"^\s*(?:%s)\s*[：:，,、\s]\s*" % _LAYOUT_KEYS_PAT)


def detect_layout(brief):
    """从需求里认出用户要的版式。返回 `(版式键, 干净剧情, 是否认到)`。

    认到就**钉死**，编剧没有否决权；认不到才走 `LAYOUT` 默认值。
    """
    text = brief or ""
    picked = ""
    for pat, key in _LAYOUT_ALIASES:
        if re.search(pat, text, re.I):
            picked = key
            break
    if not picked:
        return LAYOUT, text, False
    clean = _LAYOUT_STRONG_RE.sub(" ", text)
    clean = _LAYOUT_LEAD_RE.sub("", clean)
    clean = re.sub(r"\s{2,}", " ", clean).strip(" ，,。.:：、")
    return picked, clean, True


# ─── 闸门 ─────────────────────────────────────────────

_RUN_LOCK = threading.Lock()
_RUNNING = {}          # series_id -> {"n": 第几页, "ts": 开跑时间}

_PEND_LOCK = threading.Lock()
_PENDING = {}          # (target, target_id) -> 私聊截断确认
PEND_TTL = 180         # 3 分钟。截断是危险操作，不能让半小时前的警告还挂着


def _take(series_id, n):
    """占位。已经在跑就返回它正在跑第几页（0 = 占到了）。"""
    with _RUN_LOCK:
        cur = _RUNNING.get(series_id)
        if cur:
            return cur.get("n") or 1
        _RUNNING[series_id] = {"n": n, "ts": time.time()}
        return 0


def _release(series_id):
    with _RUN_LOCK:
        _RUNNING.pop(series_id, None)


def running_page(series_id):
    with _RUN_LOCK:
        cur = _RUNNING.get(series_id)
    return (cur or {}).get("n") or 0


def consume_if_confirmed(target, target_id, text, sender_uin=None):
    """轮入口调用：私聊里那条截断确认。

    命中（现在就开跑）返回 True，调用方直接结束本轮；否则返回 False。

    跟 `confirm_gate` 一样的语义——pending 只活到**下一条消息**，
    不是确认就作废。不一样的两点：
      · **绑人**：只有发起截断的那个人回「好」才算（群里路人回一句「好」
        不能把别人的线截掉），不是他说话时 pending 原样留着，不清；
      · **有过期**：3 分钟。`confirm_gate` 那边没有超时，因为生图确认
        错了最坏是白画一张；截断是把用户画好的后面几页踢出主线。
    """
    key = (target, target_id)
    with _PEND_LOCK:
        pend = _PENDING.get(key)
        if not pend:
            return False
        if time.time() - pend.get("ts", 0) > PEND_TTL:
            _PENDING.pop(key, None)
            return False
        if sender_uin and pend.get("uin") and str(sender_uin) != str(pend["uin"]):
            # 不是他说的「好」，当没看见——pending 留着，等他本人回
            return False
        _PENDING.pop(key, None)
    from app import confirm_gate
    if not confirm_gate.is_confirm(text):
        log.info("moss 截断确认作废（对方说的是 %r）", (text or "")[:30])
        return False
    _fire(target, target_id, "continue", pend.get("payload") or "",
          pend.get("sid") or "", pend.get("page_id") or "",
          pend.get("reply_to") or "", pend.get("uin"), truncate=True)
    return True


def _pend(target, target_id, **kw):
    kw["ts"] = time.time()
    with _PEND_LOCK:
        _PENDING[(target, target_id)] = kw


# ─── 入口 ─────────────────────────────────────────────

def _owner(target, target_id):
    """本会话的隔离键。取不到就当没隔离，不让它把整轮指令打断。"""
    try:
        return series_mod.session_key(target, target_id)
    except ValueError:
        log.warning("moss 会话键取不到（%s %r），按全局处理", target, target_id)
        return ""


def _miss_note(quote_text, payload, owner=""):
    """定位失败时，说清是**哪种**失败。

    三种情况差很多，笼统回一句「认不出来」用户只能瞎猜：
      ① 压根没给编号      → 教他怎么给（引用 / 打编号）
      ② 编号打了但没这条线 → 大概率打错字，把那串原样贴回去让他自己看
      ③ 线在但没有这一页   → 报总页数，他立刻知道越界了

    用户 2026-10-31 原话：「编号错误，很抱歉，我没有找到这一个对应的编号，
    你是不是编号弄错了？请给我正确的编号，不然我没有办法给你跑。」
    """
    code = series_mod.find_code("%s %s" % (quote_text or "", payload or ""))
    if not code:
        return ("没认出要接哪一页。\n\n"
                "两种用法：\n"
                "① **引用**那一页的图 → 回 `moss续集 <剧情>`\n"
                "② 直接打编号 → `moss续集 S7K3-P05 <剧情>`\n"
                "（编号印在图下面那行回执上）")
    sid, n = series_mod.parse_code(code)
    series = series_mod.load(sid)
    if not series:
        return ("没找到编号 `%s` 这条线。\n"
                "编号是不是打错了？卡片上印的是 `S7K3-P05` 这种。\n"
                "也可以直接**引用那一页的图**，那样不用打编号。" % code)
    total = len(series_mod.alive(series, owner))
    return ("`%s` 这条线一共 %d 页，没有第 %02d 页。\n"
            "编号对不对？" % (sid, total, n))


def start(target, target_id, action, payload, quote_id="", quote_text="",
          reply_to="", sender_uin=None):
    """起后台线程，**立刻返回**。返回 `(起没起, 要说的话)`。

    编剧 2~6 秒 + 出图 81 秒，堵在消息线程上等于这一分半整个群都听不见
    机器人说话（同 `card_bot` / `image_jobs` 那条口径）。
    """
    if action == "new":
        return _fire(target, target_id, "new", payload, "",
                     "", reply_to, sender_uin)

    # 会话隔离（2026-10-31 用户强调）：同一条线，群聊和私聊各续各的。
    # 定位时带上本会话的键，手打编号才会优先命中他自己那个第 6 页。
    owner = _owner(target, target_id)
    sid, page, how = moss_msgs.locate(quote_id, quote_text, owner=owner)
    if not page and payload:
        # 转发/截图之后 id 失效、又没引用——用户手打的编号就在正文里
        sid, page, how = moss_msgs.locate("", payload, owner=owner)
    if not page:
        return False, _miss_note(quote_text, payload, owner)

    log.info("moss 定位成功：%s 第 %s 页（靠 %s）", sid, page.get("n"), how)

    # 私聊「回头续」= 截断，先问一句（群聊不问：群里允许分支，用户拍板的）。
    # 放在 `_fire` 之前：占位要等真开跑，弹确认时还没开跑，不能白占一个名额。
    if action == "continue" and target != "group":
        series = series_mod.load(sid)
        behind = [p for p in series_mod.alive(series or {}, owner)
                  if p.get("n", 0) > page.get("n", 0)]
        if behind:
            last = behind[-1].get("n")
            _pend(target, target_id, sid=sid, page_id=page.get("id") or "",
                  payload=payload, reply_to=reply_to, uin=sender_uin)
            return False, (
                "你要从第 %02d 页往下续，但这条线已经到第 %02d 页了。\n\n"
                "确认的话 %02d~%02d 会从主线移出（图还在磁盘，不删），\n"
                "新的第 %02d 页接在第 %02d 页后面。\n\n"
                "继续 → 回「好」\n"
                "算了 → 别理它，%d 分钟后自动失效"
                % (page.get("n") or 0, last,
                   (page.get("n") or 0) + 1, last,
                   (page.get("n") or 0) + 1, page.get("n") or 0,
                   PEND_TTL // 60))

    return _fire(target, target_id, action, payload, sid, page.get("id") or "",
                 reply_to, sender_uin)


def _fire(target, target_id, action, payload, sid, page_id,
          reply_to, sender_uin, truncate=False):
    """占位 + 起线程。占不到位就回话（不排队：排到半小时后剧情早变了）。"""
    series = series_mod.load(sid) if sid else None
    if sid and not series:
        return False, "这条线不在了（编号 %s），重新开一条吧" % sid

    if sid:
        page = series_mod.by_id(series, page_id)
        nxt = (int((page or {}).get("n") or 0) + 1) if page else 1
        busy = _take(sid, nxt)
        if busy:
            return False, ("第 %02d 页还在画，等它出来再续。\n"
                           "（同一条线同时只跑一张，不然会炸出一堆一样的）" % busy)

    th = threading.Thread(target=_work, name="moss-comic", daemon=True,
                          args=(target, target_id, action, payload, sid, page_id,
                                str(reply_to or ""), sender_uin, truncate,
                                _owner(target, target_id)))
    th.start()
    return True, ""


def _work(target, target_id, action, payload, sid, page_id, reply_to,
          sender_uin, truncate, owner):
    try:
        if action == "new":
            _do_new(target, target_id, payload, reply_to, sender_uin, owner)
        elif action == "continue":
            _do_continue(target, target_id, payload, sid, page_id, reply_to,
                         sender_uin, truncate, owner)
        elif action == "redo":
            _do_redo(target, target_id, sid, page_id, reply_to, sender_uin)
        elif action == "album":
            _do_album(target, target_id, sid, page_id, payload, reply_to)
        else:
            _say(target, target_id, USAGE, reply_to)
    except Exception as exc:                    # noqa: BLE001
        log.exception("moss 线程炸了：%s", action)
        _say(target, target_id, "moss漫画没成：%s" % exc, reply_to)
    finally:
        if sid:
            _release(sid)


# ─── 动作：新开 ───────────────────────────────────────

def _do_new(target, target_id, brief, reply_to, sender_uin, owner=""):
    layout, brief, picked = _layout(brief)
    if not brief:
        _say(target, target_id,
             "moss小漫画：要 `%s` 版式，但没说画什么。\n"
             "补一句剧情再来，比如\n`moss漫画 四格 雨夜便利店，两个陌生人共用一把伞`"
             % layout, reply_to)
        return

    spec, d, _err = _write_spec(target, target_id, brief, layout, "", "", {},
                                reply_to)
    if not spec:
        return
    cells = len((spec.get("pages") or [{}])[0].get("panels") or [])
    series = series_mod.open_new(target, target_id,
                                 title=spec.get("title") or "",
                                 style=spec.get("style") or "",
                                 cast=spec.get("cast") or {})
    sid = series["id"]
    seed = random.randint(1, 2 ** 31 - 1)
    page = series_mod.mutate(sid, lambda x: series_mod.add_page(
        x, parent_id="", task=d, brief=brief, seed=seed,
        title=spec.get("title") or "", owner=owner))
    _say(target, target_id,
         "🀄 新开一条线 **%s** · 第 01 页\n%s\n%d 格 · %d 步 · cfg %s · 约 100 秒"
         % (sid, _lines(spec), cells, moss_api.STEPS, moss_api.CFG), reply_to)
    _render_and_send(target, target_id, sid, page, spec, d, seed,
                     reply_to, sender_uin,
                     hint=("想搬到别处自己玩（比如私聊）：把这张图**转发**给"
                           "大大怪，或把那行编号复制过去，\n再说 "
                           "`moss续集 %s <接下来的剧情>`，就是同一份剧情接着画。"
                           % series_mod.code_of(sid, 1)))


# ─── 动作：续集 ───────────────────────────────────────

def _do_continue(target, target_id, brief, sid, page_id, reply_to, sender_uin,
                 truncate, owner=""):
    series = series_mod.load(sid)
    if not series:
        _say(target, target_id, "这条线不在了（编号 %s）" % sid, reply_to)
        return
    page = series_mod.by_id(series, page_id)
    if not page:
        _say(target, target_id, "第 %s 页找不到了" % page_id, reply_to)
        return

    layout, brief, _picked = _layout(series_mod.strip_code(brief))
    if not brief:
        _say(target, target_id,
             "续集要写接下来的剧情，比如\n`moss续集 她发现伞被人拿走了`",
             reply_to)
        return

    # 私聊：回头续 = 截断，先问一句。群聊：直接开新分支（用户拍板的）。
    # 截断**只作废本会话看得到的页**（`truncate` 往 dead_in 加 owner）：
    # 群里那些人还在往下画的时候，私聊回头重开不该把他们那几页弄没。
    if truncate:
        behind = [p for p in series_mod.alive(series, owner)
                  if p.get("n", 0) > page.get("n", 0)]
        if behind:
            killed = series_mod.mutate(
                sid, lambda x: series_mod.truncate(x, page_id, owner)) or []
            # 占位号要跟着截断走：原来按「后面还有几页」估的，现在只剩一页了
            _release(sid)
            _take(sid, int(page.get("n") or 0) + 1)
            if killed:
                _say(target, target_id,
                     "第 %s 页之后那 %d 页已经移出主线（图还在磁盘上，没删）。"
                     % (page.get("n"), len(killed)), reply_to)

    prev = series_mod.prev_lines(series, page_id)
    prev_scene = series_mod.prev_scene(series, page_id)
    cast = series.get("cast") or {}

    spec, d, _err = _write_spec(target, target_id, brief, layout, prev,
                                prev_scene, cast, reply_to)
    if not spec:
        return
    cells = len((spec.get("pages") or [{}])[0].get("panels") or [])
    seed = random.randint(1, 2 ** 31 - 1)
    nxt = int(page.get("n") or 0) + 1
    new_page = series_mod.mutate(sid, lambda x: series_mod.add_page(
        x, parent_id=page_id, task=d, brief=brief, seed=seed, owner=owner))
    _say(target, target_id,
         "🀄 %s · 第 %02d 页\n%s\n%d 格 · %d 步 · cfg %s · 约 100 秒"
         % (sid, nxt, _lines(spec), cells, moss_api.STEPS, moss_api.CFG),
         reply_to)
    _render_and_send(target, target_id, sid, new_page, spec, d, seed,
                     reply_to, sender_uin)


# ─── 动作：重画 ───────────────────────────────────────

def _do_redo(target, target_id, sid, page_id, reply_to, sender_uin):
    series = series_mod.load(sid)
    page = series_mod.by_id(series or {}, page_id)
    if not page:
        _say(target, target_id, "第 %s 页找不到了" % page_id, reply_to)
        return
    old_dir = page.get("task") or ""
    try:
        d_old = moss_api._task_dir(old_dir)     # noqa: SLF001
        with open(os.path.join(d_old, "spec.json"), encoding="utf-8") as fh:
            spec = json.load(fh)
    except Exception as exc:                    # noqa: BLE001
        _say(target, target_id,
             "这一页的剧本取不出来了（%s），只能重新开一页" % exc, reply_to)
        return

    # 新建任务目录：**绝不覆盖原图**。重画经常画得比原来还差，
    # 用户得能说「还是原来那张好」。
    d = moss_api._new_task_dir(spec)            # noqa: SLF001
    moss_api._save(d, spec)                     # noqa: SLF001  raw=None 不写剧本原文
    seed = random.randint(1, 2 ** 31 - 1)
    series_mod.mutate(sid, lambda x: series_mod.bump(
        x, page_id, task=d, seed=seed))
    _say(target, target_id,
         "🎲 重画 %s · 第 %02d 页（换了个种子，剧情一个字没动）"
         % (sid, page.get("n") or 0), reply_to)
    fresh = dict(page, task=d, seed=seed)
    _render_and_send(target, target_id, sid, fresh, spec, d, seed,
                     reply_to, sender_uin)


# ─── 动作：合集 ───────────────────────────────────────

def _do_album(target, target_id, sid, page_id, rest, reply_to):
    series = series_mod.load(sid)
    if not series:
        _say(target, target_id, "这条线不在了（编号 %s）" % sid, reply_to)
        return
    # 取**引用那一页所在的整条链**（`chain` 顺着 parent 往根走，不过滤会话）。
    #
    # 这里以前用的是 `active_chain`（= 全局最新页那条链），会串会话：
    # 群聊玩到第 3 页 → 私聊搬过去续出第 4 页 → 群里引用第 3 页说 `moss合集`，
    # 结果把私聊那第 4 页也打包发到群里了（2026-10-31 用户点出来的）。
    # 按 page_id 取链天然就隔离：引用哪一页，就发到那一页为止。
    line = series_mod.chain(series, page_id)
    if not line:
        _say(target, target_id, "这条线还是空的", reply_to)
        return
    a, b = _span(series_mod.strip_code(rest), len(line))
    part = line[a - 1:b]
    if not part:
        _say(target, target_id, "这个范围里没有页（一共 %d 页）" % len(line), reply_to)
        return

    from app.agents import image_send_format
    try:
        fmt = image_send_format(QQ_AGENT_ID, target, target_id)
    except Exception:                           # noqa: BLE001
        fmt = None

    full = (a, b) == (1, len(line))
    nodes = [qq_api.node_segment(
        "《%s》· %s\n共 %d 页%s"
        % (series.get("title") or "无题", sid, len(part),
           "" if full else "（第 %d~%d 页）" % (a, b)),
        name="moss 小漫画", uin=0)]
    miss = 0
    for pg in part:
        try:
            d = moss_api._task_dir(pg.get("task") or "")   # noqa: SLF001
            rel = moss_api._page_file(d, 1)                # noqa: SLF001
            if not rel:
                miss += 1
                continue
            path = image_out.prepare_for_send(rel, fmt) if fmt else rel
        except Exception:                       # noqa: BLE001
            log.warning("合集取图失败 %s 第 %s 页", sid, pg.get("n"))
            miss += 1
            continue
        nodes.append(qq_api.node_segment(
            [qq_api.image_segment(path)],
            name="第 %02d 页" % (pg.get("n") or 0), uin=0))
        if pg.get("summary"):
            nodes.append(qq_api.node_segment(
                "第 %02d 页：%s" % (pg.get("n") or 0, pg["summary"]),
                name="剧情", uin=0))

    if len(nodes) == 1:
        _say(target, target_id, "这几页的图都取不出来了（%d 张缺）" % miss, reply_to)
        return
    try:
        qq_api.send_forward(target, target_id, nodes)
    except Exception as exc:                    # noqa: BLE001
        log.exception("moss 合集发不出去")
        _say(target, target_id, "合集没发出去（%s）" % exc, reply_to)
        return
    if miss:
        _say(target, target_id, "合集缺了 %d 张（任务目录被清掉了）" % miss, reply_to)


# ─── 出图 + 发图 + 记账 ───────────────────────────────

def _write_spec(target, target_id, brief, layout, prev, prev_scene, cast,
                reply_to):
    """调编剧。失败时自己回话并返回 `(None, "", 原因)`。"""
    try:
        spec, raw, err = moss_comic.write(
            brief, pages=PAGES, layout=layout,
            ratio="%dx%d" % (moss_api.WIDTH, moss_api.HEIGHT),
            prev=prev, prev_scene=prev_scene, cast=cast)
    except Exception as exc:                    # noqa: BLE001
        log.exception("moss 编剧炸了：%s", brief[:40])
        _say(target, target_id, "moss漫画没成：编剧这步出了岔子（%s）\n再试一次？"
             % exc, reply_to)
        return None, "", str(exc)
    if not spec:
        log.warning("moss 剧本解析失败：%s", (raw or "")[:200])
        _say(target, target_id,
             "moss漫画没成：剧本没写出来%s\n换个说法再试一次？"
             % (("（%s）" % err) if err else ""), reply_to)
        return None, "", err
    d = moss_api._new_task_dir(spec)            # noqa: SLF001  同包复用
    moss_api._save(d, spec, raw)                # noqa: SLF001
    return spec, d, ""


def _render_and_send(target, target_id, sid, page, spec, d, seed,
                     reply_to, sender_uin, hint=""):
    """出图 → 存档卡 → 发图 → 记账。任何一步失败都回话，绝不干等。"""
    t0 = time.time()
    try:
        moss_api._run(d, spec, [1], seed)       # noqa: SLF001  内部已兜异常
    except Exception as exc:                    # noqa: BLE001
        log.exception("moss 出图线程炸了 %s", d)
        _say(target, target_id, "moss漫画没成：出图那步炸了（%s）" % exc, reply_to)
        return

    st = (moss_api._load_state(d).get("pages") or {}).get("1") or {}   # noqa: SLF001
    rel = st.get("file") or ""
    if st.get("status") != "done" or not rel:
        _say(target, target_id,
             "moss漫画没成：%s" % (st.get("error") or "跑完了但没找到图"), reply_to)
        return

    # 存档卡：一次调用同时产「这一页演了什么」和「这一页长什么样」。
    # 放在出图**之后**——出图失败就不必花这次调用。
    summary, scene = moss_comic.digest(spec)
    pid = (page or {}).get("id") or ""
    n = (page or {}).get("n") or 1
    if pid:
        series_mod.mutate(sid, lambda x: series_mod.fill(
            x, pid, summary=summary, scene=scene, task=d, seed=seed))

    secs = round(time.time() - t0, 1)
    cells = len((spec.get("pages") or [{}])[0].get("panels") or [])
    caption = ("%s · 第 %02d 页 ·《%s》\n%d 格 · %d 步 · cfg %s · %dx%d · "
               "共 %.0f 秒 · seed %d"
               % (series_mod.code_of(sid, n), n, spec.get("title") or "无题",
                  cells, moss_api.STEPS, moss_api.CFG,
                  moss_api.WIDTH, moss_api.HEIGHT, secs, seed))
    mid = _send_image(target, target_id, rel, caption, reply_to, sender_uin)
    # 记「这条消息 → 哪一页」，引用它就是定位（见 moss_msgs）。
    # 记不上不致命：卡片上的编号照样能接上。
    moss_msgs.remember(mid, sid, pid, n)
    if hint:
        _say(target, target_id, hint, reply_to)


def _send_image(target, target_id, rel, caption, reply_to="", sender_uin=None):
    """过格式、过审核、发出去。返回 message_id（拿不到是空串）。

    图这条**一定带引用**（`reply_to` 是点单那句）：这里从前把 reply_to 收下
    却没用，结果文字回执引用着、图本身裸发——用户要的就是图跟着引用回来。
    """
    from app import image_audit
    from app.agents import image_send_format
    try:
        fmt = image_send_format(QQ_AGENT_ID, target, target_id)
        path = image_out.prepare_for_send(rel, fmt)
    except Exception as exc:                    # noqa: BLE001
        log.exception("moss 图取不出来：%s", rel)
        _say(target, target_id, "图画出来了但取不到文件（%s）" % exc, reply_to)
        return ""
    try:
        if not image_audit.allow_send(path, QQ_AGENT_ID, target, target_id,
                                      meta={"kind": "moss_comic",
                                            "caption": caption}):
            return ""
        return qq_api.send_image(target, target_id, path, caption=caption,
                                 reply_to=reply_to or None,
                                 at_user=(sender_uin if target == "group"
                                          else None))
    except Exception as exc:                    # noqa: BLE001
        log.exception("moss 图发不出去 %s", rel)
        _say(target, target_id, "图画好了但发不出去（%s）" % exc, reply_to)
        return ""


# ─── 小工具 ───────────────────────────────────────────

def _layout(brief):
    """版式识别 + 清洗。返回 `(版式, 干净剧情, 认没认到)`；剧情太短给空串。"""
    layout, clean, picked = detect_layout(brief)
    if picked:
        if len(clean) < 2:
            return layout, "", True
        log.info("moss 版式由用户指定：%s（%s）", layout, clean[:24])
        return layout, clean, True
    return layout, brief, False


def _lines(spec):
    """每格挑一句中文台词当预览（旁白 > 气泡 > 拟声）。

    没台词的格子补一个「—」而不是直接跳过：跳过会变成 `1· 2· 4·`，
    看着像少了一格，群里第一反应是「出 bug 了」。
    """
    page = (spec.get("pages") or [{}])[0]
    out = []
    for i, p in enumerate(page.get("panels") or [], 1):
        word = ""
        for key in ("caption", "bubble", "sfx"):
            word = str(p.get(key) or "").strip()
            if word:
                break
        out.append("　%d· %s" % (i, word or "—"))
    return "\n".join(out)


def _say(target, target_id, text, reply_to=""):
    try:
        if target == "group":
            qq_api.send_group(target_id, text, reply_to=reply_to or None)
        else:
            qq_api.send_private(target_id, text, reply_to=reply_to or None)
    except Exception:                           # noqa: BLE001
        log.exception("moss 回话失败：%s", str(text)[:60])
