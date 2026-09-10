"""
会话持久化 + 上下文管理
"""

import json
import os
from app.config import SESSION_FILE, MAX_TURNS


# ─── 持久化 ──────────────────────────────────────────

def save_history(history):
    """保存会话到 JSONL 文件"""
    os.makedirs(os.path.dirname(SESSION_FILE), exist_ok=True)
    with open(SESSION_FILE, "w", encoding="utf-8") as f:
        for msg in history:
            f.write(json.dumps(msg, ensure_ascii=False) + "\n")


def load_history():
    """从 JSONL 文件加载会话"""
    if not os.path.exists(SESSION_FILE):
        return []
    history = []
    with open(SESSION_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                history.append(json.loads(line))
    return history


# ─── 上下文裁剪 ──────────────────────────────────────

def trim_history(history):
    """裁剪历史，保留 system prompt + 最近 MAX_TURNS 轮"""
    system_msgs = [m for m in history if m.get("role") == "system"]
    other_msgs = [m for m in history if m.get("role") != "system"]

    turn_count = 0
    cutoff_idx = len(other_msgs)

    for i in range(len(other_msgs) - 1, -1, -1):
        if other_msgs[i]["role"] == "user":
            turn_count += 1
            if turn_count > MAX_TURNS:
                cutoff_idx = i + 1
                break

    if turn_count <= MAX_TURNS:
        return history

    return system_msgs + other_msgs[cutoff_idx:]
