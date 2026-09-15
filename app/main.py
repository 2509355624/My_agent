"""
Flask Web 服务入口
"""

import re
import json
import socket
import requests
from flask import (Flask, request, jsonify, send_from_directory, Response,
                   stream_with_context)
from app.config import (AGENT_PORT, WEB_DIR, COMFYUI_URL, MODEL, DOCUMENTS_DIR,
                        LLM_PROVIDER, PROVIDERS, OLLAMA_BASE_URL)
from app.skills import list_skills, load_skill
from app.agent_prompt import build_stable_prompt
from app.memory import load_history, save_history
from app.agent import run_agent_stream, parse_tool_calls, _strip_tool_blocks
from app.tools import execute_tool
from app.tools.normal.documents import list_documents, file_info, read_document, search_document

app = Flask(__name__, static_folder=WEB_DIR, static_url_path="")

# ─── 会话持久化（以 SESSION_FILE 为单一事实源）────────
# 一切以磁盘文件为准：每次读写都 load/save，保证手机/电脑等多端
# 刷新到的一致（不再依赖进程内存全局变量，避免多端不同步）。


def _ensure_system_prompt(only_system=False):
    """把 SESSION_FILE 的会话首条固定为稳定的 system 消息（prefix cache 锚点）。
    状态栏不在 messages[1]，而是由 agent.run_agent_stream 请求时动态追加在
    消息数组末尾，只牺牲它自己那几十个 token，避免毒化前缀缓存。

    - only_system=True：清空所有非 system 消息（用于“清空会话”）
    - only_system=False：仅补齐 system 头部，保留已有历史
    """
    stable_prompt = build_stable_prompt()
    keep = [] if only_system else [m for m in load_history() if m.get("role") != "system"]
    save_history([{"role": "system", "content": stable_prompt}] + keep)


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
    h = load_history()
    msgs = [m for m in h if m.get("role") != "system"]
    return jsonify({"messages": msgs, "model": MODEL, "count": len(msgs),
                    "provider": LLM_PROVIDER})


@app.route("/api/chat", methods=["POST"])
def chat():
    data = request.get_json()
    user_input = data.get("message", "").strip()
    provider = data.get("provider")
    model = data.get("model")

    if not user_input:
        return jsonify({"error": "消息不能为空"}), 400

    # 支持用户直接在输入框粘贴/输入 [[TOOL:name]]{...} 触发工具：
    # 解析并执行用户消息里的工具块，把结果注入历史后，再进入 LLM 循环，
    # 这样 LLM 一开始就能看到已执行的工具结果。
    user_tools = parse_tool_calls(user_input)
    clean_input = _strip_tool_blocks(user_input).strip() or user_input
    pre_results = []
    if user_tools:
        for tc in user_tools:
            try:
                result = execute_tool(tc["name"], tc["args"])
            except Exception as e:
                result = "工具执行失败: " + str(e)
            pre_results.append({"name": tc["name"], "result": result})

    # 每次从文件读取最新会话，保证多端一致；写完立即落盘
    h = load_history()
    if not h or h[0].get("role") != "system":
        _ensure_system_prompt(only_system=True)
        h = load_history()

    # 流式返回：agent 循环每产生一个事件就立刻推给前端（NDJSON，一行一个 JSON）。
    # 之前是收集完所有事件再一次性 jsonify，导致本地模型跑 60-70 秒期间前端全黑箱。
    def generate():
        try:
            for event in run_agent_stream(clean_input, h, provider=provider,
                                          model=model, pre_tool_results=pre_results):
                yield json.dumps(event, ensure_ascii=False) + "\n"
        finally:
            # 无论正常结束还是客户端中断，都落盘已产生的会话
            save_history(h)

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
    if not image:
        return jsonify({"error": "缺少图片"}), 400
    if not question:
        question = "帮我看看这张图片，描述它，并给出可以优化/改进的地方。"

    from app.agent_prompt import build_stable_prompt
    messages = [
        {"role": "system", "content": build_stable_prompt()},
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
    h = load_history()
    if not h or h[0].get("role") != "system":
        _ensure_system_prompt(only_system=True)
        h = load_history()
    h.append({"role": "user", "content": "（用户上传了一张图片" + (("：" + question) if question else "") + "）"})
    h.append({"role": "assistant", "content": reply})
    save_history(h)
    return jsonify({"reply": reply})


@app.route("/api/clear", methods=["POST"])
def clear():
    # 清空全部非 system 消息（保留稳定 system 前缀）
    _ensure_system_prompt(only_system=True)
    return jsonify({"ok": True})


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
    # 启动时确保会话文件以稳定 system 头开始（prefix cache 锚点）
    _ensure_system_prompt()

    host = "0.0.0.0"  # 监听所有网卡，允许手机/iPad 局域网访问
    ip = _local_ip()
    print("=" * 50)
    print("  My Agent - Personal AI Assistant")
    print("  Model: " + MODEL)
    print("  本机: http://localhost:" + str(AGENT_PORT))
    print("  局域网: http://" + ip + ":" + str(AGENT_PORT))
    print("  Session: " + str(len(load_history())) + " messages")
    print("  Skills: " + ", ".join(list_skills()))
    print("=" * 50)
    app.run(host=host, port=AGENT_PORT, debug=False)


if __name__ == "__main__":
    run()
