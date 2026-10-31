"""QQ moss 漫画：`moss漫画 <一句话剧情>`（也认 `moss小漫画`）→ 一页六格。

## 为什么不用 comic_agent

`comic_agent` 那套是给 Anima 写的：编剧按 Danbooru 标签出分镜、逐格渲染、
后期再用 qwen 贴气泡。moss 的文本编码器是 Qwen3.5-9B，吃自然语言、不吃
`(tag:1.2)` 那套加权（工作流里连 LoRA 槽都空着），所以这边直接走
`moss_comic`：编剧模型写剧本 → 脚本拼整页提示词 → **一页一次生成**。

一页六格 3×2、1024×1536 / 30 步 / cfg 3.5（2026-10-31 从四格 25 步升级）。

两边零 import，只共用 `image_jobs` 队列和 `moss_api` 的落盘/出图函数。

## 为什么接在 direct_gen 前面

`moss漫画` 这个词里含渠道词 `moss`。放到直达管道后面，`direct_gen` 会把它
当画面内容，最后画出一张写着「moss漫画」四个字的图。必须整轮接管——
和造卡（`card_bot`）同一个位置、同一个理由。

## 回执（2026-10-22 用户点名要的）

分三条，一条都不能省：
1. 编剧出来先报**标题 + 每格台词**——让人在等的 80 秒里就能发现「它理解跑偏了」，
   而不是等图出来才知道白等；
2. 出图完，图下面带标题 / 步数 / 尺寸 / 耗时 / seed；
3. 任何一步失败都得有话说（编剧没写出来、出图失败、图取不出来），
   绝不能让用户对着「在画了」干等。

## 需求只取「moss漫画」后面那段

和造卡同一个坑：群消息进来前面带着「昵称：」，对方还常先喊一句机器人名字。
早期造卡把这两样当成了需求，结果卡名就叫「大大怪」。所以前面一律丢掉。
"""

import json
import logging
import random
import re
import threading
import time

from app import image_out, moss_api, moss_comic, qq_api
from app.config import QQ_AGENT_ID

log = logging.getLogger("moss_bot")

WORD = "moss漫画"
# 「小」是可选的（2026-10-31）：用户口头说的是「moss 小漫画」，群里有人打
# 「moss漫画」、也有人打「moss 小 漫画」。`小\s*` 挂在 `moss\s*` 后面，
# 三种写法都落进同一套逻辑，不额外维护第二个关键词。
_WORD_RE = re.compile(r"moss\s*(?:小\s*)?漫画", re.I)

# 触发门槛（2026-10-31 用户要求）：**必须 @ 机器人，或者句子里出现「大大怪」**。
# 只写「moss漫画」不接管——群里聊到这个词太容易误触发，一误触发就是一次
# 编剧 + 一页 100~250 秒的出图，而且会盖掉别人的正常对话。
#
# @ 在解析阶段已经从文本里剥掉了（`_parse_message` 只留 text 段，另给 at_me），
# 所以这里不能指望从文本里看到 "@大大怪"，只能靠调用方把 at_me 传进来。
NAME = "大大怪"
_NAME_RE = re.compile(r"大大怪")
LAYOUT = "3x2"
PAGES = 1

# 「moss漫画怎么用」「支持什么」——问句不是需求，不挡住会被当成一句剧情，
# 最后画出一张主题叫「怎么用」的六格。
_ASK_RE = re.compile(r"^(怎么|如何|咋|怎样|什么|啥|能不能|可不可以|可以|支持|是啥|是什)")

USAGE = ("moss小漫画这样用：先 **@我**，或者句子里带上「大大怪」，然后写\n"
         "`moss小漫画 <一句话剧情>`\n"
         "例：`@大大怪 moss小漫画 雨夜便利店，两个陌生人共用一把伞`\n"
         "一页六格，编剧自己写分镜和台词，约 100~250 秒。")


# ─── 识别 ─────────────────────────────────────────────

def parse(text, at_me=False):
    """这条消息是不是 moss 漫画指令。返回 `(是不是, 剧情, 提示语)`。

    剧情 = 「moss漫画」**后面**那一段；前面的一律丢掉（昵称 / @ / 机器人名字）。
    「moss 漫画」中间带空格也认。命中但没给剧情、或者问怎么用 → 回用法。

    `at_me` 是「这条消息真 @ 了机器人」（`qq_bot` 从消息段里解析出来的布尔值）。
    没有 @ 时要求文本里出现「大大怪」，否则返回未命中——见 `NAME` 那段注释。
    """
    t = (text or "").strip()
    m = _WORD_RE.search(t)
    if not m:
        return False, "", ""
    if not at_me and not _NAME_RE.search(t):
        return False, "", ""
    rest = t[m.end():].lstrip(" #＃，,。.:：、\n")
    rest = rest.strip(" ，,。.:：、\n")
    if not rest or len(rest) < 2 or _ASK_RE.match(rest):
        return True, "", USAGE
    return True, rest, ""


