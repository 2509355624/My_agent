"""
Flask Web 服务入口
"""

import re
import json
import os
import logging
import base64
import ipaddress
import socket
import time
import requests
from flask import (Flask, request, jsonify, send_from_directory, send_file,
                   Response, stream_with_context)
from app.core import agents
from app.core import agents as agent_store
from app.core import cancel as cancel_mod
from app.features.image import comfy_out
from app.features.image import audit as image_audit
from app.features.image import migrate as image_migrate
from app.core import logsetup
from app.core import usage as usage_stats
from app.features.qq import bill as qq_bill
# 别名 `vision_mod` 是**必须**的：本文件里已有一个叫 `vision` 的路由函数
# （POST /api/vision，网页端「让 AI 看图」），模块名直接被它盖掉，
# 写成 `from app.integrations import vision` 会在调用处炸 AttributeError:
# 'function' object has no attribute 'default_prompt'。
from app.integrations import vision as vision_mod
from app.features.comic import routes as comic_api
from app.features.comic import moss_api
from app.features.image import routes as gen_api
from app.features.qq import friend_api
from app.features.qq import friend_site
from app.features.gallery import routes as gallery_api
from app.features.card import routes as card_api
from app.features.image import output_api
from app.features.image import outfit_api
from app.features.novel import routes as novel_api
from app.config import (AGENT_PORT, WEB_DIR, COMFYUI_URL, MODEL, DOCUMENTS_DIR,
                        PROVIDERS, OLLAMA_BASE_URL, DEFAULT_AGENT_ID,
                        ADMIN_ALLOW_REMOTE, ADMIN_ALLOW_REMOTE_LAN,
                        CONTEXT_BUDGET, IMAGE_AUDIT_PROMPT_MAX,
                        QQ_PRIVATE_ENABLE, QQ_WHITELIST_USERS,
                        VISION_PROMPT_MAX, VISION_PROVIDER, VISION_MODEL,
                        QQ_AGENT_ID, provider_vision,
                        CUSTOM_PROVIDERS_FILE, load_custom_providers,
                        COMFYUI_OUTPUT_DIR)
from app.core.config.llm import get_llm_settings, update_llm_settings
from app.core.config import llm as llm_config
from app.core.config.runtime import runtime_path
from app.core import provider_store
from app.core.skills import list_skills, load_skill
from app.core import model_catalog
from app.features.chat.agent_prompt import build_stable_prompt, sync_session_system
from app.features.chat.memory import load_history, save_history, estimate_messages
from app.features.chat.agent import run_agent_stream, parse_tool_calls, _strip_tool_blocks
from app.tools import execute_tool
from app.tools.normal.documents import list_documents, file_info, read_document, search_document

app = Flask(__name__, static_folder=WEB_DIR, static_url_path="")

log = logging.getLogger("main")

# 伪装 Server 头（2026-11-08）：Werkzeug 默认发
# `Server: Werkzeug/3.1.3 Python/3.14.0`——版本号等于直接告诉扫描器该试哪几个
# CVE。报一个通用值就行，真实栈不出门。
try:
    from werkzeug.serving import WSGIRequestHandler as _WSGIHandler

    class _QuietRequestHandler(_WSGIHandler):
        server_version = "nginx"
        sys_version = ""
except Exception:                                        # noqa: BLE001
    _QuietRequestHandler = None

# ─── 会话持久化（每个 agent 一个会话文件）────────────
# 一切以磁盘文件为准：每次读写都 load/save，保证手机/电脑等多端
# 刷新到的一致（不再依赖进程内存全局变量，避免多端不同步）。
# 用哪个文件由 agent id 决定，见 app/agents.py。


def _req_agent_id(data=None):
    """从当前请求解析 agent id：body 字段优先，其次 query 参数。

    非法、缺失、指向不存在的目录都会兜底到默认 agent，所以这里拿到的
    永远是可用 id——前端下拉与 URL 都可能不带参数，不该因此报错。
    """
    raw = None
    if isinstance(data, dict):
        raw = data.get("agent")
    if not raw:
        raw = request.args.get("agent")
    return agent_store.resolve(raw)


def _ensure_system_prompt(agent_id=None, only_system=False):
    """把该 agent 会话的首条固定为稳定的 system 消息（prefix cache 锚点）。
    状态栏不在 messages[1]，而是由 agent.run_agent_stream 请求时动态追加在
    消息数组末尾，只牺牲它自己那几十个 token，避免毒化前缀缓存。

    - only_system=True：清空所有非 system 消息（用于“清空会话”）
    - only_system=False：仅补齐 system 头部，保留已有历史
    """
    stable_prompt = build_stable_prompt(agent_id)
    keep = ([] if only_system
            else [m for m in load_history(agent_id) if m.get("role") != "system"])
    save_history([{"role": "system", "content": stable_prompt}] + keep, agent_id)


def _truncate_at_user(history, user_index):
    """截断到第 user_index 条用户消息之前（1 起数），供「编辑并重发」使用。

    只数 role == "user"：assistant 与 tool_result 不参与计数，所以一轮回复
    被拆成多个 assistant 气泡、中间夹了多少工具结果，都不影响定位。
    这一点很重要——前端的「我」气泡序号正是按这个口径数的。

    返回 (新历史, 是否命中)。index 非法或找不到对应消息时原样返回并标记
    未命中，由调用方决定报错还是放行（静默按原样追加会让人误以为改生效了）。
    """
    if not isinstance(user_index, int) or user_index < 1:
        return history, False
    seen = 0
    for i, msg in enumerate(history):
        if msg.get("role") == "user":
            seen += 1
            if seen == user_index:
                return history[:i], True
    return history, False


# 注意：不在这里(import 时)初始化会话文件。导入模块不应产生磁盘副作用——
# 那会让测试/复用 import app.main 时污染真实 data/session.jsonl。
# 初始化改到 run() 里做，并且各路由在发现缺 system 头时会自行补齐。


# ─── 页面路由 ────────────────────────────────────────

# 漫画工作台 API（独立 Blueprint，见 app/comic_api.py）
app.register_blueprint(comic_api.bp)

# moss 漫画台 API（独立 Blueprint，见 app/moss_api.py）：一句话 → 编剧写整页 → 一次出图
app.register_blueprint(moss_api.bp)

# 网页端角色扮演 API（独立 Blueprint，见 app/features/persona/routes.py）：和 QQ 同一套引擎，
# 会话键用 `web_<qq>` 与 QQ 的 `private_<qq>` 隔离。2026-11-03 用户要的。
from app.features.persona import routes as persona_api
app.register_blueprint(persona_api.bp)

# 公开产品展示页：独立干净素材与会话线，不挂到内部扮演 blueprint 上。
from app.features.showcase import routes as showcase_api                       # noqa: E402
app.register_blueprint(showcase_api.bp)

# 展示页的「文档问答」标签（2026-11-15）：清洗 → 大模型切分 → 小模型向量化 →
# 检索问答，每一步的中间数据都回给前端。独立 daemon + 独立向量目录，不碰
# 角色扮演那套记忆库（见 app/features/rag/lab.py 模块头）。
from app.features.rag import lab as rag_lab                            # noqa: E402
app.register_blueprint(rag_lab.bp)

