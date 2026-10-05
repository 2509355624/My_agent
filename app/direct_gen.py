#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""直达生图管道（2026-10-04，10-05 两次扩版）。

动机：agent 路径一轮动辄几万 token（系统头 + 历史 + 工具协议 + 多轮循环），
而「@我 画一只猫」这种请求本质只需要一次轻量转译。这条管道用**一次 LLM 调用**
把请求转成 {渠道, 提示词}，代码直接入队——整轮不过 agent。

结构（2026-10-05 渠道解析收归代码后）：
- /菜单（或裸 @）→ 写死的常量文本，零 LLM。
- 渠道词打头（「三档,glss,初音未来」「默认初音未来」）→ **代码正则先抽
  渠道**（档位最核心：画风词打错/没打 → 档位默认 clear），LLM 只做剩下的
  描述扩写——渠道映射从此确定性 100%。
- 裸 @ + 描述 / 画图动词 → 一次转译（LLM 顺带判渠道，判不出用默认）。
- 引用正文 + 只打渠道词（「大大怪 三档」引用一条带提示词的消息）→ 引用
  正文当描述，走锁定渠道扩写；**英文 tag 直接原样入队（零 LLM）**——
  用户贴的就是最终提示词，过一遍模型只会改坏还烧钱。豆包/ChatGPT 那种
  「好的，提示词如下：…」包着客套话的，先抽出英文段再直通。
- 引用生图回执 +「再来一张」→ 同提示词换种子重跑（_LAST_JOB 现成有），零转译。
- 引用图 + @（没别的说）→ **反推提示词**发回去，不生成；引用图 + 档位
  （「三档」，或「快档 基于图片帮我生成」这类空话）→ 反推后直接生成；
  引用图 +「帮我生成这个 / 跑一下这张图」（没渠道）→ 默认档反推后重画；
  引用图 +「图生图 / 换成…」→ **垫图改图**（source_image=1，默认动漫档
  重绘，点名 qwen 走参考图编辑、改动指令直通零调用）。
- 引用图 + 意见 → 改图，**按「账本有没有这张图」分流省钱**（2026-10-05
  用户拍板）：引用**自家 HT 图**且没点名识图 → **纯文本修正**（账本里
  当时的真实提示词 + 意见 → 改出新 prompt，走降级链，不花识图钱）；
  点名识图（识图/反推/看图…）或引用**别人的图** → **一次带图调用**（钉死
  DeepSeek 官方）。引用图 + @ / 档位 /「生成这个」也优先用账本提示词
  （零调用），账本没中才看图反推。意见不是修改请求 → @ 轮回提示词、
  关键词轮静默。
- 引用消息但没渠道词（「生图」「这词什么意思」）→ **零 API**，固定指路
  文本——别烧调用更别瞎猜（2026-10-05 用户点名的三种边界全落在这）。
- **agent 已退场（2026-10-05 用户拍板）**：@ 轮要么走工具要么回菜单。
  关键词命中但纯闲聊（别的机器人的聊天里提到名字）→ **静默不理**，止住
  菜单刷屏。唯一放行是总开关关闭（ENABLED=False）和主动接话轮（voluntary）。
