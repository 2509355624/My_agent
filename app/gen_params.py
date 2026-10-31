"""跑图台「自定义 CFG / 负向提示词 / 尺寸」：只改内存里的工作流 dict，不碰模板文件。

## 为什么和 `gen_steps` 分开

`gen_steps` 的契约是「只认键名恰好是 `steps` 的整数字段」，一个文件一个可调
参数，改错互不影响。CFG、负向、尺寸的**找法各不相同**（CFG 是采样器上的浮点
数；负向要顺着 `KSampler.negative` 的连线反查 `CLIPTextEncode`；尺寸看画布节点
上的 width/height），混在一起会让两边都不好读。共同的约束只有一条：**只改内存
dict，绝不 json.dump 回写**——模板里有 `__SEED__` / `__MULTI_PROMPTS__` 这种
非 JSON 字面量，dump 一次模板就废了（和 `gen_steps` / `lora_slots` 同一条理由）。

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
# 尺寸：和漫画台（`comic_render_opts.SIZE_RANGE`）取同一套边界与对齐规则，
# 免得「同一个数字在跑图台能填、在漫画台被钳」这种两套口径。
SIZE_RANGE = (256, 4096)
SIZE_ALIGN = 8


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


# ─── 尺寸（底图宽高）────────────────────────────────────

def size_hosts(wf):
    """写宽高的节点：`class_type` 以 `Empty` 开头且带 width/height 的画布节点
    （`EmptyLatentImage` / `EmptyFlux2LatentImage` / `EmptySD3LatentImage`）。

    和 `comic_render_opts._latent_ids` 同一套认法，没有抄它的 `_size_hosts`：
    那边还要顺带认 sd/miao 的 batch 复合节点，跑图台目前只对 moss 开放尺寸
    （moss 两个渠道都是独立 latent、没有放大链），多带一份依赖没意义。
    """
    out = []
    if not isinstance(wf, dict):
        return out
    for nid, node in wf.items():
        if not isinstance(node, dict):
            continue
        if not str(node.get("class_type") or "").startswith("Empty"):
            continue
        ins = node.get("inputs")
        if isinstance(ins, dict) and "width" in ins and "height" in ins:
            out.append(nid)
    return out


def read_size(wf):
    """模板当前底图尺寸 `(w, h)`；认不出返回 `(None, None)`。"""
    for nid in size_hosts(wf):
        ins = wf[nid]["inputs"]
        w, h = ins.get("width"), ins.get("height")
        if isinstance(w, int) and isinstance(h, int):
            return w, h
    return None, None


def describe_size(wf):
    """给界面看的默认尺寸："1024×1536"。认不出返回空串。"""
    w, h = read_size(wf)
    return "%d×%d" % (w, h) if w and h else ""


def _blank_size(v):
    """宽高的「没填」判定：None 或空字符串（前端留空就是 ""）。"""
    return v is None or (isinstance(v, str) and not v.strip())


def _int_size(v):
    """宽高的单个值 → int；非法返回 None。`True` 不算数字。"""
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(v)
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return None
        try:
            return int(round(float(s)))
        except ValueError:
            return None
    return None


def normalize_size(width, height):
    """规整宽高，返回 `((w, h) | None, 错误, 提示)`。

    - 两个都没填 → `(None, None, "")`，表示「不覆盖，按模板」；
    - 只填了一个 → 报错（半套尺寸没有意义，也免得用户以为另一个沿用模板）；
    - 越界钳进 256~4096，不是 8 的倍数**向下对齐**（ComfyUI 的 latent 本来
      就是 //8 截断，按真正会跑的尺寸记下来比拒绝有用）。
    """
    if _blank_size(width) and _blank_size(height):
        return None, None, ""
    w, h = _int_size(width), _int_size(height)
    if w is None or h is None:
        return None, "宽高要一起填整数（收到 %r × %r）" % (width, height), ""
    if w <= 0 or h <= 0:
        return None, "宽高要大于 0（收到 %d×%d）" % (w, h), ""
    lo, hi = SIZE_RANGE
    cw, ch = min(max(w, lo), hi), min(max(h, lo), hi)
    aw, ah = cw // SIZE_ALIGN * SIZE_ALIGN, ch // SIZE_ALIGN * SIZE_ALIGN
    notes = []
    if (cw, ch) != (w, h):
        notes.append("%d×%d 超出 %d~%d，已钳成 %d×%d"
                     % (w, h, lo, hi, cw, ch))
    if (aw, ah) != (cw, ch):
        notes.append("%d×%d 不是 %d 的倍数，按 %d×%d 渲染"
                     % (cw, ch, SIZE_ALIGN, aw, ah))
    return (aw, ah), None, "；".join(notes)


def apply_size(wf, width, height):
    """把底图尺寸改成 width×height，返回改了节点数。

    顺带把 `enable_hires` 渠道的第二段（`hires_width` / `hires_height`）改成
    同一尺寸——跑图台要的是「就这么大」，第二段留旧值等于出图尺寸被悄悄改掉。
    """
    size, err, _note = normalize_size(width, height)
    if err or size is None:
        return 0
    w, h = size
    n = 0
    for nid in size_hosts(wf):
        ins = wf[nid]["inputs"]
        if (ins.get("width"), ins.get("height")) != (w, h):
            ins["width"], ins["height"] = w, h
            n += 1
        if ins.get("enable_hires") and "hires_width" in ins:
            ins["hires_width"], ins["hires_height"] = w, h
    return n