# ─── 主流程 ───────────────────────────────────────────

def start(target, target_id, brief, reply_to="", sender_uin=None):
    """起后台线程，**立刻返回**。

    编剧 2~6 秒 + 出图 81 秒，堵在消息线程上等于这一分半整个群都听不见
    机器人说话（同 `card_bot` / `image_jobs` 那条口径）。

    `sender_uin` 是点单人的 QQ 号（2026-10-30）：图好了要 @ 他一下，不然
    一分半之后没人知道自己那张出图了。
    """
    th = threading.Thread(target=_work, name="moss-comic", daemon=True,
                          args=(target, target_id, brief, str(reply_to or ""),
                                sender_uin))
    th.start()
    return th


def _work(target, target_id, brief, reply_to, sender_uin=None):
    t0 = time.time()
    say = _say

    try:
        spec, raw, err = moss_comic.write(
            brief, pages=PAGES, layout=LAYOUT,
            ratio="%dx%d" % (moss_api.WIDTH, moss_api.HEIGHT))
    except Exception as exc:                    # noqa: BLE001
        log.exception("moss 编剧炸了：%s", brief[:40])
        say(target, target_id, "moss漫画没成：编剧这步出了岔子（%s）\n再试一次？"
            % exc, reply_to)
        return
    if not spec:
        log.warning("moss 剧本解析失败：%s", (raw or "")[:200])
        say(target, target_id,
            "moss漫画没成：剧本没写出来%s\n换个说法再试一次？"
            % (("（%s）" % err) if err else ""), reply_to)
        return

    d = moss_api._new_task_dir(spec)            # noqa: SLF001  同包复用
    moss_api._save(d, spec, raw)                # noqa: SLF001
    title = spec.get("title") or "无题"
    # 格数从 spec 里读，不拿 LAYOUT 反推：`check()` 只报不拦，编剧偶尔会少给
    # 一格（5 格也会照画），回执写死「六格」就会和图对不上。
    cells = len((spec.get("pages") or [{}])[0].get("panels") or [])

    # 回执①：标题 + 每格台词。看不懂画面，但看得懂台词跑没跑偏。
    say(target, target_id,
        "🀄 moss小漫画《%s》\n%s\n%d 格 · %d 步 · cfg %s · 约 100 秒"
        % (title, _lines(spec), cells, moss_api.STEPS, moss_api.CFG), reply_to)

    seed = random.randint(1, 2 ** 31 - 1)
    try:
        moss_api._run(d, spec, [1], seed)       # noqa: SLF001  内部已兜异常
    except Exception as exc:                    # noqa: BLE001
        log.exception("moss 出图线程炸了 %s", d)
        say(target, target_id, "moss漫画没成：出图那步炸了（%s）" % exc, reply_to)
        return

    st = (moss_api._load_state(d).get("pages") or {}).get("1") or {}   # noqa: SLF001
    rel = st.get("file") or ""
    if st.get("status") != "done" or not rel:
        say(target, target_id,
            "moss漫画没成：%s" % (st.get("error") or "跑完了但没找到图"), reply_to)
        return

    secs = round(time.time() - t0, 1)
    caption = ("《%s》· %d 格 · %d 步 · cfg %s · %dx%d · 共 %.0f 秒 · seed %d"
               % (title, cells, moss_api.STEPS, moss_api.CFG,
                  moss_api.WIDTH, moss_api.HEIGHT, secs, seed))
    _send_image(target, target_id, rel, caption, reply_to, sender_uin)


def _send_image(target, target_id, rel, caption, reply_to="", sender_uin=None):
    """过格式、过审核、发出去。审核拦下时它自己会回一句，这里只管不给图。

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
        return False
    try:
        if not image_audit.allow_send(path, QQ_AGENT_ID, target, target_id,
                                      meta={"kind": "moss_comic",
                                            "caption": caption}):
            return False
        qq_api.send_image(target, target_id, path, caption=caption,
                          reply_to=reply_to or None,
                          at_user=(sender_uin if target == "group" else None))
        return True
    except Exception as exc:                    # noqa: BLE001
        log.exception("moss 图发不出去 %s", rel)
        _say(target, target_id, "图画好了但发不出去（%s）" % exc, reply_to)
        return False


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
