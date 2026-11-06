# -*- coding: utf-8 -*-
"""角色 LoRA 库（2026-11-04）：出图前按 prompt 里的角色名动态挂角色 LoRA。

背景：底模（anima13 系）训练数据里没有大部分 IP 角色，画谁都不像。解法是
给角色配专门的 LoRA：prompt 里出现角色名 → 自动把对应 LoRA 插进工作流、
触发词追加进提示词。代码只做「检测 + 装填」，画谁、像不像，交给模型和 LoRA——
没命中就完全不干预，不做任何准入闸门。

事实源：`skills/<skill>/char_loras.json`（跟工作流放一起）。条目：

    {"key": "sunna", "name": "苏娜",
     "file": "sunna-anima_20260216_2230.safetensors",
     "trigger": "sunnazzz", "strength": 0.8,
     "aliases": ["sunna", "苏娜"]}

- `file` 相对 ComfyUI 的 loras 目录（extra_model_paths.yaml 里所有 loras 项
  + COMFYUI_ROOT/models/loras）。**文件不存在时跳过装填只记日志**——宁可
  这张角色不像，不能让整单生图报 value_not_in_list。
- 只有 `CHAR_LORA_SKILLS` 里的渠道参与匹配；其他渠道零干预。

装填（`patch`）：角色节点插在**固定 LoRA 链的链头之前**（链头 = model 指向
非 LoRA 节点的那个 LoraLoader）。anima-line 实测两段采样用**两个不同底模**
（anima13 主采样挂 6 个固定 LoRA；silvermoonmix 精修 denoise 0.25、本来就不
挂 LoRA）——挂在固定链上意味着角色 LoRA 与现有 6 个 LoRA 同生共死、喂哪段
就跟哪段，二段的链完全不碰。

模板不动：patch 在 load 出来的 dict 深拷贝上做。load_workflow 解析时
`__SEED__` 占位符已补引号，dumps 回去后占位符照常被
`apply_prompt_placeholders` 替换（和 `lora_slots` 同一条理由：绝不
json.dump 回写模板文件）。
"""

import copy
import json
import logging
import os

import yaml

from app.config import BASE_DIR, COMFYUI_ROOT, SKILLS_DIR

log = logging.getLogger("char_loras")

# 参与角色装填的渠道（2026-11-06 用户拍板扩到 8 个）。
# 只收「用户真在用」的那几个：silver 是默认渠道，hd_3 是高清线，
# anima-line / anima-real 是主线。silverqaq、anima-flesh（幼感）、
# anima-guizhencao 用得少，先不加——加了也只是多一处要维护的地方。
CHAR_LORA_SKILLS = frozenset((
    "anima_clear", "anima-line", "anima-real",
    "hd_3_clear", "hd_3_curvy", "hd_3_gloss", "hd_3_soft",
    "silver", "silver-hd",
))
_INDEX_NAME = "char_loras.json"


