"""
Flask Web 服务入口
"""

import re
import json
import base64
import socket
import time
import requests
from flask import (Flask, request, jsonify, send_from_directory, Response,
                   stream_with_context)
from app import agents as agent_store
from app import cancel as cancel_mod
from app import image_audit
from app import logsetup
from app import usage as usage_stats
# 别名 `vision_mod` 是**必须**的：本文件里已有一个叫 `vision` 的路由函数
# （POST /api/vision，网页端「让 AI 看图」），模块名直接被它盖掉，
# 写成 `from app import vision` 会在调用处炸 AttributeError:
# 'function' object has no attribute 'default_prompt'。
from app import vision as vision_mod
from app.config import (AGENT_PORT, WEB_DIR, COMFYUI_URL, MODEL, DOCUMENTS_DIR,
                        LLM_PROVIDER, PROVIDERS, OLLAMA_BASE_URL, DEFAULT_AGENT_ID,
                        ADMIN_ALLOW_REMOTE, CONTEXT_BUDGET, IMAGE_AUDIT_PROMPT_MAX,
                        QQ_PRIVATE_ENABLE, QQ_WHITELIST_USERS,
                        VISION_PROMPT_MAX, VISION_PROVIDER, VISION_MODEL,
                        QQ_AGENT_ID, provider_vision)
from app.skills import list_skills, load_skill
from app import model_catalog
from app.agent_prompt import build_stable_prompt, sync_session_system
from app.memory import load_history, save_history, estimate_messages
from app.agent import run_agent_stream, parse_tool_calls, _strip_tool_blocks
from app.tools import execute_tool
from app.tools.normal.documents import list_documents, file_info, read_document, search_document

app = Flask(__name__, static_folder=WEB_DIR, static_url_path="")

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

@app.route("/")
def index():
    return send_from_directory(WEB_DIR, "index.html")


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


@app.route("/api/providers")
def get_providers():
    """返回支持的模型提供商列表（供前端下拉）"""
    items = []
    for key, cfg in PROVIDERS.items():
        items.append({
            "id": key,
            "label": cfg["label"],
            "model": cfg["model"],
            "base_url": cfg["base_url"],
        })
    return jsonify({"providers": items, "default": LLM_PROVIDER, "model": MODEL})


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
    return jsonify({"messages": msgs, "model": MODEL, "count": len(msgs),
                    "provider": LLM_PROVIDER, "agent": agent_id})


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
    from app.llm import call_llm

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

    from app.agent_prompt import build_stable_prompt
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


@app.route("/api/image/<filename>")
def serve_image(filename):
    """从 ComfyUI 输出目录代理图片"""
    try:
        resp = requests.get(COMFYUI_URL + "/view", params={"filename": filename}, stream=True, timeout=30)
        resp.raise_for_status()
        return Response(resp.iter_content(chunk_size=8192),
                       content_type=resp.headers.get('content-type', 'image/png'))
    except Exception as e:
        return jsonify({"error": str(e)}), 404


@app.route("/api/skills")
def get_skills():
    skills = []
    for s in list_skills():
        data = load_skill(s)
        if data:
            desc = data["skill_md"].split("\n")[0] if data["skill_md"] else ""
            skills.append({"name": s, "description": desc})
    return jsonify({"skills": skills})


# ─── 后台管理（agent 配置读写）──────────────────────
# 只开放两样：人设（prompt.md）与模型（agent.json 的 provider/model）。
# 工具 / skills 白名单刻意不做成界面——那是「配置」而不是「管理」，手编 JSON
# 能一眼看到全貌；在勾选框里漏勾一个是静默的，改错 JSON 立刻报解析错。
# 配置落在 agent 自己的目录里，仍是「文件即真相」，后台只是一层编辑器。

# 回环地址。IPv4-mapped 形式（::ffff:127.0.0.1）也要认，某些环境下 Flask
# 拿到的 remote_addr 长这样。
_LOCAL_ADDRS = ("127.0.0.1", "::1", "::ffff:127.0.0.1", "localhost")


def _admin_allowed():
    return ADMIN_ALLOW_REMOTE or request.remote_addr in _LOCAL_ADDRS


def _agent_or_400(agent_id):
    """校验路径里的 agent id。返回 (id, None) 或 (None, 错误响应)。"""
    aid = agent_store.safe_agent_id(agent_id)
    if aid is None:
        return None, (jsonify({"error": "非法的 agent id：%s" % agent_id}), 400)
    return aid, None


def _agent_detail(aid):
    """管理页需要的完整状态。provider / model 为空串表示「继承全局默认」。"""
    cfg = agent_store.agent_config(aid)
    raw = agent_store.agent_raw_config(aid)
    eff_provider = cfg["provider"] or LLM_PROVIDER
    eff_model = cfg["model"] or PROVIDERS.get(eff_provider, {}).get("model", "")
    # 识图实际生效的那份：界面选过就按界面那份，否则退回 .env。
    # active_choice 内部会重读 settings（mtime 缓存），所以管理页改完立刻变。
    _v_pid, _v_model = vision_mod.active_choice(aid)
    _v_from_env = not agent_store.vision_choice(aid)[0]
    return {
        "id": aid,
        "name": cfg["name"] or aid,
        "description": cfg["description"],
        "provider": cfg["provider"],
        "model": cfg["model"],
        "prompt_file": cfg["prompt_file"],
        "prompt": agent_store.persona_text(aid),
        # vision 一并下发：管理页在下拉里换来换去时，用它即时判断该不该提示
        # 「这个模型读不了图」，不必等保存后再问后端
        "providers": [{"id": k, "label": v["label"], "model": v["model"],
                       "vision": bool(v.get("vision"))}
                      for k, v in PROVIDERS.items()],
        "global_provider": LLM_PROVIDER,
        "global_model": MODEL,
        # 把「继承」算进去后实际会用的模型，让用户改完能立刻看到是什么效果
        "effective_provider": eff_provider,
        "effective_model": eff_model,
        # 带图能力：生效模型能不能直接读图。不能的话，带图请求会先经过识图
        # 预处理——识图固定走 vision_provider，与该 agent 自己的模型无关。
        "vision": provider_vision(eff_provider, eff_model),
        # 识图（对方发图给机器人时当眼睛用的那个）：给**实际生效**的那份，
        # 下方 novisual 提示里要显示它；`vision_from_env` = 有没有在界面上配过
        # （true 时用的是 .env 的值，改 .env 要重启才变）。
        "vision_provider": _v_pid,
        "vision_model": (_v_model
                         or PROVIDERS.get(_v_pid, {}).get("model", "")),
        "vision_from_env": _v_from_env,
        # 上下文预算：0 → 继承全局。session_tokens 是主会话的粗估体量，
        # 让「改完到底有没有用」立刻可见（网页端会话就是这一条）。
        # QQ 那些群各自一条会话线，不在这里体现，看日志里的 [cache] 行。
        "context_budget": cfg["context_budget"],
        "global_context_budget": CONTEXT_BUDGET,
        "effective_context_budget": cfg["context_budget"] or CONTEXT_BUDGET,
        "session_tokens": estimate_messages(load_history(aid)),
        # 只读展示：白名单收窄过没有（null = 不限制）。不提供编辑入口
        "tools": raw.get("tools"),
        "skills": raw.get("skills"),
    }


@app.route("/admin")
def admin_page():
    """agent 管理页（独立页面，不动 index.html）。"""
    return send_from_directory(WEB_DIR, "admin.html")


@app.route("/status")
def status_page():
    """状态后台：队列 / 轮次 / ComfyUI / NapCat 的实时看板。"""
    return send_from_directory(WEB_DIR, "status.html")


@app.route("/api/status")
def get_status():
    """状态后台的数据源。

    QQ bot 是**独立进程**，它的内存状态（哪个群在排队、生图队列）本进程
    读不到，所以走 app/qq_status.py 落盘的 JSON 文件。那个文件停更就说明
    bot 卡死或没起 —— 用 stale 标出来让前端标红，这正是「任务卡死时看不
    到后台」最需要的信号。

    ComfyUI 是本进程能直接探的（127.0.0.1 的 HTTP 服务），不必绕 bot 的
    快照，在这里查更实时。
    """
    from app import qq_status, comfy_status

    snap, stale = qq_status.read()
    snap = snap or {}

    try:
        c = comfy_status.snapshot()
        comfy = {"online": c.get("online"), "running": c.get("running", 0),
                 "pending": c.get("pending", 0)}
    except Exception as exc:
        comfy = {"online": None, "running": 0, "pending": 0,
                 "error": repr(exc)[:120]}

    ts = snap.get("ts")
    return jsonify({
        # bot 进程本身：stale = 心跳停了（卡死/没起）
        "bot": {"stale": stale,
                "age": (round(time.time() - ts, 1) if ts else None)},
        "napcat": snap.get("napcat") or {"online": None, "detail": ""},
        "last_activity_ago": snap.get("last_activity_ago"),
        "sessions": snap.get("sessions") or [],
        "jobs": snap.get("jobs")
                or {"running": None, "queued": [], "depth": 0},
        "comfy": comfy,
    })


