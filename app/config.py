"""
配置加载
从 .env 文件读取环境变量，提供全局配置项
"""

import os
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT_DIR = BASE_DIR

_env_path = os.path.join(BASE_DIR, ".env")
if os.path.exists(_env_path):
    load_dotenv(_env_path)

# ─── LLM Provider ────────────────────────────────────

LLM_PROVIDER = os.getenv("LLM_PROVIDER", "volc").lower()

# Volcengine (Doubao)
VOLC_API_KEY = os.getenv("VOLC_API_KEY", "")
VOLC_BASE_URL = os.getenv("VOLC_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3")
VOLC_CHAT_MODEL = os.getenv("VOLC_CHAT_MODEL", "deepseek-v4-flash-ga-260731")

# DeepSeek Official
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")

# 动态 API 地址和模型名（根据 provider 切换）
if LLM_PROVIDER == "deepseek":
    API_URL = DEEPSEEK_BASE_URL.rstrip("/") + "/chat/completions"
    API_KEY = DEEPSEEK_API_KEY
    MODEL = DEEPSEEK_MODEL
else:
    API_URL = VOLC_BASE_URL.rstrip("/") + "/chat/completions"
    API_KEY = VOLC_API_KEY
    MODEL = VOLC_CHAT_MODEL

# ─── 多 Provider 配置表（web 端模型切换用）───────────

# Ollama 本地
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:7b")

# provider 元信息：每种提供商的默认 base_url / model / 是否需要 api_key
PROVIDERS = {
    # 火山引擎（deepseek 开源模型托管）
    "volc": {
        "label": "火山引擎",
        "base_url": VOLC_BASE_URL,
        "model": VOLC_CHAT_MODEL,
        "api_key": VOLC_API_KEY,
        "needs_key": False,   # 用 .env 里配置的 key
    },
    # 豆包（Doubao 自家模型）
    "doubao": {
        "label": "豆包",
        "base_url": VOLC_BASE_URL,   # 火山方舟兼容 OpenAI 接口
        "model": os.getenv("DOUBAO_MODEL", "doubao-1-5-thinking-pro-250615"),
        "api_key": VOLC_API_KEY,
        "needs_key": False,
    },
    # DeepSeek 官方
    "deepseek": {
        "label": "DeepSeek 官方",
        "base_url": DEEPSEEK_BASE_URL,
        "model": DEEPSEEK_MODEL,
        "api_key": DEEPSEEK_API_KEY,
        "needs_key": False,
    },
    # Ollama（本地免费）
    "ollama": {
        "label": "Ollama 本地",
        "base_url": OLLAMA_BASE_URL,
        "model": OLLAMA_MODEL,
        "api_key": "",
        "needs_key": False,
    },
}

# ─── ComfyUI ────────────────────────────────────────
# 没装 ComfyUI 的机器设为 false：不再注册 generate_image 工具，
# 避免 LLM 白白尝试调用一个必然失败的工具
ENABLE_IMAGE_GEN = os.getenv("ENABLE_IMAGE_GEN", "true").lower() not in ("0", "false", "no")

COMFYUI_URL = os.getenv("COMFYUI_URL", "http://127.0.0.1:8188")

# ─── Agent ──────────────────────────────────────────

AGENT_PORT = int(os.getenv("AGENT_PORT", "5174"))
MAX_TURNS = int(os.getenv("MAX_TURNS", "10"))

# ─── 路径 ───────────────────────────────────────────

SKILLS_DIR = os.path.join(BASE_DIR, "skills")
WEB_DIR = os.path.join(BASE_DIR, "web")
DATA_DIR = os.path.join(BASE_DIR, "data")
DOCUMENTS_DIR = os.path.join(BASE_DIR, "documents")
VECTOR_STORE_DIR = os.path.join(BASE_DIR, "vector_store", "chroma")

SESSION_FILE = os.path.join(DATA_DIR, "session.jsonl")
