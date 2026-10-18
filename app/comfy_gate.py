"""跨进程的 ComfyUI 独占闸门（2026-10-18 用户提出「漫画要和机器人一样排队」）。

## 它解决什么

漫画工作台跑在 Flask 进程（`python agent.py`），QQ 机器人在 bot 进程
（`python -m app.qq_bot`）。两边各自的 `image_jobs` 队列、worker、「连画了几张」
计数**都只在自己进程里成立**，而 ComfyUI 只有一张卡。于是：

- QQ 侧 `COMFY_RELEASE_AFTER_TOTAL` / 同渠道连画 / 内存守卫 一触发
  `_restart_comfy()`，正在跑的漫画格当场被掐——用户看到的就是「漫画任务
  突然结束」；
- 反过来漫画每 3 格重启一次 ComfyUI，QQ 里等着的那张同样被掐。

## 做法

一把**目录锁**（`state/comfy.lock/`，`os.mkdir` 是原子的，Windows 一样成立）
＋ 心跳租约：

- 拿锁 = `os.mkdir` 成功；锁里写 `owner.json`（谁在跑、哪个 pid、什么时候开始、
  最后一次心跳、一个随机 token）。
- 拿着的人起一条守护线程每 `HEARTBEAT` 秒刷一次；释放时停线程、删文件、删目录。
- 别人要判断「能不能重启 ComfyUI」只看 `busy_others()`：**有别的进程在跑就
  绝不重启**，宁可这一轮什么都不做。
- 等锁的人每秒试一次，等到对方跑完（一次 anima 20~60 秒）。

## 关键取舍

- **幂等/僵尸**：进程被强杀时目录不会自己消失。心跳停顿超过 `STALE` 秒即视为
  失效，接管前先清掉。`STALE` 必须明显大于 `HEARTBEAT`，又不能长到让一次崩溃
  卡死全场——90 秒 ≈ 一格 20 秒 + 三次重试，够用。
- **只锁 ComfyUI**：NAI 走云端，压根不进这把锁（调用方在 `image_jobs._worker`
  里按 `chan.local` 分叉）。
- **不锁「等队列」**：worker 取到任务才拿锁，空闲排队时一律不持锁，否则对方
  永远等不到。
- **token 校验**：释放前比对 owner.json 里的 token，防止「自己的锁被判僵尸、
  被别人抢走之后又误删别人的」。
- **失败一律不阻塞出图**：拿不到锁（超时）由调用方决定怎么办；本模块只抛
  `TimeoutError`，不自杀、不改全局状态。
"""

import json
import logging
import os
import threading
import time
import uuid

from app.config import state_path

log = logging.getLogger("comfy_gate")

# 锁目录名（state/ 下）。空目录 = 没人持有。
LOCK_NAME = "comfy.lock"

# 心跳间隔：持锁方多久刷一次 owner.json
HEARTBEAT = 10.0

# 判僵尸的门槛：心跳停顿超过这么久就算持有者已经死了，可以抢。
# 必须 > HEARTBEAT（否则正常持锁的会被误判），95 秒 ≈ 一格 20 秒跑三次还有余。
STALE = 90.0

# 等锁的重试间隔。1 秒足够细（对方一格 20 秒起步），又不会把 CPU 烧起来。
POLL = 1.0

# 默认最长等多久。QQ 队列里排几张 + 一格两三分钟，30 分钟是「肯定出问题了」的量级。
DEFAULT_WAIT = 1800.0


def lock_dir():
    return state_path(LOCK_NAME)


def _owner_file():
    return os.path.join(lock_dir(), "owner.json")


def _read_owner():
    """读锁里的持有者信息；读不到（没锁/文件坏了）返回 None。"""
    try:
        with open(_owner_file(), "r", encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) else None


def _dir_age():
    try:
        return max(0.0, time.time() - os.path.getmtime(lock_dir()))
    except OSError:
        return 0.0


def _is_stale():
    """这把锁是不是僵尸（持有者已经死了）。

    两种形状都算：① owner.json 在但心跳太旧；② 目录在、owner.json 还没写出来
    （mkdir 成功后进程立刻被杀）且目录本身也旧了。
    """
    d = _read_owner()
    if d is None:
        return _dir_age() > STALE
    beat = d.get("beat") or d.get("since") or 0
    try:
        return (time.time() - float(beat)) > STALE
    except (TypeError, ValueError):
        return True


