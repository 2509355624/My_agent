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