# 角色扮演「查看台」（2026-11-03）：看谁玩过、聊了什么。
# 只挂 5174，公网只映射 5175 的网关，所以这里不需要额外鉴权。
from app.features.admin import routes as persona_admin_api                  # noqa: E402
app.register_blueprint(persona_admin_api.bp)

# 手动跑图台 API（独立 Blueprint，见 app/gen_api.py）：无 AI 的文生图页面
app.register_blueprint(gen_api.bp)

# 群友生图页的内核（独立 Blueprint，见 app/friend_api.py）。**不对公网**：
# 只有 5175（friend_app.py）带 X-Friend-Token 转发进来时才认。
app.register_blueprint(friend_api.bp)

# 画廊（独立 Blueprint，见 app/gallery_api.py）：站主磁盘目录 → 作品集，
# 群友上传 → 压缩后入库。同样只认内部令牌，公网入口在 5175。
app.register_blueprint(gallery_api.bp)

# 工作台聊天记录（2026-11-24）：按 QQ 号一档一份，换浏览器 / 换设备登录
# 拿到的是同一份。见 app/features/chat/routes.py。
from app.features.chat import routes as chat_archive_api                 # noqa: E402
app.register_blueprint(chat_archive_api.bp)


@app.before_request
def _public_source_guard():
    """公网来源直连一律拒（2026-11-08）。

    cloudflared 跑在本机，Tunnel 流量的 `remote_addr` 是 127.0.0.1；本机浏览器、
    手机/iPad 也都是私网段。**公网 IP 出现在 `remote_addr` 上，只可能是路由器
    端口转发 / DMZ / UPnP 把 5174 捅到了外面**——那正是「反查源站 IP」之后能
    直接打进来的口子，而且这条请求**不带** `CF-Connecting-IP`，`_tunnel_guard`
    认不出来。

    本机 + 私网放行（手机、iPad 照常用）；公网一律 403。
    以后真要换别的反代（frp / ngrok 这类从公网连进来的），得把这层改成白名单。
    """
    addr = request.remote_addr or ""
    if addr in _LOCAL_ADDRS or _is_lan_addr(addr):
        return None
    log.warning("拒绝公网直连：%s %s", addr, request.path)
    return jsonify({"ok": False, "error": "forbidden"}), 403


@app.before_request
def _tunnel_guard():
    """万一隧道错指到 5174，也只放行群友页那几个接口。

    隧道连的是 127.0.0.1:5174，`remote_addr` 和本机浏览器一模一样，分不开；
    但隧道请求一定带 `CF-Connecting-IP`（或 `CF-Ray`），本机浏览器不会。
    所以这一道能准确识别「这是从公网进来的」，并把管理页、输出删除、agent
    聊天全部挡在外面——隧道本身是 5175 的，这里只是双保险。
    """
    if not (request.headers.get("CF-Connecting-IP")
            or request.headers.get("CF-Ray")):
        return None
    p = request.path or ""
    if (p.startswith("/api/friend/") or p.startswith("/api/gal/")
            or p.startswith("/api/outfit/") or p.startswith("/api/runbench/")
            or p.startswith("/api/showcase/") or p.startswith("/api/raglab/")
            or p == "/showcase" or p == "/raglab-sample.md"
            or p == "/health"):
        return None
    return jsonify({"ok": False, "error": "not found"}), 404

# 角色卡工作台 API（独立 Blueprint，见 app/features/card/routes.py）：一句话 →
# 星陨AI 的五个文本框 + 竖版封面
app.register_blueprint(card_api.bp)

# output 图片管理台 API（独立 Blueprint，见 app/output_api.py）：ComfyUI output
# 目录的网页版资源管理器——目录树点开就看，缩略图网格，勾选即删
app.register_blueprint(output_api.bp)

# 换装页 API（独立 Blueprint，见 app/outfit_api.py）：双图 qwen 换装/换姿势。
# 免登录（令牌即凭证），公网入口在 5175 的 /outfit + /api/outfit/* 反代。
app.register_blueprint(outfit_api.bp)

# 小说模式（2026-11-11）：和一对一角色扮演相互隔离的第三套，独立世界书 /
# 裁定链 / 页面。只挂 5174，站主自用。
app.register_blueprint(novel_api.bp)

# 跑图台（2026-11-13）：随心配预设面板，点卡片用预设 recipe 出图、结果推回 QQ。
# 免登录（令牌即凭证），公网入口在 5175 的 /runbench + /api/runbench/* 反代。
from app.features.bench import web as runbench_web                      # noqa: E402
app.register_blueprint(runbench_web.bp)

@app.route("/")
def index():
    return send_from_directory(WEB_DIR, "index.html")


@app.route("/api/friend_url")
def friend_url_api():
    """个人 Agent 顶栏用的当前隧道地址。

    只给本机 / 局域网页读：公网经隧道进来时 `_tunnel_guard` 会把它挡成
    404，5175 也不反代这条。地址每次从 `state/friend_url.txt` 现读，
    隧道重启换了随机域名，页面下一次点击就能拿到新值，不缓存旧地址。
    """
    url = friend_site.url()
    resp = jsonify({"ok": bool(url), "url": url})
    resp.headers["Cache-Control"] = "no-store"
    return resp


# ─── API 路由 ────────────────────────────────────────

@app.route("/api/usage")
def get_usage():
    """每日 token 用量（按「会话 / 隐形调用」聚合）。?date=YYYY-MM-DD 可选。

    管理页的「今日用量」表格吃这个接口；也能拿它跟 LLM 后台的账对数。
    """
    date = (request.args.get("date") or "").strip() or None
    if date and not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        return jsonify({"error": "date 格式应为 YYYY-MM-DD"}), 400
    data = usage_stats.daily(date)
    sessions = data.setdefault("sessions", {})
    for slot in sessions.values():
        total_in = (slot.get("hit") or 0) + (slot.get("miss") or 0)
        slot["hit_rate"] = round((slot.get("hit") or 0) / total_in, 4) \
            if total_in else 0.0
    data["dates"] = usage_stats.load_all_dates()
    return jsonify(data)


@app.route("/api/qq_bill")
def get_qq_bill():
    """QQ 机器人按会话账单（只读，站主自付，不扣任何余额）。"""
    date = (request.args.get("date") or "").strip() or None
    if date and not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        return jsonify({"ok": False, "error": "date 格式应为 YYYY-MM-DD"}), 400
    try:
        return jsonify({"ok": True, **qq_bill.daily(date)})
    except Exception:                                    # noqa: BLE001
        app.logger.exception("[qq_bill] 账单读取失败")
        return jsonify({"ok": False, "error": "账单独不出来"}), 500


@app.route("/api/qq_bill/export.csv")
def export_qq_bill():
    """按会话明细 CSV。跟代充账本一样走 fetch→Blob，不靠 Content-Disposition。"""
    date = (request.args.get("date") or "").strip() or None
    if date and not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        return jsonify({"ok": False, "error": "date 格式应为 YYYY-MM-DD"}), 400
    try:
        body = qq_bill.csv_text(qq_bill.daily(date))
    except Exception:                                    # noqa: BLE001
        app.logger.exception("[qq_bill] 账单导出失败")
        return jsonify({"ok": False, "error": "明细导不出来"}), 500
    return Response(body, mimetype="text/csv; charset=utf-8")


