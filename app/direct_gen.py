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
  正文当描述，走锁定渠道扩写。
- 引用生图回执 +「再来一张」→ 同提示词换种子重跑（_LAST_JOB 现成有），零转译。
- 引用图 + 意见 → 识图 + 会话上次任务的提示词 + 意见 → 一次修正调用 → 重跑；
  修正调用可输出 skip（意见不是修改请求，比如纯夸奖）→ 按 @/关键词分别回
  菜单或静默。
- **agent 已退场（2026-10-05 用户拍板）**：@ 轮要么走工具要么回菜单。
  关键词命中但纯闲聊（别的机器人的聊天里提到名字）→ **静默不理**，止住
  菜单刷屏。唯一放行是总开关关闭（ENABLED=False）和主动接话轮（voluntary）。
"""
import difflib
import json
import logging
import os
import re

from app import image_jobs, llm, qq_api
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
# 生图意图粗判（画图动词）。「生成」后面 0~4 字内接「图」才算，避免
# 「生成一下总结」误中。
_INTENT_RE = re.compile(r"画|绘|来张|来一张|来幅|生成.{0,4}图|图.{0,2}一[张幅]")
# 「再来一张」：引用回执（或不引用）时同提示词换种子重跑。
_AGAIN_RE = re.compile(r"再来一张|重画|再画|重跑|换种子|再跑一张")
# 引用正文里的噪音：菜单和生图回执不能当提示词用。
_NOISE_QUOTE_RE = re.compile(r"HT-\d{8}|任务已提交|生图完成")
_RECENT_NOISE_RE = re.compile(r"^🎨|任务已提交|生图完成|HT-\d{8}")

# ─── 渠道解析（代码直判，LLM 不再碰渠道） ─────────────────
# 档位是最核心的关键词；画风词是可选项。用户口径（2026-10-05）：档位打了、
# 画风词打错或没打 → 按该档默认画风 clear。
_TIER_MAP = {"三档": "3", "3档": "3", "二档": "2", "2档": "2",
             "快档": "fast", "一档": "fast", "1档": "fast"}
_TIER_RE = re.compile(r"(三档|二档|快档|[123]档|默认)")
_STYLE_RE = re.compile(r"\b(clear|curvy|gloss|soft)\b|清晰|肉感|油亮|柔和|柔软",
                       re.I)
_STYLE_ALIASES = {"clear": "clear", "curvy": "curvy", "gloss": "gloss",
                  "soft": "soft", "清晰": "clear", "肉感": "curvy",
                  "油亮": "gloss", "柔和": "soft", "柔软": "soft"}
_NAI_RE = re.compile(r"^nai\b\s*", re.I)


def _parse_channel(text):
    """代码直判渠道。返回 (skill or None, 剩余描述)。

    - 「三档 gloss 初音未来」→ hd_3_gloss / 初音未来
    - 「三档,glss,初音未来」 → hd_3_gloss / 初音未来（glss 贴回 gloss）
    - 「三档 猫」            → hd_3_clear / 猫（画风没打，档位默认）
    - 「默认初音未来」       → anima_clear / 初音未来（无分隔符也认）
    - 「gloss 一个女孩」     → anima_gloss / 一个女孩（只打画风）
    - 「nai 1girl, ...」     → nai / 1girl, ...（原样留给扩写）
    - 没有任何渠道词         → (None, 原文)
    """
    text = text.strip()
    m = _NAI_RE.match(text)
    if m:
        return "nai", text[m.end():].strip(" ，,、:：") or text
    tier = style = None
    tm = _TIER_RE.search(text)
    if tm:
        tier = _TIER_MAP.get(tm.group(1), "base")   # 「默认」→ base
    sm = _STYLE_RE.search(text)
    if sm:
        style = _STYLE_ALIASES.get(sm.group(0).lower(),
                                   _STYLE_ALIASES.get(sm.group(0)))
    if tier is None and style is None:
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
    desc = re.sub(r"^[\s,，、:：]+|[\s,，、:：]+$", "", desc)
    return skill, desc


MENU_TEXT = (
    "🎨 生图直达（不闲聊，发指令直接出图）\n"
    "@我 + 一句话，示例：\n"
    "【最简】默认 纳西妲\n"
    "【默认+画风】默认 gloss 一个女孩\n"
    "【快档】快档 一只柴犬在草地上\n"
    "【二档】二档 赛博朋克城市夜景\n"
    "【三档】三档 水晶城堡\n"
    "【画风词】clear清晰 / curvy肉感 / gloss油亮 / soft柔和"
    "（跟在档位或「默认」后面；打错或没打就按该档默认）\n"
    "【NAI 云端】nai 1girl, masterpiece, best quality\n"
    "【引用出图】引用一条带描述的消息 + 只发「三档」这样的渠道词\n"
    "【再来一张】引用出图回执 +「再来一张」（同提示词换种子）\n"
    "【改图】引用要改的那张图 + 说改什么（「多手多脚了」「衣服换红色」）\n"
    "【反推】引用图 +「反推提示词」\n"
    "描述用中文就行，我转成画法。"
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
    "你是生图提示词修正器。用户引用了一张 AI 生成的图并提出修改意见。"
    "只输出 JSON 本体，格式：{{\"skill\": \"渠道id\", \"prompt\": \"修正后的"
    "完整英文提示词\"}}\n"
    "- skill 沿用「原渠道」，除非用户点名要换\n"
    "- prompt = 在原提示词基础上按用户意见改出来的**完整**提示词；"
    "没有原提示词就从画面描述反推骨架再改\n"
    "- danbooru 标签式英文，逗号分隔短语；禁止权重语法 (tag:1.2)、{{tag}}、::\n"
    "- 具体角色没把握就写外貌特征+作品名，不要编造不存在的角色名\n"
    "- **用户的话不是修改意见**（夸奖、闲聊、问别的事）→ 输出 "
    "{{\"skip\": true}}\n"
    "画面描述：\n{seen}\n"
    "原提示词（渠道 {last_skill}）：\n{last_prompt}\n"
    "最近对话：\n{recent}\n"
    "用户意见：{text}"
)

# 修正管道用的识图指令：只描述画面本身，别客套。
_SEE_PROMPT = "用中文简洁描述这张图的内容：人物、姿势、服装、场景、显著问题。"

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


def _humanize_error(result):
    """工具返回的「错误：…」文案是写给模型看的，尾巴带着指示语句。
    直达管道直接发给真人，只留第一句。"""
    first = result.split("。")[0].strip()
    return first if first.endswith("。") else first + "。"


def _enqueue(skill, prompt, text):
    """转译结果落队。返回要发回会话的文本；成功且回执已直发返回 ""。"""
    from app.tools.normal import generate_image as gi
    try:
        result = gi._generate_image(prompt, skill=skill, _skip_confirm=True)
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


def _revise(text, data_urls, history, channel=None):
    """改图管道：引用图 + 意见 → 识图 + 原提示词 → 一次修正调用 → 重跑。

    返回 None = 意见不是修改请求（skip），调用方按 @/关键词分别回菜单/静默。
    """
    from app.vision import describe
    session_key = qq_api.current_session_key()
    last = _last_job(session_key)
    quoted = (qq_api.current_quoted_text() or "").strip()
    # 「再来一张」：引用回执（或干说）→ 同提示词换种子重跑，零转译。
    if _AGAIN_RE.search(text) and (last or _NOISE_QUOTE_RE.search(quoted)):
        if last:
            skill = channel or last["skill"]
            _remember_job(session_key, skill, last["prompt"])
            return _enqueue(skill, last["prompt"], text)
        return "没找到最近一次生图的记录，重新描述想要什么吧：@我 渠道 描述。"
    try:
        seen = (describe(data_urls[0], prompt=_SEE_PROMPT) or "").strip()
    except Exception:
        log.exception("[direct] 改图识图失败")
        seen = ""
    if not seen and not last:
        return ("没认出引用的图，也没找到最近一次生图的记录。"
                "重新描述想要什么吧：@我 渠道 描述。")
    ask = _REVISE_TEMPLATE.format(
        seen=seen or "（识图失败）",
        last_skill=(last or {}).get("skill", "（无）"),
        last_prompt=(last or {}).get("prompt", "（无）"),
        recent=_recent_lines(history) or "（无）",
        text=text)
    data = _ask(ask)
    if not data:
        return "改图请求没解析出来，换个说法再试（如「手改成插兜」）。"
    if data.get("skip"):
        return None
    prompt = (data.get("prompt") or "").strip()
    if not prompt:
        return "改图请求没解析出来，换个说法再试（如「手改成插兜」）。"
    skill = channel or (data.get("skill") or "").strip()
    if skill not in _allowed_skills():
        skill = last["skill"] if last and last["skill"] in _allowed_skills() \
            else _DEFAULT_SKILL
    _remember_job(session_key, skill, prompt)
    return _enqueue(skill, prompt, text)


def decide(own_text, history, voluntary, data_urls=None, at_me=True):
    """直达管道入口。返回值：
    - None      ：不接管（总开关关闭 / 主动接话轮 / 关键词轮的闲聊——静默）
    - ""        ：已接管且回执已直发（出图回执），本轮别再说话
    - 其他文本  ：接管并把这段发回会话（菜单/错误提示）

    @ 轮（at_me=True）一切兜底都是菜单；关键词/全量轮兜底一律静默——
    2026-10-05 用户拍板，止住「群里提到名字就刷菜单」。
    """
    text = _strip_own_names(_strip_attribution(own_text))
    # 菜单与裸 @：零 LLM，直接回常量（管道关着也照回——它本来就免费）。
    if not text or _MENU_RE.match(text):
        return MENU_TEXT
    if not ENABLED:
        return None
    # 主动接话轮（没人 @ 它）不进管道，行为维持原样。
    if voluntary:
        return None

    session_key = qq_api.current_session_key()
    quoted = (qq_api.current_quoted_text() or "").strip()
    ch, desc = _parse_channel(text)

    # 引用图轮：改图 / 反推已被 recall_gate 接走，到这说明是提意见。
    if data_urls:
        reply = _revise(text, data_urls, history, channel=ch)
        return reply if reply is not None else (MENU_TEXT if at_me else None)

    # 「再来一张」（不引用、不带渠道词）：同提示词换种子重跑，零转译。
    last = _last_job(session_key)
    if not ch and _AGAIN_RE.search(text) and last:
        _remember_job(session_key, last["skill"], last["prompt"])
        return _enqueue(last["skill"], last["prompt"], text)

    # 只打了渠道词（「大大怪 三档」+ 引用 / 干发「三档」）。
    if ch and not desc:
        if quoted and not _NOISE_QUOTE_RE.search(quoted) \
                and not quoted.startswith("🎨"):
            data = _translate(quoted, history, skill=ch)
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
        return ("只发渠道词的话，引用一条带描述的消息（提示词或你想要的内容）"
                "再发一遍渠道词，或直接 @我 渠道 描述。")

    # 渠道词打头（「三档 clear 初音未来」）→ 锁定渠道，LLM 只扩写描述。
    if ch:
        data = _translate(desc, history, skill=ch)
        if data:
            _remember_job(session_key, data["skill"], data["prompt"])
            return _enqueue(data["skill"], data["prompt"], text)
        return ("这条没转译成生图指令。照格式来：@我 渠道 描述\n\n"
                + MENU_TEXT)

    # 画图动词（「画一只猫」）或裸 @ + 描述 → 一次转译（LLM 顺带判渠道）。
    if _INTENT_RE.search(text) or at_me:
        data = _translate(text, history)
        if data:
            _remember_job(session_key, data["skill"], data["prompt"])
            return _enqueue(data["skill"], data["prompt"], text)
        if at_me:
            return ("这条没转译成生图指令。照格式来：@我 渠道 描述\n\n"
                    + MENU_TEXT)
        return None                     # 关键词轮转译失败 → 静默

    # 关键词命中但纯闲聊（别的机器人的聊天提到名字）→ 静默不理。
    return None