"""
import difflib
import json
import logging
import os
import re

from app import image_jobs, image_log, llm, qq_api
from app.config import QQ_GROUP_KEYWORDS
from app.confirm_gate import _ATTRIBUTION_RE
from app.skills import list_skills

log = logging.getLogger(__name__)

# 总开关（.env DIRECT_GEN=0 可关）：管道只影响「@ + 画图动词」的轮，关掉后
# 全部落回 agent。测试里用 mock.patch.object(direct_gen, "ENABLED", False)
# 关——不然跑真实 _run_turn 的用例会截胡，甚至真调转译 API。
ENABLED = os.getenv("DIRECT_GEN", "1") == "1"

# ─── 渠道清单 ────────────────────────────────────────────
# hd 渠道是「档位_画风」的组合命名（skills/ 下真实存在）；其余是固定渠道名。
# 别让这个集合和 skills/ 目录漂移：_allowed_skills() 每次现场核对目录，
# 对不上的（被归档/新增）以目录为准加减。
_HD_TIERS = ("fast", "2", "3")
_HD_STYLES = ("clear", "curvy", "gloss", "soft")
_FIXED_SKILLS = ("anima_clear", "anima_curvy", "anima_gloss", "anima_soft",
                 "image_gen_v1", "image_gen_v1_hires", "krea2", "nffa",
                 "qwen_image_v1", *image_jobs.NAI_SKILLS)
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

# ─── 渠道解析（代码直判，LLM 不再碰渠道） ─────────────────
# 档位是最核心的关键词；画风词是可选项。用户口径（2026-10-05）：档位打了、
# 画风词打错或没打 → 按该档默认画风 clear。
_TIER_MAP = {"三档": "3", "3档": "3", "二档": "2", "2档": "2",
             "快档": "fast", "一档": "fast", "1档": "fast"}
_TIER_RE = re.compile(r"(三档|二档|一档|快档|[123]档|默认)")
# 英文画风词的前后不能是字母：「一档curvy」连写也要认（CJK 后面 \b 不成立，
# 2026-10-05 踩过），但「glossy」这种词中片段不能算。
_STYLE_RE = re.compile(r"(?<![a-z])(clear|curvy|gloss|soft)(?![a-z])"
                       r"|清晰|肉感|油亮|柔和|柔软", re.I)
_STYLE_ALIASES = {"clear": "clear", "curvy": "curvy", "gloss": "gloss",
                  "soft": "soft", "清晰": "clear", "肉感": "curvy",
                  "油亮": "gloss", "柔和": "soft", "柔软": "soft"}
# 固定渠道词：用户点名就锁定，不劳 LLM。前后不能是字母数字（防 sdXL、
# 「qwen2」这类词中片段误中）。
_FIXED_CHAN_MAP = {"sd": "image_gen_v1", "krea2": "krea2",
                   "qwen": "qwen_image_v1", "nffa": "nffa", "nai": "nai"}
_FIXED_CHAN_RE = re.compile(r"(?<![a-z0-9])(sd|krea2|qwen|nffa|nai)(?![a-z0-9])",
                            re.I)
# 「基于图片帮我生成」这类空话：引用图 + 渠道词时不算修改意见，
# 意思就是「反推这张图然后按渠道跑」。
_GENERIC_I2I_RE = re.compile(
    r"^(?:帮我|给我|请)?(?:基于|按照|根据|参考|用)这?(?:张|个)?(?:图片|图|照片|画)"
    r"(?:帮我|给我)?(?:直接)?(?:生成|画|跑|出|出图|来一张?|来一幅?|生图)?"
    r"(?:一?[张幅])?[吧呀啊哦。.!！？?，,\s~～]*$")
# 描述尾巴上的动词残渣：「这个猪 跑 nai」剥掉渠道词后剩「这个猪 跑」。
_DESC_TAIL_RE = re.compile(
    r"\s*(?:帮我|给我)?\s*(?:跑|画一张|生成一张|来一张|来一幅|出一张?图?|生图)\s*$")

# 引用图 +「帮我生成这个 / 跑一下这张图片 / 处理一下这张图」（没打渠道）→
# 用户口径（2026-10-05）：默认反推 → 重画，别反问。动词和「这/个/张/图」
# 必须紧挨着，避免「帮我把生成的图改一下」这种改图话误中。
_IMG_GEN_INTENT_RE = re.compile(
    r"(?:帮我|给我|请)?(?:直接)?(?:生成|跑|处理|画|出|来)(?:一?下|一?[张幅个])?"
    r"这?(?:张|个)?(?:图片|图|画|个)")

# 引用消息但没打渠道词的固定指路（零 API，@ 轮和关键词轮都回这条）。
_QUOTE_NO_CHAN_TEXT = (
    "引用的内容收到了，但这轮没说渠道。再发一条渠道词（默认 / 快档 / 一档 / "
    "二档 / 三档 / sd / krea2 / qwen / nffa / nai）就能直接跑；"
    "要照着改图就引用图片并说「图生图」。")

# ─── 看图说话（单次识图，不进改图管道） ───────────────────
# 触发词收得窄：不能含「看图/识图」（那是反推 tag 的口），也不能含裸
# 「这是什么」——「这是什么破手，改成插兜」会被劫走不进改图。
_DESCRIBE_RE = re.compile(
    r"画的?(?:是|了)什么|什么画风|什么风格|描述一下|介绍一下|讲讲|说说"
    r"|什么内容|帮我?看看|这是啥")
_DESCRIBE_PROMPT = (
    "用中文自然语言描述这张图片：主体是谁/什么、外貌服装、动作姿势、"
    "场景背景、画风。150 字以内，直接描述，不分点、不寒暄。")

# ─── 私聊单轮问答（单次调用，无工具无循环） ───────────────
# agent 循环一轮能滚出十几万 token（while 拉工具结果再续写），单次管道
# 固定一次调用封顶——成本差百倍（2026-10-05 用户拍板）。所以问答永远
# 单轮：答完即收口，群聊绝不启用（保持「生图或菜单」铁律）。
_QA_RE = re.compile(r"[?？]\s*$")
_QA_SYSTEM = (
    "你是 QQ 机器人「大大怪」的私聊问答模式。用中文简短回答（尽量不超过"
    "120 字），只回答当前这一个问题：不追问、不反问、不列长清单。"
    "如果用户其实想生成图片，提示他按「@我 渠道 描述」的格式发。")
# 本轮问答的落史暂存：session_key -> (user_text, reply)。qq_bot 送出回复
# 后 pop 出来落 history，下一轮问答才有上下文。没被 pop 的（排队轮）
# 会在下一个非空 direct_reply 轮被冲掉，不积累。
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


def _extract_english_block(s):
    """从引用正文里抽出英文提示词段，抽不出返回 ""。

    豆包/ChatGPT 的回复长这样：「好的，那么我给你的提示词是下面的，你可以
    直接复制粘贴使用：\n1girl, solo, …」——用户整段复制来引用。把客套话
    一起喂转译既浪费又会被 9B 改坏，所以先把英文本体剥出来直通。
    """
    s = (s or "").strip()
    if not s:
        return ""
    if _is_english_tags(s):
        return s
    best = ""
    for line in s.splitlines():
        line = line.strip().strip("`*#→ ")
        if not line:
            continue
        if line.isascii() and _is_english_tags(line):
            if len(line) > len(best):
                best = line
            continue
        # 行内中英混排：找足够长的 ASCII 连续段，且至少 3 个词才像 tag 串
        # （下限 30 字符，别把「krea2」「NO.7749」这种短词当提示词）。
        for m in re.finditer(r"[A-Za-z][A-Za-z0-9 ,'\-_:()|]{29,}", line):
            seg = m.group(0).strip(" ,'-_:()|")
            if len(re.split(r"[, ]+", seg)) >= 3 and len(seg) > len(best):
                best = seg
    return best


def _quote_merge(quoted, desc):
    """「引用正文 + 用户补的话」合并成转译输入（引用+渠道词+额外话路径）。

    引用里能抽出英文段就用它当本体（豆包包装）；引用是噪音/占位符就只用
    用户自己的话。两样都没有返回 ""。
    """
    q = (quoted or "").strip()
    if q and not _NOISE_QUOTE_RE.search(q) and not q.startswith("🎨") \
            and not _PLACEHOLDER_QUOTE_RE.search(q):
        body = _extract_english_block(q) or q
        return body + ("\n（用户补充：%s）" % desc if desc else "")
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


def _parse_channel(text):
    """代码直判渠道。返回 (skill or None, 剩余描述)。

    - 「三档 gloss 初音未来」→ hd_3_gloss / 初音未来
    - 「三档,glss,初音未来」 → hd_3_gloss / 初音未来（glss 贴回 gloss）
    - 「三档 猫」            → hd_3_clear / 猫（画风没打，档位默认）
    - 「默认初音未来」       → anima_clear / 初音未来（无分隔符也认）
    - 「一档curvy 初音」     → hd_fast_curvy / 初音（连写也认）
    - 「gloss 一个女孩」     → anima_gloss / 一个女孩（只打画风）
    - 「sd 一只猫」          → image_gen_v1 / 一只猫（固定渠道词）
    - 「这个猪 跑 nai」      → nai / 这个猪（渠道词不限位置）
    - 没有任何渠道词         → (None, 原文)
    """
    text = text.strip()
    tier = style = None
    tm = _TIER_RE.search(text)
    if tm:
        tier = _TIER_MAP.get(tm.group(1), "base")   # 「默认」→ base
    sm = _STYLE_RE.search(text)
    if sm:
        style = _STYLE_ALIASES.get(sm.group(0).lower(),
                                   _STYLE_ALIASES.get(sm.group(0)))
    if tier is None and style is None:
        fm = _FIXED_CHAN_RE.search(text)
        if fm:
            skill = _FIXED_CHAN_MAP[fm.group(0).lower()]
            desc = text[:fm.start()] + " " + text[fm.end():]
            desc = _DESC_TAIL_RE.sub("", desc.strip())
            desc = re.sub(r"^[\s,，、:：\-]+|[\s,，、:：\-]+$", "", desc)
            return skill, desc
        return None, text
    # 画风词没匹配到但档位在：档位后紧跟的一小段纯 ASCII 可能是打错的画风
    # （glss/sof），贴得回来就修正，贴不回来按档位默认 clear。
    if tier is not None and style is None:
        segs = [s for s in re.split(r"[\s,，、:：]+", text[tm.end():].strip())
                if s]
        cand = segs[0].strip(".。!！?？") if segs else ""
        if cand and cand.isascii() and 3 <= len(cand) <= 8 \
                and not _INTENT_RE.search(cand):
            close = difflib.get_close_matches(
                cand.lower(), _HD_STYLES, n=1, cutoff=0.75)
            if close:
                style = close[0]
                text = text.replace(cand, " ", 1)
    if tier == "base":
        skill = "anima_" + style if style else _DEFAULT_SKILL
    elif tier:
        skill = "hd_%s_%s" % (tier, style or "clear")
    else:
        skill = "anima_" + style
    desc = text
    if sm:
        desc = desc[:sm.start()] + " " + desc[sm.end():]
    if tm:
        desc = desc.replace(tm.group(0), " ", 1)
    desc = _DESC_TAIL_RE.sub("", desc.strip())
    desc = re.sub(r"^[\s,，、:：]+|[\s,，、:：]+$", "", desc)
    return skill, desc


MENU_TEXT = (
    "🎨 生图指令（@我 / 喊我名字 + 渠道 + 描述）\n"
    "默认 纳西妲\n"
    "三档 gloss 一个女孩\n"
    "快档 1girl, blue hair（英文直接跑）\n"
    "渠道：默认/快档(一档)/二档/三档 + 画风 clear/curvy/gloss/soft"
    "（没打按默认）｜ sd / krea2 / qwen / nffa / nai\n"
    "引用提示词 + 渠道词 → 照跑；出图回执 +「再来一张」→ 换种子重跑\n"
    "引用图：只@我 = 反推提示词 ｜ 加档位 = 直接生成 ｜ 说改什么 = 改图"
    " ｜ 说「图生图」= 照原图改\n"
    "发「使用指南」或「/菜单」看详细版"
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
    "qwen（慢·写实·能在图里写中文文字）/ nffa（插画）/ nai（云端）\n"
    "【引用文字】引用带提示词的消息 + 只发渠道词（如「三档」「nai」）→ 照跑；"
    "豆包那种包着客套话的整段复制也认，自动抽英文本体；中英混合也行\n"
    "【引用自己的出图】\n"
    "  引用出图回执 +「再来一张」→ 同提示词换种子重跑\n"
    "  引用图 + 说改什么（「把衣服换成jk」）→ 拿当时的提示词直接改，最快\n"
    "  引用图 + 只@我 → 返回这张图的反推提示词\n"
    "  引用图 + 档位（如「三档」）或「生成这个」→ 反推后直接生成\n"
    "【改图】引用图 + 明说「图生图」+ 改什么（「图生图 把头发换成银色」）"
    "→ 照着原图改，构图不变；不点名渠道默认动漫档，点名 qwen 走精修（慢）\n"
    "【看真图】引用图 + 说「识图 / 反推 / 看图」→ 强制看图（不用账本缓存）\n"
    "提示词用中文描述就行，我来转成画法。"
)

_TRANSLATE_TEMPLATE = (
    "你是生图指令解析器。把用户的请求转成 JSON，只输出 JSON 本体，"
    "格式：{{\"skill\": \"渠道id\", \"prompt\": \"英文提示词\"}}\n"
    "渠道规则：\n"
    "- hd 渠道命名 = hd_档_画风：档∈{{fast,2,3}}，画风∈{{clear清晰,curvy肉感,"
    "gloss油亮,soft柔和}}（如「快档」=hd_fast_*、「二档」=hd_2_*）\n"
    "- 「默认」= " + _DEFAULT_SKILL + "；「默认 + 画风词」= anima_画风"
    "（如「默认 gloss 纳西妲」= anima_gloss）\n"
    "- 固定渠道：" + " ".join(_FIXED_SKILLS) + "\n"
    "- 用户点名了档位/渠道就映射过去；没点名（包括只说「高清」这种画质词）"
    "一律用 " + _DEFAULT_SKILL + "\n"
    "prompt 规则：动漫标签式英文（danbooru 风格，逗号分隔短语）；"
    "禁止权重语法 (tag:1.2)、{{tag}}、::；具体角色没把握就写外貌特征+作品名，"
    "不要编造不存在的角色名。\n"
    "如果请求跟画图无关，输出 {{\"skill\": \"\", \"prompt\": \"\"}}\n"
    "最近对话（用于理解「刚才那只」「换成卡通风格」这类指代）：\n{recent}\n"
    "用户请求：{text}"
)

_LOCKED_TEMPLATE = (
    "你是生图提示词扩写器。渠道已定：{skill}，不要改。"
    "把用户的描述转成 danbooru 标签式英文提示词（逗号分隔短语），"
    "禁止权重语法 (tag:1.2)、{{tag}}、::；具体角色没把握就写外貌特征+作品名，"
    "不要编造不存在的角色名。\n"
    "描述本来就是英文标签的（引用来的提示词），整理合并后原样保留，别翻成中文。\n"
    "只输出 JSON 本体：{{\"skill\": \"{skill}\", \"prompt\": \"英文提示词\"}}\n"
    "最近对话：\n{recent}\n"
    "用户描述：{text}"
)

_REVISE_TEMPLATE = (
    "你是生图提示词修正器。用户给你一张图并提出要求。"
    "只输出 JSON 本体，格式：{{\"skill\": \"渠道id\", \"prompt\": \"修正后的"
    "完整英文提示词\"}}\n"
    "- 先看清楚画面（人物、发色发型、瞳色、表情、服装、姿势、场景），"
    "prompt 必须覆盖画面全部要点，用户没提到的细节原样保留\n"
    "- 原提示词{anchor_note}：与画面冲突时，一律以画面为准\n"
    "- skill 沿用「原渠道」，除非用户点名要换\n"
    "- danbooru 标签式英文，逗号分隔短语；禁止权重语法 (tag:1.2)、{{tag}}、::\n"
    "- 具体角色没把握就写外貌特征+作品名，不要编造不存在的角色名\n"
    "- **用户的话不是修改/生图请求**（夸奖、闲聊、问别的事）→ 改输出 "
    "{{\"reverse\": \"这张图的英文tag反推\"}}\n"
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


def _qa_answer(text, history):
    """私聊单轮问答：一次 LLM 调用直答，无工具、无循环。

    历史只带最近 6 条（防上下文滚大），答完把本轮存进 _QA_LAST 供
    qq_bot 落史。失败返回 ""（decide 会兜底回菜单）。
    """
    msgs = [{"role": "system", "content": _QA_SYSTEM}]
    recent = [m for m in (history or [])
              if m.get("role") in ("user", "assistant")
              and isinstance(m.get("content"), str) and m["content"].strip()]
    for m in recent[-6:]:
        msgs.append({"role": m["role"], "content": m["content"][:500]})
    msgs.append({"role": "user", "content": text})
    try:
        reply = (llm.call_llm(msgs, timeout=30) or "").strip()
    except Exception as e:
        log.warning("[direct] 问答调用失败：%s", e)
        return ""
    reply = reply[:800]
    if reply:
        _QA_LAST[qq_api.current_session_key()] = (text, reply)
    return reply


def pop_qa(session_key):
    """取走本轮问答的落史记录（user_text, reply），没有返回 None。"""
    return _QA_LAST.pop(session_key, None)


def _humanize_error(result):
    """工具返回的「错误：…」文案是写给模型看的，尾巴带着指示语句。
    直达管道直接发给真人，只留第一句。"""
    first = result.split("。")[0].strip()
    return first if first.endswith("。") else first + "。"


def _enqueue(skill, prompt, text, source_image=False, seed=None):
    """转译结果落队。返回要发回会话的文本；成功且回执已直发返回 ""。

    source_image=True → 垫本轮引用的那张图（传 "1"，下游 comfy_src.resolve
    解析；引用图取不到会报错回给用户）。仅图生图路由会传。
    seed 非空 → 复刻账本里那张图的种子（引用 HT 图换档场景），下游
    _resolve_seed 会校验范围；只往本机渠道传（NAI 拒收 seed）。
    """
    from app.tools.normal import generate_image as gi
    extra = {}
    if source_image:
        extra["source_image"] = "1"
    if seed:
        extra["seed"] = seed
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


def _translate(text, history, skill=None):
    """一次 LLM 调用转成 {skill, prompt}。

    skill 给定 → 渠道锁定，LLM 只扩写描述（「引用提示词 + 渠道词」路径）；
    不给 → 整句交给 LLM 判渠道（画图动词 / 裸 @ 描述路径）。
    拿不到有效结果返回 None。
    """
    if skill:
        tmpl = _LOCKED_TEMPLATE
    else:
        tmpl = _TRANSLATE_TEMPLATE
    content = tmpl.format(skill=skill, recent=_recent_lines(history) or "（无）",
                          text=text)
    data = _ask(content)
    if not data:
        return None
    user_prompt = (data.get("prompt") or "").strip()
    out_skill = (data.get("skill") or "").strip() or skill or _DEFAULT_SKILL
    if not user_prompt:
        return None
    if out_skill not in _allowed_skills():
        log.warning("[direct] 未知渠道 %r，降回默认 %s", out_skill,
                    _DEFAULT_SKILL)
        out_skill = _DEFAULT_SKILL
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


# 引用图轮里用户**点名要识图**（识图/反推/看图/优化提示词…）才把图发给
# 视觉模型。自家图账本里存着当时的真实提示词，改字就行——看图是白花的钱
# （2026-10-05 用户拍板）。想看真图（比如画面和提示词有出入）就明说「识图」。
_VISION_ASK_RE = re.compile(r"识图|反推|看图|读图|识别|图里|优化提示词")


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
_LOCAL_SEED_SKILL_RE = re.compile(r"^(anima_|hd_|qwen_image_v1|image_gen_v1|krea2|nffa)")


# 引用自家图 + 意见的**纯文本修正**模板（2026-10-05 用户拍板：账本里有当时
# 真实提示词，改字就行，不必把图发给视觉模型——识图输入比纯文本贵，且文本
# 调用走降级链，火山免费额度正好顶上）。不带最近历史：账本提示词 + 用户的话
# 就是全部事实来源，历史里有旧 tag 时小模型照抄（02:59 实录教训）。
_TEXT_REVISE_TEMPLATE = (
    "你是生图提示词修正器。用户引用了本机器人之前生成的一张图并提出要求。"
    "只输出 JSON 本体，格式：{{\"prompt\": \"修改后的完整英文提示词\"}}\n"
    "- 下面的「原提示词」是那张图当时的真实提示词，最可信：以它为基底按用户"
    "要求改，用户没提到的细节原样保留\n"
    "- danbooru 标签式英文，逗号分隔短语；禁止权重语法 (tag:1.2)、{{tag}}、::\n"
    "- 具体角色没把握就写外貌特征+作品名，不要编造不存在的角色名\n"
    "- **用户的话不是修改请求**（夸奖、闲聊、问别的事）→ 输出 {{\"skip\": true}}\n"
    "原提示词（渠道 {skill}）：\n{prompt}\n"
    "用户的话：{text}"
)


def _revise(text, data_urls, history, channel=None, at_me=False,
            source_image=False):
    """改图管道：引用图 + 意见 → 一次调用 → 重跑。

    按「账本有没有这张图」分流（2026-10-05 用户拍板）：
    - 引用**自家图**（账本命中当时真实提示词）且没点名识图 → **纯文本
      修正**：账本提示词 + 意见 → 改出新 prompt，走降级链（省一次识图钱）；
    - 点名识图（识图/反推/看图… `_VISION_ASK_RE`）或引用**别人的图** →
      **一次带图调用**（钉死 DeepSeek 官方）——不看图就没有信息源。

    source_image=True（用户原话点名了图生图机制，`_i2i_intent` 判的）→
    入队时垫本轮引用的那张图（重绘而非重画）；渠道不可重绘就降回默认动漫档。

    返回 None = 意见不是修改请求（skip）：@ 轮回提示词文本、关键词轮静默，
    由本函数内部处理。
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
    # ── 自家图 + 没点名识图 → 纯文本修正（省一次识图调用）──────────
    if anchor_prompt and not _VISION_ASK_RE.search(text):
        data = _ask(_TEXT_REVISE_TEMPLATE.format(
            skill=anchor_skill or "（无）", prompt=anchor_prompt, text=text))
        if not data:
            return "改图请求没解析出来，换个说法再试（如「手改成插兜」）。"
        if data.get("skip"):
            # 没有修改意图 → @ 轮把当时的提示词给他（等于反推，还更准）；
            # 关键词轮静默止刷屏。
            return _reverse_text(anchor_prompt) if at_me else None
        prompt = (data.get("prompt") or "").strip()
        if not prompt:
            return "改图请求没解析出来，换个说法再试（如「手改成插兜」）。"
        skill = channel or anchor_skill
        if skill not in _allowed_skills():
            skill = _DEFAULT_SKILL
        if source_image and not _redraw_capable(skill):
            skill = _DEFAULT_SKILL
        _remember_job(session_key, skill, prompt)
        return _enqueue(skill, prompt, text, source_image=source_image)
    # ── 点名识图 / 别人的图 → 一次带图调用（钉死 DeepSeek 官方）─────
    if anchor_prompt:
        anchor_note = "是这张图当时的真实提示词（账本可查），最可信"
    else:
        anchor_skill, anchor_prompt = "（无）", "（无）"
        anchor_note = "没有（引用的不是本机器人画的图），忽略此项，以画面为准"
    ask = _REVISE_TEMPLATE.format(
        anchor_note=anchor_note,
        last_skill=anchor_skill,
        last_prompt=anchor_prompt,
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
    if rev:
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
    - None      ：不接管（总开关关闭 / 主动接话轮）
    - ""        ：已接管但闭嘴（关键词轮引用图 + 夸奖这类 skip——吞掉整轮，
                  绝不让它掉回 agent 接话）
    - 其他文本  ：接管并把这段发回会话（菜单/反推/错误提示）

    2026-10-05 用户拍板（233 粉丝群实录：闲聊句命中关键词掉回 agent 接话
    「大大怪，到」）：**@ 轮和关键词轮，没引用发言/图片时结局只有两种——
    生图或菜单**，绝不回聊天。带引用图轮 skip 时 @ 轮回反推文本、关键词轮
    闭嘴（""）。
    """
    text = _strip_own_names(_strip_attribution(own_text))
    session_key = qq_api.current_session_key()
    quoted = (qq_api.current_quoted_text() or "").strip()
    # 详细使用指南（/菜单、使用指南）：零 LLM，常量直回。比菜单判断更前，
    # 因为「/菜单」含「菜单」二字，得先于短菜单分支。
    if _GUIDE_RE.match(text):
        return GUIDE_TEXT
    # 菜单与裸 @：零 LLM，直接回常量（管道关着也照回——它本来就免费）。
    # 例外：带着引用图的空话/菜单词 → 按用户口径给反推提示词（不生成）；
    # 自家图直接回账本里的当时提示词（2026-10-05 用户拍板：比看图现推准
    # 还省一次识图），想看真图就明说「识图」。
    if not text or _MENU_RE.match(text):
        if data_urls and ENABLED and not voluntary:
            own_prompt, _skill, _own_seed = _ledger_hit(quoted)
            if own_prompt:
                return _reverse_text(own_prompt)
            return _reverse_text(_recall_tags(data_urls))
        return MENU_TEXT
    if not ENABLED:
        return None
    # 主动接话轮（没人 @ 它）不进管道，行为维持原样。
    if voluntary:
        return None

    ch, desc = _parse_channel(text)

    # ── 引用图轮 ──────────────────────────────────────────
    if data_urls:
        if desc and _GENERIC_I2I_RE.match(desc):
            desc = ""       # 「基于图片帮我生成」是空话，不算意见
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
                           at_me=at_me, source_image=True)
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
        # 其余（有意见 / 无渠道词）→ 改图管道；skip=关键词轮闭嘴吞轮。
        reply = _revise(text, data_urls, history, channel=ch, at_me=at_me)
        return reply if reply is not None else ""

    # 「再来一张」（不引用、不带渠道词）：同提示词换种子重跑，零转译。
    last = _last_job(session_key)
    if not ch and _AGAIN_RE.search(text) and last:
        _remember_job(session_key, last["skill"], last["prompt"])
        return _enqueue(last["skill"], last["prompt"], text)

    # 只打了渠道词（「大大怪 三档」+ 引用 / 干发「三档」）。
    if ch and not desc:
        # 引用的是反推回复 → 剥头直用 tag（零 LLM）。
        if quoted.startswith(_REVERSE_HEADER):
            body = quoted.split("\n", 1)[1].strip() if "\n" in quoted else ""
            if body:
                _remember_job(session_key, ch, body)
                return _enqueue(ch, body, text)
        if quoted and not _NOISE_QUOTE_RE.search(quoted) \
                and not quoted.startswith("🎨"):
            # 引用正文是占位符（图取不到/消息读不出）→ 绝不喂转译：9B 会
            # 把最近对话里的旧 tag 抄出来（02:59 实录），直接告诉用户重发。
            if _PLACEHOLDER_QUOTE_RE.search(quoted):
                return ("引用的内容没能取到（图片可能已过期，或引用的是我发的"
                        "消息）。把图/文字重新发出来再发渠道词，或直接 "
                        "@我 渠道 描述。")
            # 引用里抽得出英文提示词段 → 原样入队，过模型只会改坏。
            # 整段纯英文和豆包包装（客套话 + 英文段）都落在这。
            eng = _extract_english_block(quoted)
            if eng:
                _remember_job(session_key, ch, eng)
                return _enqueue(ch, eng, text)
            # 转译**不带最近历史**：引用正文是唯一描述来源，历史里有旧 tag
            # 时小模型照抄（负向规则它执行不了，只能断来源）。
            data = _translate(quoted, [], skill=ch)
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

    # 引用了消息但没渠道词（「生图」「帮我改改」「这词什么意思」都算）→
    # 零 API 给确定反馈。烧一次转译也只会对着「生图」两个字瞎编
    # （2026-10-05 用户点名的边界）。
    if quoted and not ch:
        return _QUOTE_NO_CHAN_TEXT

    # 渠道词打头（「三档 clear 初音未来」）→ 锁定渠道。
    if ch:
        # 描述本来就是英文提示词 → 原样直通（零 LLM，防改坏也省钱）。
        if desc and _is_english_tags(desc):
            _remember_job(session_key, ch, desc)
            return _enqueue(ch, desc, text)
        # 引用正文和补充话合并：豆包包装抽英文段当本体，否则引用全文 + 补充。
        # 真引用了才断历史（引用是唯一描述来源，防抄旧 tag）；没引用照旧带
        # 历史（「换成卡通风格 三档」的指代要靠它）。
        src = _quote_merge(quoted, desc)
        hist = [] if quoted.strip() else history
        data = _translate(src, hist, skill=ch)
        if data:
            _remember_job(session_key, data["skill"], data["prompt"])
            return _enqueue(data["skill"], data["prompt"], text)
        return ("这条没转译成生图指令。照格式来：@我 渠道 描述\n\n"
                + MENU_TEXT)

    # 私聊单轮问答（2026-10-05）：问号结尾 + 没渠道词 + 非画图动词 →
    # 单次 LLM 直答，无工具无循环。只认私聊（session 前缀 private_）——
    # 群聊保持「生图或菜单」铁律。放在裸英文直通之前：「who are you?」
    # 这类英文问句不该被当成提示词去生图。
    if (at_me and not ch and (session_key or "").startswith("private_")
            and _QA_RE.search(text) and not _INTENT_RE.search(text)):
        reply = _qa_answer(text, history)
        return reply or MENU_TEXT

    # 裸英文提示词（私聊直接粘贴、没打渠道词）→ 用户口径（2026-10-05）：
    # 英文就是最终提示词，直通默认渠道零调用。只在 @ 轮（含私聊）放行——
    # 群关键词轮里别人贴的英文句子不该触发生成。
    if at_me and not quoted and _is_english_tags(text):
        _remember_job(session_key, _DEFAULT_SKILL, text)
        return _enqueue(_DEFAULT_SKILL, text, text)

    # 画图动词（「画一只猫」）或裸 @ + 描述 → 一次转译（LLM 顺带判渠道）。
    # 转译失败 @ 轮和关键词轮都回格式提示——闲聊轮绝不掉回 agent 接话
    # （233 粉丝群 02:06 实录教训）。
    if _INTENT_RE.search(text) or at_me:
        data = _translate(text, history)
        if data:
            _remember_job(session_key, data["skill"], data["prompt"])
            return _enqueue(data["skill"], data["prompt"], text)
        return ("这条没转译成生图指令。照格式来：@我 渠道 描述\n\n"
                + MENU_TEXT)

    # 关键词命中但没渠道词没画图动词（「进黑名单你都喊不出大大怪」）→
    # 照样回菜单，绝不掉回 agent 接话。
    return MENU_TEXT
