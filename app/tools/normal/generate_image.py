"""ComfyUI 生图工具"""

import os

import requests
import json
import time
import random
import uuid
from flask import request
from app import comfy_src, image_jobs
from app.cancel import Cancelled, is_cancelled
from app.config import COMFYUI_URL, IMAGE_GEN_TIMEOUT, QQ_AGENT_ID
from app.skills import load_skill, load_workflow


# 支持图生图的 skill：只有这两个各配了一份 workflow_i2i.json。krea2 的工作流
# 结构不同（单一 unet + 一个 lora 槽），传了 source_image 直接拒，绝不静默
# 退化成文生图——用户以为在改自己那张图，实际拿到凭空画的一张。
_I2I_SKILLS = ("anima", "image_gen_v1")

# 图生图默认重绘强度：0.6 落在「构图保留、画风明显换掉」的位置（本机 krea2
# 的图生图用 0.55，同一量级）。模型可按用户的话上下调。
I2I_DEFAULT_DENOISE = 0.6


# ─── QQ 侧生图开关 ───────────────────────────────────

def _qq_gate():
    """QQ 会话里的生图总闸 + 单群闸；非 QQ 会话（网页端）不受限。

    靠 qq_api 的线程本地绑定知道「此刻在为哪个 QQ 会话服务」。管理页
    关掉后下一轮就生效（settings.json 走 mtime 缓存），不用重启。拒绝
    时返回一句模型能转述的话，而不是抛错——让它正常回话「生图被关了」，
    别让整轮变成工具执行失败。
    """
    from app import qq_api
    from app.agents import image_gen_allowed
    target, target_id = qq_api.current_context()
    if target is None:
        return None
    ok, why = image_gen_allowed(QQ_AGENT_ID, target, target_id)
    return None if ok else ("错误：" + why
                            + "，本次不生成图片。别再重试，"
                              "直接告诉对方现在画不了。")


# ─── ComfyUI 内部函数 ────────────────────────────────

def _queue_prompt(workflow):
    resp = requests.post(COMFYUI_URL + "/prompt", json={
        "prompt": workflow,
        "client_id": "agent_" + str(uuid.uuid4())[:8]
    }, timeout=30)
    resp.raise_for_status()
    return resp.json()["prompt_id"]


def _wait_for_completion(prompt_id, timeout=IMAGE_GEN_TIMEOUT):
    """轮询等待 ComfyUI 出图。批量生图逐张串行，故超时给得较宽（见 config）。

    每次轮询检查一次中断信号：用户点「停止」时立刻放弃等待，让 agent 循环
    尽快收尾。**这里不去调 ComfyUI 的 /interrupt** —— 它中断的是「当前正在
    执行」的任务，如果那一刻恰好是用户自己在界面上排的图，会被一起取消。
    放弃等待更安全：那张图会在后台照常跑完，只是不再有人等它。
    """
    start = time.time()
    while time.time() - start < timeout:
        # 放在 try 之外：下面的 except 是裸的，包进去会被它吞掉
        if is_cancelled():
            raise Cancelled("用户中断了等待")
        # 轮询本身交给 image_jobs：后台投递线程用的是同一份实现，两处各写
        # 一遍迟早会长歪（一边改了超时、另一边没改）。
        entry = image_jobs.poll_once(prompt_id)
        if entry is not None:
            return entry
        time.sleep(2)
    raise TimeoutError("生成超时 (" + str(timeout) + "s)")


# ─── 工具函数 ────────────────────────────────────────

def _parse_loras(lora_str):
    """「名字:强度,名字:强度」-> [(name, strength), ...]。

    格式刻意从简（小模型要写得出来）：强度一个数同时给 model 和 clip。
    写错抛 ValueError，消息里带上原因，让模型能自己纠正。
    """
    specs = []
    for part in lora_str.split(","):
        part = part.strip()
        if not part:
            continue
        name, sep, raw = part.rpartition(":")
        if not sep or not name.strip():
            raise ValueError("lora 参数格式应为「文件名:强度」：" + part)
        try:
            strength = float(raw)
        except ValueError:
            raise ValueError("lora 强度要是数字：" + part)
        specs.append((name.strip(), strength))
    if not specs:
        raise ValueError("lora 参数是空的")
    return specs


