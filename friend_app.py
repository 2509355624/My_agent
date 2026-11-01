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

import json
import logging
import os
import threading
import time

import requests
from flask import (Flask, Response, jsonify, make_response, request,
                   send_from_directory, stream_with_context)

from app import friend_auth
from app.config import WEB_DIR
from app.logsetup import setup as log_setup

log = logging.getLogger("friend_app")

PORT = int(os.getenv("FRIEND_PORT", "5175"))
BACKEND = os.getenv("FRIEND_BACKEND", "http://127.0.0.1:5174").rstrip("/")

app = Flask(__name__, static_folder=None)
# 页面里没有任何外部资源，CSP 就按最紧的写：只允许自己和内联样式/脚本。
# 内联没办法——整个页面就一个文件，拆成 .js/.css 反而多两个请求。
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0

_SESSION = requests.Session()
_SESSION.trust_env = False          # 走系统代理只会绕远路

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
    resp.headers["Cache-Control"] = (
        "private, max-age=31536000, immutable"
        if ctype.startswith("image/")
        else "no-store")
    return resp


@app.before_request
def _gate():
    bucket = "img" if (request.path or "").startswith("/api/gal/img/") else "all"
    if not rate_ok(bucket):
        return _secure(jsonify({"ok": False, "error": "请求太频繁，歇一下"})), 429
    return None


@app.after_request
def _after(resp):
    return _secure(resp)


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
    return resp


@app.route("/logout", methods=["POST"])
def logout():
    resp = make_response(jsonify({"ok": True}))
    resp.delete_cookie(friend_auth.COOKIE_NAME, path="/")
    return resp


@app.route("/me")
def me():
    qq = friend_auth.check_cookie(request.cookies.get(friend_auth.COOKIE_NAME))
    return jsonify({"ok": True, "qq": qq})


# ─── 页面 ────────────────────────────────────────────────

def _logged_in():
    return bool(friend_auth.check_cookie(
        request.cookies.get(friend_auth.COOKIE_NAME)))


@app.route("/")
@app.route("/index.html")
def page():
    """根路径 = 画廊（2026-11-01 用户拍板：画廊是核心，预设降成二级入口）。

    未登录仍旧回 `friend.html`——它本身就是登录页（`#login` 和 `#app` 两态
    同一个文件），登录成功后由前端 `location.reload()` 回来，服务器这时
    才看得到 cookie，才会发画廊。
    """
    if not _logged_in():
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
    if not _logged_in():
        return send_from_directory(WEB_DIR, "friend.html")
    return send_from_directory(WEB_DIR, "gallery.html")


@app.route("/health")
def health():
    return jsonify({"ok": True})


# ─── 转发 ────────────────────────────────────────────────

def _forward(prefix, sub):
    """把请求原样转给 5174，并补上登录态里的 QQ 号。

    `qq` 一律由这里写死塞进去（参数或表单），**不信客户端传来的 qq**——
    否则谁都能改个 qq 就看别人的东西。
    """
    qq = friend_auth.check_cookie(request.cookies.get(friend_auth.COOKIE_NAME))
    if not qq:
        return jsonify({"ok": False, "error": "还没登录"}), 401
    url = "%s%s/%s" % (BACKEND, prefix, sub)
    headers = {"X-Friend-Token": friend_auth.secret()}
    params = dict(request.args)
    params["qq"] = qq

    # 下载原图时浏览器和下载器会带 Range（断点续传、多线程分段）。不转发出去
    # 就退化成每次都整包重传，一张 8MB 的图白下好几遍。
    for h in ("Range", "If-None-Match", "If-Modified-Since"):
        if request.headers.get(h):
            headers[h] = request.headers[h]

    # 图片走流式。requests 默认 stream=False 会把整个响应体读进内存，一张
    # 26MB 的原图在 5175 里就多占一份，几个人同时下能把这个进程顶爆。
    # 只对图片这么干：接口响应都很小，流式没有收益，反而多一层生成器。
    streaming = (request.method == "GET"
                 and (request.path or "").startswith("/api/gal/img/"))
    try:
        if request.method == "GET":
            r = _SESSION.get(url, params=params, headers=headers,
                             timeout=(10, 600), stream=streaming)
        elif request.method == "DELETE":
            r = _SESSION.delete(url, params=params, headers=headers, timeout=60)
        elif request.mimetype == "multipart/form-data":
            # 上传作品集：文件必须原样转发（不能先读进内存再重编码——
            # 几百 MB 一次读进来会把 5175 顶爆）。requests 支持传 fileobj。
            files = [(k, (f.filename, f.stream, f.mimetype))
                     for k, f in request.files.items(multi=True)]
            form = request.form.to_dict()
            form["qq"] = qq
            r = _SESSION.post(url, params=params, data=form, files=files,
                              headers=headers, timeout=300)
        else:
            body = request.get_json(silent=True) or {}
            body["qq"] = qq
            r = _SESSION.post(url, params=params, json=body,
                              headers=headers, timeout=60)
    except requests.RequestException as e:
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
        return Response(r.content, status=r.status_code, content_type=ctype)
    except Exception:
        return jsonify({"ok": False, "error": "后台返回看不懂"}), 502


@app.route("/api/friend/<path:sub>", methods=["GET", "POST", "DELETE"])
def proxy(sub):
    return _forward("/api/friend", sub)


@app.route("/api/gal/<path:sub>", methods=["GET", "POST", "DELETE"])
def proxy_gal(sub):
    return _forward("/api/gal", sub)


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
