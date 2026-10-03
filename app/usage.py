"""每日 token 用量统计：按「天 + 会话」和「天 + 模型」两个维度聚合，另有一份
逐次调用的流水。

为什么需要它：MiMo / DeepSeek 后台只有总量，对不上「哪个群、哪个模型烧的钱」。
这里在 llm 层（_record_usage，流式/非流式共用口径）挂采集点，把每次调用的
命中/未命中/输出 token 归到当前**归属**名下——归属就是会话 key
（group_xxx / private_xxx），隐形调用（识图/接话判断/压缩摘要/长期记忆）
各有自己的类别名。

再按 **provider/model** 累一份（2026-10-03 补）：此前 record() 收了
provider/model 却只写日志、不做聚合，结果「不同模型到底花了多少」只能靠
grep 日志数行数，对不上账。

两份产物（都在 BASE_DIR/usage/，gitignore 内）：

  <date>.json        两个维度的汇总，管理页读它
      {"date": "...",
       "sessions": {"group_x": {"calls","hit","miss","output"}},
       "by_model": {"deepseek/deepseek-flash": {...}}}

  calls-<date>.jsonl  一行 = 一次真实调用，append-only
      {"t":"22:41:03","date":"...","tag":"group_x","kind":"stream",
       "prov":"deepseek","model":"deepseek-flash",
       "hit":14592,"miss":657,"out":537,"ms":1600,"ok":true}

    失败也记（`ok:false` + `err`）：降级链上被拉黑的那个候选原来只打一行
    [chain] 日志、不进账本，于是「账单 N 次 vs 日志 M 次」的缺口永远解释不了。
    append-only 还有个好处——进程被 kill 也只丢最后半行，不会污染已有记录。

归属通过线程本地变量传递：qq_bot 在轮次外层 scope(session_key)，内层的
识图、摘要等调用会被各自的 scope 覆盖成更具体的类别。落盘节流（默认
10 秒），进程重启最多丢最后一笔的落盘延迟，内存数据不丢的代价不值得付。
"""

import json
import logging
import os
import sys
import tempfile
import threading
import time

from app.config import BASE_DIR

log = logging.getLogger("usage")

# 落盘节流：两次写盘的最小间隔（秒）。统计场景不在乎这 10 秒的窗口。
_FLUSH_INTERVAL = 10.0

_local = threading.local()
_lock = threading.Lock()
# 流水 append 专用锁：写文件是磁盘 IO，不能塞进 _lock 拉长临界区
_call_lock = threading.Lock()

# {date: {tag: {"calls": n, "hit": n, "miss": n, "output": n}}}
_daily = {}
# {date: {"provider/model": {"calls": n, "hit": n, "miss": n, "output": n}}}
_by_model = {}
_dirty = set()          # 有未落盘改动的日期
_last_flush = 0.0

# {tag: 这条会话线最近一次调用的命中率}。**必须跨线程共享**：QQ 侧每条消息
# 换一个线程（asyncio.to_thread 从线程池取），而压缩跑在 save_history 里，
# 读不到发起那次调用的线程上的 usage。memory.trim_window 靠它判断「这条会话线
# 现在热不热」——2026-10-03 之前那里硬塞 hit_rate=0.0，等于把
# LOW_HIT_RATE=0.3 这个「命中率高就别压」的保护整个关掉了。
_last_hit = {}
_HIT_KEEP = 500         # 只留最近用过的若干条，防止长期运行无限增长


def _today():
    return time.strftime("%Y-%m-%d")


# ─── 测试隔离 ────────────────────────────────────────
# 测试跑一遍不能往**真实** usage/ 里灌假记录。原先只靠 test_usage.py 自己
# patch BASE_DIR，而调用流水是每轮对话、每次失败都写的——别的测试文件
# （test_agent_loop / test_llm_stream / test_vision…）并不知道要 patch，于是
# 那些 `https://example.invalid` 的 mock 调用全落进了真实账本（2026-10-03
# 实测：跑一次全量测试就往 calls-*.jsonl 里灌了上百行假记录，第二天对账
# 永远对不上）。
#
# 判据：unittest / pytest 驱动进程时一定会 import 同名顶层模块，而生产进程
# （main.py / qq_bot.py）不会——已确认 app/ 下没有任何地方 import 它们。
_TEST_DIR = None
_ORIG_BASE = BASE_DIR


