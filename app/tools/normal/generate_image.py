"""ComfyUI 生图工具"""

import os

import requests
import json
import random
from flask import request
from app import comfy_src, image_jobs
from app import nai as nai_mod
from app.cancel import Cancelled, is_cancelled
from app.config import COMFYUI_URL, DISABLED_IMAGE_SKILLS, QQ_AGENT_ID
from app.skills import load_skill, load_workflow


# 支持图生图的 skill。**现在一个都没有——图生图整体停用**（2026-09-27 用户定）。
#
# 用户的原话：引用一张图只是「让 AI 看到这张图」，他要的是**看图 → 反推提示词 →
# 文生图**，而不是改图。但模型一看见引用图就往图生图上想，屡次跑偏。唯一的图生图
# 渠道（qwen_image_v1）本身也在这台机器上带不动。所以：**整条图生图链路停用**，
# 工具收不到这个能力，模型就不会再往那个方向想。
#
# 空元组是「停用」的表达方式，不是「没写完」：判据就是它。恢复时把 qwen 填回来
# （同时清掉 config.DISABLED_IMAGE_SKILLS）即可，下面的 i2i 分支一行都没删。
_I2I_SKILLS = ()

# 没点名 skill 时的图生图渠道。停用期间用不着，留着是为了恢复时一眼能看到
# 「当初走的是哪个渠道」。
I2I_DEFAULT_SKILL = "qwen_image_v1"

