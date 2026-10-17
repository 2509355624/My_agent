# -*- coding: utf-8 -*-
"""一次性诊断：统计每次超时之前，距上一次 ComfyUI 重启之间跑了几张图。

用户提议「每生成 2 张就重启 ComfyUI」。要判断这条有没有用，先看超时发生时
ComfyUI 已经连着跑了多少张（= 内存累积了多少）。
"""
import io
import re
import sys

LOG = r"D:\AI\agent_my_test\logs\qq_bot.log"
DAY = "2026-10-09"

re_restart = re.compile(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ INFO image_jobs: ComfyUI 已重启完成")
re_done = re.compile(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ INFO image_jobs: 生图完成已发回")
re_to = re.compile(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ WARNING image_jobs: 生图任务失败 (private|group) (\d+)：生成超时")
re_sw = re.compile(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ INFO image_jobs: 渠道切换 (\S+) → (\S+)")
re_same = re.compile(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ INFO image_jobs: 同渠道 (\S+) 已连画")
re_ram = re.compile(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ WARNING image_jobs: 系统可用内存只剩 ([\d.]+)GB")

events = []
with io.open(LOG, "r", encoding="utf-8", errors="replace") as fh:
    for line in fh:
        if not line.startswith(DAY):
            continue
        t = line[:19]
        m = re_restart.match(line)
        if m:
            events.append((t, "RESTART", ""))
            continue
        m = re_to.match(line)
        if m:
            events.append((t, "TIMEOUT", "%s %s" % (m.group(2), m.group(3))))
            continue
        m = re_done.match(line)
        if m:
            events.append((t, "DONE", ""))
            continue
        m = re_sw.match(line)
        if m:
            events.append((t, "SWITCH", "%s -> %s" % (m.group(2), m.group(3))))
            continue
        m = re_same.match(line)
        if m:
            events.append((t, "SAME2", m.group(2)))
            continue
        m = re_ram.match(line)
        if m:
            events.append((t, "RAMWARN", m.group(2) + "GB"))
            continue

# 超时路径自己会先重启一次（"ComfyUI 退场了…重启它"），所以不能直接用「上一次
# RESTART」当基准。改成：对每个超时，回看**最早**能作为基准的那次重启——
# 即「该次重启 + 180s < 超时时刻」里最晚的一次。180s 正好是单张图的超时上限，
# 所以落在 (T-180, T] 里的重启一定是被这次超时自己触发的。
def hhmmss(t):
    return sum(int(x) * m for x, m in zip(t[11:].split(":"), (3600, 60, 1)))


restarts = [hhmmss(e[0]) for e in events if e[1] == "RESTART"]
timeouts = [e for e in events if e[1] == "TIMEOUT"]
for t, kind, extra in timeouts:
    tt = hhmmss(t)
    base = [r for r in restarts if r + 180 < tt]
    base = max(base) if base else 0
    done = sum(1 for e in events if e[1] == "DONE" and base < hhmmss(e[0]) < tt)
    sw = sum(1 for e in events if e[1] == "SWITCH" and base < hhmmss(e[0]) < tt)
    print("TIMEOUT %s  %-22s  距上次重启: %d 张完成 / %d 次渠道切换" %
          (t, extra, done, sw))

print()
print("总重启次数:", sum(1 for e in events if e[1] == "RESTART"))
print("总完成张数:", sum(1 for e in events if e[1] == "DONE"))
print("总渠道切换:", sum(1 for e in events if e[1] == "SWITCH"))
print("同渠道2张触发:", sum(1 for e in events if e[1] == "SAME2"))
print("内存水位重启:", sum(1 for e in events if e[1] == "RAMWARN"))
