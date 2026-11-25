"""角色扮演模型配置：默认 provider/模型、玩家可选白名单。"""

import os

# 角色扮演专用默认模型（2026-11-06 用户拍板）：
# 扮演链路的调用（主回复 / 导演 / 开场 / 小记压缩 / RAG 提炼）默认优先用这一组；
# 请求失败仍走全局 LLM_FALLBACK_CHAIN。留空 = 用全局默认。
# 玩家在网页里临时指定自定义模型时，那个优先（见 `persona._llm_call`）。
PERSONA_PROVIDER = os.getenv("PERSONA_PROVIDER", "cx_a1b2c3d4").strip()

# 默认模型换成 V4.1 Flash（2026-11-14 用户拍板 + 实测）：
# 同一段成人向 prompt，v4.1f 5.1 秒写完 730 字，v4p 11.4 秒写到 697 字突然
# 吐出「Sorry, I can't respond to this question.」然后停住——群友看到的
# 「回复被截断」就是它。v4p 更细腻，但会在露骨处自我审查，所以降级成可选项。
#
# 2026-11-24：模型名带上中转站的后缀 `-1017`。同一家中转站上
# `deepseek-v4.1-flash` 与 `deepseek-v4.1-flash-1017` **不是同一个可路由的
# 名字**，用量记录里真实跑通的是后者；不带后缀那条在某些时段会直接报错。
# v4p 也是同一个毛病，所以它从「可选」变成「这家中转站上没有」——白名单里
# 不再列它，玩家手里存着的旧选择会按未知模型回落默认（见 `persona.current_model`）。
PERSONA_MODEL = os.getenv("PERSONA_MODEL", "deepseek-v4.1-flash-1017").strip()

# 网页端玩家能挑的模型（2026-11-14）。**只放这一份白名单**：
# 前端不写死、也不允许玩家传任意模型名——providers 里能直连付费官方的那几个
# 露出去，等于把站主的 Key 送人。
# `default` 那个是没选过时的落点。
PERSONA_MODELS = [
    {"id": "deepseek-v4.1-flash-1017",
     "label": "V4.1 Flash",
     "note": "快，写得完整，默认",
     "provider": "cx_a1b2c3d4"},
]
PERSONA_MODEL_DEFAULT = os.getenv("PERSONA_MODEL_DEFAULT",
                                  "deepseek-v4.1-flash-1017").strip()

# 公共免费那一行在页面上显示成什么（2026-11-24）。
# 它只用来画字：原来前端/admin 侧写死「公用 V4 Pro」，模型一换，这一行就和
# 实际跑的模型对不上，玩家会以为 v4p 还在。真要改名字，只改这里。
PERSONA_PUBLIC_LABEL = os.getenv("PERSONA_PUBLIC_LABEL", "公用 V4.1 Flash").strip()


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
    "PERSONA_MODEL_DEFAULT", "PERSONA_PUBLIC_LABEL",
    "persona_model_ids", "persona_model_of",
]
