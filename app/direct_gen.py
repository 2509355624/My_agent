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
- 引用生图回执 +「再来一张」→ 同提示词换种子重跑（_LAST_JOB 现成有），零转译。
- 引用图 + @（没别的说）→ **反推提示词**发回去，不生成；引用图 + 档位
  （「三档」，或「快档 基于图片帮我生成」这类空话）→ 反推后直接生成；
  引用图 +「帮我生成这个 / 跑一下这张图」（没渠道）→ 默认档反推后重画。
- 引用图 +「反推提示词」（`_REVERSE_RE`，零 AI，2026-10-07 用户拍板）→
  **识图模型用自己那份提示词（`_TAGS_PROMPT`）出纯英文 tag、原样发**，
  不经过主人设、不经过改图管道。这是"识图先出词、文本模型只负责递"的落点。
- 引用图 + 意见 → 改图，**一律真识图**（2026-10-05 用户拍板：账本里存的是
  当时那句提示词，和画面实际内容可能已经对不上——背景改透明那次没看图，
  出图就不是用户要的）。一次带图调用，账本提示词只当「最可信旁证」写进
  prompt，不再作唯一事实来源。**垫不垫图由模型判**（2026-10-06 用户拍板，
  原话：「什么出现图生图、垫图、改图、重绘，这个硬编码给我去掉就行了，改成
  让 AI 它自己去判断」）：代码不再扫原话认机制词，模型输出的
  `source_image` 字段决定这一轮入队垫哪张图。引用图本身**不要求**垫图，
  文案口径写死在 `_REVISE_TEMPLATE` 的【图生图】段。模型判「不是修改请求」
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
from app.config import QQ_GROUP_KEYWORDS
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
# 默认渠道（2026-10-06 用户拍板：「我们默认渠道就是 sILVR，把它做成默认渠道
# 就行了」——**不做管理页的默认渠道下拉**，写死）。与
# `generate_image.T2I_DEFAULT_SKILL` 保持同一个值；silver / jank 不进
# `_FIXED_SKILLS`，由 `_allowed_skills()` 现场扫 skills/ 目录放行。
_DEFAULT_SKILL = "silver"


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
# 「再来一张」：引用回执（或不引用）时同提示词换种子重跑。
_AGAIN_RE = re.compile(r"再来一张|重画|再画|重跑|换种子|再跑一张")
# 引用正文里的噪音：菜单和生图回执不能当提示词用。
_NOISE_QUOTE_RE = re.compile(r"HT-\d{8}|任务已提交|生图完成")
_RECENT_NOISE_RE = re.compile(r"^🎨|任务已提交|生图完成|HT-\d{8}")
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
# krea2 系按挂的 LoRA 拆成 5 条，中文长名进这张表。
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
                   "qwen": "qwen_image_v1", "qwen-hd": "qwen-hd",
                   "nffa": "nffa", "nai": "nai",
                   "cunny": "cunny", "miao": "miao"}
