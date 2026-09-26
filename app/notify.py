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

from app.config import NOTIFY_PUSHPLUS_TOKEN, NOTIFY_QRCODE_PATH

log = logging.getLogger("notify")

_PUSHPLUS_URL = "https://www.pushplus.plus/send"

# 轮询间隔。掉线不是毫秒级事件，10 秒的粒度绰绰有余。
_POLL_SECONDS = 10

# 推过一次后多久不再推。NapCat 的二维码有效期到了会重写同一个文件，没有这个
# 闸就会被反复轰炸 —— 而人扫码需要时间，轰炸只会让手机响个不停。
_COOLDOWN_SECONDS = 300

# 启动时二维码文件比这还旧，就当「上一轮的遗留」，不补推。
_STALE_SECONDS = 300

_lock = threading.Lock()
_reason_title = ""
_reason_desc = ""
_started = False


def enabled():
    """没配 token 就整个功能关掉。"""
    return bool(NOTIFY_PUSHPLUS_TOKEN)


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


def _online():
    """探活：协议端还能取到登录信息就算在线。"""
    from app import qq_api          # 延迟 import，避免模块级循环依赖
    try:
        uid, _ = qq_api.check_alive(timeout=5)
        return bool(uid)
    except Exception:
        return False


def _build_content(qr_path, reason_title, reason_desc):
    """正文是 HTML。二维码以 base64 内嵌，图随消息走，不依赖任何图床。"""
    parts = ["<p>%s 掉线了，需要重新扫码。</p>" % time.strftime("%m-%d %H:%M:%S")]
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


def push_offline(qr_path=None, reason_title="", reason_desc=""):
    """推一条「掉线要扫码」到手机。返回 (ok, 说明)；失败只记日志，不抛。"""
    if not enabled():
        return False, "未配置 PUSHPLUS_TOKEN"
    title = "QQ 机器人掉线，需要扫码"
    payload = {
        "token": NOTIFY_PUSHPLUS_TOKEN,
        "title": title,
        "content": _build_content(qr_path or NOTIFY_QRCODE_PATH,
                                  reason_title, reason_desc),
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
        log.info("启动时已是待扫码状态，补推一条")
        return self._push()

    def tick(self, now=None):
        """轮询一次；文件变了且过了冷却就推。返回是否推了。"""
        now = time.time() if now is None else now
        cur = _mtime(self.path)
        if cur is None or cur == self.last_mtime:
            return False
        self.last_mtime = cur
        if now - self.last_push < self.cooldown:
            log.info("二维码刷新了，但 %d 秒内推过，跳过", self.cooldown)
            return False
        return self._push(now)

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
