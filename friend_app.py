# -*- coding: utf-8 -*-
"""群友页公网入口（独立进程，**只监听 127.0.0.1**，5175）。

## 为什么单独一个进程

5174 上挂着管理页、输出管理（含删除）、agent 聊天和全部 API，而且几乎没有
鉴权。隧道要是直接指向它，等于把这些一起送人。这个进程只做三件事：
托管一个静态页、发/验验证码、把 `api/friend/*` 原样转给 5174。

它自己**不 import** `image_jobs`——队列必须只有一份，见 `app/friend_api.py`
模块注释。转发时带 `X-Friend-Token`，那个值只有两个进程知道。

## 关于「真实 IP」

走 Cloudflare 隧道后 `request.remote_addr` 恒为 127.0.0.1，按它限流等于全员
共用一个桶。所以取客户端的顺序是 `CF-Connecting-IP` → `X-Forwarded-For`
第一段 → `remote_addr`。前两个头只有隧道会带；本机直连时退回 remote_addr。

## 启动

    <venv>\\python.exe friend_app.py

端口默认 5175，可用 `FRIEND_PORT` 覆盖；后端默认 127.0.0.1:5174，
可用 `FRIEND_BACKEND` 覆盖。
"""

import gzip
import hashlib
import json
import logging
import os
import threading
import time

import requests
from flask import (Flask, Response, g, jsonify, make_response, redirect, request,
                   send_from_directory, stream_with_context)

from app import friend_auth
from app import guest_view
from app.config import WEB_DIR, WORKSHOP_HTML
from app.logsetup import setup as log_setup

log = logging.getLogger("friend_app")

PORT = int(os.getenv("FRIEND_PORT", "5175"))
BACKEND = os.getenv("FRIEND_BACKEND", "http://127.0.0.1:5174").rstrip("/")

# 游客逛画廊（2026-11-04 用户要）：只读，没有「我的」和「自定义预设」，
# 但能从这里转去角色扮演。标记放在 5175 自己的 cookie 里，不进 5174 的
# 登录态——游客没有 QQ 号，本来就不该有一张「登录凭证」。
GUEST_COOKIE = "fr_guest"
GUEST_TTL = 7 * 86400

# 分享用的独立游客页（2026-11-06 用户要）：`/play` 下发这个标记，带标记的
# **未登录**访客整个浏览会话都被圈在 /play 里，改地址栏去 /、/gallery 也挡回。
# 已登录的站主不受页面锁影响——他还得进自己的画廊和管理页。
# 强制游客身份另走 `/api/solo/*` 专用代理，不用这个 cookie 判断，免得站主
# 自己打开过 /play 之后，普通 /persona 页的聊天也被当成游客。
SOLO_COOKIE = "fr_solo"
SOLO_PATH = "/play"

app = Flask(__name__, static_folder=None)
# 页面里没有任何外部资源，CSP 就按最紧的写：只允许自己和内联样式/脚本。
# 内联没办法——整个页面就一个文件，拆成 .js/.css 反而多两个请求。
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0

_SESSION = requests.Session()
_SESSION.trust_env = False          # 走系统代理只会绕远路


def _new_session():
    """每次请求一个干净 session。

    ⚠️ 2026-11-03 事故：网关原来所有转发共用一个 `_SESSION`（全局
    `requests.Session`）。`requests` 会**自动保存上游的 Set-Cookie 并在后续
    请求里自动带上**——于是：

        A 打开 /persona（无 cookie）→ 5174 签发 gs_id_A → 存进 _SESSION
        B 打开同一个链接 → _SESSION 自动带上 gs_id_A → 5174 认为 B 就是 A
        → **B 看到 A 的全部对话**

    实测复现：A 聊一句，B 打开就看到那 2 条消息。

    `requests` 的 cookie jar 是 session 级共享的，没法按请求隔离；
    显式传 `headers["Cookie"]` 也压不住它自己那份（会被合并）。
    所以**每个请求新建一个 session**，用完就 close，谁也别想蹭谁的 cookie。

    代价是失去连接复用。转发请求量很小（群友页），这个开销可以接受；
    图片流式那段另算，见 `_forward` 里的处理。
    """
    s = requests.Session()
    s.trust_env = False
    return s

# ─── 限流 ────────────────────────────────────────────────
#
# 内存计数即可：进程重启就清零，没人会盯着重启窗口刷。
# 三道闸：全局每 IP 每分钟、登录发码每 IP 每小时、登录校验每 IP 每 10 分钟。
_RL = {}
_RL_LOCK = threading.Lock()

LIMITS = {
    # 一次刷新页面 ≈ 1 次页面 + 3 次 API + N 张缩略图，给到 240 才不至于
    # 自己把自己挡住（原来是 120，网格一屏十几张就撞墙）
    "all": (240, 60),
    # 图片单独一口桶：画廊翻两屏就是几十张缩略图，跟「接口调用」混在一个
    # 计数里等于让看图把功能位吃掉。1500/分钟 对家用上行已经是天花板了。
    "img": (1500, 60),
    "send": (10, 3600),      # 发码：每小时 10 次
    "verify": (30, 600),     # 验码：10 分钟 30 次
    # 游客会话查看（2026-11-08）：管理员自己的入口，不需要太严。
    # 发码略放宽到 20/小时（他可能反复试登录），验码 30/10 分钟同群友页。
    "gv_send": (20, 3600),
    "gv_verify": (30, 600),
}


