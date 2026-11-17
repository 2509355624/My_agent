"""看门狗配置：静默判据、重启节奏、ComfyUI 分支总闸。

只服务 app/watchdog.py；QQ 接入与通知配置在 qq.py。
"""

import os

# ── 看门狗的「静默判据」（假在线）──────────────────────────────
# 看门狗的三态探针**探不出「假在线」**：:3000 接口全好、登录态在，只有
# 「腾讯 → QQ 客户端」的下行推送死了（2026-09-29 22:36~22:53 实测）。能观测到的
# 唯一信号是「本该到的消息没到」。所以改看 qq_bot 的静默时长（状态快照里的
# last_activity_ago），超过 WATCHDOG_SILENCE_SECONDS 一条都没有 → 重启当探针。
#
# ⚠️ 重启当探针为什么成立（用户 2026-09-29 的判断，已被日志证实）：
#   自动登录 → 会话本来是好的，就是「群里本来就安静」→ 静默放过、不通知；
#   要扫码   → 会话早被腾讯作废，这正是假在线/掉线的本质 → 推二维码。
# 所以误报的代价只是一次静默重启，真故障却能第一时间暴露。
WATCHDOG_SILENCE_SECONDS = float(os.getenv("WATCHDOG_SILENCE_SECONDS", "1200"))

# 连续静默时两次重启之间的**起步**间隔（秒）。每静默重启一次就翻倍，封顶
# WATCHDOG_SILENCE_MAX_GAP；一旦收到消息立刻清零。
# ⚠️ 没有它就会出事：静默重启成功后 qq_bot 一起重启、静默计时归零，整夜没人
# 说话就变成每 WATCHDOG_SILENCE_SECONDS 杀一次 QQ —— 反过来招风控。
WATCHDOG_SILENCE_COOLDOWN = float(os.getenv("WATCHDOG_SILENCE_COOLDOWN", "3600"))
WATCHDOG_SILENCE_MAX_GAP = float(os.getenv("WATCHDOG_SILENCE_MAX_GAP", "14400"))

# ⚠️ 静默时**要不要真的重启**。默认 0 = **只记日志、不重启**。
#
# 原来是 1（重启当探针），09-30 用户拍板改成默认关。原因是一天 32 次实测：
#
#   静默重启 → 一键启动全部 force → taskkill /IM QQ.exe /F → -q 快速登录
#     → 腾讯判定「刚登过又登」= 异常登录 → 作废会话
#     → 真掉线（tag=下线通知 / 你的账号当前登录已失效）
#     → 又没人说话 → 又静默 → 又重启 …… 死循环
#
# 09-30 硬证据：08:57:50 静默重启（08:57:59 还报过「已有 QQ 适配层在运行」），
# 08:58:55 重连成功，**09:00:57 就掉了线**——间隔 3 分钟。05:37 那次更直接：
# 重启后看门狗自己因为锁冲突退出（05:37:18），机器人反而活下来了。
#
# 所以「假在线」很可能是这个循环**制造**出来的，不是根因。先关掉静默重启，
# 用一天观察真实掉线频率：明显下降 = 推断成立；照旧 1 小时一次 = 真是风控。
# 想恢复老行为把 .env 里设 WATCHDOG_SILENCE_RESTART=1（改完要重启看门狗）。
WATCHDOG_SILENCE_RESTART = os.getenv("WATCHDOG_SILENCE_RESTART", "0") not in ("0", "false", "False", "")

# ── ComfyUI 看门狗分支总闸（2026-09-30）──────────────────
# ⚠️ 默认 0 = 整个 ComfyUI 分支不动作（只探活不重启）。
# 09-30 凌晨它过度重启：05:20/05:51/06:26 各触发一轮（含 1800 秒退避），
# 因为 ComfyUI 是**按需启动、常常故意不开**，看门狗却当成「崩了」。
# 现在默认开启；`comfy_seen` 闩仍保留，只在 ComfyUI 曾经在线后持续失联时执行恢复。
WATCHDOG_COMFY_ENABLED = os.getenv("WATCHDOG_COMFY_ENABLED", "1") not in ("0", "false", "False", "")
__all__ = [name for name in globals()
           if name.isupper() and not name.startswith("_")]
