# -*- coding: utf-8 -*-
"""ANIMA 随心配（随机配）后端（2026-11-13 用户拍板）。

用户一句话需求 → 后端随机抽卡出图：
- **模型池**：从 ANIMA 底模里随机选 2 个（**可重复**），一个填一段采样
  （`__MODEL_1__`）、一个填二段采样（`__MODEL_2__`）。
- **LoRA 池**：从 ANIMA 画风 LoRA 里随机选 5 个（**不重复**），strength 统一 0.8，
  依次填进 5 个 `LoraLoaderModelOnly` 槽。
- **角色卡（第 6 槽）**：用户点名角色（如「绝区零千夏」）→ 走 `char_loras.apply`
  把角色 LoRA 插到 LoRA 链头之前 + 追加 identity/trigger，跟现有角色库一致。
- **seed**：记录本次实际用的 seed，随参数卡片返回，供复刻同一张画风。

工作流骨架 `skills/_anima_random/workflow.json` 复刻 `_anima_multi_batch` 的
双模型两段采样（DualModelBatchPromptImageGenerator），只把模型 / LoRA 换成
`__MODEL_*__` / `__LORA_*__` 占位符，步数 15×15。

**代码只做「抽卡 + 替换占位符 + 记录参数」，不判断画得好不好**——抽到什么
就是什么，满意与否交给用户自己看。唯一例外：抽卡结果里 LoRA 文件 / 模型
文件实际不存在时，跳过重抽（宁可少一个，不让整单 value_not_in_list 400）。
"""

import copy
import logging
import random

log = logging.getLogger("anima_random")

# 技能 id（`_` 前缀 → `list_skills()` 跳过，只由 decide 内部点名加载）
SKILL_ID = "_anima_random"

# ── 模型池：ANIMA 系底模（不含角色底模）─────────────────────────────
# 一段 / 二段各从这池里选一个，可重复。角色 LoRA 是另一条链（char_loras），
# 底模只放「画风 / 美型」底模，不放任何角色专属底模。
MODEL_POOL = (
    "miaomiaoRealskin_anima13.safetensors",
    "miaomiaoAnimeReality_ani11_3087842.safetensors",
    "miaomiaoHarem_anima16.safetensors",
    "miaomiaoRealskin_anima29B10.safetensors",
    "silvermoonmixAnimaEvolved_v2329BTurbo.safetensors",
    "anima_aestheticV11.safetensors",
)

# ── LoRA 池：ANIMA 系画风 LoRA（全收，真·随机抽卡）──────────────────
# 用户拍板「全收」：鬼针草 / Artist-Style / Style 这类有明确画风倾向的也进池，
# 抽到什么是什么，风格跨度大正是「抽卡」的乐趣。角色 LoRA 不在此列。
LORA_POOL = (
    "ANIMA_MODARE_V2.safetensors",
    "Anima_Turbo_LoRA_v0.2.safetensors",
    "Anima_in_real_update_v2_epoch_20.safetensors",
    "Artist-Style-gulugulu.safetensors",
    "Artist-Style-khstyle.safetensors",
    "Style-Onineko.safetensors",
    "Style-gpt-image-2.safetensors",
    "_cowani_epoch_10.safetensors",
    "anima_context_detailer_base10.safetensors",
    "anima_鬼针草_2.2.safetensors",
    "anima_鬼针草_v4.0.safetensors",
    "qaq.safetensors",
    "reina_v1_epoch14.safetensors",
    "snima_鬼针草_V3.9.safetensors",
    "star cat-000022.safetensors",
    "画风-anima-baka skin.safetensors",
    "画风-anima-okitatsuki-kibro.safetensors",
)

LORA_COUNT = 5          # 随机 LoRA 槽数
LORA_STRENGTH = 0.8     # 统一强度


def draw(rng=None):
    """随机抽一张卡：返回 `{"models": [m1, m2], "loras": [l1..l5]}`。

    - 模型：可重复选 2 个（`random.choices`）。
    - LoRA：不重复选 5 个（`random.sample`，池子 ≥ 5 才有意义）。
    `rng` 传 `random.Random(seed)` 可复现（测试用），默认用全局随机。
    """
    r = rng or random
    models = r.choices(MODEL_POOL, k=2) if len(MODEL_POOL) >= 2 else list(MODEL_POOL)
    # sample 在池子 < 5 时抛 ValueError，但池子固定 17 个，正常不会走到；
    # 兜底：池子不够就全取（不足 5 个槽留空由替换阶段处理）。
    if len(LORA_POOL) >= LORA_COUNT:
        loras = r.sample(LORA_POOL, LORA_COUNT)
    else:
        loras = list(LORA_POOL)
    return {"models": list(models), "loras": list(loras)}


def apply_random(workflow, draw_result):
    """把抽卡结果写进工作流 dict 的占位符。返回新 dict，**不动模板**。

    - `__MODEL_1__` → draw_result["models"][0]，`__MODEL_2__` → models[1]
    - `__LORA_1__` ~ `__LORA_5__` → draw_result["loras"][i]
    占位符替换走字符串（和 `apply_prompt_placeholders` 同一条路，绝不
    json 回写模板文件）。
    """
    wf = copy.deepcopy(workflow or {})
    models = draw_result.get("models") or []
    loras = draw_result.get("loras") or []
    subs = {}
    for i in range(2):
        if i < len(models):
            subs["__MODEL_%d__" % (i + 1)] = models[i]
    for i in range(LORA_COUNT):
        if i < len(loras):
            subs["__LORA_%d__" % (i + 1)] = loras[i]
    text = json_dumps_safe(wf)
    for k, v in subs.items():
        text = text.replace(k, v)
    return json_loads_safe(text)


def json_dumps_safe(wf):
    import json
    return json.dumps(wf, ensure_ascii=False)


def json_loads_safe(text):
    import json
    return json.loads(text)


def describe(draw_result, seed=None):
    """把抽卡结果拼成参数卡片文本（返回卡片第 2 条）。"""
    models = draw_result.get("models") or []
    loras = draw_result.get("loras") or []
    lines = []
    lines.append("【ANIMA 随心配】抽到这一张")
    lines.append("模型：")
    for i, m in enumerate(models, 1):
        lines.append("  %d. %s" % (i, m.replace(".safetensors", "")))
    lines.append("LoRA（%d 个，强度 %.1f）：" % (len(loras), LORA_STRENGTH))
    for i, l in enumerate(loras, 1):
        lines.append("  %d. %s" % (i, l.replace(".safetensors", "")))
    if seed is not None:
        lines.append("seed：%s" % seed)
    return "\n".join(lines)