def client_ip():
    a = str(request.headers.get("CF-Connecting-IP") or "").strip()
    if a:
        return a
    xff = str(request.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
    if xff:
        return xff
    return str(request.remote_addr or "")


def rate_ok(bucket):
    """→ True 表示还能来一次。超了返回 False。"""
    limit, window = LIMITS[bucket]
    key = (bucket, client_ip())
    now = time.time()
    with _RL_LOCK:
        hits = [t for t in _RL.get(key, []) if now - t < window]
        if len(hits) >= limit:
            _RL[key] = hits
            return False
        hits.append(now)
        _RL[key] = hits
        if len(_RL) > 5000:                 # 防止长期运行把字典撑大
            for k in [k for k, v in _RL.items() if not v or now - v[-1] > 3600]:
                _RL.pop(k, None)
    return True


# ─── 响应头 ──────────────────────────────────────────────

def _secure(resp):
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data: blob:; "
        "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; "
        "form-action 'self'")
    if request.headers.get("X-Forwarded-Proto") == "https":
        resp.headers["Strict-Transport-Security"] = "max-age=15552000"
    # 图片缓存一年：item id 是一次性的（重传换新 id），同一个 URL 的字节不变，
    # 所以可以 immutable，翻回上一页、第二天再打开都不用重传。原先 10 分钟
    # 等于「每次进来都从头拉一遍」，几十张缩略图全走家用上行，慢得有道理。
    # private 不能去掉：图片要过登录态，存到 Cloudflare 边缘就等于泄漏。
    # 页面和接口一律 no-store——共享设备上退出登录后不该还能翻到东西。
    ctype = str(resp.headers.get("Content-Type") or "")
    if ctype.startswith("image/"):
        resp.headers["Cache-Control"] = (
            "private, max-age=31536000, immutable")
    elif (request.path or "").startswith("/workshop"):
        # 提示词工坊是 4.6MB 的单文件，no-store 等于每次刷新都从这台机器
        # 重新传一遍。文件换没换由 ETag 判（mtime 变 → ETag 变），这里只
        # 负责别让同一个人每进一次就重下一遍。
        resp.headers["Cache-Control"] = "private, max-age=600"
    else:
        resp.headers["Cache-Control"] = "no-store"
    return resp


@app.before_request
def _gate():
    bucket = "img" if ((request.path or "").startswith("/api/gal/img/")
                       or (request.path or "").startswith("/api/comic/img")
                       or (request.path or "").startswith("/api/outfit/img")) \
        else "all"
    if not rate_ok(bucket):
        return _secure(jsonify({"ok": False, "error": "请求太频繁，歇一下"})), 429
    # 独立游客页的隔离：只圈「没登录 + 进过 /play」的人，且只挡页面导航。
    # API 一律放行——聊天要靠它，其余接口本来就要求登录（见 _forward）。
    p = (request.path or "/").rstrip("/") or "/"
    # QQ「/画廊」「/漫画台」发出来的正式入口：先把 /play 的页面锁解开再按
    # 正常鉴权走。2026-11-06 回归：站主自己点过一次 /play 后，浏览器带着
    # 7 天的 fr_solo，再点 QQ 给的 /gallery 会被下面这条挡回游客页，看起来
    # 像「链接发错了」。带 from=qq 只可能是刚从 QQ 指令拿到的链接；访客在
    # /play 里手改地址栏到 /gallery（不带参数）仍然挡回，独立页的孤立性不变。
    # 2026-11-11 把 /comic 一并收进来：QQ 新增「/漫画台」指令，同一个坑别再踩。
    # 2026-11-12 /novel 同理：小说页上架后若加 QQ 直达链接，别让 fr_solo 挡回游客页。
    if (p in ("/gallery", "/gallery.html", "/comic", "/comic.html",
              "/novel", "/novel.html")
            and request.args.get("from") == "qq" and _solo_in()):
        g.clear_solo = True
        return None
    if (_solo_in() and not _logged_in()
            and not p.startswith("/api/")
            # `/char_intros.json` 是角色卡详情的静态数据（2026-11-09），跟
            # `/api/` 一样属于「页面要用到的东西」，拦成跳转的话独立页里
            # fetch 会拿到一段 HTML，JSON 解析失败 → 介绍节静默消失。
            # `/login/send|verify`（2026-11-11）：登录接口也得豁免。否则带着
            # fr_solo 的浏览器连验证码都发不出去、更登不进来——登录成功前
            # 不算 _logged_in，会被自己这条页面锁挡回 /play，登录这条路是死的。
            and p not in (SOLO_PATH, SOLO_PATH + ".html", "/health", "/logout",
                          "/login/send", "/login/verify",
                          "/char_intros.json", "/outfit", "/outfit.html")):
        return redirect(SOLO_PATH)
    return None


# ─── HTML 统一 gzip（2026-11-06）─────────────────────────
# 隧道 RTT 本来就 3~4 秒，聊天页 148KB 明文还要再传十几秒。页面里全是
# 重复的标签和样式，gzip 一般能压到 1/5。放在统一出口压，路由一行不用改：
# send_from_directory 的静态页、5174 反代回来的 persona.html、错误页都覆盖。
# /workshop 自己已经压过（带 Content-Encoding），这里跳过，免得压两遍。
#
# 不改 HTTP 缓存口径：页面仍旧 no-store（B 方案没做），压缩只省传输字节。
# 压缩结果按内容缓存，省掉每个请求现压的 CPU；key 是内容 hash，改了文件
# 自然换 key，不用记得清缓存。
_gz_lock = threading.Lock()
_gz_cache = {}          # sha1(raw) -> gz


