# -*- coding: utf-8 -*-
"""RAG 向量知识库工具。

支持多知识库隔离（按 kb_name 区分），Agent 可以：
- 检索知识库内容
- 将文档片段存入知识库
- 列出/删除知识库
"""

import hashlib
import re
from typing import Any


TOOL_SCHEMA = {
    "search_kb": {
        "type": "function",
        "function": {
            "name": "search_kb",
            "description": "在指定的向量知识库中搜索相关内容。用于从已有知识库中快速查找资料、引用、知识点。支持多个独立知识库（如面试、生图技能、通用文档等），通过 kb_name 指定。",
            "parameters": {
                "type": "object",
                "properties": {
                    "kb_name": {
                        "type": "string",
                        "description": "知识库名称。常用：'documents'（用户上传的通用文档）、'skills'（生图技能规范）、'interview'（面试知识）。如果不确定用哪个，先调用 list_kb 查看。"
                    },
                    "query": {
                        "type": "string",
                        "description": "搜索查询，尽量用完整的问句或关键词描述"
                    },
                    "k": {
                        "type": "integer",
                        "description": "返回结果数量，默认 4",
                        "default": 4
                    }
                },
                "required": ["kb_name", "query"]
            }
        }
    },
    "ingest_kb": {
        "type": "function",
        "function": {
            "name": "ingest_kb",
            "description": "将内容片段存入指定的向量知识库。存入后可以通过 search_kb 检索。适合把长文档、技能规范、资料等切块后入库，建立长期记忆。",
            "parameters": {
                "type": "object",
                "properties": {
                    "kb_name": {
                        "type": "string",
                        "description": "知识库名称，如 'documents'、'skills'、'interview'"
                    },
                    "entries": {
                        "type": "array",
                        "description": "要存入的片段列表，每个片段是一个对象",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {
                                    "type": "string",
                                    "description": "片段唯一标识，建议格式：'文件名#章节标题' 或 '来源#序号'"
                                },
                                "content": {
                                    "type": "string",
                                    "description": "片段正文内容，建议 200-800 字，语义完整"
                                },
                                "metadata": {
                                    "type": "object",
                                    "description": "元数据，如 source（来源）、section（章节）、tags（标签）等",
                                    "additionalProperties": True
                                }
                            },
                            "required": ["id", "content"]
                        }
                    }
                },
                "required": ["kb_name", "entries"]
            }
        }
    },
    "list_kb": {
        "type": "function",
        "function": {
            "name": "list_kb",
            "description": "列出所有可用的向量知识库及其条目数量，用于选择合适的知识库。"
        }
    },
    "delete_kb": {
        "type": "function",
        "function": {
            "name": "delete_kb",
            "description": "删除整个知识库（清空所有内容）。慎用！删除后不可恢复。",
            "parameters": {
                "type": "object",
                "properties": {
                    "kb_name": {
                        "type": "string",
                        "description": "要删除的知识库名称"
                    }
                },
                "required": ["kb_name"]
            }
        }
    },
    "delete_entries": {
        "type": "function",
        "function": {
            "name": "delete_entries",
            "description": "批量删除知识库中指定 id 的片段（ChromaDB 原生批量删除）。用于清理错入库或过期的内容，需先通过 search_kb 或查询确认要删除的片段 id。",
            "parameters": {
                "type": "object",
                "properties": {
                    "kb_name": {
                        "type": "string",
                        "description": "知识库名称"
                    },
                    "entry_ids": {
                        "type": "array",
                        "description": "要删除的片段 id 列表（可一次传多个，批量删除）",
                        "items": {"type": "string"}
                    }
                },
                "required": ["kb_name", "entry_ids"]
            }
        }
    },
    "chunk_document": {
        "type": "function",
        "function": {
            "name": "chunk_document",
            "description": "将长文档内容按语义切块（按标题/段落切分，每块约 500-800 字），为 ingest_kb 准备数据。返回切块后的列表，可直接传入 ingest_kb。",
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "文档完整内容（Markdown 或纯文本）"
                    },
                    "source": {
                        "type": "string",
                        "description": "来源标识（如文件名），用于生成片段 id 和 metadata"
                    },
                    "chunk_size": {
                        "type": "integer",
                        "description": "每块目标字数，默认 600",
                        "default": 600
                    }
                },
                "required": ["content", "source"]
            }
        }
    }
}


def _get_client():
    from app.rag.rag_client import RagClient
    return RagClient.get()


def search_kb(kb_name: str, query: str, k: int = 4) -> str:
    """在向量知识库中搜索相关内容。"""
    client = _get_client()
    result = client.search(kb_name, query, k=k)

    if not result.get("ok"):
        return f"[搜索失败] {result.get('error', '未知错误')}"

    docs = result.get("docs", [])
    if not docs:
        return f"[知识库 '{kb_name}'] 未找到相关内容"

    lines = [f"[知识库 '{kb_name}' 搜索结果 · 找到 {len(docs)} 条]\n"]
    for i, doc in enumerate(docs, 1):
        meta = doc.get("metadata", {})
        source = meta.get("source", doc.get("id", f"片段{i}"))
        score = 1.0 - doc.get("distance", 1.0)
        lines.append(f"--- 结果 {i}（相关度: {score:.3f}，来源: {source}）---")
        lines.append(doc.get("content", ""))
        lines.append("")
    return "\n".join(lines)


