"""ComfyUI 生图工具"""

import os

import requests
import json
import random
from flask import request
from app import comfy_src, image_jobs
from app.cancel import Cancelled, is_cancelled
from app.config import COMFYUI_URL, QQ_AGENT_ID
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

    # 排进**全局串行队列**：同一时刻 ComfyUI 里最多只有一张图在跑，其余老老
    # 实实排队（见 image_jobs）。从前是这里直接 _queue_prompt 提交、排队发生
    # 在 ComfyUI 内部——agent 侧看不见也管不着，多个会话并发时 N×2 张一起灌
    # 进去，显存瞬间见底。会话身份在这一刻快照下来：worker 线程读不到 qq_api
    # 的线程本地上下文。
    from app import qq_api
    target, target_id = qq_api.current_context()
    job, reason = image_jobs.enqueue(target, target_id, workflow)
    if reason is not None:
        # 拒收时工作流还在手上，ComfyUI 一点算力都没浪费，也不会留下「画了
        # 却没人发」的孤儿图。
        return reason

    if target is not None:
        # 提交完立刻返回，图由 worker 画好后自己发回原群。留在这儿同步等会把
        # 适配层的并发槽（默认 2 个）占住几分钟——文本回复和别的群都得陪着等
        # 显卡。
        ahead = image_jobs.ahead_of(job)
        if ahead > 0:
            return ("已经排上队了（前面还有 %d 张），排到就画，"
                    "画好会自动发到群里。"
                    "不要输出图片地址，也不要说「图在下面 / 稍等」，"
                    "直接把想说的话说完就行。" % ahead)
        return ("已经在画了，画好会自动发到群里。"
                + ("垫的是%s。" % source_note if source_note else "")
                + "不要输出图片地址，也不要说「图在下面 / 稍等」，"
                "直接把想说的话说完就行。")

    try:
        # 网页端：等到「排队 + 出图」全程。任务被超时中断时 image_jobs 会把
        # TimeoutError 挂到 job.error 上，这里接住当普通工具失败转述。
        history_entry = job.wait()
    except Cancelled:
        # 不把 Cancelled 抛给 execute_tool：那会被描述成"工具执行失败"，
        # 让模型以为工具坏了。中断是一个正常结局，说清楚就行。
        return "已中断：用户取消了等待。图片可能仍在后台生成，可到 ComfyUI 界面查看。"
    except TimeoutError as e:
        return "错误: " + str(e) + "，这张已经中断，换个提示词或稍后再试。"
    except Exception as e:
        # 其余失败（提交不上去、跑完了没图、ComfyUI 崩了）照样只回一句错话：
        # 直接冒到 execute_tool 会被描述成「工具坏了」，模型就该开始编了。
        return "错误: " + str(e)
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
                  "【图生图】**判据只看用户文字里有没有「要改这张图」的意图，"
                  "光给了图不算。** 只有用户明确说要改这张图时才传 source_image"
                  "（**必须给值才算图生图**），比如「图生图 / 垫图 / 照着这张改 / "
                  "把X换成Y / 保留构图只改颜色」。**反过来的一律不要传**："
                  "「看一下这张图的特征 / 提取特征 / 复刻一张 / 参考这个风格」"
                  "都是**看图 → 你写提示词 → 文生图**，它拿到的图自己看得见。"
                  "**只有 anima 和 image_gen_v1 支持**，krea2 传了会报错。"
                  "网页端填图片链接或本地路径；QQ 会话里填 1 = 对方引用的那张图"
                  "（引用里有多张就填 2、3），**没引用就取不到，报错照原话转述即可**。"
                  "此时 prompt 写「要变成什么样」，源图的构图自动保留。"
                  "默认重绘强度 0.6：用户说「改动大一点 / 换个画风」传 denoise 0.8~0.9，"
                  "说「只微调 / 保留原图」传 denoise 0.35~0.45，没提就别传 denoise。",
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
            "【图生图】**判据只看对方文字里有没有「要改这张图」的意图——"
            "不看有没有图，也不看有没有引用。** 只有对方明确说要改这张图时才传 "
            "source_image=1（**必须给值才算图生图**），比如「图生图 / 垫图 / "
            "照着这张改 / 用这张改成X / 保留构图只改颜色」。"
            "**反过来的一律不要传**：「看一下这张图的特征 / 提取特征 / 复刻一张 / "
            "参考这个风格 / 照着画一张新的 / 用 qwen 生成一个…」这些都是"
            "**看图 → 你写提示词 → 文生图**（引用图你看得见，照它写 prompt 就行），"
            "传了 source_image 就是画错东西。"
            "**只有 anima 和 image_gen_v1 支持图生图**；对方点名的 skill 不支持"
            "（krea2 / qwen_image_v1）就直接告诉他这个渠道吃不了垫图，"
            "**别偷偷换 skill**。源图只能来自对方**引用的**那条消息：填 1 就是"
            "引用里第一张（多张填 2、3），不要填链接或路径。"
            "说了要改图但没引用 → 工具会回「没看到引用的图片」，"
            "**原话转述让他引用那条图再说一次**，不要自己编一张，也不要改成文生图。"
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
            "source_image": {"type": "string", "description": "图生图的源图，**必须给值才算图生图**。**没明确说要「改这张图」就不要传**——「看特征 / 复刻 / 参考这个风格」都是文生图（图你自己看得见），传了就是画错东西。只在用户明确说「图生图 / 垫图 / 照着这张改」时才传。QQ 会话：1 = 对方引用的那张图（引用里有多张就填 2、3）；对方没引用会取不到，工具报错后照原话转述即可。网页端：填图片链接或本地路径。仅 anima / image_gen_v1 支持"},
            "denoise": {"type": "string", "description": "可选。图生图的重绘强度 0.05~1.0，不填默认 0.6（越大越自由、越小越贴原图）。只在图生图时有效，用户没提就别传"}
        },
        "required": ["prompt"]
    }
}