def _lora_chain(workflow):
    """按 checkpoint→lora 链的顺序返回 lora 节点 id 列表。

    兼容 LoraLoader（带 clip）和 LoraLoaderModelOnly（krea2 用的，只挂 model）。
    不硬编码节点 id（两个工作流的 id 编号不同），沿 model 输入的连线走：
    第一个槽的 model 来自 checkpoint 加载节点，后面每个槽的 model 来自前一个槽。
    """
    loaders = {nid: node for nid, node in workflow.items()
               if node.get("class_type") in ("LoraLoader",
                                             "LoraLoaderModelOnly")}
    sources = {}
    for nid, node in loaders.items():
        src = (node.get("inputs") or {}).get("model")
        sources[nid] = src[0] if isinstance(src, list) and src else None
    ckpts = {nid for nid, node in workflow.items()
             if "CheckpointLoader" in (node.get("class_type") or "")
             or "UnetLoader" in (node.get("class_type") or "")}
    chain, current = [], next(
        (nid for nid, src in sources.items() if src in ckpts), None)
    while current is not None and current not in chain:
        chain.append(current)
        current = next((nid for nid, src in sources.items()
                        if src == current and nid not in chain), None)
    return chain


def _available_loras():
    """从 ComfyUI 实时拉 lora 清单；拿不到返回 None（不拦截，交给 ComfyUI 自己拒）。

    清单不塞进工具描述——几十个文件名每轮都发不值当，只在写错时才拿来救场。
    """
    try:
        resp = requests.get(COMFYUI_URL + "/object_info/LoraLoader", timeout=10)
        resp.raise_for_status()
        return list(resp.json()["LoraLoader"]["input"]["required"]["lora_name"][0])
    except Exception:
        return None


def _apply_loras(workflow, lora_str):
    """把 AI 指定的 lora 填进槽位。传了就**完全接管**：没填满的槽强度归零，
    工作流里默认那组 lora 不再掺和——避免「指定了角色 lora 但饱和度修正
    还在捣乱」的混搭怪相。出错返回模型能转述的一句话，成功返回 None。
    """
    try:
        specs = _parse_loras(lora_str)
    except ValueError as e:
        return str(e)
    names = _available_loras()
    if names:
        bad = [n for n, _ in specs if n not in names]
        if bad:
            return ("错误: 这些 lora 不存在: " + ", ".join(bad)
                    + "。可用 lora: " + ", ".join(names))
    chain = _lora_chain(workflow)
    if not chain:
        return "错误: 当前工作流没有 lora 槽，去掉 lora 参数用默认的画就行"
    specs = specs[:len(chain)]          # 传多了按槽位截断，不报错
    for i, nid in enumerate(chain):
        inputs = workflow[nid]["inputs"]
        model_only = workflow[nid]["class_type"] == "LoraLoaderModelOnly"
        if i < len(specs):
            name, strength = specs[i]
            inputs["lora_name"] = name
            inputs["strength_model"] = strength
            if not model_only:
                inputs["strength_clip"] = strength
        else:
            # 闲槽等效关闭：强度归零（ModelOnly 没有 clip 输入，别塞进去，
            # 否则 ComfyUI 会报未知输入）
            inputs["strength_model"] = 0.0
            if not model_only:
                inputs["strength_clip"] = 0.0
    return None


def _denoise_value(raw):
    """把 denoise 参数变成写进工作流的数值字符串。

    不填就用默认值；填了必须落在一个说得过去的位置——0 等于原图不动、
    低于 0.2 基本看不出改动，都是白白占一次显卡，不如让模型把话说清楚。
    """
    if raw is None or str(raw).strip() == "":
        return "%.2f" % I2I_DEFAULT_DENOISE
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise ValueError("denoise 要填数字（0.05~1.0），收到：" + str(raw))
    if not 0.05 <= value <= 1.0:
        raise ValueError("denoise 要落在 0.05~1.0 之间，收到：" + str(raw))
    return "%.2f" % value


