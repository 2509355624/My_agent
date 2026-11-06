# -*- coding: utf-8 -*-
"""写作档位与分层注入（2026-11-06，接 SillyTavern 那套写法）。

和 `writing_skills.py` 的分工：那边是**按状态轴切一小段规范原文**（亲密场
景的用词和动词，管「怎么说」）；本模块管**结构和位置**（管「话放在哪」）：

| 酒馆层 | 这里落点 |
|---|---|
| L1 sysprompt 档位 | `head_style()` → `build_head` 里「## 这一局的写法」 |
| L2 厚卡 / mes_example | `card_sketch()` 把卡里已有却没进提示词的字段接上；`format_examples()` 支持 `<START>` / `***` 分块 |
| L3 作者注 | 并进 `tail()` 的「这一幕：…」，见下方取舍 |
| L4 jailbreak 近因位 | `tail()` 追加到**最后一条玩家消息**上；篇幅锚点与剧情推进也在这里 |

**为什么 L3 没有插进历史中间**：`_render_hist()` 产出的历史是 user/assistant
交替，且 assistant 消息里带着「画面 / 心情 / 状态」三行，本身就是下一轮的
格式 few-shot。中途插一条 system 会把这个节奏打断，收益不如直接贴脸。所以
作者注和近因尾注合并成一条，用词仍是正向的「可以这样」。

**默认注入的范围**：`mode=normal` 且 `note` 为空时，`head_style()` 返回
空串 —— 档位块不拼，消息角色序列不变。近因位 `tail()` **每轮都拼**两句
正向锚点（人称视角 + 这一轮写完整、600~2000 字）；跳跃 / 收尾那几条也
走同一个函数。轮次、「这一幕」、剧情推进都是玩家主动填才带。
篇幅锚点放在近因位而不是 system 头：历史 few-shot 全是三四百字的短回复，
光在契约里写「写 1000 字」压不住示范的长度（2026-11-07 归档中位 360 字）。
`card_sketch()` / `format_examples()` 属于卡层，是**有意对所有会话生效**的
小改动（把卡里写着却没用上的事实接上、把多块示例编号），约多 20 字。

口径（用户 2026-11-06 定）：只写「可以这样」，不堆「不能 / 不许 / 不要」；
代码只做归一化和事实透出，写什么交给模型。非法档位不报错，退回常态。
"""

import re

MODES = {
    "normal": {
        "label": "常态",
        "line": "",
    },
    "sensory": {
        "label": "细写",
        "line": "这一局可以让镜头在她手边的物件上多停一会儿，让声音、温度、"
                "布料替她说出情绪。",
    },
    "brisk": {
        "label": "轻快",
        "line": "这一局可以让对话短一些、快一些，用半句话和停顿把机灵劲带出来。",
    },
    "slow": {
        "label": "慢镜头",
        "line": "这一局可以把一个动作拆细，让一次转身、一回呼吸占住一小段。",
    },
    "quiet": {
        "label": "留白",
        "line": "这一局可以写得短，把落在她身上的那一瞬留白，让读的人自己感觉到。",
    },
}

DEFAULT_MODE = "normal"
NOTE_MAX_CHARS = 160
_EXAMPLE_SPLIT = re.compile(r"(?:\*{3,}|<START>)", re.I)
_WS = re.compile(r"\s+")


def norm(writing):
    """把请求里的 writing 归一化成 `(mode, note)`。

    只做事实处理：非法/缺失档位退回常态，作者注压成单行并截断。不评判内容，
    也不拿词表拦玩家想写什么。
    """
    if not isinstance(writing, dict):
        return DEFAULT_MODE, ""
    mode = str(writing.get("mode") or "").strip().lower()
    if mode not in MODES:
        mode = DEFAULT_MODE
    note = _WS.sub(" ", str(writing.get("note") or "").strip())
    return mode, note[:NOTE_MAX_CHARS]


def mode_line(mode):
    """档位那一句正向短句；常态返回空串。"""
    return MODES.get(mode, MODES[DEFAULT_MODE])["line"]


