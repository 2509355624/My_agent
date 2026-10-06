"""
ComfyUI 工作流查看/调整工具

「当前工作流」= skill 的 workflow.json 模板（generate_image 实际生成用的那份）。
- get_workflow  : 让 AI「看到」当前工作流（模型 / LoRA链 / 采样参数 / 放大），并可附可用资源清单
- update_workflow: 让 AI 用语义化操作调整（改参数、换模型、增删调 LoRA、换放大），由代码改 JSON，不丢节点图给 LLM 手写
调整会写回 workflow.json（用户已确认：写回模板，持久生效）。
"""

import json
import os
import requests
from app.config import COMFYUI_URL
from app.skills import load_skill

# 可调的目标节点（按 class_type 定位）
CHECKPT = "CheckpointLoaderSimple"
UNET = "UNETLoader"
LORA = "LoraLoader"
# LoRA 加载器有两种：LoraLoader 同时挂 model+clip（SD1.5 系，已随 image_gen_v1
# 归档），LoraLoaderModelOnly 只挂 model（anima_soft 等动漫渠道 / krea2 这类
# UNETLoader 工作流）。
# 只认前一种的话动漫渠道的整条 LoRA 链会被判成「没有 LoRA」，后果不是显示不准而是
# 真把工作流改坏：update_workflow 会把 KSampler 的 model 直接改指 UNETLoader
# （两个 LoRA 全掉），并把 CLIPTextEncode 的 clip 接到 UNETLoader 上——而它根本
# 没有 clip 输出槽，提交必被 ComfyUI 拒。所以两种都要认。
LORA_CLASSES = (LORA, "LoraLoaderModelOnly")
UPSCALE = "UpscaleModelLoader"
GEN_HINTS = ("BatchPromptImageGenerator",)  # 优先识别为“采样生成节点”

_NODE_INFO = None


# ─── ComfyUI 资源枚举 ─────────────────────────────────

def _object_info(refresh=False):
    global _NODE_INFO
    if _NODE_INFO is None or refresh:
        _NODE_INFO = requests.get(COMFYUI_URL + "/object_info", timeout=20).json()
    return _NODE_INFO


def _choices(node_class, field):
    """取某节点 required 字段的可选值（采样器/模型/lora/放大 等）。
    ComfyUI combo 字段格式 = [[choice...], {options...}]，choices 在 v[0]；
    老格式 options 也可能存在尾 dict 的 'options' 键下，两种都兼容。
    """
    try:
        req = _object_info()[node_class]["input"]["required"]
        v = req.get(field)
        if isinstance(v, list) and v:
            if isinstance(v[0], list) and v[0] and isinstance(v[0][0], str):
                return v[0]
            tail = v[-1]
            if isinstance(tail, dict) and isinstance(tail.get("options"), list):
                return tail["options"]
    except Exception:
        pass
    return []


def resource_list():
    """返回 {samplers, schedulers, ckpts, loras, upscales}，缺则空"""
    return {
        "samplers": _choices("KSampler", "sampler_name"),
        "schedulers": _choices("KSampler", "scheduler"),
        "ckpts": _choices(CHECKPT, "ckpt_name"),
        "loras": _choices(LORA, "lora_name"),
        "upscales": _choices(UPSCALE, "model_name"),
    }


# ─── 工作流解析 ────────────────────────────────────────

def _find_node(workflow, node_class):
    for nid, nd in workflow.items():
        if nd.get("class_type") == node_class:
            return nid, nd
    return None, None


def _find_generator(workflow):
    for nid, nd in workflow.items():
        if nd.get("class_type") in GEN_HINTS:
            return nid, nd
    for nid, nd in workflow.items():
        inp = nd.get("inputs", {})
        if isinstance(nd.get("class_type"), str) and "width" in inp and "steps" in inp:
            return nid, nd
    # 标准节点工作流：采样节点就是 KSampler（width/height 在 EmptyLatentImage 上，
    # 所以上面那条 width+steps 的启发式命中不了它）。
    #
    # 这一条必须有：_rebuild_lora 靠 gen_id 把重建后的 LoRA 链重新挂回采样器
    # 和正负向 CLIPTextEncode。认不出 gen 时那段整体跳过，链接仍指向已删除的
    # 旧 LoRA 节点——工作流当场断掉，表现是「换 LoRA 之后一张都画不出来」。
    for nid, nd in workflow.items():
        if nd.get("class_type") == "KSampler":
            return nid, nd
    return None, None


