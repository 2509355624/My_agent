"""漫画场景渲染参数：按任务覆盖 anima-line 工作流（2026-10-13 用户拍板）。

用户要的是「这一本漫画换个角色 LoRA / 步数调小试试 / 分辨率拉大」——所以参数
**按任务**存（任务目录下的 `render.json`），开跑时读一次，只改**这一次提交的
工作流副本**，skill 模板一个字节不动。模板是全局的（所有任务、AI 的
`update_workflow` 也在写它），不能拿来存单人单次的心血来潮。

anima-line 是**两段采样**（摸清结构才敢写覆盖，节点号以 2026-10-13 模板为准）：
  - 第一段 KSampler（节点 2）：latent 来自 EmptyLatentImage，denoise=1，
    下面挂 6 个 `LoraLoaderModelOnly`（16/15/28/29/30/31，其中
    `anima_context_detailer_base10` 出现两次、强度 0.35 和 0.8）。
    **LoRA 只在这一段生效。**
  - 第二段 KSampler（节点 27）：latent 来自 LatentUpscaleBy（×1.2），
    denoise=0.25，model 挂在**另一个底模**（silvermoonmix）上、**不接 LoRA**。
所以覆盖也按两段分开给：base / hires 各一套 steps / cfg / sampler_name /
scheduler，hires 再多一个 denoise（放大精修的强度）；LoRA 链只重建第一段那条。

⚠️ 重建 LoRA 链时**绝不能动第二段的 model 连线**：
`comfy_workflow._rebuild_lora` 按「dict 里第一个 KSampler」当 generator，而
anima-line 的 dict 顺序是 27 在 2 前面——直接用它会把第二段的 model 从
silvermoon 底模改指到 LoRA 链尾，两段采样整个变质（表现是「改了 LoRA 之后
画风/底模莫名其妙变了」）。这里自己按 **latent 来源**认段：EmptyLatentImage
→ base，LatentUpscale* → hires，不靠节点号也不靠 dict 顺序。

生效链路：`comic.run` 开跑时 `load()` 一次 → `render_scene` 建好工作流后
`apply()` 改副本 → 提交。已出的图不受影响（那些格不动就不会重画）。
"""

import copy
import json
import logging
import os

from app.skills import load_skill

log = logging.getLogger("comic")

# 任务目录里的参数文件名
RENDER_FILE = "render.json"

# 默认从哪个渠道的模板读当前值（漫画场景图渠道；调用方传 comic.SCENE_SKILL）
DEFAULT_SKILL = "anima-line"

# 两种 LoRA 加载器类名（与 comfy_workflow.LORA_CLASSES 保持一致；这里不 import
# 那个模块的常量，免得它哪天为 AI 工具改语义时把这条链路带歪）
LORA_CLASSES = ("LoraLoader", "LoraLoaderModelOnly")

# 采样参数合法范围（超出钳位 + 回一句警告，不拒——这是给人填的表，不是 AI 闸门）
STEPS_RANGE = (1, 150)
CFG_RANGE = (0.0, 20.0)
DENOISE_RANGE = (0.0, 1.0)
STRENGTH_RANGE = (0.0, 2.0)
SIZE_RANGE = (256, 4096)          # 宽高都限这个范围；且必须是 8 的倍数
MAX_SLOTS = 12                    # 槽位上限（界面给 6 个，留余量给粘贴/API）


# ─── 工作流结构识别 ─────────────────────────────────────

def _inputs(nd):
    inp = (nd or {}).get("inputs")
    return inp if isinstance(inp, dict) else {}


def _sampler_pass(wf, nid):
    """这个 KSampler 是哪一段：'base' / 'hires' / None（不认识的跳过）。

    按 latent_image 的来源认，不按节点号、不按 dict 顺序——见模块顶部的坑。
    """
    lat = _inputs(wf.get(nid)).get("latent_image")
    src = lat[0] if isinstance(lat, list) and lat else None
    ct = ((wf.get(src) or {}).get("class_type")) if src else None
    if ct == "EmptyLatentImage":
        return "base"
    if ct in ("LatentUpscaleBy", "LatentUpscale"):
        return "hires"
    return None


def _passes(wf):
    """{'base': [节点 id...], 'hires': [...]}"""
    out = {"base": [], "hires": []}
    for nid, nd in (wf or {}).items():
        if (nd or {}).get("class_type") != "KSampler":
            continue
        p = _sampler_pass(wf, nid)
        if p:
            out[p].append(nid)
    return out


def _latent_ids(wf):
    return [nid for nid, nd in (wf or {}).items()
            if (nd or {}).get("class_type") == "EmptyLatentImage"
            and "width" in _inputs(nd)]