def head_style(mode):
    """L1：拼进 system 头的档位块。常态不拼，老提示词原样。"""
    line = mode_line(mode)
    return "## 这一局的写法\n\n" + line if line else ""


def style_list():
    """给前端下拉用：`[{key, label, line}, ...]`。"""
    return [{"key": k, "label": v["label"], "line": v["line"]}
            for k, v in MODES.items()]


def card_sketch(card):
    """L2：卡里已经写好、但以前没进提示词的两块事实。

    `description` 是卡库列表用的短标签（常带作品出处），`personality` 是标签
    串。它们不在 `build_head` 的输入里，等于白写着。已经原样出现在
    `systemPrompt` 里的不重复拼——避免同一句说两遍摊薄注意力。
    """
    sysp = str((card or {}).get("systemPrompt") or "")
    bits = []
    for key in ("description", "personality"):
        val = str((card or {}).get(key) or "").strip()
        if val and val not in sysp:
            bits.append(val)
    return "## 卡面速写\n\n" + "；".join(bits) if bits else ""


def format_examples(text):
    """L2：把酒馆式 `<START>` / `***` 分块标成编号示范。

    只有一块（存量 21 张卡）时**原样返回**，一个字都不改。多块才编号，
    让模型读到的是几段独立 few-shot，而不是一整篇连贯的稿子。
    """
    src = str(text or "").strip()
    if not src:
        return ""
    parts = [p.strip() for p in _EXAMPLE_SPLIT.split(src) if p.strip()]
    if len(parts) < 2:
        return src
    return "\n\n".join("【示范 %d】\n%s" % (i + 1, part)
                       for i, part in enumerate(parts))


# 近因位固定句（2026-11-07）。它同时替代 `render_user_block(remind=True)`
# 原来那句「任何地方都不要用「他」指玩家」——把被禁概念说一遍反而会点亮它。
# 现在只把事实说清：谁是她、谁是你、镜头落在哪。全句不含禁令。
ANCHOR = "她是「她」，你是「你」；镜头可以跟着她，落在她的反应上"

# 篇幅也钉在近因位（2026-11-07）：契约里那句「字数不是目标 / 允许烂尾」，
# 加上历史里清一色 300~500 字的示范，把模型稳稳按在 360 字中位上。
# 正向写「写成什么样」，不写「不许短」。
LENGTH_ANCHOR = "这一轮写完整：600~2000 字，有起有落，收得住"

# 剧情推进是玩家给这一拍的方向，放在尾注**最后一句**，离生成点最近。
PLOT_PREFIX = "这一拍往这个方向走，让它真的发生在画面里："


def tail(turn_no=None, note="", plot=""):
    """L3 + L4：所有组装路径共用的近因尾注，只拼在最后一条 user 消息末尾。

    常态（不给轮次、没写调子、没写剧情推进）只返回「人称视角 + 篇幅」两句
    正向锚点；跳跃 / 收尾路径也走这里。轮次、「这一幕的调子」、剧情推进都是
    玩家主动填才带，剧情推进放最后一句，离生成点最近。判断写什么交给模型，
    这里只做归一化和事实透出。
    """
    bits = []
    if turn_no is not None:
        try:
            n = max(1, int(turn_no or 1))
        except (TypeError, ValueError):
            n = 1
        bits.append("第 %d 轮" % n)
    clean = _WS.sub(" ", str(note or "").strip())[:NOTE_MAX_CHARS]
    if clean:
        bits.append("这一幕：" + clean)
    bits.append(ANCHOR)
    bits.append(LENGTH_ANCHOR)
    clean_plot = _WS.sub(" ", str(plot or "").strip())[:NOTE_MAX_CHARS]
    if clean_plot:
        bits.append(PLOT_PREFIX + clean_plot)
    return "（" + " · ".join(bits) + "）"


def tail_note(turn_no, note="", plot=""):
    """兼容入口：带轮次的贴脸尾注（写法档位那条链路在用）。"""
    return tail(turn_no, note, plot)
