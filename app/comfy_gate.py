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

## 排队号（2026-10-26 补，用户报「漫画台写真没进 QQ 的队列」）

原来只有抢占：谁 `mkdir` 快谁上。实测 15:22 那次网页侧**等了 12 分钟一次没抢到**
——QQ 的 worker 是「释放 → 立刻取下一张 → mkdir」，间隔毫秒级；网页侧每秒才试
一次。只要 QQ 队列不断粮，网页侧就被饿死。

现在等锁前先登记一张票（`state/comfy.queue/<纳秒时间戳>-<pid>-<rand>.ticket`），
**只有队首才有资格 mkdir**，先来先跑。票用 mtime 当心跳：持锁方靠 `Gate` 的
心跳线程刷，排队方每次轮询刷；所以「卡在排队里很久的人」不会被误判成僵尸。
只有心跳真的停了（进程被强杀）才在 `TICKET_STALE` 秒后清掉。

⚠️ **两个进程都要用上这一版才有效**：老进程不登记票，`mkdir` 一空就抢，
无视队首——那等于给它开了后门。升级后必须把 5174 和 QQ 一起重启。

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

# 排队号目录名（state/ 下）。文件名即顺序，谁最小谁是队首。
QUEUE_NAME = "comfy.queue"

# 排队号被判僵尸的门槛：票的 mtime 超过这么久没被刷过 = 等的人已经死了。
# 和锁用同一个数量级：持锁方每 HEARTBEAT(10s) 刷一次、排队方每 POLL(1s) 刷一次，
# 都远小于 90 秒；而真被强杀的进程留下的票最多挡 90 秒。
TICKET_STALE = 90.0

# 多久扫一次僵尸票。排队轮询是每秒一次，扫秒表不能跟着每秒扫（listdir 白花）。
TICKET_SWEEP_EVERY = 5.0

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


def queue_dir():
    """排队号目录。不存在 = 没人排队。"""
    return state_path(QUEUE_NAME)


def _ticket_path():
    """一张新票的路径。文件名自带顺序：纳秒时间戳定长 20 位 → 字典序即先后。

    后面两段（pid、随机串）只用来区分「同一纳秒」和排查时认出是谁，不参与
    排序（时间戳位数固定，排在前面）。
    """
    return os.path.join(queue_dir(), "%020d-%08d-%s.ticket"
                        % (time.time_ns(), os.getpid(), uuid.uuid4().hex[:8]))


def make_ticket():
    """登记一张票，返回路径。失败返回空串（调用方退化为纯抢占）。

    不抛异常：排队号是**公平性优化**，不是正确性前提。目录建不出来（权限、
    磁盘满）时宁可回到老行为继续出图，也不能让 ComfyUI 彻底用不了。
    """
    for _ in range(3):
        fp = _ticket_path()
        try:
            os.makedirs(queue_dir(), exist_ok=True)
            with open(fp, "x", encoding="utf-8"):
                pass
            return fp
        except FileExistsError:
            continue                    # 文件名撞了（纳秒级几乎不可能），重抽
        except OSError as e:
            log.warning("排队号登记失败（%s）——本次退回纯抢占", e)
            return ""
    log.warning("排队号连撞三次文件名——本次退回纯抢占")
    return ""


def _tickets():
    """当前所有排队号，**按先后排好序**。读不到目录 = 空。"""
    try:
        names = os.listdir(queue_dir())
    except OSError:
        return []
    return sorted(n for n in names if n.endswith(".ticket"))


def _touch(fp):
    """刷票的心跳（mtime）。持锁方和排队方都要刷，否则被当僵尸清掉。"""
    try:
        os.utime(fp, None)
    except OSError:
        pass


def _drop_ticket(fp):
    if not fp:
        return
    try:
        os.remove(fp)
    except OSError:
        pass


def _sweep_tickets():
    """清掉心跳已停的票（进程被强杀留下的）。别人清不到的自己下一轮再清。"""
    now = time.time()
    d = queue_dir()
    for name in _tickets():
        fp = os.path.join(d, name)
        try:
            if now - os.path.getmtime(fp) <= TICKET_STALE:
                continue
            os.remove(fp)
            log.warning("清掉一张僵尸排队号 %s（心跳已停超过 %.0f 秒）",
                        name, TICKET_STALE)
        except OSError:
            pass