def _base_loader_ids(workflow):
    """所有「底模加载器」节点 id，按 dict 顺序。

    用**小写包含**而不是精确匹配：ComfyUI 里同一个加载器有多种拼写——
    `CheckpointLoaderSimple`（image_gen_v1）、`UnetLoaderGGUF`（krea2）、
    `UNETLoader`（anima）。写死等号的话 krea2 的底模整个认不出来，
    摘要里显示「底模模型: 无」、链头也会变成 None。
    """
    out = []
    for nid, nd in workflow.items():
        ct = (nd.get("class_type") or "").lower()
        if "checkpointloader" in ct or "unetloader" in ct:
            out.append(nid)
    return out


def _chain_head(workflow):
    """LoRA 链该挂上去的那个节点 id（底模加载器）。

    优先沿现有连线回溯：找一个 model 来源**不是另一个 LoRA** 的 LoRA 节点，
    它指向谁，链头就是谁。这比按类名猜可靠——krea2 的底模是 UnetLoaderGGUF，
    按 "UNETLoader" 精确匹配会漏，链头成 None，清空 lora 后 gen.model 会写成
    [None, 0]，工作流当场断。

    不能按 dict 顺序取「第一个 LoRA」：anima 的 dict 顺序是 15 在 16 前面，
    而 15 是链尾（model 指向 16），拿它当链头会得到另一个 LoRA 的 id。
    """
    lora_ids = {nid for nid, nd in workflow.items()
                if nd.get("class_type") in LORA_CLASSES}
    for nid in lora_ids:
        src = (workflow[nid].get("inputs") or {}).get("model")
        if isinstance(src, list) and src and str(src[0]) not in lora_ids:
            return str(src[0])
    # 一个 LoRA 都没有（或全是环）：按类名兜底
    bases = _base_loader_ids(workflow)
    return bases[0] if bases else None


def _lora_kind(workflow):
    """这个工作流该用哪种 LoRA 节点重建。

    跟着**现有**节点走，不要凭空换类：anima/krea2 是 LoraLoaderModelOnly
    （只挂 model），image_gen_v1 是 LoraLoader（model+clip）。重建时把
    ModelOnly 换成 LoraLoader 会平白多出 clip 连线，而这类工作流的 CLIP 是
    CLIPLoader 单独喂的，压根不该经过 LoRA。
    """
    for nd in workflow.values():
        if nd.get("class_type") == "LoraLoaderModelOnly":
            return "LoraLoaderModelOnly"
    return LORA


def _lora_chain(workflow, gen_id):
    """回溯出当前 LoRA 链（按加载顺序，即靠近底模的在前）:
    [{name, strength_model, strength_clip, model_only}, ...]

    ModelOnly 加载器没有 strength_clip，这里留 None，重建时据此跳过该字段
    （塞进去 ComfyUI 会报未知输入）。
    """
    lora_by_id = {nid: nd for nid, nd in workflow.items()
                  if nd.get("class_type") in LORA_CLASSES}
    traced = []
    if not gen_id:
        return traced
    cur = workflow[gen_id]["inputs"].get("model")
    if not cur:
        return traced
    node_id = cur[0]
    seen = set()
    while node_id in lora_by_id and node_id not in seen:
        seen.add(node_id)
        nd = lora_by_id[node_id]
        inp = nd["inputs"]
        traced.append({
            "name": inp.get("lora_name"),
            "strength_model": inp.get("strength_model"),
            "strength_clip": inp.get("strength_clip"),
            "model_only": nd.get("class_type") == "LoraLoaderModelOnly",
        })
        m = inp.get("model")
        node_id = m[0] if m else None
    traced.reverse()  # 回溯是从生成节点往前，反转为加载顺序
    return traced


