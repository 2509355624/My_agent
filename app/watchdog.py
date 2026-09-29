"""NapCat 心跳看门狗（独立进程）。

目的：NapCat 偶尔直接崩 / 卡死，崩了之后没人知道、扫码通知也不会发（死进程不写
qrcode.png）。这个进程独立于 qq_bot / NapCat，定时探 NapCat 是否还活着，死了就拉起
「一键启动全部 force」把三个窗口（Agent Web / NapCat / QQBOT-ADAPTER）全重启。

为什么独立：如果看门狗是 qq_bot 内部的一个线程，适配层自己卡死时就没人能救它了。

探测对象：NapCat OneBot HTTP（:3000）。复用 app.qq_api.check_alive（自带
trust_env=False，不受本机 Clash 注册表代理影响）。

判定（探活三态，见 `_probe`）：
  - "dead"：连不上（:6099 WebUI 口也没监听 = 进程没了 / 卡死）。连续 MAX_FAILS 次
    → 触发重启。
  - "offline"：:6099 在听但 :3000 不通 = 进程活着、**登录态失效**。持续 OFFLINE_GRACE
    秒（≈二维码有效期）→ 触发重启，换一张新码推给用户。

  ⚠ 「活着但没登录」以前是**不动**的，原文写着「重启也救不了会话失效要扫码，反而添乱」。
  实测推翻了这个假设：重启走的是 `-q <号>` 快速登录，2026-09-29 20:30 那次重启后
  NapCat 自己就登回去了，根本不用扫码。而且用户明确要求「掉线就重启，别只丢个二维码
  让我自己扫 —— 我扫完还没反应」（09-29）。所以现在两种掉线都重启，区别只在确认时长：
  OFFLINE_GRACE 留够时间给「人正在扫」和「NapCat 自己正在快速登录」，免得白重启一次、
  还要人多扫一次码。

重启：subprocess 拉起「一键启动全部.bat force auto」（**隐藏窗口**，不阻塞 watchdog）。
  - force = 忽略「6099 还在监听就跳过 NapCat」的保护，强制杀掉再起，处理「进程活着
    但卡死」的情形。
  - auto  = 启动器跑完直接退出，不留窗口、不等回车。看门狗每重启一次就多一个窗口的话
    桌面上很快就堆满了（09-29 用户抱怨）。

  ⚠ 这个批处理会把**看门狗自己也换掉**（用户实测 09-29 20:30：发起重启的那个看门狗
  在写出结论通知之前就没了，`finally` 里的 clear_restarting() 没执行，qq_bot 那边的
  掉线通知被孤儿标记压了 15 分钟）。所以结论通知**不能只指望发起者还活着**：
  notify.restarting() 会检查标记里记的发起 pid，发现它死了就立刻交还通知权。

**重启后只发一条通知**（09-29 用户要求）：重启一发起就立刻盯结果，等出结论再发，
不再先发「正在自动重启」、过一会儿再发「要扫码」。三种结论：
  - 已登录（:3000 探通）      → 推「已自动恢复」
  - NapCat 起来但没登录 + 二维码是本次新写的 → 推「已重启，需要扫码」+ 二维码图
  - 超时都没等到               → 推「重启后未恢复」并计入失败次数

「要扫码」也计入失败次数：一直没人扫就每隔 OFFLINE_GRACE 重启一次，连续
MAX_RESTART_FAILS 次仍停在扫码界面 → 暂停自动重启并退避 BACKOFF 秒（避免整夜
反复杀进程）。退避期间二维码换新照样由 notify 推给用户，人一扫通就自动恢复。

重启期间用 notify.mark_restarting() 把二维码通知权从 qq_bot 的 watcher 手里接管过来，
否则同一次掉线会收到两条（一条说重启、一条只说扫码）。连续 MAX_RESTART_FAILS 次重启
仍救不活 → 停止自动重启 + 推「需人工处理」+ 退避 BACKOFF 秒，避免无限杀进程。
"""

import logging
import os
import socket
import subprocess
import sys
import time

# 看门狗是 Windows 专用（NapCat / QQ 都是 Windows 程序），文件锁也只在 Windows 有。
try:
    import msvcrt
except ImportError:  # 非 Windows 上退化成无锁（本项目不会走到这）
    msvcrt = None

from app import notify
from app.config import BASE_DIR, NOTIFY_QRCODE_PATH
from app.qq_api import check_alive
from app.notify import push_text

log = logging.getLogger("watchdog")

# ── 可调参数 ──────────────────────────────────────────
PROBE_INTERVAL = 30.0      # 探测间隔（秒）
PROBE_TIMEOUT = 10.0       # 单次探测超时（秒）
MAX_FAILS = 3              # 连续失败几次判定为死（≈90 秒）

