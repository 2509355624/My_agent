"""
配置加载
从 .env 文件读取环境变量，提供全局配置项
"""

import os
import sys
import tempfile

from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT_DIR = BASE_DIR

# ─── state/ 路径与测试隔离 ───────────────────────────
# state/ 下都是**运行期真实数据**（生图账本、群名缓存…），测试跑一遍不能往里
# 写假数据。原先靠各测试文件自己 patch 模块里的 PATH，但总有人忘：2026-10-03
# 实测跑一次全量测试就把 state/qq_names.json 又覆盖成夹具内容、往
# state/image_log.jsonl 灌了一行 "a cat" 假记录（累计已 80 行）。
#
# 判据与 app/usage.py 一致：unittest / pytest 驱动进程时一定会 import 同名顶层
# 模块，而生产进程（main.py / qq_bot.py）不会。测试里**显式** patch 模块的
# PATH 仍然优先——调用点读的始终是模块属性，不经过这里。
_TEST_STATE_DIR = None


def state_path(filename):
    """state/ 下某个文件的路径。

    测试进程一律导向临时目录（同一个进程内固定不变，好让多个测试共享一份）；
    生产进程恒为 <BASE_DIR>/state/<filename>。
    """
    global _TEST_STATE_DIR
    if "unittest" in sys.modules or "pytest" in sys.modules:
        if _TEST_STATE_DIR is None:
            _TEST_STATE_DIR = tempfile.mkdtemp(prefix="agent_state_test_")
        return os.path.join(_TEST_STATE_DIR, filename)
    return os.path.join(BASE_DIR, "state", filename)

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

# 小米 MiMo 开放平台（OpenAI 兼容端点）。mimo-v2.6-flash 按量价：
# 缓存命中输入 ¥0.02/百万、普通输入 ¥1/百万、输出 ¥2/百万。
# 注意：Token Plan 套餐额度与普通 API 余额**互不通用**（官方文档明说，
# 套餐是给 Claude Code / Codex 等编程工具订阅用的），这里走的是按量计费。
MIMO_API_KEY = os.getenv("MIMO_API_KEY", "")
MIMO_BASE_URL = os.getenv("MIMO_BASE_URL", "https://api.xiaomimimo.com/v1")
MIMO_MODEL = os.getenv("MIMO_MODEL", "mimo-v2.6-flash")

# ─── 主备模型降级链 ──────────────────────────────────
# 一次请求失败（额度不足 / 超时 / 5xx / 模型退役）时依次往下试的顺序。
# 格式 "provider:model,provider:model"，第一项是主模型；留空 = 关闭降级。
# 调用方指定了 provider/model（管理页、agent 配置）时，那一项会排在链头，
# 链里重复的项自动去掉——所以管理页手动切换依然优先。
LLM_FALLBACK_CHAIN = os.getenv(
    "LLM_FALLBACK_CHAIN",
    "volc:deepseek-v4-flash-ga-260731,"
    "volc:deepseek-v4-pro-ga-260813,"
    "volc:doubao-seed-2-1-turbo-260628,"
    "volc:glm-5-2-260617,"
    "mimo:mimo-v2.6-flash,"
    "deepseek:deepseek-flash")

# 单次尝试的超时。流式下这是「两块数据之间的最大间隔」而不是总时长——
# 60 秒一个字节都没回就认为卡死，换下一个候选。原先默认 600 秒等于不切。
LLM_REQUEST_TIMEOUT = float(os.getenv("LLM_REQUEST_TIMEOUT", "60"))

# 某个候选失败后「拉黑」多久不再试。到期自动回头重试主模型，所以额度
# 充值、服务恢复之后能自愈，不用重启进程。
LLM_FALLBACK_TTL = float(os.getenv("LLM_FALLBACK_TTL", "600"))

# HTTP 429（请求频率/额度到顶）的专用拉黑时长，默认 24 小时（2026-09-29 用户定）。
# 429 不是「这条请求失败」，是「这家这一阵都不给了」——免费额度按天重置，
# 600 秒后重试纯属白撞。拉黑一整天，让降级链直接把请求交给还能用的模型。
LLM_RATE_LIMIT_TTL = float(os.getenv("LLM_RATE_LIMIT_TTL", "86400"))

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

