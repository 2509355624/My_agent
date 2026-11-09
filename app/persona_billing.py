"""角色扮演「代充」账本（2026-11-09）。

## 只干一件事

站主手动给某个 QQ 登记一笔钱（比如 5 元），之后这个人在**网页扮演**里用到
「站主付费的官方 DeepSeek」时，按官方价目表从余额里扣。免费中转站和玩家
自带 Key **只记流水、不扣钱**。

## 为什么单独一本账，而不是改 app/usage.py

`usage.py` 是全站共用的 token 统计，归属靠 `usage.scope()` 的 tag——那是
「哪个会话烧了多少 token」，不是「谁该付钱」：QQ 机器人、漫画、识图全在里面，
tag 还会被内层调用覆盖。硬往里塞 `user_id` / `cost` 会把所有调用方都拖下水。
所以这里另起一份：

  config/persona_billing.json   价格表 + 高峰/闲时规则（**进 git**，无密钥）
  state/persona_billing.json   余额 + 充值记录（gitignore）
  usage/persona_bill-<date>.jsonl  逐笔流水（gitignore，append-only）

## 钱怎么算（关键的一行）

官方价目表单位是「元 / 每百万 token」。于是：

    tokens × (元 / 1e6 token) = 元 × 1e-6 = **微元**

也就是「token 数乘以表里的数字，直接就是微元」，不用再除一百万。余额一律
存整数微元（1 元 = 1_000_000），不碰浮点累加，账不会因为几万笔而飘。

## 谁来挂计费上下文

`persona_api` 在 `before_request` 里 `begin(_qq())`、`teardown_request` 里
`end()`。线程本地——后台提炼记忆的线程拿不到上下文，自然跳过付费段；
没登记过的 QQ 也不会进账本（不给站主凭空造一堆零元流水）。

## 链路与记账是两件事

`llm.candidates()` 问 `paid_enabled()` 决定要不要把付费段插进降级链；
`llm._record_usage()` 调 `record_call()` 记这一笔。只有**真的打到付费段**的
调用才扣钱——调用失败没有 usage，本来就不会走到这里。
"""

import csv
import io
import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app import config

log = logging.getLogger("persona_billing")

MICRO = 1_000_000                       # 1 元 = 1e6 微元
CONFIG_PATH = os.path.join(config.BASE_DIR, "config", "persona_billing.json")
STATE_PATH = config.state_path("persona_billing.json")
BILL_DIR = None                          # 测试可 patch；None = 跟 usage/ 同目录

_local = threading.local()
_LOCK = threading.RLock()
_cfg = {"mtime": -1, "data": None}

# 配置文件万一被误删，也不能让整个扮演接口 500。这四个数是 2026-11-09
# 官方价目表的快照，只作兜底；正常永远走 config/persona_billing.json。
_FALLBACK = {
    "paid": {"provider": "deepseek", "model": "deepseek-flash"},
    "prices": {
        "deepseek-flash": {
            "offpeak": {"hit": 0.02, "miss": 1.0, "out": 4.0},
            "peak": {"hit": 0.04, "miss": 2.0, "out": 8.0},
        },
        "deepseek-v4-pro": {
            "offpeak": {"hit": 0.15, "miss": 4.5, "out": 13.5},
            "peak": {"hit": 0.30, "miss": 9.0, "out": 27.0},
        },
    },
    "peak": {"timezone": "Asia/Shanghai", "weekdays": [0, 1, 2, 3, 4],
             "windows": [["09:00", "12:00"], ["14:00", "18:00"]],
             "holidays": []},
}


# ─── 配置 ───────────────────────────────────────────

def load_config():
    """按 mtime 热加载价格表（改价目表不用重启 5174）。"""
    try:
        mt = os.path.getmtime(CONFIG_PATH)
    except OSError:
        return _FALLBACK
    with _LOCK:
        if _cfg["mtime"] == mt and _cfg["data"] is not None:
            return _cfg["data"]
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                d = json.load(f)
            if not isinstance(d, dict) or not d.get("prices"):
                raise ValueError("价格表为空")
            _cfg["data"] = d
            _cfg["mtime"] = mt
            return d
        except (OSError, ValueError) as e:
            log.warning("计费配置读不了，用内置兜底：%s", e)
            return _FALLBACK


