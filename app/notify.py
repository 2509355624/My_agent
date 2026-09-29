"""掉线通知：NapCat 走到扫码界面时，把二维码推到手机微信。

为什么需要它：机器人掉线后**自己发不出任何消息** —— 掉线的号就是要发消息的
号。所以必须借助一个不在 QQ 里的通道。这里用 PushPlus（微信推送），手机扫码
绑定一次拿 token 即可。

**触发点只有一个：`cache/qrcode.png` 被重写。** 这个文件只在 NapCat 需要人工
扫码时才生成（快速登录成功不会写它），所以它的修改时间就是「必须扫码了」的
准确时刻 —— 不需要另外判断掉线类型，也不需要等一段时间去抖。

启动时只记基线、不补推旧文件；从那一刻起，文件一变就推。
"""

import base64
import json
import logging
import os
import threading
import time
import urllib.request

from app.config import (BASE_DIR, NOTIFY_PUSHPLUS_TOKEN, NOTIFY_QRCODE_PATH,
                        NOTIFY_SILENCE_HOURS, QQ_BOT_NAME)

log = logging.getLogger("notify")

_PUSHPLUS_URL = "https://www.pushplus.plus/send"

# 轮询间隔。掉线不是毫秒级事件，但「扫码通知慢了」是真实抱怨（09-29），
# 而每次 tick 只是 getmtime，3 秒粒度足够快又不费电。
_POLL_SECONDS = 3

# 推过一次后多久不再推。NapCat 的二维码有效期到了会重写同一个文件，没有这个
# 闸就会被反复轰炸 —— 而人扫码需要时间，轰炸只会让手机响个不停。
_COOLDOWN_SECONDS = 300

# 启动时二维码文件比这还旧，就当「上一轮的遗留」，不补推。
_STALE_SECONDS = 300

# 看门狗自动重启期间，通知统一由看门狗自己发 —— 它把「已重启」和「要扫码」
# 合成一条推出去。这个标记就是那个窗口期：文件在 = 看门狗正在处理，watcher
# 让位，同一次掉线就不会收到两条通知。看门狗处理完（或超时）会删掉它；
# 留 TTL 是防看门狗自己挂了、标记变成永久文件把通知通道堵死。
_RESTART_FLAG = os.path.join(BASE_DIR, "state", "restart.flag")
_RESTART_FLAG_TTL = 900

_lock = threading.Lock()
_reason_title = ""
_reason_desc = ""
_started = False

# 静默告警状态：最后一次收发消息的时刻 + 本轮静默期是否已推过。
# 启动即视为「刚有活动」——进程刚起来不该立刻告警。
_last_activity = time.time()
_silence_alerted = False


def note_activity():
    """qq_bot 每收到/发出一条消息就调一下（自己发的消息 NapCat 也会上报，
    所以只挂在收消息的入口就同时覆盖了收和发）。恢复活动会重置静默告警，
    下一次静默满时长可以再报。"""
    global _last_activity, _silence_alerted
    with _lock:
        _last_activity = time.time()
        _silence_alerted = False


def last_activity():
    """最后一次收发消息的时刻（状态后台用它算「多久没动静」）。"""
    with _lock:
        return _last_activity


def enabled():
    """没配 token 就整个功能关掉。"""
    return bool(NOTIFY_PUSHPLUS_TOKEN)


def mark_restarting():
    """看门狗触发自动重启时调用：这段窗口期的掉线通知由看门狗自己发。

    为什么需要：看门狗重启后要告诉用户「已重启 + 要扫码」，watcher 那边
    只知道「二维码变了」也会推一条 —— 两条通知、两条都不完整。用一个跨进程
    的标记文件把窗口期标出来，watcher 见到就让位。
    """
    try:
        os.makedirs(os.path.dirname(_RESTART_FLAG), exist_ok=True)
        with open(_RESTART_FLAG, "w", encoding="utf-8") as f:
            f.write("%.3f" % time.time())
    except OSError as e:
        log.warning("写重启标记失败：%s", e)