@app.route("/api/agent/<agent_id>")
def get_agent_detail(agent_id):
    aid, err = _agent_or_400(agent_id)
    if err:
        return err
    return jsonify(_agent_detail(aid))


@app.route("/api/agent/<agent_id>", methods=["PUT"])
def put_agent_detail(agent_id):
    """保存人设、模型与上下文预算。

    只接受 prompt / provider / model / context_budget 四项。agent.json 以磁盘
    原文为底做部分更新，所以 tools / skills 这些界面没暴露的字段会原样保留
    ——直接用规范化后的配置回写会把它们冲成默认值（等于悄悄放开白名单）。
    空串 / 0 是有效值，表示「该字段继承全局默认」。
    """
    if not _admin_allowed():
        return jsonify({"error": "管理接口默认只允许本机访问，"
                                 "如需远程改 .env 的 ADMIN_ALLOW_REMOTE"}), 403
    aid, err = _agent_or_400(agent_id)
    if err:
        return err

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "请求体必须是 JSON 对象"}), 400

    if "provider" in data or "model" in data:
        for key in ("provider", "model"):
            val = data.get(key, "")
            if not isinstance(val, str):
                return jsonify({"error": "%s 必须是字符串" % key}), 400
        provider = (data.get("provider") or "").strip().lower()
        model = (data.get("model") or "").strip()
        if provider and provider not in PROVIDERS:
            return jsonify({"error": "未知的 provider：%s" % provider}), 400

        raw = agent_store.agent_raw_config(aid)
        raw["provider"] = provider
        raw["model"] = model
        if not agent_store.save_agent_config(aid, raw):
            return jsonify({"error": "写入 agent.json 失败"}), 500

    if "context_budget" in data:
        val = data.get("context_budget")
        try:
            budget = int(val)
        except (TypeError, ValueError):
            return jsonify({"error": "context_budget 必须是整数，"
                                     "0 表示继承全局"}), 400
        if budget and not (agent_store.MIN_CONTEXT_BUDGET
                           <= budget <= agent_store.MAX_CONTEXT_BUDGET):
            return jsonify({
                "error": "context_budget 需在 %d ~ %d 之间，或填 0 继承全局"
                         % (agent_store.MIN_CONTEXT_BUDGET,
                            agent_store.MAX_CONTEXT_BUDGET)}), 400

        raw = agent_store.agent_raw_config(aid)
        raw["context_budget"] = budget
        if not agent_store.save_agent_config(aid, raw):
            return jsonify({"error": "写入 agent.json 失败"}), 500

    if "prompt" in data:
        prompt = data.get("prompt")
        if not isinstance(prompt, str):
            return jsonify({"error": "prompt 必须是字符串"}), 400
        if not agent_store.save_persona(aid, prompt):
            return jsonify({"error": "写入人设文件失败"}), 500

    # 保存后回读：返回「已落盘并已生效」的状态，而不是前端提交上来的值
    detail = _agent_detail(aid)
    detail["ok"] = True
    return jsonify(detail)


# ─── 会话管理（列出 / 删除某条会话线）────────────────
# QQ 适配层让一个 agent 下挂多条互不相干的会话线（每个群、每个私聊各一条，
# 见 app/agents.py 的 session_file）。网页端原来的「清空」只够得着主会话，
# 想单独重置某个群没有入口——这里补上，语义就是「删掉这个群的聊天记录，
# 下次从零开始」。人设 / 白名单 / 触发词都在 agent.json 与 prompt.md 里，
# 不受影响。


def _decorate_session_names(items):
    """把群名 / 昵称补进每条会话线的 name，返回「名字是否全拿到了」。

    名字要问 QQ 协议端，属于锦上添花：NapCat 没开、这台机器压根没接 QQ、
    或者只是没进过某个群的好友——拿不到就退回只显示号，列表照样给全。
    所以这里把异常吞掉并如实回报 ok=False，而不是让整个接口失败。
    """
    kinds = {i["kind"] for i in items}
    if not ({"group", "private"} & kinds):
        return True

    # 局部 import：不接 QQ 的 agent 列表压根走不到这里，没必要在启动时加载
    from app import qq_api

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


@app.route("/api/agent/<agent_id>/image_gen", methods=["PUT"])
def set_agent_image_gen(agent_id):
    """切该 agent 的生图总闸（一键关闭/恢复全部 QQ 生图）。热生效。

    settings.json 的 image_gen 字段：False = 全关；缺省 = 开。
    """
    if not _admin_allowed():
        return jsonify({"error": "管理接口默认只允许本机访问，"
                                 "如需远程改 .env 的 ADMIN_ALLOW_REMOTE"}), 403
    aid, err = _agent_or_400(agent_id)
    if err:
        return err
    body = request.get_json(silent=True) or {}
    if not isinstance(body.get("enabled"), bool):
        return jsonify({"error": "需要布尔字段 enabled"}), 400

    settings = agent_store.load_settings(aid)
    settings["image_gen"] = body["enabled"]
    if not agent_store.save_settings(aid, settings):
        return jsonify({"error": "写入 settings.json 失败"}), 500
    return jsonify({"ok": True, "agent": aid, "image_gen": body["enabled"]})


@app.route("/api/agent/<agent_id>/image_gen/<group_id>", methods=["PUT"])
def set_agent_image_gen_group(agent_id, group_id):
    """切某个群的生图开关（总闸开着时才有效）。热生效。

    存 settings.json 的 image_gen_muted（「关」的语义，与 interject_muted
    同型）：名单里的群不能生图，不在名单的可以。
    """
    if not _admin_allowed():
        return jsonify({"error": "管理接口默认只允许本机访问，"
                                 "如需远程改 .env 的 ADMIN_ALLOW_REMOTE"}), 403
    aid, err = _agent_or_400(agent_id)
    if err:
        return err
    body = request.get_json(silent=True) or {}
    if "enabled" not in body or not isinstance(body["enabled"], bool):
        return jsonify({"error": "需要布尔字段 enabled"}), 400

    settings = agent_store.load_settings(aid)
    muted = set(settings.get("image_gen_muted") or [])
    if body["enabled"]:
        muted.discard(str(group_id))
    else:
        muted.add(str(group_id))
    settings["image_gen_muted"] = sorted(muted)
    if not agent_store.save_settings(aid, settings):
        return jsonify({"error": "写入 settings.json 失败"}), 500
    return jsonify({"ok": True, "agent": aid, "group": str(group_id),
                    "image_gen": body["enabled"]})


@app.route("/api/agent/<agent_id>/nai", methods=["PUT"])
def set_agent_nai(agent_id):
    """切该 agent 的 NAI 全局总闸（管理页「NAI·开 / 全关」）。热生效。

    settings.json 的 nai_enabled 字段：True = 启用；缺省 = 关（默认关，符合
    「默认关闭」的要求）。真正的紧急熔断在 .env 的 NAI_ENABLED。
    """
    if not _admin_allowed():
        return jsonify({"error": "管理接口默认只允许本机访问，"
                                 "如需远程改 .env 的 ADMIN_ALLOW_REMOTE"}), 403
    aid, err = _agent_or_400(agent_id)
    if err:
        return err
    body = request.get_json(silent=True) or {}
    if not isinstance(body.get("enabled"), bool):
        return jsonify({"error": "需要布尔字段 enabled"}), 400
    settings = agent_store.load_settings(aid)
    settings["nai_enabled"] = body["enabled"]
    if not agent_store.save_settings(aid, settings):
        return jsonify({"error": "写入 settings.json 失败"}), 500
    return jsonify({"ok": True, "agent": aid, "nai_enabled": body["enabled"]})


