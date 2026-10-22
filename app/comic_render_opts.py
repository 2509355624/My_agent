"""漫画场景渲染参数：按任务或全局默认覆盖 anima-line 工作流（2026-10-13 用户拍板）。

两级持久化（2026-10-18 用户提出「能不能长期保存某个参数作为默认」）：

1. **任务级** `任务目录/render.json`（默认渠道）/ `render_<渠道>.json`——这一本
   专用，只影响本任务之后渲染的格；
2. **全局默认** `state/comic_render_defaults.json`——跨任务的那一份，新任务
   没存过就照它。取值优先级：**任务 > 全局默认 > 模板当前值**。

开跑时读一次，只改**这一次提交的工作流副本**，skill 模板一个字节不动。模板是
全局的（所有任务、AI 的 `update_workflow` 也在写它），不能拿来存单人单次的心血来潮。

anima-line 是**两段采样**（摸清结构才敢写覆盖，节点号以 2026-10-13 模板为准）：
  - 第一段 KSampler（节点 2）：latent 来自 EmptyLatentImage，denoise=1，
    下面挂 6 个 `LoraLoaderModelOnly`（16/15/28/29/30/31，其中
    `anima_context_detailer_base10` 出现两次、强度 0.35 和 0.8）。
    **LoRA 只在这一段生效。**
  - 第二段 KSampler（节点 27）：latent 来自 LatentUpscaleBy（×1.2），
    denoise=0.25，model 挂在**另一个底模**（silvermoonmix）上、**不接 LoRA**。
所以覆盖也按两段分开给：base / hires 各一套 steps / cfg / sampler_name /
scheduler，hires 再多一个 denoise（放大精修的强度）；LoRA 链只重建第一段那条。

**第二段可以整个关掉**（2026-10-22 用户要的「关闭二次采样」）：`hires.skip=true`
时把消费第二段输出的地方改指第一段输出，只跑一次采样直出。省的时间很实在
（krea2 832×1216：两段 49 秒 → 单段 8 步 15.7 秒 / 6 步 10.5 秒），代价是
**latent 放大这一级跟着失效**（它只喂第二段），出图回到「底图 × 最终放大」。
batch 族（sd / miao）把高清写在自己节点内部，没有独立第二段可断，`skip_hires`
为 False —— 面板不给这个开关，免得勾了没反应。

两级放大也在链上，一起纳进覆盖（2026-10-13 用户要能看到能改）：
  - **latent 放大**（节点 25 `LatentUpscaleBy`，scale_by=1.2）：第二段采样
    **之前**把潜空间放大——它决定第二段实际跑的尺寸，改大第二段显存/耗时
    跟着涨（最终成图 ≈ 底图 × scale_by × 最终放大倍率）。
  - **最终像素放大**（节点 32 `UpscaleModelLoader` + 33 `ImageUpscaleWithModel`，
    2x_Ani4Kv2）：出图前最后一道。模型可换（ComfyUI 的 upscale_models 清单），
    也能整道跳过（SaveImage 直连 VAEDecode，出图回到 hires 尺寸）。

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

from app.config import state_path
from app.skills import load_skill

log = logging.getLogger("comic")

# 任务目录里的参数文件名
RENDER_FILE = "render.json"

# 全局默认参数（state/ 下）。跨任务生效，新任务 / 没存过 render.json 的任务照它。
DEFAULTS_FILE = "comic_render_defaults.json"

# 默认从哪个渠道的模板读当前值（漫画场景图渠道；调用方传 comic.SCENE_SKILL）
DEFAULT_SKILL = "anima-line"

# 两种 LoRA 加载器类名（与 comfy_workflow.LORA_CLASSES 保持一致；这里不 import
# 那个模块的常量，免得它哪天为 AI 工具改语义时把这条链路带歪）
LORA_CLASSES = ("LoraLoader", "LoraLoaderModelOnly")

# 底图 latent 节点（多渠道，2026-10-19）：anima/sd 是 EmptyLatentImage，
# qwen 用 EmptySD3LatentImage，moss 用 EmptyFlux2LatentImage。
# 只用于「老模板兜底」的识别；实际取节点走 `_latent_ids()`（按 Empty* 前缀）。
LATENT_CLASSES = ("EmptyLatentImage", "EmptySD3LatentImage",
                  "EmptyFlux2LatentImage")

# 采样参数合法范围（超出钳位 + 回一句警告，不拒——这是给人填的表，不是 AI 闸门）
STEPS_RANGE = (1, 150)
CFG_RANGE = (0.0, 20.0)
DENOISE_RANGE = (0.0, 1.0)
STRENGTH_RANGE = (0.0, 2.0)
SIZE_RANGE = (256, 4096)          # 宽高都限这个范围；且必须是 8 的倍数
UPSCALE_RANGE = (0.25, 4.0)       # latent 放大倍率（第二段采样前那一级）
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
    if ct in LATENT_CLASSES or (ct or "").startswith("Empty"):
        return "base"
    if ct in ("LatentUpscaleBy", "LatentUpscale"):
        return "hires"
    return None


def _family(nd):
    """采样节点属于哪一族（多渠道路由用，2026-10-19）。

    同一个「一段采样」在三种工作流里长得不一样：
      - `KSampler` / `KSamplerAdvanced`：标准采样器，参数在节点上，两段就是两个节点；
      - `BatchPromptImageGenerator`：**采样全在这个复合节点里**，它自己带
        `hires_steps` / `hires_denoise` / `hires_width` / `hires_height`（sd、miao）；
      - `SamplerCustomAdvanced`：moss 的 4 步 DMD 链，步数由适配器 step_codes
        定死，节点上根本没有 steps 可改。
    """
    ct = (nd or {}).get("class_type")
    if ct in ("KSampler", "KSamplerAdvanced"):
        return "ks"
    if ct in ("BatchPromptImageGenerator", "DualModelBatchPromptImageGenerator"):
        return "batch"
    if ct in ("SamplerCustomAdvanced",):
        return "custom"
    return None


def _passes(wf):
    """{'base': [节点 id...], 'hires': [...]}

    batch 族的 hires 是节点内部字段，不算独立段；判断「有没有第二段」用
    `_has_hires()`。
    """
    out = {"base": [], "hires": []}
    for nid, nd in (wf or {}).items():
        fam = _family(nd)
        if fam == "ks":
            p = _sampler_pass(wf, nid)
            if p:
                out[p].append(nid)
        elif fam in ("batch", "custom"):
            # custom（moss 的 DMD 链）也认成 base 段：步数改不了，但**尺寸**要能改，
            # 而且不认它就等于「这个渠道没有采样节点」，面板会错显成可调。
            out["base"].append(nid)
    return out


def _batch_hires(wf):
    """batch 族的第二段（节点自带 hires_*）→ 节点 id；没有返回 None。"""
    for nid, nd in (wf or {}).items():
        if _family(nd) == "batch":
            inp = _inputs(nd)
            if "hires_steps" in inp or "hires_denoise" in inp:
                return nid
    return None


def _has_hires(wf):
    """这份工作流有没有「第二段」可调。

    ks 族看第二个 KSampler；batch 族看节点有没有 hires_*；custom 族没有。
    """
    if _passes(wf)["hires"]:
        return True
    return _batch_hires(wf) is not None


def _latent_ids(wf):
    """底图 latent 节点：EmptyLatentImage / EmptySD3LatentImage /
    EmptyFlux2LatentImage 等（qwen 用 SD3 版、moss 用 Flux2 版）。

    统一按「class_type 以 Empty 开头且带 width」认，免得每加一个渠道补一次名单。
    batch 族没有独立 latent 节点，宽高在节点自己身上，由 `_size_hosts()` 一起返回。
    """
    return [nid for nid, nd in (wf or {}).items()
            if str((nd or {}).get("class_type") or "").startswith("Empty")
            and "width" in _inputs(nd)]


def _size_hosts(wf):
    """宽高写在哪：独立 latent 节点 + batch 复合节点（sd/miao 的 width/height
    就在它身上）。返回节点 id 列表。"""
    ids = _latent_ids(wf)
    for nid in _passes(wf)["base"]:
        if _family(wf.get(nid)) == "batch" and "width" in _inputs(wf.get(nid)):
            ids.append(nid)
    return ids


def _upscale_nodes(wf):
    """两级放大节点：{'latent': [latent 放大...], 'loader': 模型加载器,
    'final': 最终像素放大}。按 class_type 认，不按节点号。"""
    out = {"latent": [], "loader": None, "final": None}
    for nid, nd in (wf or {}).items():
        ct = (nd or {}).get("class_type")
        if ct in ("LatentUpscaleBy", "LatentUpscale"):
            out["latent"].append(nid)
        elif ct == "UpscaleModelLoader":
            out["loader"] = nid
        elif ct == "ImageUpscaleWithModel":
            out["final"] = nid
    return out


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

def _upscale_params(wf):
    """当前两级放大的配置 = 参数卡「放大」小节的默认值。"""
    up = _upscale_nodes(wf)
    latent_inp = _inputs(wf.get(up["latent"][0])) if up["latent"] else {}
    loader_inp = _inputs(wf.get(up["loader"])) if up["loader"] else {}
    return {
        "scale_by": _f(latent_inp.get("scale_by"), 1.2),
        "final_model": str(loader_inp.get("model_name") or ""),
        "final_skip": False,     # 默认走完整链（模板里两级放大都在）
    }


def _batch_hires_params(wf):
    """batch 族（sd / miao）的第二段参数：节点上的 hires_steps / hires_denoise。"""
    nid = _batch_hires(wf)
    if not nid:
        return {}
    inp = _inputs(wf.get(nid))
    out = {}
    if "hires_steps" in inp:
        out["steps"] = inp["hires_steps"]
    if "hires_denoise" in inp:
        out["denoise"] = inp["hires_denoise"]
    return out


def defaults(skill=DEFAULT_SKILL):
    """这个渠道模板的当前配置 = 参数卡的默认值（用户没存过就照它显示）。

    三种节点族都认（2026-10-19）：标准 KSampler 两段 / BatchPromptImageGenerator
    自带 hires / moss 的 SamplerCustomAdvanced。
    """
    sd = load_skill(skill) or {}
    wf = sd.get("workflow") or {}
    passes = _passes(wf)
    hosts = _size_hosts(wf)
    lat = _inputs(wf.get(hosts[0])) if hosts else {}
    base_nid = passes["base"][0] if passes["base"] else None
    if passes["hires"]:
        hires = _sampler_params(wf, passes["hires"][0], with_denoise=True)
        # 第二段默认开着（模板里它就是开着的），这里把开关这个键补出来。
        # 只给有独立 hires 节点的渠道补：batch 族（sd / miao）的高清写在复合
        # 节点内部，没有可断的引用，多一个 skip 只会让人以为能关。
        hires.setdefault("skip", False)
    else:
        hires = _batch_hires_params(wf)
    return {
        "loras": _lora_slots(wf, passes["base"]),
        "base": _sampler_params(wf, base_nid),
        "hires": hires,
        "upscale": _upscale_params(wf),
        "width": _i(lat.get("width"), 1024),
        "height": _i(lat.get("height"), 1536),
    }


def _batch_upscale(wf):
    """batch 族把最终放大写在节点里（`upscale_model` 输入指向 UpscaleModelLoader），
    没有独立的 ImageUpscaleWithModel 节点可绕。有就返回节点 id。"""
    for nid in _passes(wf)["base"]:
        if _family(wf.get(nid)) == "batch":
            u = _inputs(wf.get(nid)).get("upscale_model")
            if isinstance(u, list) and u:
                return nid
    return None


def caps(workflow):
    """这个渠道能调什么、会出多大 —— 参数卡按它渲染（2026-10-19 多渠道）。

    返回：
      family       'ks' / 'batch' / 'custom' / 'none'
      passes       1 或 2（第二段能不能调）
      editable     采样参数能不能改（moss 4 步的 custom 链是 False）
      lora         有没有 LoRA 链可调
      upscale_latent / upscale_final  两级放大的存在性
      width/height       底图尺寸
      out_width/out_height  按模板算出来的预计出图尺寸（最终像素放大按 2x 估）
    """
    wf = workflow or {}
    passes = _passes(wf)
    fam = _family(wf.get(passes["base"][0])) if passes["base"] else None
    up = _upscale_nodes(wf)
    hosts = _size_hosts(wf)
    lat = _inputs(wf.get(hosts[0])) if hosts else {}
    w0, h0 = _i(lat.get("width"), 0), _i(lat.get("height"), 0)

    if fam == "batch" and _batch_hires(wf):
        bi = _inputs(wf.get(_batch_hires(wf)))
        ow = _i(bi.get("hires_width"), w0)
        oh = _i(bi.get("hires_height"), h0)
    else:
        sy = 1.0
        if up["latent"]:
            sy = _f(_inputs(wf.get(up["latent"][0])).get("scale_by"), 1.0)
        ow, oh = int(round(w0 * sy)), int(round(h0 * sy))
    # 第二段把画面放大多少倍（ks 族 = latent 倍率、batch 族 = 高清尺寸比）：
    # 关掉二次采样时这一级就不存在了，前端按它算「预计出图」。
    hf = round(ow / float(w0), 4) if w0 else 1.0
    ff = 1
    if up["final"] or _batch_upscale(wf):
        # 本机挂的放大模型都是 2x（2x_Ani4Kv2 / 4x 的会在名字里写 4x，仍按 2x 保守估）
        ow, oh = ow * 2, oh * 2
        ff = 2

    return {
        "family": fam or "none",
        "passes": 2 if _has_hires(wf) else (1 if passes["base"] else 0),
        "editable": fam not in ("custom", None),
        # LoRA **只让 ks 族可调**（2026-10-19）。sd / miao 的链是渠道配方的一部分
        # （降对比、提饱和、表情风格），面板一露出来就会被全局默认里 anima 那 6 槽
        # 覆盖掉；moss 没有链。宁可少一个旋钮，也不让人一键把渠道配方拆了。
        "lora": fam == "ks" and any(
            (nd or {}).get("class_type") in LORA_CLASSES for nd in wf.values()),
        "upscale_latent": bool(up["latent"]),
        # batch 族把最终放大写在节点里（sd 的 `upscale_model` 输入），没有独立的
        # ImageUpscaleWithModel 节点可绕，所以「跳过最终放大」只在有那个节点时给。
        "upscale_final": bool(up["final"]) or (
            fam == "batch" and _batch_upscale(wf) is not None),
        "upscale_skip": bool(up["final"]),
        # 第二段能不能整个关掉：只有 ks 族有独立的 hires KSampler 可断引用。
        "skip_hires": bool(passes["hires"]),
        "hires_factor": hf,
        "final_factor": ff,
        "width": w0,
        "height": h0,
        "out_width": ow,
        "out_height": oh,
    }


def caps_of(skill):
    """按 skill 名取 caps（读不到模板 = 空 caps，前端当「这个渠道没参数」）。"""
    sd = load_skill(skill) or {}
    return caps(sd.get("workflow") or {})


# ─── 存取 ───────────────────────────────────────────────

def _safe_name(skill):
    """渠道名能当文件名用（skill id 就带 - 和 _，别的一律剔掉）。"""
    return "".join(c for c in str(skill or "")
                   if c.isalnum() or c in "-_.").strip(".")


def path_of(task_dir, skill=DEFAULT_SKILL):
    """**这个渠道**的参数文件。

    默认渠道沿用 `render.json`（老任务的文件不用迁）；别的渠道各存各的
    （2026-10-19）。共用一个文件的后果很具体：anima 的 20 步 / hires 20 步 /
    latent ×1.2 会整份套到 miao 的 45 步模型上，切一次渠道图就废一批。
    """
    sk = _safe_name(skill)
    if not sk or sk == DEFAULT_SKILL:
        return os.path.join(task_dir, RENDER_FILE)
    return os.path.join(task_dir, "render_%s.json" % sk)


def defaults_path():
    """全局默认参数文件的路径（state/ 下，测试进程会被导向临时目录）。"""
    return state_path(DEFAULTS_FILE)


def _read_defaults_raw():
    """全局默认文件原样读出来；没有 / 读坏 → {}。"""
    fp = defaults_path()
    if not os.path.isfile(fp):
        return {}
    try:
        with open(fp, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        log.warning("全局渲染参数读不出来（%s），按模板默认值", e)
        return {}
    return data if isinstance(data, dict) else {}


def _is_flat(data):
    """老格式：整个文件就是一份 opts（2026-10-13 ~ 10-19 只有 anima-line 时）。

    多渠道之后按渠道分键存（`{"anima-line": {...}, "miao": {...}}`），否则
    anima 的步数/放大倍率会盖到 krea2、miao 头上去（2026-10-19 实测：krea2
    模板 latent 放大是 2.0，被全局默认的 1.2 盖掉，出图尺寸少一半）。
    """
    return any(k in (data or {}) for k in ("width", "height", "base", "loras",
                                           "hires", "upscale"))


def has_defaults(skill=DEFAULT_SKILL):
    """这个渠道有没有存过全局默认。"""
    data = _read_defaults_raw()
    if not data:
        return False
    if _is_flat(data):
        return skill == DEFAULT_SKILL
    return isinstance(data.get(skill), dict)


def load_defaults(skill=DEFAULT_SKILL):
    """读**这个渠道**的全局默认参数；没存过 / 读坏了返回 None（None = 不参与叠加）。

    老格式文件只认默认渠道（anima-line）：那时候全局默认就是给它存的，套到别的
    渠道上会串参数。
    """
    data = _read_defaults_raw()
    if not data:
        return None
    if _is_flat(data):
        saved = data if skill == DEFAULT_SKILL else None
    else:
        saved = data.get(skill)
    if not isinstance(saved, dict):
        return None
    opts, errors, _w = normalize(saved, base=defaults(skill))
    if errors:
        # 存的时候校验过，这里还报错只可能是文件被手改坏——能用的部分照用，
        # 把话说在日志里，别让整本漫画卡在「参数读不出来」上。
        log.warning("全局渲染参数有不对的地方（%s），已按能用的部分生效",
                    "；".join(errors))
    return opts


def save_defaults(opts, skill=DEFAULT_SKILL):
    """存这个渠道的全局默认参数（该渠道没存过 render.json 的任务都照它跑）。"""
    opts, errors, _w = normalize(opts, base=defaults(skill))
    if errors:
        raise ValueError("；".join(errors))
    fp = defaults_path()
    data = _read_defaults_raw()
    if _is_flat(data):          # 老格式：把这份归到默认渠道名下再升级
        data = {DEFAULT_SKILL: data}
    data[skill] = opts
    os.makedirs(os.path.dirname(fp), exist_ok=True)
    with open(fp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    return opts


def reset_defaults(skill=None):
    """删掉全局默认参数 → 回到模板值。给了 skill 只删那一个渠道的。"""
    fp = defaults_path()
    if not os.path.isfile(fp):
        return False
    data = _read_defaults_raw()
    if skill and data and not _is_flat(data):
        if skill not in data:
            return False
        data.pop(skill, None)
        if data:
            with open(fp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
            return True
    os.remove(fp)
    return True


def load(task_dir, skill=DEFAULT_SKILL):
    """读参数：**任务 render.json > 全局默认 > 模板当前值**。

    两级文件都是「覆盖」语义：全局默认先叠在模板上，任务级再叠在它上面。
    读坏不炸：那是用户手改坏的文件，退回下一级照常出图，比整本漫画卡死强。

    ⚠️ 不要在这里做「LoRA 名字不在模板里就丢掉」这种收敛（2026-10-19 试过又
    撤了）：模板里当然不会有用户自己训的角色 LoRA，一过滤就把最常用的功能丢
    了。跨渠道串参数靠**存取层**解决——任务级 `render_<渠道>.json`、全局默认
    按渠道分键，两处都不共用文件，就没有「anima 的槽跑到 krea2 上」这回事。
    """
    base = load_defaults(skill) or defaults(skill)
    if not task_dir:
        return base
    fp = path_of(task_dir, skill)
    if not os.path.isfile(fp):
        return base
    try:
        with open(fp, "r", encoding="utf-8") as f:
            saved = json.load(f)
    except (OSError, ValueError) as e:
        log.warning("render.json 读不出来（%s），按全局默认 / 模板值渲染", e)
        return base
    if not isinstance(saved, dict):
        log.warning("render.json 不是对象，按全局默认 / 模板值渲染")
        return base
    opts, errors, _w = normalize(saved, base=base)
    if errors:
        # 存的时候就校验过，这里还报错只可能是文件被手改坏——不阻塞出图，
        # 但要把话说在日志里，免得用户以为存进去了。
        log.warning("render.json 有越界值（%s），已钳位", "；".join(errors))
    return opts


def save(task_dir, opts, skill=DEFAULT_SKILL):
    """存**这个渠道**的任务级参数（别的渠道那份不动）。"""
    opts, errors, _w = normalize(opts)
    if errors:
        raise ValueError("；".join(errors))
    os.makedirs(task_dir, exist_ok=True)
    with open(path_of(task_dir, skill), "w", encoding="utf-8") as f:
        json.dump(opts, f, ensure_ascii=False, indent=1)
    return opts


def reset(task_dir, skill=DEFAULT_SKILL):
    """删掉这个渠道的任务级参数 → 回到全局默认 / 模板默认值。"""
    fp = path_of(task_dir, skill)
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
    问题（LoRA 名 ComfyUI 里没有……）；warnings 是被钳位 / 对齐的字段
    （步数 999 → 150、宽 812 → 808）。base 给了就按它补缺失的键（部分更新
    不丢字段）。resources 给了（ComfyUI 在线时的清单）才校验名字——没有
    证据源就不猜。
    """
    base = base or {}
    raw = raw if isinstance(raw, dict) else {}
    res = resources or {}
    lora_list = res.get("loras") or []
    samplers = res.get("samplers") or []
    schedulers = res.get("schedulers") or []
    upscales = res.get("upscales") or []
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
        if with_denoise and ("skip" in rp or "skip" in out):
            # 第二段采样开关（"skip"）——关掉就只跑一次采样直出。
            # 只在本来就认识这个键的渠道里保留：batch 族（sd / miao）关不掉，
            # 别往它们的 render.json 里塞一个永远不起作用的字段。
            out["skip"] = bool(rp.get("skip", out.get("skip", False)))
        return out

    # ── 两级放大（latent 倍率 / 最终像素放大模型 / 跳过最终放大）
    ru = raw.get("upscale")
    bu = base.get("upscale") if isinstance(base.get("upscale"), dict) else {}
    ru = ru if isinstance(ru, dict) else {}
    if not ru and not bu:
        # 从没提供过放大信息（老 render.json / 部分更新）：不校验、给空，
        # apply 时空 dict = 放大节点原样不动。
        up = {}
    else:
        sy = _f(ru.get("scale_by", bu.get("scale_by", 1.2)), 1.2)
        csy = _clamp(sy, *UPSCALE_RANGE)
        if abs(csy - sy) > 1e-9:
            warnings.append("latent 放大 %.2f 超出 %s~%s，已钳成 %.2f"
                            % (sy, UPSCALE_RANGE[0], UPSCALE_RANGE[1], csy))
        fm = str(ru.get("final_model", bu.get("final_model", "")) or "").strip()
        skip = bool(ru.get("final_skip", bu.get("final_skip", False)))
        if not skip:
            if not fm:
                errors.append("最终放大模型不能为空（或勾选「跳过最终放大」）")
            elif upscales and fm not in upscales:
                errors.append("ComfyUI 里没有放大模型「%s」" % fm)
        up = {"scale_by": csy, "final_model": fm, "final_skip": skip}

    opts = {
        "loras": loras,
        "base": _pass("base", with_denoise=False),
        "hires": _pass("hires", with_denoise=True),
        "upscale": up,
    }

    # ── 分辨率（EmptyLatentImage 的宽高，8 的倍数）
    #
    # 不是 8 的倍数**对齐**而不是拒（2026-10-18 用户填 812×1024 起的改动）：
    # ComfyUI 的 latent 本来就是 //8 截断，812 实际渲染就是 808——与其让表
    # 存不进去，不如按它真正会跑的尺寸存下来，并把这事写进 warnings。
    w = _i(raw.get("width", base.get("width", 1024)), 1024)
    h = _i(raw.get("height", base.get("height", 1536)), 1536)
    for label, v in (("宽", w), ("高", h)):
        if v % 8:
            warnings.append("%s %d 不是 8 的倍数，按 %d 渲染（ComfyUI 的 latent "
                            "是 /8 截断的）" % (label, v, v // 8 * 8))
    cw, ch = int(_clamp(w, *SIZE_RANGE)), int(_clamp(h, *SIZE_RANGE))
    if (cw, ch) != (w, h):
        warnings.append("分辨率 %d×%d 超出 %d~%d，已钳成 %d×%d"
                        % (w, h, SIZE_RANGE[0], SIZE_RANGE[1], cw, ch))
    cw, ch = cw // 8 * 8, ch // 8 * 8
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


def _set_batch_hires(wf, nid, params):
    """BatchPromptImageGenerator 的第二段：字段名带 hires_ 前缀，只认步数和
    denoise（这个节点没有第二段的 cfg / 采样器可调）。"""
    inp = _inputs(wf.get(nid))
    if "steps" in params and "hires_steps" in inp:
        inp["hires_steps"] = params["steps"]
    if "denoise" in params and "hires_denoise" in inp:
        inp["hires_denoise"] = params["denoise"]


def _apply_upscale(wf, up):
    """两级放大覆盖：latent 倍率 / 最终放大模型 / 跳过最终放大。

    跳过时把最终放大节点的消费者（SaveImage 的 images 输入）改指它的 image
    来源（VAEDecode）。节点**留着不删**——ComfyUI 只执行输出可达的节点，没人
    引用的节点不会跑；删节点反而要扫全部引用，容易漏（load 顺序错一点整条
    链就断）。返回动了几个地方。
    """
    up = up or {}
    nodes = _upscale_nodes(wf)
    n = 0
    sy = up.get("scale_by")
    if sy is not None:
        for nid in nodes["latent"]:
            inp = _inputs(wf.get(nid))
            if "scale_by" in inp:
                inp["scale_by"] = float(sy)
                n += 1
            else:
                log.warning("渲染参数：节点 %s 不是 LatentUpscaleBy，"
                            "latent 放大倍率跳过", nid)
    fm = str(up.get("final_model") or "").strip()
    if fm and nodes["loader"]:
        inp = _inputs(wf.get(nodes["loader"]))
        if "model_name" in inp:
            inp["model_name"] = fm
            n += 1
    if up.get("final_skip") and nodes["final"]:
        fid = nodes["final"]
        src = _inputs(wf.get(fid)).get("image")
        if isinstance(src, list) and src:
            src = [str(src[0]), src[1] if len(src) > 1 else 0]
            for nd in wf.values():
                inp = _inputs(nd)
                for k, v in list(inp.items()):
                    if isinstance(v, list) and v and str(v[0]) == fid:
                        inp[k] = src
            n += 1
    return n


def pin_output_size(workflow, width, height):
    """把出图尺寸钉死成 width×height：底图、batch 的第二段、两级放大全按住。

    和 `apply()` 的语义不同：apply 是「按模板的规矩改参数」——batch 族的第二段
    尺寸会跟着底图**等比缩放**（miao 1024×1536→1536×2304，底图改成 864×1536
    就成了 1296×2304，画面还是被拉长）；这里要的是「就是这么大」，所以第二段
    直接同尺寸精修，节点内的 `upscale_model` 也断开。

    角色卡封面**曾经**靠它钉成 864×1536（星陨AI 的上传规格），10-20 用户实测
    嫌糊、改回渠道默认尺寸后就没人调了；留着给「必须精确尺寸」的场景（比如将来
    漫画台按固定画布拼版）。返回动了几个地方，方便调用方判断模板是不是意料之外
    的形状。
    """
    wf = workflow or {}
    n = _apply_upscale(wf, {"scale_by": 1.0, "final_skip": True})
    for nid in _size_hosts(wf):
        inp = _inputs(wf[nid])
        inp["width"], inp["height"] = width, height
        n += 1
        if inp.get("enable_hires") and "hires_width" in inp:
            # enable_hires 关掉的渠道（jank）别替它开，只改它本来会用的值。
            inp["hires_width"], inp["hires_height"] = width, height
            n += 1
        if "upscale_model" in inp:
            # optional 输入：不传就不放大。它在节点**内部**跑，SaveImage 那条路
            # 管不到（`final_skip` 只重接独立节点），image_gen_v1 的 2x 就是它。
            inp.pop("upscale_model", None)
            n += 1
    return n


def _cut_hires(wf):
    """关掉第二段采样：把消费 hires 采样器输出的输入改指 base 采样器。

    和 `_apply_upscale` 的 final_skip 是同一招——**断引用、不删节点**：ComfyUI
    只执行从输出可达的节点，没人引用的第二段不会跑。删节点要扫全部引用，漏
    一处整条链就断（这一点 `tools/bench_render.py` 已经实测过）。

    latent 放大节点会跟着变不可达（它只喂第二段），所以出图尺寸回到「底图 ×
    最终像素放大」——面板上那句提示和这里的实际行为必须一致。

    返回改了几处引用；没有独立的 hires 节点（单段 / batch 族）返回 0。
    """
    passes = _passes(wf)
    if not passes["hires"] or not passes["base"]:
        return 0
    base = passes["base"][0]
    dead = {str(n) for n in passes["hires"]}
    n = 0
    for nd in wf.values():
        inp = _inputs(nd)
        for k, v in list(inp.items()):
            if isinstance(v, list) and v and str(v[0]) in dead:
                # 两段输出的槽位都是 LATENT[0]，索引照搬
                inp[k] = [base, v[1] if len(v) > 1 else 0]
                n += 1
    return n


def apply(workflow, opts):
    """把渲染参数应用到工作流，返回**新 dict**（不改入参）。

    调用方传进来的是 build_t2i_workflow 刚建的新鲜副本，照样深拷贝一份：
    apply 是纯函数，测试和「同一模板跑两遍不同参数」都省心。
    """
    wf = copy.deepcopy(workflow or {})
    if not wf:
        return wf
    # LoRA 只对**模板里本来就有 LoRA 链**的渠道生效（2026-10-19 多渠道）。
    # 全局默认里存的是 anima 那 6 槽；切到 sd / miao / qwen 这些没有链的渠道时
    # 若照旧重建，会往 SDXL checkpoint 链上硬塞 LoraLoaderModelOnly（要 clip
    # 输出、加载器没这个槽），提交直接被 ComfyUI 拒。
    n_lora = _rebuild_loras(wf, (opts or {}).get("loras") or []) \
        if caps(wf)["lora"] else 0
    passes = _passes(wf)
    base_p = (opts or {}).get("base") or {}
    hires_p = (opts or {}).get("hires") or {}
    for nid in passes["base"]:
        _set_sampler(wf, nid, base_p)
    for nid in passes["hires"]:
        _set_sampler(wf, nid, hires_p)
    if hires_p:
        bh = _batch_hires(wf)
        if bh:
            _set_batch_hires(wf, bh, hires_p)
    w = _i((opts or {}).get("width"), 0)
    h = _i((opts or {}).get("height"), 0)
    if w and h:
        for nid in _size_hosts(wf):
            inp = _inputs(wf[nid])
            ow, oh = _i(inp.get("width"), w), _i(inp.get("height"), h)
            # batch 族的第二段尺寸是**写死的数值**（sd 720×1280→1440×2560、
            # miao 1024×1536→1536×2304），底图一改必须等比跟改，否则画幅被拉伸。
            if ow and oh and (ow, oh) != (w, h) and "hires_width" in inp:
                rx, ry = w / float(ow), h / float(oh)
                hw = _i(inp.get("hires_width"), 0)
                hh = _i(inp.get("hires_height"), 0)
                if hw and hh:
                    inp["hires_width"] = max(8, int(round(hw * rx)) // 8 * 8)
                    inp["hires_height"] = max(8, int(round(hh * ry)) // 8 * 8)
            inp["width"], inp["height"] = w, h
    n_up = _apply_upscale(wf, (opts or {}).get("upscale") or {})
    # 关掉二次采样：放在参数写完之后（第二段的 steps / denoise 写不写都无所谓，
    # 它已经不可达了），也不影响上面按节点 id 找放大节点。
    n_cut = _cut_hires(wf) if hires_p.get("skip") else 0
    log.info("渲染参数已应用：LoRA %d 槽，base %s步/%s，hires %s%s，放大 %s，%d×%d",
             n_lora, base_p.get("steps"), base_p.get("sampler_name"),
             "已关闭（单段直出）" if n_cut else "%s步/%s"
             % (hires_p.get("steps"), hires_p.get("sampler_name")),
             _up_desc((opts or {}).get("upscale") or {}, bool(n_cut)), w, h)
    return wf


def _up_desc(up, no_latent=False):
    if up.get("final_skip"):
        # final_skip 时 latent 那级是否生效就看二次采样开没开
        if no_latent:
            return "原尺寸（二次采样已关 + 跳过最终放大）"
        return "×%s（跳过最终放大）" % up.get("scale_by")
    if no_latent:
        return "跳过 latent（二次采样已关）+ %s" % up.get("final_model")
    return "×%s + %s" % (up.get("scale_by"), up.get("final_model"))


def describe(opts):
    """一句话摘要，进 comic 的渲染日志（出图慢时好回头查参数）。"""
    o = opts or {}
    b, hi = o.get("base") or {}, o.get("hires") or {}
    second = ("hires 关闭（单段直出）" if hi.get("skip")
              else "hires %s步 cfg%s %s+%s d%s"
              % (hi.get("steps"), hi.get("cfg"), hi.get("sampler_name"),
                 hi.get("scheduler"), hi.get("denoise")))
    return ("LoRA %d 槽 / base %s步 cfg%s %s+%s / %s / %sx%s / %s"
            % (len([s for s in (o.get("loras") or []) if s.get("name")]),
               b.get("steps"), b.get("cfg"), b.get("sampler_name"),
               b.get("scheduler"), second, o.get("width"), o.get("height"),
               _up_desc(o.get("upscale") or {}, bool(hi.get("skip")))))
