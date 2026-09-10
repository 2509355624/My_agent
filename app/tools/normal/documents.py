"""文档管理工具 — 读取、搜索、列文档

长文档阅读策略：
1. 先用 list_documents 看有哪些文件
2. 用 file_info 看文件大小和行数
3. 用 search_document 搜索关键词定位
4. 用 read_document 分段精读（offset + limit）
"""

import os
import re
from app.config import DOCUMENTS_DIR


def _safe_path(filename):
    """确保文件在 documents 目录内"""
    if not filename:
        return None, "文件名不能为空"

    # 规范化路径，过滤危险字符
    safe_name = filename.replace("..", "").lstrip("/").lstrip("\\").strip()
    if not safe_name:
        return None, "文件名非法"

    # 支持子目录，但必须仍在 DOCUMENTS_DIR 内
    target = os.path.join(DOCUMENTS_DIR, safe_name)
    try:
        real_target = os.path.realpath(target)
        real_docs = os.path.realpath(DOCUMENTS_DIR)
        if not real_target.startswith(real_docs):
            return None, "路径非法"
    except Exception:
        return None, "路径非法"

    return target, None


def _ensure_dirs():
    """确保 documents 目录存在"""
    os.makedirs(DOCUMENTS_DIR, exist_ok=True)


def list_documents():
    """列出 documents 目录下所有文件"""
    _ensure_dirs()
    result = []
    for root, dirs, files in os.walk(DOCUMENTS_DIR):
        for f in files:
            full_path = os.path.join(root, f)
            rel_path = os.path.relpath(full_path, DOCUMENTS_DIR)
            size = os.path.getsize(full_path)
            try:
                with open(full_path, "r", encoding="utf-8", errors="ignore") as fh:
                    lines = len(fh.readlines())
            except:
                lines = -1
            size_str = _format_size(size)
            result.append(f"- {rel_path}  ({size_str}, {lines} 行)")

    if not result:
        return "documents 目录为空。把文件放进去即可读取。"

    return "documents 目录下共 " + str(len(result)) + " 个文件：\n" + "\n".join(result)


def _format_size(size):
    if size < 1024:
        return str(size) + "B"
    elif size < 1024 * 1024:
        return str(round(size / 1024, 1)) + "KB"
    else:
        return str(round(size / (1024 * 1024), 1)) + "MB"


def file_info(filename):
    """
    获取文件信息（大小、行数）
    参数: filename - 相对于 documents 目录的路径
    """
    filepath, err = _safe_path(filename)
    if err:
        return "错误: " + err

    if not os.path.exists(filepath):
        return "错误: 文件 '" + filename + "' 不存在"

    size = os.path.getsize(filepath)
    try:
        with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
            lines = len(f.readlines())
    except:
        lines = -1

    return (
        "文件: " + filename + "\n"
        "大小: " + _format_size(size) + "\n"
        "行数: " + str(lines)
    )


def read_document(filename, offset=1, limit=100):
    """
    分段读取文档（带行号）
    参数:
      - filename: 文件名
      - offset: 起始行号（从1开始，默认1）
      - limit: 读取行数（默认100行，最多500行）
    """
    filepath, err = _safe_path(filename)
    if err:
        return "错误: " + err

    if not os.path.exists(filepath):
        return "错误: 文件 '" + filename + "' 不存在"

    limit = min(int(limit), 500)  # 最多读 500 行
    offset = max(1, int(offset))

    try:
        with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
            all_lines = f.readlines()

        total = len(all_lines)
        start = offset - 1
        end = min(start + limit, total)
        chunk = all_lines[start:end]

        lines_out = []
        for i, line in enumerate(chunk):
            lineno = start + i + 1
            lines_out.append(str(lineno) + "\t" + line.rstrip("\n"))

        result = (
            "文件: " + filename + "  (第 " + str(offset) + "-" +
            str(min(offset + limit - 1, total)) + " 行 / 共 " + str(total) + " 行)\n\n"
        )
        result += "\n".join(lines_out)

        if end < total:
            result += "\n\n... (还有 " + str(total - end) + " 行未显示，调整 offset 继续读取)"

        return result
    except Exception as e:
        return "读取失败: " + str(e)


def search_document(filename, query):
    """
    在文档中搜索关键词，返回匹配行及上下文
    参数:
      - filename: 文件名
      - query: 搜索关键词（支持简单字符串匹配）
    """
    filepath, err = _safe_path(filename)
    if err:
        return "错误: " + err

    if not os.path.exists(filepath):
        return "错误: 文件 '" + filename + "' 不存在"

    try:
        with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()

        matches = []
        query_lower = query.lower()
        for i, line in enumerate(lines):
            if query_lower in line.lower():
                # 取前后 2 行作为上下文
                start = max(0, i - 2)
                end = min(len(lines), i + 3)
                snippet = []
                for j in range(start, end):
                    prefix = ">" if j == i else " "
                    snippet.append(prefix + " " + str(j + 1) + "\t" + lines[j].rstrip("\n"))
                matches.append("\n".join(snippet))

                if len(matches) >= 10:  # 最多返回 10 个匹配
                    break

        if not matches:
            return "在 '" + filename + "' 中未找到 '" + query + "'"

        return (
            "在 '" + filename + "' 中找到 " + str(len(matches)) +
            " 处 '" + query + "' 匹配：\n\n" +
            "\n\n---\n\n".join(matches)
        )
    except Exception as e:
        return "搜索失败: " + str(e)


# ─── 注册为工具 ──────────────────────────────────────

tool_list = {
    "name": "list_documents",
    "description": "列出 documents 目录下所有可读取的文件",
    "function": list_documents,
    "parameters": {"type": "object", "properties": {}}
}

tool_info = {
    "name": "file_info",
    "description": "获取文件信息（大小、行数）",
    "function": file_info,
    "parameters": {
        "type": "object",
        "properties": {
            "filename": {"type": "string", "description": "文件名（相对于 documents 目录）"}
        },
        "required": ["filename"]
    }
}

tool_read = {
    "name": "read_document",
    "description": "分段读取文档内容，带行号。支持 offset 和 limit 控制读取范围，避免一次读入太多内容。",
    "function": read_document,
    "parameters": {
        "type": "object",
        "properties": {
            "filename": {"type": "string", "description": "文件名"},
            "offset": {"type": "integer", "description": "起始行号（从1开始，默认1）"},
            "limit": {"type": "integer", "description": "读取行数，默认100，最多500"}
        },
        "required": ["filename"]
    }
}

tool_search = {
    "name": "search_document",
    "description": "在文档中搜索关键词，返回匹配行及上下文。读长文档前先搜索定位。",
    "function": search_document,
    "parameters": {
        "type": "object",
        "properties": {
            "filename": {"type": "string", "description": "文件名"},
            "query": {"type": "string", "description": "搜索关键词"}
        },
        "required": ["filename", "query"]
    }
}