def _gzip_html(resp):
    if resp.status_code != 200 or resp.headers.get("Content-Encoding"):
        return resp
    if not str(resp.headers.get("Content-Type") or "").startswith("text/html"):
        return resp
    # 同一个 URL 可能回两种字节，中间的代理 / CDN 必须按这个头分开存
    resp.headers["Vary"] = "Accept-Encoding"
    if "gzip" not in (request.headers.get("Accept-Encoding") or "").lower():
        return resp
    # send_from_directory 走 direct_passthrough（文件句柄直接交给 WSGI），
    # 想读字节得先关掉直传；万一读不出来就原样发，绝不为了压缩弄坏页面。
    was_direct = resp.direct_passthrough
    if was_direct:
        resp.direct_passthrough = False
    try:
        raw = resp.get_data()
    except Exception:
        resp.direct_passthrough = was_direct
        return resp
    if not raw:
        return resp
    key = hashlib.sha1(raw).digest()
    with _gz_lock:
        gz = _gz_cache.get(key)
        if gz is None:
            gz = gzip.compress(raw, 6)
            if len(_gz_cache) >= 24:
                _gz_cache.clear()
            _gz_cache[key] = gz
    resp.set_data(gz)
    resp.headers["Content-Encoding"] = "gzip"
    if "ETag" in resp.headers:
        del resp.headers["ETag"]     # 原文的 ETag 对压缩后的字节不成立
    resp.content_length = len(gz)
    return resp


@app.after_request
def _after(resp):
    if getattr(g, "clear_solo", False):
        resp.delete_cookie(SOLO_COOKIE, path="/")
    return _gzip_html(_secure(resp))


# ─── 登录 ────────────────────────────────────────────────

def _cookie_secure():
    """隧道下来是 https，Cookie 必须加 Secure；本机 http 调试时不能加。"""
    return request.headers.get("X-Forwarded-Proto") == "https"


@app.route("/login/send", methods=["POST"])
def login_send():
    if not rate_ok("send"):
        return jsonify({"ok": False, "error": "发太多次了，等一会儿"}), 429
    data = request.get_json(silent=True) or {}
    ok, msg = friend_auth.send_code(data.get("qq"))
    return jsonify({"ok": ok, "message": msg, "error": "" if ok else msg}), \
        (200 if ok else 400)


@app.route("/login/verify", methods=["POST"])
def login_verify():
    if not rate_ok("verify"):
        return jsonify({"ok": False, "error": "试太多次了，等一会儿"}), 429
    data = request.get_json(silent=True) or {}
    ok, msg, token = friend_auth.verify(data.get("qq"), data.get("code"))
    if not ok:
        return jsonify({"ok": False, "error": msg}), 400
    resp = make_response(jsonify({"ok": True}))
    resp.set_cookie(friend_auth.COOKIE_NAME, token,
                    max_age=friend_auth.COOKIE_TTL, httponly=True,
                    samesite="Lax", secure=_cookie_secure(), path="/")
    # 登录成功顺手解开 /play 的页面锁（2026-11-11）：用户之前点过一次 /play
    # 就带着 7 天 fr_solo，若只发登录 cookie 不清它，一旦登录态过期/换设备
    # 没登录，_gate 立刻把人弹回游客页，看起来像「又变回游客界面」。
    # 这一句让「重新登录」成为可靠的解锁路径（和 /play?exit=1、/logout 齐平）。
    resp.delete_cookie(SOLO_COOKIE, path="/")
    return resp


@app.route("/logout", methods=["POST"])
def logout():
    resp = make_response(jsonify({"ok": True}))
    resp.delete_cookie(friend_auth.COOKIE_NAME, path="/")
    resp.delete_cookie(GUEST_COOKIE, path="/")
    # 退出登录顺手解开 /play 的页面锁：站主想回自己的画廊不用去清浏览器 cookie。
    resp.delete_cookie(SOLO_COOKIE, path="/")
    return resp


@app.route("/me")
def me():
    qq = friend_auth.check_cookie(request.cookies.get(friend_auth.COOKIE_NAME))
    return jsonify({"ok": True, "qq": qq,
                    "guest": bool(not qq and _guest_in())})


# ─── 页面 ────────────────────────────────────────────────

def _logged_in():
    return bool(friend_auth.check_cookie(
        request.cookies.get(friend_auth.COOKIE_NAME)))


def _guest_in():
    """游客标记。只在没登录时算数：真登录之后游客态自然失效。"""
    return bool(not _logged_in()
                and request.cookies.get(GUEST_COOKIE) == "1")


def _solo_in():
    """进过独立游客页（/play）的标记。和登录态无关：站主自己点开也要算。"""
    return request.cookies.get(SOLO_COOKIE) == "1"


@app.route("/guest")
def guest_entry():
    """游客入口：发一个只读标记，再进画廊。

    只读是服务端强制的（5174 的写接口一律要 qq），不是前端藏按钮就算——
    藏起来只能防手滑，防不住改 URL。
    """
    resp = make_response(redirect("/"))
    resp.set_cookie(GUEST_COOKIE, "1", max_age=GUEST_TTL, httponly=True,
                    samesite="Lax", secure=_cookie_secure(), path="/")
    return resp


@app.route("/")
@app.route("/index.html")
def page():
    """根路径 = 画廊（2026-11-01 用户拍板：画廊是核心，预设降成二级入口）。

    未登录仍旧回 `friend.html`——它本身就是登录页（`#login` 和 `#app` 两态
    同一个文件），登录成功后由前端 `location.reload()` 回来，服务器这时
    才看得到 cookie，才会发画廊。带游客标记的人直接进画廊（只读）。
    """
    if not _logged_in() and not _guest_in():
        return send_from_directory(WEB_DIR, "friend.html")
    return send_from_directory(WEB_DIR, "gallery.html")


