"""grep_file —— 在 skills 目录内的文本文件里按行检索（正则/子串），可选限定列。

专为标签库查询场景设计（skills/anima-tags/data/tags.tsv，32.8 万行、
三列 标签名 / 中文名 / 类别码，按名称字母序），但通用：任何 skills 内的
文本文件都能搜。比 read_file 整表读进来省上下文，比让模型去 grep 命令行
更可控、更安全（路径收在 skills 沙箱里）。

设计取舍（来自用户钉死的需求）：
  - 三参数够用：path / pattern / max_results(默认50)；再加一个可选 column。
  - pattern 按 Python 正则匹配；普通子串直接写即可。正则非法时自动退化为
    字面量匹配，避免一个元字符把整次查询变成零命中。
  - column 限定只搜某一列，挡掉「搜 dress 撞一堆描述行」的噪音：
    1=标签名 2=中文名 3=类别码。不填则搜整行。
  - 那张 TSV 字母序排好、能搜行 = 能干全部活（中→英、英→中、模糊找候选），
    不需要载入全表、不需要 SQL、不需要向量库。
"""

import os
import re

from app.tools.sandbox import resolve_in_skills, to_rel

DEFAULT_MAX_RESULTS = 50
HARD_MAX_RESULTS = 1000


def grep_file(path, pattern, max_results=DEFAULT_MAX_RESULTS, column=None):
    """
    在 skills 目录内的文本文件里按行检索。

    参数:
      - path: 相对 skills/ 的路径，如 "anima-tags/data/tags.tsv"
             （也可以带 "skills/" 前缀，或给落在 skills 内的绝对路径）
      - pattern: 要搜的词，按 Python 正则匹配；普通子串直接写即可。
                 含正则元字符想当字面量时，用 \\Q...\\E 包裹或自行转义。
                 正则非法时自动退化为字面量匹配。区分大小写；要忽略大小写
                 在 pattern 前加 (?i)。
      - max_results: 返回行数上限，默认 50（硬上限 1000）
      - column: 可选，限定只搜第几列（1=标签名 2=中文名 3=类别码）；
                不填则搜整行。用来挡掉搜 "dress" 撞一堆描述行的噪音。
    """
    target, err = resolve_in_skills(path, allow_absolute=True)
    if err:
        return "错误: " + err

    if os.path.isdir(target):
        return ("错误: '" + str(path) + "' 是目录，不是文件。"
                + "用 list_files(path=\"" + str(path) + "\") 查看它下面有哪些文件。")

    if not os.path.exists(target):
        return ("错误: 文件不存在: " + str(path)
                + "（先用 list_files 确认路径）")

    # 编译正则；非法则退化为字面量（re.escape 后）
    try:
        rx = re.compile(pattern)
    except re.error:
        rx = re.compile(re.escape(pattern))

    # 解析列参数（1-based → 0-based；越界/非法直接忽略，退回整行搜）
    col = None
    if column is not None:
        try:
            c = int(column)
            if c >= 1:
                col = c - 1
        except (TypeError, ValueError):
            col = None

    try:
        cap = int(max_results)
    except (TypeError, ValueError):
        cap = DEFAULT_MAX_RESULTS
    if cap <= 0:
        cap = DEFAULT_MAX_RESULTS
    if cap > HARD_MAX_RESULTS:
        cap = HARD_MAX_RESULTS

    matched = []
    total = 0
    try:
        with open(target, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                stripped = line.rstrip("\n").rstrip("\r")
                if col is not None:
                    parts = stripped.split("\t")
                    if len(parts) <= col:
                        continue
                    hay = parts[col]
                else:
                    hay = stripped
                if rx.search(hay):
                    total += 1
                    if len(matched) < cap:
                        matched.append(stripped)
    except Exception as e:
        return "读取失败: " + str(e)

    rel = to_rel(target)
    if not matched:
        return ("在 " + rel + " 中未找到匹配: " + pattern
                + (("（仅搜第 " + str(col + 1) + " 列）") if col is not None else ""))

    col_note = ("，仅搜第 " + str(col + 1) + " 列") if col is not None else ""
    if total > len(matched):
        head = ("匹配 " + str(len(matched)) + " 行（已截断，实际命中 "
                + str(total) + " 行，用更精确的 pattern 或 column 收敛）" + col_note)
    else:
        head = "匹配 " + str(total) + " 行" + col_note
    return head + "\n文件: " + rel + "\n" + "\n".join(matched)


tool = {
    "name": "grep_file",
    "description": "在 skills 目录内的文本文件里按行检索（正则/子串均可），可选限定只搜某一列。"
                  "专为标签库查询设计（skills/anima-tags/data/tags.tsv：三列 标签名 / 中文名 / 类别码，TAB 分隔，32.8 万行），"
                  "但通用：任何 skills 内文本文件都能搜。比 read_file 整表读进来省上下文。"
                  "查中文→标签用 column=2，查标签→中文用 column=1；两者都按名称字母序返回，不排序不推荐。",
    "function": grep_file,
    "parameters": {
        "type": "object",
        "properties": {
            "path": {"type": "string",
                     "description": "相对 skills/ 的路径，如 anima-tags/data/tags.tsv（也可带 skills/ 前缀或给落在 skills 内的绝对路径）"},
            "pattern": {"type": "string",
                        "description": "要搜的词，按 Python 正则匹配；普通子串直接写（如 skadi）。含正则元字符想当字面量时用 \\Q...\\E 或转义。区分大小写，忽略大小写加 (?i)。"},
            "max_results": {"type": "integer", "description": "返回行数上限，默认 50（硬上限 1000）"},
            "column": {"type": "integer", "description": "可选，限定只搜第几列：1=标签名 2=中文名 3=类别码。不填则搜整行，用来挡掉搜 'dress' 撞一堆描述行的噪音"}
        },
        "required": ["path", "pattern"]
    }
}
