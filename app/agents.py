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

from app.config import AGENTS_DIR, DEFAULT_AGENT_ID, PROVIDERS


# 字母数字开头，后跟字母数字 / 下划线 / 连字符；限长防超长文件名。
_AGENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

# agent.json 的默认值。tools / skills 为 None 表示「不限制」。
# provider / model 为空串表示「继承 .env 里的全局默认」，这样没写过这两个
# 字段的 agent（以及所有老 agent.json）行为与从前完全一致。
# context_budget 为 0 表示「继承 .env 的 CONTEXT_BUDGET」，同上。
_DEFAULT_CONFIG = {
    "name": "",
    "description": "",
    "prompt": "",
    "prompt_file": "prompt.md",
    "tools": None,
    "skills": None,
    "provider": "",
    "model": "",
    "context_budget": 0,
}

# context_budget 的合法区间。低于下限压缩得太频繁（每轮都在摘要，反而更贵），
# 高于上限就等于没设；越界与写错一律归 0 → 继承全局，而不是报错拦住保存。
MIN_CONTEXT_BUDGET = 4000
MAX_CONTEXT_BUDGET = 1_000_000

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


# 子会话 key 的字符白名单：字母数字开头，后跟字母数字 / 下划线 / 连字符。
# 不含路径分隔符，也不含点（挡掉 ".."），所以拼进路径不会逃逸。
_SESSION_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def safe_session_key(key):
    """校验子会话 key；非法返回 None。

    用于 QQ 这类「一个 agent 下挂多条独立会话线」的场景（每个私聊用户 /
    每个群各一条），key 会被拼进文件名，所以做白名单校验。字符集本身已经
    排除了路径分隔符和点，拼出来不可能逃出 sessions/ 目录，无需再做
    realpath —— agent_id 那一层的穿越防线在 safe_agent_id 里。
    """
    if not isinstance(key, str):
        return None
    k = key.strip()
    if not _SESSION_KEY_RE.match(k):
        return None
    return k


def session_file(agent_id, key=None):
    """该 agent 的会话文件路径（不保证存在，写入时会自动建目录）。

    key 为 None 时是 agent 的主会话（session.jsonl），网页端用的就是这条。
    传 key 时落到 sessions/<key>.jsonl，供一个 agent 承载多条互不相干的
    会话线使用（QQ 适配层按「私聊用户 / 群」分线）。key 非法则退回主会话
    —— 存不下来比抛异常打断一轮对话更糟。
    """
    aid = safe_agent_id(agent_id) or DEFAULT_AGENT_ID
    if key:
        safe = safe_session_key(key)
        if safe is not None:
            return os.path.join(AGENTS_DIR, aid, "sessions", safe + ".jsonl")
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

    # provider / model 留空串 = 继承 .env 的全局默认。
    # provider 写错（不在 PROVIDERS 里）一律清空而不是原样保留：留着会让请求
    # 带着一个不存在的名字走到 llm 层，再被静默回退到全局默认，界面显示的和
    # 实际在用的就对不上了。model 则刻意不做校验——模型名由各家平台决定，
    # 写错时让它带着 404 报出来（llm 层有专门的接入点提示），比悄悄回退有用。
    for key in ("provider", "model"):
        val = out.get(key)
        out[key] = val.strip() if isinstance(val, str) else ""
    _prov = out["provider"].lower()
    out["provider"] = _prov if _prov in PROVIDERS else ""

    # context_budget：0 = 继承全局。只接受区间内的整数——写错（空串、非数字、
    # 越界）一律归 0，不让一个手滑的数字把压缩彻底关掉、或调到每轮都摘要。
    # 容忍字符串形式的数字（"32000"），手改 json 时不必纠结类型。
    try:
        n = int(out.get("context_budget"))
    except (TypeError, ValueError):
        n = 0
    if n and not (MIN_CONTEXT_BUDGET <= n <= MAX_CONTEXT_BUDGET):
        n = 0
    out["context_budget"] = n

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


def persona_path(agent_id):
    """人设文件的绝对路径；agent_id 非法时返回 None。

    prompt_file 不允许带路径分隔符——否则在 agent.json 里写一个 ../.. 就能
    指到 agent 目录外面去。读取与保存两边都必须走这个函数，防穿越规则才
    不会各写一份、日后只改了一处。
    """
    aid = safe_agent_id(agent_id)
    if aid is None:
        return None
    fname = agent_config(aid)["prompt_file"]
    if os.path.basename(fname) != fname:
        fname = DEFAULT_PROMPT_FILE
    return os.path.join(AGENTS_DIR, aid, fname)


def persona_text(agent_id):
    """人设文本：优先 prompt_file（默认 prompt.md），其次 agent.json 的 prompt 字段。

    文件不存在时返回空串，调用方用默认角色定义兜底。
    """
    aid = safe_agent_id(agent_id)
    if aid is None:
        return ""

    cfg = agent_config(aid)
    path = persona_path(aid)
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


def _atomic_write_text(path, text):
    """临时文件 + fsync + os.replace 的原子写。

    和 memory.save_history 同一套路：直接 open("w") 覆盖时，写到一半进程
    被杀就留下半截文件，下次读直接解析失败。
    """
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def agent_raw_config(agent_id):
    """磁盘上 agent.json 的原始内容，不做补默认值与规范化。

    给「后台改配置」用：读-改-写必须以磁盘原文为底，只覆盖用户真正改过的
    字段。若拿规范化后的 dict 回写，那些界面上没暴露的字段（tools / skills
    等）会被默认值冲掉。目录缺失或文件损坏时返回空 dict。
    """
    aid = safe_agent_id(agent_id)
    if aid is None:
        return {}
    path = os.path.join(AGENTS_DIR, aid, "agent.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def save_agent_config(agent_id, cfg):
    """原子写入 agent.json，并清掉配置缓存。

    清缓存不是可选项：mtime 精度有限，「读 → 写 → 立刻再读」有可能命中旧
    缓存，表现就是「点了保存但没生效」。配置本身很小，重读一次可忽略。
    """
    aid = safe_agent_id(agent_id)
    if aid is None or not isinstance(cfg, dict):
        return False
    d = os.path.join(AGENTS_DIR, aid)
    os.makedirs(d, exist_ok=True)
    _atomic_write_text(os.path.join(d, "agent.json"),
                       json.dumps(cfg, ensure_ascii=False, indent=2) + "\n")
    clear_cache()
    return True


def save_persona(agent_id, text):
    """原子写入人设文件（默认 prompt.md），并清掉人设缓存。"""
    aid = safe_agent_id(agent_id)
    if aid is None or not isinstance(text, str):
        return False
    path = persona_path(aid)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    _atomic_write_text(path, text)
    _persona_cache.pop(aid, None)
    return True


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
            # 空串 = 继承全局默认。管理页用它标出「谁单独配过模型」
            "provider": cfg["provider"],
            "model": cfg["model"],
            # 0 = 继承全局预算，同样是「谁单独配过」的标记
            "context_budget": cfg["context_budget"],
        })

    items.sort(key=lambda a: (a["id"] != DEFAULT_AGENT_ID, a["id"]))
    return items