def paid_target():
    """付费段在降级链里的 (provider, model)。"""
    p = load_config().get("paid") or _FALLBACK["paid"]
    return (str(p.get("provider") or "deepseek"),
            str(p.get("model") or "deepseek-flash"))


def is_peak(ts=None, cfg=None):
    """北京时间是不是官方口径的高峰时段。

    周一至周五、法定节假日除外，9:00-12:00 与 14:00-18:00（**右端不含**）。
    周末 / 节假日全天闲时。调休补班的周末仍按周末算——官方写的是「周一至周五」，
    不是「工作日」。ts 传 epoch 秒便于测试固定时刻。
    """
    cfg = cfg or load_config()
    p = cfg.get("peak") or _FALLBACK["peak"]
    try:
        tz = ZoneInfo(p.get("timezone") or "Asia/Shanghai")
    except Exception:                                # noqa: BLE001
        tz = ZoneInfo("Asia/Shanghai")
    dt = datetime.fromtimestamp(float(time.time() if ts is None else ts), tz)
    if dt.weekday() not in set(p.get("weekdays") or [0, 1, 2, 3, 4]):
        return False
    day = dt.strftime("%Y-%m-%d")
    for item in p.get("holidays") or []:
        if len(item) >= 2 and str(item[0]) <= day <= str(item[1]):
            return False
    hm = dt.strftime("%H:%M")
    for w in p.get("windows") or []:
        if len(w) >= 2 and str(w[0]) <= hm < str(w[1]):
            return True
    return False


def price_for(model, peak, cfg=None):
    """某个模型在高峰/闲时下的三组单价（元 / 百万 token）。"""
    cfg = cfg or load_config()
    group = cfg.get("prices") or _FALLBACK["prices"]
    table = group.get(str(model)) or group.get(paid_target()[1]) or {}
    slot = table.get("peak" if peak else "offpeak") or {}
    return {k: float(slot.get(k) or 0.0) for k in ("hit", "miss", "out")}


def cost_micro(hit, miss, out, price):
    """一次调用的金额（微元）。见模块头：token × (元/百万token) = 微元。"""
    return max(0, int(round(
        int(hit or 0) * price["hit"]
        + int(miss or 0) * price["miss"]
        + int(out or 0) * price["out"])))


def fmt_yuan(micro, digits=4):
    return ("%%.%df" % digits) % (int(micro or 0) / MICRO)


# ─── 余额 / 充值（state/persona_billing.json）───────────

def _load_unlocked():
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict) and isinstance(d.get("accounts"), dict):
            return d
    except (OSError, ValueError):
        pass
    return {"version": 1, "accounts": {}}


def _save_unlocked(st):
    try:
        os.makedirs(os.path.dirname(STATE_PATH) or ".", exist_ok=True)
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=1)
        os.replace(tmp, STATE_PATH)
    except OSError as e:
        # 记账失败不能把聊天弄挂，但必须留痕——不然钱少了都不知道
        log.warning("计费余额落盘失败 %s：%s", STATE_PATH, e)


def balance_of(qq):
    """余额（微元）；没登记过返回 0。"""
    with _LOCK:
        acc = (_load_unlocked()["accounts"]).get(str(qq or "")) or {}
        return int(acc.get("balance") or 0)


def account(qq):
    with _LOCK:
        acc = (_load_unlocked()["accounts"]).get(str(qq or ""))
        return dict(acc) if isinstance(acc, dict) else None


def accounts():
    """查看台用：所有登记过的账号，按最近活动倒序。"""
    with _LOCK:
        rows = [dict(v, qq=k) for k, v in
                (_load_unlocked()["accounts"]).items()
                if isinstance(v, dict)]
    rows.sort(key=lambda r: r.get("updated") or 0, reverse=True)
    return rows


def recharge(qq, amount_yuan, note=""):
    """站主手动登记一笔充值。返回 (账号, 错误文本)。"""
    qq = str(qq or "").strip()
    if not qq.isdigit() or not (4 <= len(qq) <= 15):
        return None, "QQ 号看着不对"
    try:
        amount = float(amount_yuan)
    except (TypeError, ValueError):
        return None, "金额填个数字"
    if not (0 < amount <= 10000):
        return None, "金额要在 0 到 10000 元之间"
    micro = int(round(amount * MICRO))
    with _LOCK:
        st = _load_unlocked()
        acc = st["accounts"].setdefault(qq, {
            "qq": qq, "balance": 0, "recharged": 0, "spent": 0,
            "created": time.time(), "recharges": []})
        acc["balance"] = int(acc.get("balance") or 0) + micro
        acc["recharged"] = int(acc.get("recharged") or 0) + micro
        acc.setdefault("recharges", []).append({
            "t": time.time(), "amount": micro,
            "note": str(note or "").strip()[:120]})
        acc["updated"] = time.time()
        _save_unlocked(st)
        return dict(acc), ""


