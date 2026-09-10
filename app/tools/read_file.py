"""读取 Skill 内的文件（只读 skills 目录，安全限制）"""

import os
from app.config import SKILLS_DIR


def _safe_path(skill_name, filename):
    """确保路径在 skills 目录内，防止路径穿越"""
    # 过滤掉 .. / \ 等危险字符
    safe_skill = skill_name.replace("..", "").replace("/", "").replace("\\", "")
    safe_file = filename.replace("..", "").replace("/", "").replace("\\", "")

    target = os.path.join(SKILLS_DIR, safe_skill, safe_file)
    # 二次校验：规范化后必须仍在 SKILLS_DIR 内
    real_target = os.path.realpath(target)
    real_skills = os.path.realpath(SKILLS_DIR)
    if not real_target.startswith(real_skills):
        return None, "路径非法"
    return target, None


def read_file(skill_name, filename):
    """
    读取某个 Skill 目录下的文件
    参数:
      - skill_name: Skill 名称
      - filename: 文件名（如 skill.md, character.txt, workflow.json）
    """
    filepath, err = _safe_path(skill_name, filename)
    if err:
        return "错误: " + err

    if not os.path.exists(filepath):
        return "错误: 文件 '" + filename + "' 不存在于 Skill '" + skill_name + "' 中"

    try:
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()

        lines = content.split("\n")
        result = "文件: " + skill_name + "/" + filename + " (" + str(len(lines)) + " 行)\n\n"
        result += content
        return result
    except Exception as e:
        return "读取失败: " + str(e)


tool = {
    "name": "read_file",
    "description": "读取某个 Skill 目录下的文件（skill.md / character.txt / workflow.json 等）",
    "function": read_file,
    "parameters": {
        "type": "object",
        "properties": {
            "skill_name": {"type": "string", "description": "Skill 名称"},
            "filename": {"type": "string", "description": "文件名，如 skill.md、character.txt、workflow.json"}
        },
        "required": ["skill_name", "filename"]
    }
}
