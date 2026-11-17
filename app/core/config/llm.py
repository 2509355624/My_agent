"""LLM 配置域：provider、模型、降级链、超时、重试与预算。

原则：
- 这里只放“默认值”和结构定义，不放业务分支。
- 密钥只从 .env 读，绝不写进仓库；管理页只改非密钥设置。
- 后续运行时覆盖走 app/core/config/runtime.py，调用方读 effective 值，
  不再 import 一个启动时就冻结的常量。
"""

import os

# ─── LLM Provider ────────────────────────────────────

LLM_PROVIDER = os.getenv("LLM_PROVIDER", "volc").lower()

# 角色扮演专用默认模型（2026-11-06 用户拍板）：
# 扮演链路的调用（主回复 / 导演 / 开场 / 小记压缩 / RAG 提炼）默认优先用这一组；
# 请求失败仍走全局 LLM_FALLBACK_CHAIN。留空 = 用全局默认。
# 玩家在网页里临时指定自定义模型时，那个优先（见 `persona._llm_call`）。
PERSONA_PROVIDER = os.getenv("PERSONA_PROVIDER", "cx_a1b2c3d4").strip()

# 默认模型换成 v4.1-flash（2026-11-14 用户拍板 + 实测）：
# 同一段成人向 prompt，v4.1f 5.1 秒写完 730 字，v4p 11.4 秒写到 697 字突然
# 吐出「Sorry, I can't respond to this question.」然后停住——群友看到的
# 「回复被截断」就是它。v4p 更细腻，但会在露骨处自我审查，所以降级成可选项。
PERSONA_MODEL = os.getenv("PERSONA_MODEL", "deepseek-v4.1-flash").strip()

# 网页端玩家能挑的模型（2026-11-14）。**只放这一份白名单**：
# 前端不写死、也不允许玩家传任意模型名——providers 里能直连付费官方的那几个
# 露出去，等于把站主的 Key 送人。
# `default` 那个是没选过时的落点。
PERSONA_MODELS = [
    {"id": "deepseek-v4.1-flash",
     "label": "V4.1 Flash",
     "note": "快，写得完整，默认",
     "provider": "cx_a1b2c3d4"},
    {"id": "deepseek-v4-pro",
     "label": "V4 Pro",
     "note": "更细腻，慢一倍，个别情况下会中途停笔",
     "provider": "cx_a1b2c3d4"},
]
PERSONA_MODEL_DEFAULT = os.getenv("PERSONA_MODEL_DEFAULT",
                                  "deepseek-v4.1-flash").strip()


def persona_model_ids():
    """白名单里的模型 id 集合。调用方用它挡住玩家乱传的模型名。"""
    return {m["id"] for m in PERSONA_MODELS}


def persona_model_of(model_id):
    """按 id 取一条白名单记录；不在白名单里返回 None。"""
    want = str(model_id or "").strip()
    for m in PERSONA_MODELS:
        if m["id"] == want:
            return m
    return None

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
#
# ⚠️ 2026-10-14：.env 里现在也显式写着同一条链（用户拍板「.env 是改链入口」，
# 10-11 那次挪去代码结果紧急时找不到入口）。**两边必须同值**——.env 缺失时
# 用的就是这里的默认值（新部署 / MIGRATION 场景），分叉过一次（10-11：.env 3 条
# vs 代码 6 条）差点耽误事，改链请两边一起改。
#
# 2026-10-14 排序理由（火山/超算额度集体耗尽，用户：「火山不关、只降温」）：
#   deepseek 官方活着 → 打头；mimo 活着 → 第二；火山只剩 glm-5.2 还有额度
#   → 第三；欠费的火山三项留着垫底等充值（_DEAD 拉黑期内自动跳过）。
LLM_FALLBACK_CHAIN = os.getenv(
    "LLM_FALLBACK_CHAIN",
    "deepseek:deepseek-flash,"
    "mimo:mimo-v2.6-flash,"
    "volc:glm-5-2-260617,"
    "volc:deepseek-v4-flash-ga-260731,"
    "volc:deepseek-v4-pro-ga-260813,"
    "volc:doubao-seed-2-1-turbo-260628")

# 单次尝试的超时。流式下这是「两块数据之间的最大间隔」而不是总时长——
# 60 秒一个字节都没回就认为卡死，换下一个候选。原先默认 600 秒等于不切。
LLM_REQUEST_TIMEOUT = float(os.getenv("LLM_REQUEST_TIMEOUT", "60"))

# 扮演链路里「裁判 / 意图 / 打分」这类内部短调用单独的可配超时（2026-11-10）：
# grok 这类慢模型首次出字要 30s+，之前硬编码 30 会把裁判掐断、兜底成 0 分，
# 表现为「第一次打分失败、第二次成功」。默认跟随 LLM_REQUEST_TIMEOUT（60s），
# 真有更慢的模型就在 .env 里单独给 LLM_JUDGE_TIMEOUT 加长。
_LLM_JUDGE_TIMEOUT_RAW = os.getenv("LLM_JUDGE_TIMEOUT", "")
LLM_JUDGE_TIMEOUT = (float(_LLM_JUDGE_TIMEOUT_RAW)
                     if _LLM_JUDGE_TIMEOUT_RAW else LLM_REQUEST_TIMEOUT)

