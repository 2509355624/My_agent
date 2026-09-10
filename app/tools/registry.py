"""
工具注册表
所有工具在这里注册，Agent 通过 execute_tool 统一调用
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


# 导入并注册所有工具
from app.tools.get_time import tool as _get_time_tool
register_tool(**_get_time_tool)

from app.tools.web_search import tool as _web_search_tool
register_tool(**_web_search_tool)

from app.tools.load_skill import tool as _load_skill_tool
register_tool(**_load_skill_tool)

from app.tools.generate_image import tool as _generate_image_tool
register_tool(**_generate_image_tool)

from app.tools.list_skills import tool as _list_skills_tool
register_tool(**_list_skills_tool)

from app.tools.read_file import tool as _read_file_tool
register_tool(**_read_file_tool)

from app.tools.write_file import tool as _write_file_tool
register_tool(**_write_file_tool)

from app.tools.documents import (
    tool_list as _doc_list_tool,
    tool_info as _doc_info_tool,
    tool_read as _doc_read_tool,
    tool_search as _doc_search_tool,
)
register_tool(**_doc_list_tool)
register_tool(**_doc_info_tool)
register_tool(**_doc_read_tool)
register_tool(**_doc_search_tool)

# RAG 向量知识库工具
from app.rag.kb_tools import (
    TOOL_SCHEMA as _KB_SCHEMA,
    search_kb as _search_kb_fn,
    ingest_kb as _ingest_kb_fn,
    list_kb as _list_kb_fn,
    delete_kb as _delete_kb_fn,
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

# chunk_document
register_tool(
    name="chunk_document",
    description=_KB_SCHEMA["chunk_document"]["function"]["description"],
    function=_chunk_doc_fn,
    parameters=_KB_SCHEMA["chunk_document"]["function"]["parameters"],
)
