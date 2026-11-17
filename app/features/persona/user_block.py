"""把第一段拆好的用户消息拼成演员层能读的块。"""

from app.features.persona import writing_style
from app.features.persona.prompts import _SCENE_DUTY


def render_user_block(segments, addressed=True, fallback="", remind=False,
                      name=""):
    """分段渲染；没有分段就退回原文。

    「想」要带内容并标明她听不见；原文在有分段时也完整补上，避免动作和
    语气被摘要吃掉。`remind` 只在当前轮加统一尾注。
    """
    if not segments:
        body = str(fallback or "")
        stripped = body.strip()
        if remind and stripped:
            return stripped + "\n" + writing_style.tail(name=name)
        if "\n" in stripped:
            return ("【玩家这一轮写的主输入框原文】\n"
                    "这是**他这一拍要做什么**，不是已经发生的旁白，是给你的指令。"
                    "里面可能混着三种东西：\n"
                    "· **他要做的动作**——走过去、伸手、拉起来、摘下帽子、抱住……\n"
                    "· **他说的话**——带「」『』的，或者明显是冲着人说的那一句；\n"
                    "· **他心里想的**——写了「心想」「心里」「暗想」的，她看不见。\n"
                    "\n"
                    "**换行是他自己分的段**，按行读，别合并。\n"
                    "**没有引号的句子不要一律当成台词**：句子里只要有一个明确的动作"
                    "（走、站、坐、拿、拉、抱、推、摘、递……），那个动作就是「做」，"
                    "要写进正文。真正的台词只有那种「冲着人说、没有动作」的句子。\n\n"
                    + body
                    + "\n\n" + _SCENE_DUTY)
        return fallback
    lines = ["【这一轮你收到的（按发生顺序）】"]
    for seg in segments:
        who = str(seg.get("who") or "用户")
        typ = str(seg.get("type") or "说")
        text = str(seg.get("text") or "").strip()
        if who == "导演" or typ in ("剧情", "plot"):
            if text:
                lines.append("- 这一拍往这个方向走，让它真的发生在画面里：%s" % text)
            continue
        if who == "助手":
            if typ == "说":
                label = "助手 · 说（已经发生）"
            elif typ == "做":
                label = "助手 · 做（已经发生，照它往下演）"
            else:
                label = "助手 · 想（只有你自己知道）"
            if text:
                lines.append("- %s：%s" % (label, text))
            continue
        if typ == "想":
            if text:
                lines.append("- 用户 · 想（心里想的，她听不见、不知道内容；"
                             "但你的写法要让读者看出他心里有这句）：%s" % text)
            else:
                lines.append("- 用户 · 想（有一句没说出口，内容他没写）")
            continue
        if not text:
            continue
        if typ == "说":
            label = "用户 · 说（你听得见）"
        else:
            label = "用户 · 做（你看得见）"
        lines.append("- %s：%s" % (label, text))
    if not addressed:
        lines.append("（他这一轮不是在对你说，可能只是自言自语或旁白；"
                     "写你看到的反应，不要当成他在跟你对话。）")
    if str(fallback or "").strip():
        lines.append("")
        lines.append("【他这一轮的原话（完整，照着它演）】")
        lines.append("（这段里的「我」= 玩家本人，不是你；原文里的动作都是玩家做的。）")
        lines.append(str(fallback).strip())
    lines.append("")
    lines.append(_SCENE_DUTY)
    if remind:
        lines.append(writing_style.tail(name=name))
    return "\n".join(lines)


def _visible_user_text(segments, fallback=""):
    """给 RAG 用的「用户可见文本」：只留说 / 做，不含想。"""
    if not segments:
        return str(fallback or "").strip()
    parts = []
    for seg in segments:
        if not isinstance(seg, dict):
            continue
        if str(seg.get("who") or "用户") != "用户":
            continue
        if str(seg.get("type") or "说") in ("想", "剧情", "plot"):
            continue
        text = str(seg.get("text") or "").strip()
        if text:
            parts.append(text)
    return "\n".join(parts)
