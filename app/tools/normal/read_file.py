"""读取 skills 目录内的文件（只读 skills 沙箱，支持任意层级子目录）"""

import os

from app.tools.sandbox import resolve_in_skills, to_rel

# limit<=0 时的保护上限：别让一次 read_file 把上下文撑爆
DEFAULT_MAX_LINES = 1000


def read_file(path, offset=1, limit=0):
    """
    读取 skills 目录内的文件。

    参数:
      - path: 相对 skills/ 的路径，可带子目录，
              如 "human-writing/SKILL.md"、"human-writing/references/fiction.md"
      - offset: 起始行号（从 1 开始，默认 1）
      - limit: 读取行数，0 表示读到末尾（默认 0，上限 DEFAULT_MAX_LINES）
    """
    target, err = resolve_in_skills(path, allow_absolute=True)
    if err:
        return "错误: " + err

    if os.path.isdir(target):
        return ("错误: '" + str(path) + "' 是目录，不是文件。"
                + "用 list_files(path=\"" + str(path) + "\") 查看它下面有哪些文件。")

    if not os.path.exists(target):
        return ("错误: 文件不存在: " + str(path)
                + "（先用 list_files 确认路径，例如 list_files(path=\"human-writing\")）")

    try:
        with open(target, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
    except Exception as e:
        return "读取失败: " + str(e)

    total = len(all_lines)

    try:
        offset = max(1, int(offset))
    except (TypeError, ValueError):
        offset = 1
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 0
    if limit <= 0:
        limit = DEFAULT_MAX_LINES

    start = min(offset - 1, total)
    end = min(start + limit, total)
    chunk = all_lines[start:end]

    rel = to_rel(target)
    if start == 0 and end == total:
        header = "文件: " + rel + " (共 " + str(total) + " 行)\n\n"
    else:
        header = ("文件: " + rel + " (第 " + str(start + 1) + "-" + str(end)
                  + " 行 / 共 " + str(total) + " 行)\n\n")

    result = header + "".join(chunk)
    if end < total:
        result += ("\n\n... (还有 " + str(total - end)
                   + " 行未显示，用 offset=" + str(end + 1) + " 继续读取)")
    return result


tool = {
    "name": "read_file",
    "description": "读取 skills 目录内的文件，支持任意层级子目录（如 human-writing/references/fiction.md）。"
                  "大文件用 offset / limit 分段读。不确定路径时先用 list_files 查看目录结构。",
    "function": read_file,
    "parameters": {
        "type": "object",
        "properties": {
            "path": {"type": "string",
                     "description": "相对 skills/ 的路径，可带子目录，如 human-writing/references/fiction.md"},
            "offset": {"type": "integer", "description": "起始行号，从 1 开始，默认 1"},
            "limit": {"type": "integer", "description": "读取行数，0（默认）表示读到末尾，单次最多 1000 行"}
        },
        "required": ["path"]
    }
}
