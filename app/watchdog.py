"""NapCat 心跳看门狗（独立进程）。

目的：NapCat 偶尔直接崩 / 卡死，崩了之后没人知道、扫码通知也不会发（死进程不写
qrcode.png）。这个进程独立于 qq_bot / NapCat，定时探 NapCat 是否还活着，死了就拉起
「一键启动全部 force」把三个窗口（Agent Web / NapCat / QQBOT-ADAPTER）全重启。

为什么独立：如果看门狗是 qq_bot 内部的一个线程，适配层自己卡死时就没人能救它了。

探测对象：NapCat OneBot HTTP（:3000）。复用 app.qq_api.check_alive（自带
trust_env=False，不受本机 Clash 注册表代理影响）。

判定：
  - 网络层失败（连不上 / 超时）连续 MAX_FAILS 次 → 判定 NapCat 死了 → 触发重启。
  - 能连上 → 不管 online 与否都不动：offline 时 NapCat 会出二维码，notify.py 的扫码
    通知自己会推；重启也救不了「会话失效要扫码」的情况，反而添乱。

重启：subprocess 拉起「一键启动全部.bat force」（新窗口，不阻塞 watchdog）。force =
忽略「6099 还在监听就跳过 NapCat」的保护，强制杀掉再起，处理「进程活着但卡死」的情形。

重启后等 RECOVER_WAIT 秒探活；成功 → 推「已恢复」；连续 MAX_RESTART_FAILS 次重启仍
救不活 → 停止自动重启 + 推「需人工处理」+ 退避 BACKOFF 秒，避免无限杀进程。
"""

import logging
import os
import subprocess
import sys
import time

# 看门狗是 Windows 专用（NapCat / QQ 都是 Windows 程序），文件锁也只在 Windows 有。
try:
    import msvcrt
except ImportError:  # 非 Windows 上退化成无锁（本项目不会走到这）
    msvcrt = None

from app.config import BASE_DIR
from app.qq_api import check_alive
from app.notify import push_text

log = logging.getLogger("watchdog")

# ── 可调参数 ──────────────────────────────────────────
PROBE_INTERVAL = 60.0      # 探测间隔（秒）
PROBE_TIMEOUT = 10.0       # 单次探测超时（秒）
MAX_FAILS = 3              # 连续失败几次判定为死（≈3 分钟）

RECOVER_INTERVAL = 10.0    # 重启后复查间隔（秒）
RECOVER_WAIT = 180.0       # 重启后最多等多久；超时算「恢复失败」

MAX_RESTART_FAILS = 3      # 连续几次重启都救不活 → 放弃自动重启
BACKOFF = 1800.0           # 放弃后退避多久再试（秒）

_ALL_BAT = os.path.join(BASE_DIR, "一键启动全部.bat")
_LOCK_PATH = os.path.join(BASE_DIR, "state", "watchdog.lock")


def _acquire_lock():
    """Windows 文件锁：进程死亡后系统自动释放，不会留下需要清理的死锁文件。

    返回锁文件对象（保活用）。拿不到说明已有实例在跑，直接退出。
    """
    os.makedirs(os.path.dirname(_LOCK_PATH), exist_ok=True)
    f = open(_LOCK_PATH, "w")
    if msvcrt is None:
        return f
    try:
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        log.warning("看门狗已在运行（锁被占用），退出")
        sys.exit(0)
    return f


def _probe():
    """探一次 NapCat。返回 (ok, detail)。"""
    try:
        uid, nick = check_alive(timeout=PROBE_TIMEOUT)
        return True, "%s(%s)" % (nick, uid)
    except Exception as e:
        return False, "%s: %s" % (type(e).__name__, e)


def _trigger_restart():
    """拉起「一键启动全部 force」，新窗口、不阻塞。返回是否成功发起。"""
    if not os.path.exists(_ALL_BAT):
        log.error("找不到 %s，无法重启", _ALL_BAT)
        return False
    try:
        subprocess.Popen(
            ["cmd", "/c", "start", "", "cmd", "/k", _ALL_BAT, "force"],
            creationflags=subprocess.CREATE_NEW_CONSOLE,
        )
        return True
    except Exception as e:
        log.error("拉起一键启动失败：%s", e)
        return False


def _recovering():
    """重启后等 NapCat 重新上线；期间持续探，回来即返回 True。"""
    deadline = time.time() + RECOVER_WAIT
    while time.time() < deadline:
        time.sleep(RECOVER_INTERVAL)
        ok, _ = _probe()
        if ok:
            return True
    return False


def _cycle(state, now=None):
    """一个探测周期。state 是跨周期保存的计数器字典。返回 True=继续循环。

    把单步抽出来是为了可测：测试直接驱动它、把 time / 探测 / 重启都 mock 掉。
    """
    now = time.time() if now is None else now
    if now < state["backoff_until"]:
        return True  # 退避期内不动作

    ok, detail = _probe()
    if ok:
        if state["fails"] or state["restart_fails"]:
            log.info("NapCat 恢复在线：%s", detail)
            if state["restart_fails"]:
                push_text("QQ 机器人已自动恢复", "NapCat 重启后已自动上线。")
            state["fails"] = 0
            state["restart_fails"] = 0
        else:
            log.debug("NapCat 在线：%s", detail)
        return True

    state["fails"] += 1
    log.warning("NapCat 探活失败 (%d/%d)：%s",
                state["fails"], MAX_FAILS, detail)
    if state["fails"] < MAX_FAILS:
        return True

    # 连续失败到阈值 → 判定死亡，触发重启
    log.error("NapCat 连续 %d 次无响应，触发重启", state["fails"])
    state["fails"] = 0
    push_text("QQ 机器人正在自动重启",
              "NapCat 心跳丢失，已拉起一键启动全部。若需扫码会另行通知。")
    if _trigger_restart():
        if _recovering():
            log.info("重启后 NapCat 已恢复")
            state["restart_fails"] = 0
            push_text("QQ 机器人已自动恢复", "NapCat 重启后已自动上线。")
        else:
            state["restart_fails"] += 1
            log.error("重启后 NapCat 仍未恢复（%d/%d）",
                      state["restart_fails"], MAX_RESTART_FAILS)
            if state["restart_fails"] >= MAX_RESTART_FAILS:
                log.error("连续 %d 次重启均失败，停止自动重启，退避 %.0f 秒",
                          MAX_RESTART_FAILS, BACKOFF)
                push_text("QQ 机器人需人工处理",
                          "看门狗多次自动重启失败，NapCat 仍无法恢复，请检查。")
                state["backoff_until"] = now + BACKOFF
                state["restart_fails"] = 0
    return True


def run():
    _acquire_lock()
    log.info("看门狗启动：探 NapCat :3000，间隔 %.0fs，连续 %d 次失败触发重启",
             PROBE_INTERVAL, MAX_FAILS)
    state = {"fails": 0, "restart_fails": 0, "backoff_until": 0.0}
    while True:
        time.sleep(PROBE_INTERVAL)
        _cycle(state)


if __name__ == "__main__":
    from app.logsetup import setup
    setup("watchdog")
    run()
