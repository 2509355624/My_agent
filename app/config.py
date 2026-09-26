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

# SCNet 超算互联网（Token Plan 套餐）。注意：控制台页面上写的「专用地址
# /api/aim/v1」是错的（实测恒 404），真正能用的是通用 OpenAI 兼容端点
# /api/llm/v1。套餐只支持 DeepSeek-V4.1-Flash-Event 一个模型，别的不认
# （403 "The current model does not support Token Plan"）。
SCNET_API_KEY = os.getenv("SCNET_API_KEY", "")
SCNET_BASE_URL = os.getenv("SCNET_BASE_URL", "https://api.scnet.cn/api/llm/v1")
SCNET_MODEL = os.getenv("SCNET_MODEL", "DeepSeek-V4.1-Flash-Event")

# SCNet 第二个 Token Plan（另一把 key、另一个 1000 万 credits 账本）。
# 套餐模型是 DeepSeek-V4.1-Flash（不带 Event 后缀）；这个 key 的 /models
# 会列出全平台模型，但免费额度只覆盖套餐模型，其他模型要真金白银。
SCNET2_API_KEY = os.getenv("SCNET2_API_KEY", "")
SCNET2_MODEL = os.getenv("SCNET2_MODEL", "DeepSeek-V4.1-Flash")

# ─── 主备模型降级链 ──────────────────────────────────
# 一次请求失败（额度不足 / 超时 / 5xx / 模型退役）时依次往下试的顺序。
# 格式 "provider:model,provider:model"，第一项是主模型；留空 = 关闭降级。
# 调用方指定了 provider/model（管理页、agent 配置）时，那一项会排在链头，
# 链里重复的项自动去掉——所以管理页手动切换依然优先。
LLM_FALLBACK_CHAIN = os.getenv(
    "LLM_FALLBACK_CHAIN",
    "volc:deepseek-v4-flash-ga-260731,"
    "volc:deepseek-v4-pro-ga-260813,"
    "scnet2:DeepSeek-V4.1-Flash")

# 单次尝试的超时。流式下这是「两块数据之间的最大间隔」而不是总时长——
# 60 秒一个字节都没回就认为卡死，换下一个候选。原先默认 600 秒等于不切。
LLM_REQUEST_TIMEOUT = float(os.getenv("LLM_REQUEST_TIMEOUT", "60"))

# 某个候选失败后「拉黑」多久不再试。到期自动回头重试主模型，所以额度
# 充值、服务恢复之后能自愈，不用重启进程。
LLM_FALLBACK_TTL = float(os.getenv("LLM_FALLBACK_TTL", "600"))

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
# vision 表示「这个 provider 的默认模型能不能读图」。它决定带图请求走哪条路：
# 有视觉的直接多模态下发；没有的先过一道识图（见 app/vision.py）转成文字。
#
# 为什么必须显式标注、不能只靠模型名猜：火山那条线的 v4flash 与 v4-pro 都是
# 纯文本模型，把 base64 图塞进去不会报错，而是**整条请求挂死**（实测
# ReadTimeout 卡满 180 秒）——模型把 base64 当普通文本读，token 涨到几万，
# 服务端一直不返回。这是"必须预处理"而不是"预处理效果更好"。
PROVIDERS = {
    # 火山引擎（deepseek 开源模型托管）
    "volc": {
        "label": "火山引擎",
        "base_url": VOLC_BASE_URL,
        "model": VOLC_CHAT_MODEL,
        "api_key": VOLC_API_KEY,
        "needs_key": False,   # 用 .env 里配置的 key
        "vision": False,
    },
    # 豆包（Doubao 自家模型）
    "doubao": {
        "label": "豆包",
        "base_url": VOLC_BASE_URL,   # 火山方舟兼容 OpenAI 接口
        "model": os.getenv("DOUBAO_MODEL", "doubao-1-5-thinking-pro-250615"),
        "api_key": VOLC_API_KEY,
        "needs_key": False,
        "vision": False,
    },
    # DeepSeek 官方
    "deepseek": {
        "label": "DeepSeek 官方",
        "base_url": DEEPSEEK_BASE_URL,
        "model": DEEPSEEK_MODEL,
        "api_key": DEEPSEEK_API_KEY,
        "needs_key": False,
        "vision": True,
    },
    # Ollama 本地（能不能读图取决于拉的是哪个模型，默认按不能算）
    "ollama": {
        "label": "Ollama 本地",
        "base_url": OLLAMA_BASE_URL,
        "model": OLLAMA_MODEL,
        "api_key": "",
        "needs_key": False,
        "vision": False,
    },
    # SCNet 超算互联网（国家超算 Token Plan，免费 credits 计费）。模型是
    # DeepSeek-V4.1-Flash（带深度思考），套餐页面只承诺文本生成/深度思考，
    # 没提图像理解 → vision 按 False 走识图预处理，稳妥。
    "scnet": {
        "label": "SCNet 超算",
        "base_url": SCNET_BASE_URL,
        "model": SCNET_MODEL,
        "api_key": SCNET_API_KEY,
        "needs_key": False,
        "vision": False,
    },
    # SCNet 第二把 key（1000 万 credits 账本独立）。默认用 scnet（2000 万
    # 那本），这把留作备胎/分流——管理页可切。
    "scnet2": {
        "label": "SCNet 超算 2",
        "base_url": SCNET_BASE_URL,
        "model": SCNET2_MODEL,
        "api_key": SCNET2_API_KEY,
        "needs_key": False,
        "vision": False,
    },
}

