"""写入 Skill 内的文件（只写 skills 目录，安全限制）"""

import os
import json
from app.config import SKILLS_DIR


def _safe_path(skill_name, filename):
    """确保路径在 skills 目录内，防止路径穿越"""
    safe_skill = skill_name.replace("..", "").replace("/", "").replace("\\", "")
    safe_file = filename.replace("..", "").replace("/", "").replace("\\", "")

    target_dir = os.path.join(SKILLS_DIR, safe_skill)
    target = os.path.join(target_dir, safe_file)

    # 二次校验
    real_target = os.path.realpath(target)
    real_skills = os.path.realpath(SKILLS_DIR)
    if not real_target.startswith(real_skills):
        return None, None, "路径非法"
    return target_dir, target, None


def write_file(skill_name, filename, content):
    """
    写入某个 Skill 目录下的文件。如果 Skill 目录不存在，自动创建。
    参数:
      - skill_name: Skill 名称（英文小写，用下划线分隔）
      - filename: 文件名（skill.md / character.txt / workflow.json）
      - content: 文件内容
    """
    if not skill_name or not filename:
        return "错误: skill_name 和 filename 不能为空"

    target_dir, filepath, err = _safe_path(skill_name, filename)
    if err:
        return "错误: " + err

    try:
        # 自动创建 Skill 目录
        os.makedirs(target_dir, exist_ok=True)

        # 写文件
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)

        lines = content.split("\n")
        return "已写入 " + skill_name + "/" + filename + " (" + str(len(lines)) + " 行)"
    except Exception as e:
        return "写入失败: " + str(e)


tool = {
    "name": "write_file",
    "description": "写入 Skill 文件，可创建新 Skill 或更新已有 Skill 的文件。只允许写入 skills 目录内的文件。",
    "function": write_file,
    "parameters": {
        "type": "object",
        "properties": {
            "skill_name": {"type": "string", "description": "Skill 名称（英文，小写下划线，如 cyberpunk_style）"},
            "filename": {"type": "string", "description": "文件名：skill.md / character.txt / workflow.json"},
            "content": {"type": "string", "description": "文件完整内容"}
        },
        "required": ["skill_name", "filename", "content"]
    }
}
