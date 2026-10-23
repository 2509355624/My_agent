#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把「other」这坨无归属的调用按功能拆开。

`other` 是 usage 的兜底 tag：网页端（Flask 线程）和后台任务没有 QQ 那层
`usage.scope(session_key)`，于是都记成 other。从账面上看它是第二大开销
（¥11 / 2893 次），但完全不知道是什么功能。

拆法：`logs/agent.log*` 里 werkzeug 会记每个 HTTP 请求，LLM 流水有秒级时间戳。
对每条 other 调用，往前找 10 秒内最近的一个 `POST /api/...`，就认为它属于
那次操作。

局限：
- 网页会轮询 GET，只认 POST（真正触发工作的是 POST）。
- 一个 POST 内部可能连发多次 LLM（比如编剧写一页要 3 次），都会归到同一个。
- 日志轮转只保留 4 个文件，更早的日期归不上 → 单独统计「未归类」。

用法：
    python tools/bill_other.py
"""

import collections
import glob
import json
import os
import re
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
USAGE_DIR = os.path.join(BASE, "usage")
LOG_DIR = os.path.join(BASE, "logs")

PRICES = {
    "deepseek": (0.02, 1.0, 4.0), "mimo": (0.02, 1.0, 2.0),
    "volc": (0.02, 1.0, 4.0), "doubao": (0.02, 1.0, 4.0),
    "ollama": (0.0, 0.0, 0.0), "scnet": (0.0, 0.0, 0.0),
    "scnet2": (0.0, 0.0, 0.0), "llama": (0.0, 0.0, 0.0),
}
DEFAULT_PRICE = (0.02, 1.0, 4.0)

RE_HTTP = re.compile(
    r"^(\d{4}-\d{2}-\d{2}) (\d{2}):(\d{2}):(\d{2}),\d+ .*?\"(POST) (/[^ \"]+)")
RE_WINDOW = 10          # 秒：POST 到 LLM 返回的间隔


def price_of(prov):
    return PRICES.get((prov or "").split(":")[0].strip().lower(), DEFAULT_PRICE)


def cost(prov, hit, miss, out):
    h, m, o = price_of(prov)
    return (hit * h + miss * m + out * o) / 1e6


def ts_of(date, hms):
    import time as _t
    try:
        return int(_t.mktime(_t.strptime("%s %s" % (date, hms), "%Y-%m-%d %H:%M:%S")))
    except Exception:
        return None


def collect_posts():
    """[(秒, 'POST /api/xxx')]，按时间升序。"""
    out = []
    for path in sorted(glob.glob(os.path.join(LOG_DIR, "agent.log*"))):
        try:
            fh = open(path, encoding="utf-8", errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                if "POST /api" not in line:
                    continue
                m = RE_HTTP.match(line)
                if not m:
                    continue
                s = ts_of(m.group(1), "%s:%s:%s" % (m.group(2), m.group(3), m.group(4)))
                if s is not None:
                    out.append((s, m.group(6).split("?")[0]))
    out.sort()
    return out


def main():
    posts = collect_posts()
    if not posts:
        print("⚠ logs/agent.log* 里没有 POST /api 记录，无法归类。")
        return 1
    post_secs = [p[0] for p in posts]

    import bisect
    agg = collections.defaultdict(lambda: {"n": 0, "hit": 0, "miss": 0, "out": 0,
                                           "cost": 0.0})
    rows = 0
    for path in sorted(glob.glob(os.path.join(USAGE_DIR, "calls-*.jsonl"))):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if (r.get("tag") or "") != "other":
                    continue
                if (r.get("kind") or "") != "sync":
                    continue
                hit = int(r.get("hit") or 0)
                miss = int(r.get("miss") or 0)
                out = int(r.get("out") or 0)
                if hit + miss + out <= 0:
                    continue
                rows += 1
                s = ts_of(r.get("date"), r.get("t") or "")
                label = "（未归类：日志已轮转）"
                if s is not None:
                    i = bisect.bisect_right(post_secs, s) - 1
                    if i >= 0 and s - post_secs[i] <= RE_WINDOW:
                        label = posts[i][1]
                a = agg[label]
                a["n"] += 1
                a["hit"] += hit
                a["miss"] += miss
                a["out"] += out
                a["cost"] += cost(r.get("prov"), hit, miss, out)

    print("=== other（网页端 / 后台任务）按功能拆分 ===")
    print("总调用 %d 次\n" % rows)
    print("%-34s %6s %9s %9s  %8s" % ("触发接口", "调用", "命中tok", "未命中tok", "费用"))
    print("-" * 72)
    for label, a in sorted(agg.items(), key=lambda kv: -kv[1]["cost"]):
        print("%-34s %6d %8.2fM %8.1fk  ¥%7.3f"
              % (label[:34], a["n"], a["hit"] / 1e6, a["miss"] / 1e3, a["cost"]))
    print("-" * 72)
    print("%-34s %6d %8.2fM %8.1fk  ¥%7.3f"
          % ("合计", rows,
             sum(a["hit"] for a in agg.values()) / 1e6,
             sum(a["miss"] for a in agg.values()) / 1e3,
             sum(a["cost"] for a in agg.values())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
