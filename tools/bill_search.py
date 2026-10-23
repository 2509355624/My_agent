#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把「搜索」的账从混合流水里拆出来。

背景：`search_agent.search()` 直接调 `llm.call_llm`，没开 `usage.scope("search")`，
所以它的调用和生图提示词、人物卡、摘要压缩一样都记成 `kind=sync` + 会话 tag，
账单上看不出「搜索花了多少」。

拆法：search_agent 每轮都打日志（`[search] 第 N 轮调用` / `[search] N 轮结束`），
时间戳精度到秒；流水也有 `t: "HH:MM:SS"`。按 (日期, 秒) 做 ±2s 窗口的贪心配对，
配上就是一次搜索调用。

局限（用数据说话，不粉饰）：
- 同一秒里如果还有别的 sync 调用（正常对话/生图提示词），可能配错，
  但两边都记了 token 量级相近的概率不高。脚本会输出「歧义次数」。
- 日志只覆盖轮转保留的那几个文件（每个 5 MB），更早的日期配不上。

用法：
    python tools/bill_search.py
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

# 与 tools/bill.py 同口径：元 / 百万 token（命中, 未命中, 输出）
PRICES = {
    "deepseek": (0.02, 1.0, 4.0),
    "mimo": (0.02, 1.0, 2.0),
    "volc": (0.02, 1.0, 4.0),
    "doubao": (0.02, 1.0, 4.0),
    "ollama": (0.0, 0.0, 0.0),
    "scnet": (0.0, 0.0, 0.0),
    "scnet2": (0.0, 0.0, 0.0),
    "llama": (0.0, 0.0, 0.0),
}
DEFAULT_PRICE = (0.02, 1.0, 4.0)

# `[search] 第 3 轮调用 search_tags(银狼)，返回 812 字 [group_1]`  ← 一次工具轮
# `[search] 2 轮结束，资料 147 字 [group_1]`                      ← 收尾轮
# `[search] 第 2 轮调用失败：...` / `第 2 轮空回复`                ← 也是调用
RE_LINE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}) (\d{2}):(\d{2}):(\d{2}),\d+ .*?\[search\] "
    r"(?:第 (\d+) 轮(?:调用|调用失败|空回复)|(\d+) 轮结束|达到 (\d+) 轮上限)"
)
RE_TAG = re.compile(r"\[(group_[\w]+|private_[\w]+)\]")


def price_of(prov):
    return PRICES.get((prov or "").split(":")[0].strip().lower(), DEFAULT_PRICE)


def cost(prov, hit, miss, out):
    h, m, o = price_of(prov)
    return (hit * h + miss * m + out * o) / 1e6


