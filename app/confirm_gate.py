# -*- coding: utf-8 -*-
"""生图二次确认闸（2026-10-04，用户拍板「所有生图都确认 + 确认后代码直接入队」）。

出处：清酒瓶子 10-02「我建议给小小怪也加上词条提交确认~有时候词条没写对
刚发出去就秒提交了，撤回都来不及」；10-04 群里已经在用「大大怪确认」这套
口语（「确认我就跑」→「确认」，甚至有错别字「确实」）。prompt.md 的「确认
本能」节对 9B 压不住——流程性规则时灵时不灵是老问题——所以走代码闸。

两头：
- `intercept()`：挂在 generate_image 两个提交点（ComfyUI 主路 / NAI 路）
  入队前。QQ 轮必拦：把**最终参数**快照进 pending，直发确认卡（复用回执
  直发通道），返回 `RECEIPT_SENT_MARK` 让 agent 掐断本轮——模型正文不发，
  确认卡不经模型转述，不会变形。网页端放行（守卫家族惯例）。
- `consume_if_confirmed()`：挂在 qq_bot 轮入口。下一条
  消息命中确认词 → pending 参数**原样**入队 + 发回执，接管整轮（不过模型，
  快、省一次调用、参数绝不会变卦）；不是确认话 → 作废 pending 放行，模型
  再调工具会出新的确认卡。主动接话轮不消费（群聊里路人一句「好」不该引爆）。

pending 只在内存里（dict），进程重启自然清空；按 (target, target_id) 分键，
多群并发互不干扰。
"""
import json
import logging
import re
import threading

log = logging.getLogger(__name__)

_PENDING = {}
_LOCK = threading.Lock()

# ── 确认词判据（语料：_at_corpus.txt，10-01~10-04 群聊点名 + 私聊 sessions）──
# 精确匹配：整句（去首尾空白与标点）就是一个确认词。语料实录：确认 / 好 / 行 /
# 可以了 / 对对对 / 嗯 / OK / 跑吧 / 继续吧 / 就这样 / 没问题 / 你换吧 / 1，
# 以及错别字「确实」（回应「确认我就开」）。单个「跑」「画」「要」「冲」也算。
_CONFIRM_EXACT_RE = re.compile(
    r"^(?:好+|好[呀啊吧的嘛]|行+|行[吧的]|可以了?|[呀嘛]?可以|对+|对的?|嗯+|哦+|噢+|"
    r"ok|okay|确认|确认了|确实|是的?|要|要的|要画|跑|跑吧|跑把|跑呗|直接跑|直接画|"
    r"开跑|开画|画|画吧|画把|继续|继续吧|就这|就这样|就这吧|就这样吧|没问题|莫问题|"
    r"上|上吧|搞|搞吧|搞起|换吧|你换吧|那就换吧|冲|冲吧|1|👌|👍|🆗)$",
    re.I)

# 弱匹配：短消息（≤16 字）含核心确认词、且没有修改信号才算。
_CONFIRM_CORE_RE = re.compile(r"确认|可以|没问题|直接跑|开跑|就这|就这样|跑吧|画吧|"
                              r"继续吧|同意|ok|嗯", re.I)
# 修改信号：出现这些 = 用户在改需求，不当确认（重新走模型出新的确认卡）。
# 渠道名 / 档位 / 种子都在里面——「用 nai 跑」是改渠道不是确认。
_MODIFY_RE = re.compile(
    r"换|改|加|去掉|删|不要|别|再来|重新|重画|重跑|多|少|再|种子|seed|档|渠道|画风|"
    r"风格|提示词|词条|lora|qwen|nai|nffa|anima|anime|sd|krea|竖|横|放大|尺寸|像素|"
    r"高清|画质|构图|姿势|衣服|服装", re.I)
# 否定 / 疑问信号：含这些绝不确认（「不可以」「这样可以吗」）。
_REFUSE_RE = re.compile(r"不[行可以好能对要用途]|别|先不|还没|等等|不急|算了|吗", re.I)


def _normalize(text):
    """去首尾空白与常见标点——「确认。」「跑吧~」都算。"""
    return re.sub(r"^[\s，。！!？?~～、,.…·]+$|^[\s，。！!？?~～、,.…·]+|[\s，。！!？?~～、,.…·]+$",
                  "", (text or "").strip())


# 群聊/私聊消息都带署名前缀（qq_bot 入口 `sender + "：" + text`，合并窗口里
# 也是 "%s：%s"），实测 2026-10-04 233粉丝群：确认词正则锚定行首，拿原始
# 文本判「胡桃桃：好」永远不命中 → pending 反复作废 → 模型重出卡死循环、
# 一张图都没真正入队。剥掉署名与 @ 前缀再逐行判。
#
# ⚠️ **分隔符只认全角「：」**——runtime 加的署名前缀一律是全角（qq_bot.py:454/
# 639/902/1301 四处都是 `"：" +`），半角 `:` 从来不是署名分隔符。10-05 私聊
# 实录：把半角也当分隔符，NAI 的权重语法 `0.5::artist:x::` 和画面比例 `9:16`
# 里的冒号就被当成署名切了一刀，`nai 凯尔希0.5` 整段连渠道词一起被吃掉 →
# 点名 NAI 的画师串落到 anima_clear。**宁可留着「某某：」的噪音，也不吃掉指令本体。**
_ATTRIBUTION_RE = re.compile(r"^\s*[^：\n]{1,20}[：]\s*")
_AT_RE = re.compile(r"^\s*@\S+\s*")


