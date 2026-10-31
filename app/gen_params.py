"""跑图台「自定义 CFG / 负向提示词」：只改内存里的工作流 dict，不碰模板文件。

## 为什么和 `gen_steps` 分开

`gen_steps` 的契约是「只认键名恰好是 `steps` 的整数字段」，一个文件一个可调
参数，改错互不影响。CFG 和负向的**找法完全不同**（CFG 是采样器上的浮点数，
负向要顺着 `KSampler.negative` 的连线反查 `CLIPTextEncode`），混在一起会让
两边都不好读。共同的约束只有一条：**只改内存 dict，绝不 json.dump 回写**
——模板里有 `__SEED__` / `__MULTI_PROMPTS__` 这种非 JSON 字面量，dump 一次
模板就废了（和 `gen_steps` / `lora_slots` 同一条理由）。

## 负向为什么要顺着连线找

各渠道节点编号完全不通用：`moss-qunyou` 的负向是 `#51`，`hd_*` / `silver`
是两段采样**共用一个**负向节点，`cunny` / `nffa` 有三个 `CLIPTextEncode`
（其中有的没接采样器），`qwen-charsheet` 压根没有 CLIP 节点。写死编号必然
错，所以统一从采样器的 `negative` 输入反查真实节点；查不到就等于「该渠道
没有负向」，界面据此把输入框藏起来。

## 追加，不是替换

`moss-qunyou` 的模板负向是 `<|pose_and_clothing_neg|>` 四段结构化负向池，
替换等于把「防脱 / 防画风污染」整段丢掉（skill.md 里记着 cfg>1 时负向池还
会参与构图诱导）。所以用户填的内容一律**追加在模板负向后面**，不覆盖。
"""

CFG_RANGE = (0.0, 20.0)
NEG_MAX = 2000
NEG_SEP = ", "


def _is_sampler(node):
    """是不是采样节点。

    三档判定，由严到松：
    1. class_type 里带 `Sampler`（`KSampler` / `KSamplerAdvanced` /
       `SamplerCustom` 都算）；
    2. 同时接了 `positive` / `negative` 两条线（自定义采样节点的通用形状）；
    3. 输入里有**数字型** `cfg`——除采样器外几乎没有节点带这个字段，而
       `cfg` 是连线的那些（`["7", 0]`）会被第 3 档自己挡掉。
    """
    if not isinstance(node, dict):
        return False
    ins = node.get("inputs")
    if not isinstance(ins, dict):
        return False
    ct = node.get("class_type") or ""
    if "Sampler" in ct:
        return True
    if "positive" in ins and "negative" in ins:
        return True
    v = ins.get("cfg")
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _iter_sampler_inputs(wf):
    """产出每个采样节点的 inputs dict。"""
    if not isinstance(wf, dict):
        return
    for node in wf.values():
        if _is_sampler(node) and isinstance(node.get("inputs"), dict):
            yield node["inputs"]


def _linked_node_ids(wf, slot):
    """采样器 `positive` / `negative` 输入连到的节点 id（去重、保持顺序）。"""
    out = []
    for ins in _iter_sampler_inputs(wf):
        link = ins.get(slot)
        if not (isinstance(link, list) and link):
            continue
        nid = str(link[0])
        if nid not in out:
            out.append(nid)
    return out


# ─── CFG ────────────────────────────────────────────────

def read_cfg(wf):
    """模板里当前所有采样器的 cfg（按节点顺序）。非数字的跳过。"""
    vals = []
    for ins in _iter_sampler_inputs(wf):
        v = ins.get("cfg")
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            continue
        vals.append(float(v))
    return vals


def describe_cfg(wf):
    """给界面看的默认 cfg：全都一样就 "3"，多段不同就 "1+3"。"""
    vals = read_cfg(wf)
    if not vals:
        return ""
    uniq = sorted(set(vals))
    if len(uniq) == 1:
        return "%g" % uniq[0]
    return "+".join("%g" % v for v in vals)


def normalize_cfg(value):
    """把前端传来的 cfg 规整成 float。

    返回 `(cfg, 错误)`：空值 / None → `(None, None)` 表示「不覆盖，按模板」；
    非法或越界 → `(None, 错误话术)`，调用方据此 400。
    注意 `False` 是 int 的子类，得单独挡掉。
    """
    if value is None:
        return None, None
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None, None
        try:
            value = float(s)
        except ValueError:
            return None, "CFG 要填数字，收到 %r" % s
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None, "CFG 要填数字"
    lo, hi = CFG_RANGE
    if not lo <= value <= hi:
        return None, "CFG 超出 %g~%g（收到 %g）" % (lo, hi, value)
    return float(value), None


def apply_cfg(wf, cfg):
    """把所有采样器的 cfg 改成 `cfg`，返回改了几个节点。"""
    cfg, err = normalize_cfg(cfg)
    if err or cfg is None:
        return 0
    n = 0
    for ins in _iter_sampler_inputs(wf):
        if isinstance(ins.get("cfg"), bool) or not isinstance(
                ins.get("cfg"), (int, float)):
            continue              # 连线喂的 / 没这个字段 → 不是可调数字
        if ins["cfg"] != cfg:
            ins["cfg"] = cfg
            n += 1
    return n


# ─── 负向提示词 ──────────────────────────────────────────

def negative_nodes(wf):
    """真正接进采样器负向的文本节点：`[(节点 id, 节点 dict), ...]`。

    只认 inputs.text 是字符串的节点——`CLIPTextEncode` 之外还有
    `CLIPTextEncodeSDXL` 之类，按「有没有可写的 text」判定比按 class_type
    白名单稳。
    """
    out = []
    if not isinstance(wf, dict):
        return out
    for nid in _linked_node_ids(wf, "negative"):
        node = wf.get(nid)
        if not isinstance(node, dict):
            continue
        ins = node.get("inputs")
        if isinstance(ins, dict) and isinstance(ins.get("text"), str):
            out.append((nid, node))
    return out


def has_negative(wf):
    """这条渠道有没有可写的负向提示词（界面据此显示 / 隐藏输入框）。"""
    return bool(negative_nodes(wf))


def read_negative(wf):
    """模板负向的原文（第一条接进采样器的），界面拿它做 placeholder 摘要。"""
    nodes = negative_nodes(wf)
    return nodes[0][1]["inputs"]["text"] if nodes else ""


def normalize_negative(text):
    """规整用户填的负向。空 → `(None, None)` 表示「不追加」。"""
    if text is None:
        return None, None
    if not isinstance(text, str):
        return None, "负向提示词要填文本"
    s = text.strip()
    if not s:
        return None, None
    if len(s) > NEG_MAX:
        return None, "负向提示词太长了（%d 字，上限 %d）" % (len(s), NEG_MAX)
    return s, None


def apply_negative(wf, text):
    """把用户填的负向**追加**到模板负向后面，返回改了几个节点。

    模板原本是空的就直接用用户那段（不留前导逗号）。
    """
    text, err = normalize_negative(text)
    if err or text is None:
        return 0
    n = 0
    for _nid, node in negative_nodes(wf):
        ins = node["inputs"]
        old = (ins.get("text") or "").rstrip()
        ins["text"] = (old + NEG_SEP + text) if old else text
        n += 1
    return n
