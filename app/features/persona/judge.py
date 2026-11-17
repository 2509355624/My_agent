"""意图判断的两条 LLM 主链：自由模式与攻略模式。"""

import logging

from app.config import LLM_JUDGE_TIMEOUT
from app.features.persona import affection
from app.features.persona.game_text import _plan_fact
from app.features.persona.game_time import _day_of
from app.features.persona.json_utils import _parse_json_loose
from app.features.persona.judge_fields import (
    _director_fields, _item_keys_text, _judge_ledger, _norm_finish,
)
from app.features.persona.prompt_facts import _FACTS_GUIDE
from app.features.persona.prompts import _INTENT_PROMPT, _SCORE_PROMPT

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
        # 类别不认识不影响事实字段：只要它交了 facts / secret，照样能用。
        extra = _director_fields(d)
        if not (extra["facts"] or extra["secret"]):
            extra = {}
        return None, "", extra
    note = str(d.get("note") or "").strip()[:30]
    return intent, note, _director_fields(d)


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
             "plan": d.get("plan")}
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
