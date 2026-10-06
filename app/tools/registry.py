"""
工具注册表
所有工具在这里注册（普通工具 + RAG 工具），Agent 通过 execute_tool 统一调用。

目录结构：
  app/tools/normal/  普通工具（时间、搜索、文件、生图、Skill 管理、文档）
  app/tools/rag/     RAG 向量知识库工具（检索、入库、切块）
  app/rag/           RAG 引擎层（向量存储、embedding、daemon，非 Agent 工具）
"""

TOOLS = []


def register_tool(name, description, function, parameters,
                  description_overrides=None, hidden_params=None):
    """注册一个工具。

    description_overrides / hidden_params 是可选的按 agent 定制：
    {agent_id: 描述} / {agent_id: [要藏起来的参数名]}。agent_prompt
    构建 prompt 时按调用方 agent 挑着用（如 QQ 机器人不显示角色底模）。
    """
    entry = {
        "name": name,
        "description": description,
        "function": function,
        "parameters": parameters,
    }
    if description_overrides:
        entry["description_overrides"] = description_overrides
    if hidden_params:
        entry["hidden_params"] = hidden_params
    TOOLS.append(entry)


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
    # 按编号查回「那张图当初用的什么提示词」：靠的是发图时贴在消息上的编号
    from app.tools.normal.recall_image import tool as _recall_image_tool
    register_tool(**_recall_image_tool)
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
    from app.tools.normal.collect_sticker import tool as _collect_sticker_tool
    register_tool(**_collect_sticker_tool)

    # QQ 私聊「文生图预设」向量库：仅 QQ 会话可用（群聊/网页端由工具内部拒绝）
    from app.tools.rag.preset_tools import (
        TOOL_SCHEMA as _PRESET_SCHEMA,
        preset_save as _preset_save_fn,
        preset_search as _preset_search_fn,
        preset_list as _preset_list_fn,
        preset_delete as _preset_delete_fn,
    )
    for _pname, _pfn in (
        ("preset_save", _preset_save_fn),
        ("preset_search", _preset_search_fn),
        ("preset_list", _preset_list_fn),
        ("preset_delete", _preset_delete_fn),
    ):
        register_tool(
            name=_pname,
            description=_PRESET_SCHEMA[_pname]["function"]["description"],
            function=_pfn,
            parameters=_PRESET_SCHEMA[_pname]["function"]["parameters"],
        )

    # QQ 私聊「通用长期记忆」向量库：按 QQ 号隔离，自动打标签，仅 QQ 私聊可用
    from app.tools.rag.memory_tools import (
        TOOL_SCHEMA as _MEMORY_SCHEMA,
        memory_save as _memory_save_fn,
        memory_search as _memory_search_fn,
        memory_list as _memory_list_fn,
        memory_delete as _memory_delete_fn,
    )
    for _mname, _mfn in (
        ("memory_save", _memory_save_fn),
        ("memory_search", _memory_search_fn),
        ("memory_list", _memory_list_fn),
        ("memory_delete", _memory_delete_fn),
    ):
        register_tool(
            name=_mname,
            description=_MEMORY_SCHEMA[_mname]["function"]["description"],
            function=_mfn,
            parameters=_MEMORY_SCHEMA[_mname]["function"]["parameters"],
        )

from app.tools.normal.list_skills import tool as _list_skills_tool
register_tool(**_list_skills_tool)

from app.tools.normal.list_files import tool as _list_files_tool
register_tool(**_list_files_tool)

from app.tools.normal.read_file import tool as _read_file_tool
register_tool(**_read_file_tool)

# grep_file：skills 内文本文件按行检索（正则/子串 + 可选限定列），
# 专为 anima-tags 标签库查询设计，但通用。比 read_file 整表读省上下文。
from app.tools.normal.grep_file import tool as _grep_file_tool
register_tool(**_grep_file_tool)

# search_tags：标签库结构化检索（内存索引 + 中文滑窗最长匹配）。
# 比 grep_file 快约 2 万倍（3~5 µs vs 77 ms），返回候选列表而非原始行。
# 定位是**召回**：中文名歧义严重（初音未来 111 个候选），挑选交给 LLM。
from app.tools.normal.search_tags import tool as _search_tags_tool
register_tool(**_search_tags_tool)

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