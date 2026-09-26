"""
ComfyUI 工作流查看/调整工具

「当前工作流」= skill 的 workflow.json 模板（generate_image 实际生成用的那份）。
- get_workflow  : 让 AI「看到」当前工作流（模型 / LoRA链 / 采样参数 / hires / 放大），并可附可用资源清单
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
LORA = "LoraLoader"
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
    return None, None


def _lora_chain(workflow, gen_id):
    """回溯出当前 LoRA 链（按加载顺序，即靠近底模的在前）:
    [{name, strength_model, strength_clip}, ...]"""
    lora_by_id = {nid: nd for nid, nd in workflow.items() if nd.get("class_type") == LORA}
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
    for k in ("seed", "steps", "cfg", "denoise", "width", "height",
              "sampler_name", "scheduler",
              "hires_width", "hires_height", "hires_denoise", "hires_steps"):
        if k in gen_inp:
            params[k] = gen_inp[k]

    chain = _lora_chain(workflow, gen_id)

    lines = ["## 当前工作流生成配置"]
    lines.append("- 底模模型: " + str(ckpt["inputs"].get("ckpt_name")) if ckpt else "- 底模模型: 无")
    if chain:
        lines.append("- LoRA 链(" + str(len(chain)) + "):")
        for i, l in enumerate(chain, 1):
            lines.append(f"  {i}. {l['name']}  strength_model={l['strength_model']} strength_clip={l['strength_clip']}")
    else:
        lines.append("- LoRA: 无")
    if params:
        lines.append("- 采样参数: " + ", ".join(f"{k}={v}" for k, v in params.items()))
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

def get_workflow(skill="image_gen_v1"):
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
    gen["inputs"][k] = v
    return k + "=" + repr(v)


def _rebuild_lora(workflow, specs):
    """按 spec 顺序重建 LoRA 链并重连 generator / 负向CLIP文案 """
    ckpt_id, _ = _find_node(workflow, CHECKPT)
    gen_id, gen = _find_generator(workflow)
    used = sorted(int(x) for x in workflow if str(x).lstrip('-').isdigit())
    next_id = (used[-1] + 1) if used else 1

    # 删除所有旧 LoraLoader
    for nid in list(workflow.keys()):
        if workflow[nid].get("class_type") == LORA:
            del workflow[nid]

    target = ckpt_id
    new_ids = []
    for spec in specs:
        nid = str(next_id); next_id += 1
        workflow[nid] = {
            "class_type": LORA,
            "inputs": {
                "model": [target, 0],
                "clip": [target, 1],
                "lora_name": str(spec.get("name", "")),
                "strength_model": spec.get("strength_model", 1),
                "strength_clip": spec.get("strength_clip", 1),
            },
        }
        target = nid
        new_ids.append(nid)

    if gen_id:
        gen["inputs"]["model"] = [target, 0]
        gen["inputs"]["clip"] = [target, 1]
        # 负向 CLIPTextEncode 的 clip 也挂到最后一级 LoRA
        for nid, nd in workflow.items():
            if nd.get("class_type") == "CLIPTextEncode" and "clip" in nd.get("inputs", {}):
                nd["inputs"]["clip"] = [target, 1]
    return new_ids


def update_workflow(skill="image_gen_v1", ops=None):
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
    "description": "查看当前 ComfyUI 生成工作流（skill 模板）：底模模型、LoRA 链及强度、采样器/scheduler/steps/cfg/denoise、hires、放大模型，"
                  "以及 ComfyUI 当前可用的采样器/调度器/底模/LoRA/放大模型清单。用户要求调整画风/光影/清晰度等之前，先调用本工具了解现状。",
    "function": get_workflow,
    "parameters": {
        "type": "object",
        "properties": {
            "skill": {"type": "string", "description": "skill 名。默认 image_gen_v1（本工具调参逻辑按它的标准单链结构写；anima 是双段结构、krea2 只有 ModelOnly lora，改这两个要先传对应 skill 名 get_workflow 看清结构再动手）"}
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
            "skill": {"type": "string", "description": "skill 名。默认 image_gen_v1（本工具调参逻辑按它的标准单链结构写；anima 是双段结构、krea2 只有 ModelOnly lora，改这两个要先传对应 skill 名 get_workflow 看清结构再动手）"},
            "ops": {
                "type": "array",
                "description": "要执行的操作列表（每条可不同 op）",
                "items": {"type": "object"}
            }
        },
        "required": ["ops"]
    }
}