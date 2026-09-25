"""
工具注册表
所有工具在这里注册（普通工具 + RAG 工具），Agent 通过 execute_tool 统一调用。

目录结构：
  app/tools/normal/  普通工具（时间、搜索、文件、生图、Skill 管理、文档）
  app/tools/rag/     RAG 向量知识库工具（检索、入库、切块）
  app/rag/           RAG 引擎层（向量存储、embedding、daemon，非 Agent 工具）
"""

TOOLS = []


def register_tool(name, description, function, parameters):
    """注册一个工具"""
    TOOLS.append({
        "name": name,
        "description": description,
        "function": function,
        "parameters": parameters,
    })


def execute_tool(name, args):
    """执行工具，返回结果字符串"""
    for tool in TOOLS:
        if tool["name"] == name:
            try:
                return tool["function"](**args)
            except Exception as e:
                return "工具执行失败: " + str(e)
    return "未知工具: " + name


# ─── 普通工具 ─────────────────────────────────────────
from app.tools.normal.get_time import tool as _get_time_tool
register_tool(**_get_time_tool)

from app.tools.normal.web_search import tool as _web_search_tool
register_tool(**_web_search_tool)

from app.tools.normal.load_skill import tool as _load_skill_tool
register_tool(**_load_skill_tool)

# 生图工具可选：没有 ComfyUI 的机器用 ENABLE_IMAGE_GEN=false 关掉
from app.config import ENABLE_IMAGE_GEN
if ENABLE_IMAGE_GEN:
    from app.tools.normal.generate_image import tool as _generate_image_tool
    register_tool(**_generate_image_tool)
    from app.tools.normal.comfy_workflow import tool as _cw_info, tool_update as _cw_update
    register_tool(**_cw_info)
    register_tool(**_cw_update)

# QQ 主动推送工具：只有开了 QQ 接入才注册。没配 QQ 的机器不该让模型看见
# 一个必然失败的工具（与 ENABLE_IMAGE_GEN 同一取舍）
from app.config import QQ_ENABLE
if QQ_ENABLE:
    from app.tools.normal.send_qq_message import tool as _send_qq_tool
    register_tool(**_send_qq_tool)
    from app.tools.normal.send_sticker import tool as _send_sticker_tool
    register_tool(**_send_sticker_tool)
    from app.tools.normal.delete_sticker import tool as _delete_sticker_tool
    register_tool(**_delete_sticker_tool)

from app.tools.normal.list_skills import tool as _list_skills_tool
register_tool(**_list_skills_tool)

from app.tools.normal.list_files import tool as _list_files_tool
register_tool(**_list_files_tool)

from app.tools.normal.read_file import tool as _read_file_tool
register_tool(**_read_file_tool)

from app.tools.normal.write_file import tool as _write_file_tool
register_tool(**_write_file_tool)

from app.tools.normal.delete_file import tool as _delete_file_tool
register_tool(**_delete_file_tool)

from app.tools.normal.documents import (
    tool_list as _doc_list_tool,
    tool_info as _doc_info_tool,
    tool_read as _doc_read_tool,
    tool_search as _doc_search_tool,
)
register_tool(**_doc_list_tool)
register_tool(**_doc_info_tool)
register_tool(**_doc_read_tool)
register_tool(**_doc_search_tool)

# ─── RAG 知识库工具 ───────────────────────────────────
from app.tools.rag.kb_tools import (
    TOOL_SCHEMA as _KB_SCHEMA,
    search_kb as _search_kb_fn,
    ingest_kb as _ingest_kb_fn,
    list_kb as _list_kb_fn,
    delete_kb as _delete_kb_fn,
    delete_entries as _delete_entries_fn,
    chunk_document as _chunk_doc_fn,
)

# search_kb
register_tool(
    name="search_kb",
    description=_KB_SCHEMA["search_kb"]["function"]["description"],
    function=_search_kb_fn,
    parameters=_KB_SCHEMA["search_kb"]["function"]["parameters"],
)

# ingest_kb
register_tool(
    name="ingest_kb",
    description=_KB_SCHEMA["ingest_kb"]["function"]["description"],
    function=_ingest_kb_fn,
    parameters=_KB_SCHEMA["ingest_kb"]["function"]["parameters"],
)

# list_kb
register_tool(
    name="list_kb",
    description=_KB_SCHEMA["list_kb"]["function"]["description"],
    function=_list_kb_fn,
    parameters=_KB_SCHEMA["list_kb"]["function"].get("parameters", {"type": "object", "properties": {}}),
)

# delete_kb
register_tool(
    name="delete_kb",
    description=_KB_SCHEMA["delete_kb"]["function"]["description"],
    function=_delete_kb_fn,
    parameters=_KB_SCHEMA["delete_kb"]["function"]["parameters"],
)

# delete_entries
register_tool(
    name="delete_entries",
    description=_KB_SCHEMA["delete_entries"]["function"]["description"],
    function=_delete_entries_fn,
    parameters=_KB_SCHEMA["delete_entries"]["function"]["parameters"],
)

# chunk_document
register_tool(
    name="chunk_document",
    description=_KB_SCHEMA["chunk_document"]["function"]["description"],
    function=_chunk_doc_fn,
    parameters=_KB_SCHEMA["chunk_document"]["function"]["parameters"],
)