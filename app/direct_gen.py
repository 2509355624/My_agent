#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""直达生图管道（2026-10-04，10-05 两次扩版）。

动机：agent 路径一轮动辄几万 token（系统头 + 历史 + 工具协议 + 多轮循环），
而「@我 画一只猫」这种请求本质只需要一次轻量转译。这条管道用**一次 LLM 调用**
把请求转成 {渠道, 提示词}，代码直接入队——整轮不过 agent。

⚠️ 2026-10-05 晚用户拍板（原话）：「**ai 必须参与决策，绝对不能绕过 ai**！
不要代码层面去做各种解析适配，我现在的 token 完全可以做到 ai 直接判断，
匹配关键词 nai，然后把 nai 需求和用户要求需要调用 nai 告诉 ai 即可」。
据此的硬约束：
- **提示词一律由 AI 产出**。代码里不存在「英文提示词原样入队」「引用抽英文段
  直通」「成品串原样透传」这类零转译旁路——它们既是绕过 AI，也是正文被正则
  改坏的现场（`1girl, soft lighting` 被判成 anima_soft，soft 还被删掉）。
- **代码只做关键词匹配**：认出开头的渠道/档位/画风词 → 映射成一个渠道 id →
  连用户原文一起交给 AI（`_translate`）。正文一个字不碰。

结构（2026-10-05 23:xx 三改：**删掉「命中触发词就回菜单」的自动兜底**）：
- 用户**明确**要菜单（「菜单」「/菜单」「help」）→ 写死常量，零 LLM。
  **其余一切纯文本轮一律交给 AI 判意图**——要画图就出 prompt 入队，是聊天/
  提问就把 `reply` 发出去。没有菜单兜底了（用户原话：「AI 识别用户的意图…
  不要搞这个菜单触发了！别人艾特大大怪，大大怪给他一个回复」）。
- 渠道词（「三档,glss,初音未来」「默认初音未来」「NAI，…，三档」）→ **代码
  正则先抽渠道**，**优先级 = 位置**（用户拍板：谁靠前谁赢，不按类型排序）；
  画风词打错/没打 → 该档默认 clear。抽出来的渠道作为 `chan_hint` 明写给 AI。
- 纯文本轮（裸 @ + 描述 / 画图动词 / 聊天 / 提问 / 私聊裸英文）→ 一次
  `_translate`（AI 判渠道 + 判意图 + 写提示词，或直接回话）。
- 引用正文 + 只打渠道词（「大大怪 三档」引用一条带提示词的消息）→ 引用
  正文当描述，走锁定渠道扩写（引用正文原样交给 AI，不再抽英文段）。
- 带 `::` 权号的**成品串** → 也过一次 AI，权号/画师串规则写在唯一模板里；
  渠道点名词优先，两边都判不出 → 回问，绝不静默落默认档。
- 引用生图回执 +「再来一张 / 重跑一版」→ **交给 AI 判**（2026-10-17 删掉
  关键词早退）：账本里那张图的真提示词 + 会话最近一次生图当**事实**写进模板，
  由模型决定原样重跑还是顺手改一处。
- 引用图 + @（没别的说）→ **反推提示词**发回去，不生成；引用图 + 档位
  （「三档」，或「快档 基于图片帮我生成」这类空话）→ 反推后直接生成；
  引用图 +「帮我生成这个 / 跑一下这张图」（没渠道）→ 默认档反推后重画。
- 引用图 +「反推提示词」（`_REVERSE_RE`，零 AI，2026-10-07 用户拍板）→
  **识图模型用自己那份提示词（`_TAGS_PROMPT`）出英文混合式提示词、原样发**，
  不经过主人设、不经过改图管道。这是"识图先出词、文本模型只负责递"的落点。
  （形态 2026-10-26 从「纯 tag」放开到「标签 + 短语」，理由见 `_TAGS_PROMPT`。）
- 引用图 + 意见 → 改图，**一律真识图**（2026-10-05 用户拍板：账本里存的是
  当时那句提示词，和画面实际内容可能已经对不上——背景改透明那次没看图，
  出图就不是用户要的）。一次带图调用，账本提示词只当「最可信旁证」写进
  prompt，不再作唯一事实来源。**垫不垫图由模型判**（2026-10-06 用户拍板，
  原话：「什么出现图生图、垫图、改图、重绘，这个硬编码给我去掉就行了，改成
  让 AI 它自己去判断」）：代码不再扫原话认机制词，模型输出的
  `source_image` 字段决定这一轮入队垫哪张图。**引用图本身不要求垫图**
  （2026-10-11 口径：明说「图生图 / 照着画 / 复刻」这类才垫；只是提需求 =
  改词重画），文案口径写死在 `_REVISE_TEMPLATE` 的【图生图】段。
  模型判「不是修改请求」
  （夸奖、闲聊）时**照旧把反推发回**（2026-10-07 改：原先关键词轮静默吞轮，
  会把一段已生成好的反推整段丢掉——群 1103174141 实录）。引用图 + @ / 档位 /
  「生成这个」仍优先用账本提示词（零调用）。
- 引用消息但没渠道词（「生图」「这词什么意思」）→ 也交给 AI：引用正文和
  用户原话合并成一条描述喂 `_translate`，AI 自己决定是照着画还是答话。
- **agent 已退场（2026-10-05 用户拍板）**：@ 轮要么走工具要么回话。
  唯一放行（decide 返回 None）是总开关关闭（ENABLED=False）、主动接话轮
  （voluntary）、裸 @/空消息、以及转译调用失败。

⚠️ **2026-10-05 23:xx 用户再次拍板，转译模板合并成唯一一条**（原话）：
「提示词的构成就是：人设是什么？你是一个绘图 AI；第二段就是这是你的工具列表，
500~600 字，简单写清楚它有什么工具；第三段是用户的历史对话；最后一个就是最近
的用户需求，就这么简单。」「我一直想通过硬编码指令的方式去调用生图工具，但我
错了——AI 本身就能胜任整个工作，我们本末倒置了。」

