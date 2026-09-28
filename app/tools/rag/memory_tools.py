# -*- coding: utf-8 -*-
"""QQ 私聊「通用长期记忆」向量库工具。

按 QQ 号隔离：每个 QQ 一个 collection（memory_<QQ号>）。QQ 号从
qq_api.current_context() 取，绝不接受模型传入——模型无法跨 QQ 读别人的记忆。
群聊 / 网页端（无 QQ 上下文）一律拒绝。存入自动打标签（tags），便于以后按主题
检索与归类。底层复用 RAG 栈（ChromaDB + BGE-M3 + RagClient）。
"""

import time
import uuid

from app.qq_api import current_context
from app.rag.rag_client import RagClient
from app.rag.vector_store import safe_collection_name

# 记忆 collection 前缀
_MEMORY_PREFIX = "memory_"


def _get_client():
    return RagClient.get()


def _caller_qq():
    """返回当前私聊 QQ 号；非私聊 / 非 QQ 会话返回 (None, 拒绝原因)。"""
    target, target_id = current_context()
    if target != "private" or not target_id:
        return None, "记忆功能仅限 QQ 私聊使用（群聊和网页端不支持）。"
    return str(target_id), None


def _kb_for(qq):
    """当前 QQ 的记忆 collection 名。"""
    return safe_collection_name(_MEMORY_PREFIX + str(qq))


def _parse_tags(tags):
    """把逗号/顿号/空格分隔的标签串解析成列表。"""
    if not tags:
        return []
    parts = [t.strip() for t in str(tags).replace("，", ",").replace("、", ",").split(",")]
    return [t for t in parts if t]


def _content_with_tags(text, tag_list):
    """正文里带上标签，语义检索也能命中标签词。"""
    if tag_list:
        return text.strip() + "\n标签: " + "、".join(tag_list)
    return text.strip()


def _fmt_entry(entry):
    """把一条记忆渲染成可读文本（列 / 搜结果展示用）。"""
    meta = entry.get("metadata") or {}
    eid = entry.get("id")
    tags = meta.get("tags") or []
    lines = ["【记忆】" + str(eid)]
    if tags:
        lines.append("  标签: " + "、".join(tags))
    if meta.get("note"):
        lines.append("  备注: " + meta["note"])
    lines.append("  内容: " + (meta.get("content") or entry.get("content") or ""))
    return "\n".join(lines)


# ─── 工具 ───────────────────────────────────────────

def memory_save(content, tags="", note="", id=None):
    """保存一条长期记忆到当前私聊 QQ 的私有向量库（自动打标签）。"""
    qq, err = _caller_qq()
    if err:
        return err
    content = (content or "").strip()
    if not content:
        return "错误: 记忆内容(content)不能为空"
    tag_list = _parse_tags(tags)
    eid = (id or "").strip() or ("mem_" + uuid.uuid4().hex[:12])
    entry = {
        "id": eid,
        "content": _content_with_tags(content, tag_list),
        "metadata": {
            "content": content,
            "tags": tag_list,
            "note": (note or "").strip(),
            "created_at": int(time.time()),
            "updated_at": int(time.time()),
        },
    }
    result = _get_client().ingest(_kb_for(qq), [entry])
    if not result.get("ok"):
        return "[保存失败] " + result.get("error", "未知错误")
    tag_hint = ("（标签: " + "、".join(tag_list) + "）") if tag_list else "（未打标签）"
    return ("[记忆] 已保存一条到 QQ %s 的私有记忆库 %s。"
            "用 memory_search 检索、memory_list 查看全部。" % (qq, tag_hint))


def memory_search(query, k=3):
    """在当前私聊 QQ 自己已存的记忆里语义搜索。"""
    qq, err = _caller_qq()
    if err:
        return err
    query = (query or "").strip()
    if not query:
        return "错误: 搜索内容(query)不能为空"

    result = _get_client().search(_kb_for(qq), query, k=int(k or 3))
    if not result.get("ok"):
        return "[搜索失败] " + result.get("error", "未知错误")
    docs = result.get("docs", [])
    if not docs:
        return "[记忆] QQ %s 没有匹配的记忆" % qq
    lines = ["[记忆搜索结果 · 找到 %d 条]" % len(docs)]
    for doc in docs:
        lines.append(_fmt_entry(doc))
        lines.append("")
    return "\n".join(lines)


