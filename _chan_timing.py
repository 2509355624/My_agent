# -*- coding: utf-8 -*-
r"""按渠道统计出图耗时（中位/均值/最长/样本数），并给出「慢于 N 秒」的比例。

用法：D:/AI/confyui_env/Scripts/python.exe _chan_timing.py 2026-10-10 [天数]
不传日期时统计最近 N 天（默认 1 = 只看今天）。
"""
import re
import sys
from datetime import datetime, timedelta
from collections import defaultdict

TS = re.compile(r'^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d+')
DONE = re.compile(r'渠道 (\S+?)，seed \d+，耗时 ([\d.]+) 秒')

day = sys.argv[1] if len(sys.argv) > 1 else datetime.now().strftime('%Y-%m-%d')
days = int(sys.argv[2]) if len(sys.argv) > 2 else 1
since = datetime.strptime(day, '%Y-%m-%d') - timedelta(days=days - 1)

by = defaultdict(list)
with open('logs/qq_bot.log', encoding='utf-8', errors='replace') as f:
    for line in f:
        m = TS.match(line)
        if not m:
            continue
        t = datetime.strptime(m.group(1), '%Y-%m-%d %H:%M:%S')
        if t < since:
            continue
        m2 = DONE.search(line)
        if m2:
            by[m2.group(1)].append(float(m2.group(2)))

print('统计窗口：%s 起 %d 天' % (since.strftime('%Y-%m-%d'), days))
print()
print('%-18s %5s %8s %8s %8s %8s %8s' % ('渠道', 'n', '中位', '均值', 'P90', '最长', '>180s'))
print('-' * 70)
for ch in sorted(by, key=lambda c: -len(by[c])):
    v = sorted(by[ch])
    n = len(v)
    p90 = v[min(n - 1, int(n * 0.9))]
    over = sum(1 for x in v if x > 180)
    print('%-18s %5d %7.1fs %7.1fs %7.1fs %7.1fs %5d(%.0f%%)'
          % (ch, n, v[n // 2], sum(v) / n, p90, v[-1], over, 100.0 * over / n))

# 分组
groups = {
    'krea2 全系': lambda c: c.startswith('krea2'),
    'qwen 全系': lambda c: c.startswith('qwen'),
    'anima (hd_3_*)': lambda c: c.startswith('hd_3_'),
    'silver 系': lambda c: c.startswith('silver'),
}
print()
print('== 分组 ==')
for g, pred in groups.items():
    v = sorted(x for c in by if pred(c) for x in by[c])
    if not v:
        continue
    n = len(v)
    over = sum(1 for x in v if x > 180)
    print('%-18s n=%-5d 中位 %6.1fs  均值 %6.1fs  P90 %6.1fs  最长 %6.1fs  >180s %d(%.0f%%)'
          % (g, n, v[n // 2], sum(v) / n, v[min(n - 1, int(n * 0.9))], v[-1],
             over, 100.0 * over / n))
