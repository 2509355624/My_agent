"""NapCat + ComfyUI 心跳看门狗（独立进程）。

目的：NapCat 偶尔直接崩 / 卡死，崩了之后没人知道、扫码通知也不会发（死进程不写
qrcode.png）。这个进程独立于 qq_bot / NapCat，定时探 NapCat 是否还活着，死了就拉起
「一键启动全部 force」把三个窗口（Agent Web / NapCat / QQBOT-ADAPTER）全重启。

**另外还盯 ComfyUI（2026-09-30 加，见「ComfyUI 分支」一节）**：它原先**没有任何守护**
——NapCat 有自愈（杀了 QQ.exe 约 40 秒自己拉起来）、qq_bot 有本看门狗，只有 ComfyUI
崩了就彻底断，只能人工去《启动手册.txt》里抄那行命令起它。

为什么独立：如果看门狗是 qq_bot 内部的一个线程，适配层自己卡死时就没人能救它了。

探测对象：NapCat OneBot HTTP（:3000）。复用 app.qq_api.check_alive（自带
trust_env=False，不受本机 Clash 注册表代理影响）。

判定（探活三态，见 `_probe`）：
  - "dead"：连不上（:6099 WebUI 口也没监听 = 进程没了 / 卡死）。连续 MAX_FAILS 次
    → 触发重启。
  - "offline"：:6099 在听但 :3000 不通 = 进程活着、**登录态失效**。持续 OFFLINE_GRACE
    秒（≈二维码有效期）→ 触发重启，换一张新码推给用户。

  ⚠ 「活着但没登录」以前是**不动**的。**09-29 两次误判**：原文写着「实测推翻了这个假设：
  重启走 `-q <号>` 快速登录，20:30 那次 NapCat 自己就登回去了」—— **用户两次纠正：恢复
  都是他手动扫码的结果，不是自动登录。所以别假设快速登录有效。**
  现在两种掉线都重启，理由**不是**「重启能自愈」，而是：**只有重启才能把一张新二维码推到
  用户手上**（用户 09-29 要求「掉线就重启，别只丢个二维码让我自己扫 —— 我扫完还没反应」）。
  OFFLINE_GRACE 留够时间给「人正在扫」，免得白重启一次、还要人多扫一次码。

重启：subprocess 拉起「一键启动全部.bat force auto」（**隐藏窗口**，不阻塞 watchdog）。
  - force = 忽略「6099 还在监听就跳过 NapCat」的保护，强制杀掉再起，处理「进程活着
    但卡死」的情形。
  - auto  = 启动器跑完直接退出，不留窗口、不等回车。看门狗每重启一次就多一个窗口的话
    桌面上很快就堆满了（09-29 用户抱怨）。

  ⚠ 这个批处理会把**看门狗自己也换掉**（用户实测 09-29 20:30：发起重启的那个看门狗
  在写出结论通知之前就没了，`finally` 里的 clear_restarting() 没执行，qq_bot 那边的
  掉线通知被孤儿标记压了 15 分钟）。所以结论通知**不能只指望发起者还活着**：
  notify.restarting() 会检查标记里记的发起 pid，发现它死了就立刻交还通知权。

**重启后只发一条通知，而且只在「要扫码」时发**（09-29 用户拍板）：重启一发起就立刻
盯结果，等出结论再发，不再先发「正在自动重启」。三种结论：
  - 已登录（:3000 探通）      → **不通知**（用户原话：「重启可以自动登上来就不用通知」）
  - NapCat 起来但没登录 + 二维码是本次新写的 → 推「已重启，需要扫码」+ 二维码图
  - 超时都没等到               → 推「重启后未恢复」并计入失败次数

**静默判据（假在线，见 `_check_silence`）**：三态探针探不出「接口全好但收不到消息」，
所以在线时再看一眼 qq_bot 的静默时长（`state/qq_status.json` 的 `last_activity_ago`）：
超过 `SILENCE_SECONDS` 一条消息都没有 → 记一条日志。

  ⚠️ **09-30 起默认只记日志、不重启**（`WATCHDOG_SILENCE_RESTART=0`）。原设计是
  「重启一次**当探针**」，但一天 32 次实测推翻了它：重启走 `一键启动全部 force`，
  里面 `taskkill /IM QQ.exe /F` 再 `-q` 快速登录，**腾讯把「刚登过又登」当异常登录、
  转身作废会话** → 真掉线。硬证据：08:57:50 探针重启后 **3 分钟**（09:00:57）就掉线。
  也就是说「假在线」很可能是这个循环**制造**的。想恢复老行为设
  `WATCHDOG_SILENCE_RESTART=1`。详细因果见 `_check_silence` 和 config.py。

「要扫码」也计入失败次数：一直没人扫就每隔 OFFLINE_GRACE 重启一次，连续
MAX_RESTART_FAILS 次仍停在扫码界面 → 暂停自动重启并退避 BACKOFF 秒（避免整夜
反复杀进程）。退避期间二维码换新照样由 notify 推给用户，人一扫通就自动恢复。

重启期间用 notify.mark_restarting() 把二维码通知权从 qq_bot 的 watcher 手里接管过来，
否则同一次掉线会收到两条（一条说重启、一条只说扫码）。连续 MAX_RESTART_FAILS 次重启
仍救不活 → 停止自动重启 + 推「需人工处理」+ 退避 BACKOFF 秒，避免无限杀进程。

## ComfyUI 分支（2026-09-30 加）

⚠️ **整条分支默认停用**（`WATCHDOG_COMFY_ENABLED=0`）：上线当天凌晨就过度重启
（05:20 / 05:51 / 06:26 各一轮 + 1800 秒退避），因为 ComfyUI 是**按需启动、常常
故意不开**，看门狗却当成崩了。`comfy_seen` 闩是事后补的、还没真机验过，所以先整体
关掉。真要开：`.env` 设 `WATCHDOG_COMFY_ENABLED=1` 并重启看门狗。

跟 NapCat 那套**平行**、互不干扰：两边各有自己的失败计数和退避，一个挂了不影响另一个。
探的是 ComfyUI 的 `/system_stats`（:8188），连续 `MAX_FAILS` 次拿不到就拉
`启动ComfyUI.bat auto`（隐藏窗口）。

四条跟 NapCat 不一样的地方：

1. **只在「它本来在跑」时才管**（用户 09-30 拍板）：ComfyUI 按需启动，用户常常
   压根没开。所以先探到一次「在线」才认账（`comfy_seen` 闩），之后**从在线变成
   不通**才动手；从没在线过就不动。否则「故意不开省显存」会被强行拉起来。

2. **静默重启，不通知**（用户 09-30 拍板）：ComfyUI 是本地服务，重启不需要人做任何事
   ——不像 NapCat 要扫码。所以起来就自己好了，按「恢复不通知」同一个原则处理。只有
   **连续 MAX_RESTART_FAILS 次都拉不起来**时才推一条「需人工处理」。

3. **没有「登录态」这种中间态**：NapCat 要分 online/offline/dead 三态（因为「进程活着
   但没登录」需要扫码）。ComfyUI 只有「8188 通」和「不通」两态，判定简单。

4. **不 taskkill**：探活连续 3 次失败时进程多半已经没了，杀不杀一样；反过来万一误判
   （ComfyUI 正在自己重启、或只是卡了一下），一杀就是把正在跑的图连进程一起干掉。所以
   bat 只负责起，端口被占时 ComfyUI 自己会报 bind 失败退出。

⚠️ **自己没人守**这个老问题依旧：本进程崩了没人拉它（bat 只看「有没有 WATCHDOG 窗口」）。
"""