@app.route("/preset")
@app.route("/preset.html")
def preset_page():
    """自定义预设（原根页面）：调 LoRA 组合、存预设、直接出图。

    已登录/未登录都发同一个文件，它自己按 `/me` 的结果切两态。
    """
    return send_from_directory(WEB_DIR, "friend.html")


@app.route("/gallery")
@app.route("/gallery.html")
def gallery_page():
    """老地址，保留：QQ 里已经发出去的链接、群友的收藏都还指着它。"""
    if not _logged_in() and not _guest_in():
        return send_from_directory(WEB_DIR, "friend.html")
    return send_from_directory(WEB_DIR, "gallery.html")


# ─── 提示词工坊 ──────────────────────────────────────────
#
# 一个 4.6MB 的单文件 HTML，**不是我们写的**，原样发出去（一个字节都不改，
# 好让以后换 1.3.6 时只是换个路径）。它是纯前端：不联网、没有外部资源，
# 状态存在自己的 localStorage 里，所以不需要任何后端配合。
#
# 两件事必须做，否则手机上基本打不开：
#
# 1. **gzip**：那份文件九成是标签库的 JSON 和重复的样式，压完 600KB 上下。
#    走家用上行 + 隧道，4.6MB 要三秒多，600KB 一秒内。压一次缓存住，
#    按文件 mtime 失效——每次现压要 0.3 秒 CPU，还占着请求线程。
# 2. **能不能缓存**：`_secure` 默认把页面设成 `no-store`（共享设备上退出
#    登录后不该还翻得到东西），但这条对几 MB 的单文件等于「每次刷新重传
#    一遍」。这里单独放行，交给 ETag 判断「文件到底换没换」。
_ws_lock = threading.Lock()
_ws_cache = {}          # path -> (mtime, raw, gz)


def _workshop_payload(path):
    mt = os.path.getmtime(path)
    with _ws_lock:
        got = _ws_cache.get(path)
        if got and got[0] == mt:
            return got
        with open(path, "rb") as f:
            raw = f.read()
        got = (mt, raw, gzip.compress(raw, 6))
        _ws_cache[path] = got
        return got


@app.route("/workshop")
@app.route("/workshop.html")
def workshop_page():
    """提示词工坊。**不登录也能看**（2026-11-01 用户拍板，改掉了当天早些
    时候「登录可见」的版本）。

    它是个纯前端单文件：不调接口、不读登录态、草稿存在浏览器本地，所以
    登录与否看到的东西一模一样，挡着它只会白让群友多收一次验证码。

    代价得记住：这条路径等于对「拿到隧道链接的任何人」公开，里面那套
    1197 个 NSFW 标签也跟着公开。链接只在私聊和群里出现，但转发出去就
    拦不住了——这是用户明确接受的前提，不是漏掉的判断。
    """
    path = WORKSHOP_HTML
    if not path or not os.path.isfile(path):
        return _workshop_missing()
    try:
        mt, raw, gz = _workshop_payload(path)
    except OSError:
        log.warning("提示词工坊读不出来：%s", path, exc_info=True)
        return _workshop_missing()

    use_gz = "gzip" in (request.headers.get("Accept-Encoding") or "").lower()
    body = gz if use_gz else raw
    resp = app.response_class(body, mimetype="text/html")
    if use_gz:
        resp.headers["Content-Encoding"] = "gzip"
    # 同一个 URL 两种字节，缓存/代理必须按这个头分开存
    resp.headers["Vary"] = "Accept-Encoding"
    resp.set_etag("%x-%x%s" % (int(mt), len(raw), "z" if use_gz else ""))
    resp.content_length = len(body)
    return resp.make_conditional(request)


def _workshop_missing():
    """没配、或文件不在了：说人话，别把本机路径吐给群友。"""
    return app.response_class(
        "<!doctype html><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<body style=\"background:#0f1116;color:#dfe3ec;"
        "font:15px/1.75 system-ui,sans-serif;padding:44px 22px\">"
        "提示词工坊还没配上。<br>"
        "<span style=\"color:#7e8699;font-size:13px\">"
        "在 .env 里加一行 WORKSHOP_HTML=工坊 html 的完整路径，"
        "再重启群友页。</span></body>",
        status=404, mimetype="text/html")


@app.route("/health")
def health():
    return jsonify({"ok": True})


# ─── 转发 ────────────────────────────────────────────────