# llama.cpp 的 llama-server（2026-10-04 接入，主对话+识图都指向它）。
# base_url **带 /v1**：llama-server 只有 OpenAI 兼容端点（/v1/chat/completions），
# 没有 ollama 那个 /api/chat——provider 分派里它走通用 OpenAI 分支，URL 直接
# 拼在这个尾巴上。model 名 llama-server 不校验（服务端就载了一个模型），这里
# 填文件名只是让日志/下拉里能认出是谁。识图能力取决于启动时有没有 --mmproj，
# 有 = vision True（实测 MiMo Q5_K_M + mmproj f16 一张图 3.6 秒）。
LLAMA_BASE_URL = os.getenv("LLAMA_BASE_URL", "http://127.0.0.1:8081/v1")
LLAMA_MODEL = os.getenv("LLAMA_MODEL", "MiMo-V2.6-Distill-Qwen-9B-Q5_K_M")

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
    # llama.cpp 的 llama-server（2026-10-04）：OpenAI 兼容端点，主对话+识图
    # 都走它。vision=True 的前提是启动命令带了 --mmproj（见 start-server.bat）；
    # 不带 mmproj 时带图请求会 500，表现为该轮识图失败降级纯文本。
    # 注意与 ollama 是**两个独立服务**（8081 vs 11434），同时常驻会各占一份显存。
    "llama": {
        "label": "llama.cpp 本地",
        "base_url": LLAMA_BASE_URL,
        "model": LLAMA_MODEL,
        "api_key": "",
        "needs_key": False,
        "vision": True,
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
    # 小米 MiMo。mimo-v2.6-flash 官方标注全模态（文本/图像/语音），vision=True
    # 让带图请求直接多模态下发，省掉 DeepSeek 识图预处理那一跳。
    # 模型名不含 vl/vision/omni 关键字，所以必须靠这个开关显式声明。
    "mimo": {
        "label": "小米 MiMo",
        "base_url": MIMO_BASE_URL,
        "model": MIMO_MODEL,
        "api_key": MIMO_API_KEY,
        "needs_key": False,
        "vision": True,
    },
}

# 模型名里出现这些词就认为它有视觉能力，用来覆盖上面的 provider 开关。
# 场景：豆包换成 doubao-1-5-vision、本地换 qwen-vl-max，都不用改代码。
# 小米 mimo 全系列都是全模态（文本/图像/语音），但模型名 "mimo-v2.6-flash"
# 不含 vl/vision/omni 关键字——只靠 provider 的 vision 开关会在「只覆盖
# 模型、没改 provider」时漏判（误当非视觉→多跑一道识图预处理）。把 mimo
# 也加进关键字，保证只要生效模型是 mimo 就直接多模态下发。
_VISION_MODEL_HINTS = ("vl", "vision", "omni", "mimo")


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
# （实测一张图 246 + 340 token，比跑一轮对话还低。）
#
# 2026-10-03 试过把读图改走本机 ollama 省成本，实测**热态 24~42 秒**（冷启 65 秒，
# 见下面 OLLAMA_VISION_* 的注释），而云端 MiMo 只要 1~2 秒 —— 用户当天拍板
# **改回云端**。本地这条路留着当备选，不用改代码：把 .env 的 VISION_PROVIDER
# 改成 `ollama`、VISION_MODEL 填 `qwen3-vl:2b` 即可（app/vision.py 里那条原生
# /api/chat 分支还在，且实测 OCR 正确、英文报错原样保留不翻译）。
#
# 审核（app/image_audit.py）**不做本地档**：它超时只有 30 秒且 fail-closed，
# 本地那个速度会把每张图都拦死。审核永远走这里配的 VISION_PROVIDER。
VISION_PROVIDER = os.getenv("VISION_PROVIDER", "deepseek")
# 空 = 用该 provider 的默认模型
VISION_MODEL = os.getenv("VISION_MODEL", "")