import json
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
from app.config import (BASE_DIR, NOTIFY_QRCODE_PATH,
                        WATCHDOG_COMFY_ENABLED,
                        WATCHDOG_SILENCE_COOLDOWN, WATCHDOG_SILENCE_MAX_GAP,
                        WATCHDOG_SILENCE_RESTART, WATCHDOG_SILENCE_SECONDS)
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

# ── 静默判据（假在线）────────────────────────────────
# 三态探针探不出「假在线」（:3000 接口全好、登录态在，只有腾讯→客户端的下行
# 推送死了），唯一可观测的信号是「本该到的消息没到」。所以在线时再看 qq_bot 的
# 静默时长：超过 SILENCE_SECONDS 一条消息都没有 → 重启一次当探针。
SILENCE_SECONDS = WATCHDOG_SILENCE_SECONDS     # 多久没消息算「疑似假在线」
SILENCE_COOLDOWN = WATCHDOG_SILENCE_COOLDOWN   # 连续静默时两次重启的起步间隔
SILENCE_MAX_GAP = WATCHDOG_SILENCE_MAX_GAP     # 间隔倍增的上限
# ⚠️ 默认 False = 静默时**只记日志、不重启**（09-30 用户拍板，理由见 config.py）。
SILENCE_RESTART = WATCHDOG_SILENCE_RESTART

