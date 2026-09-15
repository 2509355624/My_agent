"""读取 Skill 说明文档的工具"""
from app.skills import load_skill


def _load_skill(skill_name):
    skill_data = load_skill(skill_name)
    if not skill_data:
        return "错误: 找不到 Skill '" + skill_name + "'"
    if not skill_data["skill_md"]:
        return "Skill '" + skill_name + "' 没有说明文档"

    parts = [skill_data["skill_md"]]

    if skill_data.get("version"):
        parts.append("[版本]\n" + skill_data["version"])

    if skill_data.get("references"):
        parts.append("[参考资料]\n" + skill_data["references"])

    if len(parts) > 1:
        return "\n\n".join(parts)
    return skill_data["skill_md"]


tool = {
    "name": "load_skill",
    "description": "读取某个 Skill 的主规范文档（会一并带出 references/ 全部内容，上下文开销大）。"
                  "只想读其中某一个文件时，请改用 list_files + read_file 按需读取。",
    "function": _load_skill,
    "parameters": {
        "type": "object",
        "properties": {
            "skill_name": {"type": "string", "description": "Skill 名称"}
        },
        "required": ["skill_name"]
    }
}