@app.route("/api/agent/<agent_id>/nai/<group_id>", methods=["PUT"])
def set_agent_nai_group(agent_id, group_id):
    """把某个群加入 / 移出 NAI 白名单（总闸开着时才有效）。热生效。

    存 settings.json 的 nai_groups：名单里的群能用群主的 NAI，不在名单的
    不能用——这是群主「只给那个群」诉求的代码落地。
    """
    if not _admin_allowed():
        return jsonify({"error": "管理接口默认只允许本机访问，"
                                 "如需远程改 .env 的 ADMIN_ALLOW_REMOTE"}), 403
    aid, err = _agent_or_400(agent_id)
    if err:
        return err
    body = request.get_json(silent=True) or {}
    if not isinstance(body.get("enabled"), bool):
        return jsonify({"error": "需要布尔字段 enabled"}), 400
    settings = agent_store.load_settings(aid)
    groups = set(str(x) for x in (settings.get("nai_groups") or []))
    if body["enabled"]:
        groups.add(str(group_id))
    else:
        groups.discard(str(group_id))
    settings["nai_groups"] = sorted(groups)
    if not agent_store.save_settings(aid, settings):
        return jsonify({"error": "写入 settings.json 失败"}), 500
    return jsonify({"ok": True, "agent": aid, "group": str(group_id),
                    "nai": body["enabled"]})


@app.route("/api/agent/<agent_id>/nai_private/<qq_id>", methods=["PUT"])
def set_agent_nai_private(agent_id, qq_id):
    """把某个 QQ 加入 / 移出 NAI 私聊白名单（总闸开着时才有效）。热生效。

    存 settings.json 的 nai_private：名单里的 QQ 在**私聊**里能用 NAI，不在名单
    的不能用。与群白名单 nai_groups 相互独立——同一个 QQ 的私聊和群不互推。
    """
    if not _admin_allowed():
        return jsonify({"error": "管理接口默认只允许本机访问，"
                                 "如需远程改 .env 的 ADMIN_ALLOW_REMOTE"}), 403
    aid, err = _agent_or_400(agent_id)
    if err:
        return err
    body = request.get_json(silent=True) or {}
    if not isinstance(body.get("enabled"), bool):
        return jsonify({"error": "需要布尔字段 enabled"}), 400
    settings = agent_store.load_settings(aid)
    users = set(str(x) for x in (settings.get("nai_private") or []))
    if body["enabled"]:
        users.add(str(qq_id))
    else:
        users.discard(str(qq_id))
    settings["nai_private"] = sorted(users)
    if not agent_store.save_settings(aid, settings):
        return jsonify({"error": "写入 settings.json 失败"}), 500
    return jsonify({"ok": True, "agent": aid, "qq": str(qq_id),
                    "nai": body["enabled"]})


def _image_send_format_from_body(body):
    """解析发图格式（jpg / png，大小写不敏感）；非法返回 None。"""
    v = body.get("format")
    if isinstance(v, str) and v.strip().lower() in agent_store.IMAGE_SEND_FORMATS:
        return v.strip().lower()
    return None


@app.route("/api/agent/<agent_id>/image_send_format", methods=["PUT"])
def set_agent_image_send_format(agent_id):
    """设该 agent 的全局发图格式（QQ 里发出去的图用 jpg 还是 png）。热生效。

    settings.json 的 image_send_format 字段：缺省 = jpg（加这个开关之前的行为）。
    只管发出去那一张，ComfyUI output 里的原图不动；网页端不受影响。
    """
    if not _admin_allowed():
        return jsonify({"error": "管理接口默认只允许本机访问，"
                                 "如需远程改 .env 的 ADMIN_ALLOW_REMOTE"}), 403
    aid, err = _agent_or_400(agent_id)
    if err:
        return err
    body = request.get_json(silent=True) or {}
    fmt = _image_send_format_from_body(body)
    if fmt is None:
        return jsonify({"error": "format 只能是 jpg 或 png"}), 400

    settings = agent_store.load_settings(aid)
    settings["image_send_format"] = fmt
    if not agent_store.save_settings(aid, settings):
        return jsonify({"error": "写入 settings.json 失败"}), 500
    return jsonify({"ok": True, "agent": aid, "image_send_format": fmt})


@app.route("/api/agent/<agent_id>/image_send_format/<group_id>", methods=["PUT"])
def set_agent_image_send_format_group(agent_id, group_id):
    """设单群发图格式覆盖。format=null 删除覆盖（回落全局值）。热生效。

    存 settings.json 的 image_send_format_overrides（与
    interject_cooldown_overrides 同型：键缺 = 跟全局）。
    """
    if not _admin_allowed():
        return jsonify({"error": "管理接口默认只允许本机访问，"
                                 "如需远程改 .env 的 ADMIN_ALLOW_REMOTE"}), 403
    aid, err = _agent_or_400(agent_id)
    if err:
        return err
    body = request.get_json(silent=True) or {}
    if "format" not in body:
        return jsonify({"error": "需要字段 format（jpg / png，或 null）"}), 400
    fmt = None
    if body["format"] is not None:
        fmt = _image_send_format_from_body(body)
        if fmt is None:
            return jsonify({"error": "format 要么是 jpg / png，"
                                     "要么是 null（删除覆盖，跟全局）"}), 400

    if not _put_override(aid, group_id, "image_send_format", fmt):
        return jsonify({"error": "写入 settings.json 失败"}), 500
    return jsonify({"ok": True, "agent": aid, "group": str(group_id),
                    "image_send_format": fmt})


def _set_audit_global(agent_id, scope):
    """两个总开关共用的实现（scope = "group" / "private"）。"""
    if not _admin_allowed():
        return jsonify({"error": "管理接口默认只允许本机访问，"
                                 "如需远程改 .env 的 ADMIN_ALLOW_REMOTE"}), 403
    aid, err = _agent_or_400(agent_id)
    if err:
        return err
    body = request.get_json(silent=True) or {}
    if not isinstance(body.get("enabled"), bool):
        return jsonify({"error": "需要布尔字段 enabled"}), 400

    settings = agent_store.load_settings(aid)
    settings["image_audit_" + ("groups" if scope == "group" else "private")] = \
        body["enabled"]
    if not agent_store.save_settings(aid, settings):
        return jsonify({"error": "写入 settings.json 失败"}), 500
    return jsonify({"ok": True, "agent": aid, "scope": scope,
                    "image_audit": body["enabled"]})


@app.route("/api/agent/<agent_id>/image_audit_groups", methods=["PUT"])
def set_agent_image_audit_groups(agent_id):
    """设**所有群聊**的 NSFW 审核总开关。热生效。

    settings.json 的 image_audit_groups 字段：缺省 = False（默认全关）。
    开了之后，**所有群**生图发出去之前都会先让识图模型判一次（裸露 / 性暗示 /
    暧昧动作，口径适中：泳装内衣这类穿着本身放行，配性暗示动作或表情才算）；
    判违规**或审核没生效**都不发，各回一句提示（fail-closed，见 app/image_audit.py）。
    单个群想单独不同，用行里的「审核」开关覆盖。
    """
    return _set_audit_global(agent_id, "group")


@app.route("/api/agent/<agent_id>/image_audit_private", methods=["PUT"])
def set_agent_image_audit_private(agent_id):
    """设**所有私聊**的 NSFW 审核总开关。热生效。

    跟 image_audit_groups 是**两个独立的开关**——只给群开、私聊不开（或反过来）
    是常见需求，合成一个的话每次都得再按会话类型逐个点。存
    settings.json 的 image_audit_private 字段，缺省 = False。
    """
    return _set_audit_global(agent_id, "private")


@app.route("/api/agent/<agent_id>/image_audit_prompt", methods=["PUT"])
def set_agent_image_audit_prompt(agent_id):
    """设**自定义的审核提示词**（整份替换内置默认）。热生效。

    body: {"prompt": "..."}。传空串 / null / 空白 = 删掉自定义，回落内置默认
    （不是「用空提示词去审」——那等于不审，见 agents.image_audit_prompt）。

    ⚠️ 自定义的提示词必须自己保留「只输出一行 JSON」那条约定（`parse_verdict`
    只认它，`category` 只能是 ok|skin|sexual|nudity|other）。丢了输出格式 =
    每张图都解析失败 = fail-closed = **所有图都发不出去**。所以这里只做长度
    上限和类型校验，不替用户往文本里塞东西——塞了他就不知道自己到底在审什么了。
    """
    if not _admin_allowed():
        return jsonify({"error": "管理接口默认只允许本机访问，"
                                 "如需远程改 .env 的 ADMIN_ALLOW_REMOTE"}), 403
    aid, err = _agent_or_400(agent_id)
    if err:
        return err
    body = request.get_json(silent=True) or {}
    if "prompt" not in body:
        return jsonify({"error": "需要字段 prompt（字符串；空串 = 恢复默认）"}), 400
    v = body["prompt"]
    if v is not None and not isinstance(v, str):
        return jsonify({"error": "prompt 要么是字符串，要么是 null"}), 400
    v = (v or "").strip()
    if len(v) > IMAGE_AUDIT_PROMPT_MAX:
        return jsonify({"error": "提示词太长（%d 字，上限 %d）：审核提示词每张图都要"
                                 "重发一遍，写长了纯烧 token"
                                 % (len(v), IMAGE_AUDIT_PROMPT_MAX)}), 400

    settings = agent_store.load_settings(aid)
    if v:
        settings["image_audit_prompt"] = v
    else:
        settings.pop("image_audit_prompt", None)
    if not agent_store.save_settings(aid, settings):
        return jsonify({"error": "写入 settings.json 失败"}), 500
    return jsonify({"ok": True, "agent": aid, "prompt": v,
                    "using_default": not v,
                    "effective": v or image_audit.default_prompt()})


