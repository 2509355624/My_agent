"""生图编号（tag）：给「那张图」一个能被引用、能被查回来的名字。

## 它解决什么

图由 worker 直接发回会话，模型在 enqueue 那一刻之后就**再也收不到任何关于
这张图的信息**——提示词、seed、是第几号，一概没有（`Job` 上没有这些字段，
`_recent` 回执里也没有，见 image_jobs.recent_line）。于是用户拿一张图去问
「这张用的什么提示词」，模型只能现编一段，跟他当时实际跑的不是一回事。

## 为什么编号要跟着消息走，而不是靠模型记

发图时编号写在 caption 最前面、和图片放在**同一条消息**里（`qq_api.send_image`
的 caption 参数）。群友想「回到这张图」时会**引用**那条消息，而引用会被 qq_bot
拉回正文（`_resolve_quote` → `get_msg` → `_quote_body` → `_parse_segments`
收 text 段），编号于是跟着引用**重新进入模型的输入**。

caption 整行是 `编号 · 分辨率 · 渠道`（见 image_jobs._caption），后两项是给人
看的附注；**编号必须留在最前且一个字符都不能变**——正则从整行里抠的就是它，
位置挪了、字符被降级吃掉一个，这条链路就断了。

这条路的关键好处：**编号是载体自带的**，既不依赖模型的记忆，也不依赖历史
上下文里还留着提示词（历史会被 `memory.trim_history` 摘要掉）。模型看到
编号之后按需去查账本即可。

## 账本

`state/image_log.jsonl`，一行一张图：`{"tag", "prompt", "ts", "file", "skill",
"target", "target_id"}`。追加写、按需查（工具 `recall_image`）。单开一份文件
而不是塞进会话历史，正是上面那条理由：历史会被摘要掉，账本不会。

## 编号长什么样

`<前缀>-<YYYYMMDD>-<HHMMSS>-<毫秒>`，例如 `HT-20261001-074112-384`。

**不用 ComfyUI 的输出序号**（`Anima_00276_`）：那是它扫 output 目录里同前缀
文件数出来的，**清空 output 就从 1 重来**——跨不了重启，也跨不了换机器。
日期+时间自带时序，一眼看得出新旧，正则也好写。

**毫秒那一段不能省**：入队是瞬间完成的，同一个群里连点两张、或者两个群同一
秒各来一单，`HHMMSS` 会撞号。到毫秒之后，撞号只能发生在「同一毫秒入队两张」
——而入队前还隔着一次模型工具调用，撞不上。全数字，不掺随机字符，查起来
也还是一次正则。

⚠️ 改前缀时必须**以字母开头**，且**不含 `_` 和 `*`**：caption 会过一遍
`qq_api.to_qq_text`（Markdown 降级），行首的 `-`/`*`/`+` 会被当列表符剥掉、
行首 `#` 会被当标题剥掉、`__x__` 会被当粗体剥掉。`HT-...` 三种都躲开了。
`tests/test_image_log.py` 有一条测试专门钉这个约束，改前缀前先看它。
"""

import json
import logging
import os
import re
import threading
import time

from app.config import BASE_DIR

log = logging.getLogger("image_log")

# 账本：一行一张图，编号 → 提示词。测试可以改它指向临时目录；
# 生产恒为 state/image_log.jsonl（state/ 在 .gitignore 里，不进仓库）。
PATH = os.path.join(BASE_DIR, "state", "image_log.jsonl")

_lock = threading.Lock()

# 编号前缀。换风格改这一行即可（注意上面那条「以字母开头」的约束）。
TAG_PREFIX = "HT"

# 编号本身：前缀 + 8 位日期 + 6 位时分秒 + 3 位毫秒。用来从引用回来的
# 正文里抠编号。
TAG_RE = re.compile(re.escape(TAG_PREFIX) + r"-\d{8}-\d{6}-\d{3}")


def new_tag(ts=None):
    """生成一个新编号。

    ts 传 Unix 时间戳（浮点，测试里固定时刻用）；不传取当前时刻。
    """
    t = time.time() if ts is None else float(ts)
    st = time.localtime(t)
    ms = int(round((t - int(t)) * 1000)) % 1000
    return "%s-%s-%s-%03d" % (TAG_PREFIX,
                              time.strftime("%Y%m%d", st),
                              time.strftime("%H%M%S", st), ms)


def find_tags(text):
    """把一段文字里的编号全抠出来（引用回来的正文里找）。

    去重后按出现顺序返回。群友一条消息里引用了好几张图时，正文里会有多个
    编号——都给他，让模型自己判断问的是哪张。
    """
    out = []
    for t in TAG_RE.findall(str(text or "")):
        if t not in out:
            out.append(t)
    return out


# ─── 账本：编号 → 提示词 ────────────────────────────────
#
# 为什么单开一份文件，而不是塞进会话历史：
# ① 历史会被 memory.trim_history 到预算就「整段摘要替换」，逐字提示词在那一步
#    就没了；② 编号跟着消息走（引用时回到模型眼前），账本只要按需读一次。
# 于是模型不需要记住任何东西——它手上有编号，查就是了。


def save(tag, prompt="", **fields):
    """记一行。只在图**真发出去了**之后调（没发出去的图不该有编号可查）。

    追加写、不重写整份文件：写一半崩了也只丢最后一行，前面的账还在。
    落盘失败**不抛异常**——账本只是查询用的附注，绝不能因为它把图也搭进去
    （调用方是在发图路径上）。
    """
    if not tag:
        return
    row = {"tag": tag, "prompt": str(prompt or ""), "ts": time.time()}
    row.update(fields)
    try:
        with _lock:
            d = os.path.dirname(PATH)
            if d:
                os.makedirs(d, exist_ok=True)
            with open(PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception as exc:
        log.warning("生图账本写不进去 %s：%s", PATH, exc)


def lookup(tag):
    """按编号查一行；查不到返回 None。

    同一个编号有多行时**取最后一行**：清空 ComfyUI output 会让编号从头再来，
    那时最新那行才是这张图。
    """
    if not tag:
        return None
    found = None
    try:
        with _lock:
            if not os.path.exists(PATH):
                return None
            with open(PATH, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue          # 坏行跳过，别让一行坏了整本账
                    if isinstance(row, dict) and row.get("tag") == tag:
                        found = row
    except Exception as exc:
        log.warning("生图账本读不出来 %s：%s", PATH, exc)
        return None
    return found
