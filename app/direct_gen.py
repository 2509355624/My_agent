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
  引用图 +「帮我生成这个 / 跑一下这张图」（没渠道）→ 默认档反推后重画；
  引用图 +「图生图 / 换成…」→ **垫图改图**（source_image=1，默认动漫档
  重绘，点名 qwen 走参考图编辑、改动指令直通零调用）。
- 引用图 + 意见 → 改图，**一律真识图**（2026-10-05 用户拍板：账本里存的是
  当时那句提示词，和画面实际内容可能已经对不上——背景改透明那次没看图，
  出图就不是用户要的）。一次带图调用，账本提示词只当「最可信旁证」写进
  prompt，不再作唯一事实来源。用户**明说机制词**（图生图/垫图/改图/重绘）
  时，模板里**不给 reverse 逃逸口**——意图已经定死，必须出 prompt；
  没点名机制词（「画得真好」这类真闲聊）才允许回反推，且 @ 轮才回、
  关键词轮静默。引用图 + @ / 档位 /「生成这个」仍优先用账本提示词（零调用）。
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
from app.skills import list_skills

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
# 对不上的（被归档/新增）以目录为准加减。
_HD_TIERS = ("fast", "2", "3")
_HD_STYLES = ("clear", "curvy", "gloss", "soft")
_FIXED_SKILLS = ("anima_clear", "anima_curvy", "anima_gloss", "anima_soft",
                 "image_gen_v1", "image_gen_v1_hires", "krea2", "nffa",
                 "cunny", "miao", "qwen_image_v1", *image_jobs.NAI_SKILLS)
_DEFAULT_SKILL = "anima_clear"


def _allowed_skills():
    allowed = set(_FIXED_SKILLS)
    allowed.update("hd_%s_%s" % (t, s) for t in _HD_TIERS for s in _HD_STYLES)
    # 以 skills/ 目录实况校正：目录里没有了就剔除（防归档后照发）。
    # ⚠️ NAI 是**虚拟渠道**（image_jobs 云分支，skills/ 下没有目录），
    # 不参与目录核对，否则永远被误杀。
    try:
        present = set(list_skills())
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
_TIER_MAP = {"三档": "3", "3档": "3", "二档": "2", "2档": "2",
             "快档": "fast", "一档": "fast", "1档": "fast"}
_TIER_RE = re.compile(r"(三档|二档|一档|快档|[123]档|默认)")
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
_FIXED_CHAN_MAP = {"sd": "image_gen_v1", "krea2": "krea2",
                   "qwen": "qwen_image_v1", "nffa": "nffa", "nai": "nai",
                   "cunny": "cunny", "miao": "miao"}
_FIXED_CHAN_RE = re.compile(
    r"(?<![0-9A-Za-z_])(sd|krea2|qwen|nffa|nai|cunny|miao)(?![0-9A-Za-z_])",
    re.I)
# 中文正则在画风判定里当「命令区边界」用，见 _parse_channel。
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
# 开头点名的渠道词前面可能残留的前导噪音（@ 剥完的空格、打错的「：，/」）。
# 只在这儿容错，正文里的标点一律不动。
_LEAD_SEP_RE = re.compile(r"^[\s,，、:：/!！。]+")
# 「基于图片帮我生成」这类空话：引用图 + 渠道词时不算修改意见，
# 意思就是「反推这张图然后按渠道跑」。
_GENERIC_I2I_RE = re.compile(
    r"^(?:帮我|给我|请)?(?:基于|按照|根据|参考|用)这?(?:张|个)?(?:图片|图|照片|画)"
    r"(?:帮我|给我)?(?:直接)?(?:生成|画|跑|出|出图|来一张?|来一幅?|生图)?"
    r"(?:一?[张幅])?[吧呀啊哦。.!！？?，,\s~～]*$")