def _lora_slots(wf, base_ids):
    """当前 LoRA 链，按**加载顺序**（靠近底模的在前）→ [{name, strength}]。

    回溯逻辑复用 comfy_workflow._lora_chain（它已被 test_comfy_workflow 钉住），
    但 generator 用**自己认的** base 段，不用它的 _find_generator——那个在
    anima-line 上会认成第二段（见模块顶部）。
    """
    if not base_ids:
        return []
    from app.tools.normal.comfy_workflow import _lora_chain
    chain = _lora_chain(wf, base_ids[0])
    return [{"name": str(l.get("name") or ""),
             "strength": _f(l.get("strength_model"), 1.0)}
            for l in chain]


def _sampler_params(wf, nid, with_denoise=False):
    if not nid:
        return {}
    inp = _inputs(wf.get(nid))
    out = {}
    for k in ("steps", "cfg", "sampler_name", "scheduler"):
        if k in inp:
            out[k] = inp[k]
    if with_denoise and "denoise" in inp:
        out["denoise"] = inp["denoise"]
    return out


# ─── 默认值（从 skill 模板读现状）─────────────────────────

def defaults(skill=DEFAULT_SKILL):
    """anima-line 模板的当前配置 = 参数卡的默认值（用户没存过就照它显示）。"""
    sd = load_skill(skill) or {}
    wf = sd.get("workflow") or {}
    passes = _passes(wf)
    lat_ids = _latent_ids(wf)
    lat = _inputs(wf.get(lat_ids[0])) if lat_ids else {}
    return {
        "loras": _lora_slots(wf, passes["base"]),
        "base": _sampler_params(wf, passes["base"][0] if passes["base"] else None),
        "hires": _sampler_params(wf, passes["hires"][0] if passes["hires"] else None,
                                 with_denoise=True),
        "width": lat.get("width", 1024),
        "height": lat.get("height", 1536),
    }


# ─── 存取 ───────────────────────────────────────────────

def path_of(task_dir):
    return os.path.join(task_dir, RENDER_FILE)


def load(task_dir, skill=DEFAULT_SKILL):
    """读任务目录的 render.json；没有 / 读坏了 → 模板默认值。

    读坏不炸：那是用户手改坏的文件，退回默认值照常出图，比整本漫画卡死强。
    """
    d = defaults(skill)
    fp = path_of(task_dir)
    if not os.path.isfile(fp):
        return d
    try:
        with open(fp, "r", encoding="utf-8") as f:
            saved = json.load(f)
    except (OSError, ValueError) as e:
        log.warning("render.json 读不出来（%s），按模板默认值渲染", e)
        return d
    if not isinstance(saved, dict):
        log.warning("render.json 不是对象，按模板默认值渲染")
        return d
    opts, errors, _w = normalize(saved, base=d)
    if errors:
        # 存的时候就校验过，这里还报错只可能是文件被手改坏——不阻塞出图，
        # 但要把话说在日志里，免得用户以为存进去了。
        log.warning("render.json 有越界值（%s），已钳位", "；".join(errors))
    return opts


def save(task_dir, opts):
    opts, errors, _w = normalize(opts)
    if errors:
        raise ValueError("；".join(errors))
    os.makedirs(task_dir, exist_ok=True)
    with open(path_of(task_dir), "w", encoding="utf-8") as f:
        json.dump(opts, f, ensure_ascii=False, indent=1)
    return opts


def reset(task_dir):
    """删掉 render.json → 回到模板默认值。"""
    fp = path_of(task_dir)
    if os.path.isfile(fp):
        os.remove(fp)
        return True
    return False


# ─── 校验 / 钳位 ────────────────────────────────────────