_FIXED_CHAN_RE = re.compile(
    r"(?<![0-9A-Za-z_])"
    r"(krea2\s*米山舞|krea2\s*日系|krea2\s*亚洲真人"
    r"|krea2\s*动漫真人|krea2\s*真人\s*cos"
    r"|sd|qwen-hd|qwen|nffa|nai|cunny|miao)"
    r"(?![0-9A-Za-z_])",
    re.I)


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
_TAGS_PROMPT = (
    "你在帮用户反推这张图的生图提示词。要求：\n"
    "1. 只输出英文 Danbooru 风格 tag，逗号分隔，按重要性排序：主体、"
    "人物（发色/发型/瞳色/表情/服装/姿势/构图视角）、场景与画风。\n"
    "2. 细节给全，这是后续生图的唯一事实来源；画质词（best quality 等）不用写。\n"
    "3. **只输出这一行英文 tag**：不写中文、不翻译、不解释、不寒暄，"
    "也**不要**附加任何中文画面描述或总结（中文会污染后面的生图提示词）。"
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
        return _DEFAULT_SKILL
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
    - 没有任何渠道词         → (None, 原文)
    """
    text = text.strip()
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
    # 候选池只放 ASCII 词：krea2 那 5 个中文长名也是渠道词，但输入 token 是
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
    "nai（画师串/权号这种写法就是它的）/ silver（默认渠道）/ silver-hd（4x超清）/ "
    "anima（三档）/ sd / qwen / nffa / jank / krea2米山舞"
    "（krea2 系 5 条按风格拆，点名要带风格词）。")


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
    "「silver-hd」= 4x 超清出图\n"
    "「/更多渠道」= 档位/画风/其他AI ｜ 「使用指南」= 详细玩法"
)

GUIDE_TEXT = (
    "🎨 使用指南 · 一行一项\n"
    "@我（或喊「大大怪」）+ 渠道 + 描述就能出图。\n"
    "\n"
    "【怎么写】渠道名写在最前面，后面接需求\n"
    "　silver 水晶城堡 ｜ 不写渠道就走它（默认·最快）\n"
    "　anima 女骑士 ｜ 三档 + 2x 像素放大（3072×4608）\n"
    "　anima gloss 女骑士 ｜ gloss 油亮\n"
    "　三档 clear 水晶城堡 ｜ 跟 anima 等价\n"
    "　anima 1girl, blue hair ｜ 英文照过 AI\n"
    "　只说画风词（clear/soft/gloss/curvy）→ 也是三档\n"
    "\n"
    "【档位】只剩三档（快档 / 最小档 / 二档 2026-10-07 取消）\n"
    "　　　说 anima 或三档都行 · 不写渠道词 = silver\n"
    "\n"
    "【画风】clear 清晰 ｜ curvy 肉感\n"
    "　　　gloss 油亮 ｜ soft 柔和\n"
    "　　　打错或没打 → 按该档默认\n"
    "\n"
    "【点名渠道】把渠道名写最前面就行\n"
    "　silver-hd　4x超清 · 4928×7360（约28MB）\n"
    "　jank　NoobAI 自定义（有权号词就用它）\n"
    "　qwen　慢·写实·能在图里写中文字\n"
    "　qwen-hd　qwen 的 4x 超清 · 4096×6144（更慢更大，别连刷）\n"
    "　sd　　一次多张，多段用 --- 分隔\n"
    "　krea2系　5 条按风格拆：krea2米山舞 / krea2日系 / krea2亚洲真人\n"
    "　　　　　/ krea2动漫真人 / krea2真人cos（点名要带风格词）\n"
    "　nffa　　插画感\n"
    "　cunny　超分重渠道 · 单张 2~4 分钟\n"
    "　miao　　皮肤质感滑嫩 · 2x 出 3072×4608\n"
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
    "　出图回执 +「再来一张」→ 换种子重跑\n"
    "\n"
    "【随机口令】今日老婆 ｜ 随机萝莉 / 兽耳 / 女仆\n"
    "\n"
    "【直通·零消耗】\n"
    "　老手有现成英文提示词 → 加前缀直发，不花 token\n"
    "　/直通-渠道|英文提示词\n"
    "　例：/直通-silver|1girl, blue hair\n"
    "　　/直通-三档 gloss|1girl, blue hair\n"
    "　　/直通-qwen|1girl, blue hair\n"
    "　用 | 隔开渠道和提示词\n"
    "　提示词**原样直发**（不翻译、不过 AI、秒出）\n"
    "　引用一张图 = 用该渠道垫这张图\n"
    "　（如 /直通-qwen|change hair to silver）\n"
    "\n"
    "提示词用中文描述就行，我来转成画法。"
)

# 「/更多渠道」：把全部跑法一行一项罗列（2026-10-07 用户定稿排版——
# 渠道名英文左列、右边接一句人话解释；只留一组例子；方括号分段。
# QQ 是比例字体，列对齐用全角空格「　」比半角稳）。
MORE_CHAN_TEXT = (
    "🧭 全部跑法 · 一行一项\n"
    "写法：渠道名 + 你的需求\n"
    "例：三档 女骑士 ｜ jank 银发初音未来 ｜ qwen 水晶城堡\n"
    "\n"
    "【直通·零消耗】老手直接用英文提示词\n"
    "　/直通-渠道|英文提示词\n"
    "　例：/直通-silver|1girl, blue hair\n"
    "　　/直通-三档 gloss|1girl, blue hair\n"
    "　　/直通-qwen|1girl, blue hair\n"
    "　用 | 隔开渠道和提示词；提示词原样直发\n"
    "　（不翻译、不过 AI、不花 token，秒出）\n"
    "　引用一张图 = 该渠道垫这张图（如 /直通-qwen|…）\n"
    "\n"
    "【默认】不写渠道词 → silver（最快）\n"
    "\n"
    "【档位】只剩三档（快档 / 最小档 / 二档 已取消）\n"
    "　　　anima 或三档都行 · 末尾 2x 放大 → 3072×4608\n"
    "\n"
    "【画风】clear 清晰 ｜ curvy 肉感\n"
    "　　　gloss 油亮 ｜ soft 柔和\n"
    "　　　不写就按该档默认\n"
    "\n"
    "【点名渠道】把渠道名写在最前面就行\n"
    "　silver-hd　4x超清 · 4928×7360（约28MB）\n"
    "　jank　NoobAI 自定义（有权号词就用它）\n"
    "　qwen　慢·写实·能在图里写中文字\n"
    "　qwen-hd　qwen 的 4x 超清 · 4096×6144（更慢更大，别连刷）\n"
    "　sd　　一次多张，多段用 --- 分隔\n"
    "　krea2系　5 条按风格拆：krea2米山舞 / krea2日系 / krea2亚洲真人\n"
    "　　　　　/ krea2动漫真人 / krea2真人cos（点名要带风格词）\n"
    "　nffa　　插画感\n"
    "　cunny　超分重渠道 · 单张约 2~4 分钟\n"
    "　miao　　皮肤质感滑嫩 · 2x 出 3072×4608\n"
    "　nai　　 NovelAI 云端 · 认画师串/权号\n"
    "\n"
    "【随机口令】今日老婆 ｜ 随机萝莉 / 兽耳 / 女仆\n"
    "\n"
    "【引用玩法】\n"
    "　引用提示词 + 渠道词 → 照跑\n"
    "　引用图 +「提取提示词」→ 给当初那串词\n"
    "　引用图 +「反推提示词」→ 纯英文 tag 反推\n"
    "　引用图 + 说需求 → 改词后用同渠道重画\n"
    "　引用图 + 只@我 → 反推\n"
    "　引用图 + 档位 → 反推后生成\n"
    "　引用图 +「qwen 图生图 + 怎么改」→ 只改那一处\n"
    "　出图回执 +「再来一张」→ 换种子重跑"
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
    # 渠道段 → 渠道 id。silver 是**默认档**，`_parse_channel` 平时不把它当点名
    # （不写渠道=走它）；直通里显式写了就按默认档处理（用户给的例子里有它）。
    if chan_seg.strip().lower() == _DEFAULT_SKILL:
        return _DEFAULT_SKILL, prompt, ""
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


# ─── 唯一的一条转译模板（2026-10-05 晚用户拍板重做）─────────
# 用户原话：「提示词的构成就是：人设 / 工具列表 500~600 字 / 用户的历史对话 /
# 最近的用户需求，就这么简单。」「我一直想通过硬编码指令的方式来调用生图工具，
# 但我错了——AI 本身就能胜任整个工作。」
#
# 所以这里**不再按渠道分模板、不再注入每个渠道的专属规则**。就四段：
#   ① 人设（你是谁、能聊天也能画图）
#   ② 工具列表（只有 generate_image / recall_image 两个，一句描述）
#   ③ 最近对话
#   ④ 用户这一轮的原话
# AI 自己判「要不要动手 / 调哪个工具 / 哪个渠道 / 提示词怎么写」。
#
# 三处**代码仍然要给**的东西（模型不可能自己知道的本地事实，不是「指挥 AI」）：
#   - 渠道名清单（hd_3_* / nai … 是我们自己起的，模型猜不出来）
#   - 每个渠道的尺寸/快慢（本地实测值）
#   - `chan_hint`：代码从原话里认出的开头渠道词，直接告诉模型（免它误判）
_MASTER_TEMPLATE = (
    "你是大大怪，一个生图 AI。听懂用户要什么画面，写成提示词调工具画出来。"
    "他只是在聊天、问问题、要提示词就直接回话，不要调工具。\n"
    "\n"
    "【工具】要动手时，只输出一行 JSON，前后不要写别的字：\n"
    "- {{\"tool\": \"generate_image\", \"prompt\": \"英文提示词\", "
    "\"skill\": \"渠道，可省\", \"source_image\": 1, \"seed\": 123}}\n"
    "  画一张图。prompt 必填、必须是英文。**source_image 默认不填**（判据见下面"
    "【图生图】）；seed 只在用户点名要某个种子时填；用不上的参数一律省略。\n"
    "- {{\"tool\": \"recall_image\", \"id\": \"HT-20261005-123456-789\"}}\n"
    "  查一张图当初用的提示词和种子：用户引用带编号的图问「这张什么词」时用它。\n"
    # ── 第三个工具：连续小漫画（2026-10-08）──────────────────────────
    # QQ 侧的 @ 轮**根本不跑主 Agent 循环**（`qq_bot` 的调度注释：「agent 循环
    # 只留给总开关关闭和主动接话轮」），所以注册在工具表里的 `generate_comic`
    # 在 QQ 里永远调不到——漫画只能从这条 JSON 契约进来。**别把这条删了。**
    # brief 是**中文**（剧情 + 角色），不适用下面「prompt 必须是英文」那条规则；
    # 格数由 `comic_story.clamp_panels` 收口。
    # **没有 skill 字段**：漫画只有一条渲染路（`comic_story.COMIC_SKILL`），
    # 2026-10-08 用户拍板「漫画就只有 silver 渠道呀，其他渠道没有漫画的」。
    # 模型仍可能习惯性多吐一个 skill，`_enqueue_comic` 直接忽略，无害。
    "- {{\"tool\": \"generate_comic\", \"brief\": \"中文剧情\", \"panels\": 10}}\n"
    "  画**连续多格小漫画**（同一角色、剧情连贯的一串图）：用户要「漫画 / 连环画 / "
    "多格 / 条漫」时用它。brief 写中文剧情和角色（名字照抄），panels 用用户报的格数、"
    "没报就不填（默认 10，上限 30）。**单张图别用它**。\n"
    "不需要动手时，输出 {{\"reply\": \"你要说的话\"}}。\n"
    # ── 「别反问」（2026-10-07 用户拍板）──────────────────────────
    # 用户原话：「我都明确是要求 ai 直接生成的，结果他又反问我…我希望就是我
    # 给需求，他就直接跑即可，不要问来问去」。
    # 实测病根就在上一行：模板允许 `{"reply": …}`，却**没有一条规则禁止
    # 「用户明确下单时反问确认」**——模型于是「礼貌地确认一下」。
    # 私聊 2509355624 实录 5 轮（引用反推回复 +「silver 生成」→ 回
    # 「三档？还是默认最小档？需要我用它直接生成一张吗？」）；全量扫
    # sessions/*.jsonl，680 条 LLM 回复里 137 条带问句，收紧到「原话有
    # 生成/跑/画 却只回问句」约 10 条、跨 6 个会话。是普遍现象。
    # 只加规则、不加代码判意图：判「这是不是下单」仍然全交给模型。
    # ⚠️ 这段压到 85 字是**故意的**：整条提示词的 3000 字预算（
    # `test_full_prompt_stays_within_budget`）只剩个位数余量，
    # 而「只在聊天/提问时才回话」开头人设行已经写过一遍，这里不重复。
    "\n"
    "【别反问】用户给了画面、或说「生成 / 跑 / 画一张」就是下单——"
    "**直接出图**，不许问「要不要画」「哪个档位」「这样可以吗」。"
    "渠道档位没点名用默认，细节自己补。\n"
    "\n"
    "【渠道 skill】不填 = " + _DEFAULT_SKILL + "（默认渠道）。用户点名渠道就照他说的填。\n"
    "画风四种：clear 清晰 / soft 柔和 / gloss 油亮 / curvy 肉感\n"
    "尺寸只剩一档（2026-10-07 起快档 / 最小档 / 二档全取消），id = hd_3_<画风>：\n"
    "- 「anima」「三档」「高清」都是它，末尾带 2x 像素放大 → 3072×4608，最慢\n"
    "- 没说画风 → hd_3_clear（「anima gloss」→ hd_3_gloss）；只说画风也是它\n"
    "固定渠道（说左边这些词就填右边的 id）：\n"
    "- nai / nai_wide = NovelAI 云端，竖 832×1216 / 横 1216×832，认画师串和权重语法。\n"
    "- qwen / 千问 / 通义 = qwen_image_v1，云端、慢，prompt 写完整英文句子。\n"
    "- sd = image_gen_v1，一次出多张（prompt 里用 --- 分段）。\n"
    "- nffa / cunny / miao：点名才用（cunny / miao 一张好几分钟）。\n"
    "- krea2 系按风格 LoRA 分：krea2米山舞 / krea2日系 / krea2亚洲真人 / "
    "krea2动漫真人 / krea2真人cos；点名带风格词，光说 krea2 不算。\n"
    "- silver = **默认渠道**（不填 skill 就是它）、jank = 自定义 NoobAI 渠道，"
    "明说才填、都是文生图专用、无图生图骨架。"
    "⚠️「银发 / silver hair」是发色、不是渠道。\n"
    "- silver-hd = silver 的 4x 超清版（只换放大，4928×7360）；"
    "明说「silver-hd / silver 超清」才填，否则走 silver。\n"
    "- qwen-hd = qwen 的 4x 超清版（只换放大，4096×6144）；"
    "明说「qwen-hd / qwen 超清」才填，否则走 qwen。\n"
    "{chan_hint}"
    "\n"
    "【图生图：默认不做，只认点名】`source_image` 一律默认不填：只有用户"
    "**明说「qwen 图生图」+ 要改什么**才填 1，配 skill=qwen_image_v1（默认），"
    "明说「qwen 超清 / qwen超清」时换成 skill=qwen-hd；"
    "prompt 只写一句改动指令（一张 1~2 分钟，不点名不走）。\n"
    "  引用一张图**不等于**要改它：引用带编号的自家图 + 说需求 → 看图、"
    "拿它当初的提示词并进新需求改写，**沿用原图当初的渠道**重新生成"
    "（没点名换渠道就别换）。\n"
    "【提示词怎么写】\n"
    "- 中文需求翻成英文再写，prompt 里不许出现中文"
    "（要出现在画面里的文字除外，用引号原样写）。\n"
    "- 用户给的**画师串**和权重语法（`1.2::tag::`、`artist:xxx`）原样保留，"
    "别翻译、别删、别改成平铺 tag；渠道是 nai 时它就是画风来源，必须用上。\n"
    "- prompt 只写画面内容，「重绘 / 高清 / 加强细节」这类操作词别写进去。\n"
    "- **每一轮都是新请求**：角色、服装、动作、场景全按这轮重新写，"
    "别把上一轮画过的东西抄过来。\n"
    "- 认不出的角色照外貌特征写，别编不存在的角色名。\n"
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
_SEARCH_HEADER = (
    "\n【标签库资料】以下是刚从 Danbooru 标签库（32.8 万条真实条目）里查出来的。\n"
    "格式是 `中文 → tag 名`；`｜同族：…` 是同一个主体在库里还能怎么修饰，"
    "给你参考写法用的。\n"
    "**这份资料怎么用**：\n"
    "1. 它是**候选池和写法参考**，不是必须全用。只取和用户需求相符的；"
    "明显不相符的（用户要「女孩」而资料里给了个无关角色）**直接忽略**。\n"
    "2. **角色和作品以这份资料为准**——资料里给了 tag 名的，照抄，"
    "别自己凭印象写。\n"
    "3. 同一个中文名有多个候选时，结合用户的需求和最近的对话挑最合适的那个。\n"
    "4. 资料里没有的东西（普通描述词、画法、画面氛围）你自己按需要补；"
    "但**凡是资料给了 tag 名的，一律照它写**，别改动拼写。\n"
    "{doc}\n"
)

_REVISE_TEMPLATE = (
    "你是生图提示词修正器。用户引用一张图并提出要求（也可能只是让你看看、"
    "或问你点别的）。只输出 JSON 本体，格式：{{\"skill\": \"渠道id\", \"prompt\": "
    "\"修正后的完整英文提示词\", \"source_image\": 1}}（用不上的参数一律省略）\n"
    "- 先看清楚画面（人物、发色发型、瞳色、表情、服装、姿势、场景），"
    "prompt 必须覆盖画面全部要点，用户没提到的细节原样保留\n"
    "- 原提示词{anchor_note}：与画面冲突时，一律以画面为准\n"
    "- skill 沿用「原渠道」，除非用户点名要换\n"
    # 渠道清单必须写在这里：改图管道不走 `_MASTER_TEMPLATE`，模板里没有 id
    # 表时模型听不懂「换 silver」，只会沿用原渠道（2026-10-06 19:22 群
    # 580929233 实录：引用图 +「silver 生成」→ 又跑了一次 jank）。
    "- 渠道 id 只有这些（用户点名换渠道时照左边说的话填右边那个）：\n"
    "  **默认渠道 = silver**（用户没点名渠道、引用的又不是自家图时就用它）；\n"
    "  anima（= 三档）= hd_3_<画风>（末尾带 2x 像素放大 → 3072×4608；"
    "画风只有 clear/soft/gloss/curvy 四种，例「anima gloss」= hd_3_gloss，"
    "没说画风 = hd_3_clear）；\n"
    "  nai / nai_wide = 说「nai」（画师串和权重语法认这两个）；\n"
    "  qwen_image_v1 = 说「qwen / 千问 / 通义」；image_gen_v1 = 说「sd」；\n"
    "  krea2-yoneyama / krea2-rella / krea2-asianmix / krea2-anime2real / "
    "krea2-coscandid = krea2 系 5 条，用户原话点「krea2米山舞 / krea2日系 / "
    "krea2亚洲真人 / krea2动漫真人 / krea2真人cos」时对应填；\n"
    "  ⚠️ 通用「krea2」已下架，原话只说了 krea2 没带风格词时**别填 krea2 系**"
    "（那 5 个名字都不是他说的），照默认渠道走。\n"
    "  nffa / cunny / miao = 原话点名才填；\n"
    "  jank = 原话明说「jank」才填（用户自定义渠道）。\n"
    "  silver-hd = silver 的 4x 超清版，原话明说「silver-hd」或「silver 超清」才填。\n"
    "  qwen-hd = qwen 的 4x 超清版，原话明说「qwen-hd」或「qwen 超清」才填。\n"
    "  ⚠️ silver / silver-hd 和 jank 都是**文生图专用**、没有垫图骨架。"
    "「银发 / silver hair」是画发色、不是点名渠道，别因为它就改填 silver。\n"
    "【图生图：默认不做，只走 qwen 系】`source_image` 这个字段**默认不填**——"
    "填了就是把引用的这张图垫进去改，代价很大。只有用户**明说「qwen 图生图」**"
    "（点名 qwen、要在这张图上改）时才填 1，同时 skill 填 qwen_image_v1"
    "（明说「qwen 超清 / qwen超清」时填 qwen-hd）、"
    "prompt 只写**一句改动指令**（例 `change her coat to red, keep the pose, "
    "face and background exactly the same`），不要把整张图重新描述一遍。\n"
    "  用户说「把衣服换成jk」「换个姿势」这类**需求**而没点名 qwen 图生图 → "
    "**不垫图**：看这张图、把要改的内容并进提示词，用文生图重新画一张。"
    "**引用一张图本身不是垫图要求。**\n"
    "- {lang}\n"
    "- 禁止权重语法 (tag:1.2)、{{tag}}、::\n"
    "- 具体角色没把握就写外貌特征+作品名，不要编造不存在的角色名\n"
    # 逃逸口**一直开着**：以前代码扫原话认机制词，认到就换成「必须出 prompt」
    # 那段（`_ESCAPE_FORBIDDEN`，2026-10-06 随图生图硬编码一起删）。现在由模型
    # 自己判「这轮是改图还是随口评价」，它判成评价就只回反推。
    "- **别反问**（2026-10-07 用户拍板）：用户明确要改 / 要重画（「换XX」"
    "「改XX」「跑一张」「再来一版」）就直接给 prompt，不许问「要不要改」"
    "「这样可以吗」「确认一下」。\n"
    "- **用户的话不是修改/生图请求**（夸奖、闲聊、问别的事）→ 只输出一个 "
    "reverse 字段，值 = 这张图真实的英文 danbooru tag 反推"
    "（reverse 的值要填真实 tag，别照抄这句话）\n"
    "{search}"
    "原提示词（渠道 {last_skill}）：\n{last_prompt}\n"
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
# 为什么不复用 `_LAST_JOB`：那份的语义是「单张任务」，`_AGAIN_RE`（再来一张）
# 和 `_revise`（引用图改图）都拿它当**单张提示词**用；漫画的 brief 是中文剧情，
# 塞进去会被当成提示词重跑一张垃圾图。所以两件事分开记。
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


def _strip_attribution(text):
    """剥掉群聊署名前缀（「胡桃桃：好」这种），每行各剥一次。"""
    return "\n".join(
        _ATTRIBUTION_RE.sub("", line or "", count=1)
        for line in (text or "").split("\n"))


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
             resample_fn=None):
    """转译结果落队。返回要发回会话的文本；成功且回执已直发返回 ""。

    source_image=True → 垫本轮引用的那张图（传 "1"，下游 comfy_src.resolve
    解析；引用图取不到会报错回给用户）。**只有模型在改图 JSON 里自己填了
    `source_image` 才会是 True**（2026-10-06 起代码不再扫原话判）。
    seed 非空 → 复刻账本里那张图的种子（引用 HT 图换档场景），下游
    _resolve_seed 会校验范围；只往本机渠道传（NAI 拒收 seed）。
    resample_fn 非空 → 随机口令的「被拦静默重抽」钩子（见 _random_resample）。
    """
    from app.tools.normal import generate_image as gi
    extra = {}
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
    try:
        result = gc._generate_comic(brief, panels=comic.get("panels"))
    except Exception:
        log.exception("[direct] 漫画入队失败")
        return "漫画请求没发出去，稍后再试。"
    panels = comic_story.clamp_panels(comic.get("panels"))
    log.info("[direct] 漫画入队：%d 格 %r（原话 %r）", panels, brief[:50], text[:50])
    _note_desc("[直达漫画] %d 格：%s" % (panels, brief[:200]))
    if result.startswith(image_jobs.RECEIPT_SENT_MARK):
        return ""          # 回执已由工具直发，本轮闭嘴
    if result.startswith("错误："):
        return _humanize_error(result)
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
        log.exception("[direct] 定点抽取失败，只走搜索 Agent")

    try:
        from app import search_agent
        doc = search_agent.search(text, hint=hint) or ""
        if doc:
            parts.append(doc)
    except Exception:
        log.exception("[direct] 前置搜索失败，按无资料继续")

    out = "\n\n".join(parts)
    if len(out) > DOC_MAX_CHARS:
        out = out[:DOC_MAX_CHARS].rstrip() + "…"
    return out


def _translate(text, history, skill=None, weighted=False, no_default=False,
               doc=None):
    """一次 LLM 调用 → 生图任务，或一句聊天回复。**所有提示词都由它产出**。

    2026-10-05 晚用户拍板重做：**只有一条模板 `_MASTER_TEMPLATE`**（人设 +
    工具列表 + 最近对话 + 用户原话），不再按渠道分模板、不再注入渠道专属规则。
    用户原话：「AI 本身就能胜任整个工作，我一直想通过硬编码指令去操控 AI，
    这本身就是错的。」

    返回值三态：
      - {"skill":…, "prompt":…}  → 要画图，调用方入队
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
    """
    named = (skill or "").strip()
    # 搜索资料由调用方（`decide`）统一算好传进来——**一次请求只搜一次**，
    # 所有分支共用。没传就是空串，模板里那一格为空，照老路走。
    doc = doc or ""
    if named:
        hint = "\n用户开头点名了渠道：**%s**，就用它。\n" % named
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
    # 渠道固定（漫画只有一条渲染路），格数由 `comic_story.clamp_panels` 收口，
    # 都不需要在这儿再判一遍。
    # 模型若多吐一个 `skill`（模板里已经没有它了，但习惯难改）**直接丢掉**。
    if (data.get("tool") or "").strip() == "generate_comic":
        brief = (data.get("brief") or data.get("prompt") or "").strip()
        if not brief:
            return None
        return {"comic": {"brief": brief,
                          "panels": data.get("panels")}}
    user_prompt = (data.get("prompt") or "").strip()
    if not user_prompt:
        # 模型选择不动手（聊天 / 问答 / 只要提示词）→ 把话原样带出去。
        reply = (data.get("reply") or "").strip()
        if not reply:
            return None
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
    if out_skill not in _allowed_skills():
        allowed = _allowed_skills()
        fallback = (named if named in allowed else
                    judged if judged in allowed else "")
        if not fallback and no_default:
            # 成品串：判不出就回问，绝不静默换默认档烧一张错风味的图。
            return None
        out_skill = fallback or _DEFAULT_SKILL
        log.warning("[direct] 未知渠道（点名 %r / 模型 %r），改用 %s",
                    named, judged, out_skill)
    return {"skill": out_skill, "prompt": user_prompt}


def _recall_tags(data_urls, numbered=True):
    """引用图 → 英文 danbooru tag 反推（「反推返回」「反推后生成」共用）。

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
_LOCAL_SEED_SKILL_RE = re.compile(r"^(anima_|hd_|qwen_image_v1|image_gen_v1|krea2|nffa|cunny|miao)")


def _revise(text, data_urls, history, channel=None, at_me=False, doc=None):
    """改图管道：引用图 + 意见 → 一次调用 → 重跑。

    引用带图改图**一律真识图**（2026-10-05 用户拍板）：账本里存的是当时那句
    提示词，和画面实际内容可能已经对不上（背景改透明那次没看图，出图就不是
    用户要的）。账本提示词降级为「最可信旁证」写进 prompt，不再是唯一来源。

    垫不垫图（`source_image`）**由模型在这一次调用里判**：它输出 JSON 里带
    `source_image: 1` 才入队垫图，判据是模板里的【图生图】段（用户明说
    「qwen 图生图」才垫）。代码不再扫原话认机制词，也不再因为「要垫图」
    就换渠道——那是 2026-10-06 拆掉的静默降档。

    返回 None = 模型判定这轮不是修改请求（夸奖、闲聊、问别的）：@ 轮回反推
    文本、关键词轮静默，由本函数内部处理。
    """
    session_key = qq_api.current_session_key()
    quoted = (qq_api.current_quoted_text() or "").strip()
    # 「再来一张」：引用回执（或干说）→ 同提示词换种子重跑，零调用。
    last = _last_job(session_key)
    if _AGAIN_RE.search(text) and (last or _NOISE_QUOTE_RE.search(quoted)):
        if last:
            skill = channel or last["skill"]
            _remember_job(session_key, skill, last["prompt"])
            return _enqueue(skill, last["prompt"], text)
        return "没找到最近一次生图的记录，重新描述想要什么吧：@我 渠道 描述。"
    anchor_prompt, anchor_skill, _anchor_seed = _ledger_hit(quoted + " " + text)
    own = bool(anchor_prompt)        # 引用的**是不是自家图**（账本命中）
    if own:
        anchor_note = "是这张图当时的真实提示词（账本可查），最可信"
    else:
        anchor_skill, anchor_prompt = "（无）", "（无）"
        anchor_note = "没有（引用的不是本机器人画的图），忽略此项，以画面为准"
    ask = _REVISE_TEMPLATE.format(
        search=(_SEARCH_HEADER.format(doc=doc) if doc else ""),
        anchor_note=anchor_note,
        last_skill=anchor_skill,
        last_prompt=anchor_prompt,
        lang=_prompt_lang(channel or anchor_skill),
        text=text)
    from app.llm import call_llm
    from app.vision import describe
    try:
        if own:
            # ── 自家图：账本优先，**不预先识图**（2026-10-07 用户拍板）──────
            # 账本里躺着当时真跑的那段提示词，和画面同源，改「发色红→银」这类
            # 需求直接在那段文字上改就行，没必要每次都看一眼图。用户原话：
            # 「没有必要每一次用户引用我的生成结果的图片的时候，就一直在识图」。
            # 走**纯文本**调用（`call_llm`，不是 `describe`），零识图开销。
            reply = call_llm([{"role": "user", "content": ask}])
        else:
            # 别人的图：没有账本可依，一律真识图（1 次）。
            # 不指定 provider = 跟识图配置走（.env，现在是 doubao-seed-2.1-turbo）。
            reply = describe(data_urls[0], prompt=ask)
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
    prompt = (data.get("prompt") or "").strip()
    if not prompt:
        return "改图请求没解析出来，换个说法再试（如「手改成插兜」）。"
    # 垫不垫图 = **模型在 JSON 里说的算**（2026-10-06 拆掉代码扫原话认机制词，
    # 判据改写在 `_REVISE_TEMPLATE` 的【图生图】段）。模型没填 / 填 0 = 文生图。
    si = data.get("source_image")
    source_image = si is True or str(si or "").strip() == "1"
    skill = channel or (data.get("skill") or "").strip()
    if skill not in _allowed_skills():
        skill = anchor_skill if anchor_skill in _allowed_skills() \
            else _DEFAULT_SKILL
    _remember_job(session_key, skill, prompt)
    return _enqueue(skill, prompt, text, source_image=source_image)


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
    # 策展词池直拼提示词，零 LLM；走默认档入队，额度闸门照常拦。
    # resample_fn：图是**机器人自己推的服务**，被审核拦下时 worker 静默换一条
    # 重抽（最多 2 次），绝不回「未过审」——用户点的单被拦才走那套话术。
    m = _RANDOM_CMD_RE.match(text)
    if m:
        theme = m.group(1)
        prompt = random_tags.sample_prompt(theme)
        _remember_job(session_key, _DEFAULT_SKILL, prompt)
        return _enqueue(_DEFAULT_SKILL, prompt, text,
                        resample_fn=_random_resample(theme))
    if _WAIFU_CMD_RE.match(text):
        _cn, prompt = random_tags.draw_waifu()
        _remember_job(session_key, _DEFAULT_SKILL, prompt)
        return _enqueue(_DEFAULT_SKILL, prompt, text,
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
        return _enqueue(data["skill"], data["prompt"], text)

    # 「再来一张」（不引用、不带渠道词）：同提示词换种子重跑，零转译。
    last = _last_job(session_key)
    if not ch and _AGAIN_RE.search(text) and last:
        _remember_job(session_key, last["skill"], last["prompt"])
        return _enqueue(last["skill"], last["prompt"], text)

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
                return _enqueue(data["skill"], data["prompt"], text)
            return ("引用的内容没转成生图指令，引用一条带画面描述的消息"
                    "再试，或直接 @我 渠道 描述。")
        # 引用回执（HT- 编号）或干说「再来一张」→ 重跑上次任务，渠道可换。
        if last and (_AGAIN_RE.search(text)
                     or _NOISE_QUOTE_RE.search(quoted)):
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
            return _enqueue(data["skill"], data["prompt"], text)
        return "这条没转译成生图指令。照格式来：@我 渠道 描述。"

    # 走到这里 = 没图 / 没渠道词 / 没成品串 / 没「再来一张」的纯文本轮
    #（可能带引用）。一律交给 AI 判意图：要画图就出图，是聊天/提问就回话。
    # 2026-10-05 用户拍板：删掉 `_INTENT_RE`/`at_me` 闸与菜单兜底——
    # 「AI 识别用户的意图，是画图就画，是聊天就回话」，一次请求一次回复。
    src = _quote_merge(quoted, text) if quoted.strip() else text
    data = _translate(src, [] if quoted.strip() else history, doc=doc)
    if data and data.get("reply"):
        return data["reply"]
    if data and data.get("comic"):
        return _enqueue_comic(data["comic"], text)
    if data:
        _remember_job(session_key, data["skill"], data["prompt"])
        return _enqueue(data["skill"], data["prompt"], text)
    return None      # 转译失败 → 不接管，交回上层（不再回菜单）