def _my_turn(ticket):
    """轮到我了吗：是不是队首。

    票没了（被当僵尸清掉、或目录读不到）一律算「轮到我」——否则自己就永远
    等一张不存在的票，把这一格卡死到超时。
    """
    if not ticket:
        return True
    names = _tickets()
    if not names:
        return True
    me = os.path.basename(ticket)
    if me not in names:
        return True
    return names[0] == me


def _ahead(ticket):
    """前面还有几张票（只用来写日志）。"""
    if not ticket:
        return 0
    names = _tickets()
    me = os.path.basename(ticket)
    try:
        return max(0, names.index(me))
    except ValueError:
        return 0


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
    """一次持锁。用 hold() 拿，用 release() 放；也可以 with 语句。

    `ticket` 是这次持锁时占的排队号：持锁期间一直留着（不然别人会以为队首
    空了出来抢），释放时和锁一起摘掉——**而且必须是先摘锁、再摘票**，见
    `release` 里的注释。
    """

    def __init__(self, token, label, ticket=""):
        self.token = token
        self.label = label
        self.ticket = ticket
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
            # 票也要刷：一张 anima 大图能跑两三分钟，票的 mtime 停在开跑那一刻，
            # 排队的人会把它当僵尸清掉、然后一堆人同时来抢锁。
            _touch(self.ticket)

    def release(self):
        self._stop.set()
        d = _read_owner()
        if d and d.get("token") != self.token:
            # 已经不是自己的锁了，别删别人的——**但自己的票必须摘掉**。
            # 留着的话自己永远是队首，整条队谁也不许抢，全卡到 30 分钟超时。
            _drop_ticket(self.ticket)
            return
        # 顺序不能反：先还锁、再摘票。反过来的那一瞬，下一位成了队首、锁却还
        # 在，它会 mkdir 失败白等一轮；这个顺序下是最多一次 POLL 的交接延迟。
        try:
            fp = _owner_file()
            if os.path.isfile(fp):
                os.remove(fp)
            os.rmdir(lock_dir())
        except OSError:
            pass
        _drop_ticket(self.ticket)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.release()
        return False


def hold(label="", timeout=DEFAULT_WAIT):
    """拿锁（阻塞直到拿到或超时）。返回 Gate；超时抛 TimeoutError。

    label 只用于日志/排查（`state/comfy.lock/owner.json` 里能看到「谁在跑」）。

    排队（2026-10-26）：进来先登记一张票，**队首才有资格 mkdir**。票在自己
    拿到锁时移交给 Gate（持锁期间留着），超时/异常路径在 finally 里摘掉——
    不摘的话这张票会一直挡在队首，把后面所有人卡死。
    """
    start = time.time()
    warned = False
    ticket = ""
    ticket_tried = False
    swept_at = 0.0
    try:
        while True:
            if not ticket_tried:
                # 只试一次：登记失败（权限/磁盘）就老老实实退回纯抢占，
                # 别每轮 POLL 都重试一遍把日志刷满。
                ticket_tried = True
                ticket = make_ticket()
            if ticket:
                _touch(ticket)              # 排队中也要刷，别被当僵尸
                now = time.time()
                if now - swept_at > TICKET_SWEEP_EVERY:
                    swept_at = now
                    _sweep_tickets()
            if not _my_turn(ticket):
                if not warned:
                    log.info("ComfyUI 排队中：前面还有 %d 个（%s 等）",
                             _ahead(ticket), label or "未命名任务")
                    warned = True
                if time.time() - start >= timeout:
                    raise TimeoutError(
                        "等 ComfyUI 空出来超过 %.0f 分钟（%s，还在排队）"
                        % (timeout / 60.0, describe(holder())))
                time.sleep(POLL)
                continue
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
                log.info("取得 ComfyUI 独占（%s，排队等了 %.1f 秒）",
                         label or "未命名任务", waited)
            gate = Gate(token, label, ticket=ticket)
            ticket = ""                     # 票交给 Gate 了，finally 别再删
            gate._beat.start()
            return gate
    finally:
        _drop_ticket(ticket)