# 本地识图（ollama）跑在 CPU/内存，别跟 ComfyUI 抢显存。为什么不用给 ollama
# 服务设 CUDA_VISIBLE_DEVICES：那要重启 ollama，且会影响**所有** ollama 调用；
# 走原生 /api/chat 的 options 就能按次指定（见 app/vision.py 的 _post_ollama）。
# 实测生效（`ollama ps` 显示 100% CPU、2.0GB 常驻内存、显存零占用）。
OLLAMA_VISION_NUM_GPU = int(os.getenv("OLLAMA_VISION_NUM_GPU", "0"))
# 用完多久卸载模型。2B 模型常驻内存不划算，而且这台机器可用内存常在
# 1.2~2.9GB 之间晃——给个 5 分钟，闲下来就还回去。冷启要重付一次加载成本
# （实测冷 65s / 热 24~42s），但常驻 2GB 更不划算。
OLLAMA_VISION_KEEP_ALIVE = os.getenv("OLLAMA_VISION_KEEP_ALIVE", "5m")

# 识图是单次同步调用，正常几秒返回；超时按失败降级。
# 2026-09-29 从 30 秒放宽到 120 秒：识图走的是 mimo（火山 429 后所有请求都压到它），
# 高峰期实测单张能拖到 30 秒以上——30 秒会把「慢但成功」的调用误判成失败。
# 注意：这是 requests 的「单次 socket 操作」超时（含首字节等待），不是整次请求
# 的总时长上限；服务端若持续滴数据，仍可能超过这个值。
VISION_TIMEOUT = float(os.getenv("VISION_TIMEOUT", "120"))
# 发出去前把图缩到最长边这么多像素。QQ 群里的图有 8MB 级的，直接 base64
# 是 11MB 字符串——既慢又贵，还可能撞服务端请求体上限
VISION_MAX_EDGE = int(os.getenv("VISION_MAX_EDGE", "1024"))

# 生图审核（app/image_audit.py）单张的识图超时。**故意比 VISION_TIMEOUT 短**：
# 识图预处理卡住只是让这轮对话慢，而审核卡住是**挡在发图这一步前面**，
# 用户在群里干等。实测正常 1.3~2.1 秒返回，30 秒足够宽松。注意口径已改成
# fail-closed：超时是**拦下不发**（见那个模块的注释），所以这个值往下砍等于
# 更容易误拦——识图偶尔慢一点，本来合规的图也发不出去。别乱调短。
IMAGE_AUDIT_TIMEOUT = float(os.getenv("IMAGE_AUDIT_TIMEOUT", "30"))

# 自定义审核提示词的长度上限（字符）。审核提示词**每张图都要重发一遍**，
# 它是尾巴的一部分、每次都全价计费，所以给个上限防手滑贴进来一整篇文章。
# 内置默认那份约 300 字，2000 给了很宽的余量，够写一版更细的口径。
IMAGE_AUDIT_PROMPT_MAX = int(os.getenv("IMAGE_AUDIT_PROMPT_MAX", "2000"))

# 自定义**识图（通用读图）提示词**的长度上限（字符），2026-10-02 加。
# 口径与上面那条一致：识图也是**每张图都要重发一遍**的隐形调用，写长了纯烧
# token；但它不像审核那样「丢了输出格式就全拦」，所以长度之外不再做别的校验。
# 内置默认那份约 100 字，2000 够写一版很细的读图要求。
VISION_PROMPT_MAX = int(os.getenv("VISION_PROMPT_MAX", "2000"))

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

# ─── ComfyUI 内存水位（自动重启）─────────────────────
#
# ComfyUI 的常驻内存**只涨不落**：每张图约 +600MB，`/free` 也降不下来
# （它只把权重从显存搬到 CPU，不删）。16GB 物理内存被挤干之后，GGUF 要从
# 磁盘重读（实测 5.5 秒 → 68 秒）、采样卡在 0/N 一百秒，最后被
# IMAGE_GEN_TIMEOUT 掐掉——整机还跟着换页变卡。
#
# 唯一能把内存真正还回去的手段是重启进程（走 ComfyUI-Manager 的
# /manager/reboot，Legacy 模式下 os.execv 原地重启、保留原命令行）。
#
# 这里定的是**系统可用内存**的下限：低于它就重启。用绝对水位而不是百分比，
# 因为要防的是「整机换页」这件事，跟总内存多大无关。
#
# 0 = 关掉这个功能（回到从前：只涨，从不主动重启）。
COMFY_MIN_FREE_RAM_GB = float(os.getenv("COMFY_MIN_FREE_RAM_GB", "3.0"))

