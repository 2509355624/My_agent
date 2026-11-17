"""
配置加载
从 .env 文件读取环境变量，提供全局配置项
"""

import os

from app.core.config.env import _env_bool, _env_list
from app.core.config.paths import (
    AGENTS_DIR,
    BASE_DIR,
    DATA_DIR,
    DOCUMENTS_DIR,
    ROOT_DIR,
    SKILLS_DIR,
    VECTOR_STORE_DIR,
    WEB_DIR,
    _is_test_process,
    state_path,
)

from app.core.config.llm import *  # noqa: F401,F403
from app.core.config.image import *  # noqa: F401,F403
from app.core.config.vision import *  # noqa: F401,F403
from app.core.config.rag import *  # noqa: F401,F403
from app.core.config.web import *  # noqa: F401,F403
from app.core.config.web import _parse_gallery_roots  # noqa: F401
from app.core.config.card import *  # noqa: F401,F403

# ─── Web 搜索（豆包搜索）─────────────────────────────
# 豆包搜索（原 联网搜索/融合信息搜索）专用 API Key，在火山「联网搜索控制台」创建：
# https://console.volcengine.com/search-infinity/api-key?tab=post_paid
# 注意：与方舟 ARK 的 VOLC_API_KEY 不同，需单独开通。未配置时 web_search 自动回退 DuckDuckGo。
SEARCH_API_KEY = os.getenv("SEARCH_API_KEY", "")
DOUBAO_SEARCH_ENDPOINT = os.getenv("DOUBAO_SEARCH_ENDPOINT",
                                   "https://open.feedcoopapi.com/search_api/web_search")


# ─── Agent ──────────────────────────────────────────

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
    # DeepSeek 官方（2026-10-09 实测 GET /models）：
    #   deepseek-flash (V4.1-Flash) context_window = 1,048,576，max_output 393,216
    # 云端本来就是「不列 = 不封顶」，这里列出来**只为给「把预算开到最大」的
    # agent（`agents/writing`）留出 30% 余量**：不列的话预算 100 万就等于物理
    # 窗口 104.8 万，压缩闸门形同虚设（真撞墙才想起已经超了）。对预算 3 万的
    # agent 是彻底的 no-op（min(30000, 734003) 还是 30000）。
    "deepseek": int(os.getenv("DEEPSEEK_CONTEXT_WINDOW", "1048576")),
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

# ─── 路径 ───────────────────────────────────────────
# 已搬到 app/core/config/paths.py；这里只保留引用。


# 多 agent：每个 agent 一个自包含目录（agents/<id>/ 里放 agent.json 配置、
# prompt.md 人设、session.jsonl 会话）。会话文件路径由 app/agents.py 解析，
# 不再有「全局单会话文件」这种东西。
DEFAULT_AGENT_ID = os.getenv("DEFAULT_AGENT_ID", "main")


# ─── QQ 接入（NapCat / OneBot 11）─────────────────────
# 适配层是独立进程（app/qq_bot.py），本段配置只在它里面用到；网页端进程
# 仅用到 QQ_ENABLE（决定是否注册 send_qq_message 工具）。

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

# QQ 机器人长回复从这里起改发为一张聊天记录卡片，避免群聊/私聊刷屏。
# 200 字以内仍作为一条普通消息发送；这里按字符数计算纯文本正文。
QQ_LONG_REPLY_CARD_CHARS = int(os.getenv("QQ_LONG_REPLY_CARD_CHARS", "200"))

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

# 群聊短句分条（qq_api.split_chat）：群里真人一条 5~15 字，一条 300 字的
# 回复在群里就是「小作文」，拆成两三句发才像有人在说话。私聊不分。
#   TRIGGER = 短于这个字数且只有一行 → 原样发，拆了反而碎
#   CHARS   = 攒句时每条约多少字（不是硬上限，是攒到超了才断）
#   MAX_PARTS = 一条回复最多拆几条，超出的并进最后一条，绝不刷屏
QQ_CHAT_SPLIT_TRIGGER = int(os.getenv("QQ_CHAT_SPLIT_TRIGGER", "25"))
QQ_CHAT_SPLIT_CHARS = int(os.getenv("QQ_CHAT_SPLIT_CHARS", "18"))
QQ_CHAT_SPLIT_MAX_PARTS = int(os.getenv("QQ_CHAT_SPLIT_MAX_PARTS", "3"))

