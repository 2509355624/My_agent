"""QQ 接入与掉线通知配置。

适配层是独立进程 app/qq_bot.py；看门狗读其中的 WEBUI_PORT 和通知路径。
密钥只从 .env 读（QQ_TOKEN / PUSHPLUS_TOKEN），绝不写进仓库。
"""

import os

from app.core.config.env import _env_bool, _env_list

# ─── QQ 接入（SnowLuma / OneBot 11）─────────────────────
# 适配层是独立进程（app/qq_bot.py），本段配置只在它里面用到；网页端进程
# 仅用到 QQ_ENABLE（决定是否注册 send_qq_message 工具）。

QQ_ENABLE = _env_bool("QQ_ENABLE", False)

# SnowLuma 协议端地址：WS 收事件，HTTP 发消息（WebUI 里要分别启用这两个）
QQ_WS_URL = os.getenv("QQ_WS_URL", "ws://127.0.0.1:3001")
QQ_HTTP_URL = os.getenv("QQ_HTTP_URL", "http://127.0.0.1:3000").rstrip("/")
# OneBot 访问令牌（SnowLuma 网络配置里设的那个）。空表示不鉴权，仅限本机使用
QQ_TOKEN = os.getenv("QQ_TOKEN", "")

# 协议端的 **WebUI 端口**。SnowLuma = 5099。
#
# 这个口只给看门狗用：:3000 只在登录成功后才监听，光看它分不出「进程没起来」和
# 「起来了但登录态失效」。WebUI 口**登录前就监听**，正好把两者分开。
# OneBot 端口固定 3000 / 3001；WebUI 端口默认 5099。这里保留 env 覆盖，便于本机调试。
WEBUI_PORT = int(os.getenv("QQ_WEBUI_PORT", "5099"))

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
# 触发点见 app/notify.py：盯 SnowLuma 的 qrcode.png，一变就推。
NOTIFY_PUSHPLUS_TOKEN = os.getenv("PUSHPLUS_TOKEN", "").strip()
# SnowLuma 需要人工扫码时会重写这个文件，它的修改时间就是掉线时刻。
NOTIFY_QRCODE_PATH = os.getenv("QQ_QRCODE_PATH", "").strip()

# 静默告警：连续这么多小时零收发且探活正常 → 推一条「疑似冻结」。
# 抓的是「冻而不掉」（2026-09-27 事故：协议层活着、消息同步停摆，二维码
# 机制完全无感）。半夜安静群会误报，用户拍板宁可误报。0 = 关闭。
NOTIFY_SILENCE_HOURS = float(os.getenv("NOTIFY_SILENCE_HOURS", "4"))
__all__ = [name for name in globals()
           if name.isupper() and not name.startswith("_")]