def _build_summary(workflow):
    ckpt_id, ckpt = _find_node(workflow, CHECKPT)
    up_id, up = _find_node(workflow, UPSCALE)
    gen_id, gen = _find_generator(workflow)
    gen_inp = gen["inputs"] if gen else {}

    params = {}
    for k in ("seed", "steps", "cfg", "denoise", "sampler_name", "scheduler",
              "hires_width", "hires_height", "hires_denoise", "hires_steps"):
        if k in gen_inp:
            params[k] = gen_inp[k]

    # width/height 在标准节点工作流里挂在 EmptyLatentImage 上，采样器上没有。
    # 摘要要报的是「实际出图尺寸」，所以从 latent 节点取，别指望 gen_inp 里有没有。
    lat_id, lat = _find_node(workflow, "EmptyLatentImage")
    if lat:
        for k in ("width", "height"):
            if k in lat["inputs"]:
                params[k] = lat["inputs"][k]

    chain = _lora_chain(workflow, gen_id)

    lines = ["## 当前工作流生成配置"]

    # 底模：image_gen_v1 是 CheckpointLoaderSimple 一个；anima 这类两段采样工作流
    # 有**两个** UNETLoader（两段各挂一个底模，第二段通常不接 LoRA），都得列出来，
    # 否则模型只看到一个底模，会以为工作流是单段的。krea2 的 UnetLoaderGGUF 也要算。
    bases = _base_loader_ids(workflow)
    if bases:
        for nid in bases:
            nd = workflow[nid]
            inp = nd["inputs"]
            key = "ckpt_name" if "ckpt_name" in inp else "unet_name"
            lines.append(f"- 底模[{nid} {nd.get('class_type')}]: {inp.get(key)}")
    else:
        lines.append("- 底模模型: 无")

    if chain:
        lines.append("- LoRA 链(" + str(len(chain)) + ")，按加载顺序:")
        for i, l in enumerate(chain, 1):
            if l.get("model_only"):
                # ModelOnly 没有 strength_clip，写 0/None 会让人以为 clip 被关了
                lines.append(f"  {i}. {l['name']}  strength_model={l['strength_model']}"
                             "  (ModelOnly，只挂 model)")
            else:
                lines.append(f"  {i}. {l['name']}  strength_model={l['strength_model']}"
                             f" strength_clip={l['strength_clip']}")
    else:
        lines.append("- LoRA: 无")

    if params:
        lines.append("- 采样参数: " + ", ".join(f"{k}={v}" for k, v in params.items()))

    # 两段采样时把每个采样节点都列出来，并标出 set 参数实际会写到哪个。
    # 不然模型改了「步数」只影响第一段，却对用户说整个工作流都调了。
    samplers = [(nid, nd) for nid, nd in workflow.items()
                if nd.get("class_type") == "KSampler"]
    if len(samplers) > 1:
        lines.append("- 采样节点(" + str(len(samplers)) + "段):")
        for nid, nd in samplers:
            inp = nd["inputs"]
            bits = ", ".join(f"{k}={inp[k]}" for k in
                             ("steps", "cfg", "denoise", "sampler_name", "scheduler")
                             if k in inp)
            tag = "  ← set 参数写这里" if nid == gen_id else ""
            lines.append(f"  [{nid}] {bits}{tag}")

    if up:
        lines.append("- 放大模型: " + str(up["inputs"].get("model_name")))
    return "\n".join(lines)


# ─── 读工作流 ──────────────────────────────────────────

def _load(skill):
    data = load_skill(skill)
    if not data or not data.get("workflow"):
        raise ValueError("Skill '" + skill + "' 没有 workflow.json")
    return data


def _wf_path(data):
    return os.path.join(data["path"], "workflow.json")


def _save(workflow, data):
    s = json.dumps(workflow, indent=2, ensure_ascii=False)
    s = s.replace('"__SEED__"', "__SEED__")  # 还原裸占位，保证 generate_image 的 seed 替换仍是数字
    with open(_wf_path(data), "w", encoding="utf-8") as f:
        f.write(s)


# ─── 工具 1：查看 ─────────────────────────────────────