@app.route("/api/agent/<agent_id>/vision_prompt", methods=["PUT"])
def set_agent_vision_prompt(agent_id):
    """设**自定义的识图提示词**（整份替换内置默认那份通用读图要求）。热生效。

    body: {"prompt": "..."}。传空串 / null / 空白 = 删掉自定义，回落内置默认
    （见 agents.vision_prompt）。

    与审核提示词最大的不同：**这里丢了输出格式 nothing 会坏**。识图的输出只是
    一段文字，拼进用户输入给下游模型看，写宽写窄都只是「读得细不细」，不像
    审核那样解析失败就 fail-closed 把每张图拦下。所以校验只有类型 + 长度上限
    （上限的理由见 config.VISION_PROMPT_MAX：识图每张图都要重发这一段）。

    唯一的软约定：文本里可以放 `{question}` 占位符，用户问题会替到那个位置；
    不放则在末尾补一行「用户的需求：…」。两种写法都保证识图模型知道用户想问
    什么——不带问题的话它只会泛泛描述一遍（见 app/vision.py 的注释）。
    """
    if not _admin_allowed():
        return jsonify({"error": "管理接口默认只允许本机访问，"
                                 "如需远程改 .env 的 ADMIN_ALLOW_REMOTE"}), 403
    aid, err = _agent_or_400(agent_id)
    if err:
        return err
    body = request.get_json(silent=True) or {}
    if "prompt" not in body:
        return jsonify({"error": "需要字段 prompt（字符串；空串 = 恢复默认）"}), 400
    v = body["prompt"]
    if v is not None and not isinstance(v, str):
        return jsonify({"error": "prompt 要么是字符串，要么是 null"}), 400
    v = (v or "").strip()
    if len(v) > VISION_PROMPT_MAX:
        return jsonify({"error": "提示词太长（%d 字，上限 %d）：识图每张图都要"
                                 "重发一遍，写长了纯烧 token"
                                 % (len(v), VISION_PROMPT_MAX)}), 400

    settings = agent_store.load_settings(aid)
    if v:
        settings["vision_prompt"] = v
    else:
        settings.pop("vision_prompt", None)
    if not agent_store.save_settings(aid, settings):
        return jsonify({"error": "写入 settings.json 失败"}), 500
    return jsonify({"ok": True, "agent": aid, "prompt": v,
                    "using_default": not v,
                    "effective": v or vision_mod.default_prompt()})


@app.route("/api/agent/<agent_id>/image_audit/<group_id>", methods=["PUT"])
def set_agent_image_audit_group(agent_id, group_id):
    """设单会话的审核覆盖（群号 / 对方 QQ 号都走这里）。enabled=null 删覆盖。

    存 settings.json 的 image_audit_overrides（与 image_send_format_overrides
    同型：键缺 = 跟全局）。群和私聊共用一张表，见 agents.image_audit_enabled。
    """
    if not _admin_allowed():
        return jsonify({"error": "管理接口默认只允许本机访问，"
                                 "如需远程改 .env 的 ADMIN_ALLOW_REMOTE"}), 403
    aid, err = _agent_or_400(agent_id)
    if err:
        return err
    body = request.get_json(silent=True) or {}
    if "enabled" not in body:
        return jsonify({"error": "需要字段 enabled（true / false，或 null）"}), 400
    v = body["enabled"]
    if v is not None and not isinstance(v, bool):
        return jsonify({"error": "enabled 要么是 true / false，"
                                 "要么是 null（删除覆盖，跟全局）"}), 400

    if not _put_override(aid, group_id, "image_audit", v):
        return jsonify({"error": "写入 settings.json 失败"}), 500
    return jsonify({"ok": True, "agent": aid, "group": str(group_id),
                    "image_audit": v})


@app.route("/api/agent/<agent_id>/private_enable", methods=["PUT"])
def set_agent_private_enable(agent_id):
    """切私聊总开关（一键关掉/恢复所有私聊，白名单都不看）。热生效。

    settings.json 的 private_enable 字段：False = 私聊全关；缺省回落 .env。
    """
    if not _admin_allowed():
        return jsonify({"error": "管理接口默认只允许本机访问，"
                                 "如需远程改 .env 的 ADMIN_ALLOW_REMOTE"}), 403
    aid, err = _agent_or_400(agent_id)
    if err:
        return err
    body = request.get_json(silent=True) or {}
    if not isinstance(body.get("enabled"), bool):
        return jsonify({"error": "需要布尔字段 enabled"}), 400

    settings = agent_store.load_settings(aid)
    settings["private_enable"] = body["enabled"]
    if not agent_store.save_settings(aid, settings):
        return jsonify({"error": "写入 settings.json 失败"}), 500
    return jsonify({"ok": True, "agent": aid, "private_enable": body["enabled"]})


@app.route("/api/agent/<agent_id>/private_whitelist_on", methods=["PUT"])
def set_agent_private_whitelist_on(agent_id):
    """切白名单开关。False = 名单不生效，任何人的私聊都放行（临时放开用）。

    存 settings.json 的 private_whitelist_on 字段，缺省视为 True。
    黑名单和私聊总开关不受影响，照常拦截。热生效。
    """
    if not _admin_allowed():
        return jsonify({"error": "管理接口默认只允许本机访问，"
                                 "如需远程改 .env 的 ADMIN_ALLOW_REMOTE"}), 403
    aid, err = _agent_or_400(agent_id)
    if err:
        return err
    body = request.get_json(silent=True) or {}
    if not isinstance(body.get("enabled"), bool):
        return jsonify({"error": "需要布尔字段 enabled"}), 400

    settings = agent_store.load_settings(aid)
    settings["private_whitelist_on"] = body["enabled"]
    if not agent_store.save_settings(aid, settings):
        return jsonify({"error": "写入 settings.json 失败"}), 500
    return jsonify({"ok": True, "agent": aid,
                    "private_whitelist_on": body["enabled"]})


@app.route("/api/agent/<agent_id>/private_image_quota", methods=["PUT"])
def set_agent_private_image_quota(agent_id):
    """设私聊每日生图额度：开关 + 每人每天上限。热生效。

    settings.json 两个字段：
    - `private_image_quota_on`：False = 不限量（**数字留着**，方便临时放开再开回来）；
    - `private_image_daily_limit`：每人每天张数，0 = 不限。
    只对**私聊**生效，群聊一行都不占（见 agents.image_quota_allowed）。
    """
    if not _admin_allowed():
        return jsonify({"error": "管理接口默认只允许本机访问，"
                                 "如需远程改 .env 的 ADMIN_ALLOW_REMOTE"}), 403
    aid, err = _agent_or_400(agent_id)
    if err:
        return err
    body = request.get_json(silent=True) or {}
    if "enabled" not in body and "limit" not in body:
        return jsonify({"error": "至少要给 enabled 或 limit 之一"}), 400
    if "enabled" in body and not isinstance(body["enabled"], bool):
        return jsonify({"error": "enabled 需要布尔值"}), 400
    if "limit" in body:
        v = body["limit"]
        # 只收非负整数：bool 是 int 的子类，得单独挡掉（True 会变成 1 张）
        if isinstance(v, bool) or not isinstance(v, int) or v < 0:
            return jsonify({"error": "limit 需要 0 或正整数（0 = 不限量）"}), 400

    settings = agent_store.load_settings(aid)
    if "enabled" in body:
        settings["private_image_quota_on"] = body["enabled"]
    if "limit" in body:
        settings["private_image_daily_limit"] = body["limit"]
    if not agent_store.save_settings(aid, settings):
        return jsonify({"error": "写入 settings.json 失败"}), 500
    return jsonify({"ok": True, "agent": aid,
                    "private_image_quota_on":
                        settings.get("private_image_quota_on") is not False,
                    # 回**原始**数字而不是生效值：开关关掉时生效值是 0，
                    # 直接回它会把输入框清成 0，管理员就看不见原配置了。
                    "private_image_daily_limit":
                        agent_store.private_image_daily_limit_raw(aid)})


@app.route("/api/agent/<agent_id>/private_image_quota_whitelist/<qq_id>",
           methods=["PUT"])