_STATUS_PATH = os.path.join(BASE_DIR, "state", "qq_status.json")
# 快照本身比这还旧 → qq_bot 的状态线程也停了（或没起），静默判不了，不动手。
# 状态快照每 1 秒写一次，120 秒留了很宽的余量。
STATUS_STALE = 120.0

_ALL_BAT = os.path.join(BASE_DIR, "一键启动全部.bat")
_LOCK_PATH = os.path.join(BASE_DIR, "state", "watchdog.lock")

# ── ComfyUI 分支参数（2026-09-30 加）──────────────────
# 跟 NapCat 同一套时序：30 秒探一次、连续 3 次算死（≈90 秒）。用户 09-30 拍板沿用。
COMFYUI_URL = "http://127.0.0.1:8188"
COMFYUI_BAT = os.path.join(BASE_DIR, "启动ComfyUI.bat")
COMFYUI_TIMEOUT = 5.0      # 探 /system_stats 的超时（秒）——本机口，给足 5 秒够了

# ComfyUI 崩了没人管这件事，只有本进程在兜。自己崩了就没人拉它——这是已知缺口，
# 不在这里解决（bat 只看窗口标题）。
#
# ⚠️ 总闸默认关（09-30）：见 `_comfy_cycle` docstring。
COMFY_ENABLED = WATCHDOG_COMFY_ENABLED
_COMFY_SESSION = None


def _comfy_session():
    """探 ComfyUI 用的 requests session，`trust_env=False`。

    跟 qq_api / nai 一个道理：本机所有出网口都不许走环境变量/注册表里的代理
    （这台机器上跑着 Clash，代理会把 127.0.0.1 也拦掉，表现为 502「目标计算机
    积极拒绝」——看着像 ComfyUI 挂了，其实是被代理挡了）。
    """
    global _COMFY_SESSION
    if _COMFY_SESSION is None:
        import requests
        s = requests.Session()
        s.trust_env = False
        _COMFY_SESSION = s
    return _COMFY_SESSION


def _probe_comfyui(timeout=None):
    """探一次 ComfyUI。返回 (alive, detail)。

    **只有两态**（不像 NapCat 有三态）：ComfyUI 没有「进程活着但没登录」这种需要
    人介入的中间态，8188 答得上就算活着。

    ⚠️ 判据是「HTTP 200 + 是预期的 JSON」，不是「TCP 连得上」：8188 在 ComfyUI
    刚启动、还没加载完 custom nodes 时是**不监听**的，TCP 探会得到「连不上」，而
    那正是「它正在起来」——交给连续失败计数兜住就行。反过来，端口被别的进程占了
    而它回了别的 JSON，也算不健康。
    """
    t = COMFYUI_TIMEOUT if timeout is None else timeout
    try:
        resp = _comfy_session().get(COMFYUI_URL + "/system_stats", timeout=t)
        resp.raise_for_status()
        devs = (resp.json() or {}).get("devices")
        if not devs:
            return False, "回了 200 但没有 devices"
        return True, "vram_free=%.1fGB" % (
            (devs[0].get("vram_free") or 0) / 2 ** 30)
    except Exception as e:
        return False, "%s: %s" % (type(e).__name__, e)