def clear_restarting():
    """看门狗处理完（恢复 / 要扫码 / 判定失败）后调用，交还通知权。"""
    try:
        os.remove(_RESTART_FLAG)
    except OSError:
        pass


def restarting():
    """看门狗是否正在自动重启（标记存在且没过期）。"""
    try:
        age = time.time() - os.path.getmtime(_RESTART_FLAG)
    except OSError:
        return False
    if age >= _RESTART_FLAG_TTL:
        log.info("重启标记已过期（%.0fs），按无效处理", age)
        return False
    return True


def note_offline_reason(title, desc):
    """由 qq_bot 在收到 bot_offline notice 时调用，把原因留给下一次推送。

    notice 和二维码文件是两个独立信号（一个说「被踢了」，一个说「要扫码」），
    到达顺序也不固定，所以用一个槽位把原因缓存起来，推送时取走。
    """
    global _reason_title, _reason_desc
    with _lock:
        _reason_title = (title or "").strip()
        _reason_desc = (desc or "").strip()


def _take_reason():
    global _reason_title, _reason_desc
    with _lock:
        t, d = _reason_title, _reason_desc
        _reason_title = _reason_desc = ""
    return t, d


def _mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


def _login_uin():
    """从 NapCat 的 webui.json 读出这个实例要登的号（autoLoginAccount）。

    二维码文件在 ...\\app\\napcat\\cache\\qrcode.png，往回两级就是 napcat 目录，
    webui.json 就在它下面的 config\\ 里 —— 不用再单独配一个路径。读不到就返回
    空串，推送里少个号而已，不能因为拿不到号就不推。
    """
    try:
        base = os.path.dirname(os.path.dirname(NOTIFY_QRCODE_PATH))
        with open(os.path.join(base, "config", "webui.json"),
                  encoding="utf-8") as f:
            return str(json.load(f).get("autoLoginAccount") or "").strip()
    except Exception:
        return ""


def _bot_label():
    """推送里用来指认「是谁掉了」的标签，形如 胡桃桃(3985441738)。

    一台机器上可能跑着不止一个号（小小怪 / 胡桃桃），光写「QQ 机器人掉线」
    根本分不清该去扫哪个号 —— 而二维码本身不编码账号，扫错就是把另一个号
    塞进这个实例。所以名字和 UIN 都带上。
    """
    name = (QQ_BOT_NAME or "").strip()
    uin = _login_uin()
    if name and uin:
        return "%s(%s)" % (name, uin)
    return name or uin or "QQ 机器人"


def _online():
    """探活：协议端还能取到登录信息就算在线。"""
    from app import qq_api          # 延迟 import，避免模块级循环依赖
    try:
        uid, _ = qq_api.check_alive(timeout=5)
        return bool(uid)
    except Exception:
        return False


