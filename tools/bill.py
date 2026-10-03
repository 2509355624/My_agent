#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""用量对账：把 usage/calls-<date>.jsonl 汇总成「不同模型花了多少 + 当天效果」。

用法：
    python tools/bill.py                          # 最近有流水的一天
    python tools/bill.py 2026-10-04               # 指定一天
    python tools/bill.py 2026-10-03 2026-10-04    # 跨天合并（跑一整天会跨日历天）
    python tools/bill.py --all                    # 所有有流水的日期合并

流水由 app/usage.py 在每次调用后追加一行（一行 = 一次真实调用，含失败）。
本脚本只读不改，不需要启动 agent。

单价表写死在下面；哪家调价了直接改 PRICES。单位：元 / 百万 token。
"""

import json
import os
import sys
import time

try:                                     # Windows 控制台默认 GBK，会打不出中文
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
USAGE_DIR = os.path.join(BASE, "usage", )

# (缓存命中, 输入未命中, 输出)
PRICES = {
    "deepseek": (0.02, 1.0, 4.0),        # 官方闲时价（2026-10-03 账单反推）
    "mimo": (0.02, 1.0, 2.0),            # 小米 MiMo
    "volc": (0.02, 1.0, 4.0),            # 火山方舟（免费额度已耗尽，按同口径折算）
    "doubao": (0.02, 1.0, 4.0),
    "ollama": (0.0, 0.0, 0.0),           # 本地，不花钱
    "scnet": (0.0, 0.0, 0.0),
}
DEFAULT_PRICE = (0.02, 1.0, 4.0)


def w(s):
    """显示宽度：中文/全角算 2 格。"""
    return sum(2 if ord(c) > 0x2E7F else 1 for c in str(s))


def pad(s, n):
    s = str(s)
    return s + " " * max(0, n - w(s))


def human(n):
    n = float(n or 0)
    if n >= 1e6:
        return "%.2fM" % (n / 1e6)
    if n >= 1e3:
        return "%.1fk" % (n / 1e3)
    return "%.0f" % n


def price_of(prov):
    return PRICES.get((prov or "").split(":")[0].strip().lower(),
                      DEFAULT_PRICE)


def dates_with_calls():
    try:
        names = os.listdir(USAGE_DIR)
    except OSError:
        return []
    return sorted(n[6:-6] for n in names
                  if n.startswith("calls-") and n.endswith(".jsonl"))


def read_calls(date):
    path = os.path.join(USAGE_DIR, "calls-%s.jsonl" % date)
    rows, bad = [], 0
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    bad += 1
    except OSError:
        return [], 0, path, False
    return rows, bad, path, True


def add(store, key, hit, miss, out):
    s = store.setdefault(key, {"calls": 0, "hit": 0, "miss": 0, "out": 0})
    s["calls"] += 1
    s["hit"] += hit
    s["miss"] += miss
    s["out"] += out


def cost_of(prov, s):
    h, m, o = price_of(prov)
    return (s["hit"] * h + s["miss"] * m + s["out"] * o) / 1e6


def print_table(title, store, first_col):
    print("\n【%s】" % title)
    print(pad(first_col, 34) + pad("调用", 7) + pad("命中tok", 10)
          + pad("未命中tok", 11) + pad("输出tok", 10) + "费用")
    print("-" * 78)
    tot = {"calls": 0, "hit": 0, "miss": 0, "out": 0}
    money = 0.0
    for key, s in sorted(store.items(),
                         key=lambda kv: -cost_of(kv[0].split("/")[0], kv[1])):
        prov = key.split("/")[0]
        c = cost_of(prov, s)
        money += c
        for k in tot:
            tot[k] += s[k]
        print(pad(key, 34) + pad(s["calls"], 7) + pad(human(s["hit"]), 10)
              + pad(human(s["miss"]), 11) + pad(human(s["out"]), 10)
              + ("¥%.4f" % c))
    print("-" * 78)
    print(pad("合计", 34) + pad(tot["calls"], 7) + pad(human(tot["hit"]), 10)
          + pad(human(tot["miss"]), 11) + pad(human(tot["out"]), 10)
          + ("¥%.4f" % money))
    return money


def main(argv):
    args = [a for a in argv[1:] if not a.startswith("-")]
    all_dates = dates_with_calls()
    if "--all" in argv:
        dates = all_dates
    elif args:
        dates = args
    else:
        dates = all_dates[-1:] or [time.strftime("%Y-%m-%d")]

    print("=== 用量对账 %s ===" % ("、".join(dates) if dates else "(无)"))
    if not all_dates:
        print("⚠ usage/ 下还没有任何 calls-*.jsonl —— 流水是 2026-10-03 晚才"
              "启用的，之前的日期没有模型维度的数据。")
        return 1

    model, tag_agg, fails = {}, {}, {}
    turns = tool_turns = empties = 0
    tools = {}
    lat = []
    missing = []

    for d in dates:
        rows, bad, path, exists = read_calls(d)
        if not exists:
            missing.append(d)
            continue
        for r in rows:
            kind = r.get("kind")
            if kind == "turn":                    # 一轮对话（含调没调工具）
                turns += 1
                tl = r.get("tools") or []
                if tl:
                    tool_turns += 1
                    for t in tl:
                        tools[t] = tools.get(t, 0) + 1
                continue
            if kind == "empty":                   # 空回复
                empties += 1
                continue
            prov = r.get("prov") or "?"
            model_name = r.get("model") or "-"
            key = "%s/%s" % (prov, model_name)
            if not r.get("ok", True):             # 失败/降级：也占一次调用
                f = fails.setdefault(key, {"n": 0, "err": r.get("err") or ""})
                f["n"] += 1
                continue
            hit = int(r.get("hit") or 0)
            miss = int(r.get("miss") or 0)
            out = int(r.get("out") or 0)
            if r.get("ms"):
                lat.append(int(r["ms"]) / 1000.0)
            if hit + miss + out <= 0:
                continue
            add(model, key, hit, miss, out)
            add(tag_agg, r.get("tag") or "other", hit, miss, out)

    if missing:
        print("⚠ 缺流水的日期：%s" % "、".join(missing))
    if not model:
        print("这段时间没有有效调用记录。")
        return 0

    print("\n流水文件：%s" % os.path.join("usage", "calls-<date>.jsonl"))
    print_table("按模型", model, "模型")
    print_table("按会话", tag_agg, "归属")

    if fails:
        n = sum(f["n"] for f in fails.values())
        print("\n【失败 / 降级】共 %d 次（也占账单的请求数）" % n)
        for key, f in sorted(fails.items(), key=lambda kv: -kv[1]["n"]):
            print("  %s %d 次   %s" % (pad(key, 34), f["n"], f["err"][:70]))

    print("\n【效果】")
    print("  对话轮次 %d ｜ 调了工具的 %d (%.1f%%)"
          % (turns, tool_turns, 100.0 * tool_turns / max(turns, 1)))
    if tools:
        print("  工具分布：%s"
              % "，".join("%s %d" % (k, v) for k, v in
                          sorted(tools.items(), key=lambda kv: -kv[1])))
    print("  空回复 %d 次" % empties)
    if lat:
        lat.sort()
        print("  同步调用延迟 p50 %.1fs / p90 %.1fs（%d 次）"
              % (lat[len(lat) // 2], lat[int(len(lat) * 0.9)], len(lat)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
