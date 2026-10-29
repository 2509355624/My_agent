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
SLOT_MAX = 14
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


# ─── nai5 触发词前缀（2026-10-28 晚）────────────────────────
#
# 群友的 moss-nai5（二次元，NAI5 训练集）挂在 8 号槽。作者要求：触发词
# `@//neoanime/` + 三句辅助词要跟 LoRA 同进同退——开着 LoRA 没触发词风格
# 出不来，关着 LoRA 留触发词是一段「空喊」的噪音 token。所以前缀**不烘焙
# 进模板固定前缀**，而是出图时按槽位实际状态动态拼：
#
# - QQ 侧 / 漫画台：没有槽位概念 → 照模板 `on_8` 决定（当前模板默认开）；
# - 跑图台：面板显式传了槽位 → 按传入状态，没传 8 号槽回退模板；
# - 跑图台显式关掉时，`build_t2i_workflow` 里按模板状态拼进去的前缀由
#   `strip_nai5_prefix` 抠掉（build 在 _run 里 apply_slots 之前发生）。

NAI5_SLOT = 8
NAI5_SKILL = DEFAULT_SKILL          # 目前只有 moss-qunyou 挂了这个槽
NAI5_PREFIX = ("@//neoanime/ detailed eyes with glossy highlights, "
               "realistic PBR skin texture, realistic material rendering, ")


def _nai5_on_from_template(skill):
    """读模板里 8 号槽的开关。读不到文件/节点一律按 False（宁可没风格，别空喊）。"""
    try:
        wf = load_workflow(workflow_path(skill))
    except OSError:
        return False
    _nid, nd = toggle_node(wf)
    if nd is None:
        return False
    return bool((nd.get("inputs") or {}).get("on_%d" % NAI5_SLOT))


def nai5_prefix(skill, loras=None):
    """nai5 槽启用 → 返回触发词前缀串；否则空串。

    `loras` 是跑图台面板显式传来的槽位列表；None（QQ / 漫画台）照模板。
    列表里没有 8 号槽的条目也回退模板——前端只在用户动过槽位时才全量传。
    """
    if skill != NAI5_SKILL:
        return ""
    on = None
    for s in loras or []:
        if isinstance(s, dict) and s.get("slot") == NAI5_SLOT:
            on = bool(s.get("on"))
            break
    if on is None:
        on = _nai5_on_from_template(skill)
    return NAI5_PREFIX if on else ""


def strip_nai5_prefix(wf):
    """把 `NAI5_PREFIX` 从工作流的提示词字段里抠掉（就地改，返回改了几处）。

    跑图台显式关 8 号槽时用：`build_t2i_workflow` 不知道槽位状态，是按
    模板默认拼的前缀，`_run` 里 apply_slots 之后发现关了就得剥掉。
    """
    n = 0
    for nd in (wf or {}).values():
        for k, v in (nd.get("inputs") or {}).items():
            if isinstance(v, str) and NAI5_PREFIX in v:
                nd["inputs"][k] = v.replace(NAI5_PREFIX, "")
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
    # 占位符检查对**原文**做，放在「没变化」早退之前——早退路径也绝不能
    # 给一个占位符残缺的模板放行（2026-10-28：模板被用户存成全关后，
    # 再存一次全关会踩中这条路径，测试 test_missing_placeholder_is_refused 抓的）。
    if "__SEED__" not in text or "__MULTI_PROMPTS__" not in text:
        return False, "模板占位符已缺失（__SEED__ / __MULTI_PROMPTS__），拒绝改写"

    new, hits = _patch(text, slots)
    if new == text or not hits:
        return True, "配置没变化"

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


# ─── 单挂一个 LoRA（2026-10-29 用户拍板）────────────────────
#
# 用户口径：「这些 lora 都可以单独的作为 moss 的渠道，默认 0.8」「moss 缩写
# 就可以单个 lora 的 moss 了，ai 自己去识别，然后调用」。
#
# 没有建 12 个渠道目录：渠道已经 44 条、转译模板预算也卡死，再拆 12 条等于
# 把列表冲垮；而且「单挂」只是组合里的一种（以后还要两个一起挂）。所以做成
# **一条 moss + 名字参数**：说「moss 米山舞」就全关、只开命中那个槽、0.8。
#
# 触发词只带它自己的（用户同轮拍板）：单挂 maliker 就只有 `maliker, `，
# 不再拖上 @realskin/@z1999 那一串——否则测出来的是默认组合的效果，不是
# 这一个 LoRA 的。

SOLO_STRENGTH = 0.8

# 只对裸 moss 生效；moss-wide / moss-light* 是固定配方渠道，不参与单挂
SOLO_SKILLS = ("moss-qunyou",)

