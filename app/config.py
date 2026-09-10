"""
配置加载
从 .env 文件读取环境变量，提供全局配置项
"""

import os
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_env_path = os.path.join(BASE_DIR, ".env")
if os.path.exists(_env_path):
    load_dotenv(_env_path)

# ─── LLM ────────────────────────────────────────────

API_URL = os.getenv("VOLC_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3") + "/chat/completions"
API_KEY = os.getenv("VOLC_API_KEY", "")
MODEL = os.getenv("VOLC_CHAT_MODEL", "deepseek-v4-flash-ga-260731")

# ─── ComfyUI ────────────────────────────────────────

COMFYUI_URL = os.getenv("COMFYUI_URL", "http://127.0.0.1:8188")

# ─── Agent ──────────────────────────────────────────

AGENT_PORT = int(os.getenv("AGENT_PORT", "5174"))
MAX_TURNS = int(os.getenv("MAX_TURNS", "10"))

# ─── 路径 ───────────────────────────────────────────

SKILLS_DIR = os.path.join(BASE_DIR, "skills")
WEB_DIR = os.path.join(BASE_DIR, "web")
DATA_DIR = os.path.join(BASE_DIR, "data")

SESSION_FILE = os.path.join(DATA_DIR, "session.jsonl")
