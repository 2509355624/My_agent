# -*- coding: utf-8 -*-
"""把「开跑前水位（内存余）」和「这张的耗时」配起来，看慢图是不是 RAM 低造成的。

队列是全局串行的（本地通道一条线），所以每条「开跑前水位」后面最近的那条
「生图完成已发回」就是它对应的结果。配不上的一律丢掉。
"""
import io
import re
import statistics

LOG = r"D:\AI\agent_my_test\logs\qq_bot.log"

re_water = re.compile(
    r"^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2}),\d+ INFO image_jobs: 开跑前水位：显存余 ([\d.]+)GB，内存余 ([\d.]+)GB")
re_done = re.compile(
    r"^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2}),\d+ INFO image_jobs: 生图完成已发回 \S+ \S+：\d+/\d+ 张（编号 \S+，渠道 (\S+?)，seed \d+，耗时 ([\d.]+) 秒）")
re_to = re.compile(
    r"^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2}),\d+ WARNING image_jobs: 生图任务失败")

pairs = []          # (ram, secs, channel)
pending = None

with io.open(LOG, "r", encoding="utf-8", errors="replace") as fh:
    for line in fh:
        m = re_water.match(line)
        if m:
            pending = float(m.group(4))
            continue
        m = re_done.match(line)
        if m and pending is not None:
            pairs.append((pending, float(m.group(4)), m.group(3)))
            pending = None
            continue
        if re_to.match(line):
            pending = None      # 这张没成，水位作废

print("配对成功 %d 条\n" % len(pairs))

buckets = [(0, 3), (3, 4), (4, 5), (5, 6), (6, 8)]
print("%-14s %5s  %8s %8s %8s" % ("开跑前内存余", "张数", "均耗时", "中位", "最大"))
for lo, hi in buckets:
    sel = [p[1] for p in pairs if lo <= p[0] < hi]
    if not sel:
        continue
    print("%-14s %5d  %7.1fs %7.1fs %7.1fs" %
          ("[%g,%g)GB" % (lo, hi), len(sel),
           statistics.mean(sel), statistics.median(sel), max(sel)))

print()
print("== 只看 krea2 系 ==")
k = [p for p in pairs if p[2].startswith("krea2")]
print("n=%d  均 %.1fs  中位 %.1fs  最大 %.1fs" %
      (len(k), statistics.mean([p[1] for p in k]),
       statistics.median([p[1] for p in k]), max(p[1] for p in k)))
for lo, hi in buckets:
    sel = [p[1] for p in k if lo <= p[0] < hi]
    if sel:
        print("  [%g,%g)GB  n=%2d  中位 %6.1fs  最大 %6.1fs" %
              (lo, hi, len(sel), statistics.median(sel), max(sel)))

print()
print("== 只看非 krea2（silver/hd_3/qwen/…） ==")
o = [p for p in pairs if not p[2].startswith("krea2")]
print("n=%d  均 %.1fs  中位 %.1fs  最大 %.1fs" %
      (len(o), statistics.mean([p[1] for p in o]),
       statistics.median([p[1] for p in o]), max(p[1] for p in o)))
for lo, hi in buckets:
    sel = [p[1] for p in o if lo <= p[0] < hi]
    if sel:
        print("  [%g,%g)GB  n=%2d  中位 %6.1fs  最大 %6.1fs" %
              (lo, hi, len(sel), statistics.median(sel), max(sel)))