def _trigger_comfy_restart():
    """拉起「启动ComfyUI.bat auto」，隐藏窗口、不阻塞。返回是否成功发起。"""
    if not os.path.exists(COMFYUI_BAT):
        log.error("找不到 %s，无法重启 ComfyUI", COMFYUI_BAT)
        return False
    try:
        subprocess.Popen(
            ["cmd", "/c", COMFYUI_BAT, "auto"],
            creationflags=subprocess.CREATE_NO_WINDOW,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except Exception as e:
        log.error("拉起 ComfyUI 启动脚本失败：%s", e)
        return False


def _comfy_cycle(state, now=None):
    """ComfyUI 的一个探测周期。返回 True=继续循环。

    跟 `_cycle` 平行的独立分支——两边计数分开，互不干扰。

    ⚠️ **只在「它本来在跑」时才管**（用户 09-30 拍板）：ComfyUI 是按需启动的，
    用户常常压根没开。要是「探不到就拉」，他故意不开（想省显存跑别的）也会被
    看门狗强行拉起来。所以加一道**闩**：先探到一次「在线」才认账（`comfy_seen`），
    之后**从在线变成不通**才动手。从没在线过 = 它本来就没开，不动。

    闩在重启看门狗进程时归零（state 是新建的）：新起的看门狗不该假设 ComfyUI
    曾经在跑。等它下一次探到在线，闩自己又置上。

    ⚠️ **09-30 起整条分支默认停用**（`WATCHDOG_COMFY_ENABLED=False`）：它上线当天
    凌晨就过度重启（05:20 / 05:51 / 06:26 各一轮、还烧到 1800 秒退避），因为
    `comfy_seen` 闩那一版是事后才补的、还没在真机验过。默认先关，只保留探活日志；
    等真机确认闩行为正确，再在 `.env` 里设 `WATCHDOG_COMFY_ENABLED=1` 打开。
    """
    if not COMFY_ENABLED:
        return True
    now = time.time() if now is None else now
    if now < state["comfy_backoff_until"]:
        return True

    alive, detail = _probe_comfyui()
    if alive:
        state["comfy_seen"] = True
        state["comfy_fails"] = 0
        state["comfy_backoff_until"] = 0.0
        if state["comfy_restart_fails"]:
            # 静默恢复：按用户要求不通知。
            log.info("ComfyUI 已恢复：%s", detail)
            state["comfy_restart_fails"] = 0
        else:
            log.debug("ComfyUI 在线：%s", detail)
        return True

    if not state["comfy_seen"]:
        # 从没在线过 = 用户本来就没开它。不动手，也不记失败。
        log.debug("ComfyUI 不通（本进程还没见它在线过），按「没在跑」处理，不动手")
        return True

    state["comfy_fails"] += 1
    log.warning("ComfyUI 探活失败 (%d/%d)：%s",
                state["comfy_fails"], MAX_FAILS, detail)
    if state["comfy_fails"] < MAX_FAILS:
        return True
    return _comfy_restart(state, now, detail)


def _comfy_restart(state, now, detail):
    """拉起 ComfyUI 启动脚本。**静默**，只在连续失败到上限时才通知。返回 True。"""
    log.error("ComfyUI 心跳丢失（%s），触发重启", detail)
    state["comfy_fails"] = 0
    if not _trigger_comfy_restart():
        state["comfy_restart_fails"] += 1
        if state["comfy_restart_fails"] >= MAX_RESTART_FAILS:
            log.error("连续 %d 次都拉不起 ComfyUI，暂停自动重启，退避 %.0f 秒",
                      MAX_RESTART_FAILS, BACKOFF)
            push_text("ComfyUI 需人工处理",
                      "看门狗多次尝试重启 ComfyUI 都没有成功，请手动双击"
                      "「启动ComfyUI.bat」检查。")
            state["comfy_backoff_until"] = now + BACKOFF
            state["comfy_restart_fails"] = 0
        return True

    # 静默重启：不推「正在重启」，起来了也不推（跟 NapCat「恢复不通知」同一原则）。
    # 只有一个例外——连续几次都没起来，才告诉用户要人工看一眼。
    state["comfy_restart_fails"] += 1
    if state["comfy_restart_fails"] >= MAX_RESTART_FAILS:
        log.error("连续 %d 次重启后 ComfyUI 仍未恢复，暂停自动重启，退避 %.0f 秒",
                  MAX_RESTART_FAILS, BACKOFF)
        push_text("ComfyUI 需人工处理",
                  "看门狗已连续自动重启 %d 次，ComfyUI 仍未上线，请检查。"
                  % MAX_RESTART_FAILS)
        state["comfy_backoff_until"] = now + BACKOFF
        state["comfy_restart_fails"] = 0
    else:
        log.info("已静默拉起 ComfyUI（第 %d 次尝试，按用户要求不通知）",
                 state["comfy_restart_fails"])
    return True


# 锁文件句柄必须**一直活着**：句柄一关，Windows 就把锁释放了。
# 09-29 实测踩过：run() 里写的是 `_acquire_lock()`，返回值被丢掉，函数一返回句柄
# 就被垃圾回收 → 锁立刻失效 → 能同时跑好几个看门狗（watchdog.log 里 23:33:16 和
# 23:33:30 连着两条「活着但未登录」就是两个实例各写了一条，一个实例在 30 秒内
# 不可能打印两次）。所以句柄存到模块级变量里，不指望调用方接住返回值。
_LOCK_FILE = None


def _acquire_lock():
    """Windows 文件锁：进程死亡后系统自动释放，不会留下需要清理的死锁文件。

    返回锁文件对象（也存进 `_LOCK_FILE` 保活）。拿不到说明已有实例在跑，直接退出。
    """
    global _LOCK_FILE
    os.makedirs(os.path.dirname(_LOCK_PATH), exist_ok=True)
    f = open(_LOCK_PATH, "w")
    if msvcrt is None:
        return f
    try:
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        log.warning("看门狗已在运行（锁被占用），退出")
        sys.exit(0)
    _LOCK_FILE = f
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


def _silent_seconds(now):
    """距「最后一次收发消息」过了多少秒；读不到 / 判不了返回 None。

    ⚠️ 不能直接读快照里的 `last_activity_ago` —— 那是**写快照那一刻**算出来的值，
    qq_bot 的状态线程一停它就冻住，看着永远「刚活动过」。改用快照的 `ts` 反推绝对
    时刻（last_activity = ts - last_activity_ago）再跟 now 比。

    快照自己太旧（qq_bot 卡死 / 压根没起）→ 返回 None：那是「进程级」的毛病，交给
    探活那两条分支，静默判据不抢。
    """
    try:
        with open(_STATUS_PATH, encoding="utf-8") as f:
            snap = json.load(f)
    except (OSError, ValueError):
        return None
    ts = snap.get("ts")
    ago = snap.get("last_activity_ago")
    if not isinstance(ts, (int, float)) or not isinstance(ago, (int, float)):
        return None
    if abs(now - ts) > STATUS_STALE:
        return None
    return max(0.0, now - (ts - ago))


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


def _check_silence(state, now):
    """在线、但长时间一条消息都没收到 → 疑似假在线。

    ⚠️ **09-30 起默认只记日志、不重启**（`SILENCE_RESTART=False`）。原设计的逻辑
    是「重启一次**当探针**」：重启后**自动登录**说明会话本来是好的（就是群里安静），
    **需要扫码**说明会话早被腾讯作废了。但一天 32 次实测推翻了它 —— 重启走的是
    `一键启动全部 force`，里面 `taskkill /IM QQ.exe /F` 再 `-q` 快速登录，
    **腾讯把「刚登过又登」当异常登录，转身就作废会话**。于是：

        没人说话 → 静默 → 重启 → 被腾讯作废 → 真掉线 → 又没人说话 → 又重启 …

    08:57:50 那把探针重启后 3 分钟（09:00:57）就掉线，是这条链的硬证据。
    也就是说「假在线」很可能是这个循环**制造**出来的，重启当探针是**自证预言**。

    所以现在只观察不动手：默认仍然算退避、仍然写日志，只是不调 `_restart`。
    要恢复老行为，`.env` 里设 `WATCHDOG_SILENCE_RESTART=1` 并重启看门狗。

    ⚠️ 退避仍然必须留着（即使不重启）：`silence_gap` 是观察期的节流阀，
    没有它日志会被每分钟一条刷爆。
    """
    silent = _silent_seconds(now)
    if silent is None:
        return True                      # 快照读不到 → 不下判断
    if silent < SILENCE_SECONDS:
        state["silence_gap"] = 0.0       # 有动静 → 退避清零
        return True
    if now < state["silence_until"]:
        return True                      # 还在上一轮的退避里
    gap = state["silence_gap"] or SILENCE_COOLDOWN
    state["silence_gap"] = min(gap * 2.0, SILENCE_MAX_GAP)
    state["silence_until"] = now + gap
    if not SILENCE_RESTART:
        log.warning("已静默 %.0f 分钟（阈值 %.0f 分钟）但探活正常——"
                    "疑似假在线，**只记录不重启**（WATCHDOG_SILENCE_RESTART=0）；"
                    "下次最早 %.0f 分钟后再看",
                    silent / 60.0, SILENCE_SECONDS / 60.0, gap / 60.0)
        return True
    log.warning("已静默 %.0f 分钟（阈值 %.0f 分钟）但探活正常，疑似假在线；"
                "重启一次当探针（下次最早 %.0f 分钟后再判）",
                silent / 60.0, SILENCE_SECONDS / 60.0, gap / 60.0)
    return _restart(state, now, "长时间收不到任何消息（疑似假在线）")


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
            # 恢复本身**不通知**（09-29 用户要求）：能自己登回来就不打扰他。
            log.info("NapCat 恢复在线：%s", detail)
            state["fails"] = 0
            state["restart_fails"] = 0
        else:
            log.debug("NapCat 在线：%s", detail)
        return _check_silence(state, now)

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
        # 自动登录 = 会话本来就是好的 → 按用户要求**不通知**。
        log.info("重启后 NapCat 已恢复（自动登录，按用户要求不通知）")
        state["restart_fails"] = 0
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
    log.info("看门狗启动：探 NapCat :3000，间隔 %.0fs；连不上 %d 次、或未登录 %.0fs 即重启。"
             "同时探 ComfyUI :8188，连不上 %d 次即静默拉起「启动ComfyUI.bat」",
             PROBE_INTERVAL, MAX_FAILS, OFFLINE_GRACE, MAX_FAILS)
    state = {"fails": 0, "restart_fails": 0, "offline_since": None,
             "backoff_until": 0.0, "silence_until": 0.0, "silence_gap": 0.0,
             "comfy_fails": 0, "comfy_restart_fails": 0,
             "comfy_backoff_until": 0.0, "comfy_seen": False}
    while True:
        time.sleep(PROBE_INTERVAL)
        _cycle(state)
        # ComfyUI 独立分支：它抛异常（比如 requests 炸了）不能让 NapCat 的守护停摆
        # ——那是「一个服务的问题拖垮整个看门狗」，正好是加这条要防的事。
        try:
            _comfy_cycle(state)
        except Exception:
            log.exception("ComfyUI 探活分支异常，本轮跳过")


if __name__ == "__main__":
    from app.logsetup import setup
    setup("watchdog")
    run()