def get_workflow(skill="anima_clear"):
    data = _load(skill)
    summary = _build_summary(data["workflow"])
    res_txt = ""
    try:
        r = resource_list()
        res_txt = ("\n\n## 可选资源\n"
                   "- 采样器: " + ", ".join(r["samplers"]) +
                   "\n- 调度器: " + ", ".join(r["schedulers"]) +
                   "\n- 底模(safetensors): " + ", ".join(r["ckpts"]) +
                   "\n- LoRA(safetensors): " + ", ".join(r["loras"]) +
                   "\n- 放大模型: " + ", ".join(r["upscales"]))
    except Exception as e:
        res_txt = "\n(拉取 ComfyUI 可选资源失败: " + str(e) + ")"
    return summary + res_txt


# ─── 工具 2：调整 ─────────────────────────────────────

def _apply_set(workflow, gen, op):
    keys = {
        "steps": "steps", "cfg": "cfg", "denoise": "denoise",
        "width": "width", "height": "height",
        "sampler": "sampler_name", "sampler_name": "sampler_name",
        "scheduler": "scheduler",
        "hires_width": "hires_width", "hires_height": "hires_height",
        "hires_denoise": "hires_denoise", "hires_steps": "hires_steps",
        "seed": "seed",
    }
    p = str(op.get("parameter") or op.get("key") or "").strip().lower()
    if p not in keys:
        return None
    k = keys[p]
    v = op.get("value", op.get("set"))
    if k in ("steps", "width", "height", "hires_width", "hires_height", "hires_steps", "seed"):
        v = int(v)
    elif k in ("cfg", "denoise", "hires_denoise", "strength_model", "strength_clip"):
        v = float(v)
    # 标准节点工作流里 width/height 挂在 EmptyLatentImage 上，不在采样器上。
    # 写进 KSampler 不报错但也不生效（ComfyUI 忽略多余字段），等于改了个寂寞，
    # 所以这里转投真正的宿主节点。
    if k in ("width", "height") and gen.get("class_type") == "KSampler":
        lat_id, lat = _find_node(workflow, "EmptyLatentImage")
        if lat is not None:
            lat["inputs"][k] = v
            return k + "=" + repr(v)
    gen["inputs"][k] = v
    return k + "=" + repr(v)