# 模型名里出现这些词就认为它有视觉能力，用来覆盖上面的 provider 开关。
# 场景：豆包换成 doubao-1-5-vision、本地换 qwen-vl-max，都不用改代码。
_VISION_MODEL_HINTS = ("vl", "vision", "omni")


def provider_vision(provider=None, model=None):
    """判断这个 (provider, model) 组合能不能直接读图。

    两级判断：模型名命中关键字 → 有视觉（覆盖 provider 开关）；否则看
    provider 自己的 vision 开关。这样默认行为由 PROVIDERS 管住，个别型号
    又不用回来改代码。
    """
    pid = (provider or LLM_PROVIDER or "").lower()
    cfg = PROVIDERS.get(pid) or PROVIDERS.get(LLM_PROVIDER, {})
    name = (model if model is not None else cfg.get("model", "")) or ""
    if any(h in name.lower() for h in _VISION_MODEL_HINTS):
        return True
    return bool(cfg.get("vision"))


# ─── 图片识别（视觉预处理）───────────────────────────
# 给没有视觉能力的模型用的：先把图交给这里识别成文字，再让 agent 接着跑。
# 固定走 DeepSeek 官方——它是目前唯一稳定可用的视觉来源，且单次调用很便宜
# （实测一张图 246 + 340 token，比跑一轮对话还低）。
VISION_PROVIDER = os.getenv("VISION_PROVIDER", "deepseek")
# 空 = 用该 provider 的默认模型
VISION_MODEL = os.getenv("VISION_MODEL", "")
# 识图是单次同步调用，实测 1.9 秒返回，给 30 秒余量足够；超时按失败降级
VISION_TIMEOUT = float(os.getenv("VISION_TIMEOUT", "30"))
# 发出去前把图缩到最长边这么多像素。QQ 群里的图有 8MB 级的，直接 base64
# 是 11MB 字符串——既慢又贵，还可能撞服务端请求体上限
VISION_MAX_EDGE = int(os.getenv("VISION_MAX_EDGE", "1024"))

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

# 单张图的出图上限（秒）。**注意这是「一张」的时限，不是「一次调用」的**：
# 生图在 image_jobs 里是逐张串行排队的，计时从每张**真正开跑**算起（排队时
# 间不算，否则排在第 5 位的人还没轮到就被判超时了）。
#
# 但「开跑」= 任务提交进 ComfyUI 那一刻，**模型加载也算在里面**：模型不在显存
# 里时要现加载（冷启动约 20~30s，换渠道重载更久，qwen 的 Q4_K 还要反量化）。
# 所以时限要留出这段余量，否则每次冷启动的第一张都必被掐。
#
# 到点还没出图就把这张中断掉、顺手清掉 ComfyUI 里的残留任务和显存，让后面排
# 队的人先跑——一张卡住的图不该堵着所有人。
IMAGE_GEN_TIMEOUT = int(os.getenv("IMAGE_GEN_TIMEOUT", "180"))

# ─── Agent ──────────────────────────────────────────

AGENT_PORT = int(os.getenv("AGENT_PORT", "5174"))
MAX_TURNS = int(os.getenv("MAX_TURNS", "10"))

# 上下文预算：**单次请求允许携带的 prompt token 上限**。
# 注意它跟模型的物理上下文上限（火山/DeepSeek 是 128K~1M）是两件事——
# 这里管的是「我愿意为每一轮付多少钱」，不是「模型装不装得下」。超了就
# 压缩历史（见 app/memory.py）。
# 折算参考：一个中文字符约 0.6 token，32000 约合 5.3 万汉字、50~60 轮对话。
# 单个 agent 可在 agent.json 里用 context_budget 覆盖（0 = 用这里的全局值）。
CONTEXT_BUDGET = int(os.getenv("CONTEXT_BUDGET", "32000"))

