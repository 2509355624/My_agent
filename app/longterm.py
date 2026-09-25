"""群聊长期记忆：把归档的历史摘要成一条条「回忆」，按群存、注入回去。

## 它解决什么

recent.py 的滚动缓存只装得下「刚才在聊什么」（200 条），再早的进 archive
就再也不会被模型看见——所以它记不住任何人的偏好和旧事，每次都像刚进群。
长期记忆在归档这件事上顺手多走一步：这批消息反正已经从缓存里毕业了，
交给模型压缩成一条短摘要（聊了什么话题 + 谁表达了什么偏好/立场），
存进 agents/<agent_id>/memory/group_<群号>.jsonl，之后每轮回复时把最近的
几条摘要一起注入——模型就「记得」上个月群里聊过什么了。

## 存储格式（一条一行 JSON）

    {"t": 时间戳, "d": "2026-09-25", "n": 摘掉的消息条数, "s": "摘要正文"}

追加式，不裁剪：一条摘要几百字，一天触发不了几次，攒一年也没有多少。
它本身已经是高度浓缩的信息，不急着做二级压缩。

## 关键取舍

- **摘要在后台线程跑**：触发点在 recent._trim → remember → WS 消息循环，
  在那里同步调一次 API（一两秒）会把所有群的消息处理一起卡住——跟接话
  判断必须放 worker 线程是同一个坑。摘要晚几十秒落盘没有任何影响。
- **摘要失败只记日志**：archive 里的原文还在，这批消息没有丢，只是这轮
  没生成记忆。绝不影响聊天主链路。
- **provider/model 显式取自 agent 配置**：这是后台隐形调用，漏传会回退
  全局默认（火山），见 interject.decide 里同一处注释。
"""

import json
import logging
import os
import threading
import time

from app import agents as agent_store
from app.config import QQ_MEMORY_DIGEST_TIMEOUT
from app.llm import call_llm

log = logging.getLogger("longterm")

# 单进程内写锁：两群并发摘要各自写各自的文件，理论上不需要；同一个群两个
# 归档撞在一起时保证行级完整。跨进程并发由「追加单行 < 缓冲区原子性」兜底。
_WRITE_LOCK = threading.Lock()

# 喂给摘要模型的原文上限（字符）。200 条消息正常也就几千字，这个闸只防
# 刷屏合并出来的极端情况；装不下从后往前留——近的比远的要紧。
_SOURCE_MAX_CHARS = 8000

_SYSTEM = """下面是一个 QQ 群一段时间里的聊天记录。请把它整理成一段「事后回忆」，
供群机器人以后翻看。要求：
1. 按话题概括这段时间群里聊了什么——是回忆，不是流水账，一件事两三句就够。
2. 单独留意每个人：谁偏好什么、谁在做什么事、谁跟谁聊得来或争论过什么，
   有值得记的就写成「昵称：…」；记录里没提的不要编。
3. 总长 300 字以内，直接输出回忆正文，不要开场白、不要解释。"""


def _dir(agent_id):
    aid = agent_store.safe_agent_id(agent_id) or "main"
    return os.path.join(agent_store.AGENTS_DIR, aid, "memory")


def _path(agent_id, group_id):
    """记忆文件路径；群号非法时返回空串（与 recent._path 同一套白名单）。"""
    key = agent_store.safe_session_key("group_" + str(group_id))
    if not key:
        return ""
    return os.path.join(_dir(agent_id), key + ".jsonl")


def digest_async(agent_id, group_id, lines):
    """把一批裁剪下来的原始行交给后台线程生成摘要。永不抛错、永不阻塞。

    lines 是 recent._trim 丢掉的那几百行原始 JSON 文本（还没解析）。坏行
    在这里丢掉；解析完一条都不剩就不起线程了。
    """
    try:
        recs = _parse_lines(lines)
    except Exception:
        log.exception("长期记忆：解析归档行失败")
        return
    if not recs:
        return
    try:
        threading.Thread(target=_digest, args=(agent_id, str(group_id), recs),
                         daemon=True, name="memory-digest").start()
    except Exception:
        log.exception("长期记忆：摘要线程启动失败")


def _parse_lines(lines):
    out = []
    for line in lines or []:
        line = (line or "").strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue        # 半截行跳过，别让一条坏行毁掉整批摘要
        if isinstance(rec, dict) and rec.get("x"):
            out.append(rec)
    return out


def _render(rec):
    who = rec.get("n") or rec.get("u") or "某人"
    return "%s：%s" % (who, rec.get("x", ""))


def _digest(agent_id, group_id, recs):
    """调模型生成一条摘要并追加落盘。失败只记日志——原文在 archive 里。"""
    body = "\n".join(_render(r) for r in recs)
    if len(body) > _SOURCE_MAX_CHARS:
        body = body[-_SOURCE_MAX_CHARS:]

    # provider/model 必须显式取：后台隐形调用漏传会回退全局默认 provider，
    # 表现为「切了模型之后摘要还在烧旧家的额度」（与压缩摘要同一个坑）。
    cfg = agent_store.agent_config(agent_id)
    provider = cfg.get("provider") or None
    model = cfg.get("model") or None

    try:
        summary = call_llm(
            [{"role": "system", "content": _SYSTEM},
             {"role": "user", "content": "聊天记录：\n" + body}],
            provider=provider, model=model, timeout=QQ_MEMORY_DIGEST_TIMEOUT)
    except Exception:
        log.exception("长期记忆摘要调用失败（群%s，%d 条）", group_id, len(recs))
        return
    summary = (summary or "").strip()
    if not summary:
        return

    path = _path(agent_id, group_id)
    if not path:
        return
    rec = {
        "t": int(time.time()),
        "d": time.strftime("%Y-%m-%d"),
        "n": len(recs),
        "s": summary,
    }
    with _WRITE_LOCK:
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError as exc:
            log.warning("写长期记忆失败 %s：%s", path, exc)
            return
    log.info("长期记忆[群%s] 已存一条摘要（%d 条消息 → %d 字）",
             group_id, len(recs), len(summary))


def load_memories(agent_id, group_id, limit):
    """取最近 limit 条摘要，按时间正序返回。读不到/没记过返回空列表。"""
    path = _path(agent_id, group_id)
    if not path or limit <= 0:
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return []

    out = []
    for line in lines[-limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue        # 半截行跳过
        if isinstance(rec, dict) and rec.get("s"):
            out.append(rec)
    return out


def format_memories(agent_id, group_id, limit, max_chars):
    """渲染成给模型看的「以前聊过什么」；没有记忆时返回空串（调用方跳过）。

    max_chars 从最新往前累计，装不下丢更旧的——与 format_recent 同一个
    取舍：近的比远的要紧。渲染顺序按时间正序，读起来才像回忆。
    """
    if limit <= 0 or max_chars <= 0:
        return ""
    recs = load_memories(agent_id, group_id, limit)
    if not recs:
        return ""

    picked, total = [], 0
    for rec in reversed(recs):
        summary = (rec.get("s") or "").strip()
        if not summary:
            continue
        line = "%s：%s" % (rec.get("d") or "?", summary)
        if picked and total + len(line) > max_chars:
            break
        picked.append(line)
        total += len(line)
    picked.reverse()
    if not picked:
        return ""
    return "[这个群更早的记忆]\n" + "\n".join(picked)