@app.route("/api/providers")
def get_providers():
    """返回支持的模型提供商列表（供前端下拉）

    `scope=comic` 时额外带上「只在漫画台可见」的自定义渠道（用户 2026-10-22
    加的中转站）。别的入口不带这个参数，所以那些渠道不会出现在主对话、
    管理页、moss 台的下拉里——私人额度，误选一次就是真金白银。
    """
    scope = (request.args.get("scope") or "").strip().lower()
    items = []
    for key, cfg in PROVIDERS.items():
        is_custom = bool(cfg.get("custom"))
        if is_custom and (cfg.get("scope") or "comic") == "comic" and scope != "comic":
            continue
        item = {
            "id": key,
            "label": cfg["label"],
            "model": cfg["model"],
            "base_url": cfg["base_url"],
            "custom": is_custom,
        }
        if is_custom:
            # 公开展示的形态：密钥只给尾 4 位（见 provider_store.public_view）。
            item.update(provider_store.public_view(key, cfg))
        items.append(item)
    settings = get_llm_settings()
    return jsonify({"providers": items, "default": settings["provider"],
                    "model": PROVIDERS.get(settings["provider"], {}).get("model", MODEL),
                    "custom_file": os.path.basename(CUSTOM_PROVIDERS_FILE)})


def _custom_items():
    """读出磁盘上的自定义渠道原文（**含密钥**，只在本模块内部用）。"""
    return provider_store.load(CUSTOM_PROVIDERS_FILE)


@app.route("/api/providers/custom", methods=["POST"])
def save_custom_provider():
    """新增 / 修改一个自定义中转站。

    带 `id` 是修改：`api_key` 留空表示沿用原来那把（前端拿不到密钥原文，
    不回填就只能留空，不能因为「没重填」就把配好的渠道清掉密钥）。
    """
    data = request.get_json(silent=True) or {}
    pid = str(data.get("id") or "").strip()
    items = _custom_items()

    if pid and pid not in items:
        return jsonify({"ok": False, "error": "找不到这个自定义渠道（可能已被删除）"}), 404

    payload = dict(data)
    if pid and not str(payload.get("api_key") or "").strip():
        payload["api_key"] = items[pid].get("api_key") or ""
    if pid:
        payload["created"] = items[pid].get("created")

    cfg, err = provider_store.validate(payload, pid=pid)
    if err:
        return jsonify({"ok": False, "error": err}), 400

    new_id = pid or provider_store.new_id(items)
    try:
        items[new_id] = cfg
        provider_store.save(CUSTOM_PROVIDERS_FILE, items)
    except OSError as exc:
        return jsonify({"ok": False, "error": "保存失败：%s" % exc}), 500
    load_custom_providers()          # 立刻生效，不用重启进程
    return jsonify({"ok": True, "provider": provider_store.public_view(new_id, cfg)})


@app.route("/api/providers/custom/<pid>", methods=["DELETE"])
def delete_custom_provider(pid):
    items = _custom_items()
    pid = str(pid or "").strip()
    if pid not in items:
        return jsonify({"ok": False, "error": "找不到这个自定义渠道"}), 404
    try:
        items.pop(pid)
        provider_store.save(CUSTOM_PROVIDERS_FILE, items)
    except OSError as exc:
        return jsonify({"ok": False, "error": "删除失败：%s" % exc}), 500
    load_custom_providers()
    return jsonify({"ok": True})


@app.route("/api/providers/custom/test", methods=["POST"])
def test_custom_provider():
    """真发一次请求验证这个中转站能不能用（不落盘）。

    分两步，因为失败点不一样：
      1. GET /models —— 验地址对不对、密钥通不通。401/403 一眼就看出来。
      2. POST /chat/completions —— 验模型名对不对、真能出字。
    只做第 1 步不够：有些中转站的 /models 不校验密钥，密钥错了照样 200。
    """
    data = request.get_json(silent=True) or {}
    pid = str(data.get("id") or "").strip()
    payload = dict(data)
    if pid:
        # 修改已有渠道：密钥留空就沿用存着的那把（与保存同一口径）。
        old = _custom_items().get(pid) or {}
        if not str(payload.get("api_key") or "").strip():
            payload["api_key"] = old.get("api_key") or ""
    cfg, err = provider_store.validate(payload, pid=pid)
    if err:
        return jsonify({"ok": False, "error": err}), 400

    base = cfg["base_url"]
    headers = {"Authorization": "Bearer " + cfg["api_key"]}
    session = requests.Session()
    session.trust_env = False        # 不吃系统代理（与 model_catalog 同口径）

    models = []
    try:
        resp = session.get(base + "/models", headers=headers, timeout=15)
        if resp.status_code >= 400:
            detail = resp.text[:200].replace("\n", " ")
            return jsonify({"ok": False, "stage": "models",
                            "error": "列模型失败 HTTP %d：%s" % (resp.status_code, detail)})
        rows = (resp.json() or {}).get("data") or []
        models = [str(m.get("id")) for m in rows if isinstance(m, dict) and m.get("id")]
    except Exception as exc:
        return jsonify({"ok": False, "stage": "models",
                        "error": "连不上：%s" % exc})

    body = {"model": cfg["model"], "stream": False,
            "messages": [{"role": "user", "content": "只回复两个字：可用"}]}
    body.update(provider_store.reasoning_field(cfg))
    t0 = time.time()
    try:
        resp = session.post(base + "/chat/completions", headers=headers,
                            json=body, timeout=90)
        if resp.status_code >= 400:
            detail = resp.text[:300].replace("\n", " ")
            return jsonify({"ok": False, "stage": "chat", "models": models,
                            "error": "模型 %s 调用失败 HTTP %d：%s"
                                     % (cfg["model"], resp.status_code, detail)})
        d = resp.json() or {}
        msg = ((d.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    except Exception as exc:
        return jsonify({"ok": False, "stage": "chat", "models": models,
                        "error": "请求超时或中断：%s" % exc})
    return jsonify({"ok": True, "models": models, "reply": msg.strip()[:40],
                    "elapsed": round(time.time() - t0, 1)})


@app.route("/api/llm/settings")
def get_llm_settings_api():
    """管理页读取当前生效的 LLM 设置与代码默认值。"""
    if not _admin_allowed():
        return jsonify({"error": "管理接口默认只允许本机访问，"
                                 "如需远程改 .env 的 ADMIN_ALLOW_REMOTE"}), 403
    return jsonify({
        "ok": True,
        "effective": get_llm_settings(),
        "defaults": {
            "fallback_chain": llm_config.LLM_FALLBACK_CHAIN,
            "request_timeout": llm_config.LLM_REQUEST_TIMEOUT,
            "judge_timeout": llm_config.LLM_JUDGE_TIMEOUT,
            "retry_times": llm_config.LLM_RETRY_TIMES,
            "retry_delay": llm_config.LLM_RETRY_DELAY,
            "retry_budget": llm_config.LLM_RETRY_BUDGET,
            "fallback_ttl": llm_config.LLM_FALLBACK_TTL,
            "rate_limit_ttl": llm_config.LLM_RATE_LIMIT_TTL,
            "concurrency_ttl": llm_config.LLM_CONCURRENCY_TTL,
        },
        "runtime_file": os.path.basename(runtime_path()),
    })


@app.route("/api/llm/settings", methods=["PUT"])
def put_llm_settings_api():
    """管理页保存 LLM 设置；落盘后下一次请求即生效。"""
    if not _admin_allowed():
        return jsonify({"error": "管理接口默认只允许本机访问，"
                                 "如需远程改 .env 的 ADMIN_ALLOW_REMOTE"}), 403
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "请求体必须是 JSON 对象"}), 400
    effective, error = update_llm_settings(data)
    if error:
        return jsonify({"error": error}), 400
    return jsonify({"ok": True, "effective": effective})