def _is_test_process():
    return "unittest" in sys.modules or "pytest" in sys.modules


def _usage_dir():
    global _TEST_DIR
    # 测试显式 patch 了 BASE_DIR（test_usage.py 就是这么做）→ 听它的
    if BASE_DIR != _ORIG_BASE:
        return os.path.join(BASE_DIR, "usage")
    if _is_test_process():
        if _TEST_DIR is None:
            _TEST_DIR = tempfile.mkdtemp(prefix="agent_usage_test_")
        return _TEST_DIR
    return os.path.join(BASE_DIR, "usage")


def _usage_path(date):
    return os.path.join(_usage_dir(), "%s.json" % date)


def _calls_path(date):
    return os.path.join(_usage_dir(), "calls-%s.jsonl" % date)


def calls_path(date=None):
    """当天的调用流水文件路径（对账脚本 tools/bill.py 用）。"""
    return _calls_path(date or _today())


def model_key(provider, model):
    """模型维度的分组键。provider / model 各缺一个也能成键（"-" 占位）——
    否则「配了 provider 没填 model」的调用会整条从模型维度里丢掉。"""
    return "%s/%s" % (provider or "-", model or "-")


def current_tag():
    """当前线程的归属名；没设过就是 other（不该出现，兜底可见）。"""
    return getattr(_local, "tag", None) or "other"


def set_tail(n):
    """记下本轮「尾巴」的估算 token 数（状态栏 + extra_context）。

    尾巴挂在消息数组末尾、每轮都变，所以每轮必 miss——它的大小直接决定
    命中率天花板（≈ 1 − 尾巴/prompt）。只用于日志展示，不参与计费聚合。
    """
    _local.tail = int(n or 0)


def current_tail():
    """当前线程记下的尾巴 token 数；没设过是 0。"""
    return int(getattr(_local, "tail", 0) or 0)


class scope:
    """上下文管理器：把当前线程的用量归属设为 tag（嵌套时内层生效）。"""

    def __init__(self, tag):
        self.tag = tag
        self._prev = None

    def __enter__(self):
        self._prev = getattr(_local, "tag", None)
        _local.tag = self.tag
        return self

    def __exit__(self, *exc):
        _local.tag = self._prev
        return False


def _bump(slot, hit, miss, output):
    """往一个聚合槽里累一笔（会话维度与模型维度共用同一套口径）。"""
    slot["calls"] = slot.get("calls", 0) + 1
    slot["hit"] = slot.get("hit", 0) + hit
    slot["miss"] = slot.get("miss", 0) + miss
    slot["output"] = slot.get("output", 0) + output