def _loras_dirs():
    """ComfyUI 实际搜的 loras 目录（默认目录 + extra_model_paths.yaml 全部 loras 项）。"""
    dirs = [os.path.join(COMFYUI_ROOT, "models", "loras")]
    yaml_path = os.path.join(COMFYUI_ROOT, "extra_model_paths.yaml")
    try:
        with open(yaml_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except OSError:
        data = {}
    for section in data.values():
        if not isinstance(section, dict):
            continue
        base = section.get("base_path")
        rel = section.get("loras")
        if base and rel:
            dirs.append(os.path.join(str(base), str(rel)))
    out, seen = [], set()
    for d in dirs:
        d = os.path.normpath(d)
        if d not in seen:
            seen.add(d)
            out.append(d)
    return out


def _find_file(name):
    for d in _loras_dirs():
        p = os.path.join(d, str(name))
        if os.path.isfile(p):
            return p
    return None


def _index_path(skill):
    """这个渠道用哪本角色库。

    2026-11-06：改成「渠道自己有一本就用自己那本，没有就用公共库」。
    原来每个渠道一份，加一个渠道就要复制一遍 JSON——同一个角色十几份副本，
    改一处忘一处。公共库放 `config/char_loras.json`，各渠道可继续单独覆盖。
    """
    own = os.path.join(str(SKILLS_DIR), skill, _INDEX_NAME)
    if os.path.isfile(own):
        return own
    return os.path.join(str(BASE_DIR), "config", _INDEX_NAME)


def entries(skill):
    """某渠道的角色库条目列表；没配 / 坏文件 / 渠道没开 → 空列表。"""
    if skill not in CHAR_LORA_SKILLS:
        return []
    try:
        with open(_index_path(skill), "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    chars = data.get("characters") if isinstance(data, dict) else None
    if not isinstance(chars, list):
        return []
    return [c for c in chars if isinstance(c, dict) and c.get("file")]


def match(skill, prompt):
    """用户原话 → 命中的角色条目；没点名就 None（2026-11-06 改）。

    这里**不再做别名硬匹配**（用户拍板）：硬匹配覆盖不了「绝区零的千夏」
    「把我上次那个绿头发的放出来」，错了也不会自纠。现在交给角色库 AI——
    向量召回 top-k + 大模型判定，判定不出明确人名就 skip，让生图 AI 自己发挥。

    文件缺失仍然跳过（记 warning），不拦整单。
    """
    if skill not in CHAR_LORA_SKILLS:
        return None
    try:
        from app import lora_lib
        entry = lora_lib.resolve(prompt)
    except Exception:                                  # noqa: BLE001
        log.warning("角色库检索异常，跳过装填", exc_info=True)
        return None
    if not entry:
        return None
    chans = entry.get("channels") or []
    if chans and skill not in chans:
        log.info("角色 %s 不在渠道 %s 的白名单里，跳过",
                 entry.get("key"), skill)
        return None
    if not _find_file(entry.get("file") or ""):
        log.warning("角色 LoRA 文件不在 loras 目录，跳过装填：%s（%s）",
                    entry.get("key") or "?", entry.get("file"))
        return None
    return entry


def apply(skill, prompt, workflow, user_text=""):
    """命中角色就把 LoRA 节点插进 workflow 副本、触发词追加进 prompt。

    返回 `(workflow, prompt)`；没命中时原样返回，调用方零改动。网页跑图台
    （`build_t2i_workflow`）和 QQ 直达生图（`_generate_image`）都走这一份，
    避免两条路各写一遍装填逻辑、其中一条漏掉。
    """
    # 判断用**用户原话**（2026-11-06）：`prompt` 到这里已经是翻译好的
    # 英文 tag，拿它去判等于让角色库读机器人自己写的文本。网页跑图台
    # 没有「原话」，传空就退回用 prompt 本身，行为不变。
    entry = match(skill, str(user_text or "").strip() or prompt)
    if not entry:
        return workflow, prompt
    wf = patch(workflow, entry)
    parts = [prompt.rstrip(" ,")]
    if entry.get("identity"):
        # 模型写英文 tag 时经常自己补一套错的外观（黑发、校服…），和 LoRA
        # 打架；用带权重的固定外观压回去，发色 / 瞳色 / 猫耳这些是身份，
        # 不跟着衣服走。
        parts.append(str(entry["identity"]))
    if entry.get("trigger"):
        parts.append(str(entry["trigger"]))
    return wf, ", ".join(p for p in parts if p)


def patch(workflow, entry):
    """把角色 LoRA 插进工作流 dict 的模型链头。返回新 dict，**不动模板**。

    插桩位置：**固定 LoRA 链的链头之前**（链头 = model 指向非 LoRA 节点的
    那个 LoraLoader）。这样角色 LoRA 和现有固定 LoRA 同一条链、同生共死，
    喂哪段采样就跟哪段——anima-line 实测是两段采样用**两个不同底模**
    （anima13 主采样挂 6 个固定 LoRA；silvermoonmix 精修 denoise 0.25 且
    本来就不挂 LoRA），角色特征由一段决定，二段的链一根手指都不该碰。
    """
    wf = copy.deepcopy(workflow or {})
    lora_cls = ("LoraLoader", "LoraLoaderModelOnly")
    try:
        strength = max(0.0, min(2.0, float(entry.get("strength", 0.8))))
    except (TypeError, ValueError):
        strength = 0.8

    def _new_node(up_ref):
        nid_new = "char_lora"
        while nid_new in wf:
            nid_new = "char_lora_%d" % (len(wf) + 1)
        wf[nid_new] = {
            "class_type": "LoraLoaderModelOnly",
            "inputs": {"model": list(up_ref),
                       # 子目录要用**本机分隔符**（2026-11-06）：ComfyUI 的
                       # 下拉列表在 Windows 上是 `anima\xxx.safetensors`，
                       # 写成 `anima/xxx` 会被 prompt 校验判 value_not_in_list，
                       # 整单 400。索引里两种写法都收，这里统一转换。
                       "lora_name": str(entry["file"]).replace("/", os.sep),
                       "strength_model": strength},
            "_meta": {"title": "角色 %s" % (entry.get("name")
                                            or entry.get("key") or "")},
        }
        return nid_new

    # 1) 首选：挂在现有 LoRA 链的链头之前
    head = None
    for nid, nd in wf.items():
        if str((nd or {}).get("class_type") or "") not in lora_cls:
            continue
        m = (nd.get("inputs") or {}).get("model")
        if isinstance(m, list) and m:
            src_cls = str((wf.get(str(m[0])) or {}).get("class_type") or "")
            if src_cls not in lora_cls:
                head = nid
                break
    if head is not None:
        up_ref = wf[head]["inputs"]["model"]
        nid_new = _new_node(up_ref)
        wf[head]["inputs"]["model"] = [nid_new, 0]
        return wf
    # 2) 兜底：工作流没有任何 LoRA 节点 → 插到模型加载节点与其消费者之间
    src = None
    for nid, nd in wf.items():
        if str((nd or {}).get("class_type") or "") in (
                "UNETLoader", "CheckpointLoaderSimple"):
            src = nid
            break
    if src is None:
        log.warning("工作流里没有模型加载节点，角色 LoRA 装填跳过")
        return workflow
    consumers = [nid for nid, nd in wf.items()
                 if isinstance((nd or {}).get("inputs"), dict)
                 and isinstance(nd["inputs"].get("model"), list)
                 and nd["inputs"]["model"][:1] == [src]]
    if not consumers:
        log.warning("模型加载节点没有 model 消费者，角色 LoRA 装填跳过")
        return workflow
    nid_new = _new_node([src, 0])
    for nid in consumers:
        wf[nid]["inputs"]["model"] = [nid_new, 0]
    return wf