def ingest_kb(kb_name: str, entries: list[dict[str, Any]]) -> str:
    """将内容片段存入向量知识库。"""
    client = _get_client()
    result = client.ingest(kb_name, entries)

    if not result.get("ok"):
        return f"[入库失败] {result.get('error', '未知错误')}"

    count = result.get("count", 0)
    return f"[知识库 '{kb_name}'] 成功存入 {count} 条片段"


def list_kb() -> str:
    """列出所有知识库。"""
    client = _get_client()
    result = client.list_kb()

    if not result.get("ok"):
        return f"[获取失败] {result.get('error', '未知错误')}"

    cols = result.get("collections", [])
    counts = result.get("counts", {})
    if not cols:
        return "[知识库] 暂无知识库。使用 ingest_kb 创建并存入内容。"

    lines = ["[知识库列表]"]
    for c in cols:
        cnt = counts.get(c, 0)
        lines.append(f"  - {c}: {cnt} 条")
    return "\n".join(lines)


def delete_kb(kb_name: str) -> str:
    """删除知识库。"""
    client = _get_client()
    result = client.delete_kb(kb_name)

    if not result.get("ok"):
        return f"[删除失败] {result.get('error', '未知错误')}"

    deleted = result.get("deleted", False)
    if deleted:
        return f"[知识库 '{kb_name}'] 已删除"
    return f"[知识库 '{kb_name}'] 删除失败（可能不存在）"


def delete_entries(kb_name: str, entry_ids: list[str]) -> str:
    """批量删除指定 id 的片段（ChromaDB 原生批量删除，非 for 循环）。"""
    ids = [i for i in (entry_ids or []) if i]
    if not ids:
        return "错误: entry_ids 不能为空"
    client = _get_client()
    result = client.delete_entries(kb_name, ids)

    if not result.get("ok"):
        return f"[删除失败] {result.get('error', '未知错误')}"
    deleted = result.get("deleted", 0)
    return f"[知识库 '{kb_name}'] 批量删除 {deleted} 条片段"


def chunk_document(content: str, source: str, chunk_size: int = 600) -> str:
    """将长文档按语义切块，返回 JSON 格式的切块列表。

    策略：
    1. 优先按 Markdown 标题（##/###）切分
    2. 每个标题块内，按段落合并到 chunk_size 左右
    3. 保证语义完整性
    """
    lines = content.split("\n")
    chunks: list[dict] = []

    # 按标题切分章节
    sections: list[tuple[str, list[str]]] = []  # (heading, lines)
    current_heading = "前言"
    current_lines: list[str] = []

    for line in lines:
        if re.match(r"^#{1,3}\s+", line):
            if current_lines:
                sections.append((current_heading, current_lines))
            current_heading = line.strip()
            current_lines = []
        else:
            current_lines.append(line)
    if current_lines:
        sections.append((current_heading, current_lines))

    # 每个章节内按字数合并段落
    chunk_idx = 0
    for heading, sec_lines in sections:
        # 先按段落分组（空行分隔）
        paragraphs: list[str] = []
        current_para: list[str] = []
        for line in sec_lines:
            if line.strip() == "" and current_para:
                paragraphs.append("\n".join(current_para).strip())
                current_para = []
            else:
                current_para.append(line)
        if current_para:
            paragraphs.append("\n".join(current_para).strip())

        # 合并段落成 chunk
        current_text = ""
        current_paras: list[str] = []
        for para in paragraphs:
            if not para.strip():
                continue
            if len(current_text) + len(para) + 2 <= chunk_size and current_paras:
                current_paras.append(para)
                current_text = "\n\n".join(current_paras)
            else:
                if current_paras:
                    chunk_idx += 1
                    chunk_id = f"{source}#{heading}#{chunk_idx}"
                    chunks.append({
                        "id": chunk_id,
                        "content": current_text,
                        "metadata": {
                            "source": source,
                            "section": heading,
                            "chunk_index": chunk_idx,
                        }
                    })
                current_paras = [para]
                current_text = para

        if current_paras:
            chunk_idx += 1
            chunk_id = f"{source}#{heading}#{chunk_idx}"
            chunks.append({
                "id": chunk_id,
                "content": current_text,
                "metadata": {
                    "source": source,
                    "section": heading,
                    "chunk_index": chunk_idx,
                }
            })

    # 返回 JSON 字符串（方便 Agent 看到结构后传给 ingest_kb）
    import json
    summary = {
        "total_chunks": len(chunks),
        "source": source,
        "chunk_size": chunk_size,
        "chunks": chunks[:5],  # 只展示前 5 个预览
        "note": "以上为前 5 个片段预览。使用 ingest_kb 时传入完整 chunks 列表（你可以从内容中重建）。"
    }
    return json.dumps(summary, ensure_ascii=False, indent=2)