# 某个候选失败后「拉黑」多久不再试。到期自动回头重试主模型，所以额度
# 充值、服务恢复之后能自愈，不用重启进程。
LLM_FALLBACK_TTL = float(os.getenv("LLM_FALLBACK_TTL", "600"))

# HTTP 429 的拉黑时长（2026-11-09 用户改口径：86400 → **30 秒**）。
#
# 原来是「429 = 额度到顶，免费额度按天重置，短时间重试纯属白撞」→ 拉黑一整天。
# 实战打脸：hfapi 中转站的并发上限只有 2，角色扮演一轮要打编剧+写作两次调用，
# 两个玩家同时在线就撞 429——那是**限流**，不是额度。拉黑一整天 = 站明明活着，
# 后面所有轮次全走付费的官方 DeepSeek（11-09 实录：17:46 起连黑 86400s，
# 用户看到的就是「中转站正常，角色扮演却用官方，很费钱」）。
#
# 用户拍板：中转站出问题就同一家重试 3 次，失败拉黑 30 秒走降级链，
# 到期自动回头再试。「对我没有损失，只是等待久一点」——就算真是额度到顶，
# 代价也只是降级链每轮多白撞一次 429，比黑一整天便宜得多。
LLM_RATE_LIMIT_TTL = float(os.getenv("LLM_RATE_LIMIT_TTL", "30"))

# 429 里还得分一层：**并发冲突**和 5xx / 读超时这类瞬时抖动同档（2026-11-09
# 用户统一拍板 30 秒：「重试 3 次，失败就 30 秒拉黑，之后再尝试」）。
# 2026-10-26 实测 hfapi 中转站的并发上限只有 2，报
#   {"code":"concurrency","message":"too many concurrent requests for this
#    api key (2 in flight, limit 2)"}
# 角色扮演一轮要打编剧+写作两次调用，两个玩家同时在线就撞——这是**限流**
# 不是故障，等几十秒、在飞的请求返回就能再用。这里的 30 秒是「等同时在飞的
# 那一两个请求返回」的量级，到期自动回头试链头，不会长时间待在付费官方。
LLM_CONCURRENCY_TTL = float(os.getenv("LLM_CONCURRENCY_TTL", "30"))