def _forward(prefix, sub, allow_guest=False, force_guest=False,
             post_timeout=60):
    """把请求原样转给 5174，并补上登录态里的 QQ 号。

    `qq` 一律由这里写死塞进去（参数或表单），**不信客户端传来的 qq**——
    否则谁都能改个 qq 就看别人的东西。

    `allow_guest`（2026-11-03）：角色扮演那条要放游客进来。游客没有 qq，
    转发时不塞 `qq`——5174 那边收到空 qq 会走游客分支（见
    `persona_api._who()`），身份由它自己签发的 `gs_id` cookie 认。
    cookie 要**整条带过去**，不然 5174 认不出同一个游客。

    `force_guest`（2026-11-06）：独立游客页 `/play` 的专用代理用。哪怕浏览器
    里有登录 cookie 也不注入 QQ，保证从分享链接进去的人一定是游客、看不到
    站主自己的线。只作用于 `/api/solo/*`，普通 `/persona` 页不受影响。
    """
    qq = friend_auth.check_cookie(request.cookies.get(friend_auth.COOKIE_NAME))
    if force_guest:
        qq = ""
    if not qq and not allow_guest:
        return jsonify({"ok": False, "error": "还没登录"}), 401
    url = "%s%s/%s" % (BACKEND, prefix, sub)
    headers = {"X-Friend-Token": friend_auth.secret()}
    params = dict(request.args)

    # 游客身份 cookie 必须透传：5175 不解析它，只负责原样送到 5174。
    gs = request.cookies.get("gs_id")
    if gs:
        headers["Cookie"] = "gs_id=" + gs

    if qq:
        params["qq"] = qq
    else:
        # 游客请求不能自带 qq：否则改个参数就能看别人的「我的」和私密条目
        params.pop("qq", None)

    # 下载原图时浏览器和下载器会带 Range（断点续传、多线程分段）。不转发出去
    # 就退化成每次都整包重传，一张 8MB 的图白下好几遍。
    for h in ("Range", "If-None-Match", "If-Modified-Since"):
        if request.headers.get(h):
            headers[h] = request.headers[h]

    # 图片走流式。requests 默认 stream=False 会把整个响应体读进内存，一张
    # 26MB 的原图在 5175 里就多占一份，几个人同时下能把这个进程顶爆。
    # 只对图片这么干：接口响应都很小，流式没有收益，反而多一层生成器。
    streaming = (request.method == "GET"
                 and ((request.path or "").startswith("/api/gal/img/")
                      or (request.path or "").startswith("/api/moss/img")
                      or (request.path or "").startswith("/api/comic/img")
                      or (request.path or "").startswith("/api/image/web/")
                      or (request.path or "").startswith(
                          "/api/image/角色扮演生图/")))
    #  ↑ moss 漫画图单张 2MB+（2026-11-03 实测），persona 的 CG 单张 8MB+
    #    （2026-11-04 实测），一并流式。2026-11-08 扮演 CG 改落独立的
    #    `角色扮演生图/` 目录，这一支也得流式，否则 5175 会先把 8MB 读进内存。
    # 每个请求一个干净 session，用完就关（见 `_new_session` 的说明）。
    # 绝不能复用全局 session：那会把上一个用户的 cookie 带给下一个用户。
    sess = _new_session()
    try:
        if request.method == "GET":
            r = sess.get(url, params=params, headers=headers,
                         timeout=(10, 600), stream=streaming)
        elif request.method == "DELETE":
            r = sess.delete(url, params=params, headers=headers, timeout=60)
        elif request.mimetype == "multipart/form-data":
            # 上传作品集：文件必须原样转发（不能先读进内存再重编码——
            # 几百 MB 一次读进来会把 5175 顶爆）。requests 支持传 fileobj。
            files = [(k, (f.filename, f.stream, f.mimetype))
                     for k, f in request.files.items(multi=True)]
            form = request.form.to_dict()
            if qq:
                form["qq"] = qq
            else:
                form.pop("qq", None)
            r = sess.post(url, params=params, data=form, files=files,
                          headers=headers, timeout=300)
        else:
            body = request.get_json(silent=True) or {}
            if qq:
                body["qq"] = qq
            else:
                body.pop("qq", None)
            r = sess.post(url, params=params, json=body,
                          headers=headers, timeout=post_timeout)
    except requests.RequestException as e:
        sess.close()
        log.warning("转发失败 %s：%s", sub, e)
        return jsonify({"ok": False, "error": "后台没响应，稍后再试"}), 502
    ctype = r.headers.get("Content-Type") or "application/json"

    if streaming and ctype.startswith("image/"):
        def _pump():
            try:
                for chunk in r.iter_content(64 * 1024):
                    if chunk:
                        yield chunk
            finally:
                r.close()          # 客户端中途取消也要把上游连接放掉
                sess.close()       # 这个请求专属的 session 一起收掉
        resp = Response(stream_with_context(_pump()), status=r.status_code,
                        content_type=ctype)
        # 这几个头必须照抄：少了 Content-Length 浏览器不知道进度，
        # 少了 Content-Range 断点续传直接坏。
        for h in ("Content-Length", "Content-Range", "Accept-Ranges",
                  "ETag", "Last-Modified"):
            if r.headers.get(h):
                resp.headers[h] = r.headers[h]
        return resp

    try:
        resp = Response(r.content, status=r.status_code, content_type=ctype)
    except Exception:
        sess.close()
        return jsonify({"ok": False, "error": "后台返回看不懂"}), 502
    sess.close()

    # 把 5174 下发的游客 cookie 转给浏览器（2026-11-03）。
    # 不转的话，游客每发一句话都被当成新游客，永远停在第一轮。
    _relay_guest_cookie(resp, r)
    return resp


def _relay_guest_cookie(resp, upstream):
    """照抄上游的 `gs_id` 游客 cookie（2026-11-03）。

    第一版读 `upstream.raw.headers.getlist("Set-Cookie")`，实测**拿不到**
    ——响应体已被 `r.content` 消费，raw 头在这一步不可靠。
    改成问 `requests` 要它已经解析好的 cookie，稳得多。

    这个 cookie 必须转给浏览器：不转的话，游客每发一句话都被当成新游客，
    15 轮配额永远用不完，「体验版」形同虚设。
    """
    try:
        val = upstream.cookies.get("gs_id")
    except Exception:                              # noqa: BLE001
        val = None
    if val:
        resp.set_cookie("gs_id", val, max_age=30 * 24 * 3600,
                        httponly=True, samesite="Lax", path="/")


@app.route("/api/friend/<path:sub>", methods=["GET", "POST", "DELETE"])
def proxy(sub):
    return _forward("/api/friend", sub)


