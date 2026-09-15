"""列出所有可用 Skill"""

from app.skills import list_skills, load_skill, skill_summary


def _list_skills():
    skills = list_skills()
    if not skills:
        return "暂无可用 Skill"

    lines = []
    for s in skills:
        data = load_skill(s)
        summary = skill_summary(data["skill_md"]) if data else ""
        lines.append("- **" + s + "**: " + (summary or "(无说明)"))

    return "当前可用 Skill 共 " + str(len(skills)) + " 个：\n" + "\n".join(lines)


tool = {
    "name": "list_skills",
    "description": "列出所有可用 Skill（含生图、写作等各类），并给出一行说明",
    "function": _list_skills,
    "parameters": {
        "type": "object",
        "properties": {}
    }
}