# 任务回执尾巴带不带「跑图台 / 角色扮演 / 画廊」的入口链接（2026-11-13 用户要求）。
#
# 用户原话：「可以做到每次任务回执给跑图台的最新链接吗？……反正回执是给到消息
# 卡片的，这样大家其实能够直接从回执去进到跑图台的链接」「角色扮演的链接也给吧，
# 画廊也给」。回执本来就是合并转发的卡片，收起来一行，链接藏在里面不占版面。
#
# **必须在回执构造时现读 `friend_site.url()`，绝不能写进 `receipt_note`**：
# Quick Tunnel 每次重启都换域名，写死在设置文本里的地址第二天就是 1033。
# 这条开关只用来一键关掉（隧道没开时也会自动不出链接，见 `_receipt_links`）。
QQ_RECEIPT_LINKS = os.getenv("QQ_RECEIPT_LINKS", "1") not in ("0", "false", "False")

# 回执里跑图台令牌的有效期（小时）。`/跑图台` 指令给的是 6 小时（对齐一次
# 闲聊的时长）；回执是留在聊天记录里给人回头点的，给一天，但不能更长——
# 隧道重启后旧域名本来就打不开了，令牌活再久也没有意义。
QQ_RECEIPT_LINK_TTL_HOURS = int(os.getenv("QQ_RECEIPT_LINK_TTL_HOURS", "24"))

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
# 现在默认开启；`comfy_seen` 闩仍保留，只在 ComfyUI 曾经在线后持续失联时执行恢复。
WATCHDOG_COMFY_ENABLED = os.getenv("WATCHDOG_COMFY_ENABLED", "1") not in ("0", "false", "False", "")


# ─── 自定义 LLM 中转站（2026-10-22）─────────────────────────
# 用户在漫画台自己加的中转站（第一家是 relaycore 的 grok-4.5），存在
# `data/custom_providers.json`，启动时合并进 PROVIDERS。
#
# **为什么合并进 PROVIDERS 而不是另开一张表**：`llm.get_effective_config`、
# `model_catalog._fetch`、`/api/providers`、漫画台的两级下拉全都在读这张
# dict。合并进去，这些地方一行都不用改；另开一张表就得在每一处加分支。
#
# 合并是**幂等**的：先摘掉上一次合并进去的那几个（靠 cfg["custom"] 认），
# 再按文件重新加。否则改完一个渠道调一次 reload，PROVIDERS 里就会堆同名旧值，
# 删掉的渠道也永远留在下拉里。
CUSTOM_PROVIDERS_FILE = os.path.join(DATA_DIR, "custom_providers.json")


def load_custom_providers():
    """把自定义渠道合并进 PROVIDERS，返回合并了几个。

    这个函数在模块末尾调用一次；网页上增删改之后由 `app/main.py` 再调，
    所以不重启进程也能让新渠道立刻可用。
    """
    from app import provider_store

    for pid in [k for k, v in PROVIDERS.items() if (v or {}).get("custom")]:
        PROVIDERS.pop(pid, None)

    items = provider_store.load(CUSTOM_PROVIDERS_FILE)
    n = 0
    for pid, cfg in items.items():
        if pid in PROVIDERS:      # 内置 id 优先，绝不覆盖
            continue
        PROVIDERS[pid] = cfg
        n += 1
    return n


# 启动即合并一次。出错只吞掉——自定义渠道是加分项，配置坏了不该让整个进程
# 起不来（它是被 QQ 机器人、网页端、漫画台共同 import 的）。
try:
    CUSTOM_PROVIDER_COUNT = load_custom_providers()
except Exception:                                    # pragma: no cover
    import logging as _logging
    _logging.getLogger("config").warning("自定义渠道加载失败（忽略）", exc_info=True)
    CUSTOM_PROVIDER_COUNT = 0
