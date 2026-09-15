"""列出 skills 目录（或其中某个子目录）的文件结构

补的是「模型在 skills 内部是瞎的」这个洞：
    list_skills 只给顶层目录名，read_file 只能猜文件名——
    于是模型知道 human-writing 存在，却不知道里面有 references/fiction.md。
    有了 list_files，读取链才闭合：list_skills → list_files → read_file。

输出刻意用「相对 skills/ 的完整路径」，因为它就是 read_file(path=...) 直接能用的形式。
"""

import os

from app.tools.sandbox import (
    is_noise_dir,
    is_noise_file,
    resolve_in_skills,
    skills_root,
    to_rel,
)

MAX_ENTRIES = 300      # 单次输出条目上限，防止在大仓库里刷屏
MAX_DEPTH = 5
DEFAULT_DEPTH = 3      # 3 层足够看到 skills/<name>/references/*.md

# 这些后缀值得顺便报行数；其余（图片、模型文件等）只报体积
TEXT_EXTS = {".md", ".txt", ".json", ".py", ".yaml", ".yml", ".csv",
             ".html", ".xml", ".js", ".ts", ".toml", ".ini", ".log"}


def _fmt_size(size):
    if size < 1024:
        return str(size) + "B"
    if size < 1024 * 1024:
        return str(round(size / 1024, 1)) + "KB"
    return str(round(size / (1024 * 1024), 1)) + "MB"


def _count_lines(path):
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return sum(1 for _ in f)
    except OSError:
        return -1


def list_files(path="", depth=DEFAULT_DEPTH):
    """
    列出 skills 目录下的文件结构。

    参数:
      - path: 相对 skills/ 的路径，留空表示 skills 根目录。
              传某个 skill 名（如 "human-writing"）可只看它内部。
      - depth: 向下展开层数，1 表示只看指定目录的直接子项，默认 3，最大 5
    """
    raw = "" if path is None else str(path).strip()

    # 空路径 / 泛指写法都当作根目录
    if raw in ("", ".", "/", "./", "skills", "skills/", "skills\\"):
        root = skills_root()
    else:
        root, err = resolve_in_skills(raw, allow_absolute=True)
        if err:
            return "错误: " + err

    if not os.path.exists(root):
        return ("错误: 路径不存在: " + raw
                + "（先用 list_files() 不带参数看 skills 根目录有哪些 skill）")

    if not os.path.isfile(root) and not os.path.isdir(root):
        return "错误: 路径不可读: " + raw

    if os.path.isfile(root):
        return ("'" + raw + "' 是文件不是目录，直接 read_file(path=\"" + raw + "\") 即可。")

    try:
        depth = int(depth)
    except (TypeError, ValueError):
        depth = DEFAULT_DEPTH
    depth = max(1, min(depth, MAX_DEPTH))

    entries = []          # (相对路径, 是否目录, 体积, 行数)
    truncated = [False]

    def walk(cur, level):
        if level > depth or truncated[0]:
            return
        try:
            names = sorted(os.listdir(cur))
        except OSError:
            return
        dirs = [n for n in names
                if os.path.isdir(os.path.join(cur, n)) and not is_noise_dir(n)]
        files = [n for n in names
                 if os.path.isfile(os.path.join(cur, n)) and not is_noise_file(n)]

        for name in dirs:
            if len(entries) >= MAX_ENTRIES:
                truncated[0] = True
                return
            full = os.path.join(cur, name)
            entries.append((to_rel(full), True, 0, -1))
            walk(full, level + 1)

        for name in files:
            if len(entries) >= MAX_ENTRIES:
                truncated[0] = True
                return
            full = os.path.join(cur, name)
            ext = os.path.splitext(name)[1].lower()
            try:
                size = os.path.getsize(full)
            except OSError:
                size = 0
            entries.append((to_rel(full), False, size,
                            _count_lines(full) if ext in TEXT_EXTS else -1))

    walk(root, 1)

    if not entries:
        return "目录为空: skills/" + (to_rel(root) + "/" if to_rel(root) != "." else "")

    lines_out = []
    for rel, is_dir, size, nlines in entries:
        if is_dir:
            lines_out.append("[目录] " + rel + "/")
        else:
            info = _fmt_size(size)
            if nlines >= 0:
                info += ", " + str(nlines) + " 行"
            lines_out.append(rel + "  (" + info + ")")

    scope = "skills 根目录" if root == skills_root() else ("skills/" + to_rel(root))
    head = (scope + " 文件结构（相对 skills/ 的路径 | depth=" + str(depth)
            + " | 共 " + str(len(entries)) + " 项"
            + ("，已截断" if truncated[0] else "") + "）:\n")

    tail = "\n\n用 read_file(path=\"<上面某个文件路径>\") 读取具体内容。"
    if truncated[0]:
        tail = ("\n\n（条目过多已截断，缩小 path 或 depth 再看）" + tail)

    return head + "\n".join(lines_out) + tail


tool = {
    "name": "list_files",
    "description": "列出 skills 目录的文件结构（可指定某个 skill 子目录）。"
                  "深入一个 Skill 前先用它看清里面有哪些文件，再用 read_file 按需读取，"
                  "不要盲目全量加载。",
    "function": list_files,
    "parameters": {
        "type": "object",
        "properties": {
            "path": {"type": "string",
                     "description": "相对 skills/ 的目录路径，留空表示 skills 根目录；传 skill 名只看它内部"},
            "depth": {"type": "integer", "description": "展开层数，1=只看直接子项，默认 3，最大 5"}
        },
        "required": []
    }
}