# ─── 游客会话查看（管理员只读，2026-11-08）─────────────────
#
# 和群友页共用登录（`friend_auth` 那套 6 位码 + HMAC cookie），只多一道
# 「这个 QQ 在不在 `state/guest_admins.json` 里」。
# 数据不在这里读：转发到 5174 的 `persona_admin`，再过滤成只剩游客——
# 5174 那边的读法已经处理了「跨天合并、归档优先」这些细节，抄一遍必漏。

def _gv_qq():
    return friend_auth.check_cookie(request.cookies.get(friend_auth.COOKIE_NAME))


def _gv_admin():
    """当前 cookie 对应的 QQ，是管理员才返回；否则空串。"""
    qq = _gv_qq()
    return qq if (qq and guest_view.is_admin(qq)) else ""


@app.route("/guest-view")
@app.route("/guest-view.html")
def guest_view_page():
    # `?out=1`：清掉登录 cookie 再回来（2026-11-08 用户反馈）。
    # 浏览器记着 `fr_sess` 就不给登录框，手机上没有「清站点数据」这种入口，
    # 给个地址最省事。清完跳到不带参数的地址，避免死循环。
    if request.args.get("out"):
        resp = make_response(redirect("/guest-view"))
        resp.delete_cookie(friend_auth.COOKIE_NAME, path="/")
        return resp
    return send_from_directory(WEB_DIR, "guest_view.html")


@app.route("/gv/me")
def gv_me():
    qq = _gv_qq()
    return jsonify({"ok": True, "qq": qq,
                    "admin": bool(qq and guest_view.is_admin(qq))})


@app.route("/gv/send", methods=["POST"])
def gv_send():
    if not rate_ok("gv_send"):
        return jsonify({"ok": False, "error": "发太多次了，等一会儿"}), 429
    data = request.get_json(silent=True) or {}
    qq = str(data.get("qq") or "").strip()
    # 不区分「不在名单」和「不在好友」——统一回「无法登录」，不给枚举空间。
    if not guest_view.is_admin(qq):
        return jsonify({"ok": False, "error": "无法登录"}), 403
    ok, msg = friend_auth.send_code(qq)
    return jsonify({"ok": ok, "message": msg, "error": "" if ok else msg}), \
        (200 if ok else 400)


@app.route("/gv/verify", methods=["POST"])
def gv_verify():
    if not rate_ok("gv_verify"):
        return jsonify({"ok": False, "error": "试太多次了，等一会儿"}), 429
    data = request.get_json(silent=True) or {}
    qq = str(data.get("qq") or "").strip()
    if not guest_view.is_admin(qq):
        return jsonify({"ok": False, "error": "无法登录"}), 403
    ok, msg, token = friend_auth.verify(qq, data.get("code"))
    if not ok:
        return jsonify({"ok": False, "error": msg}), 400
    resp = make_response(jsonify({"ok": True}))
    resp.set_cookie(friend_auth.COOKIE_NAME, token,
                    max_age=friend_auth.COOKIE_TTL, httponly=True,
                    samesite="Lax", secure=_cookie_secure(), path="/")
    return resp


@app.route("/gv/list")
def gv_list():
    if not _gv_admin():
        return jsonify({"ok": False, "error": "没登录"}), 401
    try:
        r = requests.get(BACKEND + "/api/persona_admin/list",
                         headers={"X-Friend-Token": friend_auth.secret()},
                         timeout=15)
        d = r.json()
    except Exception as e:                            # noqa: BLE001
        return jsonify({"ok": False, "error": "后端读不到：%s" % e}), 502
    # 2026-11-08：11-05 起游客改走 persona 存储，线键是 `guest_<uuid>__<卡>`，
    # 5174 把它们标成 source="file"；只捞 source=="guest" 就只剩 11-03 老目录，
    # 页面看着像「记录不更新」。两类都收，老记录照常显示。
    rows = []
    for x in (d.get("sessions") or []):
        key = str(x.get("key") or "")
        is_new = key.startswith("guest_")
        if x.get("source") != "guest" and not is_new:
            continue
        if is_new:
            x = dict(x)                       # 别改到后端返回的原对象
            uid = key[len("guest_"):].split("__", 1)[0]
            x["label"] = "游客 %s" % uid[:8]  # 5174 的 _label 会写成「游客 guest_8b」
            x["short"] = "游客"
        rows.append(x)
    return jsonify({"ok": True, "total": len(rows), "sessions": rows})


@app.route("/gv/detail")
def gv_detail():
    if not _gv_admin():
        return jsonify({"ok": False, "error": "没登录"}), 401
    key = str(request.args.get("key") or "").strip()
    # 裸 uuid = 老目录（跨天合并，走 guest 分支）；`guest_*` = 11-05 之后的
    # persona 线，历史在 persona_archive_<key>.jsonl，得让 5174 走 file 分支。
    src = "file" if key.startswith("guest_") else "guest"
    try:
        r = requests.get(BACKEND + "/api/persona_admin/detail",
                         params={"key": key, "source": src},
                         headers={"X-Friend-Token": friend_auth.secret()},
                         timeout=20)
    except Exception as e:                            # noqa: BLE001
        return jsonify({"ok": False, "error": "后端读不到：%s" % e}), 502
    return Response(r.content, status=r.status_code,
                    content_type=r.headers.get("Content-Type"))


@app.route("/api/gal/<path:sub>", methods=["GET", "POST", "DELETE"])
def proxy_gal(sub):
    # 游客能看画廊（只读）：必须先在登录页点 /guest 拿到标记，凭空打接口仍 401。
    # 下游 5174 的写接口一律要 qq，所以游客这一支只能读。
    return _forward("/api/gal", sub, allow_guest=_guest_in())


