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
import re

import yaml

from app.config import BASE_DIR, COMFYUI_ROOT, SKILLS_DIR

log = logging.getLogger("char_loras")

# 参与角色装填的渠道，**按底模架构分组**（2026-11-06 用户拍板：角色库不只服务
# anima）。同一个角色在不同架构下是不同文件，靠 `channels` 说归属——索引里允许
# 同一个 key 出现多条，`entry_by_key` 按渠道挑那一条。
#
#   anima  miaomiaoRealskin_anima13 / silvermoonmixAnimaEvolved
#   sdxl   miaomiaoRealskin_epsV13（miao）/ NoobAI（cunny、jank）/ Illustrious（nffa）
#   sd15   unholyDesireMixSinister（image_gen_v1、nsfw_pose_gen）
#   moss   moss_image_sft_int8_convrot
#
# krea2（gguf）和 qwen（gguf）不吃这几类 LoRA，不放进来。
_LORA_SKILLS_ANIMA = (
    "anima_clear", "anima-line", "anima-real",
    "hd_3_clear", "hd_3_curvy", "hd_3_gloss", "hd_3_soft",
    "silver", "silver-hd",
)
_LORA_SKILLS_SDXL = ("miao", "cunny", "jank", "nffa")
_LORA_SKILLS_SD15 = ("image_gen_v1", "image_gen_v1_hires", "nsfw_pose_gen")
_LORA_SKILLS_MOSS = ("moss", "moss-light", "moss-qunyou", "moss50", "moss8",
                     "moss_page")
CHAR_LORA_SKILLS = frozenset(_LORA_SKILLS_ANIMA + _LORA_SKILLS_SDXL
                             + _LORA_SKILLS_SD15 + _LORA_SKILLS_MOSS)
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