# 会话历史窗口：**保留最近多少轮完整对话**，更早的滚出窗口交给长期记忆。
#
# 判据刻意用「轮数」而不是 token：轮数是本地算的，不受 API usage 影响。
# usage 走的是 threading.local，而 QQ 适配层用 asyncio.to_thread 从线程池取
# 线程，同一会话的不同消息落在不同的线程上，读到的 total_tokens 常常是 0 ——
# 这就是「CONTEXT_BUDGET=32000 却从来没压缩过」的根因（实测过一个群攒到
# 8.2 万 token 全量重发）。按轮数裁不需要读 usage，根因直接消失。
#
# 折算参考：群里一轮平均 3.2 条记录（user + assistant + 夹着的 tool_result）
# ≈ 137 token，实测 20 轮 = 64 条 = 2751 token。0 = 关掉窗口裁剪。
CONTEXT_MAX_TURNS = int(os.getenv("CONTEXT_MAX_TURNS", "20"))

# 滚出窗口的那批轮次要摘成的摘要字数上限。比群聊归档摘要（300 字）更短：
# 细节由窗口里的原文兜着，摘要只需要留住「常聊的群友 + 他的偏好」。
MEMORY_TURN_DIGEST_CHARS = int(os.getenv("MEMORY_TURN_DIGEST_CHARS", "150"))

# 搜索结果截断上限（字符）。联网搜索单次返回动辄三四千字，而它会作为
# tool_result **永久留在历史里**——不截断的话，用过一次之后每一轮请求都要
# 重发这几千 token。截断只影响「查资料的详尽程度」，不影响聊天。
WEB_SEARCH_MAX_CHARS = int(os.getenv("WEB_SEARCH_MAX_CHARS", "800"))

# 后台管理接口是否允许非本机访问。默认只允许回环地址——服务监听 0.0.0.0
# 且没有任何鉴权，一个能改 agent 配置的口子不该顺带暴露到整个局域网。
# 需要从别的设备打开管理页时，在 .env 里设成 true。
ADMIN_ALLOW_REMOTE = os.getenv("ADMIN_ALLOW_REMOTE", "false").lower() not in ("0", "false", "no", "")

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

# 一条消息里最多处理几张图（超出的只记一句"还有 N 张没看"，不下载）。
# 群里刷图时，每张都要先下载再送一次识图，没有上限就是开口子的花销
QQ_IMAGE_MAX_COUNT = int(os.getenv("QQ_IMAGE_MAX_COUNT", "3"))
# 图片下载超时（秒）。实测腾讯的图床很快，但断连时会一直挂
QQ_IMAGE_TIMEOUT = float(os.getenv("QQ_IMAGE_TIMEOUT", "20"))
# 下载体积上限（字节）。超过就不下——8MB 的图压完也未必划算，直接说明看不到
QQ_IMAGE_MAX_BYTES = int(os.getenv("QQ_IMAGE_MAX_BYTES", str(16 * 1024 * 1024)))

# 被引用的消息带进模型的字数上限。引用是「这一轮问题的上下文」，但它可能是
# 机器人自己的一条长回复，或一张转发了几十条的聊天记录卡——不封顶就会把
# 预算吃光。超出只截断并附一句说明：引用内容只是补充，不该让整轮失败。
QQ_QUOTE_MAX_CHARS = int(os.getenv("QQ_QUOTE_MAX_CHARS", "3000"))

# 每轮结束后的静默窗口，把这段时间内到达的同一会话消息合并成一次处理
# （群里连发几句时，避免逐句各跑一轮 LLM）
QQ_DEBOUNCE_SECONDS = float(os.getenv("QQ_DEBOUNCE_SECONDS", "1.5"))

# 排队合并的上限。静默窗口只是「等连发到齐」，本身不限制攒多少——群里被
# 刷屏时 _pending 会一直涨，合并出来的那一条会长到离谱，一次全灌进模型。
# 所以合并时设两道闸：条数上限（只取最近的 N 条）与字数上限（从最新往前
# 累计，装不下就丢掉更旧的）。两个都 <=0 表示不限制（不建议）。
QQ_PENDING_MAX_ITEMS = int(os.getenv("QQ_PENDING_MAX_ITEMS", "20"))
QQ_PENDING_MAX_CHARS = int(os.getenv("QQ_PENDING_MAX_CHARS", "2000"))