# ─── 角色扮演（2026-11-03）──────────────────────────────
#
# 用户定的架构：**角色扮演必须走 5175**，5174 只在隧道后面，绝不直接暴露
# （5174 上有管理页）。所以这里加一层网关：
#   · 页面 `/persona` 从 5174 拉 HTML，**不转发 cookie 校验**，游客也能进
#   · 页面 `/play` 是同一份 HTML 的分享版（2026-11-06）：下发隔离标记，前端
#     收掉所有外链，其它页面路由把这个访客挡回 /play
#   · 接口 `/api/persona/*` 转发，登录用户注入 qq，游客透传 `gs_id`
#   · 接口 `/api/solo/*` 只给 /play 用：一律不注入 qq，强制游客
#
# 限额（15 轮）和游客身份都在 5174 的 `persona_api` 里做——网关只管转发，
# 不掺和业务判断，两边职责不重叠。

def _persona_response():
    """从 5174 拉角色扮演页 HTML，原样发出去（两个入口共用同一份）。"""
    try:
        sess = _new_session()
        r = sess.get(BACKEND + "/persona",
                     headers={"X-Friend-Token": friend_auth.secret()},
                     timeout=(10, 30))
        sess.close()
        return Response(r.content, status=r.status_code,
                        content_type="text/html; charset=utf-8")
    except requests.RequestException as e:
        log.warning("取角色扮演页失败：%s", e)
        return "后台没响应，稍后再试", 502


@app.route("/persona")
@app.route("/persona.html")
def persona_page():
    """角色扮演页。未登录也放行——5174 那边会按游客处理。"""
    return _persona_response()


@app.route("/comic")
@app.route("/comic.html")
def comic_page():
    """群友漫画工作台（2026-11-10 开放）。**必须登录**。

    没登录时发的是 `friend.html`（它本身就是登录页，验证成功后 `location.reload()`
    又回到这个地址，服务器这时才看到 cookie、才发漫画台）。游客态一律不给：
    剧本生成要花站主的 Token，后续出图还要占整条 GPU 队列，不能匿名触发。

    接口经 `/api/comic/<path>` 反代到 5174，`_forward` 会把登录 Cookie 解出的
    QQ 写死塞进请求；页面自己不需要也不能传 qq。
    """
    if not _logged_in():
        return send_from_directory(WEB_DIR, "friend.html")
    return send_from_directory(WEB_DIR, "comic_studio_friend.html")


@app.route("/api/comic/<path:sub>", methods=["GET", "POST", "DELETE"])
def proxy_comic(sub):
    """漫画台接口反代。不放游客（allow_guest 默认 False）。

    5174 那边只认「X-Friend-Token + qq」组合：带令牌又有 qq = 群友，进配额、
    归属、缩略图、跳过 2x 那套；站主直连 5174 没有令牌 = 全部放行、行为不变。
    `/api/comic/admin` 即使被转发过来，也会因 `_qq()` 非空被 5174 拒掉。
    """
    return _forward("/api/comic", sub)


# ── 小说模式（2026-11-12 上架）────────────────────────────
#
# 页面跟 /persona 一样从 5174 拉**同一份** novel.html，不复制、不做派生页：
# 以后改前端，站主和群友两边一起生效。接口逐条反代，登录用户由 _forward
# 写死注入 QQ，5174 侧 novel_api 靠它做世界书归属隔离与群友计费。
#
# 必须登录：一轮要跑编剧 + 写作两次 LLM，不能匿名替站主烧 Token。
# 不放游客，也不许带 fr_solo 的独立游客页进来（_gate 的页面锁照旧生效）。

def _novel_response():
    """从 5174 拉小说页 HTML，原样发出去。"""
    try:
        sess = _new_session()
        r = sess.get(BACKEND + "/novel",
                     headers={"X-Friend-Token": friend_auth.secret()},
                     timeout=(10, 30))
        sess.close()
        return Response(r.content, status=r.status_code,
                        content_type="text/html; charset=utf-8")
    except requests.RequestException as e:
        log.warning("取小说页失败：%s", e)
        return "后台没响应，稍后再试", 502


@app.route("/novel")
@app.route("/novel.html")
def novel_page():
    if not _logged_in():
        return send_from_directory(WEB_DIR, "friend.html")
    return _novel_response()


@app.route("/api/novel/<path:sub>", methods=["GET", "POST", "DELETE"])
def proxy_novel(sub):
    """小说接口反代。POST 单独放宽到 300s（一轮两次 LLM）。

    原来是全场统一 60s，而 LLM_REQUEST_TIMEOUT 单次就是 60s —— 小说一轮
    串行两次，群友很容易收到「后台没响应」但正文其实已经落盘了。前端每轮
    带 turn_token，超时重发同一个 token 会拿到上一轮结果，不会重复写。
    """
    return _forward("/api/novel", sub, post_timeout=300)


# ── 换装页（2026-11-11）──────────────────────────────────
#
# 「/换装」QQ 命令回的链接指向这里。它**免登录**：令牌本身是 QQ 可信通道发的
# 凭证（含来源 + 过期 + HMAC 签名），网页提交时带上，5174 就能把结果推回原会话。
# 所以页面路由直接发 HTML，接口反代走 allow_guest=True（不注入 qq、不带登录 cookie
# 也放行）——换装不需要账号体系，分享给别人也能用，滥用由令牌时效 + 接口限流兜。

@app.route("/outfit")
@app.route("/outfit.html")
def outfit_page():
    return send_from_directory(WEB_DIR, "outfit.html")


