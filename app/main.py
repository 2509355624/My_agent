"""
Flask Web 服务入口
"""

import requests
from flask import Flask, request, jsonify, send_from_directory, Response
from app.config import AGENT_PORT, WEB_DIR, COMFYUI_URL, MODEL
from app.skills import build_system_prompt, list_skills, load_skill
from app.memory import load_history, save_history
from app.agent import run_agent_stream

app = Flask(__name__, static_folder=WEB_DIR, static_url_path="")

# ─── 全局状态 ────────────────────────────────────────

history = load_history()
SYSTEM_PROMPT = build_system_prompt()

# 始终使用最新的 system prompt
if history and history[0].get("role") == "system":
    history[0] = {"role": "system", "content": SYSTEM_PROMPT}
else:
    history.insert(0, {"role": "system", "content": SYSTEM_PROMPT})


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

    events = []
    for event in run_agent_stream(user_input, history):
        events.append(event)

    save_history(history)
    return jsonify({"events": events})


@app.route("/api/clear", methods=["POST"])
def clear():
    global history
    history = [{"role": "system", "content": SYSTEM_PROMPT}]
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