# 两次**尝试**重启之间的最小间隔（秒）。防抖用：内存要是被别的程序吃掉的，
# 重启 ComfyUI 也救不回来，这个间隔保证不会退化成「每张图都重启」。
# 成功失败都算，所以重启接口被拒时也不会每张图刷一条警告。
COMFY_RESTART_MIN_GAP = float(os.getenv("COMFY_RESTART_MIN_GAP", "300"))

# 等 ComfyUI 重启回来的上限（秒）。冷启动到能接第一张图约 60 秒，留够余量。
COMFY_RESTART_WAIT = float(os.getenv("COMFY_RESTART_WAIT", "180"))

# ─── ComfyUI 显存水位（提交前先 /free）───────────────
#
# ComfyUI **从不把上一个任务清干净**。日志里那句
#   Unloaded partially: 2896.25 MB freed, 1591.04 MB remains loaded
# 就是证据：它每次只卸一部分，残留 1.6~2.1GB 会一路叠上去。实测 qwen 连画
# 16:09 成 / 16:12 成 / 16:14 崩，看着像残留累积。
#
# 现有逻辑只在**换渠道**时打 /free，同渠道连画不释放——qwen 连画正好是唯一
# 漏掉的那种情况。低于这个水位就补一次 /free，把残留腾出来。
#
# 2.0 是实测定的：anima 跑完还剩约 4.9GB、qwen 跑完约 3.1GB —— 两种都不该触发
# （模型保持热的，下一张马上还要用）。真掉到 2GB 以下才是「残留把空间吃掉了」。
# 0 = 关掉（回到从前：只在换渠道时释放）。
#
# ⚠️ **2026-10-04：.env 里那份 5.0 已改回 2.0（.env 优先于这里的默认值）。**
# 5.0 是 **12GB 卡**上量出来的（anima 跑完剩 5.5GB，刚够不触发）。本机换过机器，
# 实测 **RTX 5070 / 11.94GB**：anima 仍剩约 4.9GB、qwen 剩约 3.1GB —— 都**恒低于
# 5.0**，于是每张提交前都白打一次 /free，把刚跑热的权重整个卸掉、下一张再去磁盘
# 重读 6GB 文本编码器（01:33:40 那次只腾出 0.1GB）。同渠道耗时因此差一倍：
# 热跑 49.2s vs 冷跑 109.4s。2.0 只在真见底时才动手。
#
# ⚠️ **这条不是 qwen 崩溃的解药**（2026-09-27 16:22 真机实测推翻）：
# ComfyUI 刚重启、显存全空 10.78GB、连第一张 qwen 照样崩（3 条 nvlddmkm 153）。
# 真正的天花板是**权重本身**：TE 6018MB + unet 4487MB = 10.5GB，而 12GB 卡
# 空闲时只有 10.78GB 可用（约 1.16GB 被桌面/浏览器占着），只剩约 0.5GB 给
# 激活值——1024×1024 采样时不够，所以是概率性崩，不是必崩。
# 日志 `loaded completely; 5129.88 MB usable` 也印证：算这个「可用」时
# 6018MB 的编码器还驻留着。
# 这条水位只管「别让残留把本就紧张的空间再吃掉一块」，是保险不是解药。
COMFY_MIN_FREE_VRAM_GB = float(os.getenv("COMFY_MIN_FREE_VRAM_GB", "2.0"))