def set_agent_private_image_quota_whitelist(agent_id, qq_id):
    """把某个 QQ 加入 / 移出「私聊生图不限量」名单。热生效。

    存 settings.json 的 `private_image_quota_whitelist`。**和 private_whitelist
    是两回事**：那个管「谁能私聊」，这个管「谁生图不限量」——混用会导致哪天把
    私聊白名单打开时，名单里的人顺带变成生图不限量。
    """
    if not _admin_allowed():
        return jsonify({"error": "管理接口默认只允许本机访问，"
                                 "如需远程改 .env 的 ADMIN_ALLOW_REMOTE"}), 403
    aid, err = _agent_or_400(agent_id)
    if err:
        return err
    body = request.get_json(silent=True) or {}
    if not isinstance(body.get("enabled"), bool):
        return jsonify({"error": "需要布尔字段 enabled"}), 400
    settings = agent_store.load_settings(aid)
    users = set(str(x) for x in (settings.get("private_image_quota_whitelist") or []))
    if body["enabled"]:
        users.add(str(qq_id))
    else:
        users.discard(str(qq_id))
    settings["private_image_quota_whitelist"] = sorted(users)
    if not agent_store.save_settings(aid, settings):
        return jsonify({"error": "写入 settings.json 失败"}), 500
    return jsonify({"ok": True, "agent": aid, "qq": str(qq_id),
                    "whitelisted": body["enabled"]})


@app.route("/api/agent/<agent_id>/private_whitelist", methods=["PUT"])
def edit_agent_private_whitelist(agent_id):
    """私聊白名单增删（op: add / remove）。热生效。

    存 settings.json 的 private_whitelist。只要这个键被写进 settings，
    语义就是真白名单：名单外的 QQ（包括陌生人临时会话）一律静默不回，
    名单清空 = 谁都私聊不了。
    """
    if not _admin_allowed():
        return jsonify({"error": "管理接口默认只允许本机访问，"
                                 "如需远程改 .env 的 ADMIN_ALLOW_REMOTE"}), 403
    aid, err = _agent_or_400(agent_id)
    if err:
        return err
    body = request.get_json(silent=True) or {}
    op = str(body.get("op") or "")
    user_id = str(body.get("user_id") or "").strip()
    if op not in ("add", "remove"):
        return jsonify({"error": "op 需要 add 或 remove"}), 400
    if not user_id.isdigit():
        return jsonify({"error": "user_id 需要是 QQ 号（纯数字）"}), 400

    settings = agent_store.load_settings(aid)
    wl = [str(x) for x in (settings.get("private_whitelist") or [])]
    if op == "add":
        if user_id not in wl:
            wl.append(user_id)
    else:
        wl = [x for x in wl if x != user_id]
    wl = sorted(set(wl))
    settings["private_whitelist"] = wl
    if not agent_store.save_settings(aid, settings):
        return jsonify({"error": "写入 settings.json 失败"}), 500
    return jsonify({"ok": True, "agent": aid, "op": op,
                    "private_whitelist": wl})


@app.route("/api/agent/<agent_id>/session_prompt/<path:session_key>",
           methods=["PUT"])
def set_agent_session_prompt(agent_id, session_key):
    """保存某个会话线（group_<群号> / private_<QQ号>）的提示词配置。

    三个字段各自独立、互不覆盖，空串 = 清除该项：
    - text   → session_prompts（会话附加词，拼在人设后面）
    - agent  → session_prompt_agents（借用某个 agent 的完整系统提示词当基底）
    - system → session_system_prompts（人设覆盖：只顶 prompt.md 那一层，
               工具列表 / Skill 目录 / 环境说明照常动态拼装；优先于借用）
    热生效：qq_bot 每轮重建首条 system。
    """
    if not _admin_allowed():
        return jsonify({"error": "管理接口默认只允许本机访问，"
                                 "如需远程改 .env 的 ADMIN_ALLOW_REMOTE"}), 403
    aid, err = _agent_or_400(agent_id)
    if err:
        return err
    body = request.get_json(silent=True) or {}
    if not isinstance(body.get("text"), str):
        return jsonify({"error": "需要字符串字段 text（空串=清除自定义）"}), 400
    if "system" in body and not isinstance(body.get("system"), str):
        return jsonify({"error": "system 需要字符串（空串=清除独立提示词）"}), 400

    borrow = str(body.get("agent") or "").strip()
    if borrow:
        from app.agents import safe_agent_id
        if safe_agent_id(borrow) is None or borrow not in {
                a["id"] for a in agent_store.list_agents()}:
            return jsonify({"error": "要借用的 agent 不存在：" + borrow}), 400

    settings = agent_store.load_settings(aid)

    # 完整独立提示词：优先级最高。存成独立字典，不跟另外两项互相覆盖——
    # 这样清了它，原来的借用/附加词还能原样回来。
    systems = settings.get("session_system_prompts")
    if not isinstance(systems, dict):
        systems = {}
    system = str(body.get("system") or "").strip()
    if system:
        systems[session_key] = system
    else:
        systems.pop(session_key, None)
    if systems:
        settings["session_system_prompts"] = systems
    else:
        settings.pop("session_system_prompts", None)

    prompts = settings.get("session_prompts")
    if not isinstance(prompts, dict):
        prompts = {}
    text = body["text"].strip()
    if text:
        prompts[session_key] = text
    else:
        prompts.pop(session_key, None)
    settings["session_prompts"] = prompts

    borrowed = settings.get("session_prompt_agents")
    if not isinstance(borrowed, dict):
        borrowed = {}
    if borrow and borrow != aid:
        borrowed[session_key] = borrow
    else:
        borrowed.pop(session_key, None)
    if borrowed:
        settings["session_prompt_agents"] = borrowed
    else:
        settings.pop("session_prompt_agents", None)

    if not agent_store.save_settings(aid, settings):
        return jsonify({"error": "写入 settings.json 失败"}), 500
    return jsonify({"ok": True, "agent": aid, "session_key": session_key,
                    "set": bool(text), "borrowed": borrow or None,
                    "system": bool(system)})


@app.route("/api/agent/<agent_id>/session_prompt_bulk", methods=["PUT"])
def set_agent_session_prompt_bulk(agent_id):
    """批量保存多条会话线的提示词配置（管理页勾选多个群/私聊一键应用）。

    body：
      keys   : ["group_123", "private_456", ...] 必填、非空、最多 500 条
      clear  : true → 把这几条会话的三项配置**全部清掉**（回到 agent 自己的
               prompt.md），此时忽略下面三个字段
      text   : 会话附加词（非空才写）
      agent  : 借用助手 id（非空才写）
      system : 人设覆盖（非空才写）

    **只写非空字段 —— 没填的一律不动**（2026-10-03 用户拍板）。所以「只想清掉
    某一个字段」走单条接口 /api/agent/<id>/session_prompt/<key>，或整条 clear。

    ⚠️ 三项语义别搞混（与单条接口一致）：`system` 是**顶替** prompt.md 的人设段
    （不是叠加，抄工具目录会跟自动拼的那份重复）；`text` 是**拼**在人设后面；
    `agent` 是整份借用 TA 的系统提示词。优先级 system > agent > text。
    ⚠️ 每条被改的会话下次发言都会**整段前缀缓存作废**（system 是消息数组第一条，
    前缀缓存只认从头逐字节相同的最长前缀），约 15K token 全价 —— 一次改到位最省。

    实现上**一次 load_settings → 改 → 一次 save_settings**：绝不循环调单条接口，
    那会写 N 次盘，而且每次都在旧快照上改，彼此覆盖。
    """
    if not _admin_allowed():
        return jsonify({"error": "管理接口默认只允许本机访问，"
                                 "如需远程改 .env 的 ADMIN_ALLOW_REMOTE"}), 403
    aid, err = _agent_or_400(agent_id)
    if err:
        return err

    body = request.get_json(silent=True) or {}
    raw = body.get("keys")
    if not isinstance(raw, list) or not raw:
        return jsonify({"error": "需要非空数组字段 keys"}), 400
    if len(raw) > 500:
        return jsonify({"error": "一次最多 500 条会话"}), 400

    keys, skipped = [], []
    for k in raw:
        safe = agent_store.safe_session_key(k)
        if safe is None:
            skipped.append(str(k)[:64])
        elif safe not in keys:
            keys.append(safe)
    if not keys:
        return jsonify({"error": "keys 里没有合法的会话 key"}), 400

    for f in ("text", "system"):
        if f in body and not isinstance(body.get(f), str):
            return jsonify({"error": "%s 需要字符串" % f}), 400
    text = str(body.get("text") or "").strip()
    system = str(body.get("system") or "").strip()
    borrow = str(body.get("agent") or "").strip()
    if borrow:
        if agent_store.safe_agent_id(borrow) is None or borrow not in {
                a["id"] for a in agent_store.list_agents()}:
            return jsonify({"error": "要借用的 agent 不存在：" + borrow}), 400
    do_clear = bool(body.get("clear"))
    # 三项各自独立、互不覆盖（与单条接口同一套存储键）。**没填的不动**；
    # 借自己 = 等于不借，直接当没填（否则会 200 却什么都不写，很难查）。
    fields = (("session_system_prompts", system),
              ("session_prompts", text),
              ("session_prompt_agents", borrow))
    writes = [] if do_clear else [(n, v) for n, v in fields if v and v != aid]
    if not do_clear and not writes:
        return jsonify({"error": "text / agent / system 至少要填一项"
                                 "（借自己 = 等于不借），或者传 clear=true 清除"}), 400

    settings = agent_store.load_settings(aid)
    applied, cleared = 0, set()
    if do_clear:
        for name, _value in fields:
            d = settings.get(name)
            if not isinstance(d, dict):
                continue
            for k in keys:
                if k in d:
                    d.pop(k, None)
                    cleared.add(k)
            if d:
                settings[name] = d
            else:
                settings.pop(name, None)      # 空了就删键，settings.json 别留空壳
    else:
        for name, value in writes:
            d = settings.get(name)
            if not isinstance(d, dict):
                d = {}
            for k in keys:
                d[k] = value
            settings[name] = d
        applied = len(keys)

    if not agent_store.save_settings(aid, settings):
        return jsonify({"error": "写入 settings.json 失败"}), 500
    return jsonify({"ok": True, "agent": aid, "keys": keys,
                    "applied": applied, "cleared": len(cleared),
                    "skipped": skipped})