@app.route("/api/agents")
def get_agents():
    """返回所有 agent（供前端切换下拉）。

    实时扫 agents/ 目录、不缓存，所以新建一个 agent 目录后刷新页面即可用。
    """
    return jsonify({"agents": agent_store.list_agents(),
                    "default": DEFAULT_AGENT_ID})


@app.route("/api/models")
def get_ollama_models():
    """列出可选的模型。

    带 `provider` 时返回该 provider 当前可用的对话模型（各自去问对方，见
    app/model_catalog.py）——预选清单写死会腐烂，模型 ID 是会下线、会改名的。
    不带则维持原行为：按 `baseUrl` 代理 Ollama 的 /api/tags。
    """
    pid = request.args.get("provider", "").strip()
    if pid:
        return jsonify(model_catalog.list_models(pid))

    base = request.args.get("baseUrl", "").strip() or OLLAMA_BASE_URL
    try:
        resp = requests.get(base.rstrip("/") + "/api/tags", timeout=5)
        resp.raise_for_status()
        models = [
            {"name": t.get("name", ""), "size": t.get("size", 0)}
            for t in resp.json().get("models", [])
            if t.get("name")
        ]
        return jsonify({"ok": True, "models": models})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 502


@app.route("/api/history")
def get_history():
    agent_id = _req_agent_id()
    h = load_history(agent_id)
    msgs = [m for m in h if m.get("role") != "system"]
    settings = get_llm_settings()
    return jsonify({"messages": msgs,
                    "model": PROVIDERS.get(settings["provider"], {}).get("model", MODEL),
                    "count": len(msgs),
                    "provider": settings["provider"], "agent": agent_id})


@app.route("/api/chat", methods=["POST"])
def chat():
    data = request.get_json()
    user_input = data.get("message", "").strip()
    # 可选：本条消息附带的图片（前端压缩后的 dataURL）。带图时走同一条 agent
    # 循环，模型能先看图再决定调什么工具——这正是 /api/vision 做不到的事。
    image = (data.get("image") or "").strip()
    provider = data.get("provider")
    model = data.get("model")
    agent_id = _req_agent_id(data)
    # 中断用的请求标识：前端每轮生成一个，点「停止」时原样回传给 /api/stop。
    # 按它建键而不是按 agent —— 多标签页可能同时用同一个 agent，按 agent 中断
    # 会把另一个标签页里正常跑的请求一起打断。
    request_id = (data.get("request_id") or "").strip()
    cancel_event = cancel_mod.register(request_id)

    # 允许"只有图、没有文字"的请求（纯图提问是常见用法）
    if not user_input and not image:
        return jsonify({"error": "消息不能为空"}), 400

    # 支持用户直接在输入框粘贴/输入 [[TOOL:name]]{...} 触发工具：
    # 解析并执行用户消息里的工具块，把结果注入历史后，再进入 LLM 循环，
    # 这样 LLM 一开始就能看到已执行的工具结果。
    user_tools = parse_tool_calls(user_input)
    clean_input = _strip_tool_blocks(user_input).strip() or user_input
    pre_results = []
    if user_tools:
        for tc in user_tools:
            # 与 agent 主循环里的拦截保持一致：手打工具块同样受白名单约束
            if not agent_store.allows_tool(agent_id, tc["name"]):
                pre_results.append({
                    "name": tc["name"],
                    "result": "该工具在当前 agent 不可用：" + tc["name"],
                })
                continue
            try:
                result = execute_tool(tc["name"], tc["args"])
            except Exception as e:
                result = "工具执行失败: " + str(e)
            pre_results.append({"name": tc["name"], "result": result})

    # 每次从文件读取最新会话，保证多端一致；写完立即落盘
    h = load_history(agent_id)
    if not h or h[0].get("role") != "system":
        _ensure_system_prompt(agent_id, only_system=True)
        h = load_history(agent_id)
    elif sync_session_system(agent_id):
        # 人设/配置改过了 → 新的 system 头已写进会话，重读一份带上。
        # 没变时它一个字节都没动（内部只读首行比较），前缀缓存不受影响。
        h = load_history(agent_id)

    # 「编辑某条用户消息后重发」：该条之前的会话保留，之后的全部丢弃。
    # 被丢掉的那些消息都是在回应被改掉的那一句，留着会让上下文自相矛盾
    # ——模型会以为它们是在回答新内容。本进程不持有常驻会话对象，每次请求
    # 都重新 load，所以截断就是一次数组切片，不需要撤销已落盘的日志行。
    edit_user_index = data.get("edit_user_index")
    if edit_user_index not in (None, ""):
        try:
            edit_user_index = int(edit_user_index)
        except (TypeError, ValueError):
            return jsonify({"error": "edit_user_index 必须是整数"}), 400
        h, hit = _truncate_at_user(h, edit_user_index)
        if not hit:
            return jsonify(
                {"error": "找不到第 %d 条用户消息" % edit_user_index}), 400

    # 流式返回：agent 循环每产生一个事件就立刻推给前端（NDJSON，一行一个 JSON）。
    # 之前是收集完所有事件再一次性 jsonify，导致本地模型跑 60-70 秒期间前端全黑箱。
    #
    # 落盘时机：会话消息类事件一出流就落盘，而不是攒到整轮结束。
    # 一轮请求可能包含多次工具调用（生图更可能阻塞数十分钟），若只在 finally
    # 落盘，这段时间磁盘一直是上一轮的样子——进程被杀（改完 app/*.py 重启）
    # 整轮内容蒸发，另一个标签页刷新也读不到正在进行的内容。
    # 单次写盘是「临时文件 + fsync + os.replace」，几十上百 KB 的文件几毫秒，
    # 一轮多写几十次可以接受。
    # aborted 也在其中：它出流时那条「被中断」的说明刚写进历史，要跟着落盘
    SESSION_EVENTS = ("user", "assistant", "tool_result", "aborted")

    # 带图时：图片本体不进历史——一张图 base64 几十万字符，存进会话文件会让它
    # 迅速膨胀，而且刷新回放时也还原不出图片。历史里只留一句与 /api/vision
    # 同口径的占位文本，图片本身由 run_agent_stream 在第 1 轮以多模态形式下发。
    if image:
        turn_input = ("（用户上传了一张图片：" + clean_input + "）"
                      if clean_input else "（用户上传了一张图片）")
    else:
        turn_input = clean_input

    def generate():
        # 把取消事件绑到本线程：工具层（execute_tool 内部）靠它感知中断，
        # 生图那种长阻塞的轮询循环只有走这条链路才停得下来。
        cancel_mod.bind(cancel_event)
        try:
            # strict=True：网页端「我选谁就是谁」——不降级、不兜底，失败就是
            # 失败。模型面板上的那个名字必须等于真正答话的模型：以前链尾会
            # 顶上，界面却照旧显示你选的那个，账单和直觉对不上。
            for event in run_agent_stream(turn_input, h, provider=provider,
                                          model=model, pre_tool_results=pre_results,
                                          agent_id=agent_id, image=image,
                                          cancel_event=cancel_event, strict=True):
                # 先落盘再推送：内容一旦可见于前端，磁盘上就已经有了
                if event.get("type") in SESSION_EVENTS:
                    try:
                        save_history(h, agent_id)
                    except Exception:
                        # 落盘失败不该打断这一轮对话，finally 还会再试一次
                        pass
                yield json.dumps(event, ensure_ascii=False) + "\n"
        finally:
            # 兜底：无论正常结束、被中断还是客户端断开，都落盘已产生的会话；
            # 同时摘掉取消事件的登记，否则注册表会随会话一直变大
            cancel_mod.unregister(request_id)
            save_history(h, agent_id)

    return Response(
        stream_with_context(generate()),
        mimetype="application/x-ndjson; charset=utf-8",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",   # 禁止反向代理缓冲
        }
    )