# ─── 重渠道（qwen）优先度：2026-10-04 按用户要求关闭 ───────────────
#
# ⚠️ **2026-10-04 用户拍板关闭（原值 90）**。触发场景：他连着跑漫画加字
#   （连环 qwen i2i），每两张之间被强制插 90 秒，观感是「一个生图任务卡死
#   3 分钟」。关掉后 qwen 之间不再强制间隔，按入队顺序连着跑。
#
# **恢复办法**（真要回滚，改这三处）：① 这里或 .env 写 QWEN_COOLDOWN=90；
#   ② `skills._SKILL_PRIORITY` 加回 `{"qwen_image_v1": 5}`；
#   ③ `skills/qwen_image_v1/SKILL.md` 的 frontmatter 加回 `priority: 5`。
#   ②③ 少了任何一条，权重就回到 1，冷却窗不开（见 image_jobs._finish 的
#   `job.weight > 1` 判据）。
#
# 为什么当初要有它（保留备查）：qwen 一套权重 10.5GB（文本编码器 6018MB +
#   unet 4487MB），本机 12GB 显卡空闲可用只有 10.78GB。2026-09-27 实测的错误
#   模式是确定性的：**第 1 张必成、第 2 张必死**（提交后 2~6 秒日志断在
#   `got prompt` 中间 → nvlddmkm 153，有时整机重启）。外挂启动参数与更低的
#   量化都试到底、全被推翻（见 image_jobs 模块开头），所以当初只能从 agent
#   侧管：别让 qwen 紧接着 qwen 跑。冷却窗本意是显存/内存把 6GB 权重还回去
#   所花的时间——文档自己记着实测 **3 分钟**的间隔才足以自然恢复，90 秒已经
#   是打折后的折中值。**关掉 = 接受连跑风险**，出问题时表现是 ComfyUI 变僵尸
#   （8188 永久 500）或整机重启。
#
# ⚠️ 这个数值**跟「一张图跑多快」无关**：步数（10 / 12 / 20）只决定采样迭代
#   几轮，权重加载量一个字节都不变。用户 2026-10-04 的疑问「我都搞到 10 步长
#   了还这么重吗」就是混淆了这两件事。
#
# 0 = 关掉冷却（当前值）。
QWEN_COOLDOWN = float(os.getenv("QWEN_COOLDOWN", "0"))

# ─── 停用的生图渠道（硬件跑不动，不是配置问题）──────
#
# 逗号分隔的 skill 名；**留空 = 全部可用（当前就是空的）**。被列进来的渠道
# **代码全保留**，只是 `generate_image` 不再放行——模型点名也没用，会拿到一句
# 能直接转述的错话。要临时停掉某个渠道，在这里（或 .env）写上它的名字即可。
#
# ⚠️ **2026-10-01 用户拍板全部解封，本列表清空**：qwen_image_v1 与 krea2 都放开。
# 下面是各渠道的来历与风险，保留备查：
#
# ⚠️ 2026-10-01 起把**尺寸档**独立成 3 档，跟 4 个画风正交，凑成 16 个生图渠道：
# 普通档 `anima_<画风>`（728~768×1024，默认不放大）+ 大图档 `hd_fast_<画风>`
# （1024×1536，不放大，最快）/ `hd_2_<画风>`（1328×2000，1.3× 放大，中间档）/
# `hd_3_<画风>`（1536×2304，1.5× 放大，最大最慢、最吃显存）。hd_* 三档**比普通档
# 重得多**（`hd_2` 起就明显慢、`hd_3` 最吃显存），还没经过长期稳定性验证——
# 显存吃紧时优先走普通档。
#
# **qwen_image_v1**（2026-10-01 解封）：
# 2026-09-27 停用它，根因是**内存不足**——一套权重 10.5GB（文本编码器 6018MB +
# unet 4487MB），当时本机只有 16GB 内存，单张就能把整机拖崩（实测五次连崩、
# 三次整机重启；最后一次 17:42:05 入队、17:43:18 重启，日志停在第一张图提交后，
# 队列里**只排了它一张**）。当时外挂启动参数（`--vram-headroom` /
# `--disable-pinned-memory` / `--lowvram`）与更低量化都试到底并被推翻。
#
# ⚠️ 解封当天实测本机总内存**仍是 16GB**（`Win32_PhysicalMemory` 只有一条
# 16GiB，`TotalVisibleMemorySize` 也是 15.62GB），不是早先文档里写的 32GB。
# 用户知悉这个出入，仍拍板「照常解封、风险自担」。也就是说 qwen 的**内存风险
# 并没有真正解除**，只是用户选择接受——真跑崩了别当成新问题。
# 队列侧那套软保护**原样保留**（`skill_priority` 权重 5 排最后 + `QWEN_COOLDOWN`
# 冷却窗，见 image_jobs 的「重渠道优先度」）——它们只管调度顺序，不影响能不能跑。
#
# **krea2**（2026-10-01 解封）：2026-09-27 曾停用，合并后权重 7521.96 MB，比
# anima 重得多，出图还要经 2x Ani4K 放大到 2048×3072（当时的输出尺寸）。
# **没有崩机记录**——当初是「跑不动 / 不稳」的判断，不是像 qwen 那样实测必崩，
# 别把 qwen 那套结论套到它头上。2026-10-01 用户一并拍板解封。
# 同日按用户要求**去掉超分**：画布 1024×1536 → 832×1216，`UpscaleModelLoader` +
# `ImageUpscaleWithModel` 两个节点从工作流里删掉，现在单段直出 832×1216
# （跟 qwen / SD 一个尺寸）。"本机最大输出"这个说法已经作废——最大的是 `hd_3_*`
# （1536×2304）。
#
# **image_gen_v1（SD / SDXL）**：2026-10-01 同日写进工具描述、并加进 QQ 白名单，
# 跟 `agents/draw` 共用。832×1216 单段 30 步，它跟 krea2 都只算**普通权重**（1）。
#
# 停用渠道**代码全留**，只是不再放行；要重新打开：把这里清空即可。
DISABLED_IMAGE_SKILLS = [s.strip() for s in
                         os.getenv("DISABLED_IMAGE_SKILLS", "").split(",")
                         if s.strip()]

