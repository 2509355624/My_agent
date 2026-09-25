"""
Flask Web 服务入口
"""

import re
import json
import socket
import requests
from flask import (Flask, request, jsonify, send_from_directory, Response,
                   stream_with_context)
from app import agents as agent_store
from app import cancel as cancel_mod
from app.config import (AGENT_PORT, WEB_DIR, COMFYUI_URL, MODEL, DOCUMENTS_DIR,
                        LLM_PROVIDER, PROVIDERS, OLLAMA_BASE_URL, DEFAULT_AGENT_ID,
                        ADMIN_ALLOW_REMOTE, CONTEXT_BUDGET,
                        VISION_PROVIDER, VISION_MODEL, provider_vision)
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
            for event in run_agent_stream(turn_input, h, provider=provider,
                                          model=model, pre_tool_results=pre_results,
                                          agent_id=agent_id, image=image,
                                          cancel_event=cancel_event):
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
        "vision_provider": VISION_PROVIDER,
        "vision_model": (VISION_MODEL
                         or PROVIDERS.get(VISION_PROVIDER, {}).get("model", "")),
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
    img_muted = set(settings.get("image_gen_muted") or [])
    for item in items:
        if item["kind"] == "group":
            item["interject"] = item["target_id"] not in muted
            item["image_gen"] = item["target_id"] not in img_muted
    return jsonify({"sessions": items, "names_ok": names_ok, "agent": aid,
                    "image_gen_on": settings.get("image_gen") is not False})


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