def _f(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _i(v, default=0):
    try:
        return int(round(float(v)))
    except (TypeError, ValueError):
        return default


def _clamp(v, lo, hi):
    return lo if v < lo else (hi if v > hi else v)


def normalize(raw, base=None, resources=None):
    """把前端/文件来的原始 dict 收敛成合法参数。

    返回 (opts, errors, warnings)。errors 是「这填得不对，存不进去」的事实
    问题（宽高不是 8 的倍数、LoRA 名 ComfyUI 里没有……）；warnings 是被钳位
    的字段（步数 999 → 150）。base 给了就按它补缺失的键（部分更新不丢字段）。
    resources 给了（ComfyUI 在线时的清单）才校验名字——没有证据源就不猜。
    """
    base = base or {}
    raw = raw if isinstance(raw, dict) else {}
    res = resources or {}
    lora_list = res.get("loras") or []
    samplers = res.get("samplers") or []
    schedulers = res.get("schedulers") or []
    errors, warnings = [], []

    # ── LoRA 槽（顺序 = 加载顺序；name 空 = 这个槽不挂 LoRA）
    raw_loras = raw.get("loras")
    if raw_loras is None:
        raw_loras = base.get("loras") or []
    if not isinstance(raw_loras, list):
        errors.append("loras 必须是列表")
        raw_loras = []
    loras = []
    for i, item in enumerate(raw_loras[:MAX_SLOTS]):
        if not isinstance(item, dict):
            errors.append("第 %d 个 LoRA 槽格式不对" % (i + 1))
            continue
        name = str(item.get("name") or "").strip()
        if name and lora_list and name not in lora_list:
            errors.append("ComfyUI 里没有 LoRA「%s」" % name)
            continue
        s = _f(item.get("strength"), 1.0)
        cs = _clamp(s, *STRENGTH_RANGE)
        if abs(cs - s) > 1e-9:
            warnings.append("槽 %d 强度 %.2f 超出 0~2，已钳成 %.2f" % (i + 1, s, cs))
        loras.append({"name": name, "strength": cs})

    # ── 两段采样参数
    def _pass(key, with_denoise):
        rp = raw.get(key)
        bp = base.get(key) or {}
        if rp is None:
            rp = bp
        rp = rp if isinstance(rp, dict) else {}
        out = dict(bp) if isinstance(bp, dict) else {}
        steps = _i(rp.get("steps", out.get("steps", 20)), 20)
        cs = int(_clamp(steps, *STEPS_RANGE))
        if cs != steps:
            warnings.append("%s 步数 %d 超出 %d~%d，已钳成 %d"
                            % (key, steps, STEPS_RANGE[0], STEPS_RANGE[1], cs))
        out["steps"] = cs
        cfg = _f(rp.get("cfg", out.get("cfg", 1.0)), 1.0)
        cc = _clamp(cfg, *CFG_RANGE)
        if abs(cc - cfg) > 1e-9:
            warnings.append("%s cfg %.2f 超出 0~20，已钳成 %.2f" % (key, cfg, cc))
        out["cfg"] = cc
        for k, lst, label in (("sampler_name", samplers, "采样器"),
                              ("scheduler", schedulers, "调度器")):
            v = str(rp.get(k, out.get(k, "")) or "").strip()
            if v and lst and v not in lst:
                errors.append("ComfyUI 没有这个%s：%s" % (label, v))
                continue
            out[k] = v
        if with_denoise and ("denoise" in rp or "denoise" in out):
            dn = _f(rp.get("denoise", out.get("denoise", 0.25)), 0.25)
            cd = _clamp(dn, *DENOISE_RANGE)
            if abs(cd - dn) > 1e-9:
                warnings.append("%s denoise %.2f 超出 0~1，已钳成 %.2f"
                                % (key, dn, cd))
            out["denoise"] = cd
        return out

    opts = {
        "loras": loras,
        "base": _pass("base", with_denoise=False),
        "hires": _pass("hires", with_denoise=True),
    }

    # ── 分辨率（EmptyLatentImage 的宽高，8 的倍数）
    w = _i(raw.get("width", base.get("width", 1024)), 1024)
    h = _i(raw.get("height", base.get("height", 1536)), 1536)
    for label, v in (("宽", w), ("高", h)):
        if v % 8:
            errors.append("%s %d 不是 8 的倍数（ComfyUI 的要求）" % (label, v))
    cw = int(_clamp(w, *SIZE_RANGE))
    ch = int(_clamp(h, *SIZE_RANGE))
    if cw != w or ch != h:
        warnings.append("分辨率 %d×%d 超出 %d~%d，已钳成 %d×%d"
                        % (w, h, SIZE_RANGE[0], SIZE_RANGE[1], cw, ch))
    opts["width"], opts["height"] = cw, ch
    return opts, errors, warnings


# ─── 应用到工作流 ───────────────────────────────────────

def _rebuild_loras(wf, slots):
    """按 slots（加载顺序）重建 LoRA 链；name 为空的槽**摘掉**，链自动重连。

    只碰第一段：旧 LoRA 节点的引用统一改指新链尾（保留原 slot 号），第二段
    采样器的 model 指向自己的底模、不在旧 LoRA 节点上，天然不会被扫到。
    找不到链头（底模加载器都认不出）时什么都不做——硬写 [None, 0] 比不改糟。
    返回实际挂上的槽数。
    """
    from app.tools.normal.comfy_workflow import _chain_head
    passes = _passes(wf)
    base_ids = passes["base"]
    gen_id = base_ids[0] if base_ids else None

    old_ids = {nid for nid, nd in wf.items()
               if (nd or {}).get("class_type") in LORA_CLASSES}
    slots = [s for s in (slots or []) if str((s or {}).get("name") or "").strip()]
    if not old_ids and not slots:
        return 0                    # 本来就没链、也没要挂——一个字都不动

    # 节点类跟着现有节点走（删之前看）：ModelOnly 工作流不能换成 LoraLoader
    # （会向 UNETLoader 要 clip 输出，它没这个槽，提交必被拒）。没有现存 LoRA
    # 节点时按底模加载器的形态推：anima 系全是 UNETLoader + CLIPLoader 分开的
    # 组合，用 ModelOnly 才对。
    if old_ids:
        kind = next(nd["class_type"] for nd in wf.values()
                    if (nd or {}).get("class_type") in LORA_CLASSES)
    else:
        kind = "LoraLoaderModelOnly" if any(
            "unetloader" in str((nd or {}).get("class_type") or "").lower()
            for nd in wf.values()) else "LoraLoader"

    # 链头：有旧链就沿旧链回溯（_chain_head 干的）；没有旧链但要挂新 LoRA 时，
    # 用第一段采样器**当前**的 model 来源——那才是它正在用的底模，别按 dict
    # 顺序猜一个加载器接上去。
    head = _chain_head(wf) if old_ids else None
    if head is None and gen_id:
        cur = _inputs(wf.get(gen_id)).get("model")
        head = str(cur[0]) if isinstance(cur, list) and cur else None
    if head is None:
        log.warning("渲染参数：工作流里找不到底模加载器，LoRA 覆盖跳过")
        return 0

    for nid in old_ids:
        wf.pop(nid, None)

    used = sorted(int(x) for x in wf if str(x).lstrip("-").isdigit())
    nxt = (used[-1] + 1) if used else 1
    target = head
    n = 0
    for s in slots:
        nid = str(nxt)
        nxt += 1
        wf[nid] = {"class_type": kind,
                   "inputs": {"model": [target, 0],
                              "lora_name": str(s.get("name") or "").strip(),
                              "strength_model": _f(s.get("strength"), 1.0)}}
        target = nid
        n += 1

    # 指着已删 LoRA 节点的引用（第一段采样器就在里面）改指新链尾；全摘掉时
    # target 还是 head，等于直接挂回底模——正是想要的。
    if old_ids:
        for nd in wf.values():
            inp = _inputs(nd)
            for k, v in list(inp.items()):
                if isinstance(v, list) and v and str(v[0]) in old_ids:
                    inp[k] = [target, v[1] if len(v) > 1 else 0]
    if gen_id:
        _inputs(wf.get(gen_id))["model"] = [target, 0]
    return n


def _set_sampler(wf, nid, params):
    """把参数写进采样节点。只改**节点上已有**的键——往 KSampler 塞不认识的
    字段 ComfyUI 会直接拒收。"""
    inp = _inputs(wf.get(nid))
    for k in ("steps", "cfg", "sampler_name", "scheduler", "denoise"):
        if k in params and k in inp:
            inp[k] = params[k]


def apply(workflow, opts):
    """把渲染参数应用到工作流，返回**新 dict**（不改入参）。

    调用方传进来的是 build_t2i_workflow 刚建的新鲜副本，照样深拷贝一份：
    apply 是纯函数，测试和「同一模板跑两遍不同参数」都省心。
    """
    wf = copy.deepcopy(workflow or {})
    if not wf:
        return wf
    n_lora = _rebuild_loras(wf, (opts or {}).get("loras") or [])
    passes = _passes(wf)
    base_p = (opts or {}).get("base") or {}
    hires_p = (opts or {}).get("hires") or {}
    for nid in passes["base"]:
        _set_sampler(wf, nid, base_p)
    for nid in passes["hires"]:
        _set_sampler(wf, nid, hires_p)
    w = _i((opts or {}).get("width"), 0)
    h = _i((opts or {}).get("height"), 0)
    if w and h:
        for nid in _latent_ids(wf):
            inp = _inputs(wf[nid])
            inp["width"], inp["height"] = w, h
    log.info("渲染参数已应用：LoRA %d 槽，base %s步/%s，hires %s步/%s，%d×%d",
             n_lora, base_p.get("steps"), base_p.get("sampler_name"),
             hires_p.get("steps"), hires_p.get("sampler_name"), w, h)
    return wf


def describe(opts):
    """一句话摘要，进 comic 的渲染日志（出图慢时好回头查参数）。"""
    o = opts or {}
    b, hi = o.get("base") or {}, o.get("hires") or {}
    return ("LoRA %d 槽 / base %s步 cfg%s %s+%s / hires %s步 cfg%s %s+%s d%s / %sx%s"
            % (len([s for s in (o.get("loras") or []) if s.get("name")]),
               b.get("steps"), b.get("cfg"), b.get("sampler_name"),
               b.get("scheduler"), hi.get("steps"), hi.get("cfg"),
               hi.get("sampler_name"), hi.get("scheduler"),
               hi.get("denoise"), o.get("width"), o.get("height")))
