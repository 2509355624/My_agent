#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""直达生图管道（2026-10-04）。

动机：agent 路径一轮动辄几万 token（系统头 + 历史 + 工具协议 + 多轮循环），
而「@我 画一只猫」这种请求本质只需要一次轻量转译。这条管道用**一次 LLM 调用**
把用户原话转成 {渠道, 提示词}，代码直接入队——整轮不过 agent。

结构：
- /菜单（或裸 @、或任何不是工具指令的 @ 轮）→ 写死的常量文本，零 LLM。
- 生图意图（正则粗判）→ 一次 call_llm 转译成 JSON → 校验 → 直接调
  generate_image 的入队代码（复用渠道/档位/NAI 分流/额度/查重全套既有逻辑，
  唯一区别是跳过确认卡——用户打了指令本身就是确认）。
- 带图轮（用户引用已生成的图提意见）→ 识图 + 会话上次任务的提示词 +
  用户意见，一次 LLM 调用输出修正后的提示词 → 重新入队。
- **agent 已退场（2026-10-05 用户拍板）**：@ 轮要么走工具要么回菜单，
  绝不为聊天进 agent 循环。唯一例外是总开关关闭（ENABLED=False）和
  主动接话轮（voluntary，行为维持原样）。
"""
import json
import logging
import os
import re

from app import image_jobs, llm, qq_api
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
# 生图意图粗判。宁可漏（漏了走 agent，多花钱但不出错）不可滥（滥了闲聊也被
# 拉去转译）。「生成」后面 0~4 字内接「图」才算，避免「生成一下总结」误中。
_INTENT_RE = re.compile(r"画|绘|来张|来一张|来幅|生成.{0,4}图|图.{0,2}一[张幅]")

MENU_TEXT = (
    "🎨 直达生图（不经过 AI 对话，秒排队）\n"
    "格式：@我 + 渠道 + 描述\n"
    "【默认】画：一只戴帽子的橘猫\n"
    "【快档】快档 一只柴犬在草地上\n"
    "【二档】二档 赛博朋克城市夜景\n"
    "【三档】三档 水晶城堡\n"
    "【画风】清晰 clear / 肉感 curvy / 油亮 gloss / 柔和 soft"
    "（跟在档位后面，如「二档 gloss 一个女骑士」）\n"
    "【云端】nai <英文提示词>\n"
    "【改图】引用要改的那张图 + 说要改什么（如「多手多脚了」「衣服换成红色」）\n"
    "【反推】引用图 + 说「反推提示词」\n"
    "描述用中文就行，我会转成对应画法。本机器人只管生图，不闲聊。"
)

_TRANSLATE_TEMPLATE = (
    "你是生图指令解析器。把用户的请求转成 JSON，只输出 JSON 本体，"
    "格式：{{\"skill\": \"渠道id\", \"prompt\": \"英文提示词\"}}\n"
    "渠道规则：\n"
    "- hd 渠道命名 = hd_档_画风：档∈{{fast,2,3}}，画风∈{{clear清晰,curvy肉感,"
    "gloss油亮,soft柔和}}（如「快档」=hd_fast_*、「二档」=hd_2_*）\n"
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


_REVISE_TEMPLATE = (
    "你是生图提示词修正器。用户引用了一张 AI 生成的图并提出修改意见。"
    "只输出 JSON 本体，格式：{{\"skill\": \"渠道id\", \"prompt\": \"修正后的"
    "完整英文提示词\"}}\n"
    "- skill 沿用「原渠道」，除非用户点名要换\n"
    "- prompt = 在原提示词基础上按用户意见改出来的**完整**提示词；"
    "没有原提示词就从画面描述反推骨架再改\n"
    "- danbooru 标签式英文，逗号分隔短语；禁止权重语法 (tag:1.2)、{{tag}}、::\n"
    "- 具体角色没把握就写外貌特征+作品名，不要编造不存在的角色名\n"
    "画面描述：\n{seen}\n"
    "原提示词（渠道 {last_skill}）：\n{last_prompt}\n"
    "最近对话：\n{recent}\n"
    "用户意见：{text}"
)

# 修正管道用的识图指令：只描述画面本身，别客套。
_SEE_PROMPT = "用中文简洁描述这张图的内容：人物、姿势、服装、场景、显著问题。"

# 会话最近一次直达入队的任务（修正管道的「原提示词」来源）。
# 内存态就够：修正场景发生在刚出图之后，进程重启丢了也就是少个上下文。
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


def _recent_lines(history, limit=10):
    """会话历史的最近几条，压成「用户：…/AI：…」短行给转译调用看指代。"""
    lines = []
    for m in history or []:
        if m.get("role") == "system":
            continue
        c = m.get("content")
        if not isinstance(c, str) or not c.strip():
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


def _translate(text, history):
    """一次 LLM 调用把原话转成 {skill, prompt}；拿不到有效结果返回 None。"""
    prompt = _TRANSLATE_TEMPLATE.format(
        recent=_recent_lines(history) or "（无）", text=text)
    try:
        reply = llm.call_llm(
            [{"role": "user", "content": prompt}], timeout=30)
    except Exception as e:
        log.warning("[direct] 转译调用失败：%s", e)
        return None
    data = _extract_json(reply)
    if not data:
        log.warning("[direct] 转译输出不是 JSON（%r）", (reply or "")[:120])
        return None
    user_prompt = (data.get("prompt") or "").strip()
    skill = (data.get("skill") or "").strip()
    if not user_prompt or not skill:
        return None
    if skill not in _allowed_skills():
        log.warning("[direct] 转译给了未知渠道 %r，降回默认 %s",
                    skill, _DEFAULT_SKILL)
        skill = _DEFAULT_SKILL
    return {"skill": skill, "prompt": user_prompt}


def _revise(text, data_urls, history):
    """改图管道：引用图 + 意见 → 识图 + 原提示词 → 一次修正调用 → 重新入队。"""
    from app.vision import describe
    session_key = qq_api.current_session_key()
    last = _last_job(session_key)
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
    data = _translate(ask, history)
    if not data:
        return "改图请求没解析出来，换个说法再试（如「手改成插兜」）。"
    _remember_job(session_key, data["skill"], data["prompt"])
    return _enqueue(data["skill"], data["prompt"], text)


def decide(own_text, history, voluntary, data_urls=None):
    """直达管道入口。返回值：
    - None      ：不接管（仅限总开关关闭 / 主动接话轮），agent 照旧
    - ""        ：已接管且回执已直发（出图回执），本轮别再说话
    - 其他文本  ：接管并把这段发回会话（菜单/错误提示）

    @ 轮一律接管——工具或菜单，绝不放聊天进 agent（2026-10-05 用户拍板）。
    """
    text = _strip_attribution(own_text).strip()
    # 菜单与裸 @：零 LLM，直接回常量（管道关着也照回——它本来就免费）。
    if not text or _MENU_RE.match(text):
        return MENU_TEXT
    if not ENABLED:
        return None
    # 主动接话轮（没人 @ 它）不进管道，行为维持原样。
    if voluntary:
        return None
    # 带图轮：用户引用已生成的图提意见 → 改图管道（「反推」类已被前面的
    # recall_gate 接管，到不了这里）。
    if data_urls:
        return _revise(text, data_urls, history)
    # 生图意图（正则粗判）→ 一次转译 → 直接入队。
    if _INTENT_RE.search(text):
        data = _translate(text, history)
        if not data:
            # 转译翻车（链路挂了 / 模型说这跟画图无关）：不进 agent，
            # 回菜单让人照格式再打一遍。
            return ("这条没转译成生图指令。照格式来：@我 渠道 描述\n\n"
                    + MENU_TEXT)
        _remember_job(qq_api.current_session_key(), data["skill"],
                      data["prompt"])
        return _enqueue(data["skill"], data["prompt"], text)
    # 其余一切 @ 轮（闲聊、问问题、无意义文本）→ 菜单，绝不进 agent。
    return MENU_TEXT