def record(hit, miss, output=0, provider="", model="", elapsed=None,
           kind="", ok=True):
    """记一笔。同时累到「会话」和「provider/model」两个维度，并落一行流水。

    provider/model 传了才建模型维度的账；elapsed 是本次耗时（秒），只进流水。
    """
    global _last_flush
    date = _today()
    hit = int(hit or 0)
    miss = int(miss or 0)
    output = int(output or 0)
    with _lock:
        tag = current_tag()
        day = _daily.setdefault(date, {})
        _bump(day.setdefault(tag, {}), hit, miss, output)
        if provider or model:
            mslot = _by_model.setdefault(date, {}).setdefault(
                model_key(provider, model), {})
            _bump(mslot, hit, miss, output)
        # 顺带更新「这条会话线最近热不热」。在锁内直接写，不走 note_hit_rate
        # ——_lock 是普通 Lock，重入会死锁。
        tot = hit + miss
        if tot > 0:
            _last_hit[tag] = float(hit) / tot
            if len(_last_hit) > _HIT_KEEP:
                for k in list(_last_hit)[:_HIT_KEEP // 2]:
                    _last_hit.pop(k, None)
        _dirty.add(date)
        due = time.time() - _last_flush >= _FLUSH_INTERVAL
    log_call({"date": date, "tag": tag, "kind": kind or "llm",
              "prov": provider, "model": model,
              "hit": hit, "miss": miss, "out": output,
              "ms": int(elapsed * 1000) if elapsed else None, "ok": ok})
    if due:
        flush()


def log_call(entry):
    """追加一行调用流水到 usage/calls-<date>.jsonl。

    entry 里没给的字段用默认值补齐（t=当前时刻、date=今天、tag=当前归属）。
    值为 None 的字段会被丢掉，流水里不写一堆空键。
    """
    date = entry.get("date") or _today()
    e = {"t": time.strftime("%H:%M:%S"), "date": date, "tag": current_tag()}
    for k, v in entry.items():
        if v is not None:
            e[k] = v
    try:
        os.makedirs(_usage_dir(), exist_ok=True)
        line = json.dumps(e, ensure_ascii=False)
        with _call_lock:
            with open(_calls_path(date), "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except (OSError, TypeError, ValueError) as exc:
        log.warning("调用流水写入失败 %s：%s", _calls_path(date), exc)


def log_fail(provider, model, err, kind="llm"):
    """记一次失败（降级链上被拉黑的那个候选）。

    只记日志的失败等于没记——账单是按「请求数」算的，失败也占一次调用，
    不落流水就永远对不上「账单 N 次 vs 日志 M 次」。
    """
    reason = " ".join(str(err).split())[:120]
    log_call({"kind": kind, "prov": provider, "model": model,
              "ok": False, "err": reason})


def last_hit_rate(tag):
    """某条会话线最近一次调用的命中率（0~1）；没记过返回 None。

    调用方要区分「已知很热」和「不知道」——不知道时应当退回保守行为，
    不能当成 0（那会误触提前压缩）。
    """
    if not tag:
        return None
    with _lock:
        return _last_hit.get(str(tag))


def flush():
    """把内存里攒的用量写盘（原子写：临时文件 + replace）。"""
    global _last_flush
    with _lock:
        dates = list(_dirty)
        _dirty.clear()
        _last_flush = time.time()
        os.makedirs(_usage_dir(), exist_ok=True)
        for date in dates:
            payload = {
                "date": date,
                "sessions": _daily.get(date) or {},
                "by_model": _by_model.get(date) or {},
            }
            path = _usage_path(date)
            tmp = path + ".tmp"
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(payload, f, ensure_ascii=False, indent=1)
                os.replace(tmp, path)
            except OSError as e:
                log.warning("用量落盘失败 %s：%s", path, e)
                _dirty.add(date)             # 失败的下轮再试


def daily(date=None):
    """读一天的聚合结果（内存优先，落盘文件兜底——供跨进程/重启后查看）。"""
    date = date or _today()
    with _lock:
        data = _daily.get(date)
        if data is not None:
            return {"date": date, "sessions": data,
                    "by_model": _by_model.get(date) or {}}
    try:
        with open(_usage_path(date), encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict):
            d.setdefault("sessions", {})
            d.setdefault("by_model", {})
            d.setdefault("date", date)
            return d
    except (OSError, ValueError):
        pass
    return {"date": date, "sessions": {}, "by_model": {}}


def load_all_dates():
    """已有落盘的日期列表（管理页做日期选择用）。

    只认 <date>.json，流水 calls-<date>.jsonl 天然被排除（后缀不同）。
    """
    try:
        return sorted(f[:-5] for f in os.listdir(_usage_dir())
                      if f.endswith(".json"))
    except OSError:
        return []


def _merge_saved(saved, bucket):
    """把磁盘上的一份聚合并进内存（同日重启不能让账劈两半）。"""
    for key, slot in (saved or {}).items():
        cur = bucket.setdefault(key, {})
        for k in ("calls", "hit", "miss", "output"):
            cur[k] = cur.get(k, 0) + int(slot.get(k, 0) or 0)


def _load_from_disk_into_memory():
    """进程启动时把「今天可能已有的半份账」并回内存，避免同日重启把账劈两半。"""
    date = _today()
    saved = daily(date)
    with _lock:
        _merge_saved(saved.get("sessions"), _daily.setdefault(date, {}))
        _merge_saved(saved.get("by_model"), _by_model.setdefault(date, {}))


_load_from_disk_into_memory()
