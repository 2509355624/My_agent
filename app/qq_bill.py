"""QQ 机器人按会话账单（2026-11-10）。

## 这本账是什么

站主自付的那个官方 DeepSeek key，同时被网页扮演和 QQ 机器人（生图提示词、
接话判断、识图、聊天回复）使用。网页扮演那部分已经在 `persona_billing`
里按 QQ 代扣；QQ 侧的钱站主自己掏，以前只有 token 数、没有金额，也没按
「谁用的」摊开。这里补上：**只读调用流水，按会话折算金额，出一本账单**。

## 三条边界

1. **只出账，不扣钱**。不写余额、不改存档、不进聊天链路。改了价目表可以
   重算历史，跟正在跑的服务完全无关。
2. **只算官方 key 的调用**。provider/model 必须等于 `paid_target()`；免费
   中转和玩家自带 Key 本来就不产生这本站主账单，直接跳过。
3. **口径与官方账单一致**。单价、高峰/闲时复用 `config/persona_billing.json`
   和 `persona_billing.is_peak()`，不另建一套；时段按**这一笔流水的时间**判，
   不是按「现在」，否则隔天翻旧账会把闲时算成高峰。

## 归口

`qq_bot` 在轮次外层设 `usage.scope(session_key)`，所以每行流水自带
`private_<QQ>` / `group_<群号>`。`web_*` / `guest_*` 是网页扮演，不算 QQ 侧；
`other` / `vision` / `interject` 这类没有会话归属的单列，别硬塞给某个人——
宁可让差额显形，也不要把钱记错人。
"""

import csv
import io
import json
import os
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from app import persona_billing
from app import usage as usage_stats

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_KEYS = ("calls", "hit", "miss", "out", "cost")


def _tz(cfg):
    try:
        return ZoneInfo((cfg.get("peak") or {}).get("timezone") or "Asia/Shanghai")
    except Exception:                                    # noqa: BLE001
        return ZoneInfo("Asia/Shanghai")


def _epoch(date, hhmmss, tz):
    """流水里只有本地时间字符串，补成 epoch 供 is_peak() 判时段。"""
    try:
        return datetime.strptime("%s %s" % (date, hhmmss),
                                 "%Y-%m-%d %H:%M:%S").replace(tzinfo=tz).timestamp()
    except (ValueError, TypeError):
        return None


def _bucket():
    return {k: 0 for k in _KEYS}


def _add(b, hit, miss, out, cost):
    b["calls"] += 1
    b["hit"] += hit
    b["miss"] += miss
    b["out"] += out
    b["cost"] += cost


def _finish(b):
    b["cost_text"] = persona_billing.fmt_yuan(b["cost"], 6)
    return b


def today(cfg=None):
    cfg = cfg or persona_billing.load_config()
    return datetime.now(_tz(cfg)).strftime("%Y-%m-%d")


def dates():
    """有调用流水的日期（倒序）。只读目录，不碰任何数据。"""
    try:
        d = os.path.dirname(usage_stats.calls_path())
        return sorted((f[6:-6] for f in os.listdir(d)
                       if f.startswith("calls-") and f.endswith(".jsonl")
                       and _DATE_RE.match(f[6:-6])), reverse=True)
    except OSError:
        return []


def daily(date=None):
    """某一天的账单。date 为空 = 今天（配置时区）。"""
    cfg = persona_billing.load_config()
    pp, pm = persona_billing.paid_target()
    date = str(date or "").strip() or today(cfg)
    if not _DATE_RE.match(date):
        raise ValueError("date 格式应为 YYYY-MM-DD")
    tz = _tz(cfg)
    sessions, other, web, local = {}, _bucket(), _bucket(), _bucket()
    try:
        with open(usage_stats.calls_path(date), encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        lines = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if not isinstance(r, dict) or r.get("ok") is False:
            continue
        if str(r.get("prov") or "") != pp or str(r.get("model") or "") != pm:
            continue
        try:
            hit = int(r.get("hit") or 0)
            miss = int(r.get("miss") or 0)
            out = int(r.get("out") or 0)
        except (TypeError, ValueError):
            continue
        if hit + miss + out <= 0:
            continue
        ts = _epoch(str(r.get("date") or date), str(r.get("t") or "00:00:00"), tz)
        peak = persona_billing.is_peak(ts, cfg) if ts is not None else False
        price = persona_billing.price_for(pm, peak, cfg)
        cost = persona_billing.cost_micro(hit, miss, out, price)
        tag = str(r.get("tag") or "other")
        _add(local, hit, miss, out, cost)
        if tag.startswith(("private_", "group_")):
            _add(sessions.setdefault(tag, _bucket()), hit, miss, out, cost)
        elif tag.startswith(("web_", "guest_")):
            _add(web, hit, miss, out, cost)
        else:
            _add(other, hit, miss, out, cost)

    rows = []
    for tag, b in sessions.items():
        ident = tag.split("_", 1)[1] if "_" in tag else ""
        rows.append(_finish(dict(
            b, tag=tag, id=ident,
            kind="私聊" if tag.startswith("private_") else "群聊")))
    rows.sort(key=lambda r: (-r["cost"], r["tag"]))
    total = _bucket()
    for b in sessions.values():
        for k in _KEYS:
            total[k] += b[k]
    return {
        "date": date,
        # 前端日期下拉直接用后端时区算的「今天」，别用浏览器 UTC 的今天
        "dates": sorted(set(dates() + [date, today(cfg)]), reverse=True),
        "paid_model": "%s/%s" % (pp, pm),
        "rows": rows,
        "total": _finish(total),
        "other": _finish(other),
        "web": _finish(web),
        "local": _finish(local),
    }


def csv_text(data):
    """按会话明细 + 两行汇总。带 BOM，Excel 直接双击不乱码。"""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["日期", "会话", "类型", "QQ / 群号", "调用数",
                "命中 tokens", "未命中 tokens", "输出 tokens", "金额(元)"])
    for r in data.get("rows") or []:
        w.writerow([data["date"], r["tag"], r["kind"], r["id"],
                    r["calls"], r["hit"], r["miss"], r["out"],
                    persona_billing.fmt_yuan(r["cost"], 6)])
    for label, key in (("合计（QQ 已归属）", "total"),
                       ("无归属（后台隐形调用）", "other")):
        b = data.get(key) or {}
        w.writerow([data["date"], label, "", "", b.get("calls", 0),
                    b.get("hit", 0), b.get("miss", 0), b.get("out", 0),
                    persona_billing.fmt_yuan(b.get("cost", 0), 6)])
    return "\ufeff" + buf.getvalue()
