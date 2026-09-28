# -*- coding: utf-8 -*-
"""QQ 私聊「文生图预设」向量库工具。

按 QQ 号隔离：每个 QQ 一个 collection（presets_<QQ号>）。QQ 号从
qq_api.current_context() 取，绝不接受模型传入——模型无法跨 QQ 读别人的预设。
群聊 / 网页端（无 QQ 上下文）一律拒绝。
"""

import time

from app.qq_api import current_context
from app.rag.rag_client import RagClient
from app.rag.vector_store import safe_collection_name

# 预设 collection 前缀
_PRESET_PREFIX = "presets_"


def _get_client():
    return RagClient.get()


def _caller_qq():
    """返回当前私聊 QQ 号；非私聊 / 非 QQ 会话返回 (None, 拒绝原因)。"""
    target, target_id = current_context()
    if target != "private" or not target_id:
        return None, "预设功能仅限 QQ 私聊使用（群聊和网页端不支持）。"
    return str(target_id), None


def _kb_for(qq):
    """当前 QQ 的预设 collection 名。"""
    return safe_collection_name(_PRESET_PREFIX + str(qq))


def _entry_to_text(prompt, negative_prompt, params, note):
    """把预设各字段拼成可语义检索的正文。"""
    parts = ["正向提示词: " + (prompt or "").strip()]
    if negative_prompt and negative_prompt.strip():
        parts.append("负向提示词: " + negative_prompt.strip())
    if params and params.strip():
        parts.append("参数: " + params.strip())
    if note and note.strip():
        parts.append("备注: " + note.strip())
    return "\n".join(parts)


def _fmt_entry(entry):
    """把一条预设渲染成可读文本（列 / 搜结果展示用）。"""
    meta = entry.get("metadata") or {}
    name = meta.get("name") or entry.get("id")
    lines = ["【预设】" + str(name)]
    if meta.get("prompt"):
        lines.append("  正向: " + meta["prompt"])
    if meta.get("negative_prompt"):
        lines.append("  负向: " + meta["negative_prompt"])
    if meta.get("params"):
        lines.append("  参数: " + meta["params"])
    if meta.get("note"):
        lines.append("  备注: " + meta["note"])
    return "\n".join(lines)


# ─── 工具 ───────────────────────────────────────────

def preset_save(name, prompt, negative_prompt="", params="", note=""):
    """保存 / 更新一条命名文生图预设（存到当前私聊 QQ 的私有向量库）。"""
    qq, err = _caller_qq()
    if err:
        return err
    name = (name or "").strip()
    prompt = (prompt or "").strip()
    if not name:
        return "错误: 预设名称(name)不能为空"
    if not prompt:
        return "错误: 预设必须包含正向提示词(prompt)"

    content = _entry_to_text(prompt, negative_prompt, params, note)
    entry = {
        "id": name,
        "content": content,
        "metadata": {
            "name": name,
            "prompt": prompt,
            "negative_prompt": (negative_prompt or "").strip(),
            "params": (params or "").strip(),
            "note": (note or "").strip(),
            "updated_at": int(time.time()),
        },
    }
    result = _get_client().ingest(_kb_for(qq), [entry])
    if not result.get("ok"):
        return "[保存失败] " + result.get("error", "未知错误")
    return ("[预设 '%s'] 已保存（QQ %s 私有）。用 preset_search / preset_list 查看，"
            "或说『用这个预设画一张』即可调用。" % (name, qq))


def preset_search(query, k=3):
    """在当前私聊 QQ 自己已保存的文生图预设里语义搜索。"""
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
        return "[预设] QQ %s 没有匹配的预设" % qq
    lines = ["[预设搜索结果 · 找到 %d 条]" % len(docs)]
    for doc in docs:
        lines.append(_fmt_entry(doc))
        lines.append("")
    return "\n".join(lines)


def preset_list():
    """列出当前私聊 QQ 已保存的全部文生图预设（名称 + 配置摘要）。"""
    qq, err = _caller_qq()
    if err:
        return err

    result = _get_client().list_entries(_kb_for(qq))
    if not result.get("ok"):
        return "[获取失败] " + result.get("error", "未知错误")
    entries = result.get("entries", [])
    if not entries:
        return ("[预设] QQ %s 还没有保存任何预设。跟我说"
                "『存个预设：名字=…，提示词=…』即可。" % qq)
    lines = ["[预设列表 · QQ %s · 共 %d 条]" % (qq, len(entries))]
    for entry in entries:
        lines.append(_fmt_entry(entry))
        lines.append("")
    return "\n".join(lines)


def preset_delete(name):
    """删除当前私聊 QQ 的一条命名预设。"""
    qq, err = _caller_qq()
    if err:
        return err
    name = (name or "").strip()
    if not name:
        return "错误: 预设名称(name)不能为空"

    result = _get_client().delete_entry(_kb_for(qq), name)
    if not result.get("ok"):
        return "[删除失败] " + result.get("error", "未知错误")
    if result.get("deleted"):
        return "[预设 '%s'] 已删除（QQ %s）" % (name, qq)
    return "[预设 '%s'] 未找到（QQ %s），可能已被删除" % (name, qq)


# ─── 工具 schema（供 registry 注册）──────────────────

TOOL_SCHEMA = {
    "preset_save": {
        "type": "function",
        "function": {
            "name": "preset_save",
            "description": (
                "在 QQ 私聊里保存或更新一条『文生图预设』（命名配置）。"
                "当用户明确要存一个生图配置，或主动表达某种想反复用的生图偏好"
                "（如『存个预设：名字=XX，提示词=…』『以后画二次元妹子都用这个提示词』）时调用，"
                "把正向提示词（必填）、负向提示词、参数、备注存起来。同名会覆盖（等于更新）。"
                "仅限 QQ 私聊，且预设只属于当前私聊这个人。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "预设名称（唯一标识，同名覆盖更新）"
                    },
                    "prompt": {
                        "type": "string",
                        "description": "正向提示词（必填），描述要画成什么样"
                    },
                    "negative_prompt": {
                        "type": "string",
                        "description": "负向提示词（可选），不想要的元素"
                    },
                    "params": {
                        "type": "string",
                        "description": "额外参数（可选），如分辨率/采样器/底模等"
                    },
                    "note": {
                        "type": "string",
                        "description": "备注（可选），给这条预设加的补充说明"
                    }
                },
                "required": ["name", "prompt"]
            }
        }
    },
    "preset_search": {
        "type": "function",
        "function": {
            "name": "preset_search",
            "description": (
                "在用户自己已保存的文生图预设里做语义搜索。当用户想『找一个之前存过的、"
                "关于…的预设』时调用，返回匹配的预设及其完整配置，方便接着拿去生图。"
                "仅限 QQ 私聊。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "搜索描述，尽量说清想要哪类预设"
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
    "preset_list": {
        "type": "function",
        "function": {
            "name": "preset_list",
            "description": (
                "列出当前 QQ 已保存的全部文生图预设（名称 + 配置摘要）。"
                "用户问『我存了哪些预设』或你想确认有哪些可选时调用。仅限 QQ 私聊。"
            ),
            "parameters": {
                "type": "object",
                "properties": {}
            }
        }
    },
    "preset_delete": {
        "type": "function",
        "function": {
            "name": "preset_delete",
            "description": (
                "删除当前 QQ 的一条命名预设。用户明确说『删掉 XX 预设』时调用。"
                "仅限 QQ 私聊。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "要删除的预设名称"
                    }
                },
                "required": ["name"]
            }
        }
    },
}