# 「活着但没登录」持续这么久 → 判定「二维码过期了还没人扫」，重启换一张新码。
# 取 ≈ 二维码有效期（NapCat 约 180 秒）：等短了会把「NapCat 正在快速登录」或
# 「人正在扫码」误判成没人管，白重启一次、还要人多扫一次码；等长了用户就一直
# 拿着过期码。
OFFLINE_GRACE = 180.0

RECOVER_INTERVAL = 3.0     # 重启后复查间隔（秒）—— 要小，扫码通知才「立刻」
RECOVER_WAIT = 180.0       # 重启后最多等多久；超时算「恢复失败」
PROBE_TIMEOUT_FAST = 3.0   # 重启窗口内的探测超时，短一点免得拖慢判定

NAPCAT_WEBUI_PORT = 6099   # NapCat WebUI：它起来了就说明进程活着（登录前也监听）
QR_GRACE = 20.0            # WebUI 起来后再等这么久还没登录 → 判定要扫码

MAX_RESTART_FAILS = 3      # 连续几次重启都救不活 → 放弃自动重启
BACKOFF = 1800.0           # 放弃后退避多久再试（秒）

_RECOVER_TEXT = "NapCat 心跳丢失，看门狗已自动重启，现已恢复上线。"

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


def _probe(timeout=None):
    """探一次 NapCat。返回 (state, detail)，state 三态：

      - "online"  连得上且已登录
      - "offline" 进程活着但没登录（登录态失效，等着扫码 / 快速登录）
      - "dead"    连不上，且 WebUI 口也没监听（进程没了或卡死）

    为什么不能只看 :3000 通不通：**3000 只在登录成功后才监听**（登录前只有 6099），
    所以「3000 连不上」既可能是进程死了、也可能只是没登录 —— 前者重启能救，后者
    重启是另一回事（换新码 / 快速登录）。用 6099（登录前就监听）把两者分开。
    """
    try:
        uid, nick = check_alive(
            timeout=PROBE_TIMEOUT if timeout is None else timeout)
    except Exception as e:
        detail = "%s: %s" % (type(e).__name__, e)
        if _port_open(NAPCAT_WEBUI_PORT):
            return "offline", detail
        return "dead", detail
    if not uid:
        # 拿到了响应但没有登录态（OneBot 未登录时也可能回 status=ok + user_id 0）
        return "offline", str(nick or "")
    return "online", "%s(%s)" % (nick, uid)


def _port_open(port, timeout=1.0):
    """127.0.0.1:port 是否在监听（TCP 连得上）。"""
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect(("127.0.0.1", int(port)))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _qr_written_since(since):
    """二维码文件是不是本次重启之后新写的（= 真的在等人扫这一轮的码）。

    留 5 秒余量：文件系统时间戳精度 + NapCat 写文件的时刻可能略早于我们记的
    重启时刻（force 分支里先杀进程再拉起，中间的 ping 延迟不小）。
    """
    try:
        return os.path.getmtime(NOTIFY_QRCODE_PATH) >= since - 5
    except OSError:
        return False