@app.route("/api/outfit/<path:sub>", methods=["GET", "POST", "DELETE"])
def proxy_outfit(sub):
    """换装接口反代。allow_guest=True：令牌即凭证，不注入 qq。"""
    return _forward("/api/outfit", sub, allow_guest=True)


@app.route("/play")
@app.route("/play.html")
def play_page():
    """分享用的独立游客页（2026-11-06 用户要）。

    和 /persona 是**同一份 HTML**：前端靠 `location.pathname` 认出孤立态、收掉
    画廊 / 登录 / 角色管理这些外链；这里只负责下发隔离标记（见 _gate）。
    不复制页面 = 以后改聊天页，两个入口一起生效。

    `?exit=1` 是给站主 / 误入者的解除口：清掉标记后回首页。页面上没有这个
    入口，访客不会自己走到。
    """
    if request.args.get("exit") == "1":
        resp = make_response(redirect("/"))
        resp.delete_cookie(SOLO_COOKIE, path="/")
        return resp
    resp = _persona_response()
    if isinstance(resp, Response) and resp.status_code == 200:
        resp.set_cookie(SOLO_COOKIE, "1", max_age=GUEST_TTL, httponly=True,
                        samesite="Lax", secure=_cookie_secure(), path="/")
    return resp


@app.route("/api/persona/<path:sub>", methods=["GET", "POST", "DELETE"])
def proxy_persona(sub):
    return _forward("/api/persona", sub, allow_guest=True)


@app.route("/api/solo/<path:sub>", methods=["GET", "POST", "DELETE"])
def proxy_solo(sub):
    """独立游客页专用：一律游客，哪怕浏览器里带着登录 cookie。"""
    return _forward("/api/persona", sub, allow_guest=True, force_guest=True)


# ─── 角色扮演的漫画图（2026-11-03）──────────────────────
#
# persona 页出的漫画图走 `/api/moss/img`（persona_api.comic_status 返回的
# url 就是它）。这条不转发的话，手机端（5175/隧道）请求全部 404 → 气泡
# 下面永远是一张破图，桌面端（直连 5174）反而正常——用户实测就是这样。
#
# ⚠️ 只放这一条 GET：`/api/moss/*` 其余（render/delete/spec/list）是漫画
# 管理台的接口，不能跟着暴露到公网。img 本身只读，路径钉死在
# `state/moss_comic` 的任务目录里（moss_api._task_dir 校验），安全。

@app.route("/api/moss/img", methods=["GET"])
def proxy_moss_img():
    return _forward("/api/moss", "img")


# ─── 角色扮演的单张 CG（2026-11-04 建、2026-11-08 补第二支）─────
#
# persona 页「出图」的结果地址先是 `/api/image/web/生图页/...`，2026-11-07 起
# 改落 `/api/image/角色扮演生图/<渠道>/...`（`persona.PLAY_OUT_DIR`）。**两条
# 都得转发**：只放老的那支时，新目录的图在 5174 上 200、在 5175/隧道上 404，
# 用户看到的就是「图生成了但网页端不显示」，5174 日志里连一条请求都没有。
#
# ⚠️ 只放这两支：`/api/image` 底下还有 QQ 私聊/群聊的图，目录名里
# 带 QQ 号/群号，不能整支暴露到公网。CG 只读，路径在 5174 侧还会做越界校验。
@app.route("/api/image/web/<path:sub>", methods=["GET"])
def proxy_web_img(sub):
    return _forward("/api/image/web", sub)


@app.route("/api/image/角色扮演生图/<path:sub>", methods=["GET"])
def proxy_play_img(sub):
    # `sub` 只有 `<渠道>/<文件名>` 两段，都是 ASCII；前缀里的中文交给
    # requests 的 requote_uri 百分号编码，5174 侧 Werkzeug 会解回中文再匹配路由。
    return _forward("/api/image/角色扮演生图", sub)


@app.route("/compat")
@app.route("/compat.html")
def compat_page():
    """兼容自检（2026-11-03）。

    群友反馈「看不到输入框」，但不同手机/浏览器表现不一样。这一页不读登录态、
    不调接口，打开就能看：UA、视口实测、各特性支持与否，还有一个「复制诊断」
    按钮——让他们把结果发过来，不用猜。
    """
    return send_from_directory(WEB_DIR, "compat.html")


@app.route("/char_intros.json")
def char_intros():
    """角色卡详情的「角色介绍 / 萌娘百科」（2026-11-09）。

    `persona.html` 里是**相对路径** `fetch("/char_intros.json")`：本机 5174
    由 Flask 静态目录直接发；手机走 5175 时这条路径落在网关上，而网关只显式
    列了页面路由 + 转发 `/api/*`，静态文件一个都不认——于是 404，角色卡详情
    里那一节整块不显示。用户报的「本地电脑能看到、手机看不到」就是它。

    文件就在 `web/` 下（和 5174 同一份），这里原样发出去，不新增任何数据。
    门槛与页面一致：登录用户或游客（`/play` 独立页也是游客）；都不是就 404，
    不把卡库介绍白送出去。
    """
    if not (_logged_in() or _guest_in()):
        return "", 404
    return send_from_directory(WEB_DIR, "char_intros.json")


def run():
    log_setup("friend")
    print("=" * 50)
    print("  群友生图页（只监听本机，靠隧道出去）")
    print("  本机: http://127.0.0.1:%d" % PORT)
    print("  后端: %s" % BACKEND)
    print("=" * 50)
    # 0.0.0.0 绝对不要：一开就等于绕过验证码直连
    app.run(host="127.0.0.1", port=PORT, debug=False, threaded=True)


if __name__ == "__main__":
    run()
