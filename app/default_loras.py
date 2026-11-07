# -*- coding: utf-8 -*-
"""默认挂载 LoRA（2026-11-07）。

用户要的是「moss 渠道不管怎么换，这个风格 LoRA 都在」——不是角色 LoRA
（那个靠 `char_loras` 按点名挂），是**渠道级的底层风格**。

## 为什么插节点而不是改模板

moss 六个渠道的模板结构不一样：`moss-qunyou` / `moss-light` / `moss8` 有
`MOSSLoRAToggle`（一个节点并联若干槽），`moss` / `moss50` / `moss_page` 只有
`UNETLoader`，没有槽。

改模板得维护两套写法，还要提防槽位写满（qunyou 已经 20/20）。插节点
`LoraLoaderModelOnly` 对两类都成立：它接在模型加载节点和它的消费者之间，
和 `char_loras.patch` 找的是同一个位置。

## 和角色 LoRA 的关系

两者并存、互不干扰：
  UNETLoader → 默认 LoRA（本模块）→ 角色 LoRA（char_loras，按点名）→ 采样
节点 id 用 `dflora_<n>`，不会撞 `char_lora`。
"""
import copy
import json
import logging
import os

from app.config import BASE_DIR

log = logging.getLogger("default_loras")

_CFG = os.path.join(BASE_DIR, "config", "default_loras.json")
_CACHE = None

_LOADERS = ("UNETLoader", "CheckpointLoaderSimple", "MOSSImageUNETLoader")


def _load():
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    try:
        with open(_CFG, encoding="utf-8") as f:
            _CACHE = json.load(f) or {}
    except FileNotFoundError:
        _CACHE = {}
    except Exception:                                    # noqa: BLE001
        log.warning("默认 LoRA 配置读不了，这一轮不挂", exc_info=True)
        _CACHE = {}
    return _CACHE


def entries_for(skill):
    """这个渠道该默认挂哪些 LoRA。空列表 = 不挂。"""
    cfg = _load()
    groups = cfg.get("groups") or {}
    rows = cfg.get("defaults") or []
    skill = str(skill or "").strip()
    if not skill:
        return []
    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        g = str(r.get("group") or "")
        members = groups.get(g) or []
        if skill in members:
            f = str(r.get("file") or "").strip()
            if f:
                out.append({"file": f,
                            "strength": r.get("strength", 0.8),
                            "name": r.get("name") or g})
    return out


def _insert(wf, rows):
    """在模型加载节点后串一条 LoRA 链。返回新 dict，不动模板。"""
    out = copy.deepcopy(wf)
    src = None
    for nid, nd in out.items():
        if str((nd or {}).get("class_type") or "") in _LOADERS:
            src = nid
            break
    if src is None:
        log.warning("工作流里没有模型加载节点，默认 LoRA 跳过")
        return wf
    consumers = [nid for nid, nd in out.items()
                 if isinstance((nd or {}).get("inputs"), dict)
                 and isinstance(nd["inputs"].get("model"), list)
                 and nd["inputs"]["model"][:1] == [src]]
    if not consumers:
        log.warning("模型加载节点没有 model 消费者，默认 LoRA 跳过")
        return wf
    cur = [src, 0]
    for i, r in enumerate(rows, 1):
        try:
            strength = max(0.0, min(2.0, float(r.get("strength", 0.8))))
        except (TypeError, ValueError):
            strength = 0.8
        nid = "dflora_%d" % i
        while nid in out:
            nid = nid + "_"
        out[nid] = {
            "class_type": "LoraLoaderModelOnly",
            "inputs": {
                "model": list(cur),
                # 子目录要用本机分隔符：ComfyUI 的下拉在 Windows 上是反斜杠，
                # 写成 `/` 会被 prompt 校验判 value_not_in_list（整单 400）。
                "lora_name": str(r["file"]).replace("/", os.sep),
                "strength_model": strength,
            },
            "_meta": {"title": "默认 %s" % (r.get("name") or "")},
        }
        cur = [nid, 0]
    for nid in consumers:
        out[nid]["inputs"]["model"] = list(cur)
    return out


def apply(skill, workflow):
    """给这个渠道的工作流挂上默认 LoRA。没有配置 / 出错都原样返回。"""
    rows = entries_for(skill)
    if not rows:
        return workflow
    try:
        return _insert(workflow, rows)
    except Exception:                                    # noqa: BLE001
        log.exception("默认 LoRA 装填失败 %s，这一图不挂", skill)
        return workflow
