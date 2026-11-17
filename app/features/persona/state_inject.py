# -*- coding: utf-8 -*-
"""把当前命中的状态原文拼进这一轮。

只做拼装，不做选择、不做摘要、不设字数上限。`state_policy` 已经按
当前数值取好块；这里按顺序原样贴出来。
"""

from __future__ import annotations

from app.features.persona import state_policy


_HEADER = (
    "## 当前状态规则（原书里当前档位的原文）\n\n"
    "下面只列这一轮实际命中的档位；没命中的旧档原文不在本轮提示词里。\n"
    "按这些原文演，不要把档位名说出口。"
)


def build(aff_res, gd=None) -> str:
    """返回可直接拼进 dynamic 区的字符串；没有命中返回空串。

    V2 的成人向轴由 `adult.block()` 单独描述，这里不拿 V1 的 0 值去冒充
    堕落/屈服；V2 的关系事实由 `_v2_injection` 负责。只有 V1（以及未来
    真正带这些轴的状态）才走这份原书选择。
    """
    if not isinstance(aff_res, dict) or not aff_res.get("active"):
        return ""
    if aff_res.get("schema") == 2:
        return ""
    text = state_policy.selected_text(aff_res, gd)
    if not text:
        return ""
    return _HEADER + "\n\n" + text
