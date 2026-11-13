# -*- coding: utf-8 -*-
"""ANIMA 随心配（随机配）后端（2026-11-13 用户拍板）。

用户一句话需求 → 后端随机抽卡出图：
- **模型池**：从 ANIMA 底模里随机选 2 个（**不重复 + 冷却 2 轮**），一个填一段
  采样（`__MODEL_1__`）、一个填二段采样（`__MODEL_2__`）。
- **LoRA 池**：从 ANIMA 画风 LoRA 里随机选 5 个（**不重复 + 冷却 3 轮**），
  strength 统一 0.8，依次填进 5 个 `LoraLoaderModelOnly` 槽。
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
import json
import logging
import os
import random
import threading
import time

from app.config import state_path

log = logging.getLogger("anima_random")

# 技能 id（`_` 前缀 → `list_skills()` 跳过，只由 decide 内部点名加载）
SKILL_ID = "_anima_random"

# 「最近一次随心配配方」落盘（2026-11-13）：`/存预设` 要抓「刚出的图」的
# 配方，但 anima_draw 只在出图那一刻活在 job 内存里、caption 里是文本，
# 没有结构化持久化。这里抽卡后写一份 {models, loras, seed} 到 state，
# `/存预设` 直接读它拿配方。这是「代码只登记事实」的延续——只存、不判断。
_LAST_PATH = state_path("anima_random_last.json")
_LAST_LOCK = threading.Lock()


def save_last(draw_result, seed=None, image_rel=None):
    """把一次抽卡结果（+seed + 成品图相对路径）落盘成「最近一次配方」，供 /存预设 读取。

    `draw_result` = draw() 的返回值 {"models", "loras"}。seed 是 generate_image
    那边 resolve 出来的真种子（int），跟 models/loras 一起配对存。
    `image_rel`（2026-11-13）是成品图的相对路径（如 `qq/群_1/anima/Anima_1_.png`），
    由 image_jobs 出图完成时补记——/存预设 用它抓「刚出的图」当预设封面。
    写失败只记日志不抛——这张图已经要出了，别为存个快照把它弄断。
    """
    payload = {
        "models": list(draw_result.get("models") or []),
        "loras": list(draw_result.get("loras") or []),
        "seed": seed,
        "ts": int(time.time()),
    }
    if image_rel:
        payload["image_rel"] = image_rel
    try:
        os.makedirs(os.path.dirname(_LAST_PATH), exist_ok=True)
        tmp = _LAST_PATH + ".%d.tmp" % os.getpid()
        with _LAST_LOCK:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
            os.replace(tmp, _LAST_PATH)
    except Exception as exc:                      # noqa: BLE001
        log.warning("落盘最近随心配配方失败：%s", exc)


def save_last_image(image_rel):
    """出图完成后**只补记**成品图路径（不动 models/loras/seed）。

    image_jobs 出图那一步只有相对路径，抽卡配方早在 save_last 时已落盘——
    分两个函数，避免出图侧还要回填配方、也避免「抽卡快照」和「成图路径」
    互相覆盖对方的字段。
    """
    data = load_last() or {}
    if not data.get("models"):
        return
    data["image_rel"] = image_rel
    try:
        os.makedirs(os.path.dirname(_LAST_PATH), exist_ok=True)
        tmp = _LAST_PATH + ".%d.tmp" % os.getpid()
        with _LAST_LOCK:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, _LAST_PATH)
    except Exception as exc:                      # noqa: BLE001
        log.warning("补记成品图路径失败：%s", exc)


def load_last():
    """读最近一次随心配配方。没有/读坏返回 None。"""
    try:
        with open(_LAST_PATH, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and data.get("models"):
            return data
    except Exception:                             # noqa: BLE001
        pass
    return None

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

# ── 冷却期（2026-11-13 用户反馈「多样性不够」后加的）────────────────
# 纯均匀随机 + 放回抽样会小样本扎堆（连着出 Harem/Harem、死磕 qaq），
# 用户要「抽卡更新鲜」。业界标准做法是**带冷却的抽样（cooldown sampling）**：
# 一个项被抽中后，接下来 N 轮不进池。这里：
#   · 底模：不重复选 2（去放回）+ 被抽中的底模冷却 2 轮
#   · LoRA：不重复选 5（去放回）+ 被抽中的 LoRA 冷却 3 轮
# 冷却语义是「进程内跨请求」的——5174 进程存活期间记住，重启从零开始。
MODEL_COOLDOWN = 2      # 底模冷却轮数
LORA_COOLDOWN = 3       # LoRA 冷却轮数

# 进程内最近抽签历史（deque 存每轮的 models/loras 集合）。只影响「不带 rng」
# 的生产路径；带 rng 的调用是纯函数（复刻/测试），不看也不写历史。
from collections import deque as _deque
_history = _deque()      # 每个元素 {"models": set, "loras": set}，按时间从旧到新
_HIST_MAX = max(MODEL_COOLDOWN, LORA_COOLDOWN)   # 只需留够最长冷却的轮数


def _cooled_pool(pool, k, cooldown, r, hist_key):
    """从 `pool` 里抽 `k` 个不重复的项，避开最近 `cooldown` 轮抽中过的项。

    冷却不是硬性失败：池子去掉冷却项后不足 k 个时，放回冷却项凑数（宁可
    少冷一点，不让抽卡失败）。返回抽中的列表，并登记进本轮历史。
    """
    if len(pool) <= k:
        # 池子本身就不够抽，冷却无从谈起，直接全取
        return list(pool)

    cooled = set()
    for h in list(_history)[-cooldown:]:
        cooled |= h[hist_key]

    fresh = [x for x in pool if x not in cooled]
    if len(fresh) >= k:
        picked = r.sample(fresh, k)
    else:
        # 冷却项太多，抽不满 k：先取尽 fresh，再从未冷却不足的部分补
        picked = list(fresh)
        need = k - len(picked)
        rest = [x for x in pool if x not in picked]
        picked += r.sample(rest, need)
    return picked


def draw(rng=None):
    """随机抽一张卡：返回 `{"models": [m1, m2], "loras": [l1..l5]}`。

    - 模型：不重复选 2 个（`random.sample`），并避开最近 `MODEL_COOLDOWN` 轮
      抽中过的底模。
    - LoRA：不重复选 5 个，并避开最近 `LORA_COOLDOWN` 轮抽中过的 LoRA。

    两个调用形态：
      · `draw()`（生产路径）：走全局随机 + 冷却历史，并**更新**历史。
      · `draw(rng=random.Random(seed))`（测试 / 复刻）：**纯函数**，不读不写
        历史，保证同种子同结果（复刻同一张卡）。
    """
    r = rng or random
    use_history = rng is None

    if use_history:
        models = _cooled_pool(MODEL_POOL, 2, MODEL_COOLDOWN, r, "models")
        loras = _cooled_pool(LORA_POOL, LORA_COUNT, LORA_COOLDOWN, r, "loras")
        _history.append({"models": set(models), "loras": set(loras)})
        while len(_history) > _HIST_MAX:
            _history.popleft()
    else:
        models = (r.sample(MODEL_POOL, 2)
                  if len(MODEL_POOL) >= 2 else list(MODEL_POOL))
        loras = (r.sample(LORA_POOL, LORA_COUNT)
                 if len(LORA_POOL) >= LORA_COUNT else list(LORA_POOL))

    return {"models": list(models), "loras": list(loras)}


def reset_history():
    """清空冷却历史（测试用 / 需要「重新开始冷却」时）。"""
    _history.clear()


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