def _generate_image(prompt, skill="anima", use_character=False, lora=None,
                    source_image="", denoise=None):
    # 提交前先看一眼：已经中断就别再往 ComfyUI 队列里塞新任务了
    if is_cancelled():
        return "已中断：用户取消了本次生成。"

    gate = _qq_gate()
    if gate is not None:
        return gate

    # QQ 会话强制无底模：QQ 的工具描述里根本没有角色选项，就算模型
    # 手滑传了 use_character=true 也不生效——角色描述只在网页端可见。
    from app import qq_api
    if qq_api.current_context()[0] is not None:
        use_character = False

    skill_data = load_skill(skill)
    if not skill_data or not skill_data["workflow"]:
        return "错误: 找不到 Skill '" + skill + "'"

    # 图生图：给了源图就换成垫图工作流，并先把源图送进 ComfyUI 的 input
    # 目录。取图 / 缩放 / 上传任何一步失败都当场返回，**不退回文生图**。
    workflow = skill_data["workflow"]
    is_i2i = bool(str(source_image or "").strip())
    source_note, denoise_txt, uploaded = "", "", ""
    if is_i2i:
        if skill not in _I2I_SKILLS:
            return ("错误: 只有 anima 和 image_gen_v1 支持图生图，" + skill
                    + " 不行。去掉 source_image，按文生图重来。")
        try:
            denoise_txt = _denoise_value(denoise)
        except ValueError as e:
            return "错误: " + str(e)
        i2i = load_workflow(
            os.path.join(skill_data["path"], "workflow_i2i.json"))
        if not i2i:
            return "错误: Skill '" + skill + "' 没有图生图工作流"
        try:
            raw, source_note = comfy_src.resolve(source_image)
            fitted, _size = comfy_src.fit(raw)
            uploaded = comfy_src.upload(fitted)
        except RuntimeError as e:
            return str(e)
        workflow = i2i

    workflow_str = json.dumps(workflow)

    # 替换占位符
    seed = random.randint(1, 2**32 - 1)
    # 是否用 skill 底模：默认用；若关闭则用中性占位（不吃角色本体）
    character = skill_data.get("character", "")
    if not use_character:
        character = ""
    # 转义 character 里的换行和特殊字符
    character_escaped = character.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n').replace('\r', '')
    # __MULTI_PROMPTS__ 可以独占一个 JSON 字符串（image_gen_v1），也可以
    # 嵌在更大字符串里（krea2 的 node4 = "Yoneyama Mai Style, __MULTI_PROMPTS__"，
    # 工作流自带固定风格前缀）。统一按「字符串内部转义替换」处理，两种都兼容。
    prompt_escaped = prompt.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n').replace('\r', '')
    workflow_str = workflow_str.replace("__MULTI_PROMPTS__", prompt_escaped)
    workflow_str = workflow_str.replace("__SEED__", str(seed))
    workflow_str = workflow_str.replace("__CHARACTER__", character_escaped)
    if is_i2i:
        # 垫图专用占位符：源图文件名（按 JSON 字符串转义填，避免文件名里的
        # 引号把 JSON 打破）与重绘强度。这两个只在 workflow_i2i.json 里出现。
        workflow_str = workflow_str.replace('"__SOURCE_IMAGE__"',
                                            json.dumps(uploaded))
        workflow_str = workflow_str.replace('"__DENOISE__"', denoise_txt)

    workflow = json.loads(workflow_str)

    # 用户点名换 lora 才走这段；不传 lora 时一行替换逻辑都不执行，
    # 工作流原样提交，跟从前完全一样。
    if lora:
        err = _apply_loras(workflow, lora)
        if err:
            return err

    # 提交到 ComfyUI
    prompt_id = _queue_prompt(workflow)

    # QQ 会话：提交完立刻返回，图由后台线程画好后自己发回原群（见
    # image_jobs）。留在这儿同步等会把适配层的并发槽（默认 2 个）占住几分钟
    # ——文本回复和别的群都得陪着等显卡。会话身份在提交这一刻快照下来。
    from app import qq_api
    target, target_id = qq_api.current_context()
    if target is not None:
        ok, pending = image_jobs.submit(target, target_id, prompt_id)
        if not ok:
            return ("这个会话已经排着 %d 张了，画完这些再说。"
                    "不要跟对方提这张图，当没画过，接着把话说完。" % pending)
        return ("已经在画了，画好会自动发到群里。"
                + ("垫的是%s。" % source_note if source_note else "")
                + "不要输出图片地址，也不要说「图在下面 / 稍等」，"
                "直接把想说的话说完就行。")

    try:
        history_entry = _wait_for_completion(prompt_id)
    except Cancelled:
        # 不把 Cancelled 抛给 execute_tool：那会被描述成"工具执行失败"，
        # 让模型以为工具坏了。中断是一个正常结局，说清楚就行。
        return "已中断：用户取消了等待。图片可能仍在后台生成，可到 ComfyUI 界面查看。"
    images = image_jobs.output_images(history_entry)

    if not images:
        return "错误: 生成完成但未找到输出图片"

    # 用相对路径（不带 host）：任何端(手机/平板/PC)访问时都用当前站点 origin 加载
    urls = ["/api/image/" + img for img in images]

    return ("生成成功！seed: " + str(seed)
            + ("（垫图：%s）" % source_note if source_note else "")
            + "\n图片地址:\n" + "\n".join(urls))