# 没点名 skill 时的文生图默认渠道。
#
# 2026-09-29 改回 "anima"：`skills/anima/` 已按**单底模单段**重建（Anima 2B，
# 768×1024，一次装载、稳定），它才是「不点名就走」的那条。双底模两段那版
# 拆成了 `skills/anima_2/`，**只在用户点名时才传**。
# 中间那段「anima 目录不存在、只能临时指 image_gen_v1」的历史，随目录补齐而结束。
T2I_DEFAULT_SKILL = "anima"


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

    起点识别用**小写包含**：ComfyUI 里同一个加载器有多种拼写——`CheckpointLoaderSimple`
    （image_gen_v1）、`UnetLoaderGGUF`（krea2）、`UNETLoader`（anima / anima_2）。
    原来写成 `"UnetLoader" in class_type` 是大小写敏感的，`UNETLoader` 全大写**匹配不上**，
    于是 anima 的两个 lora 槽整条链找不到，点名换 lora 会误报「当前工作流没有 lora 槽」。
    """
    loaders = {nid: node for nid, node in workflow.items()
               if node.get("class_type") in ("LoraLoader",
                                             "LoraLoaderModelOnly")}
    sources = {}
    for nid, node in loaders.items():
        src = (node.get("inputs") or {}).get("model")
        sources[nid] = src[0] if isinstance(src, list) and src else None
    ckpts = {nid for nid, node in workflow.items()
             if "checkpointloader" in (node.get("class_type") or "").lower()
             or "unetloader" in (node.get("class_type") or "").lower()}
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


def _generate_image(prompt, skill=None, use_character=False, lora=None,
                    source_image="", denoise=None):
    # denoise：垫图重绘（anima / image_gen_v1）下线后已经没有渠道消费它了。
    # 签名里留着只为兜住模型手滑传的参数——删掉的话 execute_tool 的
    # fn(**args) 会抛 TypeError，被包成「工具执行失败」，模型就以为工具坏了。

    # 提交前先看一眼：已经中断就别再往 ComfyUI 队列里塞新任务了
    if is_cancelled():
        return "已中断：用户取消了本次生成。"

    gate = _qq_gate()
    if gate is not None:
        return gate

    # NAI（NovelAI）云端生图：群主独立 token，与 ComfyUI 完全隔离。
    # 不碰下面那套 ComfyUI 探活 / 加载 / skill：它走自己的云分支（见
    # image_jobs._process_nai），token 只给指定群用（app/agents.nai_allowed）。
    # 必须在 ComfyUI 探活之前就分流，否则没开 ComfyUI 的机器会被卡在探活那句。
    # 图生图（垫图）与文生图共用同一套 NAI 闸：nai_allowed 不放行，
    # i2i 也一样进不来——不新增开关。
    if skill == "nai":
        from app import nai, qq_api
        from app.agents import nai_allowed
        target, target_id = qq_api.current_context()
        ok, why = nai_allowed(QQ_AGENT_ID, target, target_id)
        if not ok:
            return ("错误：" + why + "，本次不使用 NAI。"
                    "直接告诉对方现在用不了，别再重试。")
        if target is None:
            return "错误：NAI 仅支持 QQ 使用，网页端用不了。"
        # 垫图：只认本轮引用的图（comfy_src.resolve 的既有契约），取图失败
        # 就实话实说，绝不退回文生图——对方以为改的是自己那张，收到的却是
        # 凭空画的，比直接报错糟得多。base64 在这里算好快照进队列：
        # worker 线程读不到 qq_api 的线程本地上下文。
        nai_i2i = None
        if str(source_image or "").strip():
            try:
                raw, note = comfy_src.resolve(source_image)
                nai_i2i = {"image": nai.prepare_image(raw),
                           "strength": _nai_strength(denoise), "note": note}
            except RuntimeError as e:
                return str(e)
        return _enqueue_nai(prompt, target, target_id, nai_i2i)

    # 图生图整体停用（_I2I_SKILLS 为空）：只要模型还试着传 source_image，就在
    # 这里当场拦住，**并且把它拉回正路**——它十有八九是看到引用图就以为要「改图」，
    # 而用户要的其实是「看图 → 反推提示词 → 文生图」。所以这句拒收的关键不是
    # 「不行」，是「你该干嘛」：照常写 prompt 出一张新的。
    #
    # 拦在 skill 解析之前：这时候谁都还没碰 ComfyUI、没读 skill 文件，一次
    # 白跑都没有；而且模型传没传 skill 也无所谓——图生图这个动作本身已经不存在了。
    is_i2i = bool(str(source_image or "").strip())
    if is_i2i:
        return ("错误：不支持传 source_image（改图 / 图生图已停用）。"
                "**别跟对方解释技术原因，也别提这个参数名**，就说改不了图。"
                "引用一张图只是让**你看得见**它——你要做的是：**照它反推出提示词，"
                "用默认的 anima 重新画一张新的**（新图不是改它那张），"
                "或者对方只是让你看图 / 点评时就直接回话。")

    # 没点名 skill 时的默认渠道：文生图照旧 anima。execute_tool 是 fn(**args)，
    # 模型不传 skill 就落到这里的默认值 None——所以「没点名」和「点名了 anima」
    # 分得开。
    if not skill:
        skill = T2I_DEFAULT_SKILL

    # 停用渠道的硬闸（见 config.DISABLED_IMAGE_SKILLS）。
    #
    # 放在这里而不是靠「不进白名单」：白名单只管**提示词里列不列**，模型要是
    # 记得这个名字，照样能把 skill 传进来。而 qwen 的代价不是「画得慢」，是
    # **把整机拖崩**——这种事必须有一道代码级的闸，不能指望模型自觉。
    #
    # 文案讲究：不把渠道名当技术名词甩给对方（群里看到 qwen_image_v1 很奇怪）；
    # 同时给出去路，别让模型以为「生图坏了」。
    if skill in DISABLED_IMAGE_SKILLS:
        return ("错误：" + skill + " 这个渠道已经停用（这台机器带不动它）。"
                "**别跟对方提这个渠道名，也别解释原因**——对方只是要一张图的话，"
                "直接改用默认的 anima 重画（prompt 改写成 anima 的标签式英文写法）；"
                "对方点名要它，就照实说这个渠道现在用不了。")

    # QQ 会话强制无底模：QQ 的工具描述里根本没有角色选项，就算模型
    # 手滑传了 use_character=true 也不生效——角色描述只在网页端可见。
    from app import qq_api
    if qq_api.current_context()[0] is not None:
        use_character = False

    skill_data = load_skill(skill)
    if not skill_data or not skill_data["workflow"]:
        return "错误: 找不到 Skill '" + skill + "'"

    # 图生图：给了源图就换成图生图工作流，并先把源图送进 ComfyUI 的 input
    # 目录。取图 / 缩放 / 上传任何一步失败都当场返回，**不退回文生图**。
    #
    # **当前走不到这里**：上面已经对 source_image 一刀切拒收了（_I2I_SKILLS
    # 是空的）。整段保留是为了恢复时改两处即可：把 qwen 填回 _I2I_SKILLS，
    # 再删掉上面那个 `if is_i2i:` 的早退分支。
    workflow = skill_data["workflow"]
    source_note, denoise_txt, uploaded = "", "", ""
    if is_i2i:
        if skill not in _I2I_SKILLS:
            return ("错误: " + skill + " 不支持图生图。"
                    "去掉 source_image，按文生图重来。")
        # Qwen-Image 2.1 的图生图是「按指令改」：官方模板里 denoise 就是
        # 1.0，没有「保留多少原图」这个旋钮——要改多少，写在 prompt 里。
        # 模型传了 denoise 也不认，免得它以为调低就是「只微调」。
        denoise_txt = "1.00"
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
    # 连引号一起换掉：ComfyUI 的 KSampler.seed 是 INT 字段，只替内容、留着引号
    # 就变成字符串 "123456"，提交时类型校验不过。老写法是给自定义节点用的
    # （它对 seed 类型不敏感），换成标准 KSampler 后必须落成真数字。
    workflow_str = workflow_str.replace('"__SEED__"', str(seed))
    workflow_str = workflow_str.replace("__SEED__", str(seed))  # 兜底：裸占位符
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

    # 入队前先确认 ComfyUI 真的在。队列在 agent 侧，enqueue 从来不碰
    # ComfyUI，所以它挂了也照样「成功」，模型就会拿到一句「已经排上队了」
    # 去跟对方承诺，几十秒后 worker 才撞上连接失败——群里先看到承诺、再看
    # 到「图没画出来」，前后打架。探不到就当场拒掉，让模型老老实实说画不了。
    if not image_jobs.comfy_alive():
        return ("错误：ComfyUI 现在没在线（" + COMFYUI_URL + " 连不上），"
                "这张画不了。直接告诉对方现在画不了、让他稍后再试，"
                "不要说图已经在画了或者马上就好。")

    # 排进**全局串行队列**：同一时刻 ComfyUI 里最多只有一张图在跑，其余老老
    # 实实排队（见 image_jobs）。从前是这里直接 _queue_prompt 提交、排队发生
    # 在 ComfyUI 内部——agent 侧看不见也管不着，多个会话并发时 N×2 张一起灌
    # 进去，显存瞬间见底。会话身份在这一刻快照下来：worker 线程读不到 qq_api
    # 的线程本地上下文。
    from app import qq_api
    target, target_id = qq_api.current_context()
    job, reason = image_jobs.enqueue(target, target_id, workflow, skill)
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


def _nai_strength(denoise):
    """denoise 参数 → NAI 的 strength（重绘噪声）。不传/写错用默认 0.7，
    越界钳回 [0.1, 0.9]——垫图不会「完全不变」也不会「完全看不出原图」。"""
    try:
        s = float(denoise)
    except (TypeError, ValueError):
        return nai_mod.NAI_I2I_STRENGTH
    return min(0.9, max(0.1, s))


def _enqueue_nai(prompt, target, target_id, nai_i2i=None):
    """把一张 NAI 图排进全局串行队列（复用现有队列，见 image_jobs）。

    NAI 是云端调用，也占「这一轮」的并发，跟 ComfyUI 的图混在同一条队列里
    排队不会更慢，还能让对方看到「前面还有几张」。enqueue 的 workflow 字段
    在这里塞的是 prompt 字符串——cloud 分支靠 skill 判断怎么用它；
    nai_i2i 非 None 时是图生图（快照好的源图 base64 + 强度）。
    """
    job, reason = image_jobs.enqueue(target, target_id, prompt, skill="nai",
                                     nai_i2i=nai_i2i)
    if reason is not None:
        # 拒收时什么算力都没花，也没有孤儿图。
        return reason
    ahead = image_jobs.ahead_of(job)
    if ahead > 0:
        return ("已经排上队了（前面还有 %d 张），排到就画，"
                "画好会自动发到群里。"
                "不要输出图片地址，也不要说「图在下面 / 稍等」，"
                "直接把想说的话说完就行。" % ahead)
    if nai_i2i:
        return ("已经在画了（垫的是%s），画好会自动发到群里。"
                "不要输出图片地址，也不要说「图在下面 / 稍等」，"
                "直接把想说的话说完就行。" % nai_i2i["note"])
    return ("已经在画了，画好会自动发到群里。"
            "不要输出图片地址，也不要说「图在下面 / 稍等」，"
            "直接把想说的话说完就行。")


tool = {
    "name": "generate_image",
    "description": "调用 ComfyUI 生成图片。"
                  "【默认 Skill】文生图默认 anima（Anima 2B 动漫模型，单底模单段出图，一次一张，"
                  "768×1024），不传 skill 就是它—— prompt 只写一段画面描述，**不要用 --- 分隔**。"
                  "【换渠道】仅当用户点名或明确需要时才换：要一次出多张（多个提示词用 --- 分隔）"
                  "或要用固定角色底模时传 skill=image_gen_v1（**= SD / SDXL 渠道**，"
                  "用户说「用 sd / sd 生图 / 用那个 sd 模型」指的就是它；单段直出 832×1216，快）。"
                  "**只有对方点名要「高清 / 大图 / 精修 / 再修一遍」时**才传 "
                  "skill=image_gen_v1_hires（SD 两遍高清版，896×1600，比默认慢）——"
                  "**没点名就别自己挑它**。"
                  "【anima_2】**只有用户点名「双采样 / 二次采样 / 双底模 / 精修那版」才传 "
                  "skill=anima_2**（同一块底模跑两遍，更精细但慢一倍，一次一张）。"
                  "**用户没点名就绝不传它**——别因为「听起来更精细」自己挑。"
                  "【qwen_image_v1 / krea2 都已停用】**不要传 skill=qwen_image_v1 或 skill=krea2**"
                  "——这台机器带不动它们，传了工具会直接拒。"
                  "用户点名 qwen / 通义 / krea2、要**画面里写出文字（尤其中文）**、或要**写实照片感**时："
                  "照常用 anima 画（写实需求可改用 image_gen_v1），**照实说那个渠道现在用不了**，"
                  "别硬试、别拿别的渠道冒充、也别把渠道名当技术名词甩给用户。"
                  "【底模】除 image_gen_v1 外都没有固定角色，你在 prompt 中自己写出完整角色提示词"
                  "(发型/发色/体型/服装/年龄等)；只有 image_gen_v1 配 use_character=true 时用它的固定角色。"
                  "【lora】用户点名要换 lora 时才传 lora 参数，平时不要传。格式「文件名:强度」，"
                  "多个逗号分隔（如 \"x.safetensors:0.8,y.safetensors:0.5\"）；文件名要完整"
                  "(.safetensors 结尾)，写错会返回可用清单；传了就完全接管本次的 lora，"
                  "槽位 image_gen_v1 3 个 / anima 2 个 / anima_2 2 个，没填满的槽自动关闭。"
                  "【引用图片：默认只看，不改】**本机渠道（anima / image_gen_v1 等）"
                  "不要传 source_image**（它们的图生图已停用，传了工具会直接拒）；"
                  "**唯一例外是下面的 nai**。用户引用一张图，只是让你**看得见**"
                  "它：你要做的是**照它反推出提示词，用 anima 画一张新的**，"
                  "或者对方只是让你看图 / 点评时直接回话。"
                  "用户真要「改这张图 / 垫图 / 把X换成Y」而 nai 又没开通时，"
                  "照实说本机渠道改不了图，不要硬凑；可以问清他想要什么效果，"
                  "用 anima 重画一张（说明是新画的、不是改他那张）。"
                  "【nai / NovelAI】**仅限管理员为特定群开通 NAI 后**才能用，"
                  "图由群主自己的 NovelAI 账号在云端出，跟本机 ComfyUI 无关；"
                  "本群没开通就传了会被直接拒绝，照实说这个渠道本群用不了、"
                  "让对方去找群主开。"
                  "文生图：skill 传 nai，**只传 prompt，其它参数都不要传**。"
                  "图生图（改图 / 垫图）：skill 传 nai + **source_image 传 1**"
                  "（= 对方本轮**引用**的那张图；对方没引用就画不了，让他引用一条"
                  "带图的消息再 @ 一次），可选 denoise（0.1~0.9，默认 0.7，"
                  "越大改得越狠，别主动传）——**只在对方明确要改图 / 垫图时才传 "
                  "source_image**，看图 / 点评照旧不传。",
    # QQ 机器人看不到角色底模这套：Sumire 的角色描述只给网页端用。
    "description_overrides": {
        QQ_AGENT_ID:
            "调用 ComfyUI 生成图片。"
            "【默认 Skill】文生图默认 anima（Anima 2B 动漫模型，单底模单段出图，一次一张，"
            "768×1024），不传 skill 就是它 —— prompt 只写一段画面描述，**不要用 --- 分隔**。"
            "【换渠道】仅当对方点名或明确需要时才换："
            "要一次出多张时传 skill=image_gen_v1（**= SD / SDXL 渠道**，"
            "对方说「用 sd / sd 生图 / 用那个 sd 模型」指的就是它；多个提示词用 --- 分隔；"
            "单段直出 832×1216，快）。"
            "**只有对方点名要「高清 / 大图 / 精修 / 再修一遍」时**才传 "
            "skill=image_gen_v1_hires（SD 两遍高清版，896×1600，比默认慢）——"
            "**没点名就别自己挑它**。"
            "【anima_2】**只有对方点名「双采样 / 二次采样 / 双底模 / 精修那版」才传 "
            "skill=anima_2**（同一块底模跑两遍，更精细但慢一倍）。"
            "**对方没点名就绝不传它**。"
            "【qwen_image_v1 / krea2 都已停用】**不要传 skill=qwen_image_v1 或 skill=krea2**"
            "——这台机器带不动它们，传了工具会直接拒。"
            "对方点名 qwen / 通义 / krea2、要**画面里写出文字（尤其中文）**、或要写实照片感时："
            "照常用 anima 画，**照实说那个渠道现在用不了**，别硬试、别拿别的渠道冒充、"
            "也别把渠道名当技术名词甩给对方。"
            "【lora】用户点名要换 lora 时才传 lora 参数，平时不要传。格式「文件名:强度」，"
            "多个逗号分隔（如 \"x.safetensors:0.8\"）；文件名要完整(.safetensors 结尾)，"
            "写错会返回可用清单；最多 3 个，传了就完全接管本次的 lora。"
            "【引用图片：默认只看，不改】**本机渠道（anima / image_gen_v1 等）"
            "不要传 source_image**（它们的图生图已停用，传了工具会直接拒）；"
            "**唯一例外是下面的 nai**。对方引用一张图，只是让你**看得见**"
            "它：你要做的是**照它反推出提示词，用 anima 画一张新的**，"
            "或者对方只是让你看图 / 点评时直接回话。"
            "对方真要「改这张图 / 垫图 / 把X换成Y」而 nai 又没开通时，"
            "照实说本机渠道改不了图，不要硬凑；可以问清他想要什么效果，"
            "用 anima 重画一张（说明是新画的、不是改他那张）。"
            "【nai / NovelAI】**仅限管理员为特定群开通 NAI 后**才能用，"
            "图由群主自己的 NovelAI 账号在云端出，跟本机 ComfyUI 无关；"
            "本群没开通就传了会被直接拒绝，照实说这个渠道本群用不了、"
            "让对方去找群主开。"
            "文生图：skill 传 nai，**只传 prompt，其它参数都不要传**。"
            "图生图（改图 / 垫图）：skill 传 nai + **source_image 传 1**"
            "（= 对方本轮**引用**的那张图；对方没引用就画不了，让他引用一条"
            "带图的消息再 @ 一次），可选 denoise（0.1~0.9，默认 0.7，"
            "越大改得越狠，别主动传）——**只在对方明确要改图 / 垫图时才传 "
            "source_image**，看图 / 点评照旧不传。",
    },
    "hidden_params": {QQ_AGENT_ID: ["use_character"]},
    "function": _generate_image,
    "parameters": {
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "提示词。写逗号分隔的标签式英文短句（anima / image_gen_v1 都是这个写法），只写一段、不要用 --- 分隔（只有 skill=image_gen_v1 时才用 --- 分隔多张）。画面里没有固定角色时须包含完整角色描述"},
            "skill": {"type": "string", "description": "Skill名称。**不传就是默认 anima**（单底模）。可选值见系统提示 Available Skills 里标 [底模]/[无底模] 的生图类；image_gen_v1（**= SD / SDXL 渠道**）仅在用户点名或场景匹配时才用；**anima_2（双采样）和 image_gen_v1_hires（SD 高清版）都只在用户点名时才传，绝不主动选**。**qwen_image_v1 / krea2 已停用，不要传**；nai（NovelAI 云端）仅限已开通的群，文生图 / 图生图都走它"},
            "use_character": {"type": "boolean", "description": "是否使用该Skill自带的角色描述（默认false）。只有 image_gen_v1 有角色底模，设为true时固定该角色，你只写动作/环境/构图"},
            "lora": {"type": "string", "description": "可选。「文件名:强度」逗号分隔，如 x.safetensors:0.8,y.safetensors:0.5。仅在用户点名要换 lora 时传"},
            "source_image": {"type": "string", "description": "**仅 skill=nai 时可用**（图生图 / 垫图）：填 1 = 垫对方本轮**引用**的那张图（对方没引用会报错），可配 denoise（0.1~0.9，默认 0.7）。其它渠道的图生图已停用，传了会被拒；只是看图 / 点评时任何渠道都不要传这个参数"}
        },
        "required": ["prompt"]
    }
}