# 同一候选内部的**重试**（2026-11-09，用户拍板「重试 3 次之后再降级」）。
#
# 起因：中转站常常只是这一秒抽风（502 / 读超时 / 连接被掐），而旧逻辑一失败
# 就把这个候选拉黑一小时，后面所有轮次都得走付费的官方 DeepSeek——一次瞬时
# 抖动换一整小时的账单。现在瞬时故障先在**同一个候选**上重试，3 次都不过才
# 换下一家。
#
#   LLM_RETRY_TIMES   单个候选最多尝试几次（含第一次，默认 3）
#   LLM_RETRY_DELAY   第 n 次重试前等 n×该值秒（默认 1.0 → 等 1s、2s）
#   LLM_RETRY_BUDGET  单个候选花在重试上的**总时长上限**（秒，默认 20）
#
# 预算这条是给「读超时」留的闸：一次超时已经吃满 LLM_REQUEST_TIMEOUT（60s），
# 再重试三次等于让用户干等三分钟。超预算就直接降级，下一轮反正有 60 秒短
# 拉黑兜着（见 llm._ttl_for）。
LLM_RETRY_TIMES = max(1, int(os.getenv("LLM_RETRY_TIMES", "3") or 3))
LLM_RETRY_DELAY = max(0.0, float(os.getenv("LLM_RETRY_DELAY", "1.0") or 1.0))
LLM_RETRY_BUDGET = max(0.0, float(os.getenv("LLM_RETRY_BUDGET", "20") or 20))

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
    # 2026-10-07：默认模型换成 doubao-seed-2-1-turbo-260628（多模态，能读图）。
    # 用户在管理页把「识图模型」选成豆包时，下拉存的是 `doubao:`（模型留空），
    # 生效时回落到**这里的默认模型**——所以默认值必须是能读图的那个，否则
    # 识图会拿纯文本模型去读 base64，整条请求挂死（见文件顶部的说明）。
    # 纯文本的旧型号（doubao-1-5-thinking-pro-*）仍可显式指定，届时靠模型名
    # 判定为准（见 _VISION_MODEL_HINTS），不会误当视觉模型。
    "doubao": {
        "label": "豆包",
        "base_url": VOLC_BASE_URL,   # 火山方舟兼容 OpenAI 接口
        "model": os.getenv("DOUBAO_MODEL", "doubao-seed-2-1-turbo-260628"),
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
# "seed" 同理：豆包 Seed 系列（doubao-seed-2-1-*）是多模态，而 doubao
# provider 的开关仍是 False（纯文本的 doubao-1-5-thinking-pro-* 还在用），
# 靠模型名兜住——换回纯文本型号时这里不命中，自然退回非视觉，不会挂死。
_VISION_MODEL_HINTS = ("vl", "vision", "omni", "mimo", "seed")


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


def get_llm_settings():
    """当前生效的 LLM 设置 = 代码默认值 + 运行期覆盖。

    调用方每次请求都读这个函数；`runtime_settings.json` 改了之后下一轮
    就生效，不需要重启。返回值里的字段名是小写，便于调用方使用。
    """
    try:
        from app.core.config import runtime
        patch = runtime.load()
    except Exception:
        patch = {}

    provider = str(patch.get("LLM_PROVIDER", LLM_PROVIDER) or LLM_PROVIDER)
    provider = provider.strip().lower()
    if provider not in PROVIDERS:
        provider = LLM_PROVIDER

    chain = patch.get("LLM_FALLBACK_CHAIN", LLM_FALLBACK_CHAIN)
    if not isinstance(chain, str):
        chain = LLM_FALLBACK_CHAIN

    def num(key, default, cast, low, high):
        try:
            value = cast(patch.get(key, default))
        except (TypeError, ValueError):
            value = cast(default)
        return max(low, min(high, value))

    return {
        "provider": provider,
        "fallback_chain": chain,
        "request_timeout": num("LLM_REQUEST_TIMEOUT", LLM_REQUEST_TIMEOUT, float, 5, 600),
        "judge_timeout": num("LLM_JUDGE_TIMEOUT", LLM_JUDGE_TIMEOUT, float, 5, 600),
        "fallback_ttl": num("LLM_FALLBACK_TTL", LLM_FALLBACK_TTL, float, 0, 86400),
        "rate_limit_ttl": num("LLM_RATE_LIMIT_TTL", LLM_RATE_LIMIT_TTL, float, 0, 86400),
        "concurrency_ttl": num("LLM_CONCURRENCY_TTL", LLM_CONCURRENCY_TTL, float, 0, 86400),
        "retry_times": num("LLM_RETRY_TIMES", LLM_RETRY_TIMES, int, 1, 10),
        "retry_delay": num("LLM_RETRY_DELAY", LLM_RETRY_DELAY, float, 0, 30),
        "retry_budget": num("LLM_RETRY_BUDGET", LLM_RETRY_BUDGET, float, 0, 600),
    }


_RUNTIME_NUM = {
    "LLM_REQUEST_TIMEOUT": (float, 5, 600),
    "LLM_JUDGE_TIMEOUT": (float, 5, 600),
    "LLM_FALLBACK_TTL": (float, 0, 86400),
    "LLM_RATE_LIMIT_TTL": (float, 0, 86400),
    "LLM_CONCURRENCY_TTL": (float, 0, 86400),
    "LLM_RETRY_TIMES": (int, 1, 10),
    "LLM_RETRY_DELAY": (float, 0, 30),
    "LLM_RETRY_BUDGET": (float, 0, 600),
}
_RUNTIME_KEYS = set(_RUNTIME_NUM) | {"LLM_PROVIDER", "LLM_FALLBACK_CHAIN"}


def validate_llm_patch(patch):
    """校验管理页提交的 LLM 设置；返回 (clean, error)。"""
    if not isinstance(patch, dict):
        return None, "设置必须是 JSON 对象"
    clean = {}
    for key, value in patch.items():
        if key not in _RUNTIME_KEYS:
            return None, "未知设置项：%s" % key
        if value is None:
            clean[key] = None
            continue
        if key == "LLM_PROVIDER":
            pid = str(value).strip().lower()
            if pid and pid not in PROVIDERS:
                return None, "未知 provider：%s" % pid
            clean[key] = pid
            continue
        if key == "LLM_FALLBACK_CHAIN":
            text = str(value).strip()
            if len(text) > 1000:
                return None, "降级链太长"
            for part in text.split(","):
                part = part.strip()
                if not part:
                    continue
                pid, sep, model = part.partition(":")
                pid = pid.strip().lower()
                if pid not in PROVIDERS:
                    return None, "降级链里有未知 provider：%s" % pid
                if sep and not model.strip():
                    return None, "降级链里的模型名不能为空：%s" % part
            clean[key] = text
            continue
        cast, low, high = _RUNTIME_NUM[key]
        try:
            num = cast(value)
        except (TypeError, ValueError):
            return None, "%s 必须是数字" % key
        if num != num or num < low or num > high:
            return None, "%s 需在 %s ~ %s 之间" % (key, low, high)
        clean[key] = num
    return clean, None


def update_llm_settings(patch):
    """校验并写入运行期设置；返回 (effective, error)。"""
    clean, error = validate_llm_patch(patch)
    if error:
        return None, error
    try:
        from app.core.config import runtime
    except Exception:                       # pragma: no cover
        return None, "运行期设置模块不可用"
    if not runtime.save(clean):
        return None, "写入 runtime_settings.json 失败"
    return get_llm_settings(), None


__all__ = [
    name for name in globals()
    if name.isupper() or name in ("provider_vision", "persona_model_of",
                                  "persona_model_ids", "get_llm_settings",
                                  "validate_llm_patch", "update_llm_settings")
]
