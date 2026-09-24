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

# ─── Web 搜索（豆包搜索）─────────────────────────────
# 豆包搜索（原 联网搜索/融合信息搜索）专用 API Key，在火山「联网搜索控制台」创建：
# https://console.volcengine.com/search-infinity/api-key?tab=post_paid
# 注意：与方舟 ARK 的 VOLC_API_KEY 不同，需单独开通。未配置时 web_search 自动回退 DuckDuckGo。
SEARCH_API_KEY = os.getenv("SEARCH_API_KEY", "")
DOUBAO_SEARCH_ENDPOINT = os.getenv("DOUBAO_SEARCH_ENDPOINT",
                                   "https://open.feedcoopapi.com/search_api/web_search")

# ─── ComfyUI ────────────────────────────────────────
# 没装 ComfyUI 的机器设为 false：不再注册 generate_image 工具，
# 避免 LLM 白白尝试调用一个必然失败的工具
ENABLE_IMAGE_GEN = os.getenv("ENABLE_IMAGE_GEN", "true").lower() not in ("0", "false", "no")

COMFYUI_URL = os.getenv("COMFYUI_URL", "http://127.0.0.1:8188")

# 单次 generate_image 调用的等待上限（秒）。批量生图是逐张串行跑的，
# 张数越多总耗时越长，所以这里给足时间，避免整批在最后一张前被掐断
# （超时会丢掉本批已经生成出来的图）。
IMAGE_GEN_TIMEOUT = int(os.getenv("IMAGE_GEN_TIMEOUT", "3600"))

# ─── Agent ──────────────────────────────────────────

AGENT_PORT = int(os.getenv("AGENT_PORT", "5174"))
MAX_TURNS = int(os.getenv("MAX_TURNS", "10"))

# ─── 路径 ───────────────────────────────────────────

SKILLS_DIR = os.path.join(BASE_DIR, "skills")
WEB_DIR = os.path.join(BASE_DIR, "web")
DATA_DIR = os.path.join(BASE_DIR, "data")
DOCUMENTS_DIR = os.path.join(BASE_DIR, "documents")
VECTOR_STORE_DIR = os.path.join(BASE_DIR, "vector_store", "chroma")

# 多 agent：每个 agent 一个自包含目录（agents/<id>/ 里放 agent.json 配置、
# prompt.md 人设、session.jsonl 会话）。会话文件路径由 app/agents.py 解析，
# 不再有「全局单会话文件」这种东西。
AGENTS_DIR = os.path.join(BASE_DIR, "agents")
DEFAULT_AGENT_ID = os.getenv("DEFAULT_AGENT_ID", "main")

# ─── QQ 接入（NapCat / OneBot 11）─────────────────────
# 适配层是独立进程（app/qq_bot.py），本段配置只在它里面用到；网页端进程
# 仅用到 QQ_ENABLE（决定是否注册 send_qq_message 工具）。
def _env_bool(name, default=False):
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() not in ("0", "false", "no", "")


def _env_list(name):
    """逗号分隔的字符串列表，去空项。"""
    raw = os.getenv(name, "")
    return [x.strip() for x in raw.split(",") if x.strip()]


QQ_ENABLE = _env_bool("QQ_ENABLE", False)

# NapCat 协议端地址：WS 收事件，HTTP 发消息（WebUI 里要分别启用这两个）
QQ_WS_URL = os.getenv("QQ_WS_URL", "ws://127.0.0.1:3001")
QQ_HTTP_URL = os.getenv("QQ_HTTP_URL", "http://127.0.0.1:3000").rstrip("/")
# OneBot 访问令牌（NapCat 网络配置里设的那个）。空表示不鉴权，仅限本机使用
QQ_TOKEN = os.getenv("QQ_TOKEN", "")

# 用哪个 agent 的人设与工具白名单
QQ_AGENT_ID = os.getenv("QQ_AGENT_ID", "qq")

# 触发规则
QQ_PRIVATE_ENABLE = _env_bool("QQ_PRIVATE_ENABLE", True)   # 是否响应私聊
QQ_GROUP_AT_ONLY = _env_bool("QQ_GROUP_AT_ONLY", True)     # 群里必须 @ 才回
QQ_GROUP_KEYWORDS = _env_list("QQ_GROUP_KEYWORDS")         # 群里命中即回（免 @）

# 名单：留空表示不限。黑名单优先于白名单
QQ_WHITELIST_GROUPS = _env_list("QQ_WHITELIST_GROUPS")
QQ_WHITELIST_USERS = _env_list("QQ_WHITELIST_USERS")
QQ_BLACKLIST_USERS = _env_list("QQ_BLACKLIST_USERS")

# 并发：同一条会话线永远串行，这里是跨会话的并行上限。
# agent 一轮可能跑几十秒（生图更久），开太大没什么收益，还容易顶满 LLM 限流
QQ_MAX_CONCURRENCY = int(os.getenv("QQ_MAX_CONCURRENCY", "2"))

# 单条 QQ 消息的字数上限，超出按段落切分成多条发送
QQ_REPLY_MAX_CHARS = int(os.getenv("QQ_REPLY_MAX_CHARS", "700"))

# 每轮结束后的静默窗口，把这段时间内到达的同一会话消息合并成一次处理
# （群里连发几句时，避免逐句各跑一轮 LLM）
QQ_DEBOUNCE_SECONDS = float(os.getenv("QQ_DEBOUNCE_SECONDS", "1.5"))