@app.route("/api/vision", methods=["POST"])
def vision():
    """多模态：让 AI 看一张图片并文字分析/给优化建议。

    走 deepseek 官方 deepseek-flash（官方文档验证支持图像理解），content 用块数组
    内联 data URL 图片。只分析讨论，绝不由 AI 自动重绘——重绘与否由用户决定。
    """
    from app.integrations.llm import call_llm

    data = request.get_json() or {}
    image = (data.get("image") or "").strip()
    question = (data.get("question") or "").strip()
    provider = data.get("provider")
    model = data.get("model")
    agent_id = _req_agent_id(data)
    if not image:
        return jsonify({"error": "缺少图片"}), 400
    if not question:
        question = "帮我看看这张图片，描述它，并给出可以优化/改进的地方。"

    from app.features.chat.agent_prompt import build_stable_prompt
    messages = [
        {"role": "system", "content": build_stable_prompt(agent_id)},
        {"role": "user", "content": [
            {"type": "text", "text": question},
            {"type": "image_url", "image_url": {"url": image, "detail": "low"}},
        ]},
    ]
    try:
        reply = call_llm(messages, provider=provider or None, model=model or None, timeout=600)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    # 记入历史（图片不入档避免会话文件膨胀；保留用户问题文本）
    h = load_history(agent_id)
    if not h or h[0].get("role") != "system":
        _ensure_system_prompt(agent_id, only_system=True)
        h = load_history(agent_id)
    h.append({"role": "user", "content": "（用户上传了一张图片" + (("：" + question) if question else "") + "）"})
    h.append({"role": "assistant", "content": reply})
    save_history(h, agent_id)
    return jsonify({"reply": reply})


@app.route("/api/stop", methods=["POST"])
def stop():
    """手动中断正在跑的那一轮。

    只做一件事：置位进程内的取消事件。真正停在哪里交给 agent 循环的三个检查点
    （模型输出中 / 轮次之间 / 生图轮询中都能立刻停），之后按正常流程收尾——
    出 aborted 事件、落盘、生成器结束。所以这里不杀线程、不动会话文件，被中断
    那一轮已产生的内容会完整留在会话里。

    之所以不能靠前端断开连接来充当"停止"：服务端要等到下一次往流里写数据才会
    发现你走了，而卡在工具执行里时它根本不写流——那正是只能杀进程的原因。

    hit=false 表示这条请求已经跑完（或 request_id 对不上），没什么可中断的。
    """
    data = request.get_json(silent=True) or {}
    request_id = (data.get("request_id") or "").strip()
    if not request_id:
        return jsonify({"error": "缺少 request_id"}), 400
    return jsonify({"ok": True, "hit": cancel_mod.cancel(request_id)})


@app.route("/api/clear", methods=["POST"])
def clear():
    # 清空该 agent 的全部非 system 消息（保留稳定 system 前缀）
    agent_id = _req_agent_id(request.get_json(silent=True))
    _ensure_system_prompt(agent_id, only_system=True)
    return jsonify({"ok": True, "agent": agent_id})


def _local_output_file(rel):
    """相对路径 → ComfyUI output 目录里的真实文件；没有就返回 None。

    2026-11-10 实现下沉到 `comfy_out.local_output_file`：跑图台 `/api/gen/img`
    也要走同一套「先读盘」，两份路径校验迟早漂移。这里保留函数名给
    `serve_image` 和既有测试。事故背景与校验理由见那边。
    """
    return comfy_out.local_output_file(rel, root=COMFYUI_OUTPUT_DIR)


@app.route("/api/image/<path:filename>")
def serve_image(filename):
    """从 ComfyUI 输出目录取图。

    `path:` 是 2026-10-18 加的：输出图按会话分目录后，地址变成
    `/api/image/web/生图页/silver/Anima_1_.png`，普通 `<filename>` 匹配不到斜杠。

    取图顺序：**先读本地磁盘**（`COMFYUI_OUTPUT_DIR`，`send_file` 带条件请求，
    大图还能断点续传），磁盘上没有才代理到 ComfyUI 的 `/view`。反过来做的代价
    见 `_local_output_file` 的注释——ComfyUI 重启窗口里整页图全变破图。
    """
    local = _local_output_file(filename)
    if local:
        return send_file(local, conditional=True)
    try:
        name, sub = comfy_out.split_rel(filename)
        params = {"filename": name}
        if sub:
            params["subfolder"] = sub
        resp = requests.get(COMFYUI_URL + "/view", params=params,
                            stream=True, timeout=30)
        resp.raise_for_status()
        return Response(resp.iter_content(chunk_size=8192),
                       content_type=resp.headers.get('content-type', 'image/png'))
    except Exception as e:
        return jsonify({"error": str(e)}), 404


@app.route("/api/skills")
def get_skills():
    # 别直接取正文第一行：带 YAML frontmatter 的 skill.md 首行是 `---`，
    # 列表里就成一排横线（2026-10-30 部署包实测）。skill_summary 会跳过
    # frontmatter 再找标题，两边口径也统一了（跑图台用的是同一个函数）。
    from app.core.skills import skill_summary
    out = []
    for s in list_skills():
        data = load_skill(s)
        if data:
            out.append({"name": s,
                        "description": skill_summary(data["skill_md"] or "")})
    return jsonify({"skills": out})


# ─── 后台管理（agent 配置读写）──────────────────────
# 只开放两样：人设（prompt.md）与模型（agent.json 的 provider/model）。
# 工具 / skills 白名单刻意不做成界面——那是「配置」而不是「管理」，手编 JSON
# 能一眼看到全貌；在勾选框里漏勾一个是静默的，改错 JSON 立刻报解析错。
# 配置落在 agent 自己的目录里，仍是「文件即真相」，后台只是一层编辑器。

# 回环地址。IPv4-mapped 形式（::ffff:127.0.0.1）也要认，某些环境下 Flask
# 拿到的 remote_addr 长这样。
_LOCAL_ADDRS = ("127.0.0.1", "::1", "::ffff:127.0.0.1", "localhost")


