"""写入 skills 目录内的文件（只写 skills 沙箱，支持子目录，父目录自动创建）"""

import os

from app.tools.sandbox import resolve_in_skills, to_rel


def write_file(path, content):
    """
    写入 skills 目录内的文件；父目录不存在会自动创建。

    参数:
      - path: 相对 skills/ 的路径，可带子目录，
              如 "my_skill/skill.md"、"my_skill/references/notes.md"
      - content: 文件完整内容
    """
    if path is None or not str(path).strip():
        return "错误: path 不能为空"
    if content is None:
        content = ""

    target, err = resolve_in_skills(path, allow_absolute=True)
    if err:
        return "错误: " + err

    if os.path.isdir(target):
        return ("错误: '" + str(path) + "' 是目录，path 必须指向文件。"
                + "如果要建新文件，请把文件名带上，如 'my_skill/skill.md'")

    existed = os.path.exists(target)

    try:
        parent = os.path.dirname(target)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(target, "w", encoding="utf-8") as f:
            f.write(content)
    except Exception as e:
        return "写入失败: " + str(e)

    lines = content.split("\n")
    return ("已" + ("覆盖" if existed else "写入") + " " + to_rel(target)
            + " (" + str(len(lines)) + " 行)")


tool = {
    "name": "write_file",
    "description": "写入 skills 目录内的文件，可创建新 Skill 或更新已有文件（父目录自动创建）。"
                  "只允许写入 skills 目录内部。path 是相对 skills/ 的路径，可带子目录。",
    "function": write_file,
    "parameters": {
        "type": "object",
        "properties": {
            "path": {"type": "string",
                     "description": "相对 skills/ 的路径，如 my_skill/skill.md 或 my_skill/references/notes.md"},
            "content": {"type": "string", "description": "文件完整内容"}
        },
        "required": ["path", "content"]
    }
}
