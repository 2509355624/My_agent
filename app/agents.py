"""
Agent 定义与解析

每个 agent 是 agents/ 下的一个自包含目录：

    agents/<id>/
        agent.json     配置（显示名、工具 / skills 白名单）
        prompt.md      角色人设
        session.jsonl  会话历史

设计要点：
1. 配置与人设都按文件 mtime 缓存 → 改 agent.json / prompt.md、甚至新建
   agent 目录，刷新页面即可生效，不需要重启服务（热加载）。
2. agent_id 会直接参与文件路径，必须先过 safe_agent_id()：既做字符白名单，
   也做路径穿越防线（解析后必须确实落在 AGENTS_DIR 的直接子目录里）。
3. 配置坏了、目录缺了都退回默认值，不让一个写错的 json 把整个服务带崩。
"""

import json
import os
import re

from app.config import AGENTS_DIR, DEFAULT_AGENT_ID


# 字母数字开头，后跟字母数字 / 下划线 / 连字符；限长防超长文件名。
_AGENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

# agent.json 的默认值。tools / skills 为 None 表示「不限制」。
_DEFAULT_CONFIG = {
    "name": "",
    "description": "",
    "prompt": "",
    "prompt_file": "prompt.md",
    "tools": None,
    "skills": None,
}

DEFAULT_PROMPT_FILE = "prompt.md"

# {agent_id: (mtime, config)} / {agent_id: (mtime, text)}
_cfg_cache = {}
_persona_cache = {}


def clear_cache():
    """清掉配置与人设缓存。

    测试用；也留给外部在特殊情况下强制重载（正常情况下靠 mtime 自动失效）。
    """
    _cfg_cache.clear()
    _persona_cache.clear()


def safe_agent_id(agent_id):
    """校验并规范化 agent_id；非法返回 None。

    agent_id 会被拼进文件路径，所以这里既是格式白名单（挡掉奇怪字符），
    也是路径穿越防线：realpath 解析后必须确实是 AGENTS_DIR 的直接子目录。
    """
    if not isinstance(agent_id, str):
        return None
    aid = agent_id.strip()
    if not _AGENT_ID_RE.match(aid):
        return None
    root = os.path.realpath(AGENTS_DIR)
    target = os.path.realpath(os.path.join(root, aid))
    if os.path.dirname(target) != root:
        return None
    return aid


def agent_dir(agent_id):
    """agent 目录绝对路径（不保证存在）；非法 id 返回 None。"""
    aid = safe_agent_id(agent_id)
    if aid is None:
        return None
    return os.path.join(AGENTS_DIR, aid)


def session_file(agent_id):
    """该 agent 的会话文件路径（不保证存在，写入时会自动建目录）。"""
    aid = safe_agent_id(agent_id) or DEFAULT_AGENT_ID
    return os.path.join(AGENTS_DIR, aid, "session.jsonl")


def resolve(agent_id):
    """把请求里的 agent_id 解析成可用的 agent id。

    非法、缺失、目录不存在 → 兜底到 DEFAULT_AGENT_ID。默认 agent 即使目录
    还没建也能用：写入会话时自动创建，人设为空则用默认角色定义。
    """
    aid = safe_agent_id(agent_id)
    if aid and os.path.isdir(os.path.join(AGENTS_DIR, aid)):
        return aid
    return DEFAULT_AGENT_ID


def _normalize(cfg):
    """规范化配置：tools / skills 收敛成 None 或去重后的字符串列表。"""
    out = dict(cfg)

    for key in ("tools", "skills"):
        val = out.get(key)
        if val is None:
            continue
        if not isinstance(val, (list, tuple)):
            # 写成了字符串之类的，视为「不限制」，比默默当成白名单更安全
            out[key] = None
            continue
        seen, items = set(), []
        for x in val:
            if isinstance(x, str) and x.strip() and x.strip() not in seen:
                seen.add(x.strip())
                items.append(x.strip())
        out[key] = items

    if not isinstance(out.get("prompt_file"), str) or not out["prompt_file"].strip():
        out["prompt_file"] = DEFAULT_PROMPT_FILE
    if not isinstance(out.get("prompt"), str):
        out["prompt"] = ""
    for key in ("name", "description"):
        if not isinstance(out.get(key), str):
            out[key] = ""
    return out