_LOCAL_ADDRS = ("127.0.0.1", "::1", "::ffff:127.0.0.1", "localhost")


def _is_lan_addr(addr):
    """兼容门面：实现在 app.core.admin_access，供本文件旧调用点使用。"""
    from app.core import admin_access

    return admin_access.is_lan_addr(addr)


def _admin_allowed():
    from app.core import admin_access

    return admin_access.admin_allowed()


def _agent_or_400(agent_id):
    """兼容门面：实现在 app.core.agent_http。"""
    from app.core.agent_http import agent_or_400

    return agent_or_400(agent_id)


# 两张**附言**的内置默认（管理页拿它当编辑框初始内容）。这两个模块都不在
# main 的顶层依赖里（image_jobs 会拉起 worker、generate_image 是工具层），
# 所以照本文件其它地方一样在函数内延迟 import。
from app.features.admin import agent_profile as admin_agent_profile
from app.features.admin.agent_profile import (_receipt_note_default,
                                              _caption_note_default)
app.register_blueprint(admin_agent_profile.bp)




# 附言的长度上限（两张共用）。它是要**刷屏发进群里**的文字，不是提示词：
# 写长了每条回执都带着走，所以卡得比 VISION_PROMPT_MAX 紧。
_NOTE_TEXT_MAX = 400










from app.web import pages
app.register_blueprint(pages.bp)


















@app.after_request
def _no_cache_novel(resp):
    """小说页也是单页工具页，改了前端刷新就该生效（同 /persona 的坑）。"""
    try:
        if request.path == "/novel" or request.path.startswith("/api/novel/"):
            resp.headers["Cache-Control"] = "no-store, must-revalidate"
    except Exception:                                  # noqa: BLE001
        pass
    return resp


@app.after_request
def _no_cache_persona(resp):
    """角色扮演的前端**不许缓存**（2026-11-06）。

    踩过：改了 `web/persona.html` 之后重启，浏览器还在用旧 JS——旧版里
    弹窗的「确定」根本没有绑新逻辑，点下去毫无反应，看起来就像页面卡死。
    这类单页工具页每次都应该拿最新的，代价只是几十 KB。
    """
    try:
        if request.path.startswith("/persona") and not request.path.startswith(
                "/api/"):
            resp.headers["Cache-Control"] = "no-store, must-revalidate"
            resp.headers["Pragma"] = "no-cache"
            resp.headers["Expires"] = "0"
    except Exception:                                  # noqa: BLE001
        pass
    return resp










from app.features.persona import cards_api
app.register_blueprint(cards_api.bp)




@app.route("/api/status")
def get_status():
    """状态后台的数据源。

    QQ bot 是**独立进程**，它的内存状态（哪个群在排队、生图队列）本进程
    读不到，所以走 app/features/qq/status.py 落盘的 JSON 文件。那个文件停更就说明
    bot 卡死或没起 —— 用 stale 标出来让前端标红，这正是「任务卡死时看不
    到后台」最需要的信号。

    ComfyUI 是本进程能直接探的（127.0.0.1 的 HTTP 服务），不必绕 bot 的
    快照，在这里查更实时。
    """
    from app.features.qq import status as qq_status
    from app.features.image import comfy_status

    snap, stale = qq_status.read()
    snap = snap or {}

    try:
        c = comfy_status.snapshot()
        comfy = {"online": c.get("online"), "running": c.get("running", 0),
                 "pending": c.get("pending", 0)}
    except Exception as exc:
        comfy = {"online": None, "running": 0, "pending": 0,
                 "error": repr(exc)[:120]}

    # 网页端队列（2026-11-22）：跑图台 / 漫画工作台 / moss 漫画 / 扮演配图
    # 都排在本进程的 image_jobs 里，bot 的快照文件看不到它们——在这里
    # 直接取进程内快照，状态页就能同时看到两边（谁在占 ComfyUI 一目了然）。
    try:
        from app.features.image import jobs as image_jobs
        web = image_jobs.snapshot()
    except Exception as exc:
        web = {"running": None, "queued": [], "depth": 0,
               "nai": {"running": [], "queued": [], "depth": 0},
               "error": repr(exc)[:120]}

    ts = snap.get("ts")
    return jsonify({
        # bot 进程本身：stale = 心跳停了（卡死/没起）
        "bot": {"stale": stale,
                "age": (round(time.time() - ts, 1) if ts else None)},
        "protocol": snap.get("protocol") or {"online": None, "detail": ""},
        "last_activity_ago": snap.get("last_activity_ago"),
        "sessions": snap.get("sessions") or [],
        "jobs": snap.get("jobs")
                or {"running": None, "queued": [], "depth": 0},
        "web": web,
        "comfy": comfy,
    })






# ─── 会话管理（列出 / 删除某条会话线）────────────────
# QQ 适配层让一个 agent 下挂多条互不相干的会话线（每个群、每个私聊各一条，
# 见 app/agents.py 的 session_file）。网页端原来的「清空」只够得着主会话，
# 想单独重置某个群没有入口——这里补上，语义就是「删掉这个群的聊天记录，
# 下次从零开始」。人设 / 白名单 / 触发词都在 agent.json 与 prompt.md 里，
# 不受影响。


def _decorate_session_names(items):
    """把群名 / 昵称补进每条会话线的 name，返回「名字是否全拿到了」。

    名字要问 QQ 协议端，属于锦上添花：协议端没开、这台机器压根没接 QQ、
    或者只是没进过某个群的好友——拿不到就退回只显示号，列表照样给全。
    所以这里把异常吞掉并如实回报 ok=False，而不是让整个接口失败。
    """
    kinds = {i["kind"] for i in items}
    if not ({"group", "private"} & kinds):
        return True

    # 局部 import：不接 QQ 的 agent 列表压根走不到这里，没必要在启动时加载
    from app.features.qq import api as qq_api

    names, ok = {}, True
    if "group" in kinds:
        try:
            for g in qq_api.get_group_list():
                gid = str(g.get("group_id", ""))
                if gid:
                    names["group_" + gid] = g.get("group_name") or ""
        except Exception:
            ok = False
    if "private" in kinds:
        try:
            for f in qq_api.get_friend_list():
                uid = str(f.get("user_id", ""))
                if uid:
                    names["private_" + uid] = (f.get("remark")
                                               or f.get("nickname") or "")
        except Exception:
            ok = False

    for item in items:
        if item["kind"] == "main":
            continue          # 主会话的 name 在 list_sessions 里已写好
        label = names.get(item["key"], "")
        if item["kind"] == "private":
            item["name"] = (label or "私聊") + "（" + item["target_id"] + "）"
        elif item["kind"] == "group":
            item["name"] = (label or "群") + "（" + item["target_id"] + "）"
        else:
            item["name"] = item["key"]
    return ok