def _clear_stale():
    """清掉僵尸锁。清不掉就返回 False（别的进程正在抢先，下一轮再看）。"""
    try:
        fp = _owner_file()
        if os.path.isfile(fp):
            os.remove(fp)
        os.rmdir(lock_dir())
        log.warning("清掉一个僵尸 ComfyUI 锁（心跳已停超过 %.0f 秒）", STALE)
        return True
    except OSError:
        return False


def _write_owner(token, label):
    d = {"pid": os.getpid(), "token": token, "label": label,
         "since": time.time(), "beat": time.time()}
    try:
        os.makedirs(lock_dir(), exist_ok=True)
        with open(_owner_file(), "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
        return True
    except OSError as e:
        log.warning("写 ComfyUI 锁信息失败（%s）——锁仍然有效，只是没法标主人", e)
        return False


def holder():
    """当前持有者（未过期才算）；没人持有返回 None。"""
    if not os.path.isdir(lock_dir()):
        return None
    if _is_stale():
        return None
    return _read_owner()


def busy_others():
    """**别的进程**正在用 ComfyUI 吗？

    只认 pid 不同：自己持锁时（正在提交/等图）重启决策是允许的——
    那正是「提交前先重启」正常路径。同进程多线程的情况不存在
    （本地通道 `workers=1`，串行）。
    """
    d = holder()
    if not d:
        return False
    try:
        return int(d.get("pid") or 0) != os.getpid()
    except (TypeError, ValueError):
        return True


def describe(d):
    """一句话说清「现在是谁在跑」——日志和超时提示都用它。"""
    if not d:
        return "未知"
    label = str(d.get("label") or "").strip()
    pid = d.get("pid")
    try:
        age = time.time() - float(d.get("since") or time.time())
    except (TypeError, ValueError):
        age = 0.0
    return "%s%s，已跑 %.0f 秒" % (label or "另一个进程", " (pid %s)" % pid if pid else "", age)


class Gate:
    """一次持锁。用 hold() 拿，用 release() 放；也可以 with 语句。"""

    def __init__(self, token, label):
        self.token = token
        self.label = label
        self._stop = threading.Event()
        self._beat = threading.Thread(target=self._heartbeat, daemon=True,
                                      name="comfy-gate-heartbeat")

    def _heartbeat(self):
        while not self._stop.wait(HEARTBEAT):
            d = _read_owner()
            # token 对不上 = 锁已经被判僵尸、别人抢走了 → 自己悄悄停手，
            # 别去覆盖别人的 owner.json（那会把真正在跑的人写没）。
            if not d or d.get("token") != self.token:
                log.warning("ComfyUI 锁已易主（%s），本次心跳停止", self.label)
                return
            d["beat"] = time.time()
            try:
                with open(_owner_file(), "w", encoding="utf-8") as f:
                    json.dump(d, f, ensure_ascii=False)
            except OSError:
                pass

    def release(self):
        self._stop.set()
        d = _read_owner()
        if d and d.get("token") != self.token:
            return                      # 已经不是自己的锁了，别删
        try:
            fp = _owner_file()
            if os.path.isfile(fp):
                os.remove(fp)
            os.rmdir(lock_dir())
        except OSError:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.release()
        return False


def hold(label="", timeout=DEFAULT_WAIT):
    """拿锁（阻塞直到拿到或超时）。返回 Gate；超时抛 TimeoutError。

    label 只用于日志/排查（`state/comfy.lock/owner.json` 里能看到「谁在跑」）。
    """
    start = time.time()
    warned = False
    while True:
        try:
            os.makedirs(os.path.dirname(lock_dir()), exist_ok=True)
            os.mkdir(lock_dir())
        except FileExistsError:
            if _is_stale() and _clear_stale():
                continue
            if not warned:
                log.info("ComfyUI 正被 %s 占用，排队等它跑完（%s 等）",
                         describe(holder()), label or "未命名任务")
                warned = True
            waited = time.time() - start
            if waited >= timeout:
                raise TimeoutError(
                    "等 ComfyUI 空出来超过 %.0f 分钟（%s）"
                    % (timeout / 60.0, describe(holder())))
            time.sleep(POLL)
            continue
        except OSError as e:
            # mkdir 的其他失败（权限/路径）不该把出图卡死，退化成「没有闸门」
            log.warning("ComfyUI 闸门不可用（%s），这次不加锁直接跑", e)
            return Gate("", label)

        token = uuid.uuid4().hex
        _write_owner(token, label)
        waited = time.time() - start
        if waited > 1.0:
            log.info("取得 ComfyUI 独占（%s，排队等了 %.1f 秒）", label or "未命名任务", waited)
        gate = Gate(token, label)
        gate._beat.start()
        return gate