def collect_search_marks():
    """从日志里提取搜索**轮次**的时间点。

    注意：`第 N 轮调用 xxx` 是**一行一个工具**，同一轮查 3 个词就有 3 行、
    但它们只对应 1 次 LLM 调用。所以按 (秒, 轮号) 去重，否则会把一次调用
    算成三次（第一版就高估了一倍多）。

    返回 [(日期时间, 轮标识)]。
    """
    marks = []
    seen = set()
    files = sorted(glob.glob(os.path.join(LOG_DIR, "qq_bot.log*")))
    for path in files:
        try:
            fh = open(path, encoding="utf-8", errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                if "[search]" not in line:
                    continue
                m = RE_LINE.match(line)
                if not m:
                    continue
                key = "%s %s:%s:%s" % (m.group(1), m.group(2), m.group(3), m.group(4))
                # 工具轮按轮号去重；收尾轮 / 到上限各算一次
                if m.group(5):
                    ident = "r%s" % m.group(5)
                elif m.group(6):
                    ident = "end"
                else:
                    ident = "cap"
                pair = (key, ident)
                if pair in seen:
                    continue
                seen.add(pair)
                t = RE_TAG.search(line)
                marks.append((key, t.group(1) if t else "-"))
    return marks


def collect_sync_calls():
    """流水里所有 kind=sync 且有 token 的调用 → {'date HH:MM:SS': [dict, ...]}"""
    calls = collections.defaultdict(list)
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
                if (r.get("kind") or "") != "sync":
                    continue
                hit = int(r.get("hit") or 0)
                miss = int(r.get("miss") or 0)
                out = int(r.get("out") or 0)
                if hit + miss + out <= 0:
                    continue
                key = "%s %s" % (r.get("date"), r.get("t"))
                calls[key].append({"prov": r.get("prov") or "?",
                                   "tag": r.get("tag") or "-",
                                   "hit": hit, "miss": miss, "out": out,
                                   "used": False})
    return calls


def _secs(key):
    """'2026-10-21 09:00:55' → 秒级整数，跨天也能算差。"""
    try:
        import time as _t
        return int(_t.mktime(_t.strptime(key, "%Y-%m-%d %H:%M:%S")))
    except Exception:
        return None


def main():
    marks = collect_search_marks()
    calls = collect_sync_calls()
    if not marks:
        print("⚠ 日志里没找到 [search] 埋点，无法拆分。")
        return 1

    # 建索引：秒 → key，便于 ±2s 窗口查找
    by_sec = {}
    for key, rows in calls.items():
        s = _secs(key)
        if s is not None:
            by_sec.setdefault(s, []).append(key)

    matched = collections.defaultdict(lambda: {"n": 0, "hit": 0, "miss": 0, "out": 0,
                                               "cost": 0.0})
    hits_last = 0
    ambiguous = 0
    unmatched = 0
    per_day = collections.defaultdict(lambda: {"n": 0, "cost": 0.0})

    for key, tag in sorted(marks):
        s = _secs(key)
        if s is None:
            continue
        day = key[:10]
        got = None
        # 先试同一秒，再 ±1s、±2s（日志在工具执行后才打，流水是调用返回时记）
        for delta in (0, -1, 1, -2, 2):
            for cand in by_sec.get(s + delta, []):
                for row in calls[cand]:
                    if not row["used"]:
                        if got is not None:
                            ambiguous += 1
                            break
                        got = row
            if got is not None:
                break
        if got is None:
            unmatched += 1
            continue
        got["used"] = True
        a = matched[got["tag"]]
        a["n"] += 1
        a["hit"] += got["hit"]
        a["miss"] += got["miss"]
        a["out"] += got["out"]
        c = cost(got["prov"], got["hit"], got["miss"], got["out"])
        a["cost"] += c
        per_day[day]["n"] += 1
        per_day[day]["cost"] += c
        if got["hit"] > 0:
            hits_last += 1

    tn = sum(a["n"] for a in matched.values())
    th = sum(a["hit"] for a in matched.values())
    tm = sum(a["miss"] for a in matched.values())
    to = sum(a["out"] for a in matched.values())
    tcost = sum(a["cost"] for a in matched.values())

    print("=== 搜索（search_agent）用量拆分 ===")
    print("日志覆盖的搜索轮次标记：%d 个" % len(marks))
    print("配上流水的：%d 次｜配不上：%d 次｜歧义：%d 次"
          % (tn, unmatched, ambiguous))
    print()
    print("调用次数  %d" % tn)
    print("命中tok   %.2fM" % (th / 1e6))
    print("未命中tok %.1fk" % (tm / 1e3))
    print("输出tok   %.1fk" % (to / 1e3))
    print("费用      ¥%.4f" % tcost)
    print()
    print("按会话：")
    for tag, a in sorted(matched.items(), key=lambda kv: -kv[1]["cost"]):
        print("  %-22s %5d 次  miss %8.1fk  out %6.1fk  ¥%.4f"
              % (tag, a["n"], a["miss"] / 1e3, a["out"] / 1e3, a["cost"]))
    print()
    print("按天：")
    for day, a in sorted(per_day.items()):
        print("  %s  %4d 次  ¥%.4f" % (day, a["n"], a["cost"]))

    # 占总盘
    total = 0.0
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
                if r.get("ok", True) is False:
                    continue
                total += cost(r.get("prov"), int(r.get("hit") or 0),
                              int(r.get("miss") or 0), int(r.get("out") or 0))
    print()
    print("总盘 ¥%.2f → 搜索占 %.1f%%" % (total, 100.0 * tcost / max(total, 1e-9)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
