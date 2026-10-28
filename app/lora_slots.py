"""`MOSSLoRAToggle` 这类「一个节点内并联多槽」LoRA 节点的读写层（2026-10-28）。

## 为什么不复用 `comic_render_opts._rebuild_loras`

两种 LoRA 载体长得不一样：

- `LoraLoader` / `LoraLoaderModelOnly` 是**链**：model 进 model 出，挂几个就要
  建几个节点、逐根接引用。改它得重建链（`_rebuild_loras` 干的就是这件事）。
- `MOSSLoRAToggle` 是**一个节点里的 N 个槽**，每槽三个字段
  `on_i` / `lora_i` / `strength_i`。改它只改字段，不建节点、不动引用。

把 Toggle 硬塞进链式逻辑会走成「删光旧节点再建节点」，而这个节点还连着底模和
采样器，删一次整条链就断。所以这里独立成一层：读槽 → 校验 → 写字段 → 落盘。

## 「存为默认」为什么用正则改文本，不是 json.load → dump

`skills/moss-qunyou/workflow.json` 是**模板**：`"seed": __SEED__` 和
`__MULTI_PROMPTS__` 都不是合法 JSON，`json.load` 直接抛。先替换占位符再 dump，
模板就被写成死值，下次生图种子固定、提示词槽消失。所以按字段名做定点替换，
其余字节一个不动——键序、缩进、换行符、占位符全部保持原样。
"""

import io
import json
import logging
import os
import re
import shutil

from app.config import SKILLS_DIR
from app.skills import load_workflow

log = logging.getLogger("lora_slots")

TOGGLE_CLASS = "MOSSLoRAToggle"
DEFAULT_SKILL = "moss-qunyou"
SLOT_MAX = 10
STRENGTH_RANGE = (0.0, 2.0)

# 模板里空槽写成 `"None"`（字符串），别当成真文件名
_BLANK = {"", "none", "null", "无"}