# 「跑这张 / 跑一版这张 / 生成这个 / 来一张」= 照这张跑的意思，同样不算修改
# 意见（2026-10-05 用户口径：引用图 + 渠道词 +「跑这个」→ 直接用该渠道跑，
# 别掉进改图管道）。带真实改动内容的（「画成银发」）不匹配，照旧走改图。
_RUN_THIS_RE = re.compile(
    r"^(?:帮我|给我|请|直接)?(?:跑|生成|画|出|来|处理|重跑|复刻|还原)"
    r"(?:一?下|一?版|一?[张幅个])*(?:这|那)?(?:张|个|版|图片|图|画)?"
    r"[吧呀啊哦。.!！？?，,\s~～]*$")
# 描述尾巴上的动词残渣：「这个猪 跑 nai」剥掉渠道词后剩「这个猪 跑」。
_DESC_TAIL_RE = re.compile(
    r"\s*(?:帮我|给我)?\s*(?:跑|画一张|生成一张|来一张|来一幅|出一张?图?|生图)\s*$")

# 引用图 +「帮我生成这个 / 跑一下这张图片 / 处理一下这张图」（没打渠道）→
# 用户口径（2026-10-05）：默认反推 → 重画，别反问。动词和「这/个/张/图」
# 必须紧挨着，避免「帮我把生成的图改一下」这种改图话误中。
_IMG_GEN_INTENT_RE = re.compile(
    r"(?:帮我|给我|请)?(?:直接)?(?:生成|跑|处理|画|出|来)(?:一?下|一?[张幅个])?"
    r"这?(?:张|个)?(?:图片|图|画|个)")

# ─── 看图说话（单次识图，不进改图管道） ───────────────────
# 触发词收得窄：不能含「看图/识图」（那是反推 tag 的口），也不能含裸
# 「这是什么」——「这是什么破手，改成插兜」会被劫走不进改图。
_DESCRIBE_RE = re.compile(
    r"画的?(?:是|了)什么|什么画风|什么风格|描述一下|介绍一下|讲讲|说说"
    r"|什么内容|帮我?看看|这是啥")
_DESCRIBE_PROMPT = (
    "用中文自然语言描述这张图片：主体是谁/什么、外貌服装、动作姿势、"
    "场景背景、画风。150 字以内，直接描述，不分点、不寒暄。")

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
    "3. 不写中文，不解释，不寒暄。"
)

# 反推回复的头：用户引用这条回复 + 渠道词就能直接生成（decide 里有对应直通）。
_REVERSE_HEADER = "这张图的提示词反推如下，引用本条 + 渠道词（如「三档」）可直接生成："


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


def _redraw_capable(skill):
    """垫图重绘只认动漫档（anima_* / hd_fast_* / hd_2_*）——hd_3 不支持，
    固定渠道里只有 qwen（参考图编辑，单独走）和 NAI。"""
    return (skill.startswith("anima_") or skill.startswith("hd_fast_")
            or skill.startswith("hd_2_"))


