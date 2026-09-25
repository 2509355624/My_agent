"""主动接话判断：决定「此刻值不值得在群里插一句」。

## 它解决什么

真人开口不是因为「轮到我了」，而是因为刚才那句话有个可以接的点。随机 / 定时
发言做不到这件事，只会变成定时播报。所以这里需要一个判断，而这个判断只能由
语言模型来做。

## 为什么是 LLM 而不是本地小模型（实测过的，别再试一遍）

最初用本地 Laya（ConvAI Innovations 的判别式模型，Apache-2.0，322M 的
multilingual 分支）来做，因为它是这两天的热点、免费、单次前向只要几十毫秒。
**实测下来完全不可用**：同一批 10 个群聊场景（5 该接 / 5 不该接），它把该接的
5 条全判成「不接」——连「最近想入个二手平板，有推荐的型号吗」的「接」概率
都只有 0.06。换过 5 种 criteria 写法（对称、直白、极简、noul 是非题），最高
也就 6/10，且错的都是正样本。改问法让它做「消息分类」，7 类只对 6/10，
连「这个报错有人遇到过吗」都判成「闲聊」（判成「提问」的概率 0.006）；
改成 0-2 打分的 score 题型，方向整个反了（该接的平均 0.98 < 不该接的 1.15）。

根因：Laya 是**判别式分类模型**，没有「理解任务」的能力，只是在选项间算匹配。
它宣称的中文 95% 来自客服工单分类（话题域明确、类别差异大），而「群里这句话
该不该有人接」类别之间差异极小，落在它能力之外。本机 Ollama 里现成的小模型
（Qwen3.5-0.8B）也试过：6/10，平均 3.89 秒，比调 API 还慢。

对照数据（同一批样本）：
    Laya 判别式模型     5/10    30ms    免费
    Ollama 0.8B        6/10    3.89s   免费
    deepseek-flash     9/10    1.56s   一天约 5 分钱  ← 用它

所以「用本地小模型省钱」这个前提是错的：省下的钱一天不到一毛，代价是判断
完全不可用。而这笔钱本来就不是开销的大头——真正贵的是被叫起来聊天的主模型，
判断的职责恰恰是**让它少被叫几次**。

## 三种模式（`QQ_INTERJECT_MODE`）

    off     完全不做（默认）。零开销，行为跟以前一模一样。
    shadow  照常判断、照常记日志、照常走冷却，但**不发言**。用来看它判得准
            不准，跑一两天翻日志再决定放不放开。
    on      判「接」且过了冷却闸就真的开口。

## 两道闸

    MIN_GAP   两次**判断**之间的最小间隔——判断本身也是 API 调用，群聊刷屏
              时不能每条都问。
    COOLDOWN  两次**发言**之间的最小间隔——防刷屏。判错一次是意外，连着说
              就是骚扰，这道闸比判断准不准更要紧。
"""

import json
import logging
import os
import threading
import time

from app import agents as agent_store
from app import recent
from app.config import (
    QQ_INTERJECT_CONTEXT_MAX_CHARS, QQ_INTERJECT_CONTEXT_MESSAGES,
    QQ_INTERJECT_COOLDOWN, QQ_INTERJECT_GROUPS, QQ_INTERJECT_MIN_GAP,
    QQ_INTERJECT_MODE,
)
from app.llm import call_llm

log = logging.getLogger("interject")

# 判断标准的写法就是这个功能的全部「策略」。Laya 那轮测试证明了模型给什么
# 标准就按什么标准判——所以「什么算该接话」要调，改的是这段而不是代码。
# 口径 = 「适中」打底（有明确落点才接）+ 好奇心（2026-09-25 用户加的）：
# 看到新东西/图会想凑一句。注意判断模型只看得到「[图片]」占位符。
_SYSTEM = """你在帮一个 QQ 群机器人判断：此刻要不要主动开口接一句话。

下面给你群里最近的对话，按时间顺序排列，最后一条是刚发出来的。
机器人没被 @，也没人点它的名，它只是自己判断要不要插一句。

判断口径（像个人，有点表达欲和好奇心，不是客服）：
- 该接：有人在提问还没人回答、求推荐、找人、吐槽抱怨、抛出观点想讨论、
        冷场没人接话、话说到一半明显还需要人回应
- 好奇也算该接：有人发了张图、提到没见过的新东西/新梗/奇怪的报错，
        想问一句「这是啥」「在哪弄的」就说
- 群里聊得热闹、有你能插上话的空隙，也算该接
- 不接：两个人的私事、无意义刷屏、明显是别人之间的事不需要第三个人插嘴
- 拿不准的时候倾向「接」——真人插话本来就不需要充分的理由

注意：你只能看到「[图片]」这样的占位符，看不到图的内容——好奇可以，
别假装你看清了图里画的是什么。

只回答两个字之一：「接」或「不接」。不要任何解释、不要标点、不要思考过程。"""