据此：`_TRANSLATE_TEMPLATE` / `_LOCKED_TEMPLATE` / `_WEIGHTED_LOCKED_TEMPLATE`
/ `_WEIGHTED_JUDGE_TEMPLATE` 四条模板**全部删除**，只剩 `_MASTER_TEMPLATE`
（人设 + 两个工具 + 渠道清单 + 历史 + 用户原话）。**不再按渠道注入专属规则。**
`_translate` 现在返回三态：`{skill,prompt}` 生图 / `{reply}` 聊天 / `None` 失败。
"""
import difflib
import json
import logging
import os
import re

from app import image_jobs, image_log, llm, qq_api, random_tags
from app.config import IMAGE_DEFAULT_SKILL, QQ_GROUP_KEYWORDS
from app.confirm_gate import _ATTRIBUTION_RE
from app.skills import list_skills, load_skill

log = logging.getLogger(__name__)

# 总开关（.env DIRECT_GEN=0 可关）：管道只影响「@ + 画图动词」的轮，关掉后
# 全部落回 agent。测试里用 mock.patch.object(direct_gen, "ENABLED", False)
# 关——不然跑真实 _run_turn 的用例会截胡，甚至真调转译 API。
ENABLED = os.getenv("DIRECT_GEN", "1") == "1"

# 标签库前置搜索（.env DIRECT_SEARCH=0 可关）。
#
# 开：转译之前先让 search_agent 查一遍标签库，把「角色名 → 真实 tag」的候选
#     资料塞进模板。解决两个问题：**角色不对**、**提示词不符合要求**。
# 关：完全走老路，一个字都不多花。
#
# 关掉它的场景：搜索 Agent 出问题要紧急熔断、或者想对比「有没有搜索」的
# 效果差。关掉后 _translate 的模板里那一格是空的，模型按自己的判断写。
# 成本（实测）：搜索 Agent 约 1,254 token/张（2 轮），资料本身约 350 token
# 进转译调用。500 张/天合计约 80 万，占 600 万文本额度的 13%。
SEARCH_ENABLED = os.getenv("DIRECT_SEARCH", "1") == "1"

# 搜索 agent 的 **LLM 补漏层**（2026-10-23 用户关掉）。
#
# ⚠️ 这个和上面的 SEARCH_ENABLED 是**两件事**，别混：
#   - `search_tags.extract()`：代码扫 32.8 万条标签库，**0 token、微秒级、
#     不可能编造**。字面出现的词直接定成 tag（连衣裙→dress）。这一层留着的
#     好处是白拿的，关它省不到一分钱。
#   - `search_agent.search()`：1~3 次 LLM 调用，只补 extract 补不到的
#     （同义词、歧义角色、库外名词）。**花钱和变慢的都是这一层。**
#
# 用户口径：「效果提升一般，而且还费钱，我们直接一次 api 请求即可」。
# 关掉后生图链路 = extract（免费）→ 生图 API（1 次），不再多跑 LLM。
# 2026-10-23 实账：搜索 20 天 ¥5.91（占总盘 12%），其中 **64% 的调用结局是
# 「判定无需查库」**——extract 已经查完，模型没东西可补，白花一次调用。
# ⚠️ 默认值就是 **"0"（关）**，不是 "1"：`.env` 是本地文件、不进仓库，写成默认 1
#    的话换台机器/重装一次 `.env` 丢了，这笔钱会**静默长回来**。要补漏再显式写
#    `DIRECT_SEARCH_AGENT=1`。
SEARCH_AGENT_ON = os.getenv("DIRECT_SEARCH_AGENT", "0") == "1"
_SEARCH_OFF_LOGGED = False          # 关闭态只提醒一次，别每张图刷一行

# ─── 渠道清单 ────────────────────────────────────────────
# hd 渠道是「档位_画风」的组合命名（skills/ 下真实存在）；其余是固定渠道名。
# 别让这个集合和 skills/ 目录漂移：_allowed_skills() 每次现场核对目录，
# 对不上的（被屏蔽/新增）以目录为准加减。
# 2026-10-07 用户拍板：anima 族的**快档 / 最小档 / 二档全取消，只剩三档**
# （`hd_3_<画风>`，末尾带 2x 像素放大 → 3072×4608）。原话：「取消 anima 的快档、
# 普通、二档，只留一个三档，到时候就是你说 anima，就跑三档加上 x2 像素」。
# 12 个旧目录**原地保留、只是被屏蔽**（`app/skills.ARCHIVED_SKILLS` →
# `list_skills()` 不再吐它们，但 `load_skill` 照样能加载，i2i 重绘骨架要用）。
# `_allowed_skills()` 现场扫目录后自然只剩这 4 个。
_HD_TIERS = ("3",)
_HD_STYLES = ("clear", "curvy", "gloss", "soft")
_FIXED_SKILLS = ("image_gen_v1", "image_gen_v1_hires", "nffa",
                 "cunny", "miao", "qwen_image_v1", *image_jobs.NAI_SKILLS)
# 默认单图渠道（与 generate_image.py 里那个 T2I 默认常量同一个值）：**既是**
# 「用户没点名渠道」时的默认出图方式（判据在 `_translate` 末尾），**也是**单张
# 那几条路的落点——改图（`_revise` 判不出渠道）、随机口令、`/直通`、还有
# 「默认」这个老档位词。
#
# 2026-10-17 用户拍板翻回来：「帮我把 silver 作为默认渠道吧，现在这个 anima多图
# 效果太差了」——2026-10-16 那天曾把默认换成 anima多图 3 张，实测画质不行。
# ⚠️ anima多图 没下架，原话点名仍走它的专路（`_ANIMA_MULTI_RE`）。
# ⚠️ 改默认渠道**必须同时改模板**：`_MASTER_TEMPLATE` / `_REVISE_TEMPLATE` 里
# 只要还写着「默认渠道 = 某某」，模型就会**主动填 skill**，`if not skill` 那条
# 默认分支永远走不到，代码改了等于没改。
#
# silver / jank 不进 `_FIXED_SKILLS`，由 `_allowed_skills()` 现场扫 skills/ 目录放行。
_FALLBACK_SKILL = IMAGE_DEFAULT_SKILL

# 图生图（模型填了 `source_image: 1`）却判不出渠道时的落点（2026-10-17）。
# 用户 10-06 的口径：「Anima 的图生图没有 Qwen 好用，图生图只需要一个 Qwen
# 的途径」。只给**垫图**用；不垫图（改词重画）仍落 `_FALLBACK_SKILL`。
# ⚠️ 同样受上面那条约束：模板 ① 里写着「没有原渠道就填 qwen_image_v1」，
# 模型通常自己会填，这条兜底只管它漏填。
_I2I_FALLBACK_SKILL = "qwen_image_v1"


def _allowed_skills():
    allowed = set(_FIXED_SKILLS)
    allowed.update("hd_%s_%s" % (t, s) for t in _HD_TIERS for s in _HD_STYLES)
    # 以 skills/ 目录实况校正：目录里没有了就剔除（防屏蔽后照发）。
    # ⚠️ NAI 是**虚拟渠道**（image_jobs 云分支，skills/ 下没有目录），
    # 不参与目录核对，否则永远被误杀。
    try:
        present = set(list_skills())
        # 用户自定义的生图渠道（silver / jank 这类）落进 skills/ 就是可用渠道，
        # 不用再写死进 _FIXED_SKILLS——否则 AI 即使被告知这两个渠道、传了
        # skill=silver，也会被下面这层（out_skill not in _allowed_skills）静默
        # 降级成默认档，等于白告诉 AI（2026-10-06 实录：silver 和泉纱雾 被画成
        # hd_3_clear）。只收「磁盘上存在且确为 生图 类」的目录，避免把写作类
        # skill 当生图渠道放行。
        for sk in (present - allowed - set(image_jobs.NAI_SKILLS)):
            sd = load_skill(sk)
            if sd and sd.get("kind") == "生图":
                allowed.add(sk)
        # 仍以磁盘实况收口：写死名单里目录已被删的渠道剔除（防点名已屏蔽渠道
        # 还去 enqueue 报「找不到 workflow」）。NAI 是虚拟云端渠道，不参与核对。
        allowed &= present | set(image_jobs.NAI_SKILLS)
    except Exception:
        log.exception("list_skills 失败，渠道校验跳过目录核对")
    return allowed


_MENU_RE = re.compile(r"^\s*[\/／!！]?\s*(菜单|帮助|帮助菜单|help|指令)\s*$",
                      re.I)
# 详细使用指南：带斜杠的「/菜单」或「使用指南」。裸「菜单」仍是短菜单——
# 2026-10-05 用户拍板：日常场景短菜单就够，详细版给主动要的人。
_GUIDE_RE = re.compile(
    r"^\s*(?:[\/／]\s*菜单|使用指南|详细指南|详细使用指南|使用说明|帮助指南)\s*$",
    re.I)
# 生图意图粗判（画图动词）。「生成」后面 0~4 字内接「图」才算，避免
# 「生成一下总结」误中。
_INTENT_RE = re.compile(r"画|绘|来张|来一张|来幅|生成.{0,4}图|图.{0,2}一[张幅]")
# ⚠️ 「再来一张 / 重跑 / 换种子」这条**关键词早退已删**（2026-10-17 用户
# 拍板「让 AI 自己去判断如何做」）。原来这里有个 `_AGAIN_RE`，命中就零调用
# 直出上次的提示词，两处（`_revise` 开头、`decide` 的渠道词之前）都在用。
# 别再加回来：它替用户决定「这是重跑、不是改」，而且判据依赖进程内
# `_LAST_JOB`（bot 重启即空）。现在由 AI 判，事实由账本 + `{recent}` 提供。
# 引用正文里的噪音：菜单和生图回执不能当提示词用。
_NOISE_QUOTE_RE = re.compile(r"HT-\d{8}|任务已提交|生图完成")
_RECENT_NOISE_RE = re.compile(r"^🎨|任务已提交|生图完成|HT-\d{8}")
# 自家**出图记账行**（`[直达生图] <渠道>：<prompt>`，落在会话历史里的那一条）。
# 2026-10-16 用户拍板：给转译调用的【最近对话】**AI 侧只留这一种行**，其余
# 一律不喂——包括 `[直达漫画] N 格：<中文剧情>` 和**它自己写的任何回话**。
# 理由（实测七组对照，deepseek-flash，色情向需求）：
#   空历史 2/2 出图；最近对话里带着它自己的拒绝行 → 6/6 全拒；
#   把拒绝行剔掉 → 2/2 立刻恢复出图。
# 也就是说，拒答的**触发源是「它看见自己上一轮拒了」**，跟模板里怎么写、
# 有没有「以用户为中心」都无关（加那段声明重跑 4 次，照拒）。拒绝话术是
# 开放集合，拿词表去认它盖不全——所以判据用结构：AI 侧的纯文本回话一律不是
# 出图记录，一律不喂。顺带修掉 `[直达漫画]` 那条：它的中文 brief 会被逐字
# 抄成下一轮的 brief（22:02 私聊「krea2 鬼针草，横屏」→ 抄成 silver 漫画）。
# ⚠️ `[直达生图]` **绝不能滤**：指代（「换成白裙子」这类）全靠它。
_RECENT_AI_KEEP_RE = re.compile(r"^\[直达生图\]")
# 引用块里的**占位符**：图片取不到 url（"​[图片]"）、下载失败（"（图片）"）、
# get_msg 拉不到被引消息（"[引用的消息无法读取]"——引用机器人自己发的回执
# 就是这种）。2026-10-05 02:59 实录：占位符被当提示词喂进转译，9B 把最近
# 对话里的旧 tag 原样抄出来，生成与引用毫无关系。一律当「没有引用」处理。
_PLACEHOLDER_QUOTE_RE = re.compile(
    r"^\[[^\[\]]{0,14}(?:图片|表情|无法读取)[^\[\]]{0,14}\]$"
    r"|^（(?:图片|表情)）$"
    r"|（没有可读内容）|无法读取")
# **自己发的**纯图消息：正文只剩「[图片]」占位符（qq_api 把图片 CQ 转译成
# 这个），一张或多张连排、可有空白。2026-10-05 用户拍板：只发图、没给任何
# 生图指令 = **绝不跑图**——归一成空文本走「空文本+图 → 反推」分支。
# 16:33 私聊实录：占位符被当成真话掉进 _revise 改图管道，识图完直接入队
# 生图了。真实意见（「改成猫」「三档」）不受影响——占位符后面有字就不归一。
_IMG_ONLY_RE = re.compile(r"^\s*(?:\[图片\]\s*)+$")

# ─── 渠道解析（代码直判，LLM 不再碰渠道） ─────────────────
# 档位是最核心的关键词；画风词是可选项。用户口径（2026-10-05）：档位打了、
# 画风词打错或没打 → 按该档默认画风 clear。
# 2026-10-07 只剩三档，所以**所有档位词一律映射到 "3"**：用户还在说「二档 /
# 快档 / 一档」时按用户口径「当没说过、照跑三档」。词照旧从正文里抠掉，
# 不许漏进提示词。
# `anima` 也当「档位词」用——用户原话「你说 anima，就跑三档加上 x2 像素」，
# 它与「三档」完全等价，后面可跟画风词（`anima soft` → hd_3_soft）。
# ⚠️ 它是这里面**唯一**的 ASCII 词，必须带边界：danbooru 标签和模型名里到处
# 是下划线连词（`miaomiaoRealskin_anima13`），不挡就会从中间误命中。
_TIER_MAP = {"三档": "3", "3档": "3", "二档": "3", "2档": "3",
             "快档": "3", "一档": "3", "1档": "3", "anima": "3"}
_TIER_RE = re.compile(r"(三档|二档|一档|快档|[123]档|默认"
                      r"|(?<![0-9A-Za-z_])anima(?![0-9A-Za-z_]))", re.I)
# 出图方向词（2026-10-07 加）：**指令词，不是画面内容**。在这里从「给 AI 看的
# 正文」里抠掉，免得模型把它当描述写进提示词（中文写进英文 tag 串就是垃圾）。
# ⚠️ 判据**不在这儿**——`image_jobs.turn_is_landscape()` 读的是本轮**原话**
#    （`qq_api.current_turn_text`），所以抠掉不影响横屏生效。
# 只认这三个明确的词；「横向」「横的」不认——它们太容易出现在正常描述里
# （「横向构图」「横的条纹」），认了就是误伤。
_LANDSCAPE_STRIP_RE = re.compile(r"[，,、:：]?\s*(?:横屏|横版|横图)\s*[，,、:：]?")
# 英文画风词的前后不能是字母或下划线：「一档curvy」连写也要认（CJK 后面 \b
# 不成立，2026-10-05 踩过），但「glossy」这种词中片段不能算。⚠️ 下划线也必须
# 挡住——10-05 私聊实录：画师串里的 `0.8::soft_focus::` 被认成画风 soft，
# 一条点名 nai 的权重串落到了 anima_soft（danbooru tag 全用下划线连词）。
_STYLE_RE = re.compile(r"(?<![a-z_])(clear|curvy|gloss|soft)(?![a-z_])"
                       r"|清晰|肉感|油亮|柔和|柔软", re.I)
_STYLE_ALIASES = {"clear": "clear", "curvy": "curvy", "gloss": "gloss",
                  "soft": "soft", "清晰": "clear", "肉感": "curvy",
                  "油亮": "gloss", "柔和": "soft", "柔软": "soft"}
# 固定渠道词：用户点名就锁定，不劳 LLM。前后不能是字母数字（防 sdXL、
# 「qwen2」这类词中片段误中），**下划线也必须挡**——danbooru tag 全用下划线
# 连词，不挡的话 `anime_nffa_1`、`artist:okonogi_nai` 里的词会被当成点名词，
# 从中间把提示词剪断（2026-10-05 实测）。
#
# ⚠️ 2026-10-08：**通用 `krea2` 已下架**（`app/skills.ARCHIVED_SKILLS`），
# krea2 系按挂的 LoRA 拆成 8 条，中文长名进这张表。
# ⚠️ **长名必须在 `_FIXED_CHAN_RE` 里排在 `krea2` 前面**（这里已经没有裸 `krea2`
# 了，但同族长短名共存时同理）：正则同起点左优先，短名排前面会把长名吃掉。
# 2026-10-08 实测过：当时 `krea2` 在前，`krea2米山舞 1girl` 被解析成
# `('krea2', '米山舞 1girl')` —— 渠道锁成通用档，还把风格名当正文塞进提示词。
# 名字里允许夹空格（`krea2 米山舞` 也认），`_fixed_chan` 会先把空白去掉再查表。
_FIXED_CHAN_MAP = {"sd": "image_gen_v1",
                   "krea2米山舞": "krea2-yoneyama",
                   "krea2日系": "krea2-rella",
                   "krea2亚洲真人": "krea2-asianmix",
                   "krea2动漫真人": "krea2-anime2real",
                   "krea2真人cos": "krea2-coscandid",
                   "krea2宝可梦": "krea2-pokemon",
                   "krea2柔光": "krea2-softlight",
                   "krea2奶油": "krea2-creamy",
                   "krea2写实": "krea2-real",
                   "krea2通用": "krea2-general",
                   # 2026-10-15 用户加的两条新风格 LoRA（铭音带触发词 meion krea2
                   # style；鬼针草是 LoKr+DoRA，元数据里没有触发词）。
                   "krea2铭音": "krea2-mingyin",
                   "krea2鬼针草": "krea2-guizhencao",
                   # 2026-10-15 用户拍板的混搭：铭音 0.6 打底 + 鬼针草 1.0，
                   # 族里唯一的双 LoRA 工作流（骨架多一个节点 16）。
                   "krea2铭鬼": "krea2-minggui",
                   "qwen": "qwen_image_v1", "qwen-hd": "qwen-hd",
                   "qwen角色设定表": "qwen-charsheet",
                   "qwen修复": "qwen-restore",
                   # 2026-10-16 用户新增：qwen 的高步数副本（只把 steps 提到 30，
                   # 文生图 / 图生图各一支，其余参数照抄 qwen_image_v1）。
                   # 用户原话：「给我上一个 qwen30 步的图生图，到时候我的指令就是，
                   # qwen30步，图生图，需求」。`qwen30` 不带「步」也认——归一化后
                   # （`_fixed_chan` 去空格 + 小写）两种写法都落到这两个 key 上。
                   "qwen30步": "qwen30", "qwen30": "qwen30",
                   "nffa": "nffa", "nai": "nai",
                   "cunny": "cunny", "miao": "miao",
                   # 2026-10-11 新增：anima 的 LoRA 堆叠版（6 条 LoRA + silvermoonmix
                   # turbo 二段）。⚠️ **跟 `hd_3_*` 不是同一族**（那族是 er_sde 20 步
                   # cfg5 / latent 1.5× / 输出 3072×4608；这两条是 euler 10+15 步 cfg1 /
                   # latent 1.2× / 输出 2456×3686），所以不做成第 5、6 个画风。
                   #
                   # ⚠️ **点名词故意不用「柔和」**（id 也不叫 `anima-soft`）。
                   # 2026-10-11 用户拍板从「anima柔和」改名成「anima线条」：「柔和」
                   # 是 `_STYLE_ALIASES` 里活的画风词（柔和→soft = `hd_3_soft`），
                   # 拿它当点名词会和画风词双关——粉丝群里「高光的地方再柔和一点」
                   # 这种日常说法全是「柔和」，大大怪分不清是形容词还是点名。
                   # ⚠️ 改名后**没有留旧别名**：「anima柔和」现在落回 `hd_3_soft`
                   # （= anima 三档 + soft 画风，就是它在改名前的本来含义）。
                   # 「线条」不在画风表、也不在任何渠道名里，光说「线条」什么都不触发。
                   "anima线条": "anima-line",
                   "anima写实": "anima-real",
                   # 2026-10-15 新增：anima 的 7 条 LoRA 混搭版（用户在 ComfyUI 里
                   # 搭的单段配方：鬼针草 2.2/V3.9/v4.0 三代 + 画师风格 4 条，
                   # er_sde 30 步 cfg 4 → 2x Ani4K，输出 2048×3072）。同样**不做成
                   # 画风**——「鬼针草」不在画风表里，但为防以后手滑加进去双关，
                   # 点名词走固定表，跟线条/写实同款待遇。
                   "anima鬼针草": "anima-guizhencao",
                   # 2026-10-23 用户新增：anima 的「肉感」配方（miaomiaoHarem
                   # anima16 底模 + Anima_Turbo LoRA v0.2 + qaq 1.4，euler 8 步
                   # cfg1 → 2x Ani4K，默认 1024×1536 出 2048×3072）。
                   # ⚠️ **点名词必须带 `anima` 前缀**，和线条/写实/鬼针草同款待遇。
                   # 裸「肉感」坚决不登记——它已经是 `_STYLE_ALIASES` 里活的画风词
                   # （肉感 → curvy = `hd_3_curvy`），拿它当点名词会和画风词双关，
                   # 正是 2026-10-11「anima柔和」改名那次踩过的坑。
                   "anima肉感": "anima-flesh",
                   # ⚠️ 渠道 **id 本身**（`anima-line` / `anima-real`）也进固定表。
                   # 不进的后果 2026-10-11 实测过：正则认不出带连字符的 id →
                   # 落到 `_STYLE_RE`，「anima-soft」被 `soft` 抢成 `hd_3_soft`、
                   # 「anima-real」更糟（`real` 被当画风吃掉，正文里留个 `-real`）。
                   # 模型在 JSON 里填 id 那一路本来就有 `_fixed_chan` 兜底，坏的是
                   # **原话里直接打 id** 这条（`_parse_channel` 拿的是原话）。
                   "anima-line": "anima-line",
                   "anima-real": "anima-real",
                   "anima-guizhencao": "anima-guizhencao",
                   "anima-flesh": "anima-flesh",
                   # 2026-10-24 用户新建的**半厚涂测试渠道**：配方逐行照抄 silver，
                   # 只把 `Anima_in_real_update_v2_epoch_20`（元数据标题 `@Ani3rel`，
                   # Anima→写实/3D）从 **1.0 降到 0.55**、`_cowani_epoch_10` 0.8→0.5，
                   # 再往正向插半厚涂风格前缀。起因是用户反馈 silver 厚涂感重、
                   # 对比度和饱和度偏高，要一条对照渠道。
                   # ⚠️ 0.55 是实测平衡点：降到 0.4 连远景结构一起糊掉（发雾），
                   #    这只 LoRA 同时负责锐度，不是只负责「厚」。
                   # ⚠️「半厚涂」**全句可认**（跟宝可梦 / 米山舞同款待遇）：
                   # 它是渠道专名，`_STYLE_ALIASES` 里没有这一条，不会被画风词抢。
                   # ⚠️ 默认渠道仍然是 `silver`，本渠道不点名绝不自动跑。
                   "半厚涂": "semi-thick",
                   "semi-thick": "semi-thick",
                   # 2026-10-24 新建的**完整版底模渠道**（用户起名 `sf`，
                   # 即 silver-full 的缩写）：Civitai 的 SilvermoonMix-Anima-Evolved
                   # v2.3 **INT8 完整版**（非 Turbo），配方见 `skills/sf/`。
                   # 现参数 12+8 步 / cfg 2 / euler / **零 LoRA**
                   # （cfg 3.5+零LoRA 实测会出六趾，2026-10-25 定 cfg 2；
                   # int8 挂 LoRA 慢的旧结论已证伪，见 skills/sf/skill.md）。
                   # ⚠️ **只收中文点名词，ASCII 的 `sf` 故意不进本表**：
                   #    `_lead_typo_channel` 的候选池就是本表的 ASCII 键，而
                   #    短 id 进池子最危险——任何以 s 开头的输入（「s 一张图」）
                   #    都可能被 difflib 判成打错渠道词劫到 sf。裸 id 走
                   #    `_SKILL_ID_RE` 那条路（跟 krea2-coscandid 同款），不经过本表。
                   # ⚠️ 默认渠道仍是 silver，本渠道不点名绝不自动跑。
                   "完整版": "sf",
                   "silver完整版": "sf",
                   # ── 2026-10-16 用户拍板：krea2 系的中文点名词**不带 `krea2`
                   # 前缀也认**（原话「也放开吧，让 ai 自己选个 krea2 的，不然
                   # 老是跑偏」）。私聊实录：`亚洲真人 不穿衣服 高抬腿 腿部特写`
                   # 认不出渠道 → 落默认 anima多图（10 格漫画）。分两档收录，
                   # 理由见下面 `_KREA2_PLAIN_RE`。
                   "亚洲真人": "krea2-asianmix",
                   "动漫真人": "krea2-anime2real",
                   "宝可梦": "krea2-pokemon",
                   "米山舞": "krea2-yoneyama",
                   "铭音": "krea2-mingyin",
                   # ⚠️「鬼针草」两边同名（anima / krea2 各挂一条）。无前缀时
                   # 按用户拍板落 **krea2** 那条；要 anima 的打全名「anima鬼针草」。
                   "鬼针草": "krea2-guizhencao",
                   # 光打「krea2」两个字 → krea2 系最中性的那条（用户拍板）。
                   # ⚠️ 通用 `krea2` 渠道 2026-10-08 已下架（ARCHIVED_SKILLS），
                   # 这里不是把它请回来，是给「没指定具体变体」一个落点。
                   "krea2": "krea2-general",
                   # ↓ 通用词那一档，**只认开头**（见 `_KREA2_PLAIN_RE`）
                   "写实": "krea2-real",
                   "通用": "krea2-general",
                   "奶油": "krea2-creamy",
                   "柔光": "krea2-softlight",
                   # moss（MOSS-Image，本地 5B）：2026-10-28 晚**收敛成一条 25 步**。
                   # 用户原话「改成 25 步长吧，25 步长的效果可以了，不需要 50 步长了…
                   # 我们 moss 以后就一个 25 步长的渠道即可，这个时间消耗是值得的，
                   # 比 8 步长的快速版本好用，8 步长效果很差」。
                   # ⇒ `moss8` / `moss50` / `moss_page` / 4 步 `moss` 全部进
                   #   `app/skills.ARCHIVED_SKILLS`（目录原地保留：`tools/run_moss_*.py`
                   #   那些一次性脚本还按老名字取工作流，历史图也要按老目录显示）。
                   #
                   #   `moss` / `moss4` / `moss8` / `moss50` / `moss25`
                   #   （含「步」「v」后缀）→ 全部落 `skills/moss-qunyou/`
                   #        1024×1536 竖屏 / maliker @0.4 / **25 步** / VAEDecodeTiled，
                   #        热态约 74 秒。步数账见 `app/moss_api.py` 顶部注释。
                   #
                   # ⚠️ 渠道**名字**保留 `moss-qunyou` 不改：漫画台 `/moss`、
                   #    历史任务 `spec.json`、`moss_api.LEGACY_SKILLS` 全按它找图，
                   #    改名要连带迁历史数据，没收益。
                   # ⚠️ 老说法一律**不报错**、全部落到这 25 步渠道——用户不想再看到
                   #    同一模型分出好几个步数档。`moss45` 仍然停用（10-20 就改名了），
                   #    别再往表里加，它只会让老用户以为还有 45 步那一档。
                   "moss": "moss-qunyou", "moss4": "moss-qunyou", "moss4步": "moss-qunyou",
                   "moss8": "moss-qunyou", "moss8步": "moss-qunyou",
                   "moss8v": "moss-qunyou",
                   "moss25": "moss-qunyou", "moss25步": "moss-qunyou",
                   "moss50": "moss-qunyou", "moss50步": "moss-qunyou",
                   # moss 横屏孪生（2026-10-28，`skills/moss-wide/` 1536×1024）：
                   # 照 `nai` / `nai_wide` 的套路，点名才走，裸「moss」仍是竖版。
                   # ⚠️ 中文别名必须整词登记进 map——`_fixed_chan` 是把正则剥下来
                   #    的 token 去空格后查这张表，正则里认了而 map 里没有就会
                   #    把「moss横屏」原样当渠道 id 报未知。
                   "mosswide": "moss-wide",
                   "moss横屏": "moss-wide", "moss横版": "moss-wide",
                   "moss横图": "moss-wide",
                   # 光影双档（2026-10-29，`skills/moss-light1/` / `moss-light2/`）：
                   # 用户在跑图台挑出的两套 LoRA 组合，定成两条独立渠道。
                   # 骨架与竖版逐字节相同（1024×1536 / 25 步 / cfg 3），只有槽位
                   # 组合和固定前缀不同，所以耗时同档。裸「moss」仍是 `moss-qunyou`。
                   # ⚠️ 同 moss-wide：中文别名整词登记，正则认了 map 里没有就会把
                   #    「moss光影1」原样当 id 报未知。
                   "moss光影1": "moss-light1", "moss光影2": "moss-light2",
                   "mosslight1": "moss-light1", "mosslight2": "moss-light2",
                   "日系": "krea2-rella"}
_FIXED_CHAN_RE = re.compile(
    r"(?<![0-9A-Za-z_])"
    r"(anima\s*线条|anima\s*写实|anima\s*鬼针草|anima\s*肉感"
    r"|anima-line|anima-real|anima-guizhencao|anima-flesh"
    # 2026-10-24 半厚涂测试渠道（配方照抄 silver，降写实化 LoRA + 加半厚涂前缀）。
    # 中文名全句可认：它是渠道专名，画风表里没有「半厚涂」这一条。
    # `semi-thick` 整串必须写全 —— 只写 `semi` 会把 `-thick` 落在正文里。
    r"|半厚涂|semi-thick"
    # 2026-10-24 完整版底模渠道（用户起名 `sf`，即 silver-full 缩写）。
    # 「完整版」是独特词，全句可认。`silver\s*完整版` 整串登记的理由：`完整版`
    # 前面是字母，公共 lookbehind `(?<![0-9A-Za-z_])` 会把它挡住，不整串写就落空。
    # `sf` 写在本正则里**安全**——difflib 的 typo 候选池只取
    # `_FIXED_CHAN_MAP` 的 ASCII 键（本正则不是池子），而 `sf` 故意
    # 不进那个表（原因见上面 map 里的长注释）。命中后 `_fixed_chan` 查不到键
    # 会原样返回 token，它本身就是合法渠道 id。
    r"|完整版|silver\s*完整版|sf"
    # 2026-10-26 新增 `rs29`（Realskin 2.9B 底模版 clear，见 `skills/rs29/`）。
    # ⚠️ 必须写进本正则：`_SKILL_ID_RE` 要求 id 里至少有一个 `-` / `_`，
    #    而 `rs29` 是纯字母数字，走不了那条路（`sf` 同理，所以它也在这里）。
    # ⚠️ **不往 `_FIXED_CHAN_MAP` 里加**：那是 difflib typo 候选池的 ASCII 键来源，
    #    短 id 进池子容易被「s 一张图」这类输入劫持（`sf` 的既有教训）。
    #    命中后 `_fixed_chan` 查不到键会原样返回 token，token 本身就是合法渠道 id。
    # ⚠️ 尾巴 `29` 不写 `\s*`：用户会说「rs29」，不会说「rs 29」。
    r"|rs29"
    r"|krea2\s*米山舞|krea2\s*日系|krea2\s*亚洲真人"
    r"|krea2\s*动漫真人|krea2\s*真人\s*cos"
    r"|krea2\s*宝可梦|krea2\s*柔光|krea2\s*奶油"
    r"|krea2\s*写实|krea2\s*通用"
    r"|krea2\s*铭音|krea2\s*鬼针草|krea2\s*铭鬼"
    r"|qwen\s*角色设定表"
    r"|qwen\s*修复"
    # ⚠️ 必须排在下面裸词 `qwen` 前面：`qwen30步` 里的 `qwen` 本来被尾部
    # `(?![0-9A-Za-z_])` 挡住（后面紧跟数字 3），认不出来就会整句交给模型；
    # 显式认了才稳。`步?` 让「qwen30」也命中。
    r"|qwen\s*30\s*步?"
    # 2026-10-16 用户拍板放开的 krea2 系中文点名词（不带 `krea2` 前缀也认，
    # 这批是**独特词**——当正文形容词用几乎不可能，所以全句可认）。
    r"|亚洲真人|动漫真人|宝可梦|米山舞|铭音|鬼针草"
    # 光打「krea2」两个字 → 落 krea2-general 兜底（用户拍板）。
    # ⚠️ 两条约束都不能省：
    #   ① 必须排在上面所有 `krea2\s*xxx` 长名**之后**——正则同起点左优先，
    #      短名在前会把长名吃掉（2026-10-08 实测 `krea2米山舞` 被吃成 `krea2`）；
    #   ② 尾部要**额外挡住 `-`**：公共尾巴 `(?![0-9A-Za-z_])` 对连字符放行，
    #      不挡的话机器人回执里那句 `krea2-coscandid：…` 会被吃成 `krea2`
    #      （渠道锁成 general），刚修好的「id 直接点名」那条路又坏掉。
    r"|krea2(?![0-9A-Za-z_-])"
    # moss（MOSS-Image，本地 5B）：英文单词 id，但中文用户的「moss」不会拿去当
    # 画面内容（苔藓一般写「青苔」），整词可认。
    # ⚠️ 2026-10-28 晚起 **所有 moss 说法都落同一条 25 步渠道**，原语「moss 以后就
    #    一个 25 步长的渠道即可」。这里的多档写法纯粹是**为了别让老说法漏进正文**，
    #    不是真的还有 8/50 步两档（它们已进 `ARCHIVED_SKILLS`）。
    # ⚠️ 长名必须排在裸 `moss` 前：`moss4` / `moss8` / `moss25` / `moss50` 里的数字
    #    本来就被尾部 `(?![0-9A-Za-z_])` 挡住（裸 `moss` 的前瞻看见数字直接失败），
    #    不显式登记就整句交给模型了。
    #    `\s*步?` 末尾那个「步」必须一起吃掉（跟上面 `qwen\s*30\s*步?` 同理）：
    #    用户会写「moss25步 招牌」，不吞掉的话正文里会剩一个孤零零的「步」。
    #    ⚠️ `moss45` 停用（10-20 就改名成 50 步了），这里别加回来。
    #    `moss8v` 是 10-26 那份竖屏实验目录的旧名，留别名只是让「moss8v 跑一张」
    #    不报错，而且必须排在 `moss\s*8\s*步?` 前面把尾巴那个 v 一起吃掉。
    r"|sd|qwen-hd|qwen|nffa|nai|cunny|miao"
    r"|moss\s*50\s*步?|moss\s*25\s*步?|moss\s*4\s*步?"
    r"|moss\s*8\s*v|moss\s*8\s*步?"
    # moss 横屏孪生（2026-10-28）：必须排在裸 `moss` 前，否则「moss横屏」
    # 只吃掉「moss」、正文剩「横屏」两个字。
    r"|moss\s*(?:wide|横屏|横版|横图)"
    # 光影双档（2026-10-29）：同样排在裸 `moss` 前，「moss光影1」才能整词吃掉；
    # 只写 `moss` 会把「光影1」当正文喂进提示词。`[-\s]*` 同时认 `moss-light1`
    # 与「moss 光影 1」——不给连字符的话 id 原名会被裸 `moss` 先吃掉。
    r"|moss[-\s]*(?:light|光影)\s*[12]"
    r"|moss)"
    r"(?![0-9A-Za-z_])",
    re.I)

# krea2 系里**通用词**那几条的中文点名词（2026-10-16 用户拍板放开）。
# ⚠️ 跟上面那批「独特词」分开收录，因为独特性差得远：这几个在日常语境里本来
# 就是形容词（「写实点的」「奶油色调」「日系写真」），塞进 `_FIXED_CHAN_RE`
# 会在**全句**乱抢（那里句中是 `search`）、还把词从正文里抠掉。
# 所以这里只认**开头**那一次，而且**后面得跟分隔符或行尾**——用户在句首点名
# 才算渠道（`写实 不穿衣服 高抬腿`）；`写实点的猫` / `日系风格的少女` 留在
# 正文里，AI 自己看得懂那是描述。
_KREA2_PLAIN_RE = re.compile(r"(写实|通用|奶油|柔光|日系)(?=[\s,，、。:：]|$)")

# 开头/句中点名的**渠道 id 原名**（带 `-` / `_` 的复合名，如 `krea2-coscandid`）。
# 2026-10-16 私聊实录：用户把机器人回执里那句
# `[直达生图] krea2-coscandid：realistic cosplay photo, 1girl, …` 整行复制回来
# 当指令发（同一句试了三次），三次都判「没点名渠道」→ 走默认 anima多图。
# 病根：`_FIXED_CHAN_RE` 里登记的**全是中文名**（`krea2真人cos` 等），
# 38 个渠道里 **29 个用 id 喊它都认不出**（silver / silver-hd / hd_3_* /
# jank / qwen_image_v1 / krea2-* 全系…）。
# ⚠️ **只收含 `-` / `_` 的复合 id，不收 `silver` / `jank` 这类单词 id**：
#   - 「silver 三档」→ hd_3_clear 是**既有行为**（`LeftmostChannelTest` 钉着）；
#   - `silver hair, 1girl, blue eyes` 这种英文 tag 开头会把 `silver` 认成渠道。
# 复合名在英文 tag 里几乎不出现，再叠「必须命中白名单」这道闸，误判率≈0。
_SKILL_ID_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*[-_][A-Za-z0-9_-]*")


def _fixed_chan(token, text):
    """固定渠道词 → 渠道 id（与「qwen」→ qwen_image_v1 同一类「认渠道名」逻辑）。

    「qwen 超清 / qwen超清」（用户点名 qwen 的 4x 超清版）统一锁定 qwen-hd；
    这是用户明说的渠道名，必须认，不是替 AI 拍板意图（prompt / 垫图与否仍由
    AI 决定）。qwen-hd 也作为字面渠道名直接命中。
    """
    token = re.sub(r"\s+", "", token).lower()
    if token == "qwen" and "超清" in text:
        return "qwen-hd"
    return _FIXED_CHAN_MAP.get(token, token)
# 中文正则在画风判定里当「命令区边界」用，见 _parse_channel。
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
# 开头点名的渠道词前面可能残留的前导噪音（@ 剥完的空格、打错的「：，/」）。
# 只在这儿容错，正文里的标点一律不动。
_LEAD_SEP_RE = re.compile(r"^[\s,，、:：/!！。]+")
# 描述尾巴上的动词残渣：「这个猪 跑 nai」剥掉渠道词后剩「这个猪 跑」。
_DESC_TAIL_RE = re.compile(
    r"\s*(?:帮我|给我)?\s*(?:跑|画一张|生成一张|来一张|来一幅|出一张?图?|生图)\s*$")

# ─── 单次调用封顶（无工具无循环） ─────────────────────────
# agent 循环一轮能滚出十几万 token（while 拉工具结果再续写），单次管道
# 固定一次调用封顶——成本差百倍（2026-10-05 用户拍板）。**聊天和生图现在
# 共用 `_translate` 这一次调用**：不再有独立的私聊问答模板/分支，AI 判
# 「要画图」就出图、判「是聊天」就把 `reply` 发出去。
# 问句尾巴：`_lead_typo_channel` 判「开头那个 ASCII 词是不是打错的渠道词」
# 时用来排除问句（问句不可能是下单）。
_QA_RE = re.compile(r"[?？]\s*$")
# 本轮回复的落史暂存：session_key -> (user_text, reply)。qq_bot 送出回复
# 后 pop 出来落 history，下一轮才有上下文（聊天轮靠它记住 AI 说过什么）。
# 没被 pop 的（排队轮）会在下一个非空 direct_reply 轮被冲掉，不积累。
_QA_LAST = {}

# 引用图轮的反推式识图指令：要**英文 danbooru tag 全量细节**（改图和反推
# 共用）。别用「简洁描述」——2026-10-05 群实录教训：一两百字的简述喂给修正
# 调用，出图跟引用的图毫无关系。
# 2026-10-26 从「只输出纯 Danbooru tag」改成「英文混合式 + 槽位清单」。
# 依据：本机 49 个渠道里 43 个是 Qwen 系编码器（anima 系 Qwen3-0.6B、
# krea2 系 Qwen3-VL-4B、qwen 系 Qwen3-VL-8B），本身是语言模型，读得懂句子；
# 纯 tag 串会把「杯子放在桌子左侧」这类空间关系整条丢掉，而 tag 库里
# **没有**方位关系标签（实测 on table / to the left 都查不到）——只堆 tag
# 结构上就表达不出来。混合式两边都安全：SD 系退化成标签不崩，Qwen 系吃满。
# 顺序照 `skills/ai-prompts/references/models.md` §Anima（主体→服装→动作→
# 场景→构图）与 §Qwen-Image（分区游走、方位短语开头）合起来排。
_TAGS_PROMPT = (
    "你在帮用户反推这张图的生图提示词。这张图马上要被重画，"
    "所以图上每一个看得见的细节都是后续唯一的事实来源，漏掉就补不回来。\n\n"
    "输出**一行英文**，逗号分隔，不换行、不写中文、不解释、不寒暄。"
    "写法是**标签打底 + 英文短语补关系**：\n"
    "属性（人数 / 角色 / 发色发型 / 瞳色 / 表情 / 动作 / 服装 / 画风）用 Danbooru 风格标签；"
    "空间关系、互动、材质细节用英文短语，例如 "
    "`on the left side of the red wooden desk`、"
    "`holding a white mug in her right hand`、"
    "`white ceramic, half filled with tea, steam rising`。\n\n"
    "按这个顺序写全，一项都不要省：\n"
    "人数与角色（认得出就写角色名加作品名）→ 外貌（发色发型、瞳色、体型、妆容）→ "
    "表情与视线（眼睛看哪、嘴角什么状态）→ 动作（身体姿态，加上**左手右手各自在做什么**）→ "
    "**服装分层**（外衣、内搭、内衣、袜鞋，每层写颜色材质，以及它此刻的状态："
    "扣着 / 敞开 / 褪到哪 / 拿在手里）→ **道具**（什么物件、在画面哪一侧、离人多人近、"
    "本身什么材质颜色、里面装了什么）→ 场景（具体地点、家具、墙面地面）→ "
    "镜头（景别、视角、谁在前谁在后）→ 光照（光从哪来、明暗、色温）→ 画风。\n\n"
    "画面里有文字就用英文双引号括起来原样写出，没有就不写。"
    "不要写画质词（best quality / masterpiece 这类），工作流已内置。"
)

# 反推回复的头（2026-10-07 用户拍板改成**一行短头**）：
#   「反推的是：」
#   「1girl, solo, ...」   ← 纯英文 tag，原样，不再附加任何中文描述
# 旧头是一长串「这张图的提示词反推如下，引用本条 + 渠道词（如「三档」）可
# 直接生成：」——用户原话「一行一行的来，不要一大堆字堆在一起」，且要求
# 反推结果**纯英文**（中文描述进提示词是污染，引用时会被一起带走）。
# 改完后整段回复就是「头 + 英文 tag」，干净、可直接复制去生图。
_REVERSE_HEADER = "反推的是："


def _is_english_tags(s):
    """描述是不是本来就是英文提示词（用户贴的最终 prompt，直通零转译）。"""
    s = (s or "").strip()
    return len(s) >= 4 and s.isascii() and any(c.isalpha() for c in s)


def _quote_merge(quoted, desc):
    """「引用正文 + 用户补的话」合并成转译输入（引用+渠道词+额外话路径）。

    引用是噪音/占位符就只用用户自己的话。两样都没有返回 ""。

    2026-10-05 用户拍板：**不再抽英文段**。以前豆包那种「好的，提示词如下：
    …」的包装会先被正则剥出英文本体再直通，那是代码替 AI 做解析；现在引用
    正文原样交给 AI，让它自己决定留哪句（用户原话：「不要代码层面去做各种
    解析适配」）。
    """
    q = (quoted or "").strip()
    if q and not _NOISE_QUOTE_RE.search(q) and not q.startswith("🎨") \
            and not _PLACEHOLDER_QUOTE_RE.search(q):
        return q + ("\n（用户补充：%s）" % desc if desc else "")
    return desc


# 这里原先有两道**代码判图生图**的闸，2026-10-06 按用户要求删掉，一个不留：
#   - `_i2i_intent(text)`：扫原话找机制词（图生图/垫图/改图/重绘/i2i），
#     命中就把这轮路由成垫图轮、还给 qwen 开了「改动指令直通」的零调用旁路；
#   - `_redraw_capable(skill)`：垫图轮只认动漫档，模型填了别的渠道就**静默
#     降回默认档**——「一说图生图就掉回 anima」那个怪事的病根就是这一行。
# 用户原话：「什么出现图生图、垫图、改图、重绘，这个硬编码给我去掉就行了，
# 改成让 AI 它自己去判断」「没有兜底没有问题。因为大模型它自己就知道怎么去做，
# 我们不需要代码去给它硬兜底的」。现在垫不垫图只看模型输出的 `source_image`
# 字段（判据写在 `_REVISE_TEMPLATE` 的文案里），渠道也不再由代码换。


def _lead_commands(text):
    """从开头吃掉一串「档位词 / 画风词 / 分隔符」，返回 (tier, style, 正文)。

    只吃**开头的命令词**，遇到第一个不是命令词的东西就停——后面的全算正文，
    一个字不碰。这是「代码只认渠道关键词、不解析提示词」的落点（2026-10-05
    用户拍板：「ai 必须参与决策，不要代码层面去做各种解析适配」）。

    画风词额外要一道闸：**后面得有中文正文，或者前面已经吃到了档位词**。
    不然 `soft lighting, 1girl` 这种英文 tag 串会被当成「画风 soft」——既抢
    渠道、又把 soft 从正文里删掉（2026-10-05 实测：`1girl, soft lighting,
    blue hair` 被判成 anima_soft，正文烂成 `1girl,   lighting, blue hair`）。
    """
    tier = style = None
    rest = text
    while True:
        rest = _LEAD_SEP_RE.sub("", rest, count=1)
        m = _TIER_RE.match(rest)
        if m and tier is None:
            # `.lower()` 只为 `anima`（`_TIER_RE` 带 re.I，`ANIMA` 也得认）；
            # 中文键不受影响。
            tier = _TIER_MAP.get(m.group(1).lower(), "base")
            rest = rest[m.end():]
            continue
        m = _STYLE_RE.match(rest)
        if m and style is None:
            body = rest[m.end():]
            if tier is not None or _CJK_RE.search(body):
                style = _STYLE_ALIASES.get(m.group(0).lower(),
                                           _STYLE_ALIASES.get(m.group(0)))
                rest = body
                continue
        break
    return tier, style, rest


def _tier_skill(tier, style):
    """档位 + 画风 → 渠道 id。

    2026-10-07 只剩三档，所以不再按档位分支：打了任何档位词（三档 / 二档 /
    快档 / 一档 / anima…）或只打了画风词，**一律落 `hd_3_<画风>`**；画风没打
    或打错就是 `hd_3_clear`。唯一例外是「默认」且没说画风——那是「默认渠道」
    的意思，照旧回 `silver`。
    """
    if tier == "base" and not style:
        return _FALLBACK_SKILL
    return "hd_3_%s" % (style or "clear")


def _fix_typo_style(rest):
    """档位后紧跟的纯 ASCII 短词像是打错的画风词（glss→gloss）就纠回来。

    只纠**档位后第一个**词，且贴得回来才认；贴不回来按档位默认 clear，
    原词留在正文里不丢（「三档,miku,初音未来」→ hd_3_clear + 描述含 miku）。
    返回 (style or None, 纠正后的 rest)。
    """
    segs = [s for s in re.split(r"[\s,，、:：]+", rest) if s]
    cand = segs[0].strip(".。!！?？") if segs else ""
    if not cand or not cand.isascii() or not 3 <= len(cand) <= 8 \
            or _INTENT_RE.search(cand):
        return None, rest
    close = difflib.get_close_matches(cand.lower(), _HD_STYLES, n=1,
                                      cutoff=0.75)
    if not close:
        return None, rest
    return close[0], rest.replace(cand, " ", 1)


def _parse_channel(text):
    """代码直判渠道。返回 `(skill or None, 剩余描述)`。

    代码只干一件事：**认出用户点名的渠道/档位/画风词**，映射成一个渠道 id；
    提示词怎么写全交给 AI（2026-10-05 用户拍板：「ai 必须参与决策，绝对不能
    绕过 ai」）。所以这里**只认命令词**，正文一个字不碰。

    **优先级 = 位置**（2026-10-05 用户拍板原话：「从开头开始匹配，第一个匹配
    到的是谁…谁靠前，谁的优先级最高」）：固定渠道词（nai/sd/qwen/…）和档位词
    （三档/二档/快档/默认）谁先出现谁赢，**不按类型排序**——所以
    「NAI，…，三档」是 nai（NAI 靠前）、「三档 … nai」是 hd_3_clear。
    画风词（clear/curvy/gloss/soft）只在**开头**认：它当画面内容的时候太多
    （`柔和室内光`、`soft lighting`），全文乱搜会抢渠道、还会把那个词从正文
    里删掉。

    **多个渠道词（2026-10-07 用户拍板）**：**严格取从左到右第一个命中的**，
    不做二次上报、不做兜底裁决（「他打 Six 三档，那就走 Six；如果是三档 Six，
    那就走三档——就看哪一个排在第一」）。AI 判不准 → 走默认渠道。

    - 「三档 gloss 初音未来」→ hd_3_gloss / 初音未来
    - 「anima soft 初音」    → hd_3_soft / 初音（`anima` ≡ 三档）
    - 「三档,glss,初音未来」 → hd_3_gloss / 初音未来（glss 贴回 gloss）
    - 「三档 猫」            → hd_3_clear / 猫（画风没打，默认 clear）
    - 「二档 curvy 初音」    → hd_3_curvy / 初音（旧档位词一律落三档）
    - 「默认初音未来」       → silver / 初音未来（「默认」= 默认渠道）
    - 「gloss 一个女孩」     → hd_3_gloss / 一个女孩（只打画风）
    - 「sd 一只猫」          → image_gen_v1 / 一只猫（固定渠道词）
    - 「NAI，…，三档」       → nai / …（谁靠前谁优先）
    - 「这个猪 跑 nai」      → nai / 这个猪（渠道词不限位置）
    - 「nai，少女 柔和光线」  → nai / 少女 柔和光线（**开头**渠道词最高优先）
    - 「1girl, soft lighting」→ (None, 原文)  ← soft 是画面内容，不是画风词
    - 「silver 三档」        → hd_3_clear / silver（silver 认不出，落档位；silver
                                留在正文里，由 AI 判是不是渠道）
    - 「亚洲真人 高抬腿」     → krea2-asianmix / 高抬腿（krea2 裸后缀，全句可认）
    - 「krea2 一只猫」        → krea2-general / 一只猫（裸 krea2 兜底）
    - 「写实 一只猫」         → krea2-real / 一只猫（通用词**只认开头**）
    - 「写实点的猫」          → (None, 原文)  ← 通用词后不跟分隔符时是描述
    - 「krea2-coscandid：…」  → krea2-coscandid / …（id 原名，见 `_SKILL_ID_RE`）
    - 没有任何渠道词         → (None, 原文)
    """
    text = text.strip()
    # 「anima多图」**不是档位词**：`anima` 在 `_TIER_RE` 里 ≡ 三档，不加这道闸
    # 就会被吃成 `hd_3_clear`、把「多图」整条渠道顶掉（2026-10-16 群 1104825153
    # 实录：原话「anima 多图」→ 直达入队 hd_3_clear）。点名它时**不锁渠道**，
    # 交给 `_decide` 的 anima多图 专路（见下面「前置搜索」之前那段）。
    if _ANIMA_MULTI_RE.search(text):
        return None, text
    # ① 写在**开头**的固定渠道词最高优先，档位/画风词一律不许顶掉它
    # （2026-10-05 私聊 2509355624 实录：「nai，真人 Cos 阿米娅…柔和…」里的
    # 「柔和」被当画风，一条点名 NAI 的单落到了 anima_soft。用户原话：「我
    # 写了 nai 了！前缀已经是 nai 了！」）。画风词留在描述里不动——它对 NAI
    # 只是画面内容，不是渠道。
    lead = _LEAD_SEP_RE.sub("", text, count=1)
    hm = _FIXED_CHAN_RE.match(lead)
    if hm:
        skill = _fixed_chan(hm.group(0), text)
        desc = _DESC_TAIL_RE.sub("", lead[hm.end():].strip())
        desc = re.sub(r"^[\s,，、:：]+|[\s，、]+$", "", desc)
        return skill, desc

    # ①-2 krea2 系的**通用词**后缀（写实 / 通用 / 奶油 / 柔光 / 日系）：
    # 同样只认**开头**这一次，且后跟分隔符（2026-10-16 用户拍板放开，理由
    # 见 `_KREA2_PLAIN_RE`）。放在固定渠道词之后，所以「nai 写实 …」这种
    # 前面点了名的一律按那个渠道走；档位词同理（`三档 写实 …` 开头是档位，
    # 这里不匹配，照旧落 `hd_3_clear`）。
    pm = _KREA2_PLAIN_RE.match(lead)
    if pm:
        skill = _fixed_chan(pm.group(1), text)
        desc = _DESC_TAIL_RE.sub("", lead[pm.end():].strip())
        desc = re.sub(r"^[\s,，、。:：]+|[\s，、]+$", "", desc)
        return skill, desc

    tier, style, rest = _lead_commands(text)
    if tier is not None and style is None:
        style, rest = _fix_typo_style(rest)

    if tier is None and style is None:
        # ② 开头没有命令词 → 固定渠道词和档位词都可能在句子中间
        # （「这个猪 跑 nai」「画个女孩 三档」），**取位置最靠前的那个**。
        hits = []
        fm = _FIXED_CHAN_RE.search(text)
        if fm:
            hits.append((fm.start(), "fixed", fm))
        # 渠道 id 原名（复合名 + 命中白名单），跟「fixed」同一档：
        # 它同样是「用户点名的渠道」，只是写的是机器人自己印的那个 id。
        # finditer 按位置升序，第一个命中的就是最靠前的那个。
        allowed = set(_allowed_skills())
        for im in _SKILL_ID_RE.finditer(text):
            if im.group(0).lower() in allowed:
                hits.append((im.start(), "fixed", im))
                break
        tm = _TIER_RE.search(text)
        if tm:
            hits.append((tm.start(), "tier", tm))
        if not hits:
            return None, text
        _pos, kind, m = min(hits, key=lambda h: h[0])
        if kind == "fixed":
            skill = _fixed_chan(m.group(0), text)
            desc = text[:m.start()] + " " + text[m.end():]
            desc = _DESC_TAIL_RE.sub("", desc.strip())
            desc = re.sub(r"^[\s,，、:：\-]+|[\s,，、:：\-]+$", "", desc)
            return skill, desc
        # 句子中间的档位词：它后面紧跟的画风词一并认，档位词本身抠掉。
        tier, style, rest = _lead_commands(text[m.start():])
        if style is None:
            style, rest = _fix_typo_style(rest)
        rest = text[:m.start()] + " " + rest

    skill = _tier_skill(tier, style)
    desc = _DESC_TAIL_RE.sub("", rest.strip())
    desc = re.sub(r"^[\s,，、:：]+|[\s,，、:：]+$", "", desc)
    return skill, desc


# ─── 成品提示词轮（NAI 权重串 `::`）与 AI 判渠道 ────────────
#
# 2026-10-05 用户拍板，两条硬要求：
#   ① 他开头写了渠道词（nai）就**必须**走那个渠道——「我要用 nai，大模型
#      为什么不能自己判断」：能判的交给他判，判不出**不许**静默换 anima。
#   ② 带权号的画师串（`1.1::artist:x::`、`-1::_multiple_views::`）是**成品
#      提示词**，权号语法一个字不许动。以前三处转译模板写着「禁止权重语法
#      ::」，结果 19:23 群聊那次渠道对了、权重却被抹平成平铺 tag——对 NAI
#      而言 `::` 是官方语法，那条禁令本来就不该套在它身上。
#
# ⚠️ 2026-10-05 晚用户加码（原话：「全部都要求过 ai，ai 必须参与决策，绝对
#    不能绕过 ai！」）：成品串**不再零转译直通**，照样过一次 AI（见
#    `_translate(..., weighted=True)`）。代码的活儿只剩「认出开头的渠道词」，
#    正文交给 AI；模板里把「权号/画师串/负号/换行一律原样保留」写成硬要求，
#    免得模型把成品串翻译成平铺 tag。
_FINAL_PROMPT_RE = re.compile(r"::")


def _named_channel(text):
    """原文**开头**有没有明确点名的固定渠道词。有 → (skill, 剥掉该词的正文)。

    比 `_parse_channel` 保守，专为成品串服务：
    - 只认固定渠道词，不认档位/画风词（成品串里的 `soft_focus`、`clear sky`
      会被那边当画风误吃）；
    - 点名词必须出现在第一个 `::` **之前**——串正文里出现的 `nai` 是 tag，
      不是指令（边界正则与 `_FIXED_CHAN_RE` 同源，前后连下划线都不许挨着）。
    """
    text = text or ""
    m = _FIXED_CHAN_RE.search(text)
    first_w = text.find("::")
    if not m or (first_w >= 0 and m.start() > first_w):
        return None, text
    skill = _fixed_chan(m.group(0), text)
    # 只抠掉那一个词本身，**其余字节一律不动**（换行、连续空格、结尾逗号都
    # 原样保留——用户要的是「一个字不改」，替他压段落或抹掉标点就是改）。
    # 只清开头那个因抠词留下的孤立分隔符，且不碰 `-`：`-1::tag::` 的负号
    # 是语义（负向权重），吃掉它整段负面提示词就变正面了。
    rest = text[:m.start()] + text[m.end():]
    rest = re.sub(r"^[\s,，、:：]+", "", rest)
    rest = re.sub(r"\s+$", "", rest)
    return skill, rest


def _weighted_channel(text):
    """成品串的渠道判定：返回 (skill or None, 正文)。

    只认**第一个 `::` 之前**的命令词——串正文里出现的 `nai` / `soft_focus`
    是 tag，不是指令（`_named_channel` 认固定渠道词，`_lead_commands` 认
    档位/画风）。正文只剥掉开头那串命令词，其余一个字不动。
    """
    named, body = _named_channel(text)
    if named:
        return named, body
    cut = text.find("::")
    tier, style, _rest = _lead_commands(text[:cut] if cut >= 0 else text)
    if tier is None and style is None:
        return None, text
    return _tier_skill(tier, style), _lead_commands(text)[2]


_LEAD_TOKEN_RE = re.compile(r"^([A-Za-z][A-Za-z0-9_]{1,7})(?![A-Za-z0-9_])")

# 开头的渠道词打错、又同时像两个渠道时的回问（不赌、不静默兜底）。
_LEAD_AMBIGUOUS_TEXT = ("开头那个词我不确定是哪个渠道——你说的可能是 %s。"
                        "把渠道词写清楚再发一次就行（只回一个词也可以）。")


def _lead_typo_channel(text):
    """开头那个**代码认不出**的 ASCII 短词，可能是打错的渠道词。

    2026-10-05 私聊实录：「MAI 正面提示词： 真人 Cosplay 阿米娅…」——MAI 是
    nai 手滑，代码正则认不出就等于没点渠道，整条掉进默认档 anima_soft。
    判据用编辑距离，**不叫模型自由猜**：实测同样这条输入，路由器一次回空串、
    一次回 qwen_image_v1，渠道判定不能靠运气。规则和画风词打错（glss→gloss）
    同源：只贴得回**一个**渠道就当打错直接锁定；几个都像就返回候选让调用方
    回问；一个都不像就当没这回事（普通聊天不白烧任何调用）。
    返回 (skill or None, 去掉该词的正文, 候选渠道词元组)。
    """
    m = _LEAD_TOKEN_RE.match(text)
    if not m:
        return None, text, ()
    token = m.group(1)
    rest = text[m.end():]
    # 只在「开头独立 ASCII 词 + 正文是中文描述」时判：裸英文 tag 的第一个词是
    # 提示词本体（那是直通轮），问句是私聊问答轮，都不该被当成打错的渠道词。
    if (_is_english_tags(text) or _QA_RE.search(rest)
            or not re.search(r"[\u4e00-\u9fff]", rest)):
        return None, text, ()
    # 候选池只放 ASCII 词：krea2 那 8 个中文长名也是渠道词，但输入 token 是
    # `_LEAD_TOKEN_RE` 抠出来的 ASCII 词，拿它去跟中文串比编辑距离只会得到
    # 「krea → krea2米山舞」这种看似命中、实则随机的候选，回问文案也没法看。
    cands = difflib.get_close_matches(
        token.lower(), sorted(k for k in _FIXED_CHAN_MAP if k.isascii()),
        n=3, cutoff=0.6)
    body = re.sub(r"^[\s,，、:：]+", "", _LEAD_SEP_RE.sub("", rest, count=1))
    body = _DESC_TAIL_RE.sub("", body).strip()
    if len(cands) == 1:
        return _FIXED_CHAN_MAP[cands[0]], body, ()
    return None, body, tuple(cands)


# 成品串但两边都判不出渠道时的回问（零入队，绝不静默跑默认档）。
_FINAL_PROMPT_NO_CHAN_TEXT = (
    "这段带权号的提示词我照原样收下了，但这轮没说渠道，我不敢替你选"
    "（选错就是整张图换风味）。在前面补一个渠道词再发："
    "nai（画师串/权号这种写法就是它的）/ silver（单张·最快）/ silver-hd（4x超清）/ "
    "anima（三档）/ sd / qwen / nffa / jank / krea2米山舞"
    "（krea2 系 13 条按风格拆，点名要带风格词）。")


MENU_TEXT = (
    "🎨 三种出图方式\n"
    "@我 + 描述（中英都行）\n"
    "今日老婆 / 随机萝莉 / 随机兽耳 / 随机女仆\n"
    "贴一段英文提示词\n"
    "\n"
    "引用一张图：\n"
    "+「三档」照着画 ｜ 「反推提示词」给纯英文 tag\n"
    "+ 说需求 = 照这张改词重画 ｜ 「qwen 图生图」= 只改那一处\n"
    "\n"
    "📇 造卡：一句话出角色卡（设定 + 封面，合成一张卡片）\n"
    "　渠道 造卡 需求 ｜ 例：anima线条 造卡 一个傲娇的女高中生\n"
    "　渠道可省，省了走 anima-line · 封面按该渠道默认分辨率出，两三分钟\n"
    "　引用那张卡片说「重新生成」→ 只换种子重画封面\n"
    "\n"
    "🀄 moss漫画：一句话出一页四格（编剧写分镜 + 台词）\n"
    "　例：moss漫画 雨夜便利店，两个陌生人共用一把伞\n"
    "　720×1280 竖屏 · 30 步 · 约 90 秒\n"
    "\n"
    "「silver-hd」= 4x 超清出图\n"
    "「/更多渠道」= 档位/画风/其他AI ｜ 「使用指南」= 详细玩法"
)

GUIDE_TEXT = (
    "🎨 使用指南 · 一行一项\n"
    "@我（或喊「大大怪」）+ 渠道 + 描述就能出图。\n"
    "\n"
    "【怎么写】渠道名写在最前面，后面接需求\n"
    "　不写渠道 → silver 单张快出（默认渠道）\n"
    "　silver 水晶城堡 ｜ 单张快出 · 不写渠道也是它\n"
    "　anima 女骑士 ｜ 三档 + 2x 像素放大（3072×4608）\n"
    "　anima gloss 女骑士 ｜ gloss 油亮\n"
    "　三档 clear 水晶城堡 ｜ 跟 anima 等价\n"
    "　anima 1girl, blue hair ｜ 英文照过 AI\n"
    "　只说画风词（clear/soft/gloss/curvy）→ 也是三档\n"
    "\n"
    "【档位】只剩三档（快档 / 最小档 / 二档 2026-10-07 取消）\n"
    "　　　说 anima 或三档都行 · 不写渠道词 = silver 单张\n"
    "\n"
    "【画风】clear 清晰 ｜ curvy 肉感\n"
    "　　　gloss 油亮 ｜ soft 柔和\n"
    "　　　打错或没打 → 按该档默认\n"
    "\n"
    "【点名渠道】把渠道名写最前面就行\n"
    "　anima多图　双模型两段 · 一次 3 张（要明说）\n"
    "　rs29　跟 anima clear 同参数，底模换 realskin 2.9（更新版）\n"
    "　silver-hd　4x超清 · 4928×7360（约28MB）\n"
    "　jank　NoobAI 自定义（有权号词就用它）\n"
    "　anima线条　anima 堆 6 条 LoRA 的版本 · 2456×3686\n"
    "　anima写实　同上，一段底模换成 AnimeReality\n"
    "　anima鬼针草　anima 堆 7 条 LoRA 的混搭 · 2048×3072\n"
    "　qwen　慢·写实·能在图里写中文字\n"
    "　qwen-hd　qwen 的 4x 超清 · 4096×6144（更慢更大，别连刷）\n"
    "　qwen角色设定表　7 分区 character design sheet（2304×1536 / 16 步）\n"
    "　qwen修复　引用图 → 去糊/去噪修清楚（1536 面积档 / 25 步）\n"
    "　sd　　一次多张，多段用 --- 分隔\n"
    "　krea2系　13 条按风格拆：krea2米山舞 / krea2日系 / krea2亚洲真人\n"
    "　　　　　/ krea2动漫真人 / krea2真人cos / krea2宝可梦\n"
    "　　　　　/ krea2柔光 / krea2奶油 / krea2写实 / krea2通用\n"
    "　　　　　/ krea2铭音 / krea2鬼针草 / krea2铭鬼（点名要带风格词）\n"
    "　nffa　　插画感\n"
    "　cunny　超分重渠道 · 单张 2~4 分钟\n"
    "　miao　　皮肤质感滑嫩 · 2x 出 3072×4608\n"
    "　moss　　MOSS-Image · 图里写中英文最准 · **25 步 1024×1536 约 74 秒**\n"
    "　　　　　（就这一条，没有 8/50 步档了；已内置 nai5 二次元风格，\n"
    "　　　　　触发词自动加，提示词里别再写 @//neoanime/）\n"
    "　moss横屏 同上 · **1536×1024 横版**（说「moss横屏/横版/横图」才走，\n"
    "　　　　　裸「moss」永远竖屏；像素数一样，耗时不变）\n"
    "　moss光影1 同骨架的固定 LoRA 光影组合（maliker + 光影 + 米山舞 +\n"
    "　　　　　光润皮肤）· 写实向 · 点名「moss光影1」才走\n"
    "　moss光影2 同上换配方（光影 + 米山舞 + wangzhe）· 光影氛围更重\n"
    "　moss+名字 只挂那一个 LoRA（强度一律 0.8，其余槽全关）· 写法「moss 米山舞 需求」\n"
    "　　　　　可选 fkey / maliker / anireal / ningen / 光影 / 748 / 1999 /\n"
    "　　　　　nai5 / takamichi / 米山舞 / wangzhe / 光润 · 两个用 + 连\n"
    "　nai　　 NovelAI 云端 · 认画师串/权号\n"
    "\n"
    "【引用玩法】\n"
    "　引用提示词 + 只发渠道词 → 照跑\n"
    "　（豆包的客套话整段复制也认，自动抽英文本体）\n"
    "　引用图 +「提取提示词」→ 给当初那串词（零调用）\n"
    "　引用图 +「反推提示词」→ 纯英文 tag 反推（零 AI）\n"
    "　引用图 + 说需求（「把头发改成银色」）→ 改词后同渠道重画\n"
    "　引用图 + 只@我 → 反推\n"
    "　引用图 + 档位 / 「生成这个」→ 反推后生成\n"
    "　引用图 +「qwen 图生图 + 怎么改」→ 只改那一处（1~2 分钟）\n"
    "　引用图 +「qwen修复」→ 把这张图修清楚（不放大）\n"
    "　出图回执 +「再来一张」→ 换种子重跑\n"
    "\n"
    "【造卡】一句话出角色卡：六项设定 + 封面（走渠道默认尺寸）\n"
    "　写法：渠道 造卡 需求（渠道可省，省了走 anima-line）\n"
    "　例：anima线条 造卡 一个傲娇的女高中生 ｜ krea2 造卡 古代女帝\n"
    "　两三分钟回一张聊天记录卡片：封面 + 开场白/简介/说话风格/\n"
    "　背景记忆/补充规则，都是照着星陨AI 创建表单的框写的，\n"
    "　复制粘贴就能建卡\n"
    "　不满意 → 引用那张卡片说「重新生成」：只换种子重画封面，\n"
    "　设定一个字不动、也不再重发一遍\n"
    "　封面过不过审跟普通生图同一个开关\n"
    "\n"
    "【moss漫画】一句话出一页四格，编剧自己写分镜和台词\n"
    "　写法：moss漫画 一句话剧情\n"
    "　例：moss漫画 雨夜便利店，两个陌生人共用一把伞\n"
    "　　moss漫画 清晨，一只胖橘猫跳上还在睡觉的男主人胸口\n"
    "　720×1280 竖屏（抖音 9:16）、30 步、约 90 秒\n"
    "　不用写 tag、不用写画风——它吃自然语言，风格按剧情自己定\n"
    "　先回一句标题 + 四格台词，再出图：理解跑偏了这时候就能喊停\n"
    "\n"
    "【随机口令】今日老婆 ｜ 随机萝莉 / 兽耳 / 女仆\n"
    "\n"
    "提示词用中文描述就行，我来转成画法。"
)

# 「/更多渠道」：把全部跑法一行一项罗列（2026-10-07 用户定稿排版——
# 渠道名英文左列、右边接一句人话解释；只留一组例子；方括号分段。
# QQ 是比例字体，列对齐用全角空格「　」比半角稳）。
#
# 2026-10-21 重排（用户：「/更多渠道 也是写不清楚，一大堆字」）。改了三件事，
# 文本反而更长——**这是有意的**：回执被压到只留 5 个常用渠道（见
# `generate_image._RECEIPT_NOTE_DEFAULT`），原来挂在回执上的玩法必须在这里
# 找得到，否则就从群友眼前彻底消失了。长的代价只落在**主动查的人**身上，
# 而回执是每张图都跟着发、被动刷屏的那一条。
#   ① 常用 5 个（anima / krea2 / qwen / miao / silver）置顶并标 ★，
#      判据是 `state/image_log.jsonl` 6799 张账本的实际频次；
#   ② 补齐回执里删掉的玩法：横屏 / 漫画 / 聊角色 / 造卡，集中进【一句话玩法】；
#   ③ 顺序按「群友实际会问什么」排：默认 → 常用 → 其他渠道 → 一句话玩法 →
#      引用玩法。原来引用玩法夹在渠道中间，查到一半要往上翻。
MORE_CHAN_TEXT = (
    "🧭 全部渠道\n"
    "用法：渠道名 + 需求　例：anima 女骑士 ｜ krea2 日系 银发少女\n"
    "★ 是群里最常用的\n"
    "\n"
    "【★默认】silver\n"
    "【★anima】anima ｜ 画风加 clear / curvy / gloss / soft\n"
    "　　　　　rs29 = 同 clear，底模换 realskin 2.9（更新版）\n"
    "【★krea2】krea2 ｜ 风格加 米山舞 / 日系 / 亚洲真人 / 动漫真人 / 真人cos / 宝可梦\n"
    "　　　　　柔光 / 奶油 / 写实 / 通用 / 铭音 / 鬼针草 / 铭鬼\n"
    "【★qwen】qwen\n"
    "【★miao】miao\n"
    "\n"
    "【其他】moss ｜ moss光影1 ｜ moss光影2 ｜ silver-hd ｜ qwen-hd\n"
    "　　　　qwen角色设定表 ｜ qwen修复 ｜ 半厚涂 ｜ sf ｜ anima多图\n"
    "　　　　anima线条 ｜ anima写实 ｜ anima鬼针草 ｜ anima肉感 ｜ jank\n"
    "　　　　sd ｜ nffa ｜ cunny ｜ nai\n"
    "\n"
    "【玩法】\n"
    "横屏：silver 横屏 一个女孩\n"
    "漫画：silver漫画 10个 雷电将军在樱花树下\n"
    "moss漫画：moss漫画 雨夜便利店，两个陌生人共用一把伞（一页四格）\n"
    "moss单挂：moss 米山舞 一个女孩（只挂这一个，0.8；两个用 + 连，如 moss 米山舞+748）\n"
    "　可选：fkey / maliker / anireal / ningen / 光影 / 748 / 1999 / nai5 /\n"
    "　　　　takamichi / 米山舞 / wangzhe / 光润\n"
    "造卡：anima-line 造卡 需求 ｜ 引用卡片说「重新生成」换种子\n"
    "随机：今日老婆 ｜ 随机萝莉 ｜ 随机兽耳 ｜ 随机女仆\n"
    "聊天：#角色扮演 ｜ #角色名\n"
    "\n"
    "【引用】\n"
    "引用图：提取提示词 / 反推提示词 / 说需求改图\n"
    "引用回执：再来一张 = 换种子"
)

# 随机口令与今日老婆（2026-10-05）：斜杠可带可不带，认纯口令。
_RANDOM_CMD_RE = re.compile(r"^\s*/?\s*随机(萝莉|兽耳|女仆)\s*$")
_WAIFU_CMD_RE = re.compile(r"^\s*/?\s*今日老婆\s*$")
# 「/更多渠道」按**关键词**识别（2026-10-05 用户口径）：消息里含「更多渠道」即回
# 全部跑法，不再要求整条精确匹配——用户实际发过「：更多渠道」（全角冒号）掉进兜底。
_MORE_CHAN_RE = re.compile(r"更多渠道")

# ─── 「/直通-」零 token 直出（2026-10-07 用户拍板）──────────
#
# 用户原话：「如果我要保留渠道+提示词直接生成…就应该是特定格式前缀来保证…
# 自己人想要跑的话，就不需要消耗 token 了」「严格点吧，默认后面接英文提示词」。
#
# **为什么必须带前缀**：裸写「silver 三档 1girl」代码不敢直接跑——它可能是
# 聊天、是讨论、是问句，必须过一次 AI 才知道是不是下单。前缀是用户**主动
# 拍板「这就是下单」**的信号，代码才敢跳过 AI、跳过搜索，零 token 直发。
#
# 格式：`/直通-<渠道段>|<英文提示词>`
#   - 前缀 `/直通-`（全角/半角斜杠都认），**顶格**（前面可留空格）。
#   - 渠道段与提示词之间用 **`|`（半角竖线）** 分隔——**刻意**不用 `-`：连字符
#     在提示词里到处都是（`blue-hair`、NAI 负号权号 `-1::tag::`），拿它当分隔
#     必撞车（10-07 实测 `三档-gloss-1girl` 会把 gloss 吞进正文）。`|` 在
#     danbooru/NAI 标签流里几乎不出现，语义又天然是「分段」。
#   - **第一个 `|` 之前** = 渠道段（里面用空格隔档位/画风：`三档 gloss`）。
#   - **第一个 `|` 之后** = 提示词，**原样直发**（`|`、`-`、`::` 一律不碰）。
#   - 没写 `|`（整条都是渠道段）→ 解析不出提示词 → 回一句提示，不入队。
# 严格匹配（用户要求）：渠道段解析不出渠道 → 落回原管道（交 AI），不猜。
_DIRECT_RE = re.compile(r"^\s*[\/／]\s*直通\s*[-－—]\s*(?P<body>.*?)\s*$",
                        re.S)
# 光有前缀、后面空的（`/直通-`）：`body` 为空，单独回格式提示。
_DIRECT_HEAD_RE = re.compile(r"^\s*[\/／]\s*直通\s*[-－—]\s*$")


def _parse_direct(text):
    """`/直通-` 前缀解析。返回 (skill, prompt, err)。

    命中前缀且渠道可认、提示词非空 → (skill, prompt, "")，调用方直接入队。
    其余情况 err 非空（调用方原样发回会话），skill 为 None：
      - 没写 `|`            → 提醒「用 | 分隔渠道和提示词」
      - 渠道段认不出         → 返回 ("", "", None) 哨兵：**落回原管道**（交 AI），
                              not 直通（严格匹配，不猜）。
    """
    m = _DIRECT_RE.match(text)
    if not m:
        return None, None, None
    body = m.group("body").strip()
    if not body:
        return None, None, ("直通格式：`/直通-渠道|英文提示词`\n"
                            "例：/直通-silver|1girl, blue hair\n"
                            "　　/直通-三档 gloss|1girl, blue hair")
    if "|" not in body:
        # 整条都是渠道段，没给提示词——多半是忘了写 `|`。
        return None, None, ("直通格式：`/直通-渠道|英文提示词`\n"
                            "渠道和提示词之间要用 `|` 隔开。\n"
                            "例：/直通-silver|1girl, blue hair\n"
                            "　　/直通-三档 gloss|1girl, blue hair")
    chan_seg, prompt = body.split("|", 1)
    prompt = prompt.strip()
    chan_seg = chan_seg.strip()
    if not prompt:
        return None, None, ("`|` 后面没写提示词。补上英文提示词再发，"
                            "例：/直通-silver|1girl, blue hair")
    # 渠道段 → 渠道 id。silver 是**默认单图档**，`_parse_channel` 平时不认这个
    # 单字（`silver hair, 1girl` 这种英文 tag 开场会被误判）；直通里显式写了就
    # 按单图档处理（用户给的例子里有它）。
    if chan_seg.strip().lower() == _FALLBACK_SKILL:
        return _FALLBACK_SKILL, prompt, ""
    skill, _desc = _parse_channel(chan_seg)
    if not skill:
        # 渠道段认不出 → 不直通，交回原管道（严格匹配）。用哨兵区分于 err。
        return "", None, None
    if skill not in _allowed_skills():
        return "", None, None
    return skill, prompt, ""

# ─── 提示词的「写法 + 语言」按渠道分家 ─────────────────────
# 2026-10-05 用户点名要明确写进模板：「如果我给的是中文的需求，他要翻译成
# 英文再跑图」——**这条必须明写**，不然模型看到中文输入很容易把中文原样抄进
# prompt（以前是靠「danbooru 标签式英文」顺带暗示，不够硬）。
# 写法分家（对齐 skills/qwen_image_v1/SKILL.md）：
#   - 标签渠道（hd_* / image_gen_v1 / krea2-* / nffa / nai）
#     → 逗号分隔的英文 danbooru 标签串
#   - qwen_image_v1 → **完整主谓宾的自然语言句子**，不写标签堆、不写负面词
#     （SKILL.md 原话：「这里写自然语言句子，不写标签」「英文最好，中文也认」）
# 唯一不翻译的：**要出现在画面里的文字**（招牌、台词、LOGO）——照原样用引号写。
_PROMPT_LANG_TAGS = (
    "prompt 写法：**英文 danbooru 标签串**（逗号分隔短语）。"
    "**用户给的是中文需求就先翻译成英文再写，prompt 字段里不许出现中文**。")
_PROMPT_LANG_QWEN = (
    "prompt 写法：**完整主谓宾的英文自然语言句子**（不是标签堆、不写负面词）。"
    "用户给的是中文需求就翻成英文句子；"
    "**要出现在画面里的文字**（招牌、台词、LOGO）用引号原样写、别翻译。")
_PROMPT_LANG_ANY = (
    "prompt 写法：**一律英文**。用户给的是中文需求就先翻译成英文再写，"
    "prompt 字段里不许出现中文。默认渠道用 danbooru 标签串（逗号分隔短语）；"
    "qwen_image_v1 例外——它写完整主谓宾的自然语言句子，"
    "**要出现在画面里的文字**（招牌、台词、LOGO）用引号原样写、别翻译。")


def _prompt_lang(skill):
    """按渠道给「写法 + 语言」那一句。skill 为空（渠道还没定）给通用版。"""
    if not skill:
        return _PROMPT_LANG_ANY
    return _PROMPT_LANG_QWEN if skill.startswith("qwen") else _PROMPT_LANG_TAGS




# ─── 照片感通用写法（2026-10-23 用户拍板：从 qwen 专用提升成通用方法）──
# 来路：8 张同 seed 对照实测（产物在 `web/实测_qwen写实/`）。同一条需求写
# `photo-realistic ... natural skin texture`，出来是磨皮网红脸（面部高频能量
# 186，全场最低）；换成「皮肤分区 + 侧后轮廓光 + 一半脸进阴影」立刻像照片。
# 变量拆开验过：**光的方向是主因**（只留光、皮肤词全删也成立），皮肤词是次因。
#
# 2026-10-23 用户判定：这不是 qwen 专有，而是「照片感从哪来」的通用方法
# ——「其实完全可以直接加到我们的怎么写提示词里面，这个本身就是一个通用的
# 方法」。所以它从「点名 qwen 时附加的 chan_hint」改成正文里的一节，
# `_MASTER_TEMPLATE` 与 `_REVISE_TEMPLATE` 共用同一份。
# qwen 专有的那条（cfg=1.0、负向完全不生效）留在渠道表的 qwen 行，不混进通用规则。
#
# ⚠️ 里面的英文例子是实测配方，改措辞可以，**别删例子**——删了就退回
# 「写 white stockings 这种空词」，而那正是被证明无效的写法。
_REAL_PORTRAIT_RULES = (
    "\n【照片感从哪来】画面里是真人、要的是照片感时，质感来自拍摄条件，"
    "不来自形容词。\n"
    "- 光先定方向：主光从哪来、另一侧有没有轮廓光、另一半脸是不是落进阴影。"
    "例 `soft key light from the window plus a warm rim light from behind, "
    "the other side of the face falls into shadow`。只写 soft / natural lighting "
    "会得到平光，平光就是塑料感的来源。\n"
    "- 皮肤写物理状态和高光落点：半哑光的底，T 区（额头、鼻梁、鼻尖）有淡淡"
    "皮脂高光，脸颊 / 下颌 / 眼周保持哑光，鼻翼有细微毛孔，肤色轻微不均。\n"
    "- 皮肤缺陷词一次挑 2~3 个（pores / freckles / slight redness），"
    "写多了会放大成一张脏脸。\n"
    "- 织物同样写物理状态：织法、拉伸后的变形、拉伸处的透光、光在纤维上那层"
    "软反射。白丝 `sheer white stockings, fine knit weave, soft sheen along the "
    "thigh, slight gathering at the knee`；渔网 `black fishnet stockings, the mesh "
    "distorts over the skin, diamonds stretch wider at the knee`。"
    "`white stockings` / `fabric texture` 这类词没有物理信息，等于没写。\n"
    "- 收尾补一句拍摄条件（焦段 / 光圈 / 胶片颗粒），再补「像朋友随手拍的抓拍，"
    "不是杂志拍摄」。\n"
    "- `photo-realistic`、`realistic`、`8k`、`masterpiece` 会把画面拉回精修"
    "写真的默认值，越写越假——它们不提供任何拍摄信息。\n"
)




# ─── 唯一的一条转译模板（2026-10-05 晚用户拍板重做；2026-10-23 按社群规范重排）──
# 用户原话（10-05）：「提示词的构成就是：人设 / 工具列表 / 用户的历史对话 /
# 最近的用户需求，就这么简单。」「AI 本身就能胜任整个工作。」
# 用户原话（10-23）：「我们按照社区的来吧，社区教的东西比我们自己总结的好一些。」
#
# 结构 = 三节：① 怎么用工具 ② 怎么设计提示词 ③ 渠道对照表，
# 末尾接 {chan_hint} {search} 【最近对话】 【用户】。
#
# 2026-10-23 重排（用户拍板 B 方案）：
#   - 画面部分从「六层」换成 booru 社群通用的**八位顺序**（质量词 → 人数 →
#     角色作品 → 画师串 → 外观 → 服饰 → 姿势 → 场景光线构图）。顺序即权重；
#     旧版把画师串放第 1 位，且完全没有质量词和人数位。
#   - 补 3 个完整对照示例（用户原话 → 最终 prompt）：示例天然带着顺序，
#     比再加三百字规则管用。
#   - 清掉全部 `**` 与反向指令（粉色大象），改成陈述后果 + 给替代动作。
#   - `{chan_hint}` 从模板正中（2574 字处）挪到末尾：它原是正文里唯一的变量，
#     点名渠道那一轮会把后面 2500 字全部踢出缓存前缀。
#   - moss 刻字规范整段删除（用户拍板「moss 走普通路线，刻字走漫画台」），
#     qwen 写实规范提为通用规则 `_REAL_PORTRAIT_RULES`（主模板/改图模板共用）。
#
# 三处**代码仍然要给**的东西（模型不可能自己知道的本地事实，不是「指挥 AI」）：
#   - 渠道名清单（hd_3_* / nai … 是我们自己起的，模型猜不出来）
#   - `chan_hint`：代码从原话里认出的开头渠道词，直接写明给模型（免它误判）
#   - 标签库资料 `{search}`
#
# ⚠️ 正文里的字面大括号一律写双份（`.format()` 只认 chan_hint / search /
# recent / text 四个占位符）。改完先跑 `test_full_prompt_stays_within_budget`。
_MASTER_TEMPLATE = (
    "你是大大怪，一个生图 AI。用户给画面，你写成提示词调工具画出来；"
    "用户聊天、提问、要提示词，你直接回话。\n"
    "\n"
    "## 一、怎么用工具\n"
    "\n"
    "动手时只输出一行 JSON，前后不加别的字。\n"
    "\n"
    "### 画一张图\n"
    "\n"
    "{{\"tool\": \"generate_image\", \"prompt\": \"英文提示词\", "
    "\"skill\": \"渠道，可省\", \"lora\": \"LoRA名，可省\", "
    "\"source_image\": 1, \"seed\": 123}}\n"
    "\n"
    "- `prompt` 必填，danbooru 格式的英文标签串，逗号分隔的短标签"
    "（如 `1girl, silver hair, school uniform, looking_at_viewer`）。"
    "整句叙述会被标签渠道当乱码、画面错位；走 qwen 才写完整英文句子。\n"
    "- `skill` 只在用户点名渠道时填，对照表在最后。\n"
    "- `lora` 只在用户点了 moss 的某个 LoRA（「moss 米山舞」）时填，写那个名字"
    "本身、别翻译、别加 .safetensors；两个用 `+` 连（`米山舞+748`）。"
    "名字原样留在 prompt 开头也认，中文名照原词写。\n"
    "- `source_image` 填 1 表示垫图，判据见下面「带图轮怎么走」。\n"
    "- `seed` 只在用户点名种子时填。用不上的字段省略。\n"
    "\n"
    "### 画一组图或连续漫画\n"
    "\n"
    "{{\"tool\": \"generate_comic\", \"brief\": \"中文剧情\", \"panels\": 10}}\n"
    "\n"
    "- `brief` 写中文剧情和角色，名字照抄用户原词。\n"
    "- `panels` 填用户报的张数，没报就省略，上限 50。\n"
    "\n"
    "用在三种情况：用户要漫画、连环画、多格、条漫；用户要「N 张」"
    "（生成10张 / 来5张 / 出4张）；用户点名「anima多图」。要好几张只有这一条路。\n"
    "\n"
    "走哪条渠道由代码从原话判定，你省略渠道字段。只要 1 张时用 `generate_image`。\n"
    "\n"
    "### 查历史图\n"
    "\n"
    "{{\"tool\": \"recall_image\", \"id\": \"HT-20261005-123456-789\"}}\n"
    "\n"
    "用在用户引用带编号的图、问「这张用的什么词」「种子多少」的时候。\n"
    "\n"
    "### 不动手\n"
    "\n"
    "{{\"reply\": \"你要说的话\"}}\n"
    "\n"
    "用在用户聊天、提问、要提示词，或者这一轮只是讨论画面的时候。\n"
    "\n"
    "### 直接出图\n"
    "\n"
    "用户给了画面、或者说了「生成 / 跑 / 画一张」，就是下单。这类轮次直接出图，"
    "prompt 由你补全，细节按常理填满——反问只是多耗他一轮时间。\n"
    "\n"
    "### 再来一张\n"
    "\n"
    "最近对话里以 `[直达生图]` 开头的行，是上一张图实际用的渠道和提示词。"
    "用户说「再来一张、换个种子」，照那一行的渠道和词重跑，`seed` 留空让系统换种子。"
    "要微调时拿那一行当底稿在上面改，原词是最可靠的起点。\n"
    "\n"
    "### 带图轮怎么走\n"
    "\n"
    "1. 用户明说「图生图 / 垫图 / 照着画 / 复刻 / 参考这张的构图」，"
    "`source_image` 填 1，渠道沿用他点名的那个或原渠道，prompt 照常写完整画面描述。\n"
    "\n"
    "2. 用户引用了图、同时又提需求（换装 / 换发色 / 改动作 / 要全身照），"
    "`source_image` 留空，这是照这张改词重画：拿那张图当初的提示词把新需求并进去，"
    "再用原渠道文生图。自家图从账本取原词和原渠道，别人的图看画面反推；"
    "查不到原渠道时 `skill` 留空。\n"
    "\n"
    "3. 用户说「qwen 图生图」且只改一处，`source_image` 填 1 配 `qwen_image_v1`"
    "（原话是「qwen 超清」时换 `qwen-hd`），prompt 只写一句改动指令、末尾补 "
    "`keep everything else exactly the same`。说「qwen修复 / 修清楚 / 去糊」"
    "则是修这张图本身、不是重画：带引用图时代码已经直接入队、轮不到你，"
    "没图时回一句「修复需要先有一张图」。\n"
    "\n"
    "4. 垫图能力在 silver 系和 qwen 系，silver 有锁构图骨架；jank、nffa、cunny、"
    "miao、krea2 系、sd、moss 是文生图专用，填了会退回文生图、白等一轮。"
    "用户只是评价这张图、或只要反推提示词时，`source_image` 留空，本轮不画。\n"
    "\n"
    "5. 本轮一张引用图都没有、用户却明说「图生图 / 垫图」时，回一句"
    "「把你要改的那张图引用一下（或直接发上来），我再动手」——这句走 "
    "`{{\"reply\": \"…\"}}` 那一态，本轮不写 prompt，散文解析不出来、"
    "整轮会掉回 agent。\n"
    "\n"
    "## 二、怎么设计提示词\n"
    "\n"
    "### 按这个顺序写\n"
    "\n"
    "标签串有位置：越靠前的词权重越大，同一个词放第 3 位和第 8 位效果不同。\n"
    "\n"
    "| 位 | 放什么 | 例子 |\n"
    "|---|---|---|\n"
    "| 1 | 质量词 | `masterpiece, best quality, score_7` |\n"
    "| 2 | 人数 | `1girl, solo`、`2girls`、`1girl, 1boy` |\n"
    "| 3 | 角色与作品 | `hu_tao_(genshin_impact), genshin_impact` |\n"
    "| 4 | 画师串 | 用户给的那个，原样放 |\n"
    "| 5 | 外观 | 发色、瞳色、发型、表情 |\n"
    "| 6 | 服饰 | |\n"
    "| 7 | 姿势与动作 | 主动作，一个 |\n"
    "| 8 | 场景、光线、构图 | |\n"
    "\n"
    "第 1 位这三段 Anima、NoobAI、Illustrious、Pony 四套体系都认，可再跟 "
    "`absurdres`；点名 anima-line / anima-real / anima-guizhencao 时跳过这一位，"
    "那三条已内置质量词和画师串。`8k`、`4k`、`ultra hd` 是摄影词、不占 booru 词表，"
    "交给第 8 位的光线表达。\n"
    "\n"
    "第 2 位每轮必写、自己数人头，它是模型判断画几个人的主要依据。\n"
    "\n"
    "第 3、4 位照抄：角色用 `角色_(作品)` 跟一个作品 tag，画师串和权重语法"
    "（`1.2::tag::`、`artist:xxx`）原样保留。认不出的角色跳过第 3 位、改到第 5 位"
    "用外貌描述；用户没给画师串就跳过第 4 位。\n"
    "\n"
    "第 5 到 8 位缺的按常理补；第 8 位两三个场景词加两三个光线词就够，"
    "写长了会把人物和动作稀释掉。两人以上时长相和服装分别挂在各自的人身上，"
    "如 `1girl, silver hair, red dress, 1boy, black hair, white shirt`，"
    "服装就不会穿错人。\n"
    "\n"
    "### 三个完整例子\n"
    "\n"
    "用户：画个银发女高中生，放学后在教室窗边\n"
    "`masterpiece, best quality, score_7, 1girl, solo, silver hair, "
    "school uniform, sitting on desk, classroom, window, backlighting, "
    "sunset, cowboy shot`\n"
    "\n"
    "用户：两个女孩在咖啡馆聊天\n"
    "`masterpiece, best quality, score_7, 2girls, black hair, white sweater, "
    "brown hair, denim jacket, sitting across from each other, cafe interior, "
    "warm lighting, upper body`\n"
    "\n"
    "用户：要个御姐\n"
    "`masterpiece, best quality, score_7, 1girl, solo, mature female, tsurime, "
    "red lipstick, sharp jawline, collarbone, black turtleneck, "
    "dim lighting, rim light, upper body`\n"
    "\n"
    "### 五条基本写法\n"
    "\n"
    "- 中文需求翻成英文再写。画面文字（招牌、台词等）原文照抄，"
    "用英文双引号括住，紧跟形式与位置：台词 `speech bubble`，字幕 "
    "`subtitle at the bottom`。十个字内，超了印前半句。\n"
    "- prompt 只写画面内容，「重绘 / 高清 / 加强细节」这类操作词交给渠道参数。\n"
    "- 每一轮都是新请求：角色、服装、动作、场景按这轮重新写。\n"
    "- 同一个意思写一个词就够，重复写不加强效果，反而稀释别的词。\n"
    "- 走 qwen 时写完整英文句子，其余渠道按上面那张表写标签串。\n"
    "\n"
    "### 抽象气质要落成可见线索\n"
    "\n"
    "用户说的成熟、清纯、飒、温柔、疲惫、妖、高级感，是这张图的核心，"
    "把它落到能画出来的东西上：年龄感、骨相脸型、妆、眼神形状、体态、服装材质、光。\n"
    "\n"
    "只写 `mature female` 太弱，至少再配两三个可见线索，像第三个例子里那样"
    "（`tsurime`、`red lipstick`、`sharp jawline`、`collarbone`）。"
    "按这张图挑词，库里查不到的也照样写。\n"
    "\n"
    "### 取景、角度与光线\n"
    "\n"
    "用户没指定时自己挑。景别决定别人一眼看到什么：`portrait` 到肩、"
    "`upper body` 到腰、`cowboy shot` 到膝盖、`full body` 全身、`close-up` 只到脸。"
    "要全身就写 `full body`。景别和角度各挑一个，两个一起写会互相打架——"
    "角度用 `from above` 俯视、`from below` 仰视、`from side` 侧拍、"
    "`dutch angle` 倾斜、`over-the-shoulder` 过肩。\n"
    "\n"
    "光线决定这张图是干净还是氛围重，按画面情绪挑一组：\n"
    "\n"
    "- 方向：`backlighting`、`rim light`、`side lighting`、`top-down lighting`\n"
    "- 质感：`soft lighting`、`hard lighting`、`dramatic lighting`、`chiaroscuro`\n"
    "- 来源：`sunset`、`golden hour`、`window light`、`neon lights`、`candlelight`、"
    "`moonlight`\n"
    "- 氛围：`god rays`、`lens flare`、`dust motes`、`bloom`\n"
    "- 空间：`depth of field`、`wide angle`、`fisheye`\n"
    "\n"
    "阴影方向跟着光走：写侧光，影子就落到另一侧。走 qwen 时这几条写成完整句子，"
    "例如 `soft window light from her left, shallow depth of field`。\n"
    + _REAL_PORTRAIT_RULES +
    "\n"
    "### 动作配额\n"
    "\n"
    "一条 prompt 只有一个主动作。模型按词分配身体，两个动作抢同一只手，"
    "画面就会多出手或者叠手。要同时拥抱和倒茶，就拆到两只手上写清楚："
    "`holding a teapot in her right hand, her left arm around him`；"
    "拆不开就降级成状态——`one arm around his neck` 是状态，不算第二个主动作。\n"
    "\n"
    "手被占了一只，其余写单数：`arms around his neck` 已经用掉两只手，"
    "改成 `one arm around his neck`。一个人两只手两条腿，"
    "画面里有几个人就按几倍算。肢体动作带主体，写 `her left hand on cheek`、"
    "`his right thigh`；没有主语的 `hand on cheek` 会让模型多长一只手。\n"
    "\n"
    "标签串没有先后也没有时序，冲突只能靠删、合并、降级成状态。\n"
    "\n"
    "## 三、渠道对照表\n"
    "\n"
    "用户点名渠道时填 `skill`，没点名就省略，代码会走默认渠道 "
    + _FALLBACK_SKILL + "。\n"
    "\n"
    "### 画风档\n"
    "\n"
    "要更高清、或者点名 anima 时用这一族。画风四种：clear 清晰 / soft 柔和 / "
    "gloss 油亮 / curvy 肉感。尺寸只剩一档，快档 / 最小档 / 二档于 2026-10-07 "
    "全部取消。\n"
    "\n"
    "| 用户说的 | 填 skill |\n"
    "|---|---|\n"
    "| anima、三档、高清 | `hd_3_clear` |\n"
    "| anima clear、清晰、清透 | `hd_3_clear` |\n"
    "| anima soft、柔和 | `hd_3_soft` |\n"
    "| anima gloss、油亮 | `hd_3_gloss` |\n"
    "| anima curvy、丰满、肉感 | `hd_3_curvy` |\n"
    "| rs29 | `rs29` |\n"
    "\n"
    "`rs29` 不是第五种画风：参数同 clear（两段 Realskin、15+15 cfg 3、"
    "3072×4608），只把底模换成 2.9B（更细、更慢）。点名才走。\n"
    "\n"
    "### 独立渠道\n"
    "\n"
    "| 用户说的 | 填 skill | 什么时候走这条 |\n"
    "|---|---|---|\n"
    "| silver | `silver` | 默认，没特别要求就它 |\n"
    "| silver-hd、silver 超清 | `silver-hd` | 要超清、大图、当壁纸 |\n"
    "| 半厚涂 | `semi-thick` | 点名，silver 的配方但笔触更薄、对比和饱和度更低 |\n"
    "| sf、完整版、silver完整版 | `sf` | 点名，完整版底模（12+8 步 / cfg 2 / 零 LoRA，默认竖屏，用户要横向画面时自动出横版；配场景和光源出图，平色背景会放大伪影） |\n"
    "| nai | `nai` | 点名 nai，或给了画师串 |\n"
    "| nai_wide | `nai_wide` | 点名 nai 且要横向画面 |\n"
    "| qwen、千问、通义 | `qwen_image_v1` | 点名，要写实照片感、要出中文。"
    "这个渠道 cfg=1.0，负向完全不生效，约束一律写进正向 |\n"
    "| qwen30步 | `qwen30` | 明说 qwen30步，更慢更精细 |\n"
    "| qwen-hd、qwen 超清 | `qwen-hd` | 明说 qwen 超清 |\n"
    "| qwen 角色设定表 | `qwen-charsheet` | 要设定表、三视图、表情集，必须配参考图 |\n"
    "| qwen修复 | `qwen-restore` | 要把图本身修清楚，必须配原图 |\n"
    "| sd | `image_gen_v1` | 点名 sd |\n"
    "| jank | `jank` | 点名 jank |\n"
    "| nffa、cunny、miao | 同名 id | 点名才用，cunny / miao 很慢 |\n"
    "| moss | `moss-qunyou` | 点名 moss：竖版 25 步约 74 秒，出字最准；nai5 触发词自动加，提示词不用写 @//neoanime/ |\n"
    "| moss横向 | `moss-wide` | 点名横向才走（1536×1024，其余同 moss；裸「moss」永远竖版） |\n"
    "| moss光影1/2 | `moss-light1`/`moss-light2` | 只点名才走，两套固定 LoRA 组合 |\n"
    "| moss+名字 | `moss-qunyou` + `lora` 参数 | 只挂那一个（0.8）：fkey/maliker/anireal/ningen/光影/748/1999/nai5/takamichi/米山舞/wangzhe/光润；两个用 `+` 连 |\n"
    "| anima线条 | `anima-line` | 点名，线条感重 |\n"
    "| anima写实 | `anima-real` | 点名，真人质感 |\n"
    "| anima鬼针草 | `anima-guizhencao` | 点名，鬼针草画风 |\n"
    "| anima肉感 | `anima-flesh` | 点名，皮肤质感和体量感最重 |\n"
    "\n"
    "最后四条是 anima 的独立配方，不参与画风档那套。有两个字形要分清："
    "「anima肉感」连写是渠道 `anima-flesh`；想说画风肉一点，用「anima curvy」"
    "走 `hd_3_curvy`。\n"
    "\n"
    "krea2 系按风格分开，点名时照下面填：krea2米山舞、krea2日系、krea2亚洲真人、"
    "krea2动漫真人、krea2真人cos、krea2宝可梦、krea2柔光、krea2奶油、krea2写实、"
    "krea2通用、krea2铭音、krea2鬼针草、krea2铭鬼。光说「krea2」本身不算点名。\n"
    "\n"
    "两点容易混：银发、silver hair 是发色，属于画面内容；画面要出中文时走 moss。\n"
    "{chan_hint}"
    "{search}"
    "\n"
    "【最近对话】\n{recent}\n"
    "\n"
    "【用户】\n{text}"
)

# 标签库资料段（{search} 槽）。用户点名要求：**数据缺失就按实际情况补，
# 但一定要参考搜索给回来的**。
#
# 2026-10-06 措辞收紧：资料包从「几个角色名」变成「500~1000 字的候选池 +
# 同族写法参考」之后，**里面必然带一些不相关的噪音**（中文滑窗对「三档」
# 这种词会命中 `gear_third`）。原来那句「资料里给了的一律照它写」会把噪音
# 直接钉进提示词——所以改成明写「候选池，不相符的直接忽略」，但保留
# 「给了 tag 名的照抄」这条防编造的核心约束。
#
# 2026-10-20 再改定位（用户原话：「anima-tags 其实就是给一个参考，我们的 AI
# 自己就有大量的训练数据，可以自己基于参考的内容和实际场景去决策」）：
# 原第 2 条「**角色和作品**以这份资料为准」没错，但第 4 条「凡是资料给了
# tag 名的一律照它写」被模型读成了「整条 prompt 只能从库里挑」，于是
# 「成熟一点」这种库里查不到的语义就被悄悄丢掉。现在拆成两条：**角色/作品
# 照抄**（防编造，这是库的真正价值），**其余描述词/气氛/气质/镜头全交模型**。
# 另补一条下划线转空格——库里存的 `white_shirt` 原先被原样抄进 prompt。
_SEARCH_HEADER = (
    "\n【标签库资料】从 Danbooru 标签库（32.8 万条真实条目）里查出来的，"
    "**只是参考池，不是词表上限**。\n"
    "格式 `中文 → tag 名`；`｜同族：…` 是同一主体在库里还能怎么修饰。\n"
    "**怎么用**：\n"
    "1. 它是**候选池和写法参考**，不是必须全用。只取和用户需求相符的；"
    "明显不相符的（用户要「女孩」而资料里给了个无关角色）**直接忽略**。\n"
    "2. **角色和作品**以这份资料为准——资料里给了 tag 名的照抄，别凭印象写"
    "（同一个名字库里可能有几十个候选，凭印象必错）。\n"
    "3. **其余全靠你自己**：普通描述词、画法、画面氛围、气质、镜头，"
    "库里本来就不收，**用你自己知道的最贴切的英文写**，"
    "别因为资料里没查到就不写、也别硬找个不相干的库内 tag 顶替。\n"
    "4. 同一个中文名有多个候选时，结合用户的需求和最近的对话挑最合适的那个。\n"
    "5. **资料里是下划线形态**（`white_shirt`），写进 prompt 要换成空格"
    "（`white shirt`），`score_*` 除外。\n"
    "{doc}\n"
)

_REVISE_TEMPLATE = (
    "你是生图提示词修正器。用户引用一张图并提出要求（也可能只是让你看看、"
    "或问你点别的）。只输出 JSON 本体，格式：{{\"skill\": \"渠道id\", \"prompt\": "
    "\"修正后的完整英文提示词\", \"source_image\": 1}}（用不上的参数一律省略）\n"
    # ── 漫画（2026-10-16）──────────────────────────────────────────
    # 改图管道原先**根本没有** `generate_comic` 这条路，模型物理上吐不出漫画：
    # 带引用图 +「silver 10张 漫画」→ 落成 silver-hd **单图**（19:44 私聊
    # 455242826 实录，用户报「ai 无法理解需求」）。补上这条路，让模型有机会表达
    # 「画一批」。渠道仍不进 JSON——`_enqueue_comic` 会 `_comic_style` 从原话判。
    # ⚠️ 2026-10-16 二修：`brief` **必须是中文剧情**，且**只许输出 tool / brief /
    # panels 三个字段**。原措辞没把「别再给 prompt」说死，模型就把识图反推的英文
    # tag 填成 brief，编剧拿到一段没剧情的 tag 就去照抄规范示例里的角色名
    # （实录：引用小猪图 +「anima 多图」→ 和泉纱雾 + 和泉正宗 + 猪）。
    "- **用户要「漫画 / 连环画 / 多格 / 条漫 / 多图」，或要「N 张」同一个角色的连贯图时，"
    "只输出 {{\"tool\": \"generate_comic\", \"brief\": \"<中文剧情>\", "
    "\"panels\": <张数>}} 这三个字段**——这是**另一条路**，画一批、不是改这一张；"
    "**走上这条路就不要再输出 prompt / source_image / reverse / skill**。"
    "`brief` **必须是中文**（绝不许把画面英文标签填进来）：写清「主角是谁、"
    "长什么样、要做什么」，**引用图里的主体就直接当主角写进去**——图里是什么就"
    "写什么（猪就写猪、人就写人），把它的外观照实写出来；用户另外提了角色名／"
    "剧情／要求就一并并进去（角色名照抄用户的写法）。"
    "panels 填用户报的张数、没报就不填；**只要 1 张就别用这条路**，照上面改图的路走。\n"
    "- 先看清楚画面（人物、发色发型、瞳色、表情、服装、姿势、场景），"
    "prompt 必须覆盖画面全部要点，用户没提到的细节原样保留\n"
    "- 原提示词{anchor_note}：与画面冲突时，一律以画面为准\n"
    "- skill 沿用「原渠道」，除非用户点名要换；**引用的不是自家图、又没有原渠道，\n"
    "  这时 skill 就别填**（留空系统会走默认）\n"
    # 渠道清单必须写在这里：改图管道不走 `_MASTER_TEMPLATE`，模板里没有 id
    # 表时模型听不懂「换 silver」，只会沿用原渠道（2026-10-06 19:22 群
    # 580929233 实录：引用图 +「silver 生成」→ 又跑了一次 jank）。
    "- 渠道 id 只有这些（用户点名换渠道时照左边说的话填右边那个）：\n"
    "  **用户没点名渠道时就别填 skill**（留空就行，系统自己挑默认的，别替它选）；\n"
    "  anima（= 三档）= hd_3_<画风>（末尾带 2x 像素放大 → 3072×4608；"
    "画风只有 clear/soft/gloss/curvy 四种，例「anima gloss」= hd_3_gloss，"
    "没说画风 = hd_3_clear）；\n"
    "  anima-line / anima-real / anima-guizhencao / anima-flesh = "
    "说「anima线条 / anima写实 / anima鬼针草 / anima肉感」"
    "（四条独立配方，不在画风体系里）；\n"
    "  nai / nai_wide = 说「nai」（画师串和权重语法认这两个）；\n"
    "  qwen_image_v1 = 说「qwen / 千问 / 通义」；image_gen_v1 = 说「sd」；\n"
    "  qwen30 = 说「qwen30步」（qwen 的 **30 步**版，其余参数一模一样，更慢）；\n"
    "  krea2-yoneyama / krea2-rella / krea2-asianmix / krea2-anime2real / "
    "krea2-coscandid / krea2-pokemon / krea2-softlight / krea2-creamy / "
    "krea2-real / krea2-general / krea2-mingyin / krea2-guizhencao / "
    "krea2-minggui"
    " = krea2 系 13 条，用户原话点「krea2米山舞 / krea2日系 / krea2亚洲真人 / "
    "krea2动漫真人 / krea2真人cos / krea2宝可梦 / krea2柔光 / krea2奶油 / "
    "krea2写实 / krea2通用 / krea2铭音 / krea2鬼针草 / krea2铭鬼」时对应填；\n"
    "  qwen-charsheet = 说「qwen 角色设定表」（**7 分区** character design sheet 工作流，\n"
    "  qwen-image-2.1 上，2304×1536 / 16 步，不放大；右下细节格不画文字）。\n"
    "  qwen-restore = 说「qwen修复」（**引用图修复**工作流，qwen-image-2.1 + 修复 LoRA，\n"
    "  1536 面积档 / 25 步，不放大；引用图轮由代码专路直接入队，不用你填 skill）。\n"
    "  ⚠️ 通用「krea2」已下架，原话只说了 krea2 没带风格词时**别填 krea2 系**"
    "（那 13 个名字都不是他说的），skill 别填。\n"
    "  nffa / cunny / miao = 原话点名才填；\n"
    "  moss = 原话点名「moss」才填（MOSS-Image 5B，**25 步** / 1024×1536 / "
    "约 74 秒，画面出字最准，纯文生图；8 步 / 50 步两档已下架，"
    "说「moss8 / moss50」也一律落这一条）；\n"
    "  moss-wide = 同模型横版 1536×1024（其余同 moss；原话点名横向才填，"
    "裸「moss」永远是竖版，别替用户横过来）；\n"
    "  moss-light1 / moss-light2 = 原话点名「moss光影1 / moss光影2」才填"
    "（同 moss 骨架，两套固定的 LoRA 光影组合，竖版 25 步）；\n"
    "  moss + LoRA 名 = 照旧填 `moss-qunyou`，再把名字原样填进 `lora` 参数"
    "（不用写 .safetensors、不用写强度，代码统一按 0.8 单挂；两个用 `+` 连）。"
    "名字留在 prompt 开头也认——「米山舞 一只猫」会自动抽走米山舞、正文只剩一只猫。"
    "可选名字见渠道表那一行；\n"
    "  jank = 原话明说「jank」才填（用户自定义渠道）。\n"
    "  silver-hd = silver 的 4x 超清版，原话明说「silver-hd」或「silver 超清」才填。\n"
    "  qwen-hd = qwen 的 4x 超清版，原话明说「qwen-hd」或「qwen 超清」才填。\n"
    "  ⚠️ silver / silver-hd **有锁构图骨架**（见下面【图生图】）；"
    "jank 是文生图专用、没有骨架。"
    "「银发 / silver hair」是画发色、不是点名渠道，别因为它就改填 silver。\n"
    "【图生图：判据是「构图要不要留」，不是有没有说「图生图」三个字】\n"
    "**引用 / 附图本身不等于要垫图**——只看一件事：用户要的是**同一张图改一处**，"
    "还是**另一张新画面**。\n"
    " ① **明说要照着那张画**（「图生图 / 垫图 / 照着画 / 照这张画 / 复刻 / "
    "参考这张的构图」）→ `source_image` 填 1；skill **沿用原渠道或用户点名的"
    "渠道**，**这两样都没有、或者原渠道没有图生图骨架（见本段末那行）时就填 "
    "qwen_image_v1**（2026-10-17 用户拍板：图生图就走 qwen 这一条路）；"
    "prompt 照常写完整画面描述。系统会把那张图当构图条件喂进去，"
    "出图是**重画的**、尺寸仍是本档画布。\n"
    " ② **引用 / 附图 + 要在这张图上改**——先分两种，判据只有一条："
    "**这张的构图、人物、姿势要不要保留**。\n"
    # 2026-10-21：② 原来把所有「引用图 + 提需求」一律判成改词重画，理由是
    # 「用户没明说图生图」。但用户根本不会说「图生图」这三个字——他说的是
    # 「把眼镜去掉」。这条把自然语言需求系统性误判成「重画」。
    "    · **要保留，只动画面里的一部分**（去掉某样东西 / 加上某样东西 / 换衣服 / "
    "换发色 / 改表情 / 修某个局部）→ **这就是图生图，别改词重画**："
    "`source_image` 填 1，**skill 填 qwen_image_v1**"
    "（只有 qwen 系能只动局部；silver 的锁构图是整张重画，会把没让改的地方一起改掉）；"
    "prompt 只写**一句改动指令**，例 `remove her glasses, keep the pose, face and "
    "background exactly the same`，**别把整张图重新描述一遍**。"
    "**用户不会说「图生图」这三个字，别等他明说才认。**\n"
    "    · **要的是另一张画面**（换姿势 / 换角度 / 改景别 /「要全身照」/ 重画一版 / "
    "同样感觉的再来一张）→ **不填** `source_image`：构图会变，这才是「照这张改词重画」。"
    "先拿那张图当初的提示词（自家图走账本、别人发的图就看画面反推）、把新需求并进去，"
    "**自家图沿用账本里那个渠道**、别换；**引用的不是自家图（账本查不到）就没有"
    "原渠道，这时 skill 别填**，系统会走默认。\n"
    " ③ **明说「qwen 图生图」**（改几处都算）→ `source_image` 填 1，"
    "同时 skill 填 qwen_image_v1"
    "（明说「qwen 超清 / qwen超清」时填 qwen-hd；"
    "原话说「qwen30步 图生图」时填 qwen30）、"
    "prompt 只写**一句改动指令**（例 `change her coat to red, keep the pose, "
    "face and background exactly the same`），不要把整张图重新描述一遍。\n"
    " ④ **修复**（引用图 + 「qwen修复 / 修复 / 修清楚 / 去糊 / 脸修清楚点」）= "
    "把**这张图本身**修清楚 / 去噪 / 去糊，不是重画 → skill 填 qwen-restore、"
    "`source_image` 填 1，prompt 只写用户额外提的要求（没提就空着）。"
    "⚠️ 原话里带「qwen」的修复，代码专路已经自己入队了、轮不到你；"
    "**只打「修复」两个字时得你自己填**。没给图就直说「修复 得配一张图」。\n"
    # ⑤ 是 2026-10-17 补的：原先四条分支**全都假设用户说了「改哪里」**，
    # 「带图但啥也没说要改」没有落点 → 模型只能吐空 prompt，代码报「改图请求
    # 没解析出来」。当天 5 次报错全是这个形状（「anima跑一版，<需求>」「qwen，
    # 图生图」「图生图 不用做其它改变」）。这不是加限制，是**把缺的那条路补上**。
    " ⑤ **带图但什么都没说要改哪**（「图生图」「照着这个再跑一版」「不用做其它"
    "改变」「换个种子重画」）→ **不许留空、不许反问**：prompt 直接填**这张图"
    "完整的画面描述**（自家图用上面「原提示词」那段原词，别人的图按画面写全），"
    "要垫图就照 ① 填 `source_image`。\n"
    "  ⚠️ jank / nffa / cunny / miao / krea2 系 / sd / moss "
    "**没有图生图骨架**，"
    "别给它们填 `source_image`（填了会自动退回文生图、白等一轮）。"
    "只是评价这张图 → **不填**。\n"
    "- {lang}\n"
    # 写实人像通用写法：改图管道**不走** `_MASTER_TEMPLATE`，所以这里也要有
    # 一份，与主模板共用 `_REAL_PORTRAIT_RULES`（2026-10-23 起不再按渠道附加）。
    # 来路：2026-10-20 22:57 前这里没有任何注入点，群里三条「qwen + 引用图」
    # 产出的 prompt 里连 `A realistic photograph ... photorealistic style`
    # 都出来了——正是这套规则要压掉的写法。
    + _REAL_PORTRAIT_RULES +
    "- 禁止权重语法 (tag:1.2)、{{tag}}、::\n"
    "- 具体角色没把握就写外貌特征+作品名，不要编造不存在的角色名\n"
    # 逃逸口**一直开着**：以前代码扫原话认机制词，认到就换成「必须出 prompt」
    # 那段（`_ESCAPE_FORBIDDEN`，2026-10-06 随图生图硬编码一起删）。现在由模型
    # 自己判「这轮是改图还是随口评价」，它判成评价就只回反推。
    "- **别反问**（2026-10-07 用户拍板）：用户明确要改 / 要重画（「换XX」"
    "「改XX」「跑一张」「再来一版」）就直接给 prompt，不许问「要不要改」"
    "「这样可以吗」「确认一下」。\n"
    # 2026-10-17 用户拍板：删掉 `_AGAIN_RE` 关键词早退，把「最近一次生图」当
    # **事实**喂给模型，让它自己判断是重跑还是改。所以这里得告诉它这份事实怎么用。
    "- 用户说「再来一张 / 重跑一版 / 换个种子 / 再画一次」而**没说要改什么** → "
    "prompt 直接用上面「原提示词」（它是空的就用「本会话最近一次生图」那行），"
    "skill 沿用当时的渠道（用户点名换才换），**不反问、不留空**。\n"
    "- **用户的话不是修改/生图请求**（夸奖、闲聊、问别的事）→ 只输出一个 "
    "reverse 字段，值 = 这张图真实的英文 danbooru tag 反推"
    "（reverse 的值要填真实 tag，别照抄这句话）\n"
    "{search}"
    "原提示词（渠道 {last_skill}）：\n{last_prompt}\n"
    "{recent}"
    "用户的话：{text}"
)

# 会话最近一次直达入队的任务（修正/重跑管道的「原提示词」来源）。
# 内存态就够：这些场景发生在刚出图之后，进程重启丢了也就是少个上下文。
_LAST_JOB = {}
_JOB_LOCK = __import__("threading").Lock()


def _remember_job(session_key, skill, prompt):
    with _JOB_LOCK:
        _LAST_JOB[session_key] = {"skill": skill, "prompt": prompt}


# 会话最近一次「直达动作」的一句话，给 `qq_bot` 落史用——下一轮的【最近对话】
# 里得有 AI 上一轮到底干了什么。单张生图和连续漫画都走这儿，格式统一。
#
# 为什么不复用 `_LAST_JOB`：那份的语义是「单张任务」，`_revise`（引用图改图）
# 和 `_revise` 的 `{recent}` 事实都拿它当**单张提示词**用；漫画的 brief 是
# 中文剧情，塞进去会被当成提示词重跑一张垃圾图。所以两件事分开记。
_LAST_DESC = {}


def _note_desc(line):
    """记下本轮直达动作。取不到会话就不记——落史是附赠，不许影响主流程。"""
    try:
        key = qq_api.current_session_key()
    except Exception:
        return
    if not key:
        return
    with _JOB_LOCK:
        _LAST_DESC[key] = line


def last_direct_desc(session_key):
    """上一轮直达动作的一句话（qq_bot 落史用）；没干过返回 ""。"""
    with _JOB_LOCK:
        return _LAST_DESC.get(session_key, "")


def _last_job(session_key):
    with _JOB_LOCK:
        job = _LAST_JOB.get(session_key)
        return dict(job) if job else None


def _is_channel_word(tok):
    """`tok` 是不是**用户点名的渠道 / 档位 / 画风词**（整词，不含正文）。

    只给「署名例外」用：命中就说明这个冒号前缀是**指令**，不是群聊署名。
    """
    t = (tok or "").strip()
    if not t:
        return False
    low = t.lower()
    return (low in _allowed_skills() or t in _FIXED_CHAN_MAP
            or bool(_FIXED_CHAN_RE.fullmatch(t))
            or t in _TIER_MAP or low in _TIER_MAP
            or t in _STYLE_ALIASES or low in _STYLE_ALIASES)


def _strip_attribution(text):
    """剥掉群聊署名前缀（「胡桃桃：好」这种），每行各剥一次。

    ⚠️ **渠道名不许当署名剥掉**（2026-10-16 用户报）。`_ATTRIBUTION_RE` 的形状
    是「行首 1~20 字 + 全角冒号」，而用户点渠道最顺手的写法正好长这样——他
    直接抄机器人回执里那句 `krea2-coscandid：realistic cosplay photo, 1girl…`。
    实测这层会把前缀整个吃掉、渠道跟着丢、静默落默认渠道：23:01 / 23:05 /
    23:06 三次「krea2-coscandid：…」全中（`_parse_channel` 拿到的是剥完的纯
    英文，自然认不出）。所以先看一眼前缀是不是认得出的渠道词，是就**不剥**。
    （同一个正则以前也踩过类似的坑，见 `confirm_gate` 里关于 NAI 权号 `::`
    那段——那次是半角冒号，这次是全角。）
    """
    out = []
    for line in (text or "").split("\n"):
        m = _ATTRIBUTION_RE.match(line or "")
        if m:
            head = m.group(0).strip()
            if head.endswith("："):
                head = head[:-1].strip()
            if _is_channel_word(head):
                out.append(line)
                continue
        out.append(_ATTRIBUTION_RE.sub("", line or "", count=1))
    return "\n".join(out)


def _strip_own_names(text):
    """剥掉消息开头的机器人自己的名字（含文字 @ 的括号扩展）。

    群实录（2026-10-05 01:07）：「@大大怪（生图机器人，贼拉快，种类多） 三档
    soft 初音未来」顶着昵称扩展，渠道词永远匹配不上。名字表 = 群触发关键词
    （QQ_GROUP_KEYWORDS，就是机器人的名字们）。
    """
    t = (text or "").strip()
    for _ in range(3):                       # 最多剥三层（名字叠名字很少见）
        t = re.sub(r"^[＠@]\s*", "", t)
        for kw in sorted(QQ_GROUP_KEYWORDS, key=len, reverse=True):
            # 名字后面的括号扩展（「大大怪（生图机器人，贼拉快，种类多）」）
            # 和分隔符都可有可无：「大大怪 三档」「大大怪三档」都要剥干净。
            m = re.match(
                re.escape(kw) + r"(?:\s*[（(][^）)]{0,40}[）)])?\s*", t)
            if m:
                t = t[m.end():]
                break
        else:
            break
    return t.strip()


def _recent_lines(history, limit=10):
    """会话历史的最近几条，压成「用户：…/AI：…」短行给转译调用看指代。

    菜单和生图回执整行滤掉（2026-10-05 用户点名）：不然最近 10 条全是
    「🎨 生图直达…」，指代上下文等于没有。

    AI 侧**只留出图记账行**（2026-10-16 用户拍板，判据与理由见
    `_RECENT_AI_KEEP_RE`）：纯文本回话不喂——它自己的拒绝话术一旦回喂，
    下一轮就会照着继续拒。**用户侧照旧全留**。
    """
    lines = []
    for m in history or []:
        if m.get("role") == "system":
            continue
        c = m.get("content")
        if not isinstance(c, str) or not c.strip():
            continue
        if _RECENT_NOISE_RE.search(c):
            continue
        if m.get("role") != "user" and not _RECENT_AI_KEEP_RE.search(c.strip()):
            continue
        who = "用户" if m.get("role") == "user" else "AI"
        lines.append("%s：%s" % (who, c.strip()[:120]))
    return "\n".join(lines[-limit:])


def _extract_json(text):
    """从回复里抠 JSON。容错 ```json 围栏、前后废话；抠不出返回 None。"""
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    return data


def _ask(content):
    """一次 LLM 调用 + 抠 JSON。拿不到有效结果返回 None。"""
    try:
        reply = llm.call_llm(
            [{"role": "user", "content": content}], timeout=30)
    except Exception as e:
        log.warning("[direct] LLM 调用失败：%s", e)
        return None
    data = _extract_json(reply)
    if not data:
        log.warning("[direct] 输出不是 JSON（%r）", (reply or "")[:120])
    return data


def pop_qa(session_key):
    """取走本轮回复的落史记录（user_text, reply），没有返回 None。"""
    return _QA_LAST.pop(session_key, None)


def _humanize_error(result):
    """工具返回的「错误：…」文案是写给模型看的，尾巴带着指示语句。
    直达管道直接发给真人，只留第一句。"""
    first = result.split("。")[0].strip()
    return first if first.endswith("。") else first + "。"


def _random_resample(kind):
    """随机口令被审核拦下时，worker 用来换一条新提示词的可调用体。

    kind：萝莉 / 兽耳 / 女仆（同主题重抽）或 waifu（整个角色重抽）。
    任何异常都吞掉返回 ""——重抽失败走兜底话术，不该炸 worker 线程。
    """
    def fn():
        try:
            if kind == "waifu":
                return random_tags.draw_waifu()[1]
            return random_tags.sample_prompt(kind)
        except Exception:
            log.exception("[direct] 随机重抽失败（%s）", kind)
            return ""
    return fn


def _enqueue(skill, prompt, text, source_image=False, seed=None,
             resample_fn=None, lora=None):
    """转译结果落队。返回要发回会话的文本；成功且回执已直发返回 ""。

    source_image=True → 垫本轮引用的那张图（传 "1"，下游 comfy_src.resolve
    解析；引用图取不到会报错回给用户）。**只有模型在改图 JSON 里自己填了
    `source_image` 才会是 True**（2026-10-06 起代码不再扫原话判）。
    seed 非空 → 复刻账本里那张图的种子（引用 HT 图换档场景），下游
    _resolve_seed 会校验范围；只往本机渠道传（NAI 拒收 seed）。
    resample_fn 非空 → 随机口令的「被拦静默重抽」钩子（见 _random_resample）。
    lora 非空 → 模型点名的 LoRA（2026-10-29 加，moss 单挂用）：原样透传给
    `_generate_image`。模型没填也不怕——名字留在 prompt 开头时，那边会自己抽
    （见 lora_slots.take_leading），两条路等效。
    """
    from app.tools.normal import generate_image as gi
    extra = {}
    if lora:
        extra["lora"] = lora
    if source_image:
        extra["source_image"] = "1"
    if seed:
        extra["seed"] = seed
    if resample_fn:
        extra["resample_fn"] = resample_fn
    try:
        result = gi._generate_image(
            prompt, skill=skill, _skip_confirm=True, **extra)
    except Exception:
        log.exception("[direct] 直接入队失败")
        return "生图请求没发出去，稍后再试。"
    log.info("[direct] 直达入队：%s %r（原话 %r）", skill, prompt[:50],
             text[:50])
    _note_desc("[直达生图] %s：%s" % (skill, prompt[:200]))
    if result.startswith(image_jobs.RECEIPT_SENT_MARK):
        return ""          # 回执已由工具直发，本轮闭嘴
    if result.startswith("错误："):
        return _humanize_error(result)
    return result


# 「anima多图」的触发词（2026-10-16 用户拍板的新渠道名）。**只认这一个写法**，
# 不给单字「anima」放行——「anima线条 / anima写实 / anima鬼针草」是**单张生图
# 渠道**的点名词（见 `_FIXED_CHAN_MAP` / `_FIXED_CHAN_RE`），单看「anima」会把
# 两者搅在一起。
_ANIMA_MULTI_RE = re.compile(r"anima\s*多图", re.I)


def _comic_style(text):
    """原话里点没点名「anima多图」→ "anima"；没点名 → ""（render 侧落回 silver）。

    代码判定，**不让模型复议**：模型习惯把「要好几张」一律当 silver 漫画（模板里
    写的就是那一条），交给它判容易把「anima多图」静默降级成 silver——同 2026-10-08
    「要 10 张漫画、模型填 skill=sd 被回落 silver」那次事故的性质。
    """
    return "anima" if _ANIMA_MULTI_RE.search(text or "") else ""


def _quote_as_brief(tags):
    """把「引用图的识图反推结果」包成一句**中文** brief 交编剧。

    ⚠️ **绝不许把英文 tag 直接当剧情**（2026-10-16 用户报的现场）。识图反推的
    tag 只是**画面描述**，编剧（`comic_story.write_story`）拿到一段没有剧情、
    没有中文的 tag，无从下笔就会去**照抄规范里的示例角色名**——实录：「，anima
    多图」+ 引用一张小猪图 → 出图是「和泉纱雾 + 和泉正宗（1boy）+ 猪」，固定块
    一字不差是 `skills/storyboard-prompt/skill.md` 的男女双人示例。

    用户拍板：**引用图 + 多图 = 把引用图里的内容当主角画成多格**。所以这里如实
    把图里的东西写成主角描述交出去，不回问用户、也不替它编剧情。
    返回 "" = 连画面都没认出来（调用方照常走「说清要画什么」那条提示）。
    """
    tags = (tags or "").strip()
    return ("主角就是引用图里的这个东西：%s" % tags) if tags else ""


def _enqueue_comic(comic, text):
    """漫画轮落队。语义与 `_enqueue` 完全一致：回执已直发返回 ""，否则返回要发的话。

    执行体就是 `generate_comic._generate_comic`——和工具路径同一份代码（闸门、
    剧本、整批后台渲染、回执直发都在里面），不在这儿重写一遍。
    """
    from app.tools.normal import generate_comic as gc
    from app import comic_story
    brief = (comic.get("brief") or "").strip()
    if not brief:
        return "漫画这条我没看清要画什么故事，把剧情再说一次。"
    # 渠道：契约里钉了就用钉的——**默认路**（没点名渠道）用户什么都没说，
    # 是 `_translate` 代码钉的 anima；没钉才看原话里有没有「anima多图」字样
    # （那是点名路）。`style` 空 = 落回 silver 漫画（默认 10 格）。
    style = (comic.get("style") or "").strip() or _comic_style(text)
    try:
        result = gc._generate_comic(brief, panels=comic.get("panels"), style=style)
    except Exception:
        log.exception("[direct] 漫画入队失败")
        return "漫画请求没发出去，稍后再试。"
    panels = comic_story.clamp_panels(
        comic.get("panels"), default=comic_story.default_panels(style))
    log.info("[direct] 漫画入队：%d 格 %r（style=%s，原话 %r）", panels, brief[:50],
             style, text[:50])
    if result.startswith("错误："):
        # 被闸拦掉（群聊禁 silver 漫画 / 生图关了 / 额度用完）= **一张都没画**，
        # 绝不能写进「本轮做了什么」的账本：那一行会被 `_recent_lines` 当事实
        # 回喂给下一轮，模型会照着「[直达漫画] 10 格」继续往下说。
        return _humanize_error(result)
    _note_desc("[直达漫画] %d 格：%s" % (panels, brief[:200]))
    if result.startswith(image_jobs.RECEIPT_SENT_MARK):
        return ""          # 回执已由工具直发，本轮闭嘴
    return result


# 资料包总上限。用户 2026-10-06 定的：一次搜索组合成 500~1000 字发给生图 API。
# 分两段拼：代码定点抽取（≤600 字，确定性）+ 搜索 Agent（≤600 字，补漏）。
# 真超了从**尾巴**截——尾巴是 Agent 那段，定点抽取是确定性的真值，不能动。
DOC_MAX_CHARS = 1000


def _prefetch_search(text):
    """组装一份资料包：代码定点抽取 + 搜索 Agent 补漏。失败返回 ""。

    **用户 2026-10-06 拍板：全部流程默认先走一遍搜索，再由最后的单次生图
    API 出提示词。** 所以搜索提到 `decide()` 入口统一做一次，`_translate`
    和 `_revise` 共用——原先只有 `_translate` 内部会搜，而改图管道 `_revise`
    根本不经过它，导致群里最常见的「图生图，角色换XX」从来没走过搜索。

    **同一天的第二轮改动**：用户原话「我们进行搜索应该是一套工作流，跑一次
    搜索，然后组合成差不多 500~1000 个字的这样一套东西发给这个生图 API，
    让它去可以参考这个写法」。所以这里变成两段：

      ① `search_tags.extract()` —— 代码扫库，**0 token、微秒级、不可能编造**。
         用户原话里字面出现的词直接定成 tag（连衣裙→dress、白色围裙→white_apron），
         还顺带给同族标签（dress → white_dress / floral_print_dress…）。
      ② `search_agent.search(hint=…)` —— 1 次 LLM，只补 ① 补不到的：
         同义词桥接、歧义角色、库外名词、联网。

    为什么 ① 放在前面：用户的目标是「AI 不要犯错」。确定性抽取错不了，而
    LLM 层要为每个词各跑一轮才等价——那正是 10-06 上午实测出 4,325 miss
    token / 单场 14,487 token 的原因。让代码干确定的活，LLM 只干需要判断的活，
    两边都变好。

    失败/查不到返回 ""，调用方按「没有资料」继续走原来的路——搜索是增强，
    不是主链路，它挂了不该让生图也挂。
    """
    if not SEARCH_ENABLED:
        return ""
    text = (text or "").strip()
    if not text:
        return ""

    parts, hint = [], ""
    try:
        from app.tools.normal.search_tags import extract
        got = extract(text)
        if got.get("doc"):
            parts.append(got["doc"])
            hint = got.get("hint") or ""
    except Exception:
        log.exception("[direct] 定点抽取失败，按无资料继续")

    if SEARCH_AGENT_ON:
        try:
            from app import search_agent
            doc = search_agent.search(text, hint=hint) or ""
            if doc:
                parts.append(doc)
        except Exception:
            log.exception("[direct] 前置搜索失败，按无资料继续")
    else:
        # 只提醒一次：确认线上真的按「extract → 生图 API」单请求在跑
        global _SEARCH_OFF_LOGGED
        if not _SEARCH_OFF_LOGGED:
            _SEARCH_OFF_LOGGED = True
            log.info("[direct] 搜索 agent 已关闭（DIRECT_SEARCH_AGENT=0），"
                     "只走 0 成本的定点抽取")

    out = "\n\n".join(parts)
    if len(out) > DOC_MAX_CHARS:
        out = out[:DOC_MAX_CHARS].rstrip() + "…"
    return out


# 「点名渠道最高优先级」的兜底话术（2026-10-16 用户拍板）。仅用于「模型已经
# 吐了漫画契约、但用户原话里点了渠道」这一种冲突，追加在**同一条内容**末尾
# 重问一次——同一份前缀，prompt cache 基本全命中，实测 0.1~0.3 秒。
# `%s` 出现两次：渠道 id（说给模型听的那一个）。
_COMIC_VETO_NOTE = (
    "\n\n【更正，以本条为准】不要用 generate_comic：用户已经点名了渠道 **%s**，"
    "点名渠道优先级最高，本轮就按这个渠道画**一张**（就算他嘴上说要好几张）。"
    "只输出 {{\"tool\": \"generate_image\", \"prompt\": \"英文提示词\", "
    "\"skill\": \"%s\"}}。\n")


def _retry_without_comic(content, named):
    """点名渠道时重问一次，只要单图 prompt。仍要漫画 / 拿不到 JSON → None。"""
    try:
        data = _ask(content + _COMIC_VETO_NOTE % (named, named))
    except Exception:
        log.exception("[direct] 重问（去漫画）失败")
        return None
    if not data or (data.get("tool") or "").strip() == "generate_comic":
        return None
    return data


# 点名渠道却只回了话 → 重问一次用。2026-10-17 现场：私聊「moss渠道 + 一大段
# JSON 提示词」，模型判成「贴资料给我看」，回了一句「收到，按你给的分镜描述
# 直接出图——不过画面裁切到喉部…」，**图一张没出**。同一句话在群里 21:05 /
# 21:11 都正常出图，说明不是渠道识别问题、是模型这一轮的意图判断摇摆。
# 用户口径：「都说了 moss 渠道和提示词了，直接生图即可了呀」。
_REPLY_VETO_NOTE = (
    "\n\n【更正，以本条为准】用户**点名了渠道 %s**，并且给了正文——这是下单，"
    "不是聊天。正文哪怕是一段 JSON / 元数据 / 英文串，也只是你写提示词的素材："
    "照它把画面转译成英文 prompt，直接输出 "
    "{{\"tool\": \"generate_image\", \"prompt\": \"英文提示词\", \"skill\": \"%s\"}}。"
    "**不许只回 reply、不许反问「要不要画」**。"
    "只有原话确实是在提问（「这个渠道怎么样」「能画什么」）才允许 reply。\n")


def _retry_force_prompt(content, named):
    """点名渠道却只回了话 → 重问一次，逼它出 prompt。

    仍坚持 reply（真在闲聊 / 真缺素材）返回 None，调用方按原话回。
    """
    try:
        data = _ask(content + _REPLY_VETO_NOTE % (named, named))
    except Exception:
        log.exception("[direct] 重问（要点名渠道出图）失败")
        return None
    if not data or not (data.get("prompt") or "").strip():
        return None
    return data


def _translate(text, history, skill=None, weighted=False, no_default=False,
               doc=None):
    """一次 LLM 调用 → 生图任务，或一句聊天回复。**所有提示词都由它产出**。

    2026-10-05 晚用户拍板重做：**只有一条模板 `_MASTER_TEMPLATE`**（人设 +
    工具列表 + 最近对话 + 用户原话），不再按渠道分模板、不再注入渠道专属规则。
    用户原话：「AI 本身就能胜任整个工作，我一直想通过硬编码指令去操控 AI，
    这本身就是错的。」

    返回值四态：
      - {"skill":…, "prompt":…}  → 要画**一张**，调用方入队。**没点名渠道时
                                   也是这一态**（2026-10-17 起默认 = silver）
      - {"comic":…}              → 要画**一批**（漫画 / 多图），调用方走
                                   `_enqueue_comic`。只有模型自己判出「用户要
                                   漫画 / 多图 / N 张」才会出现
                                   ⚠️ **点名了渠道（`skill` 入参非空）就没有这一态**
                                   ——点名渠道最高优先级，模型吐 comic 也会被
                                   按点名的渠道拉回单图（见下面 comic 契约那段）
      - {"reply": "…"}           → 模型判断这轮不用动手，直接把这句发出去
      - None                     → 调用没成功 / 没拿到有效内容，调用方兜底

    参数：
      skill        代码从用户原话里认出的渠道词，作为 chan_hint 明写给模型，
                   同时兜底（模型没给或给了个不存在的渠道时用它）。传 None
                   表示「没认出渠道词」，完全交给模型判。多个渠道词时，这里是
                   **从左到右第一个命中的那个**（2026-10-07 用户拍板）。
      weighted     保留形参：成品串（`::` 权号）现在与普通请求共用同一条模板，
                   权号规则写在模板的「提示词怎么写」段里。**不再影响模板选择。**
      no_default   True → 模型既没判出渠道、代码也没认出来时返回 None
                   （调用方回问），**不静默落默认档**烧一张错风味的图。
                   ⚠️ 只有成品串那条路传 True；普通轮次两边都判不出 = 走默认
                   渠道 silver 单图（见末尾那段）。
    """
    named = (skill or "").strip()
    # 搜索资料由调用方（`decide`）统一算好传进来——**一次请求只搜一次**，
    # 所有分支共用。没传就是空串，模板里那一格为空，照老路走。
    doc = doc or ""
    if named:
        # 点名渠道 = **最高优先级**（2026-10-16 用户拍板，原话：「用户明确点名
        # 渠道，必须使用该渠道去跑，出现冲突的时候，比如点名 nffa，然后又点
        # 多图，nffa 没有多图，我们默认以最开始的渠道来进行生图，点名渠道必须
        # 是最高优先级的」）。所以这里把话说死，别让它吐漫画契约；代码侧另有
        # 一道兜底（见下面 comic 契约那段）。
        hint = ("\n本轮开头点名了渠道：%s，就用它，优先级最高——按它画一张，"
                "不用 generate_comic（嘴上说要好几张也一样）。\n"
                "打了渠道名就等于下单：正文哪怕只是一段 JSON / 元数据 / 英文串 / "
                "分镜表，那也是他给的提示词素材，照它转译成 prompt 直接出图。"
                "（除非他确实在问这个渠道本身的事。）\n" % named)
    else:
        hint = ""
    content = _MASTER_TEMPLATE.format(
        recent=_recent_lines(history) or "（无）",
        text=text,
        search=(_SEARCH_HEADER.format(doc=doc) if doc else ""),
        chan_hint=hint)
    data = _ask(content)
    if not data:
        return None
    # ── 漫画（2026-10-08）：第三种形态 ───────────────────────────────
    # QQ 的 @ 轮不跑主 Agent 循环（见 `qq_bot` 的调度注释），`generate_comic`
    # 那个工具在 QQ 里调不到，漫画只能从这条 JSON 契约进来。
    # brief 是**中文剧情**，不套下面「prompt 必须英文 + 渠道白名单」那套校验：
    # 渠道由 `_comic_style` 从原话判定（silver / anima），格数由
    # `comic_story.clamp_panels` 按渠道取默认，都不需要在这儿再判一遍。
    # 模型若多吐一个 `skill`（模板里已经没有它了，但习惯难改）**直接丢掉**。
    if (data.get("tool") or "").strip() == "generate_comic":
        if named:
            # ── 点名渠道最高优先级（2026-10-16 用户拍板）──────────────────
            # comic 契约原先排在「渠道优先级」那行**之前**，而且是无条件接受，
            # 所以模型改判成漫画时点名的渠道会被整个丢掉。实录（私聊
            # 2509355624 22:02，原话「krea2 鬼针草，横屏，和泉纱雾」）：
            # 代码明明认出了 krea2-guizhencao 并写进 chan_hint，模型照抄上一轮
            # 「[直达漫画] 20 格：和泉纱雾…」的格式吐了 comic → 出 silver 漫画
            # 10 格。用户拍板：点名渠道必须最高优先级。
            # 补一次调用把话说死（带 cache，约 0.2 秒）；仍要漫画就退回用它给的
            # brief 出一张——宁可提示词差点，也不能把点名的渠道丢掉。
            again = _retry_without_comic(content, named)
            if again:
                log.info("[direct] 模型原判漫画，点名渠道 %s 优先 → 改出一张",
                         named)
                data = again
            else:
                brief = (data.get("brief") or data.get("prompt") or "").strip()
                log.warning("[direct] 重问后仍要漫画，按点名渠道 %s 出一张"
                            "（brief 当提示词）", named)
                return {"skill": named, "prompt": brief} if brief else None
        else:
            brief = (data.get("brief") or data.get("prompt") or "").strip()
            if not brief:
                return None
            return {"comic": {"brief": brief,
                              "panels": data.get("panels")}}
    user_prompt = (data.get("prompt") or "").strip()
    reply = (data.get("reply") or "").strip()
    if not user_prompt and named and reply:
        # ── 点名渠道却只回了话 = 大概率是模型判错了（2026-10-17 现场）──────
        # 私聊「moss渠道 + 一大段 JSON 提示词」→ 模型回「收到，按你给的分镜
        # 描述直接出图…」，图一张没出。同一句话在群里 21:05 / 21:11 都正常，
        # 所以不是渠道识别问题。用户口径：「都说了 moss 渠道和提示词了，
        # 直接生图即可了呀」。重问一次把话说明白，**仍然回话就照它回话**
        # （真在问问题的那种不硬掰成出图）。
        again = _retry_force_prompt(content, named)
        if again:
            log.info("[direct] 模型原判回话，点名渠道 %s → 重问后出图：%.60s",
                     named, (again.get("prompt") or "")[:60])
            data = again
            user_prompt = (data.get("prompt") or "").strip()
            reply = (data.get("reply") or "").strip()
        else:
            log.warning("[direct] 点名渠道 %s 仍只回话、没出图：%.80s",
                        named, reply)
    if not user_prompt:
        # 模型选择不动手（聊天 / 问答 / 只要提示词）→ 把话原样带出去。
        if not reply:
            return None
        # 回话轮以前一点日志都不留，出问题时没法查「它为什么没画」
        # （2026-10-17 就是靠猜）。这里留一行，带点名渠道和回复开头。
        log.info("[direct] 模型判回话（点名 %s）：%.60s", named or "无", reply)
        # 聊天回复也落史暂存：qq_bot 送出后 pop 出来写进 history，下一轮的
        # 最近对话里才有 AI 说过的话（生图轮走 [直达生图] 那条路，不经这里）。
        try:
            _QA_LAST[qq_api.current_session_key()] = (text, reply)
        except Exception:
            log.exception("[direct] 聊天回复落史暂存失败")
        return {"reply": reply}
    # 渠道优先级：**代码点名的（skill 入参）> 模型判的**。用户开头写了 nai
    # 就要 nai，模型不许在这一步复议顶掉（2026-10-05 私聊事故的另一半——
    # 以前是 `模型值 or 代码值`，等于把点名的渠道交给模型重新裁决）。
    judged = (data.get("skill") or "").strip()
    # 模型有时填的是**命令词**而不是渠道 id（模板里写着「sd = image_gen_v1」，
    # 它就直接填 `sd`），而白名单里只有 id → 会静默降级成默认档。
    # 2026-10-08 实录：用户要 10 张漫画，模型填 skill="sd"，回落 silver。
    if judged:
        judged = _fixed_chan(judged, text)
    out_skill = named or judged
    if out_skill in _allowed_skills():
        return {"skill": out_skill, "prompt": user_prompt}
    allowed = _allowed_skills()
    fallback = (named if named in allowed else
                judged if judged in allowed else "")
    if fallback:
        log.warning("[direct] 未知渠道（点名 %r / 模型 %r），改用 %s",
                    named, judged, fallback)
        return {"skill": fallback, "prompt": user_prompt}
    if no_default:
        # 成品串：判不出就回问，绝不静默换默认档烧一张错风味的图。
        return None
    # ── 谁也没点名渠道 → 默认出图方式 = silver 单图（2026-10-17 用户拍板）──
    # 用户原话：「帮我把 silver 作为默认渠道吧，现在这个 anima多图效果太差了」。
    # 这是把 2026-10-16「默认 = anima多图 3 张」的口径**翻回来**：anima 那条
    # 批量工作流要多喂一段剧本、画质也不稳，用户实测后否掉了。
    #
    # anima多图 **没下架**：原话点名「anima多图」仍走 `_ANIMA_MULTI_RE` 专路，
    # 只是不再是「什么都没说」时的落点。
    #
    # 这里只把 skill 顶成 `_FALLBACK_SKILL`（silver），提示词用模型刚写好的
    # 那份英文 tag——不走 comic 契约，就是**一张**普通单图。
    if named:
        # 点名了、但那个 skill 目录不存在（改名 / 忘了带走 skills / 进程没重启
        # 而目录已经被换掉）。**绝不能跟「没点名」共用一条日志**：2026-10-18
        # 00:36 用户写「moss4 渠道」拿到的正是「没点名渠道，走默认 silver」，
        # 害得排查方向跑偏到分辨率上——真相是进程还是 00:20 启动的旧代码，
        # 而 00:28 目录已把 `moss4` 改成 `moss`，旧 id 自然查不到。
        log.warning("[direct] 点名的渠道 %r 不存在（skills 里没有；查目录名 /"
                    " 是否重启过），退回默认 %s：%r",
                    named, _FALLBACK_SKILL, text[:50])
    else:
        log.info("[direct] 没点名渠道，走默认渠道 %s：%r",
                 _FALLBACK_SKILL, text[:50])
    return {"skill": _FALLBACK_SKILL, "prompt": user_prompt}


def _recall_tags(data_urls, numbered=True):
    """引用图 → 英文提示词反推（「反推返回」「反推后生成」共用）。

    输出形态是**英文混合式**（标签打底 + 短语补空间关系），不再是纯 tag 串——
    纯 tag 表达不了「哪一侧、什么材质」，而本机 88% 的渠道是吃得下句子的
    Qwen 系编码器。详见 `_TAGS_PROMPT` 上方注释。

    走 .env 的识图配置（VISION_PROVIDER/VISION_MODEL，2026-10-05 起是
    火山 doubao-seed-2.1-turbo；此前钉死 DeepSeek 官方）。识图调用本来
    就不过降级链，不指定 provider = 跟着配置走。最多看 3 张，全部失败
    返回 ""。

    numbered=True（回给用户看）多图带「（第 N 张）」前缀；False（结果
    直接当生成提示词入队）多图用「, 」拼接——中文前缀进提示词是污染。
    """
    from app.vision import describe
    urls = (data_urls or [])[:3]
    outs = []
    for i, data_url in enumerate(urls, 1):
        try:
            text = (describe(data_url, prompt=_TAGS_PROMPT) or "").strip()
        except Exception:
            log.exception("[direct] 引用图反推失败（第 %d 张）", i)
            continue
        if not text:
            continue
        outs.append("（第 %d 张）%s" % (i, text)
                    if numbered and len(urls) > 1 else text)
    return ", ".join(outs) if not numbered else "\n\n".join(outs)


# 上游拒答时给用户的话（2026-10-07）：绝不能把「The request was rejected…」
# 那种英文报错当反推结果发出去（实测一天 7 次，群 3 私聊 4）。
_REVERSE_REFUSED_TEXT = "这张图我读不出来（内容没过审核），换一张再试。"


def _reverse_text(tags):
    """反推结果拼成回复；空 tags / 上游拒答 都给兜底提示。

    ⚠️ 拒答判据放在这**唯一出口**：反推有三条路（`_REVERSE_RE` 硬触发、
    改图兜底的 `_is_english_tags`、JSON 里的 `reverse` 字段），它们全都汇到
    这里，所以在这拦一道就全覆盖了。背景见 vision.looks_like_refusal 上方。
    """
    tags = (tags or "").strip()
    if not tags:
        return "没认出这张图，重发一次试试。"
    from app import vision
    if vision.looks_like_refusal(tags):
        return _REVERSE_REFUSED_TEXT
    return _REVERSE_HEADER + "\n" + tags


# 「提取提示词」直通（2026-10-07 用户拍板，收窄到**这 5 个字**）：命中 →
# 引用/正文里带 HT 编号时，直接把这张图当初真实用的提示词发回去，零 LLM、
# 比让 AI 现推准。这是**唯一保留**的硬编码话术，因为它落在自家账本上、足够确定。
#
# 用户原话：「我就只需要这一句话就行了…提取提示词…它只需要这 5 个字，其他的
# 全部给我删掉，其他的全部都给我跑 AI」「你永远无法揣测用户意图…交给大模型，
# 至少能覆盖 99%」。所以原先那条 `_PROMPT_ASK_RE`（动词 + 提示词/词条…）
# 已删：用户说「我要这个图片的提示词」「这个提示词是什么」这类，正则永远盖
# 不全，一律交给 AI（工具 `recall_image` 与模板已覆盖）。
# 只认裸的「提取提示词」（可带标点/空格）。账本没中（引用的不是自家图）就
# 不劫这轮，照旧走下面的流程。
_PROMPT_BARE_RE = re.compile(
    r"^\s*提取提示词\s*[。.!！~～？?]*\s*$")

# 「反推提示词」硬触发（2026-10-07 用户拍板，**零 AI 调用**）：原话
# 「我们可以设置指令，反推提示词，然后触发后调用反推提示词的模型」。
#
# 和 `_PROMPT_BARE_RE`（提取提示词 = 翻自家账本）是**两回事**：
#   - 提取提示词 → 账本里躺着这张图当时真跑的串，直接发，最准；
#   - 反推提示词 → 账本没有（引用的是别人的图）时，让**识图模型**用自己的
#     提示词（_TAGS_PROMPT）看图出英文 tag。
# 以前后者被塞进改图管道 `_revise` 的 JSON 模板（主人设），识图模型既要读图
# 又要挑渠道、吐 JSON，结果要么解析失败（「改图请求没解析出来」）、要么在
# 关键词轮被 `at_me=False` 静默丢掉（2026-10-07 群 1103174141 两条实录）。
# 现在明确命中就直出，不再过主人设。用户原话：「识图模型是要单独把提示词
# 跑出来的」「纯英文 tag，原样发」。
#
# 只认「反推」二字（可带「提示词/词条」，可带前后缀和标点）。前面允许一句
# 引导语（「识别图片，」「大大怪，」「帮我」），后面允许一句跟进指令
# （「然后生成」「看看」）——真实群里就是这么说的（2026-10-07 实录：
# 「识别图片，反推提示词」「大大怪，反推提示词，然后生成」）。
# **不动**裸的「识别图片」这类含糊说法——那交给 AI（否则「识别图片里写了
# 什么字」会被劫）。
_REVERSE_RE = re.compile(
    r"^\s*(?:[^，,。.!！~～？?\n]{0,12}[，,]\s*)?"
    r"(?:请|帮我|麻烦)?\s*"
    r"反推\s*(?:一?下\s*)?(?:这[些张张]图\s*的?\s*)?"
    r"(?:提示词|提示語|词条|詞條|tag|tags)?\s*"
    r"(?:[，,。.!！~～]?\s*(?:然后|再|接着)?\s*(?:生成|出图|画|看看))?"
    r"\s*[。.!！~～？?]*\s*$",
    re.I)


def _ledger_hit(source):
    """引用正文/原话里的 HT 编号 → 账本 (提示词, 渠道, 种子)。没中返回 ("", "", "")。

    账本是本机器人每次生图落下的账：编号对应当时**真实用掉**的提示词，
    比看图现推准。引用别人的图查不到，返回空。seed 是那张图生下来用的
    种子（空串 = 没记录），供「换档复刻」用。
    """
    try:
        tags = image_log.find_tags(source or "")
    except Exception:
        log.exception("[direct] 账本查询失败")
        return "", "", ""
    if not tags:
        return "", "", ""
    row = image_log.lookup(tags[0]) or {}
    return ((row.get("prompt") or "").strip(),
            (row.get("skill") or "").strip(),
            str(row.get("seed") or "").strip())


# 认 seed 的本机渠道前缀（与 generate_image 工具描述里那句清单同源）：
# NAI 不认 seed（传了直接报错），所以换档复刻只往这些渠道带种子。
# ⚠️ `krea2` 这条是**前缀**匹配，`krea2-yoneyama` 等 5 条靠它覆盖
# （2026-10-08 krea2 系拆 5 条后没改这里，改的是注释）。
_LOCAL_SEED_SKILL_RE = re.compile(r"^(anima_|hd_|qwen_image_v1|qwen30|image_gen_v1|krea2|nffa|cunny|miao|moss)")


def _revise(text, data_urls, history, channel=None, at_me=False, doc=None):
    """改图管道：引用图 + 意见 → 一次调用 → 重跑。

    引用带图改图**一律真识图**（2026-10-05 用户拍板）：账本里存的是当时那句
    提示词，和画面实际内容可能已经对不上（背景改透明那次没看图，出图就不是
    用户要的）。账本提示词降级为「最可信旁证」写进 prompt，不再是唯一来源。

    垫不垫图（`source_image`）**由模型在这一次调用里判**：它输出 JSON 里带
    `source_image: 1` 才入队垫图，判据是模板里的【图生图】段（**明说「图生图 /
    照着画 / 复刻」才垫**；只提需求 = 改词重画）。代码不扫原话认机制词。
    ⚠️ 判错了也不会报错：渠道没有 i2i 骨架时 `generate_image` 会**丢掉
    source_image、按文生图重画**（2026-10-11），别在模板里教模型去等一句拒收。

    **渠道**：点名渠道 > 模型填的 > 账本原渠道 > 按垫不垫图走默认
    （垫图 → `qwen_image_v1`，不垫图 → silver，2026-10-17 用户拍板）。
    2026-10-06 曾禁止「因为要垫图就换渠道」，现在只放开**判不出渠道时**这一个
    落点——点名/模型填的渠道一律不动，那不是静默降档，是补默认值。

    **「再来一张 / 重跑一版」不再零调用直出**（2026-10-17 用户拍板「让 AI 自己
    去判断如何做」）：账本真词 + 会话最近一次生图（`{recent}`）当事实给模型，
    由它决定原样重跑还是改一处。代价是 +1 次 LLM。

    返回 None = 模型判定这轮不是修改请求（夸奖、闲聊、问别的）：@ 轮回反推
    文本、关键词轮静默，由本函数内部处理。
    """
    session_key = qq_api.current_session_key()
    quoted = (qq_api.current_quoted_text() or "").strip()
    # 2026-10-17 用户拍板：**删掉「再来一张」的关键词早退**（原先这里
    # `_AGAIN_RE` 命中 + 内存 `_LAST_JOB` 有值就零调用直出）。它有两个毛病：
    #   ① 抢在 AI 前面替用户决定「这是重跑、不是改」（用户原话：「让 AI 自己去
    #      判断如何做」，代码只该给事实、不该做决定）；
    #   ② `_LAST_JOB` 是进程内字典，bot 一重启就空 → 20:18:30 私聊「重跑一版」
    #      + 引用带 HT 编号的回执，明明账本查得到真词，却回了「没找到最近一次
    #      生图的记录」。现在这条路改由下面两件事承担：账本（`_ledger_hit`，
    #      重启不丢）+ 会话最近一次生图（当**事实**写进模板 `{recent}`），
    #      判「重跑还是改」的活交回模型。代价 = 多 1 次 LLM（+1~2 秒）。
    last = _last_job(session_key)
    anchor_prompt, anchor_skill, _anchor_seed = _ledger_hit(quoted + " " + text)
    own = bool(anchor_prompt)        # 引用的**是不是自家图**（账本命中）
    if own:
        anchor_note = "是这张图当时的真实提示词（账本可查），最可信"
        recent = ""
    else:
        anchor_skill, anchor_prompt = "（无）", "（无）"
        anchor_note = "没有（引用的不是本机器人画的图），忽略此项，以画面为准"
        # 账本查不到、但本会话刚画过一张 → 那串词**可能**就是用户指的东西
        # （比如他把图重新发出来而不是引用回执）。给模型当旁证，用不用它自己判：
        # 说得像「接着上一轮」就照它重跑，说得像在改这张图就无视它。
        recent = (
            "本会话最近一次生图（**可能**就是引用的这张，也可能无关，自己判）：\n"
            "渠道 %s\n提示词：%s\n" % (last["skill"], last["prompt"])
            if last else "")
    ask = _REVISE_TEMPLATE.format(
        search=(_SEARCH_HEADER.format(doc=doc) if doc else ""),
        anchor_note=anchor_note,
        last_skill=anchor_skill,
        last_prompt=anchor_prompt,
        recent=recent,
        lang=_prompt_lang(channel or anchor_skill),
        text=text)
    from app.llm import call_llm
    from app.vision import describe

    def _ask_image(question):
        """改图管道的那一次调用（重问时按同一形态重放）。"""
        if own:
            # ── 自家图：账本优先，**不预先识图**（2026-10-07 用户拍板）──────
            # 账本里躺着当时真跑的那段提示词，和画面同源，改「发色红→银」这类
            # 需求直接在那段文字上改就行，没必要每次都看一眼图。用户原话：
            # 「没有必要每一次用户引用我的生成结果的图片的时候，就一直在识图」。
            # 走**纯文本**调用（`call_llm`，不是 `describe`），零识图开销。
            return call_llm([{"role": "user", "content": question}])
        # 别人的图：没有账本可依，一律真识图（1 次）。
        # 不指定 provider = 跟识图配置走（.env，现在是 doubao-seed-2.1-turbo）。
        return describe(data_urls[0], prompt=question)

    try:
        reply = _ask_image(ask)
    except Exception:
        log.exception("[direct] 改图调用失败")
        return "改图请求没发出去，稍后再试。"
    data = _extract_json(reply)
    if not data:
        # 模型没吐 JSON（回了散文 / 一段 tag）。这类回复在带图的轮次里**几乎
        # 总是"它其实看懂了图、只是没按 JSON 说"**——2026-10-07 群 1103174141
        # 12:02:01 实录：识图模型回了 60 字散文，代码直接回「改图请求没解析
        # 出来」。兜一道：识别出来本来就是英文 tag → 当成反推发回去；否则
        # 再退一步，用识图模型自己的提示词（_TAGS_PROMPT）重跑一次反推。
        # 这条兜底只在 data_urls 非空时到得了（调用方保证）。
        if _is_english_tags(reply):
            return _reverse_text(reply.strip())
        fallback = _recall_tags(data_urls)
        if fallback:
            return _reverse_text(fallback)
        return "改图请求没解析出来，换个说法再试（如「手改成插兜」）。"
    # ── 漫画 / N张（2026-10-16）：带引用图的轮次也能画一批 ──────────────
    # 原先只有 `_translate` 认 `generate_comic`，`_revise` 不认；又因为**带引用图
    # 一律走 `_revise`**（见 `decide` 的「引用图轮」），所以凡带图就再也吐不出
    # 漫画——用户「silver 10张 漫画」+ 引用一张图 → 落成 silver-hd 单图
    # （19:44 私聊 455242826 实录）。模板已补这条路，这里接住它。
    # 渠道不进 JSON：`_enqueue_comic` → `_comic_style` 从原话判（silver / anima）。
    if (data.get("tool") or "").strip() == "generate_comic" and channel:
        # 点名渠道最高优先级（2026-10-16 用户拍板，口径与 `_translate` 完全一致，
        # 只是这里的调用可能带图、得按同一形态重放一次识图）。重问后它就落到
        # 下面**通用改图分支**（prompt / reverse / source_image 那套），渠道由
        # `channel or 模型填的` 定格——点名的那个赢。
        try:
            again = _extract_json(_ask_image(
                ask + _COMIC_VETO_NOTE % (channel, channel)))
        except Exception:
            log.exception("[direct] 改图重问（去漫画）失败")
            again = None
        if again and (again.get("tool") or "").strip() != "generate_comic":
            log.info("[direct] 模型原判漫画，点名渠道 %s 优先 → 改出一张",
                     channel)
            data = again
        else:
            brief = (data.get("brief") or data.get("prompt") or "").strip()
            if not brief:
                return "改图请求没解析出来，换个说法再试（如「手改成插兜」）。"
            log.warning("[direct] 重问后仍要漫画，按点名渠道 %s 出一张"
                        "（brief 当提示词）", channel)
            data = {"prompt": brief}
    if (data.get("tool") or "").strip() == "generate_comic":
        brief = (data.get("brief") or "").strip()
        if not _CJK_RE.search(brief):
            # ⚠️ **识图反推的英文 tag 绝不许原样当「剧情」喂编剧**（2026-10-16 修）。
            # 带图轮次的 `prompt`（以及抄错栏时的 `brief`）都只是识图反推的
            # 画面 tag（`no humans, animal focus, pig, pink pig...`）——原样递过去，
            # 编剧无从下笔就去**照抄规范示例里的角色名**：实录引用一张小猪图 +
            # 「anima 多图」→ 出图是「和泉纱雾 + 和泉正宗（1boy）+ 小猪」，固定块
            # 一字不差是规范里的男女双人示例。
            # 用户拍板：**引用图 + 多图 = 把引用图里的内容当主角画成多格**。
            # 所以这里把画面描述包成一句中文说明交出去——不回问用户，也不拿
            # 英文 tag 硬当剧情（那样编剧同样无从下笔）。
            quote = (data.get("prompt") or "").strip() or brief
            brief = _quote_as_brief(quote)
            if not brief:
                return "漫画这条我没看清要画什么故事，把剧情再说一次。"
        return _enqueue_comic({"brief": brief, "panels": data.get("panels")}, text)
    rev = (data.get("reverse") or "").strip()
    if rev:
        # 模型判定「这轮不是修改请求」→ 把它识别出的 prompt 发回去。
        #
        # ⚠️ 2026-10-07 修：以前是 `_reverse_text(rev) if at_me else None`，
        # 关键词轮（不是 @，只是命中触发词）会**静默丢掉整段反推**。群
        # 1103174141 12:02:58 实录：识图成功出了 598 字、JSON 也解析了，
        # 结果因为 at_me=False 一个字都没发出去（用户报的"后台有日志但群里
        # 没回复"）。判据修正：**能走到这里说明 data_urls 非空**（调用方已
        # 保证），即"用户确实给了图、模型确实读了图"，那就没有理由不发。
        # 关键词轮本来就是"用户在叫我"（冷却闸只管主动接话，见 qq_bot），
        # 丢回复不是止刷屏、是故障。
        return _reverse_text(rev)
    # 垫不垫图 = **模型在 JSON 里说的算**（2026-10-06 拆掉代码扫原话认机制词，
    # 判据改写在 `_REVISE_TEMPLATE` 的【图生图】段）。模型没填 / 填 0 = 文生图。
    si = data.get("source_image")
    source_image = si is True or str(si or "").strip() == "1"
    prompt = (data.get("prompt") or "").strip()
    if not prompt:
        # ⚠️ 空 prompt **不一定是失败**：`qwen-restore` 的修复指令内置在工作流
        # 里，模板明说「没提就空着」，模型交空串是对的。以前一律回「改图请求
        # 没解析出来」——用户「引用图 + 修复」就永远走不通（2026-10-17 日志：
        # 裸「修复」+ 引用图 3 次全落这句）。判据是**模型自己填的渠道能不能
        # 空 prompt**，不是代码拿词表猜他想干什么。
        own_skill = (channel or (data.get("skill") or "").strip())
        if source_image and own_skill == "qwen-restore":
            pass
        else:
            return "改图请求没解析出来，换个说法再试（如「手改成插兜」）。"
    skill = channel or (data.get("skill") or "").strip()
    if skill not in _allowed_skills():
        skill = anchor_skill if anchor_skill in _allowed_skills() else ""
    if not skill:
        # 判不出渠道 → 按「垫不垫图」分两支（2026-10-17 用户拍板，把 10-06 的
        # 决定改回来：「Anima 的图生图没有 Qwen 好用，图生图只需要一个 Qwen
        # 的途径」）：
        #   垫图   → `qwen_image_v1`（本机唯一真 i2i 骨架里最合用的那条）
        #   不垫图 → `_FALLBACK_SKILL`（silver 单图）
        # 2026-10-16 那版把两支合成了一条（一律 silver），是当时「默认渠道
        # 从 anima多图 换回 silver」顺手带下去的副作用——silver 的骨架锁的是
        # 构图，改词重画够用，但用户明说图生图时他要的是 qwen。
        skill = _I2I_FALLBACK_SKILL if source_image else _FALLBACK_SKILL
    _remember_job(session_key, skill, prompt)
    return _enqueue(skill, prompt, text, source_image=source_image,
                    lora=(data.get("lora") or "").strip())


def decide(own_text, history, voluntary, data_urls=None, at_me=True):
    """直达管道入口。返回值：
    - None      ：不接管（总开关关闭 / 主动接话轮 / 裸 @ 空消息 / 转译失败）
    - ""        ：已接管但闭嘴（引用图 + 夸奖这类「不是修改请求」的反推轮，
                  吞掉整轮，绝不让它掉回 agent 接话）
    - 其他文本  ：接管并把这段发回会话（生图回执 / 聊天回话 / 反推 / 菜单）

    2026-10-05 23:xx 用户拍板：**删掉菜单触发与 `_INTENT_RE`/`at_me` 闸**——
    「AI 识别用户的意图，是画图就画，是聊天就回话」。纯文本轮一律过一次
    `_translate`（人设 + 工具 + 历史 + 原话），AI 判不了才不接管。用户原话：
    「别人艾特大大怪，大大怪给他一个回复」「正常交流就行了，问一次回一次」。
    """
    text = _strip_own_names(_strip_attribution(own_text))
    # 「横屏 / 横版 / 横图」是出图方向指令，不是画面内容 → 抠掉再给 AI 看
    # （判据在 image_jobs，读的是原话，所以这儿抠掉不影响横屏生效）。
    text = _LANDSCAPE_STRIP_RE.sub(" ", text).strip()
    if _IMG_ONLY_RE.match(text):
        text = ""       # 裸图（只发图没说话）：见 _IMG_ONLY_RE 处的拍板
    session_key = qq_api.current_session_key()
    quoted = (qq_api.current_quoted_text() or "").strip()
    # ── 「/直通-」零 token 直出（放在最前，比菜单更优先）────────────────
    # 用户明确带前缀 = 拍板「这是下单」→ 跳过 AI、跳过搜索，直接入队。
    # 渠道认不出 → 返回哨兵 ("")，**落回下面原管道**（严格匹配，不猜）。
    if _DIRECT_RE.match(text):
        d_skill, d_prompt, d_err = _parse_direct(text)
        if d_err:
            return d_err
        if d_skill:
            _remember_job(session_key, d_skill, d_prompt)
            # 本轮带图（引用了 / 自己刚发）→ 垫这张图（qwen 图生图就靠这个）。
            # 不带图 = 纯文生图，跟平时一样。source_image="1" 由 comfy_src
            # 在生成时解析成「本轮引用的那张」。
            return _enqueue(d_skill, d_prompt, text,
                            source_image=bool(data_urls))
        # d_skill == ""：渠道段认不出 → 不直通，继续往下交 AI。
    # 详细使用指南（/菜单、使用指南）：零 LLM，常量直回。比菜单判断更前，
    # 因为「/菜单」含「菜单」二字，得先于短菜单分支。
    if _GUIDE_RE.match(text):
        return GUIDE_TEXT
    # 用户**明确**要菜单（「菜单」「/菜单」「help」）→ 零 LLM 直回常量。
    # 2026-10-05 用户拍板：**删掉「命中触发词/关键词就回菜单」的自动兜底**。
    # 普通轮次一律交给 AI 判意图——是画图就画，是聊天/提问就回话，不再用
    # 硬编码菜单吞掉整轮。用户原话：「AI 识别用户的意图…不要搞这个菜单触发了」。
    if _MENU_RE.match(text) and not data_urls:
        return MENU_TEXT
    # 裸 @ / 空消息：没内容可判 → 不接管，交回上层。
    if not text and not data_urls:
        return None
    # 只发图、没说话（_IMG_ONLY_RE 把正文归一成空）→ 反推这张图的提示词。
    # 自家图先查账本（零调用最准），别人的图才识图（1 次）。
    # ⚠️ 2026-10-07：用户明确要求**保留**这条行为（群里单发图 = 想要词条）。
    # 私聊场景的「裸图不触发」改在 `_should_reply`（私聊只认文本）里拦，
    # 不走这里——这里照旧反推，别动。
    if not text and data_urls and ENABLED and not voluntary:
        own_prompt, _skill, _own_seed = _ledger_hit(quoted)
        if own_prompt:
            return _reverse_text(own_prompt)
        return _reverse_text(_recall_tags(data_urls))
    if not ENABLED:
        return None
    # 主动接话轮（没人 @ 它）不进管道，行为维持原样。
    if voluntary:
        return None

    # ── 「反推提示词」直通（2026-10-07，零 AI 调用）──────────────────
    # 明确命中 + 本轮带图 → 直接让识图模型出词、原样发，**不经过主人设、
    # 不经过搜索、不经过改图管道**。放在搜索/引用图分支之前，才能真正零调用。
    # 自家账本能中的（引用的是本机器人画的图）优先走账本——那段是当时真跑的
    # 串，比看图现推准。没带图（纯文字「反推提示词」）→ 不劫这轮，交回下面
    # （AI 会问一句「哪张图」）。quoted 也算带图线索：QQ 引用图时正文里没有
    # data_urls，但 quoted 里有 HT 编号。
    if _REVERSE_RE.match(text):
        if data_urls:
            own_prompt, _skill, _own_seed = _ledger_hit(quoted + " " + text)
            if own_prompt:
                return _reverse_text(own_prompt)
            return _reverse_text(_recall_tags(data_urls))
        own_prompt, _skill, _own_seed = _ledger_hit(quoted + " " + text)
        if own_prompt:
            return _reverse_text(own_prompt)

    # ── 随机口令（2026-10-05）：/随机萝莉 /随机兽耳 /随机女仆 /今日老婆 ──
    # 策展词池直拼提示词，零 LLM；走兜底单图渠道入队，额度闸门照常拦。
    # ⚠️ **故意不跟默认的多图**（2026-10-16 换默认时没动这里）：`resample_fn`
    # 只在单图那条 worker 上生效——被审核拦下静默换一条重抽是这个玩法的命根
    # 子，comic 批量路没有这个钩子，改过去等于把「重抽」丢掉。要一起改成 3 张
    # 就得先在批量路上把重抽做出来。
    m = _RANDOM_CMD_RE.match(text)
    if m:
        theme = m.group(1)
        prompt = random_tags.sample_prompt(theme)
        _remember_job(session_key, _FALLBACK_SKILL, prompt)
        return _enqueue(_FALLBACK_SKILL, prompt, text,
                        resample_fn=_random_resample(theme))
    if _WAIFU_CMD_RE.match(text):
        _cn, prompt = random_tags.draw_waifu()
        _remember_job(session_key, _FALLBACK_SKILL, prompt)
        return _enqueue(_FALLBACK_SKILL, prompt, text,
                        resample_fn=_random_resample("waifu"))
    # 「/更多渠道」：全部跑法罗列（零 API）。关键词命中即可（见 _MORE_CHAN_RE）。
    if _MORE_CHAN_RE.search(text):
        return MORE_CHAN_TEXT

    # 「提取提示词」直通（2026-10-07 用户拍板收窄到**就这 5 个字**）：命中 →
    # 把这张图当初真实用的提示词发回去，零 LLM、比让 AI 现推准。
    # 引用的图带 HT 编号 → 账本直通（这是唯一保留的硬编码，因为它足够确定）。
    # **其余一切说法**（「我要这个提示词」「这图提示词是什么」「给我词条」…）
    # 一律交给下面的 AI 判——用户原话：「你永远无法揣测用户意图…你交给大模型，
    # 至少能覆盖 99%」。模板/工具（`recall_image`）已覆盖这类请求。
    if _PROMPT_BARE_RE.search(text):
        prompt, skill, seed = _ledger_hit(quoted + " " + text)
        if prompt:
            extra = ""
            if skill:
                extra += "渠道：" + skill + "\n"
            if seed:
                extra += "种子：" + seed + "\n"
            return ("这张图当初用的提示词：\n" + prompt
                    + ("\n" + extra if extra else ""))

    # ── anima多图 专路（2026-10-16 用户拍板）──────────────────────────
    # 用户**原话里点名**「anima多图」→ 一律走多图批量（`_anima_multi_batch`：
    # 双模型两段、默认 3 张）。必须放在 `_parse_channel` 和「引用图轮 / 渠道词轮」
    # **之前**拦下来，否则：
    #   ① `anima` 会被 `_TIER_RE` 当档位词吃掉（≡三档 → hd_3_clear），代码还会
    #      把 hd_3_clear 当 chan_hint 明写给 AI，AI 就永远不出 comic 契约
    #      （2026-10-16 群 1104825153 实录：原话「anima 多图」→ 直达入队
    #      hd_3_clear，用户原话「我都明确是多图了，还是走的 clear」）；
    #   ② 带引用图时会被改图管道 `_revise` 截走，出的仍是**单张**。
    # 为什么敢在这儿用代码定渠道：它是**点名词**（跟 nai/sd/anima线条 同一类），
    # 「代码判渠道」本来就是既有设计；提示词创作仍在 `write_story` 那层由 LLM
    # 把这份中文需求扩写成 tag，AI 没有被绕开。
    if _ANIMA_MULTI_RE.search(text):
        body = _ANIMA_MULTI_RE.sub(" ", text)
        body = re.sub(r"^[\s，,、:：]+|[\s，,、:：]+$", "", body)
        quoted_txt = quoted.strip()
        if quoted_txt and not _PLACEHOLDER_QUOTE_RE.search(quoted_txt):
            body = (quoted_txt + " " + body).strip() if body else quoted_txt
        if not body and data_urls:
            # ⚠️ 反推的是**画面 tag**，不是剧情。直接当 brief 递编剧，编剧无从
            # 下笔就会照抄规范示例里的角色名（2026-10-16 实录：「，anima 多图」
            # + 引用小猪图 → 和泉纱雾 + 和泉正宗 + 猪）。包一句中文再交出去。
            body = _quote_as_brief(_recall_tags(data_urls[:3], numbered=False))
        if not body:
            return ("「anima多图」后面跟上你想要的需求（画什么、谁、什么场景），"
                    "我一次给你出几张。")
        m = re.search(r"(\d+)\s*[张格]", body)
        panels = int(m.group(1)) if m else None
        return _enqueue_comic({"brief": body, "panels": panels}, text)

    ch, desc = _parse_channel(text)
    if ch is None and at_me:
        # 开头的 ASCII 词代码认不出（「MAI 正面提示词…」这种手滑的渠道词）：
        # 只贴得回一个渠道就直接锁定，几个都像就回问一句——不赌、也不掉回
        # 默认档。裸英文 tag 轮和问句轮不会被它碰（判据见 _lead_typo_channel）。
        typo, typo_desc, cands = _lead_typo_channel(text)
        if typo:
            ch, desc = typo, typo_desc
        elif cands:
            return _LEAD_AMBIGUOUS_TEXT % " / ".join(cands)

    # ── qwen-charsheet 专路：引用图 = 角色参考图，直接当 source 入队 ──
    # 这个渠道**没有纯文生图模式**：TextEncodeQwenImage21 必须吃一张参考图
    # （images.image_1）才能编码。用户「引用一张图 + 说 qwen 角色设定表」期望的是
    # 「拿这张图出角色设定表」，不是「把这张图识图反推再文生图重画」（那条路
    # 会把图当「要复刻的提示词」、source_image 却空着，最后撞上「需要参考图」闸）。
    # 所以在下面的「引用图轮」之前拦截：有图就强制 source_image=1，prompt 用用户
    # 自己的描述（没有就留空，版式指令全在工作流内置静态提示词里）。
    # 没引用图不在这里拦，落到通用路径 → generate_image 的「需要参考图」闸给报错。
    if ch == "qwen-charsheet" and data_urls:
        prompt = (desc or "").strip()
        _remember_job(session_key, ch, prompt)
        return _enqueue(ch, prompt, text, source_image=True)

    # ── qwen-restore 专路：引用图 = 要修复的原图，直接当 source 入队 ──
    # 同 qwen-charsheet 的道理（2026-10-10 加，用户口径「引用图片 + qwen修复
    # 就触发了」）：本渠道**没有文生图模式**，参考图就是被修复的那张原图。
    # 而下面通用的「引用图 + 只打渠道词」分支（`if ch and not desc:` →
    # `_enqueue(ch, tags, text, seed=seed)`）**不带 source_image**——它会去账本 /
    # 识图反推提示词，把这张图当**文生图**重画一遍。用户要的是「把这张图本身
    # 拿去修」，不是「照着这张图另画一张」，所以在「引用图轮」之前拦下来。
    # 用户附带的要求（「qwen修复 脸修清楚点」）拼在固定的修复指令后面，见骨架。
    if ch == "qwen-restore" and data_urls:
        prompt = (desc or "").strip()
        _remember_job(session_key, ch, prompt)
        return _enqueue(ch, prompt, text, source_image=True)

    # ── 前置搜索：全部流程默认先走一遍搜索 Agent（用户 2026-10-06 拍板）──
    # 放在这里而不是 `_translate` 里面，是为了让下面**所有**会产出提示词的
    # 分支共用同一份资料——改图管道 `_revise` 原先根本不经过 `_translate`，
    # 所以群里最常见的「图生图，角色换XX」一直没走过搜索。
    # 一次请求只搜一次；上面的早退分支（菜单 / 裸图 / 随机口令）都已经 return，
    # 不会白搜。（原先这里还列着「提示词反问」那条早退，2026-10-06 随硬编码
    # 一起删了——见上面那段注释。它没了不影响这行结论：引用图轮现在会走到
    # 搜索，而那正是用户要的「改图也先搜一遍」。）
    doc = _prefetch_search(
        _quote_merge(quoted, text) if quoted.strip() else text)

    # ── 引用图轮 ──────────────────────────────────────────
    # 2026-10-07 用户拍板：**发图 + 指令默认走 AI**，把原先这批
    # 「代码认死话术」的路由全删（`_GENERIC_I2I_RE` / `_RUN_THIS_RE` /
    # `_IMG_GEN_INTENT_RE` / `_DESCRIBE_RE` 都删了）。用户原话：「你期望
    # 用户说这六句话，但实际上他说的话根本匹配不到这六句话…必须走 AI」。
    # 分三种：
    #   ① 引用图 + 只打渠道/档位（没意见）→ 自家图直接账本提示词（零调用），
    #      别人的图才识图反推（1 次）→ 入队。自家图连种子一起复刻。
    #   ② 其余（有意见 / 有渠道 / 都没）→ 改图管道 `_revise`：把图 + 用户这句
    #      交给 AI，一次调用出提示词。**要垫图由模型自己在 JSON 里填
    #      `source_image`**，代码不扫机制词。图生文（「这画的是什么」）也走
    #      这里——模型判不是修改请求就回一段反推文本。
    if data_urls:
        if ch and not desc:
            own_prompt, _skill, own_seed = _ledger_hit(quoted)
            # 多图反推放开到 3 张（2026-10-05 用户拍板）：turbo 免费烧得
            # 起。numbered=False——合并结果直接当生成提示词，「（第 N 张）」
            # 中文前缀是污染。
            tags = own_prompt or _recall_tags(data_urls[:3], numbered=False)
            if not tags:
                return "没认出引用的图，重发一次，或直接 @我 渠道 描述。"
            _remember_job(session_key, ch, tags)
            seed = own_seed if (own_seed and
                                _LOCAL_SEED_SKILL_RE.match(ch)) else None
            return _enqueue(ch, tags, text, seed=seed)
        reply = _revise(text, data_urls, history, channel=ch, at_me=at_me,
                        doc=doc)
        return reply if reply is not None else ""

    # ── 成品提示词轮（带 NAI 权号 `::`）：过一次 AI，权号语法一个字不许动 ──
    # 这类串是用户自己调好的成品（`1.1::artist:x::`、`-1::tag::` 是 NAI 官方
    # 语法）。2026-10-05 晚用户拍板「ai 必须参与决策，绝对不能绕过 ai」：
    # 不再零转译直通，照样过一次 AI。代码只认开头点名的渠道词
    # （`_named_channel`），正文交给 AI；模板把「权号/画师串/负号/换行一律
    # 原样保留」写成硬要求。渠道两边都判不出 → 回问，**绝不静默落默认档**
    # （19:23 群聊那次就是这么把画师串喂给 anima 的）。
    # 放在引用图轮之后、「再来一张」之前：带图的轮另有规则。
    # 放行条件：@ 轮，或关键词轮但**用户自己点了渠道词**——群里别人贴一段串
    # 来讨论，不该被当成下单。
    if _FINAL_PROMPT_RE.search(text) and (at_me or _named_channel(text)[0]):
        skill, body = _weighted_channel(text)
        if not re.search(r"[0-9A-Za-z\u4e00-\u9fff]", body):
            return ("权号串我只看到渠道词，正文是空的。把整段提示词一起发，"
                    "别只有渠道词。")
        data = _translate(body, history, skill=skill, weighted=True,
                          no_default=True, doc=doc)
        if data and data.get("reply"):
            # 模型判断这串不是下单（比如群里贴串讨论）→ 直接回话，不入队。
            return data["reply"]
        if data and data.get("comic"):
            return _enqueue_comic(data["comic"], text)
        if not data:
            return _FINAL_PROMPT_NO_CHAN_TEXT
        _remember_job(session_key, data["skill"], data["prompt"])
        return _enqueue(data["skill"], data["prompt"], text, lora=data.get("lora"))

    # 2026-10-17 用户拍板：**删掉「再来一张」的关键词早退**（原来是
    # `_AGAIN_RE` 命中 + 内存 `_LAST_JOB` 有值就零调用直出）。理由同 `_revise`：
    # 代码不该替用户决定「这是重跑、不是改」；而且 `_LAST_JOB` 是进程内字典，
    # bot 一重启就空，同一句话重启前能跑、重启后就不认。现在这句话落到下面的
    # AI 分支——它要的事实就在最近对话里：`[直达生图] <渠道>：<提示词>` 那行
    # （`_RECENT_AI_KEEP_RE` 专门保留，见本文件顶部说明）。
    # 带图轮的「再来一张」事实更全：`_revise` 里有账本 + `{recent}`。
    last = _last_job(session_key)

    # 只打了渠道词（「大大怪 三档」+ 引用 / 干发「三档」）。
    if ch and not desc:
        if quoted and not _NOISE_QUOTE_RE.search(quoted) \
                and not quoted.startswith("🎨"):
            # 引用正文是占位符（图取不到/消息读不出）→ 绝不喂转译：9B 会
            # 把最近对话里的旧 tag 抄出来（02:59 实录），直接告诉用户重发。
            if _PLACEHOLDER_QUOTE_RE.search(quoted):
                return ("引用的内容没能取到（图片可能已过期，或引用的是我发的"
                        "消息）。把图/文字重新发出来再发渠道词，或直接 "
                        "@我 渠道 描述。")
            # 引用正文当描述，锁定渠道交给 AI（2026-10-05 用户拍板：AI 必须
            # 参与决策——「引用正文抽英文段直通」和「引用反推回复剥头直用」
            # 这两条零转译旁路都已删除）。
            # 转译**不带最近历史**：引用正文是唯一描述来源，历史里有旧 tag
            # 时小模型照抄（负向规则它执行不了，只能断来源）。
            data = _translate(quoted, [], skill=ch, doc=doc)
            if data and data.get("reply"):
                return data["reply"]
            if data and data.get("comic"):
                return _enqueue_comic(data["comic"], text)
            if data:
                _remember_job(session_key, data["skill"], data["prompt"])
                return _enqueue(data["skill"], data["prompt"], text, lora=data.get("lora"))
            return ("引用的内容没转成生图指令，引用一条带画面描述的消息"
                    "再试，或直接 @我 渠道 描述。")
        # 引用回执（HT- 编号）→ 重跑上次任务，渠道可换。
        # ⚠️ 2026-10-17：原来这里还带 `or _AGAIN_RE.search(text)`（干说「再来
        # 一张」也走这条），已删——那句话交回 AI 判（见本节开头那段注释）。
        # 留下的是「引用回执」这个**结构判据**：用户引用的就是我发的那条带
        # HT 编号的回执，指向明确，不需要猜。
        if last and _NOISE_QUOTE_RE.search(quoted):
            skill = ch if ch in _allowed_skills() else last["skill"]
            _remember_job(session_key, skill, last["prompt"])
            return _enqueue(skill, last["prompt"], text)
        return ("只发渠道词的话，后面直接跟上描述再发（如「三档 女骑士」），"
                "或引用一条带描述的消息再发渠道词。")

    # 渠道词打头（「三档 clear 初音未来」）→ 锁定渠道。
    if ch:
        # 描述一律交给 AI（2026-10-05 用户拍板：英文提示词也过一次 AI，不再
        # 「原样直通」——那条旁路正是 `1girl, soft lighting` 被正则删词的现场）。
        # 引用正文和补充话合并成一条描述。
        # 真引用了才断历史（引用是唯一描述来源，防抄旧 tag）；没引用照旧带
        # 历史（「换成卡通风格 三档」的指代要靠它）。
        src = _quote_merge(quoted, desc)
        hist = [] if quoted.strip() else history
        data = _translate(src, hist, skill=ch, doc=doc)
        if data and data.get("reply"):
            # 模型判这轮不是下单（点名了渠道也只是在聊）→ 直接回话。
            return data["reply"]
        if data and data.get("comic"):
            return _enqueue_comic(data["comic"], text)
        if data:
            _remember_job(session_key, data["skill"], data["prompt"])
            return _enqueue(data["skill"], data["prompt"], text, lora=data.get("lora"))
        return "这条没转译成生图指令。照格式来：@我 渠道 描述。"

    # 走到这里 = 没图 / 没渠道词 / 没成品串 / 没「再来一张」的纯文本轮
    #（可能带引用）。一律交给 AI 判意图：要画图就出图，是聊天/提问就回话。
    # 2026-10-05 用户拍板：删掉 `_INTENT_RE`/`at_me` 闸与菜单兜底——
    # 「AI 识别用户的意图，是画图就画，是聊天就回话」，一次请求一次回复。
    # ⚠️ 这里是**默认出图方式**的落点：`_translate` 拿不到任何渠道（代码没
    # 认出、模型也没点名）时回的是 `{"skill": "silver", …}`——2026-10-17 起
    # 默认 = silver 单图（此前是 anima多图 3 张，用户实测画质不行否掉了）。
    # 这个分支原本就在，不用另加判断。
    src = _quote_merge(quoted, text) if quoted.strip() else text
    data = _translate(src, [] if quoted.strip() else history, doc=doc)
    if data and data.get("reply"):
        return data["reply"]
    if data and data.get("comic"):
        return _enqueue_comic(data["comic"], text)
    if data:
        _remember_job(session_key, data["skill"], data["prompt"])
        return _enqueue(data["skill"], data["prompt"], text, lora=data.get("lora"))
    return None      # 转译失败 → 不接管，交回上层（不再回菜单）