def _rebuild_lora(workflow, specs):
    """按 spec 顺序重建 LoRA 链并重连引用。

    ⚠️ 要重连的**不止 generator 一个**（2026-09-30 补）：anima 是两段采样，
    旧版里第二段的 KSampler 也挂在 LoRA 链尾（当时节点 2 是一段、27 是二段，
    两者都从链尾节点取 model）。旧实现只改 generator 和负向 CLIPTextEncode，
    链尾一换，另一段就指向一个**已被删除的节点 id**，工作流当场断掉，表现是
    「改了 LoRA 之后一张都画不出来」——和 `_find_generator` 里那条注释记的坑同源。

    所以先把「指着旧 LoRA 节点的引用」全记下来，重建完统一改指新链尾。
    （2026-09-30 换成 anime2 工作流后，二段的 KSampler 19 改挂**另一个底模**
    UNETLoader 20、不接 LoRA 了，那条重连对它自然不生效——这是工作流本身的
    设计，不是漏改。）

    ⚠️ 节点类必须跟着现有节点走：anima/krea2 是 LoraLoaderModelOnly（只挂
    model），image_gen_v1 是 LoraLoader（model+clip）。写死 LoraLoader 的话
    在 anima 上会往 UNETLoader 要 clip 输出（它没有这个槽），提交必被拒。
    """
    head_id = _chain_head(workflow)
    if head_id is None:
        # 连底模加载器都认不出来：这个工作流结构本工具不认识。什么都别动——
        # 硬重建会写出 [None, 0] 的 model 连线，比不改更糟。
        return []
    gen_id, gen = _find_generator(workflow)
    used = sorted(int(x) for x in workflow if str(x).lstrip('-').isdigit())
    next_id = (used[-1] + 1) if used else 1

    # 跟着现有节点的类重建：ModelOnly 工作流不能换成 LoraLoader（会多出 clip
    # 连线，而它的 CLIP 是 CLIPLoader 单独喂的），反之亦然。
    kind = _lora_kind(workflow)
    model_only = kind == "LoraLoaderModelOnly"

    # 旧 LoRA 节点 id：删掉之后，任何还指着它们的引用都得改指新链尾。
    # 两种类都算，漏掉 ModelOnly 的话旧节点删不掉、引用也改不动。
    old_lora_ids = {nid for nid, nd in workflow.items()
                    if nd.get("class_type") in LORA_CLASSES}

    # 删除所有旧 LoRA 节点
    for nid in list(workflow.keys()):
        if workflow[nid].get("class_type") in LORA_CLASSES:
            del workflow[nid]

    target = head_id
    new_ids = []
    for spec in specs:
        nid = str(next_id); next_id += 1
        inputs = {
            "model": [target, 0],
            "lora_name": str(spec.get("name", "")),
            "strength_model": spec.get("strength_model", 1),
        }
        if not model_only:
            inputs["clip"] = [target, 1]
            inputs["strength_clip"] = spec.get("strength_clip", 1)
        workflow[nid] = {"class_type": kind, "inputs": inputs}
        target = nid
        new_ids.append(nid)

    # 指着已删 LoRA 节点的引用统一改指新链尾（slot 原样保留：0=model / 1=clip）。
    # 链头都没找到（target is None）时不动——那说明这个工作流结构本来就不认识，
    # 写了 [None, 0] 进去只会更难查。
    if old_lora_ids and target is not None:
        for nd in workflow.values():
            inputs = nd.get("inputs")
            if not isinstance(inputs, dict):
                continue
            for key, val in list(inputs.items()):
                if isinstance(val, list) and val and str(val[0]) in old_lora_ids:
                    inputs[key] = [target, val[1] if len(val) > 1 else 0]

    if gen_id:
        gen["inputs"]["model"] = [target, 0]
        if not model_only:
            gen["inputs"]["clip"] = [target, 1]
            # 负向 CLIPTextEncode 的 clip 也挂到最后一级 LoRA。
            # ModelOnly 工作流**不能**这么做：它的 CLIP 走 CLIPLoader，接上
            # LoraLoader 的 clip 输出会让文本编码绕一圈，且目标节点没有 clip 槽。
            for nid, nd in workflow.items():
                if nd.get("class_type") == "CLIPTextEncode" and "clip" in nd.get("inputs", {}):
                    nd["inputs"]["clip"] = [target, 1]
    return new_ids


def update_workflow(skill="anima_clear", ops=None):
    data = _load(skill)
    wf = data["workflow"]
    msgs = []
    ops = ops or []

    ckpt_id, ckpt = _find_node(wf, CHECKPT)
    up_id, up = _find_node(wf, UPSCALE)
    gen_id, gen = _find_generator(wf)
    if not gen:
        return "错误: 工作流里找不到采样生成节点"

    lora_specs = _lora_chain(wf, gen_id)  # 维护一个可变链

    for op in ops:
        kind = str(op.get("op", "")).strip().lower()
        if kind in ("set", "set_param", "set_parameter", "set_param"):
            r = _apply_set(wf, gen, op)
            if r is not None:
                msgs.append("已设 " + r)
            else:
                msgs.append("跳过未知参数 " + str(op.get("parameter")))
        elif kind == "set_model":
            v = op.get("value")
            if ckpt and v:
                ckpt["inputs"]["ckpt_name"] = str(v); msgs.append("底模=" + str(v))
            else:
                msgs.append("无模型节点可改")
        elif kind == "set_upscale":
            v = op.get("value")
            if up and v:
                up["inputs"]["model_name"] = str(v); msgs.append("放大模型=" + str(v))
            else:
                msgs.append("无放大节点可改")
        elif kind == "add_lora":
            name = op.get("name")
            if name:
                lora_specs.append({
                    "name": name,
                    "strength_model": op.get("strength_model", 1),
                    "strength_clip": op.get("strength_clip", 1),
                })
                msgs.append("追加 LoRA " + str(name))
            else:
                msgs.append("add_lora 缺 name")
        elif kind in ("remove_lora", "del_lora"):
            name = op.get("name")
            index = op.get("index")
            target_idx = None
            if name is not None:
                target_idx = next((i for i, l in enumerate(lora_specs) if l["name"] == name), None)
            elif index is not None:
                target_idx = int(index)
            if target_idx is not None and 0 <= target_idx < len(lora_specs):
                removed = lora_specs.pop(target_idx)
                msgs.append("移除 LoRA " + str(removed["name"]))
            else:
                msgs.append("remove_lora 找不到目标")
        elif kind == "set_lora_strength":
            name = op.get("name")
            for l in lora_specs:
                if l["name"] == name:
                    if "strength_model" in op:
                        l["strength_model"] = op["strength_model"]
                    if "strength_clip" in op:
                        l["strength_clip"] = op["strength_clip"]
                    msgs.append("调整 LoRA " + str(name) + " 强度")
                    break
            else:
                msgs.append("set_lora_strength 找不到 " + str(name))
        else:
            msgs.append("未知操作 " + str(kind))

    _rebuild_lora(wf, lora_specs)
    _save(wf, data)
    return ("已更新工作流:\n" + "\n".join(msgs) +
            "\n\n" + _build_summary(wf))