def _read_index(path):
    """读一本角色库，返回带 `file` 的条目；坏文件 / 结构不对 → 空列表。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    chars = data.get("characters") if isinstance(data, dict) else None
    if not isinstance(chars, list):
        return []
    return [c for c in chars if isinstance(c, dict) and c.get("file")]


def entries(skill):
    """某渠道的角色库条目列表；没配 / 坏文件 / 渠道没开 → 空列表。"""
    if skill not in CHAR_LORA_SKILLS:
        return []
    return _read_index(_index_path(skill))


def entry_by_key(key, skill=None):
    """按 key 取条目（2026-11-06）：**大大怪自己挑好了，代码只负责查**。

    取库顺序与 `match` 一致：先看这个渠道自己的库（可能覆盖过），再看公共库
    `config/char_loras.json`。查不到返回 None —— 调用方当作「没点名」,
    不装填、也不拦整单。渠道白名单与文件存在性仍由 `_accept` 把关。

    **同一个 key 允许多条**（2026-11-06）：一个角色在 anima / SDXL / SD1.5 下
    是三份不同文件，各条用自己的 `channels` 声明归属。这里按渠道挑那条——
    先挑 channels 明确写了本渠道的，再挑不限渠道的兜底，都没有就退回第一条
    （交给 `_accept` 按白名单拒掉，不会错挂）。
    """
    k = str(key or "").strip()
    if not k:
        return None
    pools = []
    if skill:
        pools.append(_index_path(skill))
    common = os.path.join(str(BASE_DIR), "config", _INDEX_NAME)
    if common not in pools:
        pools.append(common)
    hits = []
    for p in pools:
        for c in _read_index(p):
            if str(c.get("key")) == k:
                hits.append(c)
    if not hits:
        return None
    if skill:
        for c in hits:
            chans = c.get("channels") or []
            if chans and skill in chans:
                return c
    for c in hits:
        if not (c.get("channels") or []):
            return c
    return hits[0]


def _accept(skill, entry):
    """渠道白名单 + LoRA 文件存在性：两条都过才装填，否则 None。"""
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
    return _accept(skill, entry)


def _strip_conflicts(prompt):
    """把翻译稿里跟角色身份打架的 tag 摘掉（2026-11-06）。

    起因：用户点「爱弥斯」，生图提示词里却是
    `long white hair, pale blue eyes, original character, aemeath concept`——
    爱弥斯是粉发金瞳。翻译模型不认识角色时会自己编一套外观，
    `original character` 更是直接告诉底模「这不是已知角色」，与角色 LoRA 对着干。

    只摘三类（其余一律不动，免得把用户真想要的东西删掉）：
      1. `original character` / `oc` / `<名字> concept` 这种「这是原创」的宣告；
      2. 发色 / 瞳色（它们是身份，必须由索引里的 identity 决定）；
      3. 因上面两条留下的空项。
    衣服、动作、构图、镜头一律保留——那些是用户的意图。
    """
    if not prompt:
        return prompt
    drop = re.compile(
        r"^(?:original character|oc|unknown character"
        r"|(?:[a-z0-9_]+ ){0,2}concept"
        r"|(?:[a-z]+[- ])?(?:hair|eyes))$", re.I)
    hair_eye = re.compile(
        r"\b(?:white|black|blonde|blond|brown|pink|blue|green|red|purple|"
        r"grey|gray|silver|gold|yellow|orange|aqua|cyan|multicolored|"
        r"gradient|two-tone)\s+(?:hair|eyes)\b", re.I)
    out = []
    for tok in str(prompt).split(","):
        t = tok.strip()
        if not t:
            continue
        if drop.match(t.lower()) or hair_eye.search(t):
            continue
        out.append(t)
    return ", ".join(out)


def apply(skill, prompt, workflow, user_text="", key=""):
    """命中角色就把 LoRA 节点插进 workflow 副本、触发词追加进 prompt。

    返回 `(workflow, prompt)`；没命中时原样返回，调用方零改动。网页跑图台
    （`build_t2i_workflow`）和 QQ 直达生图（`_generate_image`）都走这一份，
    避免两条路各写一遍装填逻辑、其中一条漏掉。

    `key`（2026-11-06）：大大怪在候选块里已经挑好的角色 key。给了就直接查库，
    **不再走向量召回 + 大模型判定**；空串 = 旧行为（走 `match`），这条默认值
    就是整套改动的回滚开关。
    """
    key = str(key or "").strip()
    if key:
        # 渠道不在白名单时零干预：krea2 / qwen / moss / nai 这些本来就不挂
        # 角色 LoRA，模型就算填了 key 也不生效（用户点 krea2 就不会被改成
        # silver）。渠道内白名单 + 文件存在性仍由 `_accept` 把关。
        entry = _accept(skill, entry_by_key(key, skill))
        if not entry:
            log.info("[role] key=%s 在渠道 %s 不可用，不装填", key, skill)
            return workflow, prompt
    else:
        # 判断用**用户原话**（2026-11-06）：`prompt` 到这里已经是翻译好的
        # 英文 tag，拿它去判等于让角色库读机器人自己写的文本。网页跑图台
        # 没有「原话」，传空就退回用 prompt 本身，行为不变。
        entry = match(skill, str(user_text or "").strip() or prompt)
    if not entry:
        return workflow, prompt
    wf = patch(workflow, entry)
    # 装填成功也留一条（2026-11-06）：从前只在跳过时打日志，成功一声不响，
    # 线上排查「到底挂没挂」只能去翻 ComfyUI history。大模型填的 key 和旧路
    # 判定分开标，一眼能看出走的是哪条。
    log.info("[role] 装填 %s ← 渠道 %s（%s）", entry.get("key"), skill,
             "模型填 key" if key else "旧路判定")
    # 命中之后先**洗掉翻译稿里跟角色打架的那几个 tag**（2026-11-06 用户报
    # 「爱弥斯被画成白发蓝瞳」）：翻译模型不认识角色时会自己编一套外观，
    # 还会加 `original character`——那句等于告诉底模「这不是已知角色」。
    # 不清掉的话，后面再补 identity 也是两边打架，谁赢看运气。
    extra = _strip_conflicts(prompt)
    # ── 顺序（2026-11-06 用户拍板）──────────────────────────────
    #
    # **原始角色提示词放最前面**，翻译稿里的其余需求（衣服、动作、场景、
    # 构图）跟在后面，触发词压尾。理由：
    #   · 角色库已经确认「这是谁」，身份就该由索引里的原始 tag 说了算；
    #   · 放最后时它只是「补充说明」，和前面那串放不对地方的描写抢注意力，
    #     经常被压过去（爱弥斯那次就是这么变成白发蓝瞳的）；
    #   · 用户要换衣服时，衣服 tag 在它后面，仍然生效——脸部/身体特征由
    #     前面那一段钉住，这正是「基于原始提示词改衣服、保住脸」。
    parts = []
    if entry.get("identity"):
        parts.append(str(entry["identity"]).strip(" ,"))
    if extra:
        parts.append(extra)
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
    #    moss（2026-11-06）：它的加载器是自定义节点 MOSSImageUNETLoader，
    #    output 也是 MODEL，但类名对不上上面那个元组，不加进来就整单跳过。
    src = None
    for nid, nd in wf.items():
        if str((nd or {}).get("class_type") or "") in (
                "UNETLoader", "CheckpointLoaderSimple", "MOSSImageUNETLoader"):
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