def agent_config(agent_id):
    """读取 agent.json（按 mtime 热加载），缺失字段用默认值补齐。

    读不到 / 解析失败都返回默认配置，而不是报错：agent 目录里只有
    session.jsonl 也应该能用（等价于全部工具、全部 skills）。
    """
    aid = safe_agent_id(agent_id)
    if aid is None:
        return dict(_DEFAULT_CONFIG)

    path = os.path.join(AGENTS_DIR, aid, "agent.json")
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return dict(_DEFAULT_CONFIG)

    cached = _cfg_cache.get(aid)
    if cached and cached[0] == mtime:
        return cached[1]

    cfg = dict(_DEFAULT_CONFIG)
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if isinstance(raw, dict):
            for k in _DEFAULT_CONFIG:
                if k in raw:
                    cfg[k] = raw[k]
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        cfg = dict(_DEFAULT_CONFIG)

    cfg = _normalize(cfg)
    _cfg_cache[aid] = (mtime, cfg)
    return cfg


def persona_text(agent_id):
    """人设文本：优先 prompt_file（默认 prompt.md），其次 agent.json 的 prompt 字段。

    文件不存在时返回空串，调用方用默认角色定义兜底。
    """
    aid = safe_agent_id(agent_id)
    if aid is None:
        return ""

    cfg = agent_config(aid)
    fname = cfg["prompt_file"]
    # prompt_file 同样不允许带路径分隔符，避免从 agent 目录里逃逸
    if os.path.basename(fname) != fname:
        fname = DEFAULT_PROMPT_FILE

    path = os.path.join(AGENTS_DIR, aid, fname)
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return cfg.get("prompt") or ""

    cached = _persona_cache.get(aid)
    if cached and cached[0] == mtime:
        return cached[1]

    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read().strip()
    except (OSError, UnicodeDecodeError):
        text = cfg.get("prompt") or ""

    _persona_cache[aid] = (mtime, text)
    return text


def revision(agent_id):
    """该 agent 配置的版本串（agent.json 与人设文件的 mtime 拼接）。

    给上层缓存用：system prompt 的稳定层把版本串算进指纹，于是改了配置
    或人设就自动重建，不用重启服务。
    """
    aid = safe_agent_id(agent_id)
    if aid is None:
        return "-"
    cfg = agent_config(aid)
    parts = [aid]
    for fname in ("agent.json", cfg["prompt_file"]):
        path = os.path.join(AGENTS_DIR, aid, fname)
        try:
            parts.append(str(os.path.getmtime(path)))
        except OSError:
            parts.append("0")
    return "|".join(parts)


def allows_tool(agent_id, name):
    """该 agent 是否允许使用某个工具（tools 为 None 表示全部允许）。"""
    limit = agent_config(agent_id)["tools"]
    return limit is None or name in limit


def allows_skill(agent_id, name):
    """该 agent 是否允许看到某个 skill（skills 为 None 表示全部允许）。"""
    limit = agent_config(agent_id)["skills"]
    return limit is None or name in limit


def list_agents():
    """列出所有 agent（默认 agent 排最前，其余按 id 排序）。

    每次都实时扫目录、不缓存：新建一个 agent 目录，刷新页面就能用。
    """
    if not os.path.isdir(AGENTS_DIR):
        return []

    items = []
    for name in os.listdir(AGENTS_DIR):
        path = os.path.join(AGENTS_DIR, name)
        if not os.path.isdir(path) or safe_agent_id(name) is None:
            continue
        cfg = agent_config(name)
        items.append({
            "id": name,
            "name": cfg["name"] or name,
            "description": cfg["description"],
            "tools": cfg["tools"],
            "skills": cfg["skills"],
        })

    items.sort(key=lambda a: (a["id"] != DEFAULT_AGENT_ID, a["id"]))
    return items