def _build_content(qr_path, reason_title, reason_desc, intro=""):
    """正文是 HTML。二维码以 base64 内嵌，图随消息走，不依赖任何图床。

    intro 给定时用它当开头（看门狗要写「已自动重启」），否则用默认的
    「掉线了，需要重新扫码」——默认文案带上「是谁掉了」的标签（见 _bot_label），
    一台机器跑多个号（小小怪 / 胡桃桃）时才分得清该去扫哪个。
    """
    uin = _login_uin()
    parts = []
    if intro:
        parts.append("<p>%s</p>" % intro)
    else:
        parts.append("<p>%s <b>%s</b> 掉线了，需要重新扫码。</p>"
                     % (time.strftime("%m-%d %H:%M:%S"), _bot_label()))
    if reason_title or reason_desc:
        parts.append("<p>原因：%s %s</p>" % (reason_title, reason_desc))
    if qr_path and os.path.isfile(qr_path):
        try:
            with open(qr_path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode("ascii")
            parts.append('<p><img src="data:image/png;base64,%s" '
                         'style="width:260px"></p>' % b64)
        except OSError as e:
            log.warning("读取二维码失败：%s", e)
    else:
        parts.append("<p>（没拿到二维码文件）</p>")
    parts.append("<p>扫码方式：把图存到相册 → 手机 QQ → 扫一扫 → 右上角选相册"
                 "里的这张图。（微信自己的「识别图中二维码」扫不了 QQ 登录码）</p>")
    if uin:
        parts.append("<p>⚠ 二维码不绑定账号：手机上点确认前先看清楚是不是 "
                     "<b>%s</b>，别把别的号扫进来。</p>" % uin)
    return "".join(parts)


def _post(payload, timeout=20):
    """POST 到 PushPlus。

    显式用空 ProxyHandler：本机 Clash 会把代理写进注册表，而 urllib 在环境变量
    为空时会 fallback 到注册表设置 —— 那样请求会被拐进代理。PushPlus 是公网
    服务，实测直连即可，不该受本机代理影响。
    """
    req = urllib.request.Request(
        _PUSHPLUS_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=timeout) as resp:
        return resp.status, resp.read().decode("utf-8", "replace")


def push_offline(qr_path=None, reason_title="", reason_desc="",
                 title=None, intro=""):
    """推一条「掉线要扫码」到手机。返回 (ok, 说明)；失败只记日志，不抛。

    title / intro 留给看门狗：它把「已自动重启」和「要扫码」合成一条推出去，
    不想再套用「掉线了」那套默认文案。
    """
    if not enabled():
        return False, "未配置 PUSHPLUS_TOKEN"
    if not title:
        title = "%s 掉线，需要扫码" % _bot_label()
    payload = {
        "token": NOTIFY_PUSHPLUS_TOKEN,
        "title": title or "QQ 机器人掉线，需要扫码",
        "content": _build_content(qr_path or NOTIFY_QRCODE_PATH,
                                  reason_title, reason_desc, intro),
        "template": "html",
    }
    try:
        status, text = _post(payload)
    except Exception as e:
        log.warning("掉线通知推送异常：%s", e)
        return False, repr(e)
    ok = False
    try:
        ok = json.loads(text).get("code") == 200
    except (ValueError, TypeError):
        pass
    log.info("掉线通知推送%s：%s %s", "成功" if ok else "失败", status, text[:200])
    return ok, text[:200]


def push_silence(hours):
    """推一条「疑似冻结」到手机。抓的是冻而不掉：协议层活着、消息同步停摆
    （2026-09-27 事故，二维码机制完全无感）。返回 (ok, 说明)。"""
    if not enabled():
        return False, "未配置 PUSHPLUS_TOKEN"
    payload = {
        "token": NOTIFY_PUSHPLUS_TOKEN,
        "title": "%s 疑似冻结" % _bot_label(),
        "content": "<p>%s <b>%s</b> 已连续 %.0f 小时没有任何收发，但协议探活正常。</p>"
                   "<p>可能是消息同步停摆（冻而不掉），也可能是群里真的没人说话。</p>"
                   "<p>去群里喊它一声试试；真没反应就重启 NapCat（大概率要扫码，"
                   "二维码会自动推过来）。</p>"
                   % (time.strftime("%m-%d %H:%M:%S"), _bot_label(), hours),
        "template": "html",
    }
    try:
        status, text = _post(payload)
    except Exception as e:
        log.warning("静默告警推送异常：%s", e)
        return False, repr(e)
    ok = False
    try:
        ok = json.loads(text).get("code") == 200
    except (ValueError, TypeError):
        pass
    log.info("静默告警推送%s：%s %s", "成功" if ok else "失败", status, text[:200])
    return ok, text[:200]


def push_text(title, content):
    """推一条通用文本到手机（看门狗等用它报状态）。返回 (ok, 说明)。

    与 push_offline / push_silence 共用 PushPlus 通道；没配 token 时静默返回 False。
    """
    if not enabled():
        return False, "未配置 PUSHPLUS_TOKEN"
    payload = {
        "token": NOTIFY_PUSHPLUS_TOKEN,
        "title": title,
        "content": "<p>%s</p>" % content,
        "template": "html",
    }
    try:
        status, text = _post(payload)
    except Exception as e:
        log.warning("通用推送异常：%s", e)
        return False, repr(e)
    ok = False
    try:
        ok = json.loads(text).get("code") == 200
    except (ValueError, TypeError):
        pass
    log.info("通用推送%s：%s %s", "成功" if ok else "失败", status, text[:200])
    return ok, text[:200]


class _Watcher:
    """盯二维码文件的轮询器。

    状态只有两个：文件上次的修改时间、上次推送时刻。做成独立对象是为了让测试
    能同步驱动（tick 一次看结果），不必陪真线程玩时序。
    """

    def __init__(self, path=None, cooldown=_COOLDOWN_SECONDS):
        self.path = path or NOTIFY_QRCODE_PATH
        self.cooldown = cooldown
        # 启动基线：先记下当前值，不把它当成「刚发生的掉线」。
        self.last_mtime = _mtime(self.path)
        self.last_push = 0.0

    def prime(self):
        """启动时的判断：此刻是不是已经卡在待扫码状态？

        正常启动时二维码文件要么不存在、要么是上一轮的旧文件，两种情况都不推。
        但如果它**很新**且探活确认不在线，那就是「进程重启前就掉了、还没人扫」
        —— 这时要补一条，否则要等到下一次掉线才知道。
        """
        if not self.last_mtime:
            return False
        if time.time() - self.last_mtime >= _STALE_SECONDS:
            return False
        if _online():
            return False
        if restarting():
            log.info("看门狗正在自动重启并负责通知，跳过补推")
            return False
        log.info("启动时已是待扫码状态，补推一条")
        return self._push()

    def tick(self, now=None):
        """轮询一次；文件变了且过了冷却就推。返回是否推了。"""
        now = time.time() if now is None else now
        silence_pushed = self._check_silence(now)
        cur = _mtime(self.path)
        if cur is None or cur == self.last_mtime:
            return silence_pushed
        self.last_mtime = cur
        if restarting():
            # 先把 last_mtime 收下（免得让位期间攒着的旧码在窗口期结束后补推），
            # 但不推 —— 这一轮的二维码由看门狗随「已重启」一起发。
            log.info("看门狗正在自动重启并负责通知，跳过二维码推送")
            return silence_pushed
        if now - self.last_push < self.cooldown:
            log.info("二维码刷新了，但 %d 秒内推过，跳过", self.cooldown)
            return silence_pushed
        return self._push(now) or silence_pushed

    def _check_silence(self, now):
        """连续 NOTIFY_SILENCE_HOURS 小时零收发且探活正常 → 推「疑似冻结」。

        探活失败说明是真掉线，那是二维码路径的活，这里不抢。一个静默期内
        只推一次（恢复活动由 note_activity 重置）。宁可误报——半夜安静的
        群收到一条「疑似冻结」的代价，远小于真冻结无人知晓。
        """
        global _silence_alerted
        if NOTIFY_SILENCE_HOURS <= 0:
            return False
        with _lock:
            last, alerted = _last_activity, _silence_alerted
        if alerted or now - last < NOTIFY_SILENCE_HOURS * 3600:
            return False
        if not _online():
            return False
        with _lock:
            _silence_alerted = True
        log.warning("已静默 %.1f 小时但探活正常，推疑似冻结告警",
                    (now - last) / 3600)
        return push_silence((now - last) / 3600)

    def _push(self, now=None):
        t, d = _take_reason()
        ok, _ = push_offline(self.path, t, d)
        if ok:
            # now 由 tick 透传，好让测试用假时间驱动冷却窗口
            self.last_push = time.time() if now is None else now
        return ok


def _watch_loop():
    w = _Watcher()
    w.prime()
    while True:
        time.sleep(_POLL_SECONDS)
        try:
            w.tick()
        except Exception as e:         # 守护线程不能因为一次异常就静默死掉
            log.warning("掉线通知轮询异常：%s", e)


def start_watcher():
    """起守护线程。没配 token 就只记一条日志、不开线程。"""
    global _started
    if not enabled():
        log.info("未配置 PUSHPLUS_TOKEN，掉线通知关闭")
        return
    if _started:
        return
    _started = True
    threading.Thread(target=_watch_loop, daemon=True,
                     name="notify-watch").start()
    log.info("掉线通知已启用：盯 %s", NOTIFY_QRCODE_PATH)