@app.route("/api/agent/<agent_id>/interject/<group_id>", methods=["PUT"])
def set_agent_interject(agent_id, group_id):
    """切某个群的「主动发言」开关。热生效，不用重启。

    管理类写操作与 DELETE 同一道门：默认只允许本机。设置存 settings.json
    （agent 级运行时开关），不动 agent.json（那是重启级配置）。
    """
    if not _admin_allowed():
        return jsonify({"error": "管理接口默认只允许本机访问，"
                                 "如需远程改 .env 的 ADMIN_ALLOW_REMOTE"}), 403
    aid, err = _agent_or_400(agent_id)
    if err:
        return err
    body = request.get_json(silent=True) or {}
    if "enabled" not in body or not isinstance(body["enabled"], bool):
        return jsonify({"error": "需要布尔字段 enabled"}), 400

    settings = agent_store.load_settings(aid)
    muted = set(settings.get("interject_muted") or [])
    if body["enabled"]:
        muted.discard(str(group_id))
    else:
        muted.add(str(group_id))
    settings["interject_muted"] = sorted(muted)
    if not agent_store.save_settings(aid, settings):
        return jsonify({"error": "写入 settings.json 失败"}), 500
    return jsonify({"ok": True, "agent": aid, "group": str(group_id),
                    "interject": body["enabled"]})


@app.route("/api/agent/<agent_id>/groups_muted", methods=["PUT"])
def set_groups_muted(agent_id):
    """切「所有群都不回」总开关。热生效，不用重启。

    开了之后群里连 @ 都不接（私聊照常），用于把机器人当纯私人工具。
    与逐群的 at_only 正交：那条收窄到「只认 @」，这条是「彻底不理群」。
    存 settings.json 的 groups_muted=true。

    写之前必须 read-modify-write（同 at_only：settings.json 还有 tools/skills
    白名单、会话人设、NAI 名单等键，整体覆盖会冲掉）。
    """
    if not _admin_allowed():
        return jsonify({"error": "管理接口默认只允许本机访问，"
                                 "如需远程改 .env 的 ADMIN_ALLOW_REMOTE"}), 403
    aid, err = _agent_or_400(agent_id)
    if err:
        return err
    body = request.get_json(silent=True) or {}
    if "enabled" not in body or not isinstance(body["enabled"], bool):
        return jsonify({"error": "需要布尔字段 enabled"}), 400

    settings = agent_store.load_settings(aid)
    settings["groups_muted"] = bool(body["enabled"])
    if not agent_store.save_settings(aid, settings):
        return jsonify({"error": "写入 settings.json 失败"}), 500
    return jsonify({"ok": True, "agent": aid, "groups_muted": bool(body["enabled"])})


def _local_vision_model_ids():
    """本机 ollama 里**真能读图**的模型名集合。探不到返回空集（= 跳过校验）。

    判据两条同时成立：ollama 报了 vision（`is True`，None 是不确定、不算），
    且名字里有真视觉架构标记（`_ollama_vision_untrustworthy` 说明为什么不能
    只看 capabilities）。清单接口和保存校验都调这个——两处判据必须一致，
    否则会出现「下拉里看不到但能存进去」或反之。

    ⚠️ 别把它写成集合推导式里的 `and not`：vision=True 的真模型会被
    `is not True` 一起滤掉，结果本地清单整个变空、下拉里一个本地模型都没有
    （我踩过一次）。写成显式循环，条件只有一个地方。
    """
    try:
        cat = model_catalog.list_models("ollama")
    except Exception:                               # noqa: BLE001
        return set()
    good = set()
    for m in cat.get("models") or []:
        mid = m.get("id") or ""
        if m.get("vision") is not True:
            continue
        if _ollama_vision_untrustworthy(mid):
            continue
        good.add(mid)
    return good


# ─── 识图模型（对方发图给机器人时当眼睛用的那个）────────────
# 下拉里的选项**现查**不写死：本地模型会 `ollama pull`、云端模型会下线，
# 写死的清单必然腐烂（与 model_catalog 同一理由）。云端只列 PROVIDERS 里
# 标了 vision 的那些，本地列 /api/tags 里 capabilities 含 vision 的。
#
# 本地识图**很慢**（2026-10-04 实测 qwen3-vl:2b 冷启 42 秒、同一张图重复请求
# 103 秒、第三次直接 180 秒超时），所以选项里带上「本地 · 慢」字样，让用户在
# 界面上就看到代价，不用切过去踩一轮才后悔。
_VISION_LOCAL_SLOW_NOTE = "本地 · 慢（几十秒~几分钟，失败就拿不到图）"


def _ollama_vision_untrustworthy(model_id):
    """这个本地模型「报了 vision 但实测不读图」→ 别列进识图下拉。

    ollama 的 /api/tags 里 capabilities 是**按模型名猜**的，不是实测：纯文本的
    qwen3.5-abliterated:9b 与 nexusriot/Qwen3.5-Uncensored-...:9b 都报 vision，
    真发一张图过去它不读（同款现象在 qwen3.8-9b-heretic 上实测过：HTTP 400
    does not support multimodal）。列出来等于骗用户切过去、然后每轮识图失败。

    判据看**模型名里有没有真的视觉架构标记**（vl / vision / omni），不看
    capabilities。新装了别的视觉模型时这里不用改——只挡「名字里没有任何
    视觉标记」的那批，不写死具体名字。
    """
    name = (model_id or "").lower()
    if any(k in name for k in ("vl", "vision", "-vl-", "omni")):
        return False
    # 没有任何视觉架构标记 → 一律不信 capabilities。
    return True


@app.route("/api/vision/models")
def vision_models():
    """列出可选的识图模型：云端带 vision 的 provider + 本地 ollama 视觉模型。

    带上当前生效的那个（`current`），好让前端默认选中它。
    `agent` 参数决定读谁的配置（管理页切 agent 时要跟着变），缺省用 QQ_AGENT_ID。
    """
    from app import vision as vmod

    aid = (request.args.get("agent") or QQ_AGENT_ID).strip()
    if agent_store.safe_agent_id(aid) is None:
        aid = QQ_AGENT_ID
    cur_pid, cur_model = vmod.active_choice(aid)
    items = []
    for pid, cfg in PROVIDERS.items():
        if not provider_vision(pid, None):
            continue
        # llama 是本地服务，标「云端」会误导（实测它识图 3.6 秒，也不慢）。
        if pid == "llama":
            note = "本地 llama.cpp，几秒"
        else:
            note = "云端，1~2 秒"
        items.append({
            "value": pid + ":",
            "provider": pid,
            "model": "",
            "label": "%s · %s（%s）"
                     % (cfg.get("label", pid), cfg.get("model", ""), note),
        })
    # 本地：判据只有一处（_local_vision_model_ids），别在下拉里另写一份
    local = []
    items_error = ""
    try:
        cat = model_catalog.list_models("ollama")
        good = _local_vision_model_ids()
        for mid in sorted(good):
            local.append({
                "value": "ollama:" + mid,
                "provider": "ollama",
                "model": mid,
                "label": "%s（%s）" % (mid, _VISION_LOCAL_SLOW_NOTE),
            })
        if cat.get("error"):
            items_error = "本地模型清单不完整（%s）" % cat["error"]
    except Exception as e:                        # noqa: BLE001
        # ollama 没开不是错误：云端那几项照样能用，只是本地那几项缺席。
        items_error = "拿不到本地模型清单（%s）" % e
    items.extend(local)

    cur_value = (cur_pid + ":" + (cur_model or "")) if cur_pid else ""
    return jsonify({"items": items, "current": cur_value,
                    "error": items_error,
                    "from_env": not agent_store.vision_choice(aid)[0]})


