"""
Flask Web 服务入口
"""

import requests
from flask import Flask, request, jsonify, send_from_directory, Response
from app.config import AGENT_PORT, WEB_DIR, COMFYUI_URL, MODEL, DOCUMENTS_DIR
from app.skills import list_skills, load_skill
from app.agent_prompt import build_system_prompt
from app.memory import load_history, save_history
from app.agent import run_agent_stream
from app.tools.documents import list_documents, file_info, read_document, search_document

app = Flask(__name__, static_folder=WEB_DIR, static_url_path="")

# ─── 全局状态 ────────────────────────────────────────

history = load_history()


def _refresh_system_prompt():
    """用最新的状态栏刷新 system prompt（每次对话前调用）"""
    last_tool = "none"
    # 从历史中找最后一个工具调用
    for msg in reversed(history):
        if msg.get("role") == "tool_result":
            last_tool = msg.get("tool_name", "none")
            break

    msg_count = len([m for m in history if m.get("role") != "system"])
    prompt = build_system_prompt(message_count=msg_count, last_tool=last_tool)

    if history and history[0].get("role") == "system":
        history[0] = {"role": "system", "content": prompt}
    else:
        history.insert(0, {"role": "system", "content": prompt})


# 初始化时先刷新一次
_refresh_system_prompt()


# ─── 页面路由 ────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory(WEB_DIR, "index.html")


# ─── API 路由 ────────────────────────────────────────

@app.route("/api/history")
def get_history():
    msgs = [m for m in history if m.get("role") != "system"]
    return jsonify({"messages": msgs, "model": MODEL, "count": len(msgs)})


@app.route("/api/chat", methods=["POST"])
def chat():
    global history
    data = request.get_json()
    user_input = data.get("message", "").strip()

    if not user_input:
        return jsonify({"error": "消息不能为空"}), 400

    # 每次对话前刷新 system prompt（更新状态栏）
    _refresh_system_prompt()

    events = []
    for event in run_agent_stream(user_input, history):
        events.append(event)

    save_history(history)
    return jsonify({"events": events})


@app.route("/api/clear", methods=["POST"])
def clear():
    global history
    history = []
    _refresh_system_prompt()
    save_history(history)
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


@app.route("/api/documents/upload", methods=["POST"])
def api_upload_document():
    """上传文档到 documents 目录"""
    import os
    from werkzeug.utils import secure_filename

    os.makedirs(DOCUMENTS_DIR, exist_ok=True)

    if "file" not in request.files:
        return jsonify({"error": "没有文件"}), 400

    f = request.files["file"]
    if not f.filename:
        return jsonify({"error": "文件名为空"}), 400

    filename = secure_filename(f.filename)
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

def run():
    print("=" * 50)
    print("  My Agent - Personal AI Assistant")
    print("  Model: " + MODEL)
    print("  URL: http://localhost:" + str(AGENT_PORT))
    print("  Session: " + str(len(history)) + " messages")
    print("  Skills: " + ", ".join(list_skills()))
    print("=" * 50)
    app.run(host="127.0.0.1", port=AGENT_PORT, debug=False)


if __name__ == "__main__":
    run()