def _i2i_intent(text):
    """这轮原话里有没有**点名图生图机制**（图生图/垫图/重绘/改图/修图/i2i…）。

    2026-10-05 用户拍板：只说「把衣服换成jk」这类改动内容、没说机制名的，
    **不垫图**——改提示词重新画一张（反复垫图会越改越糊，denoise 0.6
    每代丢四成原图信息）。判据直接复用垫图闸门的 `_I2I_EXPLICIT_RE`
    ——路由判据和垫图闸门同源，绝不会「路由判成图生图、闸门又拦下」。"""
    from app.tools.normal.generate_image import _I2I_EXPLICIT_RE
    return bool(_I2I_EXPLICIT_RE.search((text or "").lower()))


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
            tier = _TIER_MAP.get(m.group(1), "base")
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
    """档位 + 画风 → 渠道 id（档位最核心；画风没打/打错按该档默认 clear）。"""
    if tier == "base":
        return "anima_" + style if style else _DEFAULT_SKILL
    if tier:
        return "hd_%s_%s" % (tier, style or "clear")
    return "anima_" + style


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
    """代码直判渠道。返回 (skill or None, 剩余描述)。

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

    - 「三档 gloss 初音未来」→ hd_3_gloss / 初音未来
    - 「三档,glss,初音未来」 → hd_3_gloss / 初音未来（glss 贴回 gloss）
    - 「三档 猫」            → hd_3_clear / 猫（画风没打，档位默认）
    - 「默认初音未来」       → anima_clear / 初音未来（无分隔符也认）
    - 「一档curvy 初音」     → hd_fast_curvy / 初音（连写也认）
    - 「gloss 一个女孩」     → anima_gloss / 一个女孩（只打画风）
    - 「sd 一只猫」          → image_gen_v1 / 一只猫（固定渠道词）
    - 「NAI，…，三档」       → nai / …（谁靠前谁优先）
    - 「三档 … nai」         → hd_3_clear / …（三档靠前）
    - 「这个猪 跑 nai」      → nai / 这个猪（渠道词不限位置）
    - 「nai，少女 柔和光线」  → nai / 少女 柔和光线（**开头**的渠道词最高优先，
      画风词抢不走）
    - 「1girl, soft lighting」→ (None, 原文)  ← soft 是画面内容，不是画风词
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
        skill = _FIXED_CHAN_MAP[hm.group(0).lower()]
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
            skill = _FIXED_CHAN_MAP[m.group(0).lower()]
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
    skill = _FIXED_CHAN_MAP[m.group(0).lower()]
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
    cands = difflib.get_close_matches(token.lower(), sorted(_FIXED_CHAN_MAP),
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
    "nai（画师串/权号这种写法就是它的）/ 默认 / 快档 / 二档 / 三档 / "
    "sd / krea2 / qwen / nffa。")


MENU_TEXT = (
    "🎨 三种出图方式\n"
    "@我 + 描述（中英都行）\n"
    "今日老婆 / 随机萝莉 / 随机兽耳 / 随机女仆\n"
    "贴一段英文提示词\n"
    "\n"
    "引用一张图：\n"
    "+「三档」照着画 ｜ 只@我 反推 ｜ 「提示词」给词 ｜ 说改什么 = 改图\n"
    "\n"
    "「/更多渠道」= 档位/画风/其他AI ｜ 「使用指南」= 详细玩法"
)

GUIDE_TEXT = (
    "🎨 详细使用指南\n"
    "【快速上手】@我 或喊「大大怪」+ 渠道 + 描述：\n"
    "  默认 纳西妲（默认档，最快）\n"
    "  二档 gloss 女骑士（二档=更精细，gloss=油亮画风）\n"
    "  三档 clear 水晶城堡（三档=最高清）\n"
    "  快档 1girl, blue hair（英文提示词原样直接跑，不改动）\n"
    "  私聊直接发一整段英文提示词（不带渠道词）→ 也直接跑，走默认档\n"
    "【档位】默认 < 快档(=一档) < 二档 < 三档，越往后越清晰越慢\n"
    "【画风】clear清晰 / curvy肉感 / gloss油亮 / soft柔和；"
    "跟在档位后，打错或没打按该档默认\n"
    "【渠道】sd（一次多张，多段描述用 --- 分隔）/ krea2 / "
    "qwen（慢·写实·能在图里写中文文字）/ nffa（插画）/ "
    "cunny（超分重渠道·单张 2~4 分钟）/ miao（皮肤质感滑嫩）/ nai（云端）\n"
    "【引用文字】引用带提示词的消息 + 只发渠道词（如「三档」「nai」）→ 照跑；"
    "豆包那种包着客套话的整段复制也认，自动抽英文本体；中英混合也行\n"
    "【引用自己的出图】\n"
    "  引用出图回执 +「再来一张」→ 同提示词换种子重跑\n"
    "  引用图 + 说改什么（「把衣服换成jk」）→ 拿当时的提示词直接改，最快\n"
    "  引用图 + 只@我 → 返回这张图的反推提示词\n"
    "  引用图 + 档位（如「三档」）或「生成这个」→ 反推后直接生成\n"
    "  引用回执或图片 +「查看提示词」→ 直接把这张图当初用的提示词发你（零调用）\n"
    "【改图】引用图 + 明说「图生图」+ 改什么（「图生图 把头发换成银色」）"
    "→ 照着原图改，构图不变；不点名渠道默认动漫档，点名 qwen 走精修（慢）\n"
    "【看真图】引用图 + 说「识图 / 反推 / 看图」→ 强制看图（不用账本缓存）\n"
    "【随机口令】零门槛直接玩：\n"
    "  今日老婆 → 随机角色 + 随机穿搭出一张（每人随机，可反复抽）\n"
    "  随机萝莉 / 随机兽耳 / 随机女仆 → 全新随机角色，每张都不重样\n"
    "提示词用中文描述就行，我来转成画法。"
)

# 「/更多渠道」：把全部跑法罗列一遍（2026-10-05 用户定的新菜单结构——
# 主菜单只留入口，想看细的再主动要）。
MORE_CHAN_TEXT = (
    "🧭 全部跑法\n"
    "【档位】默认 < 快档(一档) < 二档 < 三档，后面直接跟描述"
    "（例：三档 女骑士）\n"
    "【画风】clear 清晰 / curvy 肉感 / gloss 油亮 / soft 柔和，跟在档位后"
    "（例：二档 gloss 少女），没打按该档默认\n"
    "【固定渠道】sd / krea2 / qwen / nffa / cunny / miao / nai"
    "（例：qwen 水晶城堡；sd 支持多段描述用 --- 分隔一次多张；"
    "cunny = 两段超分重渠道，单张约 2~4 分钟；"
    "miao = 皮肤质感滑嫩，2x 超分出 2048×3072）\n"
    "【随机口令】今日老婆 ｜ 随机萝莉 / 随机兽耳 / 随机女仆\n"
    "【引用玩法】引用提示词 + 渠道词 = 照跑；引用图 + 只@我 = 反推；"
    "引用图 + 档位 = 反推后生成；引用图 + 图生图 + 改法 = 照原图改；"
    "出图回执 +「再来一张」= 换种子重跑"
)

# 随机口令与今日老婆（2026-10-05）：斜杠可带可不带，认纯口令。
_RANDOM_CMD_RE = re.compile(r"^\s*/?\s*随机(萝莉|兽耳|女仆)\s*$")
_WAIFU_CMD_RE = re.compile(r"^\s*/?\s*今日老婆\s*$")
# 「/更多渠道」按**关键词**识别（2026-10-05 用户口径）：消息里含「更多渠道」即回
# 全部跑法，不再要求整条精确匹配——用户实际发过「：更多渠道」（全角冒号）掉进兜底。
_MORE_CHAN_RE = re.compile(r"更多渠道")

# ─── 提示词的「写法 + 语言」按渠道分家 ─────────────────────
# 2026-10-05 用户点名要明确写进模板：「如果我给的是中文的需求，他要翻译成
# 英文再跑图」——**这条必须明写**，不然模型看到中文输入很容易把中文原样抄进
# prompt（以前是靠「danbooru 标签式英文」顺带暗示，不够硬）。
# 写法分家（对齐 skills/qwen_image_v1/SKILL.md）：
#   - 标签渠道（anima_* / hd_* / image_gen_v1 / krea2 / nffa / nai）
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
#   - 渠道名清单（anima_clear / hd_3_* / nai … 是我们自己起的，模型猜不出来）
#   - 每个渠道的尺寸/快慢（本地实测值）
#   - `chan_hint`：代码从原话里认出的开头渠道词，直接告诉模型（免它误判）
_MASTER_TEMPLATE = (
    "你是大大怪，一个生图 AI。看用户说的话，理解他想要什么画面，写成提示词，"
    "调工具画出来。用户只是在聊天、问问题、要提示词时，就直接回话，不要调工具。\n"
    "\n"
    "【工具】要动手时，只输出一行 JSON，前后不要写别的字：\n"
    "- {{\"tool\": \"generate_image\", \"prompt\": \"英文提示词\", "
    "\"skill\": \"渠道，可省\", \"source_image\": 1, \"seed\": 123}}\n"
    "  画一张图。prompt 必填、必须是英文。source_image 只在用户要「改这张 / "
    "垫图 / 图生图」而且这一轮确实有图时填 1；seed 只在用户点名要某个种子时填；"
    "用不上的参数一律省略。\n"
    "- {{\"tool\": \"recall_image\", \"id\": \"HT-20261005-123456-789\"}}\n"
    "  查一张图当初真正用的提示词和种子。用户引用一张带编号的图问「这张什么词」"
    "时用它。\n"
    "不需要动手时，输出 {{\"reply\": \"你要说的话\"}}。\n"
    "\n"
    "【渠道 skill】不填 = " + _DEFAULT_SKILL + "。用户点名了渠道就照他说的填。\n"
    "画风四种，跟在档位后面：clear 清晰 / soft 柔和 / gloss 油亮 / curvy 肉感\n"
    "尺寸四档，id 就是「档位_画风」拼起来的：\n"
    "- 默认档 = anima_<画风>，728×1024，最快（例 anima_clear）\n"
    "- 快档   = hd_fast_<画风>，1024×1536（例 hd_fast_clear）\n"
    "- 二档   = hd_2_<画风>，1328×2000（例 hd_2_gloss）\n"
    "- 三档   = hd_3_<画风>，1536×2304，最慢（例 hd_3_clear）\n"
    "  用户说「三档 gloss」→ hd_3_gloss；只说「二档」没提画风 → hd_2_clear。\n"
    "固定渠道（用户说左边这些词，就填右边那个 id）：\n"
    "- nai / nai_wide = NovelAI 云端；nai 是竖版 832×1216，nai_wide 是横版 1216×832。"
    "这两条认画师串和权重语法。\n"
    "- qwen / 千问 / 通义 = qwen_image_v1，云端、慢，prompt 写完整英文句子；"
    "图生图精修走它。\n"
    "- sd = image_gen_v1，能一次出多张（prompt 里用 --- 分段）。\n"
    "- krea2 / nffa / cunny / miao 是用户点名才用的特殊渠道"
    "（cunny 和 miao 很慢，单张好几分钟）。\n"
    "{chan_hint}"
    "\n"
    "【提示词怎么写】\n"
    "- 中文需求翻成英文再写，prompt 里不许出现中文"
    "（要出现在画面里的文字除外，用引号原样写）。\n"
    "- 用户给的**画师串**和权重语法（`1.2::tag::`、`artist:xxx`）原样保留，"
    "别翻译、别删、别改成平铺 tag；渠道是 nai 时它就是画风来源，必须用上。\n"
    "- prompt 只写画面内容，「重绘 / 高清 / 加强细节」这类操作词不要写进去。\n"
    "- **每一轮都是新请求**：角色、服装、动作、场景全按这一轮重新写，"
    "别把上一轮画过的东西抄过来。\n"
    "- 认不出的角色照外貌特征写，不要编不存在的角色名。\n"
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
    "你是生图提示词修正器。用户给你一张图并提出要求。"
    "只输出 JSON 本体，格式：{{\"skill\": \"渠道id\", \"prompt\": \"修正后的"
    "完整英文提示词\"}}\n"
    "- 先看清楚画面（人物、发色发型、瞳色、表情、服装、姿势、场景），"
    "prompt 必须覆盖画面全部要点，用户没提到的细节原样保留\n"
    "- 原提示词{anchor_note}：与画面冲突时，一律以画面为准\n"
    "- skill 沿用「原渠道」，除非用户点名要换\n"
    "- {lang}\n"
    "- 禁止权重语法 (tag:1.2)、{{tag}}、::\n"
    "- 具体角色没把握就写外貌特征+作品名，不要编造不存在的角色名\n"
    "{escape}"
    "{search}"
    "原提示词（渠道 {last_skill}）：\n{last_prompt}\n"
    "用户的话：{text}"
)

# 逃逸口只在这种情况下开：用户**没点名机制词**、只是在图下面随口评价。
# 「画得真好」这种真闲聊不该被硬改一张图出来。
_ESCAPE_ALLOWED = (
    "- **用户的话不是修改/生图请求**（夸奖、闲聊、问别的事）→ 只输出一个 "
    "reverse 字段，值 = 这张图真实的英文 danbooru tag 反推"
    "（reverse 的值要填真实 tag，别照抄这句话）\n")

# 用户明说机制词（图生图/垫图/改图/重绘）→ 意图已经定死，模型没有改判的余地。
# 2026-10-05 用户拍板：明说了图生图还回一段反推提示词，很奇怪。
_ESCAPE_FORBIDDEN = (
    "- 用户已经点名「图生图 / 改图」这类机制词，这是**确定的改图请求**："
    "必须给出 prompt，没有「不是修改请求」这个选项；用户的话再短再笼统，"
    "也要把它并进画面描述，拿不准的地方保留画面原样\n")

# 会话最近一次直达入队的任务（修正/重跑管道的「原提示词」来源）。
# 内存态就够：这些场景发生在刚出图之后，进程重启丢了也就是少个上下文。
_LAST_JOB = {}
_JOB_LOCK = __import__("threading").Lock()


def _remember_job(session_key, skill, prompt):
    with _JOB_LOCK:
        _LAST_JOB[session_key] = {"skill": skill, "prompt": prompt}


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
    解析；引用图取不到会报错回给用户）。仅图生图路由会传。
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
                   表示「没认出渠道词」，完全交给模型判。
      weighted     保留形参：成品串（`::` 权号）现在与普通请求共用同一条模板，
                   权号规则写在模板的「提示词怎么写」段里。**不再影响模板选择。**
      no_default   True → 模型既没判出渠道、代码也没认出来时返回 None
                   （调用方回问），**不静默落默认档**烧一张错风味的图。
    """
    named = (skill or "").strip()
    # 搜索资料由调用方（`decide`）统一算好传进来——**一次请求只搜一次**，
    # 所有分支共用。没传就是空串，模板里那一格为空，照老路走。
    doc = doc or ""
    content = _MASTER_TEMPLATE.format(
        recent=_recent_lines(history) or "（无）",
        text=text,
        search=(_SEARCH_HEADER.format(doc=doc) if doc else ""),
        chan_hint=("\n用户开头点名了渠道：**%s**，就用它。\n" % named) if named
                  else "")
    data = _ask(content)
    if not data:
        return None
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


