"""意图判断的两条 LLM 主链：自由模式与攻略模式。"""

import logging

from app.config import LLM_JUDGE_TIMEOUT
from app.features.persona import affection
from app.features.persona.game_text import _plan_fact
from app.features.persona.game_time import _day_of
from app.features.persona.json_utils import _parse_json_loose
from app.features.persona.judge_fields import (
    _director_fields, _item_keys_text, _judge_ledger, _norm_finish, _norm_rag,
)
from app.features.persona.prompt_facts import _FACTS_GUIDE
from app.features.persona.prompts import (
    _INTENT_PROMPT, _SCORE_PROMPT, _SCORE_PROMPT_V2,
)

log = logging.getLogger("persona")


INTENTS = {
    "intimate": "亲密/求欢：想亲近、调情、要求身体接触或性",
    "joke": "玩笑/逗你：拿角色开玩笑、威胁着玩、故意说离谱的话",
    "literal": "字面请求：真的要求做某件事、问某个问题",
    "insult": "冒犯：贬低、辱骂、羞辱角色本身",
    "neutral": "普通对话：闲聊、叙述、没有明确指向",
}


def judge_intent(card, said, state="", history=None, note="",
                 llm_call=None, name_fn=None, recent_fn=None):
    """判断这句话在干什么，并顺手记下这一轮的事实。返回 `(intent, note, extra)`。

    单独一次调用、不带人设——人设会影响判断（傲娇角色看什么都像调戏）。
    超时短、输出短，失败就退化成原流程，不阻塞这一轮。

    `extra` 是事实字段（facts / secret / addressed / items），不再含任何
    「这一拍怎么写」的判断。失败时是空 dict，调用方退回原文。
    """
    who = name_fn(card, "对方")
    prompt = _INTENT_PROMPT % {
        "who": who + ("（" + state + "）" if state else ""),
        "said": str(said or "")[:2000],
        "recent": recent_fn(history),
        "ledger": _judge_ledger(note),
        "facts_guide": _FACTS_GUIDE,
        "item_keys": _item_keys_text(),
    }
    try:
        # 2026-11-05：分段 + 导演条后输出变长，20s 偶发不够；放到 30s。
        # 判挂了只是退回原文，不会阻塞这一轮，但能判出来质量更好。
        raw = llm_call([{"role": "user", "content": prompt}], timeout=LLM_JUDGE_TIMEOUT)
    except Exception:                            # noqa: BLE001
        log.warning("意图判断调用失败，走原流程")
        return None, "", {}
    d = _parse_json_loose(raw)
    if not isinstance(d, dict):
        log.info("意图判断没交 JSON，走原流程：%r", str(raw or "")[:80])
        return None, "", {}
    intent = str(d.get("intent") or "").strip().lower()
    if intent not in INTENTS:
        log.info("意图判断给了不认识的类别 %r，走原流程", intent)
        # 类别不认识不影响事实字段：只要它交了 facts / secret / rag，照样能用。
        extra = _director_fields(d)
        if "rag" in d:
            extra["rag"] = _norm_rag(d.get("rag"))
        if not (extra["facts"] or extra["secret"] or "rag" in extra):
            extra = {}
        return None, "", extra
    note = str(d.get("note") or "").strip()[:30]
    extra = _director_fields(d)
    # RAG 路由（2026-11-18）：只有模型真的交了 rag 字段才透传。
    # 老提示词/老 mock 没这个字段时保持旧行为，由 turn.py 兜底。
    if "rag" in d:
        extra["rag"] = _norm_rag(d.get("rag"))
    return intent, note, extra


