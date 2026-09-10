"""
Skill 管理
加载、列出 Skills
"""

import os
import json
from app.config import SKILLS_DIR


def load_skill(skill_name):
    """加载 Skill: 返回 {name, workflow, skill_md, character}"""
    skill_dir = os.path.join(SKILLS_DIR, skill_name)
    if not os.path.isdir(skill_dir):
        return None

    workflow_path = os.path.join(skill_dir, "workflow.json")
    skill_md_path = os.path.join(skill_dir, "skill.md")

    workflow = None
    if os.path.exists(workflow_path):
        try:
            with open(workflow_path, "r", encoding="utf-8") as f:
                # 兜底：ComfyUI 导出的 workflow 可能写成裸占位符
                raw = f.read().replace(": __SEED__", ': "__SEED__"')
                workflow = json.loads(raw)
        except json.JSONDecodeError as e:
            print("[警告] Skill '" + skill_name + "' 的 workflow.json 解析失败: " + str(e))
            return None

    skill_md = ""
    if os.path.exists(skill_md_path):
        with open(skill_md_path, "r", encoding="utf-8") as f:
            skill_md = f.read()

    # 角色底模
    character = ""
    char_path = os.path.join(skill_dir, "character.txt")
    if os.path.exists(char_path):
        with open(char_path, "r", encoding="utf-8") as f:
            character = f.read().strip()

    return {
        "name": skill_name,
        "workflow": workflow,
        "skill_md": skill_md,
        "character": character,
    }


def list_skills():
    """列出所有可用 Skill"""
    if not os.path.isdir(SKILLS_DIR):
        return []
    return [d for d in os.listdir(SKILLS_DIR)
            if os.path.isdir(os.path.join(SKILLS_DIR, d))]


def build_system_prompt():
    """
    构建系统提示词。
    只告诉 LLM 有哪些工具和 Skill，具体规范在 Skill 的 skill.md 里，
    由 LLM 自己通过 load_skill 工具按需读取。
    """
    skill_list = list_skills()
    skill_descs = []
    for s in skill_list:
        skill_data = load_skill(s)
        if skill_data and skill_data["skill_md"]:
            first_line = skill_data["skill_md"].strip().split("\n")[0].lstrip("# ").strip()
            skill_descs.append("- **" + s + "**: " + first_line)

    skill_text = "\n".join(skill_descs) if skill_descs else "（暂无）"

    prompt = """你是用户的私人 AI 助理，擅长理解需求并调用合适的工具完成任务。

## 工具调用格式

当你需要调用工具时，在回复中使用以下格式：

[[TOOL:工具名]]{"参数名": "参数值"}[[/TOOL]]

## 可用工具

### get_time
获取当前日期和时间。无需参数。
调用示例: [[TOOL:get_time]]{}[[/TOOL]]

### web_search
网页搜索，获取最新信息、参考资料或灵感素材。
参数:
- query (必填): 搜索关键词
- max_results (可选): 返回结果数量，默认 5

### load_skill
读取某个 Skill 的完整说明文档（skill.md）。
当你需要使用某个生图 Skill 但不确定具体规范时，先调用此工具读取说明。
参数:
- skill_name (必填): Skill 名称

### generate_image
调用 ComfyUI AI 绘图工具生成图片。
参数:
- prompt (必填): 英文提示词，逗号分隔的标签
- skill (可选): 生图风格 Skill 名称，默认 image_gen_v1

**重要**: 使用 generate_image 之前，如果不熟悉该 Skill 的规范，务必先调用 load_skill 读取说明。

## 可用 Skills
""" + skill_text + """

## 规则
- 不需要工具时直接回答用户
- 调用工具后，根据结果继续回答或调用下一个工具
- 生成图片时，先扩展提示词再调用工具
- 用中文回复用户，但传给 generate_image 的 prompt 必须是英文
"""
    return prompt