@app.route("/api/agent/<agent_id>/sessions")
def get_agent_sessions(agent_id):
    """列出该 agent 的所有会话线（主会话 + 每个群 / 私聊各一条）。"""
    aid, err = _agent_or_400(agent_id)
    if err:
        return err
    items = agent_store.list_sessions(aid)
    # 「收到过消息但从没回过」的群没有会话线（被 @ 之前一直潜水），
    # 管理页也要列出它们才能统一开关主动发言。有会话线的以会话为准。
    for gid, stat in agent_store.recent_group_stats(aid).items():
        key = "group_" + gid
        if any(i["key"] == key for i in items):
            continue
        items.append(dict(stat, key=key, kind="group", target_id=gid,
                          name="", session=False))
    items.sort(key=lambda x: x["mtime"], reverse=True)
    names_ok = _decorate_session_names(items)
    # 每个群顺手带上「主动发言」「生图」两个开关的当前值，管理页渲染用
    settings = agent_store.load_settings(aid)
    muted = set(settings.get("interject_muted") or [])
    at_only = set(str(x) for x in (settings.get("at_only_groups") or []))
    img_muted = set(settings.get("image_gen_muted") or [])
    nai_groups = set(str(x) for x in (settings.get("nai_groups") or []))
    nai_privates = set(str(x) for x in (settings.get("nai_private") or []))
    nai_enabled = bool(settings.get("nai_enabled"))
    overrides = settings.get("interject_cooldown_overrides") or {}
    chance_ov = settings.get("interject_chance_overrides") or {}
    gap_ov = settings.get("interject_min_gap_overrides") or {}
    fmt_ov = settings.get("image_send_format_overrides") or {}
    for item in items:
        if item["kind"] == "group":
            item["interject"] = item["target_id"] not in muted
            item["at_only"] = str(item["target_id"]) in at_only
            item["image_gen"] = item["target_id"] not in img_muted
            item["nai"] = item["target_id"] in nai_groups
            ov = overrides.get(str(item["target_id"]))
            item["cooldown_override"] = ov if isinstance(ov, (int, float)) else None
            ch = chance_ov.get(str(item["target_id"]))
            item["chance_override"] = ch if isinstance(ch, (int, float)) else None
            gp = gap_ov.get(str(item["target_id"]))
            item["gap_override"] = gp if isinstance(gp, (int, float)) else None
        if item["kind"] in ("group", "private"):
            # 发图格式覆盖：群和私聊都认（None = 没单独设过，管理页显示「格式·跟全局」）
            fo = fmt_ov.get(str(item["target_id"]))
            item["image_send_format"] = (
                fo if fo in agent_store.IMAGE_SEND_FORMATS else None)
            # NSFW 审核开关：**给的是生效值**（不是覆盖值）——按钮就一个「开/关」，
            # 管理员想知道的是「这个群到底审不审」，不是「它有没有单独设过」。
            item["image_audit"] = agent_store.image_audit_enabled(
                aid, item["kind"], item["target_id"])
            # NAI 白名单：群行在上面 group 分支里算，这里补私聊行（私聊看 nai_private）。
            if item["kind"] == "private":
                item["nai"] = item["target_id"] in nai_privates
                # 私聊每日生图额度：给管理页显示「今日 3/10」和「免额」标记。
                # 只私聊行有——群聊不限额（见 agents.image_quota_allowed）。
                item["image_quota"] = agent_store.private_image_quota_info(
                    aid, item["target_id"])
    # 全局主动发言三件套（settings 里没设就回落默认），管理页输入框用
    # 私聊闸当前值：settings 优先，键缺失回落 .env（跟 qq_bot._private_gate 同口径）
    if "private_enable" in settings:
        priv_on = settings.get("private_enable") is not False
    else:
        priv_on = QQ_PRIVATE_ENABLE
    if "private_whitelist" in settings:
        priv_wl = [str(x) for x in (settings.get("private_whitelist") or [])]
    else:
        priv_wl = [str(x) for x in (QQ_WHITELIST_USERS or [])]
    return jsonify({"sessions": items, "names_ok": names_ok, "agent": aid,
                    "image_gen_on": settings.get("image_gen") is not False,
                    # 群聊总闸（2026-10-04）：开着 = 所有群都不回、私聊照常。
                    # 生效值直接取 qq_bot 判的那一份（`groups_muted()` 返回
                    # True 或群号列表，两者都算开），别在前端另算一套。
                    "groups_muted_on": bool(agent_store.groups_muted(aid)),
                    "nai_enabled": nai_enabled,
                    # 全局发图格式（群覆盖之外的总开关）。target=None 走的就是全局那层。
                    "image_send_format":
                        agent_store.image_send_format(aid, None, None),
                    # NSFW 审核的两个总开关（群聊 / 私聊分开，默认都关）。
                    # 单会话覆盖见每行的 image_audit。
                    "image_audit_globals":
                        agent_store.image_audit_globals(aid),
                    # 自定义审核提示词：prompt 是用户存的那份（可能为空 = 用默认），
                    # default 是内置默认。编辑器要拿 default 当初始内容，用户才能
                    # 从「现在实际在用的口径」开始改，而不是对着空白框瞎写。
                    "image_audit_prompt":
                        agent_store.image_audit_prompt(aid),
                    "image_audit_prompt_default":
                        image_audit.default_prompt(),
                    # 自定义**识图（通用读图）提示词**：同上，prompt 是用户存的
                    # 那份（空 = 用内置默认），default 给编辑框当初始内容。
                    # 2026-10-02 用户要求「识图老是分析不清楚，提示词我要能自己改」。
                    # 只管 agent 收图转文字那一条链路，审核和表情包各自写死。
                    "vision_prompt": agent_store.vision_prompt(aid),
                    "vision_prompt_default": vision_mod.default_prompt(),
                    # 两张**附言**（2026-10-06 用户要求「生图后的描述我也希望
                    # 可以自己加字」）：提交回执那行后面、出图 caption 那行后面
                    # 各接一段，都能在这儿改。存的那份给原值（null = 从没设过，
                    # 前端就预填 default；空串 = 用户明确不要这段字，别拿
                    # default 盖回去）。
                    "receipt_note": settings.get("receipt_note"),
                    "receipt_note_default": _receipt_note_default(),
                    "caption_note": settings.get("caption_note"),
                    "caption_note_default": _caption_note_default(),
                    "private_enable": priv_on,
                    "private_whitelist": priv_wl,
                    "private_whitelist_on":
                        settings.get("private_whitelist_on") is not False,
                    # 私聊每日生图额度（2026-09-30 加）：全局三件套 + 免额名单。
                    # limit 为 0 = 不限量，管理页显示「不限」。
                    "private_image_quota_on":
                        settings.get("private_image_quota_on") is not False,
                    "private_image_daily_limit":
                        agent_store.private_image_daily_limit(aid),
                    "private_image_quota_whitelist":
                        sorted(agent_store.private_image_quota_whitelist(aid)),
                    "session_prompts":
                        settings.get("session_prompts") or {},
                    "session_prompt_agents":
                        settings.get("session_prompt_agents") or {},
                    "session_system_prompts":
                        settings.get("session_system_prompts") or {},
                    "interject_cooldown":
                        agent_store.interject_cooldown(aid, ""),
                    "interject_chance":
                        agent_store.interject_chance(aid, ""),
                    "interject_min_gap":
                        agent_store.interject_min_gap(aid, "")})


from app.features.admin import image_settings as admin_image_settings
app.register_blueprint(admin_image_settings.bp)
























from app.features.image import admin_routes as image_admin_routes
from app.features.image.admin_routes import *  # noqa: F401,F403
app.register_blueprint(image_admin_routes.bp)
















