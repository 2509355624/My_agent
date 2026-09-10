"""读取 Skill 说明文档的工具"""
from app.skills import load_skill


def _load_skill(skill_name):
    skill_data = load_skill(skill_name)
    if not skill_data:
        return "错误: 找不到 Skill '" + skill_name + "'"
    if not skill_data["skill_md"]:
        return "Skill '" + skill_name + "' 没有说明文档"
    return skill_data["skill_md"]


tool = {
    "name": "load_skill",
    "description": "读取某个 Skill 的完整说明文档",
    "function": _load_skill,
    "parameters": {
        "type": "object",
        "properties": {
            "skill_name": {"type": "string", "description": "Skill 名称"}
        },
        "required": ["skill_name"]
    }
}