# 主动开口时给模型的正文。这里**不能**把群聊消息当提问喂进去——没人点名它，
# 那些消息也不是对它说的。真正的上下文由 extra_context（群聊背景）提供，
# 这段只负责说清「现在该你主动接一句」这件事。
INTERJECT_PROMPT = (
    "（没人点名你。你刚看到群里在聊上面那些，想顺势接一句——"
    "说一句自然的、像群里人说的话就行。别 @ 谁，也别解释你在做什么。"
    "上下文里你自己名字开头的那几行，是你自己刚说过的话——"
    "别换个说法重复，也别接着自己上一句往下说。）"
)

_state_lock = threading.Lock()
_last_spoke = {}        # (agent_id, group_id) -> 上次开口的时间戳
_last_judged = {}       # (agent_id, group_id) -> 上次判断的时间戳


def enabled():
    """这个功能是否参与判断。off 时调用方压根不必走到这里。"""
    return QQ_INTERJECT_MODE in ("shadow", "on")


def speaking():
    """判断通过之后要不要真的发言。

    影子模式下判断照跑、日志照记，但一个字都不发出去——这就是观察期的意义。
    """
    return QQ_INTERJECT_MODE == "on"


def _gap_ok(agent_id, group_id):
    """距离上次判断是否够久。判断也要调 API，群聊刷屏时不能每条都问。"""
    if QQ_INTERJECT_MIN_GAP <= 0:
        return True
    with _state_lock:
        last = _last_judged.get((agent_id, group_id))
    return last is None or (time.time() - last) >= QQ_INTERJECT_MIN_GAP


def _mark_judged(agent_id, group_id):
    """在真正调用**之前**记时间：调用失败时也计入节流，否则会疯狂重试。"""
    with _state_lock:
        _last_judged[(agent_id, group_id)] = time.time()


def _cooldown_ok(agent_id, group_id):
    if QQ_INTERJECT_COOLDOWN <= 0:
        return True
    with _state_lock:
        last = _last_spoke.get((agent_id, group_id))
    return last is None or (time.time() - last) >= QQ_INTERJECT_COOLDOWN


def mark_spoke(agent_id, group_id):
    """记下「机器人这个群刚开过口」。

    调用方不止主动接话一处——qq_bot 的 _deliver 在任何一次回复真正发出去后
    都要调它（被 @ 的回复也算说话）。冷却管的是这张嘴，不只是接话这个动作；
    否则刚 @ 完就接话，接出来的话跟刚回的内容撞车。
    """
    with _state_lock:
        _last_spoke[(agent_id, group_id)] = time.time()


def _parse(out):
    """从模型回复里取「接」或「不接」。认不出来返回空串（当成不接）。"""
    head = (out or "").strip()[:12]
    if "不接" in head:
        return "不接"
    if "接" in head:
        return "接"
    return ""


def _log_path(agent_id, group_id):
    aid = agent_store.safe_agent_id(agent_id) or "main"
    key = agent_store.safe_session_key("group_" + str(group_id))
    if not key:
        return ""
    return os.path.join(agent_store.AGENTS_DIR, aid, "interject", key + ".jsonl")