# ─── NovelAI（群主独立生图 token）────────────────────
# NAI 是一个**云端**动漫生图 API，跟本机 ComfyUI 完全独立：token 是群主自己的，
# 只在管理员为指定群开通后才可用（见 app/agents.nai_allowed），不共享给别的群。
#
# 为什么单独配代理：本机常驻 Clash 把代理写进了**注册表**，而项目里所有出网口
# 一律 trust_env=False（为了 localhost 的 ComfyUI 不被劫持）。NAI 是外网，必须
# 走代理——这里单独读注册表 / 显式代理，不跟 ComfyUI 那套混在一起。
#
# NAI_API_KEY：群主给的持久 token，只放 .env（已在 .gitignore），绝不进仓库。
# NAI_PROXY：显式代理地址；留空 = 自动读本机注册表系统代理（用户开 VPN 后写在这）。
# NAI_ENABLED：.env 级的硬总闸（默认开），纯紧急熔断用——真正控制「开不开」的
#   是管理页 settings.json 的 nai_enabled（默认关）+ 单群白名单 nai_groups。
NAI_API_KEY = os.getenv("NAI_API_KEY", "")
NAI_PROXY = os.getenv("NAI_PROXY", "")
NAI_ENABLED = os.getenv("NAI_ENABLED", "true").lower() not in ("0", "false", "no", "")

# ─── Agent ──────────────────────────────────────────

AGENT_PORT = int(os.getenv("AGENT_PORT", "5174"))
MAX_TURNS = int(os.getenv("MAX_TURNS", "10"))

# 上下文预算：**单次请求允许携带的 prompt token 上限**。
# 注意它跟模型的物理上下文上限（火山/DeepSeek 是 128K~1M）是两件事——
# 这里管的是「我愿意为每一轮付多少钱」，不是「模型装不装得下」。超了就
# 压缩历史（见 app/memory.py）。
# **这是压缩的唯一判据**（2026-09-29 起）：到预算就压，不再看轮数。
# 判据用 memory.estimate_messages 的**本地估算**，刻意不读 API 的 usage：
# usage 走 threading.local，而 QQ 适配层用 asyncio.to_thread 从线程池取线程，
# 同一会话的不同消息落在不同线程上，读到的 total_tokens 常常是 0 ——
# 这就是「预算设了却从来没压缩过」的根因（实测一个群攒到 18 万字全量重发）。
# 折算参考：一个中文字符约 0.6 token，32000 约合 5.3 万汉字、50~60 轮对话。
# 单个 agent 可在 agent.json 里用 context_budget 覆盖（0 = 用这里的全局值）。
CONTEXT_BUDGET = int(os.getenv("CONTEXT_BUDGET", "32000"))

