"""
Flask Web 服务入口
"""

import requests
from flask import Flask, request, jsonify, send_from_directory, Response
from app.config import AGENT_PORT, WEB_DIR, COMFYUI_URL, MODEL, DOCUMENTS_DIR
from app.skills import list_skills, load_skill
from app.agent_prompt import build_stable_prompt, build_status_bar
from app.memory import load_history, save_history
from app.agent import run_agent_stream
from app.tools.documents import list_documents, file_info, read_document, search_document

app = Flask(__name__, static_folder=WEB_DIR, static_url_path="")

# ─── 全局状态 ────────────────────────────────────────

history = load_history()


def _ensure_system_prompt():
    """确保 history 开头有两条 system 消息：
    [0] 稳定层（内容固定，prefix cache 锚点）
    [1] 动态层（状态栏，每轮更新，放在后面不影响前缀缓存）
    """
    stable_prompt = build_stable_prompt()
    msg_count = len([m for m in history if m.get("role") != "system"])
    status_bar = build_status_bar(message_count=msg_count, last_tool="none")

    # 移除现有的 system 消息
    non_system = [m for m in history if m.get("role") != "system"]

    history.clear()
    history.append({"role": "system", "content": stable_prompt})
    history.append({"role": "system", "content": status_bar})
    history.extend(non_system)


def _refresh_status_bar():
    """只更新状态栏（第二条 system 消息），稳定层保持不变。
    这样第一条 system 消息永远相同，prefix cache 命中率最高。"""
    last_tool = "none"
    for msg in reversed(history):
        if msg.get("role") == "tool_result":
            last_tool = msg.get("tool_name", "none")
            break

    msg_count = len([m for m in history if m.get("role") != "system"])
    status_bar = build_status_bar(message_count=msg_count, last_tool=last_tool)

    # 找到第二条 system 消息（状态栏）
    system_count = 0
    for i, msg in enumerate(history):
        if msg.get("role") == "system":
            system_count += 1
            if system_count == 2:
                history[i] = {"role": "system", "content": status_bar}
                return

    # 没有第二条，重建 system 消息
    _ensure_system_prompt()


# 初始化时设置 system prompt
_ensure_system_prompt()


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

    # 每次对话前刷新状态栏（第二条 system 消息），稳定层保持不变
    _refresh_status_bar()

    events = []
    for event in run_agent_stream(user_input, history):
        events.append(event)

    save_history(history)
    return jsonify({"events": events})


@app.route("/api/clear", methods=["POST"])
def clear():
    global history
    history = []
    _ensure_system_prompt()
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
