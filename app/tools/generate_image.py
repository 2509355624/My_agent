"""ComfyUI 生图工具"""

import requests
import json
import time
import random
import uuid
from flask import request
from app.config import COMFYUI_URL, AGENT_PORT
from app.skills import load_skill


# ─── ComfyUI 内部函数 ────────────────────────────────

def _queue_prompt(workflow):
    resp = requests.post(COMFYUI_URL + "/prompt", json={
        "prompt": workflow,
        "client_id": "agent_" + str(uuid.uuid4())[:8]
    }, timeout=30)
    resp.raise_for_status()
    return resp.json()["prompt_id"]


def _wait_for_completion(prompt_id, timeout=300):
    start = time.time()
    while time.time() - start < timeout:
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

def _generate_image(prompt, skill="image_gen_v1"):
    skill_data = load_skill(skill)
    if not skill_data or not skill_data["workflow"]:
        return "错误: 找不到 Skill '" + skill + "'"

    workflow_str = json.dumps(skill_data["workflow"])

    # 替换占位符
    seed = random.randint(1, 2**32 - 1)
    character = skill_data.get("character", "")
    # 转义 character 里的换行和特殊字符
    character_escaped = character.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n').replace('\r', '')
    workflow_str = workflow_str.replace('"__MULTI_PROMPTS__"', json.dumps(prompt))
    workflow_str = workflow_str.replace("__SEED__", str(seed))
    workflow_str = workflow_str.replace("__CHARACTER__", character_escaped)

    workflow = json.loads(workflow_str)

    # 提交到 ComfyUI
    prompt_id = _queue_prompt(workflow)
    history_entry = _wait_for_completion(prompt_id)
    images = _get_output_images(history_entry)

    if not images:
        return "错误: 生成完成但未找到输出图片"

    base_url = request.host_url.rstrip('/') if request else "http://localhost:" + str(AGENT_PORT)
    urls = [base_url + "/api/image/" + img for img in images]

    return "生成成功！seed: " + str(seed) + "\n图片地址:\n" + "\n".join(urls)


tool = {
    "name": "generate_image",
    "description": "调用 ComfyUI 生成图片",
    "function": _generate_image,
    "parameters": {
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "英文提示词，逗号分隔的标签"},
            "skill": {"type": "string", "description": "Skill名称，默认image_gen_v1"}
        },
        "required": ["prompt"]
    }
}