def _reverse_text(tags):
    """反推结果拼成回复；空 tags 给兜底提示。"""
    if not tags:
        return "没认出这张图，重发一次试试。"
    return _REVERSE_HEADER + "\n" + tags


# ⚠️ 原先这里有一个 `_PROMPT_ASK_RE = re.compile(r"提示词")`，配合下面
# `decide()` 里那条「引用 + 提示词 → 直接回词条、不跑 LLM」的直通分支使用。
# 2026-10-06 随分支一起删（删它的理由见 decide() 里的注释）：只认关键词不认
# 意图，把改图请求误判成查账。


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
_LOCAL_SEED_SKILL_RE = re.compile(r"^(anima_|hd_|qwen_image_v1|image_gen_v1|krea2|nffa|cunny|miao)")


def _revise(text, data_urls, history, channel=None, at_me=False,
            source_image=False, doc=None):
    """改图管道：引用图 + 意见 → 一次调用 → 重跑。

    引用带图改图**一律真识图**（2026-10-05 用户拍板）：账本里存的是当时那句
    提示词，和画面实际内容可能已经对不上（背景改透明那次没看图，出图就不是
    用户要的）。账本提示词降级为「最可信旁证」写进 prompt，不再是唯一来源。

    source_image=True（用户原话点名了图生图机制，`_i2i_intent` 判的）→
    入队时垫本轮引用的那张图（重绘而非重画），渠道不可重绘就降回默认动漫档；
    同时**关掉 reverse 逃逸口**——意图已定死，必须出 prompt。

    返回 None = 意见不是修改请求（只在**没点名机制词**时可能）：@ 轮回反推
    文本、关键词轮静默，由本函数内部处理；用户明说了机制词就不可能走到 None。
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
    # ── 一律一次带图调用（钉死 DeepSeek 官方）——不看图就没有信息源 ─────
    if anchor_prompt:
        anchor_note = "是这张图当时的真实提示词（账本可查），最可信"
    else:
        anchor_skill, anchor_prompt = "（无）", "（无）"
        anchor_note = "没有（引用的不是本机器人画的图），忽略此项，以画面为准"
    ask = _REVISE_TEMPLATE.format(
        escape=_ESCAPE_FORBIDDEN if source_image else _ESCAPE_ALLOWED,
        search=(_SEARCH_HEADER.format(doc=doc) if doc else ""),
        anchor_note=anchor_note,
        last_skill=anchor_skill,
        last_prompt=anchor_prompt,
        lang=_prompt_lang(channel or anchor_skill),
        text=text)
    from app.vision import describe
    try:
        # 不指定 provider = 跟识图配置走（.env，现在是 doubao-seed-2.1-turbo）。
        reply = describe(data_urls[0], prompt=ask)
    except Exception:
        log.exception("[direct] 改图调用失败")
        return "改图请求没发出去，稍后再试。"
    data = _extract_json(reply)
    if not data:
        return "改图请求没解析出来，换个说法再试（如「手改成插兜」）。"
    rev = (data.get("reverse") or "").strip()
    # 用户明说了机制词（source_image）→ 模板里根本没给这条逃逸口，模型不该
    # 走到这。万一它还是不听话，宁可让他换个说法，也别把反推当结果发回去
    # ——「明说图生图却回一段反推提示词」正是 2026-10-05 用户报的怪事。
    if rev and not source_image:
        # 不是修改请求 → @ 轮把反推给他（2026-10-05 用户口径）；
        # 关键词轮静默止刷屏。
        return _reverse_text(rev) if at_me else None
    prompt = (data.get("prompt") or "").strip()
    if not prompt:
        return "改图请求没解析出来，换个说法再试（如「手改成插兜」）。"
    skill = channel or (data.get("skill") or "").strip()
    if skill not in _allowed_skills():
        skill = anchor_skill if anchor_skill in _allowed_skills() \
            else _DEFAULT_SKILL
    if source_image and not _redraw_capable(skill):
        skill = _DEFAULT_SKILL
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
    if _IMG_ONLY_RE.match(text):
        text = ""       # 裸图（只发图没说话）：见 _IMG_ONLY_RE 处的拍板
    session_key = qq_api.current_session_key()
    quoted = (qq_api.current_quoted_text() or "").strip()
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

    # 「查看提示词」直通（2026-10-06 晚用户要）：打这 5 个字 → 直接把这张图
    # 当初真实用的提示词发回去，零 LLM、比让 AI 现推准。
    # 只认这 5 个字（不像之前被删的那条——那是「任何含『提示词』+ 有引用」就
    # 吞整轮，把「角色换成花火」这类改图轮误判成查账；这次关键词足够具体，
    # 不会撞改图请求）。查哪张图：引用机器人发的带 HT 编号的回执/图，或正文
    # 直接贴了 HT 编号——`_ledger_hit` 从引用正文+原话里抽编号去账本查。
    # 没引用/没编号 → 回一句提示，不误触发 AI 生图（其他没打这 5 字的轮照常走 AI）。
    if "查看提示词" in text:
        prompt, skill, seed = _ledger_hit(quoted + " " + text)
        if prompt:
            extra = ""
            if skill:
                extra += "渠道：" + skill + "\n"
            if seed:
                extra += "种子：" + seed + "\n"
            return ("这张图当初用的提示词：\n" + prompt
                    + ("\n" + extra if extra else ""))
        return ("没识别到图片编号（HT-…）。引用机器人发的带编号的图，"
                "或直接把编号发我。")

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
    if data_urls:
        if desc and (_GENERIC_I2I_RE.match(desc)
                     or (ch and _RUN_THIS_RE.match(desc))):
            desc = ""       # 「基于图片帮我生成 / 跑这张」是空话，不算意见
        # 图生图意图（词表与垫图闸门同源）优先：先于「渠道+反推」，否则
        # 「三档 图生图」会被当成普通反推、还落在不支持重绘的 hd_3 上。
        if desc and _i2i_intent(text):
            if ch == "qwen_image_v1" and desc:
                # qwen 参考图编辑：只吃一句改动指令，直通零调用。
                # 机制词（图生图/垫图…）不是指令本体，剥掉再给。
                inst = re.sub(r"图生图|垫[个一?张]?图?|改图|重绘", "", desc)
                inst = inst.strip(" ，,、:：")
                if inst:
                    _remember_job(session_key, ch, inst)
                    return _enqueue(ch, inst, text, source_image=True)
                desc = ""                       # 只说了机制词 → 走下面修正
            # 默认动漫档重绘：视觉模型出修正后的完整 tag，入队垫图。
            return _revise(text, data_urls, history, channel=ch,
                           at_me=at_me, source_image=True, doc=doc)
        # 引用图 + 只打渠道/档位 → 自家图直接用账本提示词（零调用），
        # 别人的图才看图反推（1 次识图），然后入队生成。自家图命中时**连
        # 种子一起复刻**（2026-10-05 用户拍板）：同提示词新种子=构图细节
        # 必然变，用户要的「原样跑」是构图贴近原图、只换画质档位。别人的
        # 图（账本没中）没有种子可复刻，照旧随机。
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
        # 引用图 +「帮我生成这个 / 跑一下这张图」（没渠道）→ 用户口径：
        # 反推 → 重画，别反问。自家图同样先用账本提示词（零调用）。
        if not ch and desc and _IMG_GEN_INTENT_RE.search(desc):
            own_prompt, _skill, _own_seed = _ledger_hit(quoted)
            tags = own_prompt or _recall_tags(data_urls[:1])
            if not tags:
                return "没认出引用的图，重发一次，或直接 @我 渠道 描述。"
            _remember_job(session_key, _DEFAULT_SKILL, tags)
            return _enqueue(_DEFAULT_SKILL, tags, text)
        # 看图说话（@ 轮、没渠道词）：「这画的是什么/什么画风」→ 单次识图
        # 中文描述，不进改图管道（那口是奔着生成提示词去的）。
        if at_me and not ch and _DESCRIBE_RE.search(text):
            from app.vision import describe
            try:
                reply = (describe(data_urls[0],
                                  prompt=_DESCRIBE_PROMPT) or "").strip()
            except Exception:
                log.exception("[direct] 看图说话失败")
                return "图没看成，稍后再试。"
            return reply or "没认出这张图，重发一次试试。"
        # 其余（有意见 / 无渠道词）→ 改图管道；模型判「不是修改请求」时
        # @ 轮回反推文本、关键词轮闭嘴吞轮（明说机制词的轮不会走到这）。
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
    if data:
        _remember_job(session_key, data["skill"], data["prompt"])
        return _enqueue(data["skill"], data["prompt"], text)
    return None      # 转译失败 → 不接管，交回上层（不再回菜单）