def judge_intent_scored(card, said, state="", line_key="", gameday=None,
                         history=None, note="", llm_call=None, profile_fn=None,
                         recent_fn=None):
    """攻略模式的第一段：意图 + 关系打分 + 分段/导演条。返回 `(intent, note, score)`。

    判断失败时 `(None, "", None)`——退化成没判过（跟 `_judge_intent` 同一
    口径），**打不出分就按 +0 结算**，不让整轮回不了话。
    """
    gd = gameday if isinstance(gameday, dict) else {}
    plan = str(gd.get("plan") or "").strip()
    ctx = affection.scoring_ctx(line_key) or {"value": 0, "tier": "正常关系",
                                              "level": "隔得很远", "arousal": 0,
                                              "corrupt": 0, "submit": 0,
                                              "suspicion": 0,
                                              "recent": "（暂无记录）"}
    prompt = _SCORE_PROMPT % {
        "value": ctx["value"],
        "tier": ctx["tier"],
        "level": ctx.get("level") or "?",
        "arousal": int(ctx.get("arousal") or 0),
        "arousalName": ctx.get("arousalName") or "—",
        "excite": int(ctx.get("excite") or 0),
        "exciteName": ctx.get("exciteName") or "还没进场景",
        "dynamicBrief": ctx.get("dynamicBrief") or "（这一轮没有额外的动态规则）",
        "corrupt": int(ctx.get("corrupt") or 0),
        "submit": int(ctx.get("submit") or 0),
        # 戒备（2026-11-05）：模型要知道她现在起没起疑心，才知道同一句话
        # 该放行还是该往回打。缺字段的老 ctx 按 0 走。
        "suspicion": int(ctx.get("suspicion") or 0),
        # 开发度（2026-11-05）：编剧看到五处各到哪一档，才判得出要不要给勾子。
        # 缺字段的老 ctx 按「都还没开发过」走。
        "dev": ctx.get("devText") or "（五处都还没被开发过）",
        "recent": ctx["recent"],
        # 游戏内时间（2026-11-04）：打分 AI 顺带管时间推进，得知道今天第几天、
        # 有没有挂着没兑现的约定。
        "gday": _day_of(gd),
        # 当前时段（2026-11-07）：2026-11-07 的复测发现 phase 字段永远是空的——
        # 打分器只看得到「第几天」，看不到「现在是早晨」，它就没法判断「变了没有」。
        # 不给这一格，`gd["phase"]` 只能靠玩家手动点时间按钮才会动。
        "gphase": str(gd.get("phase") or "还没定"),
        "gplan": _plan_fact(gd, plan),
        "profile": profile_fn(card, state, key=line_key),
        "said": str(said or "")[:2000],
        "history": recent_fn(history),
        "ledger": _judge_ledger(note),
        "_facts_guide": _FACTS_GUIDE,
    }
    try:
        # **不传 max_tokens**（2026-11-05 用户拍板：不要任何输出上限）。
        # 实测过三档：不传（服务端默认）706 字、1600 给 582 字、4000 给 615 字
        # ——根本不是长度限制的问题，加了反而更短。真正的原因在提示词里
        # 那句「原文照抄」：模型逐字复述露骨原文时会被内容审核当场掐断，
        # 末尾补一句 `Sorry, I can't respond`。改措辞就好了（见 `segments`）。
        raw = llm_call([{"role": "user", "content": prompt}], timeout=LLM_JUDGE_TIMEOUT)
    except Exception:                            # noqa: BLE001
        log.warning("攻略模式打分调用失败，按 0 分走")
        return None, "", None
    d = _parse_json_loose(raw)
    if not isinstance(d, dict):
        # 打日志时给足上下文：前面是回复开头，后面是结尾——截断的现场在末尾。
        log.warning("攻略模式没交 JSON，按 0 分走（共 %d 字）：头=%r 尾=%r",
                    len(str(raw or "")), str(raw or "")[:120],
                    str(raw or "")[-120:])
        return None, "", None
    intent = str(d.get("intent") or "").strip().lower()
    if intent not in INTENTS:
        intent = None
    note = str(d.get("note") or "").strip()[:30]
    score = {"delta": d.get("delta"),
             "turn_type": d.get("turn_type") or "chat",
             "reason": d.get("reason") or "",
             # 怀孕（2026-11-05 / 2026-11-09 补回）：模型判的「射在里面」以前
             # 在这里被丢掉，只有「高潮满 → 弹窗」能记账；玩家正文里写明的
             # 内射全漏。`run_turn` 的 `_preg_record` 只认 `finish == "inside"`。
             "finish": _norm_finish(d.get("finish")),
             # 2026-11-04 距离层级三件事：这一刻的气氛、有没有越界、她答不答应、
             # 有没有做成一件能让关系往前走的事。**别在这里漏掉任何一项**——
             # 这个 dict 是 settle 唯一的输入，漏了就等于模型白判。
             "arousal": d.get("arousal"),
             "cross": d.get("cross"),
             "allow": d.get("allow"),
             "beat": d.get("beat"),
             # 2026-11-04 晚 兴奋度两件事：这轮在不在场景里、她的兴奋涨落多少。
             # 同样**不许漏**——settle 只认这个 dict。
             "in_scene": d.get("in_scene"),
             # 2026-11-05 用户改回：涨多少由这一轮的场景 AI 判（`excite` 就是
             # 增量本身，-40~+40），`act` 只判「有没有身体接触」，
             # orgasm_hints.base 退成「模型没给」时的兜底。
             "act": d.get("act"),
             "excite": d.get("excite"),
             # 堕落 / 屈服（2026-11-04 深夜）：这两根轴不进好感结算，
             # 只改她这一轮怎么演。`soothe` 是唯一能让堕落往下走的口子——
             # 不带上它，模型给的负值会被 `settle` 吞掉（见那边注释）。
             "corrupt": d.get("corrupt"),
             "submit": d.get("submit"),
             "soothe": d.get("soothe"),
             # 戒备（2026-11-05）：settle 只认这个 dict，漏了警戒轴就死在 0。
             "suspicion": d.get("suspicion"),
             # 开发度（2026-11-05）：`dev` 是五处增量、`spot` 是这一轮主碰的
             # 部位、`hook` 是身体先失控那一句。三个都**不许漏**——settle 只认
             # 这个 dict，漏了 developer 轴就永远停在 0，面板一行都不会动。
             "dev": d.get("dev"),
             "spot": d.get("spot"),
             "hook": d.get("hook"),
             # 游戏内时间（2026-11-04）：settle 不认识这几个键，会原样忽略；
             # run_turn 从同一个 dict 里取，落进会话的 gameday。
             "time": d.get("time"),
             "phase": d.get("phase"),
             "plan": d.get("plan"),
             # RAG 路由（2026-11-18）：编剧顺手判这句话要不要翻旧账。
             # 不进 settle，只给 run_turn 决定是否调用长期记忆。
             "rag": _norm_rag(d.get("rag"))}
    # 导演条（2026-11-05）：分段 + 潜台词，第二段演员要拿它。
    # 放在 score 里一起返回，不另开接口——`settle` 不认识这些键，会原样忽略。
    score.update(_director_fields(d))
    # 诊断行（2026-11-05）：以前只看得到失败，成功的打分一次都不留痕，
    # 「数值为什么不涨」只能靠猜。这一行把模型实际给的关键字段摊出来，
    # 排查时 grep `[score]` 就行。
    log.info("[score] act=%r spot=%r excite=%r dev=%r Δ=%r",
             d.get("act"), d.get("spot"), d.get("excite"), d.get("dev"),
             d.get("delta"))
    return intent, note, score