def _deduct_unlocked(st, qq, micro, ts):
    acc = st["accounts"].get(qq)
    if not isinstance(acc, dict):
        return 0
    before = int(acc.get("balance") or 0)
    # 单次调用最多把余额吃到 0，不记负数；差的那几分钱当站主送的，
    # 下一轮 paid_enabled() 看见 0 就不再插付费段。
    after = max(0, before - micro)
    acc["balance"] = after
    acc["spent"] = int(acc.get("spent") or 0) + min(before, micro)
    acc["updated"] = ts
    st["accounts"][qq] = acc
    return after


# ─── 请求上下文（线程本地）─────────────────────────────

def begin(qq):
    """挂上本次请求的付费归属。未登记 / 游客 = 不挂。

    **先清空再判定**：状态文件万一读不了，也不能把上一个请求留在这个线程里
    的 QQ 继承过来——那会把别人的钱记到另一个人头上。
    """
    qq = str(qq or "").strip()
    _local.prev = getattr(_local, "qq", "")
    _local.qq = ""
    if not qq:
        return ""
    try:
        if account(qq) is not None:
            _local.qq = qq
    except Exception:                                    # noqa: BLE001
        log.warning("计费账号读取失败，本轮按未登记处理", exc_info=True)
    return _local.qq


def end():
    # 不留「上一轮」：Flask 一个请求一个线程，钩子只有一层，恢复 prev 只在
    # 手动嵌套 begin 时才有意义，而它更容易把陈旧的归属带回来。
    _local.qq = ""
    _local.prev = ""


def current_qq():
    return getattr(_local, "qq", "") or ""


def paid_enabled():
    """这一轮要不要把站主付费的官方 DeepSeek 插进降级链。"""
    qq = current_qq()
    if not qq:
        return False
    if (load_config().get("paid") or {}).get("enabled", True) is False:
        return False
    return balance_of(qq) > 0


# ─── 逐笔流水（usage/persona_bill-<date>.jsonl）─────────

def _bill_dir():
    if BILL_DIR:
        return BILL_DIR
    # 复用 usage 的目录决议：测试进程自动导向临时目录，不会往真实 usage/ 灌假账
    from app import usage as usage_stats
    return os.path.dirname(usage_stats.calls_path())


def bill_path(date):
    return os.path.join(_bill_dir(), "persona_bill-%s.jsonl" % date)


def _beijing_date(ts, cfg=None):
    cfg = cfg or load_config()
    try:
        tz = ZoneInfo((cfg.get("peak") or {}).get("timezone") or "Asia/Shanghai")
    except Exception:                                # noqa: BLE001
        tz = ZoneInfo("Asia/Shanghai")
    return datetime.fromtimestamp(float(ts), tz)


