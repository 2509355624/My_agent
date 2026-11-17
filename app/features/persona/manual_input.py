"""玩家精确输入（心理活动 / 动作 / 台词 / 剧情）的清洗与渲染。

纯字符串处理，不读会话状态；编剧层和演员层拿到的是不同视图。
"""

_MANUAL_KEYS = ("thought", "action", "speech", "plot")

_LABELS = {
    "thought": "心理活动（她听不见、不知道内容；写的时候按它来安排她的反应）",
    "action": "动作（她看得见）",
    "speech": "台词（她听得见）",
    "plot": "剧情推进想法（导演层，不是她已知的事）",
}


def _manual_norm(manual):
    """洗一遍前端传来的精确输入：只留非空字符串，长度封顶。"""
    if not isinstance(manual, dict):
        return {}
    out = {}
    for k in _MANUAL_KEYS:
        v = str(manual.get(k) or "").strip()
        if v:
            out[k] = v[:2000]
    return out


def _manual_block(m):
    """给编剧 AI 看的标注块。心理活动也在这里——只有它能看见。"""
    if not m:
        return ""
    lines = ["【玩家这一轮明确标注了】"]
    for k in _MANUAL_KEYS:
        if m.get(k):
            lines.append("- %s：%s" % (_LABELS[k], m[k]))
    lines.append("（分段以这里为准。心理活动用来读潜台词；剧情推进写成"
                 "「这一拍往哪走」的方向；动作和台词照原样给她看。"
                 "心里话的原文留在编剧这一层。）")
    return "\n".join(lines)


def _manual_segments(m):
    """精确输入 → 手工分段。『想』带内容，但渲染层会把它藏起来。"""
    segs = []
    if m.get("thought"):
        segs.append({"who": "用户", "type": "想", "text": m["thought"]})
    if m.get("action"):
        segs.append({"who": "用户", "type": "做", "text": m["action"]})
    if m.get("speech"):
        segs.append({"who": "用户", "type": "说", "text": m["speech"]})
    if m.get("plot"):
        segs.append({"who": "导演", "type": "剧情", "text": m["plot"]})
    return segs


def _manual_display(m, text=""):
    """落历史/网页展示用的合成文本。演员不读这个，读的是 `seg`。"""
    parts = []
    if m.get("thought"):
        parts.append("（心里：%s）" % m["thought"])
    if m.get("action"):
        parts.append("（%s）" % m["action"])
    if m.get("speech"):
        parts.append("「%s」" % m["speech"])
    if m.get("plot"):
        parts.append("【剧情推进】%s" % m["plot"])
    if str(text or "").strip():
        parts.append(str(text).strip())
    return "\n".join(parts)
