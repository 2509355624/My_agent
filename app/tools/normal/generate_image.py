"""ComfyUI 生图工具"""

import requests
import json
import time
import random
import uuid
from flask import request
from app.cancel import Cancelled, is_cancelled
from app.config import COMFYUI_URL, IMAGE_GEN_TIMEOUT, QQ_AGENT_ID
from app.skills import load_skill


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
        try:
            resp = requests.get(COMFYUI_URL + "/history/" + prompt_id, timeout=10)
            resp.raise_for_status()
            history = resp.json()
            if prompt_id in history:
                return history[prompt_id]
        except:
            pass
        time.sleep(2)
    raise TimeoutError("生成超时 (" + str(timeout) + "s)")


def _get_output_images(history_entry):
    images = []
    outputs = history_entry.get("outputs", {})
    for node_id, node_output in outputs.items():
        if "images" in node_output:
            for img in node_output["images"]:
                images.append(img["filename"])
    return images


# ─── 工具函数 ────────────────────────────────────────

def _generate_image(prompt, skill="image_gen_v1", use_character=True):
    # 提交前先看一眼：已经中断就别再往 ComfyUI 队列里塞新任务了
    if is_cancelled():
        return "已中断：用户取消了本次生成。"

    gate = _qq_gate()
    if gate is not None:
        return gate

    skill_data = load_skill(skill)
    if not skill_data or not skill_data["workflow"]:
        return "错误: 找不到 Skill '" + skill + "'"

    workflow_str = json.dumps(skill_data["workflow"])

    # 替换占位符
    seed = random.randint(1, 2**32 - 1)
    # 是否用 skill 底模：默认用；若关闭则用中性占位（不吃角色本体）
    character = skill_data.get("character", "")
    if not use_character:
        character = ""
    # 转义 character 里的换行和特殊字符
    character_escaped = character.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n').replace('\r', '')
    workflow_str = workflow_str.replace('"__MULTI_PROMPTS__"', json.dumps(prompt))
    workflow_str = workflow_str.replace("__SEED__", str(seed))
    workflow_str = workflow_str.replace("__CHARACTER__", character_escaped)

    workflow = json.loads(workflow_str)

    # 提交到 ComfyUI
    prompt_id = _queue_prompt(workflow)
    try:
        history_entry = _wait_for_completion(prompt_id)
    except Cancelled:
        # 不把 Cancelled 抛给 execute_tool：那会被描述成"工具执行失败"，
        # 让模型以为工具坏了。中断是一个正常结局，说清楚就行。
        return "已中断：用户取消了等待。图片可能仍在后台生成，可到 ComfyUI 界面查看。"
    images = _get_output_images(history_entry)

    if not images:
        return "错误: 生成完成但未找到输出图片"

    # 用相对路径（不带 host）：任何端(手机/平板/PC)访问时都用当前站点 origin 加载
    urls = ["/api/image/" + img for img in images]

    return "生成成功！seed: " + str(seed) + "\n图片地址:\n" + "\n".join(urls)


tool = {
    "name": "generate_image",
    "description": "调用 ComfyUI 生成图片，支持批量生成。多个提示词用 --- 分隔，一次调用可生成多张图。"
                  "【底模两种模式】use_character=true时使用Skill自带角色底模(固定角色，prompt只写动作/环境/构图)；"
                  "use_character=false时无底模，你必须自己在prompt中写出完整角色提示词(发型/发色/体型/胸围/服装/年龄等)，再叠加动作和环境。"
                  "【默认 Skill】没特别说明就用 image_gen_v1，不要无理由换。"
                  "仅当用户明确点名 krea2（如「用 krea2」「krea2 生图」）时才传 skill=krea2——"
                  "它是备选的 Krea2 Turbo + retroanime lora 工作流，一次只出一张，prompt 不要带 --- 分隔。",
    "function": _generate_image,
    "parameters": {
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "英文提示词，逗号分隔的标签。多张图用 --- 分隔，例如: prompt1 --- prompt2 --- prompt3。无底模时须包含完整角色描述"},
            "skill": {"type": "string", "description": "Skill名称，默认image_gen_v1。可选值见系统提示 Available Skills 里标 [底模]/[无底模] 的生图类；krea2 仅在用户点名时用"},
            "use_character": {"type": "boolean", "description": "是否使用该Skill自带的角色底模（默认true）。设为false时无底模，你必须把完整角色提示词写进prompt"}
        },
        "required": ["prompt"]
    }
}