def _trigger_restart():
    """拉起「一键启动全部 force auto」，隐藏窗口、不阻塞。返回是否成功发起。"""
    if not os.path.exists(_ALL_BAT):
        log.error("找不到 %s，无法重启", _ALL_BAT)
        return False
    try:
        subprocess.Popen(
            ["cmd", "/c", _ALL_BAT, "force", "auto"],
            creationflags=subprocess.CREATE_NO_WINDOW,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except Exception as e:
        log.error("拉起一键启动失败：%s", e)
        return False


def _await_outcome(since):
    """重启后判定结果：'online'（已登录）/ 'qr'（要扫码）/ 'unknown'（没救活）。

    'qr' 要三个条件同时成立：NapCat 进程活着但没登录（_probe 给 "offline"，等价于
    6099 在听而 :3000 不通）+ 这个状态持续够 QR_GRACE + 二维码文件是本次重启后新写的。
    缺一不可 —— 只看端口会把「刚起来、快速登录还没走完」误判成要扫码；只看文件会把
    上一轮的旧码误判成本次的。
    """
    deadline = time.time() + RECOVER_WAIT
    offline_since = None
    while time.time() < deadline:
        st, _ = _probe(timeout=PROBE_TIMEOUT_FAST)
        if st == "online":
            return "online"
        if st == "offline":
            if offline_since is None:
                offline_since = time.time()
            elif (time.time() - offline_since >= QR_GRACE
                  and _qr_written_since(since)):
                return "qr"
        else:
            offline_since = None
        time.sleep(RECOVER_INTERVAL)
    return "unknown"


def _cycle(state, now=None):
    """一个探测周期。state 是跨周期保存的计数器字典。返回 True=继续循环。

    把单步抽出来是为了可测：测试直接驱动它、把 time / 探测 / 重启都 mock 掉。
    """
    now = time.time() if now is None else now
    if now < state["backoff_until"]:
        return True  # 退避期内不动作

    st, detail = _probe()

    if st == "online":
        state["offline_since"] = None
        state["backoff_until"] = 0.0        # 真的好了，把退避一并清掉
        if state["fails"] or state["restart_fails"]:
            log.info("NapCat 恢复在线：%s", detail)
            if state["restart_fails"]:
                push_text("QQ 机器人已自动恢复", _RECOVER_TEXT)
            state["fails"] = 0
            state["restart_fails"] = 0
        else:
            log.debug("NapCat 在线：%s", detail)
        return True

    if st == "dead":
        state["offline_since"] = None
        state["fails"] += 1
        log.warning("NapCat 探活失败 (%d/%d)：%s",
                    state["fails"], MAX_FAILS, detail)
        if state["fails"] < MAX_FAILS:
            return True
        return _restart(state, now, "NapCat 心跳丢失")

    # "offline"：进程活着，登录态没了 —— 先给扫码/快速登录留够时间
    state["fails"] = 0
    if state["offline_since"] is None:
        state["offline_since"] = now
        log.warning("NapCat 活着但未登录，先等 %.0f 秒看会不会自己扫码/快速登录",
                    OFFLINE_GRACE)
        return True
    if now - state["offline_since"] < OFFLINE_GRACE:
        return True
    return _restart(state, now, "登录态失效、二维码过期仍没人扫")


def _restart(state, now, reason):
    """拉起一键启动、等结论、把结论推给用户。返回 True（继续循环）。

    reason 只进通知文案，用来区分是「心跳丢失」还是「登录态失效」。
    """
    log.error("%s，触发重启", reason)
    state["fails"] = 0
    state["offline_since"] = None
    since = time.time()
    notify.mark_restarting()
    try:
        if not _trigger_restart():
            state["restart_fails"] += 1
            push_text("QQ 机器人自动重启失败",
                      "找不到或无法拉起「一键启动全部.bat」，请手动处理。")
            if state["restart_fails"] >= MAX_RESTART_FAILS:
                state["backoff_until"] = now + BACKOFF
                state["restart_fails"] = 0
            return True
        outcome = _await_outcome(since)
    finally:
        notify.clear_restarting()

    if outcome == "online":
        log.info("重启后 NapCat 已恢复")
        state["restart_fails"] = 0
        push_text("QQ 机器人已自动恢复", _RECOVER_TEXT)
    elif outcome == "qr":
        # 「已重启」和「要扫码」合并成一条 —— 不再先发一条说在重启。
        log.info("重启后需要扫码，已把二维码随通知一起推给用户")
        state["restart_fails"] += 1
        notify.push_offline(
            NOTIFY_QRCODE_PATH,
            title="QQ 机器人已重启，需要扫码",
            intro="%s %s，看门狗已自动重启；重启后需要扫码登录。"
                  % (time.strftime("%m-%d %H:%M:%S"), reason))
        if state["restart_fails"] >= MAX_RESTART_FAILS:
            log.error("连续 %d 次重启后都还停在扫码界面，暂停自动重启，退避 %.0f 秒",
                      MAX_RESTART_FAILS, BACKOFF)
            push_text("QQ 机器人仍在等待扫码",
                      "已连续自动重启 %d 次仍停在扫码界面，暂停自动重启 %.0f 分钟；"
                      "二维码换新后仍会推给你。"
                      % (MAX_RESTART_FAILS, BACKOFF / 60))
            state["backoff_until"] = now + BACKOFF
            state["restart_fails"] = 0
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
        else:
            push_text("QQ 机器人重启后未恢复",
                      "看门狗已自动重启，但 NapCat 仍未上线，稍后会再试。")
    return True


def run():
    _acquire_lock()
    log.info("看门狗启动：探 NapCat :3000，间隔 %.0fs；连不上 %d 次、或未登录 %.0fs 即重启",
             PROBE_INTERVAL, MAX_FAILS, OFFLINE_GRACE)
    state = {"fails": 0, "restart_fails": 0, "offline_since": None,
             "backoff_until": 0.0}
    while True:
        time.sleep(PROBE_INTERVAL)
        _cycle(state)


if __name__ == "__main__":
    from app.logsetup import setup
    setup("watchdog")
    run()
