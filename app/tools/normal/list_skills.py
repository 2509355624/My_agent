"""列出所有可用 Skill"""

from app.skills import list_skills, load_skill


def _list_skills():
    skills = list_skills()
    if not skills:
        return "暂无可用 Skill"

    lines = []
    for s in skills:
        data = load_skill(s)
        if data and data["skill_md"]:
            first_line = data["skill_md"].strip().split("\n")[0].lstrip("# ").strip()
            lines.append("- **" + s + "**: " + first_line)
        else:
            lines.append("- **" + s + "**: (无说明)")

    return "当前可用 Skill 共 " + str(len(skills)) + " 个：\n" + "\n".join(lines)


tool = {
    "name": "list_skills",
    "description": "列出所有可用的生图 Skill",
    "function": _list_skills,
    "parameters": {
        "type": "object",
        "properties": {}
    }
}