def _log_verdict(agent_id, group_id, verdict):
    """把一次判断落盘。影子模式靠它复盘，所以带上判据和模型原话。

    同时往标准日志打一条：影子模式开着的时候用户多半正盯着控制台。
    """
    if verdict["pass"]:
        tail = ("→ 会开口（影子模式，不发出）" if QQ_INTERJECT_MODE == "shadow"
                else "→ 会开口")
    elif verdict["want"]:
        tail = "→ 想接，但冷却中"
    else:
        tail = "→ 不接"
    log.info("接话判断[%s] 群%s：%s 耗时%.1fs %s",
             QQ_INTERJECT_MODE, group_id, verdict["choice"],
             verdict["latency_ms"] / 1000.0, tail)

    path = _log_path(agent_id, group_id)
    if not path:
        return
    rec = {"t": int(time.time()), "mode": QQ_INTERJECT_MODE}
    rec.update(verdict)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError as exc:
        log.warning("写接话判断日志失败 %s：%s", path, exc)


def muted_groups(agent_id):
    """该 agent 被管理页静音的群列表（settings.json 的 interject_muted）。

    热生效：管理页保存即写盘，这里 mtime 缓存读，下一轮消息就生效，
    不用重启。名单是「关」的语义——env 的试点白名单管「谁可以」，这里
    管「谁被按了静音」，两层独立。
    """
    return agent_store.load_settings(agent_id).get("interject_muted") or []


def decide(agent_id, group_id):
    """判断这个群此刻值不值得主动开口。

    返回 None 表示「这次不判断」（功能没开、群不在试点名单、被静音、
    冷却中、节流中、没上下文、调用失败）。返回 dict 时字段含义：
        choice      模型给的判断，「接」或「不接」
        want        模型说该接
        cooled      发言冷却已过
        pass        want 且 cooled —— 只有它为真才该真开口
        latency_ms  这次判断耗时
        raw         模型原话前若干字，排查用
    """
    if not enabled():
        return None
    gid = str(group_id)
    if QQ_INTERJECT_GROUPS and gid not in QQ_INTERJECT_GROUPS:
        return None
    if gid in muted_groups(agent_id):
        return None
    # 正式模式下冷却中连判断都不做——判断也是一次 API 调用，判完反正开不了
    # 口，白花时间白刷日志。影子模式不跳：观察期就是要看它对每条消息的判断。
    if speaking() and not _cooldown_ok(agent_id, gid):
        return None
    if not _gap_ok(agent_id, gid):
        return None

    # 上下文直接从「最近群聊」缓存取——它按时间正序、已带昵称，而且不 @ 机器人
    # 的消息也在里面（这正是判断时机所需要的）。口子比给主模型的紧——判断是
    # 每群高频调用，prompt 越短越省钱。
    state = recent.format_recent(agent_id, gid, QQ_INTERJECT_CONTEXT_MESSAGES,
                                 QQ_INTERJECT_CONTEXT_MAX_CHARS)
    if not state:
        return None

    # provider/model 必须显式取，不能让 call_llm 用全局默认——这是后台的隐形
    # 调用，跟「摘要漏传 provider 导致回退火山」是同一个坑（见 app/memory.py）。
    cfg = agent_store.agent_config(agent_id)
    provider = cfg.get("provider") or None
    model = cfg.get("model") or None

    _mark_judged(agent_id, gid)
    t0 = time.time()
    try:
        out = call_llm(
            [{"role": "system", "content": _SYSTEM},
             {"role": "user", "content": "群聊记录：\n" + state}],
            provider=provider, model=model, timeout=60)
    except Exception:
        log.exception("接话判断调用失败，这次当「不接」")
        return None
    latency = (time.time() - t0) * 1000

    choice = _parse(out)
    want = (choice == "接")
    cooled = _cooldown_ok(agent_id, gid)
    verdict = {
        "choice": choice or "?",
        "want": bool(want),
        "cooled": bool(cooled),
        "pass": bool(want and cooled),
        "latency_ms": round(latency, 1),
        "context_chars": len(state),
        "raw": (out or "").strip()[:40],
    }

    if verdict["pass"]:
        mark_spoke(agent_id, gid)

    # 影子模式全记（就是要看它「不接」判得对不对）；正式模式只记「想接却被
    # 冷却挡掉」这种，否则日志会跟着群消息量一起涨。
    if QQ_INTERJECT_MODE == "shadow" or (verdict["want"] and not verdict["cooled"]):
        _log_verdict(agent_id, gid, verdict)
    return verdict
