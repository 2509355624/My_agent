"""自定义 LLM 中转站的持久化与校验（2026-10-22）。

用户要的是「漫画台能自己加中转站，加完存住」——第一家是 relaycore 的
grok-4.5（0.02x 分组，私人用）。

**为什么是独立 JSON 而不是 .env**：增删改由网页发起，改 .env 得重启进程；
而且这里存的是**列表**（以后还会加别家），`.env` 的 key=value 表达不了。
代价是 api_key 明文落盘——文件放在 `data/` 下，已 gitignore，不进仓库。

**字段口径跟 `config.PROVIDERS` 完全一致**，前端不用区分内置和自定义。
多出来两个自有字段：
  - `scope`            `"comic"` = 只在漫画台的下拉里出现（默认）。
                       用户 2026-10-22 选的：私人用途，别处误选会烧他自己的额度。
  - `reasoning_effort` OpenAI 的思考档位。grok 系默认就是 high，显式写下来
                       是为了以后能在面板上切。实测 low 34 秒 / high 42 秒
                       （3 格脚本），`none` 服务端直接 400，关不掉。

这个模块**不 import config**：它被 `config.py` 反过来调用（启动时合并进
PROVIDERS），反过来引入会成环。路径由调用方给。
"""

import json
import os
import re
import time
import uuid

# 存进去、也允许用户改的字段。`created` 只在这里补，不接受前端传。
FIELDS = ("label", "base_url", "model", "api_key", "vision", "needs_key",
          "scope", "reasoning_effort")

# 能接受、但代码里还没有人消费的档位先不放进来：grok 侧实测 `none` 报 400、
# `minimal` 等同 low，写进下拉只会让人白点一次。
REASONING_CHOICES = ("", "low", "medium", "high")

SCOPES = ("comic", "all")

# id 只用来做 URL 片段和字典键，不该让用户填中文。前缀 `cx` = custom，
# 一眼能跟内置的 volc / deepseek 分开。
_ID_RE = re.compile(r"^cx_[0-9a-f]{8}$")


def store_path(data_dir):
    return os.path.join(data_dir, "custom_providers.json")


def load(path):
    """读出全部自定义渠道，返回 {id: cfg}。

    文件不在 / JSON 坏 / 顶层结构不对，一律返回空 dict——配置坏了最多是
    「自定义渠道消失了」，不能连累进程起不来（整个网页端都靠它）。
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    items = raw.get("providers")
    if not isinstance(items, dict):
        return {}
    out = {}
    for pid, cfg in items.items():
        if not isinstance(cfg, dict):
            continue
        pid = str(pid).strip()
        if not _ID_RE.match(pid):
            continue
        # 坏字段不让它带着上：base_url / model 缺了这个渠道根本发不出请求，
        # 留着只会在下拉里变成一个点了就报错的名字。
        if not (cfg.get("base_url") and cfg.get("model")):
            continue
        out[pid] = dict(cfg)
    return out


def save(path, items):
    """写盘。先写临时文件再 replace：写一半被断电/打断，也不会留下半个 JSON
    把下次启动的全部自定义渠道清空。
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    payload = {"providers": items, "saved": int(time.time())}
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def new_id(items):
    """生成一个不跟现有冲突的 id。"""
    while True:
        pid = "cx_" + uuid.uuid4().hex[:8]
        if pid not in (items or {}):
            return pid


def _clean_url(url):
    """去掉首尾空白和**末尾斜杠**。

    `model_catalog` 和 `llm` 都是 `base_url.rstrip("/") + "/models"` /
    `+ "/chat/completions"`，末尾带斜杠不会出错，但用户复制粘贴时常带，
    存下来看着别扭、界面上也不好核对。
    """
    return str(url or "").strip().rstrip("/")


def validate(payload, *, pid=""):
    """校验并归一化一条自定义渠道。返回 `(cfg, err)`，err 非空即不合法。

    只做「明显填错」的拦截（缺地址、缺模型、地址不是 http、档位不认识）；
    能不能真的连上，交给 test() 实打实发一次请求——这里不下判断。
    """
    p = payload if isinstance(payload, dict) else {}
    label = str(p.get("label") or "").strip()
    base_url = _clean_url(p.get("base_url"))
    model = str(p.get("model") or "").strip()
    api_key = str(p.get("api_key") or "").strip()

    if not base_url:
        return None, "缺少 API 地址（要填到 /v1 这一层，例如 https://api.relaycore.cn/v1）"
    if not base_url.startswith(("http://", "https://")):
        return None, "API 地址要以 http:// 或 https:// 开头"
    if not model:
        return None, "缺少模型名（例如 grok-4.5）"
    if not api_key:
        return None, "缺少 API 密钥"

    effort = str(p.get("reasoning_effort") or "").strip().lower()
    if effort not in REASONING_CHOICES:
        return None, "思考档位只能是 low / medium / high（留空 = 不下发）"

    scope = str(p.get("scope") or "comic").strip().lower()
    if scope not in SCOPES:
        scope = "comic"

    cfg = {
        "label": label or model,
        "base_url": base_url,
        "model": model,
        "api_key": api_key,
        # 与内置 provider 同口径：这两个开关决定「带图请求怎么走」和「要不要
        # 用户自己填 key」。中转站都是自带 key 的，needs_key 恒 False。
        "vision": bool(p.get("vision", False)),
        "needs_key": False,
        "scope": scope,
        "reasoning_effort": effort,
        # 标记：前端和接口靠它区分「内置」和「用户自己加的」，决定能不能删。
        "custom": True,
    }
    if pid:
        cfg["created"] = p.get("created") or int(time.time())
    return cfg, ""


def public_view(pid, cfg):
    """给前端看的形态：**不回传密钥全文**，只给尾 4 位。

    前端不需要真值（保存时用新填的，不填就沿用旧的）；回传全文等于把密钥
    摊在浏览器缓存、截图、浏览器历史里。
    """
    key = str(cfg.get("api_key") or "")
    return {
        "id": pid,
        "label": cfg.get("label") or pid,
        "base_url": cfg.get("base_url") or "",
        "model": cfg.get("model") or "",
        "scope": cfg.get("scope") or "comic",
        "reasoning_effort": cfg.get("reasoning_effort") or "",
        "custom": True,
        "key_tail": ("…" + key[-4:]) if len(key) > 8 else ("已填" if key else "未填"),
    }


def reasoning_field(cfg):
    """渠道配置 → 请求体里的思考档位字段。空 = 不下发，走服务端默认。

    `llm._reasoning_field` 是同一个口径，但它还要处理「400 降级重试时不带
    额外字段」，所以那边多一层 extras 判断，没有直接复用。
    """
    v = str((cfg or {}).get("reasoning_effort") or "").strip()
    return {"reasoning_effort": v} if v else {}