@app.route("/api/agent/<agent_id>/vision_model", methods=["PUT"])
def set_vision_model(agent_id):
    """切识图模型。热生效，不用重启（vision.active_choice 每次调用重读配置）。

    body: {"value": "ollama:qwen3-vl:2b"} 或 {"value": ""}（= 退回 .env）。

    ⚠️ 只影响「对方发图给机器人时把它读成文字」这条**对话**链路。
    生图发出去前的 NSFW 审核不跟着切（用户 2026-10-04 拍板）——审核是
    fail-closed 且超时只有 30 秒，本地那个速度会把每张图都拦死。见
    vision.audit_choice。

    校验查 PROVIDERS 有没有这个 provider、本地模型在不在 ollama 清单里：
    写错名字的话，表现是每轮识图都 HTTP 404，而用户看不出是自己拼错了。
    """
    if not _admin_allowed():
        return jsonify({"error": "管理接口默认只允许本机访问，"
                                 "如需远程改 .env 的 ADMIN_ALLOW_REMOTE"}), 403
    aid, err = _agent_or_400(agent_id)
    if err:
        return err
    body = request.get_json(silent=True) or {}
    if "value" not in body or not isinstance(body["value"], str):
        return jsonify({"error": "需要字符串字段 value"}), 400
    value = body["value"].strip()

    if value:
        pid, _, model = value.partition(":")
        pid = pid.strip()
        if pid not in PROVIDERS:
            return jsonify({"error": "没有这个 provider：%r" % pid}), 400
        if pid == "ollama" and model.strip():
            names = _local_vision_model_ids()
            if names and model.strip() not in names:
                return jsonify({"error": "本机 ollama 里没有这个能读图的模型：%r"
                                 % model.strip()}), 400

    # 写之前 read-modify-write（settings.json 还有 tools/skills 白名单等键）
    settings = agent_store.load_settings(aid)
    if value:
        settings[agent_store.VISION_MODEL_KEY] = value
    else:
        settings.pop(agent_store.VISION_MODEL_KEY, None)
    if not agent_store.save_settings(aid, settings):
        return jsonify({"error": "写入 settings.json 失败"}), 500
    pid, model = vision_mod.active_choice(aid)
    return jsonify({"ok": True, "agent": aid, "value": value,
                    "effective_provider": pid, "effective_model": model})


@app.route("/api/agent/<agent_id>/at_only/<group_id>", methods=["PUT"])
def set_group_at_only(agent_id, group_id):
    """切某个群的「只认 @」开关。热生效，不用重启。

    开了之后这个群**不再认 QQ_GROUP_KEYWORDS 那套免 @ 呼叫词**，只有真 @ 到
    才回。存 settings.json 的 at_only_groups（「加严」的语义），与
    interject_muted 的「关掉主动发言」正交——某群两个都开 = 只有 @ 才有反应。

    写之前必须 read-modify-write：settings.json 里还有 tools/skills 白名单、
    会话人设、NAI 名单等一大堆键，整体覆盖会把它们冲掉。
    """
    if not _admin_allowed():
        return jsonify({"error": "管理接口默认只允许本机访问，"
                                 "如需远程改 .env 的 ADMIN_ALLOW_REMOTE"}), 403
    aid, err = _agent_or_400(agent_id)
    if err:
        return err
    body = request.get_json(silent=True) or {}
    if "enabled" not in body or not isinstance(body["enabled"], bool):
        return jsonify({"error": "需要布尔字段 enabled"}), 400

    settings = agent_store.load_settings(aid)
    only = set(str(x) for x in (settings.get("at_only_groups") or []))
    if body["enabled"]:
        only.add(str(group_id))
    else:
        only.discard(str(group_id))
    settings["at_only_groups"] = sorted(only)
    if not agent_store.save_settings(aid, settings):
        return jsonify({"error": "写入 settings.json 失败"}), 500
    return jsonify({"ok": True, "agent": aid, "group": str(group_id),
                    "at_only": body["enabled"]})


def _cooldown_from_body(body):
    """从请求体解析冷却秒数。合法返回 0~3600 的整数（0=不限频），
    不合法/越界返回 None——写入层明确拒绝，越界收敛只在读取层兜底。"""
    v = body.get("cooldown")
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    if not (agent_store.INTERJECT_COOLDOWN_MIN
            <= v <= agent_store.INTERJECT_COOLDOWN_MAX):
        return None
    return int(v)


@app.route("/api/agent/<agent_id>/interject_cooldown", methods=["PUT"])
def set_interject_cooldown(agent_id):
    """设全局主动发言冷却秒数（settings.json 的 interject_cooldown）。热生效。

    0 = 不限频；删除限制走 PUT cooldown=0。回落 .env 默认目前没有做 UI，
    想恢复出厂就把值设成 .env 里的 QQ_INTERJECT_COOLDOWN。
    """
    if not _admin_allowed():
        return jsonify({"error": "管理接口默认只允许本机访问，"
                                 "如需远程改 .env 的 ADMIN_ALLOW_REMOTE"}), 403
    aid, err = _agent_or_400(agent_id)
    if err:
        return err
    body = request.get_json(silent=True) or {}
    cd = _cooldown_from_body(body)
    if cd is None:
        return jsonify({"error": "需要数字字段 cooldown（0~3600 秒）"}), 400

    settings = agent_store.load_settings(aid)
    settings["interject_cooldown"] = cd
    if not agent_store.save_settings(aid, settings):
        return jsonify({"error": "写入 settings.json 失败"}), 500
    return jsonify({"ok": True, "agent": aid, "cooldown": cd})


@app.route("/api/agent/<agent_id>/interject_cooldown/<group_id>",
           methods=["PUT"])
def set_interject_cooldown_group(agent_id, group_id):
    """设单群主动发言冷却覆盖。cooldown=null 删除覆盖（回落全局值）。"""
    if not _admin_allowed():
        return jsonify({"error": "管理接口默认只允许本机访问，"
                                 "如需远程改 .env 的 ADMIN_ALLOW_REMOTE"}), 403
    aid, err = _agent_or_400(agent_id)
    if err:
        return err
    body = request.get_json(silent=True) or {}
    if "cooldown" not in body:
        return jsonify({"error": "需要字段 cooldown（0~3600 的数字，或 null）"}), 400
    if body["cooldown"] is None:
        cd = None
    else:
        cd = _cooldown_from_body(body)
        if cd is None:
            return jsonify({"error": "cooldown 要么是 0~3600 的数字，"
                                     "要么是 null（删除覆盖）"}), 400

    settings = agent_store.load_settings(aid)
    overrides = settings.get("interject_cooldown_overrides")
    if not isinstance(overrides, dict):
        overrides = {}
    if cd is None:
        overrides.pop(str(group_id), None)
    else:
        overrides[str(group_id)] = cd
    settings["interject_cooldown_overrides"] = overrides
    if not agent_store.save_settings(aid, settings):
        return jsonify({"error": "写入 settings.json 失败"}), 500
    return jsonify({"ok": True, "agent": aid, "group": str(group_id),
                    "cooldown": cd})


def _percent_from_body(body):
    """解析 0~100 的整数百分比；非数字/越界返回 None（写入层明确拒绝）。"""
    v = body.get("chance")
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    if not 0 <= v <= 100:
        return None
    return int(v)


def _gap_from_body(body):
    """解析判断间隔秒数（0~3600）。0 = 不做这道闸。"""
    v = body.get("min_gap")
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    if not (agent_store.INTERJECT_GAP_MIN <= v <= agent_store.INTERJECT_GAP_MAX):
        return None
    return int(v)


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


