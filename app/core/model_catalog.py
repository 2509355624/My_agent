"""按 provider 列出可用模型，供管理页与聊天页的预选清单使用。

为什么不写死在配置里：模型 ID 有生命周期。实测火山方舟的 `/models` 会给每一项
一个 `status`（字段缺失 = 在用 / `Retiring` / `Shutdown`），官方 DeepSeek 那边
只剩 `deepseek-flash` 与 `deepseek-v4-pro` 两个 ID。原先网页端硬编码的那份清单
里已经有退役 ID 和根本不存在
的 ID，而写错的症状是「切过去之后请求 404」——不报错、只是不回话，很难归因。

顺带白捡到一个能力标记：Ark 的 `task_type` 里有 `VisualQuestionAnswering`，
官方 DeepSeek 的 `input_modalities` 里有 `image`——都是「这个模型能不能读图」
的权威口径，比按模型名猜可靠（后者会把火山的视觉模型判成不能读图，而那个判断
直接决定带图请求要不要先过识图）。
"""

import threading
import time

import requests

from app.config import PROVIDERS

# 缓存时长。开一次管理页会按 provider 各拉一次，缓存避免反复打远端。
CACHE_TTL = 600
TIMEOUT = 10

_cache = {}
_lock = threading.Lock()


def _session():
    """绕开代理的会话。

    环境变量或注册表里若有代理，连 `127.0.0.1:11434`（Ollama）这类回环请求
    也会被送进代理，表现是 502，而真因看上去像服务没起。
    """
    s = requests.Session()
    s.trust_env = False
    return s


def list_models(provider_id, force=False):
    """列出该 provider 当前可用的对话模型。

    返回 `{"ok", "models", "error", "cached"}`，其中每条模型是
    `{"id", "vision", "retiring"}`。`vision` 为 None 表示该 provider 没给出
    这项信息（不是"不能读图"）。`ok=False` 时 `models` 退化为「该 provider
    配置里的默认模型」一条——下拉空着会让人以为功能坏了，而真实原因是网络或 key。
    """
    pid = (provider_id or "").strip().lower()
    cfg = PROVIDERS.get(pid)
    if not cfg:
        return {"ok": False, "models": [], "cached": False,
                "error": "未知 provider：%s" % (pid or "(空)")}

    if not force:
        with _lock:
            hit = _cache.get(pid)
        if hit and time.time() - hit[0] < CACHE_TTL:
            out = dict(hit[1])
            out["cached"] = True
            return out

    try:
        models, err = _fetch(pid, cfg), None
    except Exception as e:
        models, err = [], "%s: %s" % (type(e).__name__, e)

    if not models:
        default = (cfg.get("model") or "").strip()
        if default:
            models = [{"id": default, "vision": cfg.get("vision"),
                       "retiring": False}]

    out = {"ok": not err, "models": models, "error": err, "cached": False}
    with _lock:
        _cache[pid] = (time.time(), out)
    return dict(out)


def clear_cache():
    """丢掉缓存（测试用；也可在改过密钥后手动调）。"""
    with _lock:
        _cache.clear()


def _fetch(pid, cfg):
    base = (cfg.get("base_url") or "").rstrip("/")
    if pid == "ollama":
        data = _get_json(base + "/api/tags", None)
        return _from_ollama(data)
    rows = _get_json(base + "/models", cfg.get("api_key") or "")
    data = rows.get("data") if isinstance(rows, dict) else None
    if data is None and isinstance(rows, dict):
        data = rows.get("models") or []
    rows = data or []
    return _from_ark(rows) if pid in ("volc", "doubao") else _from_openai(rows)


def _get_json(url, api_key):
    headers = {"Authorization": "Bearer " + api_key} if api_key else {}
    resp = _session().get(url, headers=headers, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def _clean(rows):
    """去掉不是 dict、缺 id、以及重复的条目。"""
    seen, out = set(), []
    for m in rows or []:
        if not isinstance(m, dict):
            continue
        mid = str(m.get("id") or "").strip()
        if not mid or mid in seen:
            continue
        seen.add(mid)
        out.append(m)
    return out


def _sort(models):
    """在用的排前面；同一组内按 ID 排（尾部的日期让它近似按新旧排）。"""
    return sorted(models, key=lambda m: (bool(m.get("retiring")), m["id"]))


def _from_ark(rows):
    """火山方舟：靠 status 与 task_type 判断可用性与能力。

    - `Shutdown` 直接丢（已下线，选中必然失败）
    - `Retiring` 保留但标记：可能还在免费额度里，不该替用户决定不用
    - 只留 `TextGeneration`：向量/生图/视频那些不是对话模型，混进下拉只会误导
    - `VisualQuestionAnswering` 就是「能读图」
    """
    out = []
    for m in _clean(rows):
        if m.get("status") == "Shutdown":
            continue
        tasks = [str(t) for t in (m.get("task_type") or [])]
        if "TextGeneration" not in tasks:
            continue
        out.append({
            "id": str(m["id"]).strip(),
            "vision": "VisualQuestionAnswering" in tasks,
            "retiring": m.get("status") == "Retiring",
        })
    return _sort(out)


def _from_openai(rows):
    """OpenAI 兼容 /models（DeepSeek 官方）：用 input_modalities 判视觉。"""
    out = []
    for m in _clean(rows):
        modalities = [str(x).lower() for x in (m.get("input_modalities") or [])]
        out.append({
            "id": str(m["id"]).strip(),
            "vision": "image" in modalities if modalities else None,
            "retiring": False,
        })
    return _sort(out)


def _from_ollama(data):
    """Ollama `/api/tags`：本地模型。capabilities 里有 vision 才算能读图，
    没给出这项信息时留 None（不同版本上报的字段不一样）。"""
    out = []
    for m in (data or {}).get("models") or []:
        if not isinstance(m, dict):
            continue
        name = str(m.get("name") or m.get("model") or "").strip()
        if not name:
            continue
        caps = [str(c).lower() for c in (m.get("capabilities") or [])]
        out.append({
            "id": name,
            "vision": "vision" in caps if caps else None,
            "retiring": False,
        })
    return _sort(out)