# ─── 工具描述 ─────────────────────────────────────────

tool = {
    "name": "get_workflow",
    "description": "查看当前 ComfyUI 生成工作流（skill 模板）：底模模型、LoRA 链及强度、采样器/scheduler/steps/cfg/denoise、放大模型，"
                  "以及 ComfyUI 当前可用的采样器/调度器/底模/LoRA/放大模型清单。用户要求调整画风/光影/清晰度等之前，先调用本工具了解现状。",
    "function": get_workflow,
    "parameters": {
        "type": "object",
        "properties": {
            "skill": {"type": "string", "description": "skill 名。默认 anima_clear（16 个「画风 × 尺寸档」渠道之一；**它不是默认生图渠道**，默认生图渠道是 silver）。本工具按**两段采样**结构写：2 个 KSampler（一段建构 + 二段精修，中间夹 LatentUpscaleBy），LoRA 只挂第一段、用 LoraLoaderModelOnly。要改别的渠道（anima_soft / anima_gloss / anima_curvy / hd_fast_clear / hd_2_clear / hd_3_clear，或带 _<画风> 后缀的其它档）先传 skill 名 get_workflow 看清结构再动手——它们节点编号不同，除 anima_clear（只有一块底模）外其余都是两块底模。"}
        },
        "required": []
    }
}

tool_update = {
    "name": "update_workflow",
    "description": "调整当前 ComfyUI 生成工作流的参数并写回模板。ops 为操作列表，每项含 op 字段："
                  "{'op':'set','parameter':'steps','value':40} 设置参数(steps/cfg/denoise/width/height/sampler/scheduler/hires_width/hires_height/hires_denoise/hires_steps/seed)；"
                  "{'op':'set_model','value':'xx.safetensors'} 换底模；"
                  "{'op':'set_upscale','value':'yy.pth'} 换放大模型；"
                  "{'op':'add_lora','name':'xx.safetensors','strength_model':0.8,'strength_clip':1} 追加LoRA；"
                  "{'op':'remove_lora','name':'xx'|'index':2} 移除LoRA；"
                  "{'op':'set_lora_strength','name':'xx','strength_model':0.6,'strength_clip':1} 调LoRA强度。"
                  "改完返回最新工作流摘要。",
    "function": update_workflow,
    "parameters": {
        "type": "object",
        "properties": {
            "skill": {"type": "string", "description": "skill 名。默认 anima_clear（16 个「画风 × 尺寸档」渠道之一；**它不是默认生图渠道**，默认生图渠道是 silver）。本工具按**两段采样**结构写：2 个 KSampler（一段建构 + 二段精修，中间夹 LatentUpscaleBy），LoRA 只挂第一段、用 LoraLoaderModelOnly。要改别的渠道（anima_soft / anima_gloss / anima_curvy / hd_fast_clear / hd_2_clear / hd_3_clear，或带 _<画风> 后缀的其它档）先传 skill 名 get_workflow 看清结构再动手——它们节点编号不同，除 anima_clear（只有一块底模）外其余都是两块底模。"},
            "ops": {
                "type": "array",
                "description": "要执行的操作列表（每条可不同 op）",
                "items": {"type": "object"}
            }
        },
        "required": ["ops"]
    }
}