@app.route("/api/agent/<agent_id>/interject_chance", methods=["PUT"])
def set_interject_chance(agent_id):
    """设全局主动发言的触发概率（百分比整数）。热生效。

    12 ≈ 1/8（默认）；0 = 从不主动开口；100 = 每批消息都去问模型。
    """
    if not _admin_allowed():
        return jsonify({"error": "管理接口默认只允许本机访问，"
                                 "如需远程改 .env 的 ADMIN_ALLOW_REMOTE"}), 403
    aid, err = _agent_or_400(agent_id)
    if err:
        return err
    body = request.get_json(silent=True) or {}
    chance = _percent_from_body(body)
    if chance is None:
        return jsonify({"error": "需要数字字段 chance（0~100）"}), 400
    settings = agent_store.load_settings(aid)
    settings["interject_chance"] = chance
    if not agent_store.save_settings(aid, settings):
        return jsonify({"error": "写入 settings.json 失败"}), 500
    return jsonify({"ok": True, "agent": aid, "chance": chance})


@app.route("/api/agent/<agent_id>/interject_chance/<group_id>",
           methods=["PUT"])
def set_interject_chance_group(agent_id, group_id):
    """设单群触发概率覆盖。chance=null 删除覆盖（回落全局值）。"""
    if not _admin_allowed():
        return jsonify({"error": "管理接口默认只允许本机访问，"
                                 "如需远程改 .env 的 ADMIN_ALLOW_REMOTE"}), 403
    aid, err = _agent_or_400(agent_id)
    if err:
        return err
    body = request.get_json(silent=True) or {}
    if "chance" not in body:
        return jsonify({"error": "需要字段 chance（0~100 的数字，或 null）"}), 400
    if body["chance"] is None:
        chance = None
    else:
        chance = _percent_from_body(body)
        if chance is None:
            return jsonify({"error": "chance 要么是 0~100 的数字，"
                                     "要么是 null（删除覆盖）"}), 400
    if not _put_override(aid, group_id, "interject_chance", chance):
        return jsonify({"error": "写入 settings.json 失败"}), 500
    return jsonify({"ok": True, "agent": aid, "group": str(group_id),
                    "chance": chance})


@app.route("/api/agent/<agent_id>/interject_min_gap", methods=["PUT"])
def set_interject_min_gap(agent_id):
    """设全局「两次判断之间的最小秒数」。热生效。

    判断也是一次 API 调用，这道闸防刷屏时每条都问。0 = 不做这道闸
    （那就只剩概率门挡着）。
    """
    if not _admin_allowed():
        return jsonify({"error": "管理接口默认只允许本机访问，"
                                 "如需远程改 .env 的 ADMIN_ALLOW_REMOTE"}), 403
    aid, err = _agent_or_400(agent_id)
    if err:
        return err
    body = request.get_json(silent=True) or {}
    gap = _gap_from_body(body)
    if gap is None:
        return jsonify({"error": "需要数字字段 min_gap（0~3600 秒）"}), 400
    settings = agent_store.load_settings(aid)
    settings["interject_min_gap"] = gap
    if not agent_store.save_settings(aid, settings):
        return jsonify({"error": "写入 settings.json 失败"}), 500
    return jsonify({"ok": True, "agent": aid, "min_gap": gap})


@app.route("/api/agent/<agent_id>/interject_min_gap/<group_id>",
           methods=["PUT"])
def set_interject_min_gap_group(agent_id, group_id):
    """设单群判断间隔覆盖。min_gap=null 删除覆盖（回落全局值）。"""
    if not _admin_allowed():
        return jsonify({"error": "管理接口默认只允许本机访问，"
                                 "如需远程改 .env 的 ADMIN_ALLOW_REMOTE"}), 403
    aid, err = _agent_or_400(agent_id)
    if err:
        return err
    body = request.get_json(silent=True) or {}
    if "min_gap" not in body:
        return jsonify({"error": "需要字段 min_gap（0~3600 的数字，或 null）"}), 400
    if body["min_gap"] is None:
        gap = None
    else:
        gap = _gap_from_body(body)
        if gap is None:
            return jsonify({"error": "min_gap 要么是 0~3600 的数字，"
                                     "要么是 null（删除覆盖）"}), 400
    if not _put_override(aid, group_id, "interject_min_gap", gap):
        return jsonify({"error": "写入 settings.json 失败"}), 500
    return jsonify({"ok": True, "agent": aid, "group": str(group_id),
                    "min_gap": gap})


@app.route("/api/agent/<agent_id>/sessions/<key>", methods=["DELETE"])
def delete_agent_session(agent_id, key):
    """删除一条会话线（= 重置这条对话）。不可恢复。

    与 PUT 同一道门：管理类写操作默认只允许本机。二次确认在前端做，
    后端不做「要不要删」的判断——它只守住「谁有资格删」。
    """
    if not _admin_allowed():
        return jsonify({"error": "管理接口默认只允许本机访问，"
                                 "如需远程改 .env 的 ADMIN_ALLOW_REMOTE"}), 403
    aid, err = _agent_or_400(agent_id)
    if err:
        return err

    ok, msg = agent_store.delete_session(aid, key)
    if not ok:
        return jsonify({"error": msg}), (404 if msg == "会话不存在" else 400)
    return jsonify({"ok": True, "agent": aid, "key": key})


# ─── 文档 API ────────────────────────────────────────

@app.route("/api/documents")
def api_list_documents():
    """列出所有文档"""
    import os
    os.makedirs(DOCUMENTS_DIR, exist_ok=True)
    result = []
    for root, dirs, files in os.walk(DOCUMENTS_DIR):
        for f in files:
            full_path = os.path.join(root, f)
            rel_path = os.path.relpath(full_path, DOCUMENTS_DIR)
            size = os.path.getsize(full_path)
            try:
                with open(full_path, "r", encoding="utf-8", errors="ignore") as fh:
                    lines = len(fh.readlines())
            except:
                lines = -1
            result.append({
                "name": rel_path,
                "size": size,
                "lines": lines
            })
    return jsonify({"files": result})


def _safe_base_filename(fname):
    """净化上传文件名：保留中文等 Unicode 字符，仅去除路径分隔符和危险字符防穿越"""
    fname = fname.replace("\\", "/").split("/")[-1].strip()
    # 只允许 中文/字母/数字/下划线/中划线/空格/点，去其他危险字符
    cleaned = re.sub(r"[^\w\u4e00-\u9fff. \-]", "", fname, flags=re.UNICODE)
    cleaned = cleaned.strip(". ")
    if not cleaned:
        cleaned = "unnamed.txt"
    return cleaned


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
    from app import vision as vmod

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


@app.route("/api/documents/upload", methods=["POST"])
def api_upload_document():
    """上传文档到 documents 目录"""
    import os

    os.makedirs(DOCUMENTS_DIR, exist_ok=True)

    if "file" not in request.files:
        return jsonify({"error": "没有文件"}), 400

    f = request.files["file"]
    if not f.filename:
        return jsonify({"error": "文件名为空"}), 400

    filename = _safe_base_filename(f.filename)
    filepath = os.path.join(DOCUMENTS_DIR, filename)
    f.save(filepath)

    size = os.path.getsize(filepath)
    try:
        with open(filepath, "r", encoding="utf-8", errors="ignore") as fh:
            lines = len(fh.readlines())
    except:
        lines = -1

    return jsonify({
        "ok": True,
        "name": filename,
        "size": size,
        "lines": lines
    })


@app.route("/api/documents/<path:filename>")
def api_get_document(filename):
    """读取文档内容（带行号，支持 offset/limit 参数）"""
    offset = request.args.get("offset", "1")
    limit = request.args.get("limit", "100")
    try:
        offset = int(offset)
        limit = int(limit)
    except ValueError:
        offset = 1
        limit = 100

    content = read_document(filename, offset=offset, limit=limit)
    return jsonify({"content": content})


@app.route("/api/documents/<path:filename>", methods=["DELETE"])
def api_delete_document(filename):
    """删除文档"""
    import os

    # 安全校验：确保在 documents 目录内
    safe_name = filename.replace("..", "").lstrip("/").lstrip("\\").strip()
    if not safe_name:
        return jsonify({"error": "文件名非法"}), 400

    filepath = os.path.join(DOCUMENTS_DIR, safe_name)
    try:
        real_target = os.path.realpath(filepath)
        real_docs = os.path.realpath(DOCUMENTS_DIR)
        if not real_target.startswith(real_docs):
            return jsonify({"error": "路径非法"}), 400
    except Exception:
        return jsonify({"error": "路径非法"}), 400

    if not os.path.exists(filepath):
        return jsonify({"error": "文件不存在"}), 404

    os.remove(filepath)
    return jsonify({"ok": True})


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

    host = "0.0.0.0"  # 监听所有网卡，允许手机/iPad 局域网访问
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
    app.run(host=host, port=AGENT_PORT, debug=False)


if __name__ == "__main__":
    run()
