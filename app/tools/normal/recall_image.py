"""按编号查回「那张图到底用的什么提示词」。

## 它解决什么

群友引用一张图问「这张的词条发我一份」，模型手上只有编号——它不可能记得
当时写的提示词：那段字在工具调用参数里，而历史到预算就会被 `trim_history`
摘要掉。于是它只能**现编一段**，跟当时真正跑的那段不是一回事，对方照着跑
出来的图也对不上。

账本（`app/image_log.py`，`state/image_log.jsonl`）存的就是「编号 → 提示词」
这份对应关系。这个工具只做一件事：拿编号去查，把**当时那一段原文**还给模型。
拿到之后是原样转述、还是拿去重新生一张，由模型自己决定。

## 编号从哪来

发图时编号是 caption、和图片在同一条消息里。群友**引用**那条消息，编号就
跟着引用回到模型眼前（见 app/image_log.py 的模块注释）。所以模型通常是「在
对方引用的正文里看到一个 HT-... 」——把它填进来就行。

工具对入参做了兜底：贴一整段引用正文进来也认（内部会正则抠编号），不用
模型自己先切干净。
"""

import time

from app import image_log

# 一次最多查几个编号：防止模型把一整段历史贴进来，一口气翻几百行
_MAX_PER_CALL = 5


def _seed_note(row):
    """「seed 数字」，或者一句「种子没记下」。

    缺失要**明说**：老图（2026-10-02 之前画的）账本里没这个字段，不点名的话
    模型容易从别处凑一个数报给对方。注意 0 是**合法种子**，不能当「没填」。
    """
    seed = row.get("seed")
    if seed is None or seed == "":
        return "种子没记下"
    try:
        return "seed %d（动漫渠道两段采样共用这一个数）" % int(seed)
    except (TypeError, ValueError):
        return "种子没记下"


def _render(row):
    parts = []
    prompt = (row.get("prompt") or "").strip()
    if prompt:
        parts.append(prompt)
    else:
        # 记账时就没拿到提示词（老图 / 异常路径）——说实话，不要去编一段。
        parts.append("（这张当初没记下提示词）")
    bits = []
    if row.get("skill"):
        bits.append("渠道 " + str(row["skill"]))
    if row.get("ts"):
        bits.append(time.strftime("%m-%d %H:%M", time.localtime(row["ts"])))
    bits.append(_seed_note(row))
    if bits:
        parts.append("（" + "，".join(bits) + "）")
    return "".join(parts)


def _recall_image(tag):
    tags = image_log.find_tags(tag)[:_MAX_PER_CALL]
    if not tags:
        return ("没认出图号。编号长这样：HT-20261001-081132-772。"
                "在对方**引用的那条消息**里找——它是跟图片同一条发出去的。"
                "找不到编号就说明这张不是你画的（或者是很早以前、还没有编号"
                "那会儿的图），老实说不知道，别自己编一段提示词。")

    hits, missed = [], []
    for t in tags:
        row = image_log.lookup(t)
        if row is None:
            missed.append(t)
        else:
            hits.append("%s：%s" % (t, _render(row)))

    if not hits:
        return ("这几个编号在账本里查不到（%s）：可能是很早以前的图（编号机制"
                "是后来才加的），或者记账那次没写进去。老实说不知道，别自己编"
                "一段提示词。" % "、".join(missed))

    out = ("这就是当时真正跑的那段提示词和种子（提示词原样，别改写）：\n"
           + "\n".join(hits)
           + "\n要「换个提示词、同一个种子重画」：把上面那段照原样改，"
             "seed 原样填回 generate_image，渠道也要用回同一个。")
    if missed:
        out += "\n（另有 %s 查不到，别替它们编）" % "、".join(missed)
    return out


tool = {
    "name": "recall_image",
    "description": (
        "按图号查出**这张图当初真正用的提示词和种子**。每张发出的图都带一个编号"
        "（形如 HT-20261001-081132-772，跟图片在同一条消息里）。对方引用那张"
        "图问「词条发我一份 / 这张用的什么提示词 / 这张的种子是多少 / 按这个"
        "再来一张」时，就把引用正文里那个编号填进来。返回的是**当时跑的那段"
        "原文加种子**——可以直接转述给对方，也可以拿去 generate_image 重画"
        "（换个提示词、同一个种子再来一张，就是靠这条路）。\n"
        "引用的那行字是「编号 · 分辨率 · 渠道 · seed 数字」（如 "
        "HT-20261001-081132-772 · 1024×1536 · anima_soft · seed 4100493889）："
        "分辨率、渠道、种子当场就能答，不用查；要查的只有提示词"
        "**和当初那个种子的存档**（对方只报了个号、图上那行没带 seed 时才查得到）。\n"
        "⚠️ 绝对不要自己编提示词、也不要瞎猜种子：你记不住当时写的什么（对话"
        "历史会被压缩），编出来的和原图对不上，对方照着跑会翻车。查不到就老实"
        "说不知道。"
    ),
    "function": _recall_image,
    "parameters": {
        "type": "object",
        "properties": {
            "tag": {
                "type": "string",
                "description":
                    "图号，如「HT-20261001-081132-772」。把对方引用正文里看到"
                    "的那串填进来就行，贴一整段引用正文也可以（会自动识别）。",
            },
        },
        "required": ["tag"],
    },
}