# 群聊背景：被 @ 时顺带把群里最近这几条消息送去，让它知道刚才在聊什么。
# 原先收到群消息、不 @ 就整个丢掉，模型每轮只看得到「有人问了它一句」，
# 所以只能一问一答、像个问答助手。0 表示不带（退回旧行为）。
QQ_CONTEXT_MESSAGES = int(os.getenv("QQ_CONTEXT_MESSAGES", "30"))
# 背景的字数上限，从最新往前累计。群聊刷屏时不封顶会吃光单轮预算。
QQ_CONTEXT_MAX_CHARS = int(os.getenv("QQ_CONTEXT_MAX_CHARS", "1500"))

# ─── 主动接话 ─────────────────────────────────────────
# 让机器人在群里像个人一样自己开口，而不是只在被 @ 时回复。没 @ 也没命中
# 触发词的消息，会先问一次模型「此刻值不值得插一句」，判「接」且过了冷却闸
# 才叫主模型开口。详见 app/interject.py 开头（含为什么不用本地小模型的实测）。
#
# off 完全不做（默认，零开销）；shadow 照常判断、记日志，但不发言——用来
# 观察判得准不准；on 才真的开口。**建议先 shadow 跑一两天**。
QQ_INTERJECT_MODE = os.getenv("QQ_INTERJECT_MODE", "off").strip().lower()

# 只在这些群里生效，留空表示所有群。主动说话说错撤不回来，建议先填一个群。
QQ_INTERJECT_GROUPS = _env_list("QQ_INTERJECT_GROUPS")

# 同一个群两次主动开口的最小间隔（秒）。这是防刷屏的硬闸：判错一次是意外，
# 连着说就是骚扰。0 表示不限制（不建议）。
QQ_INTERJECT_COOLDOWN = int(os.getenv("QQ_INTERJECT_COOLDOWN", "180"))

# 同一个群两次「判断」之间的最小间隔（秒）。判断本身也是一次 API 调用，
# 群聊刷屏时不能每条都问。0 表示不限制（不建议）。
QQ_INTERJECT_MIN_GAP = int(os.getenv("QQ_INTERJECT_MIN_GAP", "15"))

# 判断时看多少条群聊上下文 / 字数上限。窗口要能盖住一个冷却期里攒下的消息，
# 不然冷却结束只能看到最新一条，冷却期的发言全错过了。
QQ_INTERJECT_CONTEXT_MESSAGES = int(
    os.getenv("QQ_INTERJECT_CONTEXT_MESSAGES", "20"))
QQ_INTERJECT_CONTEXT_MAX_CHARS = int(
    os.getenv("QQ_INTERJECT_CONTEXT_MAX_CHARS", "1000"))

# 机器人在群里的名字（跟 agents/<id>/prompt.md 的人设保持一致）。机器人自己
# 发出去的回复会以这个名字记进群聊缓存——主模型靠它认出「哪些是我刚说过的」，
# 认不出来就会换个说法复读上一句。
QQ_BOT_NAME = os.getenv("QQ_BOT_NAME", "小小怪").strip() or "小小怪"

# ─── 长期记忆（群聊摘要） ─────────────────────────────
# 群聊缓存写满裁剪时，把丢掉的那批消息交给模型压成一条「回忆」，按群存进
# agents/<id>/memory/，回复时随群聊背景一起注入——让它记得住以前聊过什么。
# 详见 app/longterm.py。
# 每轮注入最近几条摘要 / 字数上限（从最新往前累计）。条数 0 = 不注入。
QQ_MEMORY_INJECT_LIMIT = int(os.getenv("QQ_MEMORY_INJECT_LIMIT", "10"))
QQ_MEMORY_INJECT_MAX_CHARS = int(os.getenv("QQ_MEMORY_INJECT_MAX_CHARS", "1500"))
# 摘要调用的超时（秒）。在后台线程跑，卡不到聊天主链路，给宽一点没关系。
QQ_MEMORY_DIGEST_TIMEOUT = int(os.getenv("QQ_MEMORY_DIGEST_TIMEOUT", "60"))

# ─── 掉线通知（微信推送） ─────────────────────────────
# 机器人掉线需要扫码时，把二维码推到手机。它自己发不出消息 —— 掉线的号就是
# 发消息的号 —— 所以必须走一个不在 QQ 里的通道。用 PushPlus：手机扫码登录
# https://www.pushplus.plus/ 拿 token 填这里，留空则整个功能关闭。
# 触发点见 app/notify.py：盯 NapCat 的 qrcode.png，一变就推。
NOTIFY_PUSHPLUS_TOKEN = os.getenv("PUSHPLUS_TOKEN", "").strip()
# NapCat 需要人工扫码时会重写这个文件，它的修改时间就是掉线时刻。
NOTIFY_QRCODE_PATH = os.getenv(
    "NAPCAT_QRCODE_PATH", r"D:\AI\NapCat\napcat\cache\qrcode.png").strip()