# 各provider 的**物理上下文窗口**（token）。用于给 CONTEXT_BUDGET 封顶：
# 预算是「我愿意为这一轮付多少钱」，物理窗口是「模型装不装得下」——
# 预算线高过物理墙时，压缩这条闸门就形同虚设（prompt 撞满窗口后才想起
# 已经超了，可那时已经截断完了）。
#
# 只列**明显小于全局预算**的 provider。云端（volc/deepseek/scnet/mimo）都是
# 128K~1M，装得下 32000，不列= 不封顶，行为与从前完全一致。
#
# ollama 的窗口不是模型属性，而是 Modelfile 里写死的 num_ctx（实测这台
# 机器上 qwen3.8-9b-heretic = 16384）。要改就改 Modelfile 的 num_ctx，
# 这里只是读同一个数、提前一步拦住，别指望它等于模型上限。
_PROVIDER_CONTEXT_WINDOW = {
    "ollama": int(os.getenv("OLLAMA_NUM_CTX", "16384")),
    # llama-server 的窗口由启动参数 -c 决定（start-server.bat 里 16384），
    # 两边保持同一个数：改启动脚本时这里要跟着改。
    "llama": int(os.getenv("LLAMA_NUM_CTX", "16384")),
}

# 压缩后的目标水位 = 窗口的这个比例。留出的余量给状态栏、extra_context
# 这些挂在尾巴上的东西（实测≈465 token）以及本轮新发的消息。
_CONTEXT_WINDOW_SAFE_RATIO = 0.7


def physical_budget(provider=None, model=None, budget=None):
    """把 token 预算压到模型物理窗口以内。

    provider 不在 _PROVIDER_CONTEXT_WINDOW 里（或窗口 >= 预算）就原样返回，
    所以对云端 provider 是彻底的 no-op。
    """
    if budget is None:
        budget = CONTEXT_BUDGET
    pid = (provider or LLM_PROVIDER or "").lower()
    window = _PROVIDER_CONTEXT_WINDOW.get(pid)
    if not window:
        return budget
    cap = int(window * _CONTEXT_WINDOW_SAFE_RATIO)
    return min(budget, cap) if cap > 0 else budget

# 会话历史窗口（**已废弃，只作兼容开关**）。
#
# 2026-09-27 曾按轮数开窗（保留最近 N 轮），理由是「轮数不用读 usage」。
# 2026-09-29 又改回 token 判据（见 CONTEXT_BUDGET）：轮数开窗挡不住
# 「100 轮里塞了 500 条消息」的群——实测那个群到 384 条 / 13.2 万字，
# 按轮数裁完全压不住，单轮又慢又贵。
#
# 现在 trim_window 只在 max_turns<=0 时**关闭裁剪**，正数一律忽略。
# 保留这个配置项是为了「一键关掉压缩」这条退路。
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

# 协议端的 **WebUI 端口**。NapCat = 6099，SnowLuma = 5099。
#
# 这个口只给看门狗用：:3000 只在登录成功后才监听，光看它分不出「进程没起来」和
# 「起来了但登录态失效」。WebUI 口**登录前就监听**，正好把两者分开。
# 2026-09-30 起协议端可换（SnowLuma 与 NapCat 的 OneBot 端口 3000/3001 完全一致，
# 只有 WebUI 口不同），所以这里改成可配。默认保持 6099，不影响现有部署。
WEBUI_PORT = int(os.getenv("QQ_WEBUI_PORT", "6099"))

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

# 单轮硬上限：一轮超过这么久还没跑完就放弃（线程留在后台自生自灭，会话线
# 立刻恢复干活）。这是「单群卡死传染」的保险丝——2026-09-27 实测出现过
# runner 卡 ~16 分钟、积压消息全部迟到的事故，且卡点不在任何已设超时的
# 网络调用上。正常一轮（LLM + 工具）远到不了这个数；MAX_TURNS=10 的极端
# 链路重试也撑不到 600s，能被砍掉的只有真卡死的轮次。
QQ_TURN_TIMEOUT = float(os.getenv("QQ_TURN_TIMEOUT", "600"))

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