tool = {
    "name": "generate_image",
    "description": "调用 ComfyUI 生成图片。"
                  "【默认 Skill】anima（Anima 2B 动漫模型，双段精修，一次一张）—— 不传 skill 就用它，"
                  "prompt 只写一段画面描述，**不要用 --- 分隔**。"
                  "【换渠道】仅当用户点名或明确需要时才换：说 krea2（如「用 krea2」）传 skill=krea2"
                  "（Krea2 Turbo + retroanime lora，一次一张）；要一次出多张（多个提示词用 --- 分隔）"
                  "或要用固定角色底模时传 skill=image_gen_v1；用户要**在画面里写出文字（尤其中文）**、"
                  "要**写实照片感**、或点名 qwen / 通义时传 skill=qwen_image_v1"
                  "（Qwen-Image 2.1，此时提示词改写自然语言句子、不要写标签，一次一张、约 100 秒）。"
                  "【底模】除 image_gen_v1 外都没有固定角色，你在 prompt 中自己写出完整角色提示词"
                  "(发型/发色/体型/服装/年龄等)；只有 image_gen_v1 配 use_character=true 时用它的固定角色。"
                  "【lora】用户点名要换 lora 时才传 lora 参数，平时不要传。格式「文件名:强度」，"
                  "多个逗号分隔（如 \"x.safetensors:0.8,y.safetensors:0.5\"）；文件名要完整"
                  "(.safetensors 结尾)，写错会返回可用清单；传了就完全接管本次的 lora，"
                  "槽位 image_gen_v1 3 个 / anima 2 个 / krea2 1 个，没填满的槽自动关闭。"
                  "【图生图】用户给了图、并明确说「图生图 / 垫图 / 照着这张改」时才传 "
                  "source_image（**必须给值才算图生图**）——**只有 anima 和 image_gen_v1 "
                  "支持**，krea2 传了会报错。网页端填图片链接或本地路径；QQ 会话里填 "
                  "1 = 对方引用的那张图（引用里有多张就填 2、3），**没引用就取不到，"
                  "报错照原话转述即可**。此时 prompt 写「要变成什么样」，"
                  "源图的构图自动保留。默认重绘强度 0.6：用户说「改动大一点 / 换个画风」"
                  "传 denoise 0.8~0.9，说「只微调 / 保留原图」传 denoise 0.35~0.45，"
                  "没提就别传 denoise。",
    # QQ 机器人看不到角色底模这套：Sumire 的角色描述只给网页端用。
    "description_overrides": {
        QQ_AGENT_ID:
            "调用 ComfyUI 生成图片。"
            "【默认 Skill】anima（Anima 2B 动漫模型，双段精修，一次一张）—— 不传 skill 就用它，"
            "prompt 只写一段画面描述，**不要用 --- 分隔**。"
            "【换渠道】仅当用户点名或明确需要时才换：krea2 传 skill=krea2；"
            "要一次出多张时传 skill=image_gen_v1（多个提示词用 --- 分隔）；"
            "对方要**画面里写出文字（尤其中文）**、要写实照片感、或点名 qwen 时传 "
            "skill=qwen_image_v1（此时提示词改写自然语言句子、不要写标签，一次一张、约 100 秒）。"
            "【lora】用户点名要换 lora 时才传 lora 参数，平时不要传。格式「文件名:强度」，"
            "多个逗号分隔（如 \"x.safetensors:0.8\"）；文件名要完整(.safetensors 结尾)，"
            "写错会返回可用清单；最多 3 个，传了就完全接管本次的 lora。"
            "【图生图】对方明确说「图生图 / 垫图 / 照着这张改」时传 source_image=1"
            "（**必须给值才算图生图**），**只有 anima 和 image_gen_v1 支持**。"
            "源图**只能来自对方引用的那条消息**：对方引用一张带图的消息、@你、"
            "说要什么效果，填 1 就是引用那张（一条消息里有多张图就填 2、3）。"
            "对方没引用、只是自己发了图的话，也照样传 source_image=1——工具会回"
            "一句提示，你**原话转述**给对方（让他引用那张图再说一次），"
            "不要自己编一张，也不要改成文生图。不要填链接或路径。"
            "垫图时 prompt 写「要变成什么样」，原图构图自动保留。默认重绘强度 0.6："
            "对方说「改动大一点 / 换个画风」传 denoise 0.8~0.9，说「只微调 / 保留原图」"
            "传 denoise 0.35~0.45，没提就别传。",
    },
    "hidden_params": {QQ_AGENT_ID: ["use_character"]},
    "function": _generate_image,
    "parameters": {
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "提示词。默认（anima / krea2 / image_gen_v1）写逗号分隔的标签式英文短句，只写一段、不要用 --- 分隔（只有 skill=image_gen_v1 时才用 --- 分隔多张）；**skill=qwen_image_v1 时改写自然语言完整句子**（不写 masterpiece 这类标签）。画面里没有固定角色时须包含完整角色描述。图生图时写「要变成什么样」（目标画面），不用再描述源图里已有的构图"},
            "skill": {"type": "string", "description": "Skill名称，默认anima（不传就用它）。可选值见系统提示 Available Skills 里标 [底模]/[无底模] 的生图类；krea2 / image_gen_v1 / qwen_image_v1 仅在用户点名或场景匹配时才用（qwen_image_v1 用于画面内写字、写实照片感）"},
            "use_character": {"type": "boolean", "description": "是否使用该Skill自带的角色描述（默认false）。只有 image_gen_v1 有角色底模，设为true时固定该角色，你只写动作/环境/构图"},
            "lora": {"type": "string", "description": "可选。「文件名:强度」逗号分隔，如 x.safetensors:0.8,y.safetensors:0.5。仅在用户点名要换 lora 时传"},
            "source_image": {"type": "string", "description": "图生图的源图，**必须给值才算图生图**。QQ 会话：1 = 对方引用的那张图（引用里有多张就填 2、3）；对方没引用会取不到，工具报错后照原话转述即可。网页端：填图片链接或本地路径。仅 anima / image_gen_v1 支持，且只在用户明确说「图生图 / 垫图 / 照着这张改」时才传"},
            "denoise": {"type": "string", "description": "可选。图生图的重绘强度 0.05~1.0，不填默认 0.6（越大越自由、越小越贴原图）。只在图生图时有效，用户没提就别传"}
        },
        "required": ["prompt"]
    }
}