def memory_list():
    """列出当前私聊 QQ 记忆库里的全部条目（含标签与 id）。"""
    qq, err = _caller_qq()
    if err:
        return err

    result = _get_client().list_entries(_kb_for(qq))
    if not result.get("ok"):
        return "[获取失败] " + result.get("error", "未知错误")
    entries = result.get("entries", [])
    if not entries:
        return ("[记忆] QQ %s 的记忆库还是空的。跟我说『记一下…』或分享值得"
                "长期留存的偏好/计划/决定/链接，我就存进来。" % qq)
    lines = ["[记忆列表 · QQ %s · 共 %d 条]" % (qq, len(entries))]
    for entry in entries:
        lines.append(_fmt_entry(entry))
        lines.append("")
    return "\n".join(lines)


def memory_delete(id):
    """删除当前私聊 QQ 记忆库里的一条（id 取自 memory_list）。"""
    qq, err = _caller_qq()
    if err:
        return err
    id = (id or "").strip()
    if not id:
        return "错误: 要删除的记忆 id 不能为空（先 memory_list 拿到 id）"

    result = _get_client().delete_entry(_kb_for(qq), id)
    if not result.get("ok"):
        return "[删除失败] " + result.get("error", "未知错误")
    if result.get("deleted"):
        return "[记忆 %s] 已删除（QQ %s）" % (id, qq)
    return "[记忆 %s] 未找到（QQ %s），可能已被删除" % (id, qq)


# ─── 工具 schema（供 registry 注册）──────────────────

TOOL_SCHEMA = {
    "memory_save": {
        "type": "function",
        "function": {
            "name": "memory_save",
            "description": (
                "把用户想长期记住的东西存进**当前私聊这个人专属**的向量记忆库。"
                "当用户明确说『记一下/存起来/帮我记住』，或主动分享明显值得长期留存的"
                "偏好、计划、决定、链接、账号信息、事实时，**主动调用**（这是默认动作，"
                "别说『要不要我帮你记』，直接记）。自动从内容提炼 2-4 个标签"
                "（类别/主题/相关人）填到 tags；用户指定的标签也带上。content 必填，"
                "note 可选补一句说明。仅限 QQ 私聊，且记忆只属于当前私聊这个人。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "要记住的正文内容（必填）"
                    },
                    "tags": {
                        "type": "string",
                        "description": "标签，逗号或顿号分隔，如『工作,计划,小张』；留空则由 AI 自动提炼"
                    },
                    "note": {
                        "type": "string",
                        "description": "备注（可选），给这条记忆加的补充说明"
                    }
                },
                "required": ["content"]
            }
        }
    },
    "memory_search": {
        "type": "function",
        "function": {
            "name": "memory_search",
            "description": (
                "在用户自己已保存的记忆里做语义搜索。当用户问『我之前是不是说过…』"
                "『你记了我什么关于 XX 的』，或你想回忆某件事时调用，返回匹配的记忆"
                "及其标签，方便接着聊。仅限 QQ 私聊。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "搜索描述，尽量说清想找哪类记忆"
                    },
                    "k": {
                        "type": "integer",
                        "description": "返回条数，默认 3",
                        "default": 3
                    }
                },
                "required": ["query"]
            }
        }
    },
    "memory_list": {
        "type": "function",
        "function": {
            "name": "memory_list",
            "description": (
                "列出当前 QQ 记忆库里的全部条目（含标签与 id）。用户问『你都记了我什么』"
                "或你想确认有哪些记忆时调用；删除前也先列出来拿 id。仅限 QQ 私聊。"
            ),
            "parameters": {
                "type": "object",
                "properties": {}
            }
        }
    },
    "memory_delete": {
        "type": "function",
        "function": {
            "name": "memory_delete",
            "description": (
                "删除当前 QQ 记忆库里的一条（id 取自 memory_list 的输出）。"
                "用户明确说『删掉那条关于 XX 的记忆』时，先 memory_list 找到 id 再调。"
                "仅限 QQ 私聊。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {
                        "type": "string",
                        "description": "要删除的记忆 id（先 memory_list 拿到）"
                    }
                },
                "required": ["id"]
            }
        }
    },
}