# 名字 → 触发词。按**文件名包含**匹配，不按槽号：槽位以后挪了也不会错位。
_TRIGGERS = (
    ("maliker", "maliker, "),
    ("ningen", "ningen_mame, "),
    ("748cm", "748cm, "),
    ("1999", "@z1999, "),
    ("nai5", NAI5_PREFIX),
    ("光润", "@realskin, "),
)


def _norm_name(s):
    """匹配用归一化：小写、只留字母数字汉字（吃掉 .safetensors / 空格 / 连字符）。"""
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]", "", str(s or "").lower())


def find_slots(wf, word):
    """按名字找槽。`米山舞` → `moss-米山舞.safetensors`，`748` → `moss-748cm`。

    短于 2 个字符一律不认——单字会乱撞（`风` 能撞上 `光影风格`）。
    """
    w = _norm_name(word)
    if len(w) < 2:
        return []
    return [s for s in read_slots(wf) if w in _norm_name(s["name"])]


def trigger_for(name):
    """这个 LoRA 自己的触发词（没有就空串）。"""
    n = _norm_name(name)
    for key, trig in _TRIGGERS:
        if _norm_name(key) in n:
            return trig
    return ""


def apply_solo(wf, word, strength=SOLO_STRENGTH):
    """全关 + 只开命中的槽（默认 0.8）。返回命中的槽位条目；没命中返回 []。

    `word` 可以是单个词，也可以是词列表（`["米山舞", "748"]`＝两个一起挂）。
    """
    words = [word] if isinstance(word, str) else list(word or [])
    hits = []
    for w in words:
        for h in find_slots(wf, w):
            if h not in hits:
                hits.append(h)
    if not hits:
        return []
    keep = {h["slot"] for h in hits}
    plan = []
    for s in read_slots(wf):
        if s["slot"] in keep:
            plan.append({"slot": s["slot"], "on": True, "strength": strength})
        else:
            plan.append({"slot": s["slot"], "on": False})
    apply_slots(wf, plan)
    return sorted(hits, key=lambda x: x["slot"])


def names_hint(wf):
    """槽里现有 LoRA 的名字（报错时给模型看，别让它瞎猜）。"""
    return [s["name"] for s in read_slots(wf)]


def solo_prompt_prefix(hits):
    """单挂这几个槽时的前缀：各带各的触发词，按槽号顺序拼。"""
    return "".join(trigger_for(h["name"])
                   for h in sorted(hits, key=lambda x: x["slot"]))


def take_leading(prompt, wf, max_hits=2):
    """把 prompt **开头**命中的 LoRA 词扣走 → (命中词, 余下 prompt)。

    只吃开头连续命中的那几个：`米山舞 一只猫` → `(['米山舞'], '一只猫')`；
    `米山舞 748 一只猫` 两个都吃。句子中间提到的不碰——「画风像米山舞」
    不该悄悄把整个组合换掉。
    """
    text = (prompt or "").strip()
    hits = []
    while len(hits) < max_hits:
        m = re.match(r"([^\s,，、+]+)[\s,，、+]*", text)
        if not m:
            break
        w = m.group(1)
        if not find_slots(wf, w):
            break
        hits.append(w)
        text = text[m.end():]
    return hits, text.lstrip()


def template_prefix(skill=DEFAULT_SKILL):
    """模板里 `__MULTI_PROMPTS__` 前面那段固定前缀（nai5 前缀不在此列）。"""
    wf = load_workflow(workflow_path(skill))
    if not wf:
        return ""
    for nd in wf.values():
        if (nd or {}).get("class_type") != "CLIPTextEncode":
            continue
        t = (nd.get("inputs") or {}).get("text") or ""
        if "__MULTI_PROMPTS__" in t:
            return t.split("__MULTI_PROMPTS__")[0]
    return ""


def set_prompt_prefix(wf, prefix, skill=DEFAULT_SKILL):
    """把提示词头部的「模板固定前缀 + nai5 前缀」换成 prefix。返回改了几处。

    顺序有讲究：先抠掉已经拼进去的 nai5 前缀，再换模板前缀——单挂的正好是
    nai5 时两步会作用在同一段上，反过来做会把刚加的前缀又删掉。
    """
    old = template_prefix(skill)
    n = 0
    for nd in (wf or {}).values():
        inputs = nd.get("inputs") or {}
        for k, v in list(inputs.items()):
            if not isinstance(v, str):
                continue
            s = v.replace(NAI5_PREFIX, "")
            if old and s.startswith(old):
                s = prefix + s[len(old):]
            if s != v:
                inputs[k] = s
                n += 1
    return n