def record_call(provider="", model="", hit=0, miss=0, out=0,
                kind="", tag=""):
    """一次成功的 LLM 调用记一笔；只有打到付费段才扣余额。

    由 `llm._record_usage()` 调——那里同步/流式共用，且只有拿到 usage 的
    成功调用才会走到。免费中转 / 玩家自带 Key 记 `cost=0` 的流水，方便玩家
    在「查看」里分清「哪些是我自己付的」。
    """
    qq = current_qq()
    if not qq:
        return None
    cfg = load_config()
    pp, pm = paid_target()
    is_paid = (str(provider) == pp and str(model) == pm)
    channel = ("paid" if is_paid else
               "custom" if str(provider) == "custom" else "free")
    ts = time.time()
    dt = _beijing_date(ts, cfg)
    peak = is_peak(ts, cfg)
    row = {
        "ts": ts,
        "date": dt.strftime("%Y-%m-%d"),
        "time": dt.strftime("%H:%M:%S"),
        "qq": qq,
        "session": str(tag or ""),
        "channel": channel,
        "provider": str(provider or ""),
        "model": str(model or ""),
        "peak": bool(peak),
        "hit": int(hit or 0), "miss": int(miss or 0), "out": int(out or 0),
        "cost": 0,
        "kind": str(kind or ""),
    }
    if is_paid:
        row["price"] = price_for(model, peak, cfg)
        row["cost"] = cost_micro(hit, miss, out, row["price"])
    with _LOCK:
        st = _load_unlocked()
        if row["cost"]:
            row["balance_after"] = _deduct_unlocked(st, qq, row["cost"], ts)
            _save_unlocked(st)
        else:
            row["balance_after"] = int(
                (st["accounts"].get(qq) or {}).get("balance") or 0)
    try:
        os.makedirs(_bill_dir(), exist_ok=True)
        with open(bill_path(row["date"]), "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except (OSError, TypeError, ValueError) as e:
        log.warning("计费流水写入失败：%s", e)
    return row


def read_calls(qq=None, days=7, limit=20000):
    """最近 N 天的逐笔流水，按时间倒序。qq=None = 所有人（查看台用）。"""
    cfg = load_config()
    today = _beijing_date(time.time(), cfg).date()
    # qq=""（游客）要明确返回空，不能当成「不限定用户」把全站流水倒出去
    if qq is not None and not str(qq).strip():
        return []
    want = {str(qq)} if qq is not None else None
    out = []
    for i in range(max(1, int(days or 7))):
        d = (today - timedelta(days=i)).strftime("%Y-%m-%d")
        try:
            with open(bill_path(d), encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if want and str(row.get("qq")) not in want:
                        continue
                    out.append(row)
        except OSError:
            continue
    out.sort(key=lambda r: r.get("ts") or 0, reverse=True)
    return out[:max(1, int(limit or 20000))]


def summary(qq, days=7):
    """玩家「查看」面板的数据：余额 + 汇总 + 每日横条 + 最近几笔。"""
    days = max(1, min(90, int(days or 7)))
    rows = read_calls(qq, days=days)
    total = {"calls": 0, "hit": 0, "miss": 0, "out": 0, "cost": 0,
             "free_calls": 0, "paid_calls": 0, "custom_calls": 0}
    by_day = {}
    for r in rows:
        total["calls"] += 1
        for k in ("hit", "miss", "out", "cost"):
            total[k] += int(r.get(k) or 0)
        ch = r.get("channel") or "free"
        total[ch + "_calls"] = total.get(ch + "_calls", 0) + 1
        slot = by_day.setdefault(r.get("date"),
                                 {"date": r.get("date"), "cost": 0,
                                  "hit": 0, "miss": 0, "out": 0,
                                  "calls": 0})
        slot["calls"] += 1
        for k in ("cost", "hit", "miss", "out"):
            slot[k] += int(r.get(k) or 0)
    cfg = load_config()
    today = _beijing_date(time.time(), cfg).date()
    daily = []
    for i in range(days):
        d = (today - timedelta(days=i)).strftime("%Y-%m-%d")
        daily.append(by_day.get(d, {"date": d, "cost": 0, "hit": 0,
                                    "miss": 0, "out": 0, "calls": 0}))
    bal = balance_of(qq)
    return {
        "qq": str(qq or ""),
        "registered": account(qq) is not None,
        "balance": bal,
        "balance_text": fmt_yuan(bal, 4),
        "days": days,
        "total": total,
        "daily": daily,
        "recent": rows[:20],
        "paid_model": "/".join(paid_target()),
        "peak_now": is_peak(cfg=cfg),
    }


def csv_text(rows):
    """按笔明细导出。带 BOM，Excel 直接双击不乱码。"""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["时间", "日期", "QQ", "会话", "渠道", "模型", "时段",
                "输入命中", "输入未命中", "输出", "金额(元)", "余额(元)",
                "调用类型"])
    names = {"free": "免费中转", "paid": "站主代充", "custom": "自带模型"}
    for r in rows:
        dt = r.get("date", "") + " " + r.get("time", "")
        w.writerow([
            dt.strip(), r.get("date", ""), r.get("qq", ""),
            r.get("session", ""), names.get(r.get("channel"), "其他"),
            r.get("model", ""), "高峰" if r.get("peak") else "闲时",
            r.get("hit", 0), r.get("miss", 0), r.get("out", 0),
            fmt_yuan(r.get("cost", 0), 6),
            fmt_yuan(r.get("balance_after", 0), 6),
            r.get("kind", ""),
        ])
    return "\ufeff" + buf.getvalue()