# 静默告警：连续这么多小时零收发且探活正常 → 推一条「疑似冻结」。
# 抓的是「冻而不掉」（2026-09-27 事故：协议层活着、消息同步停摆，二维码
# 机制完全无感）。半夜安静群会误报，用户拍板宁可误报。0 = 关闭。
NOTIFY_SILENCE_HOURS = float(os.getenv("NOTIFY_SILENCE_HOURS", "4"))

# ── 看门狗的「静默判据」（假在线）──────────────────────────────
# 看门狗的三态探针**探不出「假在线」**：:3000 接口全好、登录态在，只有
# 「腾讯 → QQ 客户端」的下行推送死了（2026-09-29 22:36~22:53 实测）。能观测到的
# 唯一信号是「本该到的消息没到」。所以改看 qq_bot 的静默时长（状态快照里的
# last_activity_ago），超过 WATCHDOG_SILENCE_SECONDS 一条都没有 → 重启当探针。
#
# ⚠️ 重启当探针为什么成立（用户 2026-09-29 的判断，已被日志证实）：
#   自动登录 → 会话本来是好的，就是「群里本来就安静」→ 静默放过、不通知；
#   要扫码   → 会话早被腾讯作废，这正是假在线/掉线的本质 → 推二维码。
# 所以误报的代价只是一次静默重启，真故障却能第一时间暴露。
WATCHDOG_SILENCE_SECONDS = float(os.getenv("WATCHDOG_SILENCE_SECONDS", "1200"))

# 连续静默时两次重启之间的**起步**间隔（秒）。每静默重启一次就翻倍，封顶
# WATCHDOG_SILENCE_MAX_GAP；一旦收到消息立刻清零。
# ⚠️ 没有它就会出事：静默重启成功后 qq_bot 一起重启、静默计时归零，整夜没人
# 说话就变成每 WATCHDOG_SILENCE_SECONDS 杀一次 QQ —— 反过来招风控。
WATCHDOG_SILENCE_COOLDOWN = float(os.getenv("WATCHDOG_SILENCE_COOLDOWN", "3600"))
WATCHDOG_SILENCE_MAX_GAP = float(os.getenv("WATCHDOG_SILENCE_MAX_GAP", "14400"))

# ⚠️ 静默时**要不要真的重启**。默认 0 = **只记日志、不重启**。
#
# 原来是 1（重启当探针），09-30 用户拍板改成默认关。原因是一天 32 次实测：
#
#   静默重启 → 一键启动全部 force → taskkill /IM QQ.exe /F → -q 快速登录
#     → 腾讯判定「刚登过又登」= 异常登录 → 作废会话
#     → 真掉线（tag=下线通知 / 你的账号当前登录已失效）
#     → 又没人说话 → 又静默 → 又重启 …… 死循环
#
# 09-30 硬证据：08:57:50 静默重启（08:57:59 还报过「已有 QQ 适配层在运行」），
# 08:58:55 重连成功，**09:00:57 就掉了线**——间隔 3 分钟。05:37 那次更直接：
# 重启后看门狗自己因为锁冲突退出（05:37:18），机器人反而活下来了。
#
# 所以「假在线」很可能是这个循环**制造**出来的，不是根因。先关掉静默重启，
# 用一天观察真实掉线频率：明显下降 = 推断成立；照旧 1 小时一次 = 真是风控。
# 想恢复老行为把 .env 里设 WATCHDOG_SILENCE_RESTART=1（改完要重启看门狗）。
WATCHDOG_SILENCE_RESTART = os.getenv("WATCHDOG_SILENCE_RESTART", "0") not in ("0", "false", "False", "")

# ── ComfyUI 看门狗分支总闸（2026-09-30）──────────────────
# ⚠️ 默认 0 = 整个 ComfyUI 分支不动作（只探活不重启）。
# 09-30 凌晨它过度重启：05:20/05:51/06:26 各触发一轮（含 1800 秒退避），
# 因为 ComfyUI 是**按需启动、常常故意不开**，看门狗却当成「崩了」。
# 后来给分支加了 `comfy_seen` 闩（只在「本来在跑」时才管），但那一版还没在
# 真机验过就撞上了这次事故，所以先整体关掉；等真机验证过 comfy_seen 再开。
WATCHDOG_COMFY_ENABLED = os.getenv("WATCHDOG_COMFY_ENABLED", "0") not in ("0", "false", "False", "")
