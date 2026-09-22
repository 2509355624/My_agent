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
from app.config import (AGENT_PORT, WEB_DIR, COMFYUI_URL, MODEL, DOCUMENTS_DIR,
                        LLM_PROVIDER, PROVIDERS, OLLAMA_BASE_URL, DEFAULT_AGENT_ID)
from app.skills import list_skills, load_skill
from app.agent_prompt import build_stable_prompt
from app.memory import load_history, save_history
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
    """列出 Ollama 本地已安装的模型（代理 /api/tags）"""
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
    SESSION_EVENTS = ("user", "assistant", "tool_result")

    # 带图时：图片本体不进历史——一张图 base64 几十万字符，存进会话文件会让它
    # 迅速膨胀，而且刷新回放时也还原不出图片。历史里只留一句与 /api/vision
    # 同口径的占位文本，图片本身由 run_agent_stream 在第 1 轮以多模态形式下发。
    if image:
        turn_input = ("（用户上传了一张图片：" + clean_input + "）"
                      if clean_input else "（用户上传了一张图片）")
    else:
        turn_input = clean_input

    def generate():
        try:
            for event in run_agent_stream(turn_input, h, provider=provider,
                                          model=model, pre_tool_results=pre_results,
                                          agent_id=agent_id, image=image):
                # 先落盘再推送：内容一旦可见于前端，磁盘上就已经有了
                if event.get("type") in SESSION_EVENTS:
                    try:
                        save_history(h, agent_id)
                    except Exception:
                        # 落盘失败不该打断这一轮对话，finally 还会再试一次
                        pass
                yield json.dumps(event, ensure_ascii=False) + "\n"
        finally:
            # 兜底：无论正常结束还是客户端中断，都落盘已产生的会话
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
