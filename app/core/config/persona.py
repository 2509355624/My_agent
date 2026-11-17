"""角色扮演模型配置：默认 provider/模型、玩家可选白名单。"""

import os

# 角色扮演专用默认模型（2026-11-06 用户拍板）：
# 扮演链路的调用（主回复 / 导演 / 开场 / 小记压缩 / RAG 提炼）默认优先用这一组；
# 请求失败仍走全局 LLM_FALLBACK_CHAIN。留空 = 用全局默认。
# 玩家在网页里临时指定自定义模型时，那个优先（见 `persona._llm_call`）。
PERSONA_PROVIDER = os.getenv("PERSONA_PROVIDER", "cx_a1b2c3d4").strip()

# 默认模型换成 v4.1-flash（2026-11-14 用户拍板 + 实测）：
# 同一段成人向 prompt，v4.1f 5.1 秒写完 730 字，v4p 11.4 秒写到 697 字突然
# 吐出「Sorry, I can't respond to this question.」然后停住——群友看到的
# 「回复被截断」就是它。v4p 更细腻，但会在露骨处自我审查，所以降级成可选项。
PERSONA_MODEL = os.getenv("PERSONA_MODEL", "deepseek-v4.1-flash").strip()

# 网页端玩家能挑的模型（2026-11-14）。**只放这一份白名单**：
# 前端不写死、也不允许玩家传任意模型名——providers 里能直连付费官方的那几个
# 露出去，等于把站主的 Key 送人。
# `default` 那个是没选过时的落点。
PERSONA_MODELS = [
    {"id": "deepseek-v4.1-flash",
     "label": "V4.1 Flash",
     "note": "快，写得完整，默认",
     "provider": "cx_a1b2c3d4"},
    {"id": "deepseek-v4-pro",
     "label": "V4 Pro",
     "note": "更细腻，慢一倍，个别情况下会中途停笔",
     "provider": "cx_a1b2c3d4"},
]
PERSONA_MODEL_DEFAULT = os.getenv("PERSONA_MODEL_DEFAULT",
                                  "deepseek-v4.1-flash").strip()


def persona_model_ids():
    """白名单里的模型 id 集合。调用方用它挡住玩家乱传的模型名。"""
    return {m["id"] for m in PERSONA_MODELS}


def persona_model_of(model_id):
    """按 id 取一条白名单记录；不在白名单里返回 None。"""
    want = str(model_id or "").strip()
    for m in PERSONA_MODELS:
        if m["id"] == want:
            return m
    return None


__all__ = [
    "PERSONA_PROVIDER", "PERSONA_MODEL", "PERSONA_MODELS",
    "PERSONA_MODEL_DEFAULT", "persona_model_ids", "persona_model_of",
]