def score_profile(card, state="", key=""):
    from app.features.persona import service as svc
    """打分用的「她是谁」：名字 + 当前状态 + 性格 + 设定。

    2026-11-03：以前只传名字，等于所有卡共用一把尺子，谁都不难攻略。现在把
    性格和设定给打分模型，让它按**这个人的感受**判分值——黏人的角色靠近就
    加分，戒备的角色同样的靠近要扣分，难度自然长在卡上，而不是长在代码里。

    截断是为了控制这段的长度：personality 常见一两百字，description 可能
    几千字，全塞进去既慢又容易把「他说的」挤到后面被忽略。1600 字的卡
    前面这段就够模型认人了。
    """
    c = card if isinstance(card, dict) else {}
    name = svc.char_name(c, "对方", key=key)
    lines = ["- 名字：" + name + (("（" + state + "）") if state else "")]
    p = str(c.get("personality") or "").strip()
    if p:
        lines.append("- 性格：" + p[:200])
    d = str(c.get("description") or "").strip()
    if d:
        lines.append("- 设定：" + d[:300])
    sb = svc._state_behavior_text(c)
    if sb:
        lines.append("- 状态下的她（不是阈值，是她高值时会怎么变）：\n" + sb)
    return "\n".join(lines)


# ── v2 正向关系线的第一段（2026-11-08）─────────────────────────
# 和上面那份 v1 打分提示词完全分开：v1 那套服务的是老档的距离层级 /
# 冲动 / 高潮 / 堕落 / 开发度，v2 一条都不碰。这里只判四条正向轴，
# 而且**每一轴都用中文把「这个数是什么意思」说清楚**——只扔英文字段名
# 给第二段演员，它根本不知道前面算的表白值代表什么，等于白算。


