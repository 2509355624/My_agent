# -*- coding: utf-8 -*-
r"""出图慢/超时的取证工具（2026-10-10 建）。

用法：
    D:/AI/confyui_env/Scripts/python.exe _log_forensics.py 2026-10-10

一次打印四块：
  1. 重启 / 释放 / 完成 的次数与重启触发原因分布
  2. 重启停机时长（合计、中位、最长）
  3. 每次超时前 8 分钟的事件回放（看它是在什么状态下开跑的）
  4. 出图耗时 vs 距上次重启/释放多久（判断重启是帮忙还是拖累）
  5. 相邻两次出图的间隔（>60s 就是队列被堵住的时刻）

⚠️ 带反斜杠的脚本必须落成 .py 再跑 —— MSYS 的 heredoc 会把 `\d` 吃成 `/d`。
"""
import re
import sys
from datetime import datetime

TS = re.compile(r'^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d+')
DONE = re.compile(r'渠道 (\S+?)，seed \d+，耗时 ([\d.]+) 秒')
DUR = re.compile(r'已重启完成，耗时 (\d+) 秒')
WATER = re.compile(r'开跑前水位：显存余 ([\d.]+)GB，内存余 ([\d.]+)GB')

RULES = [
    ('TIMEOUT',         '画超时了'),
    ('RESTART_TRIGGER', '已连画 2 张'),
    ('RESTART_DONE',    '已重启完成'),
    ('RESTART_WAIT',    '正在重启'),
    ('RELEASE',         '渠道切换'),
    ('WATERLINE',       '开跑前水位'),
    ('IDLE',            'ComfyUI 已空闲'),
    ('ENQUEUE',         '直达入队'),
]

day = sys.argv[1] if len(sys.argv) > 1 else datetime.now().strftime('%Y-%m-%d')
events, dones = [], []
with open('logs/qq_bot.log', encoding='utf-8', errors='replace') as f:
    for line in f:
        m = TS.match(line)
        if not m:
            continue
        t = datetime.strptime(m.group(1), '%Y-%m-%d %H:%M:%S')
        if t.strftime('%Y-%m-%d') != day:
            continue
        line = line.rstrip()
        for kind, needle in RULES:
            if needle in line:
                events.append((t, kind, line))
                break
        m2 = DONE.search(line)
        if m2:
            dones.append((t, m2.group(1), float(m2.group(2))))

restarts = [t for t, k, _ in events if k == 'RESTART_DONE']
releases = [t for t, k, _ in events if k == 'RELEASE']
timeouts = [t for t, k, _ in events if k == 'TIMEOUT']

print('### 1. 当天概览')
for kind, _ in RULES:
    print('  %-16s %d' % (kind, sum(1 for e in events if e[1] == kind)))
print()

durs = [int(DUR.search(l).group(1)) for _, k, l in events
        if k == 'RESTART_DONE' and DUR.search(l)]
if durs:
    print('### 2. 重启停机')
    print('  %d 次，合计 %d 秒（%.1f 分钟），中位 %d 秒，最长 %d 秒'
          % (len(durs), sum(durs), sum(durs) / 60.0,
             sorted(durs)[len(durs) // 2], max(durs)))
    print()

print('### 3. 每次超时前 8 分钟的事件回放')
for i, (t, k, _) in enumerate(events):
    if k != 'TIMEOUT':
        continue
    print('-' * 74)
    print('超时 @ %s' % t)
    for bt, bk, bl in events[:i]:
        gap = (t - bt).total_seconds()
        if gap <= 480:
            print('   -%4ds  %-16s %s' % (gap, bk, bl[24:136]))
print()

print('### 4. 出图耗时 vs 距上次重启多久')
buckets = {'<60s': [], '60~180s': [], '180~600s': [], '>600s': []}
for t, ch, sec in dones:
    prev = [r for r in restarts if r <= t]
    if not prev:
        continue
    g = (t - prev[-1]).total_seconds()
    key = '<60s' if g < 60 else '60~180s' if g < 180 else '180~600s' if g < 600 else '>600s'
    buckets[key].append(sec)
for k in ('<60s', '60~180s', '180~600s', '>600s'):
    v = buckets[k]
    if not v:
        print('  %-10s 无样本' % k)
        continue
    v.sort()
    print('  %-10s n=%-4d 中位 %6.1fs  均值 %6.1fs  最长 %6.1fs'
          % (k, len(v), v[len(v) // 2], sum(v) / len(v), v[-1]))
print()

print('### 5. 相邻两次出图的间隔 Top 10')
gaps = sorted(((dones[i][0] - dones[i - 1][0]).total_seconds(), dones[i - 1], dones[i])
              for i in range(1, len(dones)))[::-1]
for g, a, b in gaps[:10]:
    n = len([r for r in restarts if a[0] <= r <= b[0]])
    print('  %6.0fs  %s(%s) → %s(%s)   期间重启 %d 次'
          % (g, a[0].strftime('%H:%M:%S'), a[1], b[0].strftime('%H:%M:%S'), b[1], n))

print()
wl = [WATER.search(l) for _, _, l in events if WATER.search(l)]
low = [m for m in wl if float(m.group(2)) < 3.0]
print('### 6. 开跑前水位')
print('  样本 %d 条，开跑时内存余 <3.0GB 的 %d 条（%.0f%%）'
      % (len(wl), len(low), 100.0 * len(low) / max(len(wl), 1)))