from app.features.admin import interject as admin_interject
app.register_blueprint(admin_interject.bp)










# ─── 识图模型（对方发图给机器人时当眼睛用的那个）────────────
# 下拉里的选项**现查**不写死：本地模型会 `ollama pull`、云端模型会下线，
# 写死的清单必然腐烂（与 model_catalog 同一理由）。云端只列 PROVIDERS 里
# 标了 vision 的那些，本地列 /api/tags 里 capabilities 含 vision 的。
#
# 本地识图**很慢**（2026-10-04 实测 qwen3-vl:2b 冷启 42 秒、同一张图重复请求
# 103 秒、第三次直接 180 秒超时），所以选项里带上「本地 · 慢」字样，让用户在
# 界面上就看到代价，不用切过去踩一轮才后悔。
_VISION_LOCAL_SLOW_NOTE = "本地 · 慢（几十秒~几分钟，失败就拿不到图）"




















def _put_override(aid, group_id, field, value):
    """写 settings.json 的 <field>_overrides[群号]；value=None 表示删覆盖。"""
    settings = agent_store.load_settings(aid)
    name = field + "_overrides"
    overrides = settings.get(name)
    if not isinstance(overrides, dict):
        overrides = {}
    if value is None:
        overrides.pop(str(group_id), None)
    else:
        overrides[str(group_id)] = value
    settings[name] = overrides
    if not agent_store.save_settings(aid, settings):
        return False
    return True












# ─── 文档 API ────────────────────────────────────────

from app.features.rag import documents_api
from app.features.rag.documents_api import _safe_base_filename
app.register_blueprint(documents_api.bp)




@app.route("/api/vision/test", methods=["POST"])
def vision_test():
    """拿一张图试读一次，把**结果和耗时**回给界面。

    为什么必须有这个：本地识图慢到「切过去才发现」是不行的——用户切完才发现
    每张图要等两分钟，就晚了（2026-10-04 实测 qwen3-vl:2b 冷启 42 秒、同一张图
    重复请求 103 秒、第三次直接 180 秒超时）。而且慢的那个还有输出质量问题：
    qwen2.5-vl-abliterated:3b 试同一张图只回了「小小怪」三个字（图上确实有
    「大大怪」水印，它把水印当答案了）。这些光看模型名看不出来。

    传 `value` = 试**指定**那个（还没保存也能先试）；不传 = 试当前生效的。
    审核那条链路不参与（这里只测「对方发图给机器人」的识图）。

    ⚠️ 超时故意给 200 秒：本地实测能到 180 秒还在跑，给 30 秒会永远失败。
    这是个手动按钮、不是对话热路径，慢一点无妨。
    """
    from app.integrations import vision as vmod

    data = request.get_json(silent=True) or {}
    value = (data.get("value") or "").strip()
    if value:
        pid, _, model = value.partition(":")
        pid, model = pid.strip(), model.strip()
        if pid not in PROVIDERS:
            return jsonify({"error": "没有这个 provider：%r" % pid}), 400
    else:
        pid, model = vmod.active_choice()

    raw = (data.get("image_base64") or "").strip()
    if not raw:
        return jsonify({"error": "缺少 image_base64"}), 400
    if raw.startswith("data:"):
        raw = raw.split(",", 1)[1]
    try:
        blob = base64.b64decode(raw, validate=False)
    except Exception as e:                            # noqa: BLE001
        return jsonify({"error": "base64 解不开：%s" % e}), 400
    if not blob:
        return jsonify({"error": "图片是空的"}), 400

    try:
        data_url = vmod.to_data_url(blob)
    except Exception as e:                            # noqa: BLE001
        return jsonify({"error": "压缩失败：%s" % e}), 400

    t0 = time.monotonic()
    try:
        text = vmod.describe(data_url, timeout=200, prompt=vmod.default_prompt(),
                             provider=pid, model=model or None)
    except Exception as e:                            # noqa: BLE001
        return jsonify({"error": "识图失败（%.1f 秒）：%s"
                                 % (time.monotonic() - t0, e),
                        "elapsed": round(time.monotonic() - t0, 1),
                        "provider": pid, "model": model}), 200
    return jsonify({"ok": True, "text": text,
                    "elapsed": round(time.monotonic() - t0, 1),
                    "provider": pid, "model": model})








# ─── 启动 ──────────────────────────────────────────

def _local_ip():
    """探测本机局域网 IP（供手机/平板同网访问）"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    except Exception:
        ip = "127.0.0.1"
    finally:
        s.close()
    return ip


def run():
    # 控制台 + logs/agent.log（轮转）。从前这个进程**根本没配 logging**，
    # 所有 log.info 都被 lastResort 悄悄丢掉（它只放 WARNING 以上）——网页端
    # 的模型调用、生图、QQ 推送在日志里全程无声。这里补上。
    logsetup.setup("agent")

    # 启动时确保每个 agent 的会话文件都以稳定 system 头开始（prefix cache 锚点）。
    # agents/ 下一个目录都没有时，也要保证默认 agent 可用，否则第一次聊天
    # 会缺 system 头（各路由虽有兜底，但启动时铺好更省事）。
    agent_ids = [a["id"] for a in agent_store.list_agents()] or [DEFAULT_AGENT_ID]
    for aid in agent_ids:
        _ensure_system_prompt(aid)

    # 游客会话目录现状（2026-11-03）。
    # **只报不删**——用户要自己手动清（原话：「我有时间也会自己去手动清洗」）。
    # 这里把超过 24 小时的目录点名出来，方便他按目录删。
    try:
        from app.gateways import guest
        guest.log_status()
    except Exception:                              # noqa: BLE001
        logging.getLogger("main").exception("游客目录检查失败")

    # 角色扮演 RAG 预热（2026-11-08）：daemon 懒启动要 3~8 秒。不预热的话
    # 重启后第一轮检索必然返回空，模型手里一条记忆都没有，只能从 12 条窗口
    # 里编（实测把「门板被撞见那次」编成了「第一次」）。异步，不阻塞启动。
    try:
        from app.features.persona import rag as persona_rag
        persona_rag.warm()
    except Exception:                                # noqa: BLE001
        logging.getLogger("main").exception("RAG 预热失败")

    host = "0.0.0.0"  # 监听所有网卡，允许手机/iPad 局域网访问
    # 公网直连由 `_public_source_guard` 拦（source IP 不是私网就 403），
    # 所以这里保持 0.0.0.0 仍然安全。
    ip = _local_ip()
    print("=" * 50)
    print("  My Agent - Personal AI Assistant")
    print("  Model: " + MODEL)
    print("  本机: http://localhost:" + str(AGENT_PORT))
    print("  局域网: http://" + ip + ":" + str(AGENT_PORT))
    print("  Agents: " + ", ".join(agent_ids))
    print("  会话(默认 " + DEFAULT_AGENT_ID + "): "
          + str(len(load_history(DEFAULT_AGENT_ID))) + " messages")
    print("  Skills: " + ", ".join(list_skills()))
    print("=" * 50)
    _run_kw = {}
    if _QuietRequestHandler is not None:
        _run_kw["request_handler"] = _QuietRequestHandler
    app.run(host=host, port=AGENT_PORT, debug=False, **_run_kw)


if __name__ == "__main__":
    run()