def judge_v2(card, said, state="", line_key="", gameday=None,
             history=None, note=""):
    from app.features.persona import service as svc
    """v2 的第一段：意图 + 四条正向轴 + 分段/导演条。返回 `(intent, note, score)`。

    判断失败返回 `(None, "", None)`——按 0 分走，不让整轮回不了话。
    """
    gd = gameday if isinstance(gameday, dict) else {}
    plan = str(gd.get("plan") or "").strip()
    ctx = affection.scoring_ctx(line_key) or {}
    # 前缀缓存（2026-11-10）：模板里「稳定规则在前、这一轮事实在末尾」。
    # 早期版本把动态数值放在第 3 行，整段提示词每轮都变，hit 恒为 0；
    # 现在前半段逐字节不变，DeepSeek 能整段命中，只有末尾事实按 miss 付。
    # 调用仍是单条 user 消息，消息结构没动。
    prompt = _SCORE_PROMPT_V2 % {
        "relation_name": ctx.get("relation_name") or "正常关系",
        "relation_desc": ctx.get("relation_desc") or "",
        "together": ("你们已经在一起了。" if ctx.get("together")
                     else "还没在一起；没表白之前，关系档不会自己往前跳。"),
        "contact_name": ctx.get("contact_name") or "还没碰过",
        "confess": int(ctx.get("confess") or 0),
        "confess_max": int(ctx.get("confess_max") or 50),
        "confess_name": ctx.get("confess_name") or "还没想过",
        "confess_desc": ctx.get("confess_desc") or "",
        "yandere_name": ctx.get("yandere_name") or "没有",
        "dev": ctx.get("devText") or "（五处都还没被开发过）",
        "item_keys": _item_keys_text(),
        "recent": ctx.get("recent") or "（你们还没什么交集）",
        "gday": _day_of(gd),
        "gphase": str(gd.get("phase") or "还没定"),
        "gplan": _plan_fact(gd, plan),
        "profile": svc._score_profile(card, "", key=line_key),
        # 上一轮状态逐轮会变，从「她是谁」里拆出来留在事实段：
        # 卡面那段因此恒定，能整段吃前缀缓存；信息一点没少给。
        "state_line": ("她上一轮身上的状态：" + str(state) + "\n")
                      if str(state or "").strip() else "",
        "said": str(said or "")[:2000],
        "history": svc._judge_recent(history),
        # 2026-11-19：长期账本只给开头 300 字。编剧判的是这一轮状态怎么变，
        # 最近 6 条原文 + 画像 recent + 状态数值已经覆盖语境；整本小记
        # （实测约 926 字 / 1244 token）每轮按未命中重付，省不下。
        # 只在这里截，不动 _judge_ledger 默认（v1 / 自由模式照旧看全本）。
        "ledger": _judge_ledger(str(note or "")[:300]),
        "facts_guide": _FACTS_GUIDE,
    }
    try:
        raw = svc._llm_call([{"role": "user", "content": prompt}], timeout=LLM_JUDGE_TIMEOUT)
    except Exception:                            # noqa: BLE001
        log.warning("v2 打分调用失败，按 0 分走")
        return None, "", None
    d = _parse_json_loose(raw)
    if not isinstance(d, dict):
        log.warning("v2 没交 JSON，按 0 分走（共 %d 字）：头=%r 尾=%r",
                    len(str(raw or "")), str(raw or "")[:120],
                    str(raw or "")[-120:])
        return None, "", None
    intent = str(d.get("intent") or "").strip().lower()
    if intent not in INTENTS:
        intent = None
    note = str(d.get("note") or "").strip()[:30]
    # settle_v2 只认这个 dict，少带一项等于模型白判。
    score = {k: d.get(k) for k in (
        "delta", "turn_type", "reason", "contact", "confess", "finish",
        "yandere", "proposal", "accept", "refused_confession", "facts",
        "addressed", "secret", "time", "phase", "plan", "place")}
    # RAG 路由（2026-11-18）：不进 settle，只给 run_turn 决定要不要翻旧账。
    score["rag"] = _norm_rag(d.get("rag"))
    # adult（2026-11-08）：身体这条线。**原样透传，不在这里净化**——
    # 结算器 affection_v2_adult.apply 负责 clamp/闸门/回落/去重，
    # 这里多改一处就多一个离线口径。
    if isinstance(d.get("adult"), dict):
        score["adult"] = d["adult"]
    # items（2026-11-08）：原样透传，净化在 app/items.py 里做。
    if isinstance(d.get("items"), dict):
        score["items"] = d["items"]
    return intent, note, score