def _f(v, default=1.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _clamp(v, lo, hi):
    return lo if v < lo else (hi if v > hi else v)


def toggle_node(wf):
    """→ (节点 id, 节点 dict)；没有就 (None, None)。"""
    for nid, nd in (wf or {}).items():
        if (nd or {}).get("class_type") == TOGGLE_CLASS:
            return nid, nd
    return None, None


def has_toggle(wf):
    return toggle_node(wf)[0] is not None


def read_slots(wf):
    """当前槽位 → [{slot, name, strength, on}]。

    只返回**有名字**的槽：模板里 `lora_8/9/10` 是 `"None"`，那三个不该出现在
    界面上（露出来只会让人去填一个空槽，然后疑惑为什么没反应）。
    """
    _nid, nd = toggle_node(wf)
    if nd is None:
        return []
    inp = nd.get("inputs") or {}
    out = []
    for i in range(1, SLOT_MAX + 1):
        name = str(inp.get("lora_%d" % i) or "").strip()
        if name.lower() in _BLANK:
            continue
        out.append({"slot": i, "name": name,
                    "strength": _f(inp.get("strength_%d" % i), 1.0),
                    "on": bool(inp.get("on_%d" % i))})
    return out


def normalize(raw, resources=None):
    """前端传来的槽位 → (slots, errors, warnings)。

    `resources` 给了（ComfyUI 在线时的 LoRA 清单）才校验文件名——没有证据源
    就不猜，这条和 `comic_render_opts.validate` 的规矩一致。

    `name` 为空表示「这个槽不改名字」，只有 `on` / `strength` 会生效；这样前端
    只勾开关、不动下拉也能提交。
    """
    if raw is None:
        return [], [], []
    if not isinstance(raw, list):
        return [], ["LoRA 槽必须是列表"], []
    pool = (resources or {}).get("loras") or []
    errors, warnings, out = [], [], []
    seen = set()
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            errors.append("第 %d 个 LoRA 槽格式不对" % (i + 1))
            continue
        try:
            slot = int(item.get("slot") or (i + 1))
        except (TypeError, ValueError):
            errors.append("第 %d 个槽的序号不是数字" % (i + 1))
            continue
        if not 1 <= slot <= SLOT_MAX:
            errors.append("槽号 %d 超出 1~%d" % (slot, SLOT_MAX))
            continue
        if slot in seen:
            errors.append("槽 %d 重复提交" % slot)
            continue
        seen.add(slot)
        name = str(item.get("name") or "").strip()
        if name.lower() in _BLANK:
            name = ""
        if name and pool and name not in pool:
            errors.append("ComfyUI 里没有 LoRA「%s」" % name)
            continue
        raw_s = _f(item.get("strength"), 1.0)
        s = _clamp(raw_s, *STRENGTH_RANGE)
        if abs(s - raw_s) > 1e-9:
            warnings.append("槽 %d 强度已钳到 0~2 的 %.2f" % (slot, s))
        row = {"slot": slot, "strength": round(s, 4)}
        if name:
            row["name"] = name
        if "on" in item:
            row["on"] = bool(item.get("on"))
        out.append(row)
    return out, errors, warnings


def apply_slots(wf, slots):
    """把槽位写进工作流（**就地改**，返回处理了几个槽）。

    只碰 `MOSSLoRAToggle` 的 `on_/lora_/strength_` 字段，不碰节点连线。槽号越界
    或没有槽的条目直接跳过——宁可少改一个，也不改到别的槽上去。
    """
    _nid, nd = toggle_node(wf)
    if nd is None:
        return 0
    inp = nd.setdefault("inputs", {})
    n = 0
    for s in slots or []:
        if not isinstance(s, dict):
            continue
        try:
            slot = int(s.get("slot"))
        except (TypeError, ValueError):
            continue
        if not 1 <= slot <= SLOT_MAX:
            continue
        if s.get("on") is not None:
            inp["on_%d" % slot] = bool(s["on"])
        name = str(s.get("name") or "").strip()
        if name and name.lower() not in _BLANK:
            inp["lora_%d" % slot] = name
        if s.get("strength") is not None:
            inp["strength_%d" % slot] = round(
                _clamp(_f(s["strength"], 1.0), *STRENGTH_RANGE), 4)
        n += 1
    return n


# ─── 存为默认（写回模板）───────────────────────────────────

def workflow_path(skill=DEFAULT_SKILL):
    return os.path.join(SKILLS_DIR, str(skill or DEFAULT_SKILL), "workflow.json")


_FIELD_PATTERNS = {
    "on": r'"on_%d":\s*(?:true|false)',
    "lora": r'"lora_%d":\s*"[^"]*"',
    "strength": r'"strength_%d":\s*-?[0-9][0-9.eE+-]*',
}


def _patch(text, slots):
    """逐字段定点替换。返回 (新文本, 改了几个字段)。"""
    new, hits = text, 0
    for s in slots or []:
        if not isinstance(s, dict):
            continue
        try:
            slot = int(s.get("slot"))
        except (TypeError, ValueError):
            continue
        if not 1 <= slot <= SLOT_MAX:
            continue
        if s.get("on") is not None:
            pat = _FIELD_PATTERNS["on"] % slot
            rep = '"on_%d": %s' % (slot, "true" if s["on"] else "false")
            new, k = re.subn(pat, lambda m, r=rep: r, new)
            hits += k
        name = str(s.get("name") or "").strip()
        if name and name.lower() not in _BLANK:
            pat = _FIELD_PATTERNS["lora"] % slot
            rep = '"lora_%d": "%s"' % (slot, name.replace('"', ""))
            new, k = re.subn(pat, lambda m, r=rep: r, new)
            hits += k
        if s.get("strength") is not None:
            pat = _FIELD_PATTERNS["strength"] % slot
            val = json.dumps(round(_clamp(_f(s["strength"], 1.0),
                                          *STRENGTH_RANGE), 4))
            new, k = re.subn(pat, lambda m, v=val, s2=slot:
                             '"strength_%d": %s' % (s2, v), new)
            hits += k
    return new, hits


def save_default(skill, slots):
    """把槽位写回模板文件。返回 (ok, msg)。

    写之前 `workflow.json.bak-lora` 备份，写之后必须过三道自检：
    ① `load_workflow` 还能解析；② 两个占位符都还在；③ 读回来的槽值等于提交值。
    任何一道不过就回滚——模板坏了等于这个渠道整个废掉，比不保存严重得多。
    """
    path = workflow_path(skill)
    if not os.path.isfile(path):
        return False, "找不到模板：%s" % path
    try:
        with io.open(path, encoding="utf-8", newline="") as f:
            text = f.read()
    except OSError as e:
        return False, "读不到模板：%s" % e
    if '"%s"' % TOGGLE_CLASS not in text:
        return False, "模板里没有 %s 节点" % TOGGLE_CLASS

    new, hits = _patch(text, slots)
    if new == text or not hits:
        return True, "配置没变化"
    if "__SEED__" not in new or "__MULTI_PROMPTS__" not in new:
        return False, "占位符会被改坏，已放弃保存"

    bak = path + ".bak-lora"
    try:
        shutil.copyfile(path, bak)
    except OSError as e:
        return False, "备份失败：%s" % e

    def _rollback(why):
        try:
            shutil.copyfile(bak, path)
        except OSError:
            log.exception("回滚 %s 失败", path)
        return False, why + "（已回滚）"

    try:
        with io.open(path, "w", encoding="utf-8", newline="") as f:
            f.write(new)
    except OSError as e:
        return _rollback("写不进去：%s" % e)

    wf = load_workflow(path)
    if not wf:
        return _rollback("写回去之后模板解析不了")
    got = {r["slot"]: r for r in read_slots(wf)}
    for s in slots or []:
        if not isinstance(s, dict) or s.get("slot") is None:
            continue
        slot = int(s["slot"])
        cur = got.get(slot)
        if cur is None:
            return _rollback("保存后读不到槽 %d" % slot)
        if s.get("on") is not None and bool(cur.get("on")) != bool(s["on"]):
            return _rollback("槽 %d 的开关没落盘" % slot)
        if s.get("strength") is not None \
                and abs(_f(cur.get("strength")) - _f(s["strength"])) > 1e-6:
            return _rollback("槽 %d 的强度没落盘" % slot)
    log.info("LoRA 槽位存为默认：%s（%d 处字段，备份 %s）",
             skill, hits, os.path.basename(bak))
    return True, "已存为默认（备份 %s）" % os.path.basename(bak)