def _candidate_lines(text):
    """剥掉每行的署名 / @ 前缀，返回去空后的候选行（保序）。"""
    out = []
    for line in (text or "").splitlines():
        prev = None
        while prev != line:
            prev = line
            line = _ATTRIBUTION_RE.sub("", line, count=1)
            line = _AT_RE.sub("", line, count=1)
        line = line.strip()
        if line:
            out.append(line)
    return out


def is_confirm(text):
    """这条消息是不是在确认开跑。宁可漏判（作废重出卡）不可误判（白花钱）。"""
    t = _normalize(text)
    if not t or len(t) > 24:
        return False
    if _REFUSE_RE.search(t):
        return False
    if _CONFIRM_EXACT_RE.match(t):
        return True
    return len(t) <= 16 and bool(_CONFIRM_CORE_RE.search(t)) and not _MODIFY_RE.search(t)


def _card_text(pend):
    lines = ["将画：" + pend["skill"]]
    if pend.get("seed") is not None:
        lines.append("seed：" + str(pend["seed"]))
    if pend.get("note"):
        lines.append(pend["note"])
    lines.append("提示词：" + pend["prompt"])
    lines.append("回复「好」开跑；要改就直接说改哪里")
    return "\n".join(lines)


def intercept(kind, *, skill, prompt, seed=None, intent=None, workflow=None,
              nai_i2i=None, note="", skip_confirm=False):
    """生图提交前的最后一道闸。返回 None=放行（网页端），返回 str=已拦下等确认
    （以 RECEIPT_SENT_MARK 开头，工具原样返回，agent 掐断本轮）。

    skip_confirm：直达生图管道（direct_gen）专用——用户打的指令本身就是确认，
    不再拦（agent 路径保持 False）。
    """
    from app import image_jobs, qq_api
    if skip_confirm:
        return None
    target, target_id = qq_api.current_context()
    if target is None:
        return None
    # 守卫家族同一判据：current_turn_text() 为 None = 不在 QQ 轮里（网页端、
    # 单测直接调工具）——没有原话这个证据源，一律放行。QQ 轮没打字是 ""，
    # 照拦（发图不带字也该确认）。
    if qq_api.current_turn_text() is None:
        return None
    with _LOCK:
        _PENDING[(target, target_id)] = {
            "kind": kind, "skill": skill, "prompt": prompt, "seed": seed,
            "intent": intent, "workflow": workflow, "nai_i2i": nai_i2i,
            "note": note,
            # 横屏（2026-10-07）：**在这里快照**。确认是在下一条消息（「好」）
            # 里才发生的，那时 `current_turn_text()` 已经是「好」，横屏这个词
            # 早没了；而 enqueue 那一刻的线程本地变量也不是本轮了。
            "landscape": image_jobs.turn_is_landscape(),
        }
    card = _card_text(_PENDING[(target, target_id)])
    from app.tools.normal.generate_image import _send_receipt
    try:
        _send_receipt(target, target_id, card)
    except Exception:
        log.exception("确认卡直发失败 %s %s（参数已存，对方下一条消息仍可确认）",
                      target, target_id)
    log.info("确认闸拦下生图：%s %s %s，等对方确认", target, target_id, skill)
    return (image_jobs.RECEIPT_SENT_MARK
            + "（这张在等对方确认，确认卡已直接发给对方；别再重复调用本工具）")


def consume_if_confirmed(target, target_id, text):
    """轮入口：有 pending 且这条话是确认 → 原样入队发回执，返回要直发的文本；
    否则返回 None（pending 没了就作废，没有就原样放行）。主动接话轮调用方
    应该跳过本函数（群聊路人的「好」不引爆）。"""
    with _LOCK:
        pend = _PENDING.pop((target, target_id), None)
    if pend is None:
        return None
    # 合并窗口可能混进好几行（署名各不相同）：任何一行是确认就算确认。
    if not any(is_confirm(line) for line in _candidate_lines(text)):
        log.info("确认闸：pending 作废（对方说的是 %r，不是确认）",
                 (text or "")[:40])
        return None
    from app import image_jobs
    from app.agents import image_gen_allowed
    from app.tools.normal import generate_image as gi
    ok, why = image_gen_allowed(gi.QQ_AGENT_ID, target, target_id)
    if not ok:
        return "错误：" + why + "，这张不放行了。"
    if pend["kind"] == "nai":
        from app.agents import nai_allowed
        ok, why = nai_allowed(gi.QQ_AGENT_ID, target, target_id)
        if not ok:
            return "错误：" + why + "，这张不放行了。"
        job, reason = image_jobs.enqueue(target, target_id, pend["prompt"],
                                         skill=pend["skill"],
                                         nai_i2i=pend["nai_i2i"],
                                         intent=pend["intent"],
                                         landscape=pend.get("landscape"))
        if reason is not None:
            return reason
        quota_tail = gi._charge_quota(job, target, target_id)
        return gi._qq_receipt(job, target, target_id,
                              source_note=(pend["nai_i2i"]["note"]
                                           if pend["nai_i2i"] else pend["note"]),
                              quota_tail=quota_tail)
    # ComfyUI 路：确认时重探活——卡发出到确认之间机器可能关了 ComfyUI。
    if not image_jobs.comfy_alive():
        return ("错误：ComfyUI 现在没在线，刚那张画不了。等它开了再让 AI 重新出。")
    job, reason = image_jobs.enqueue(target, target_id, pend["workflow"],
                                     pend["skill"], prompt=pend["prompt"],
                                     intent=pend["intent"], seed=pend["seed"],
                                     landscape=pend.get("landscape"))
    if reason is not None:
        return reason
    quota_tail = gi._charge_quota(job, target, target_id)
    return gi._qq_receipt(job, target, target_id,
                          source_note=pend["note"], quota_tail=quota_tail)
