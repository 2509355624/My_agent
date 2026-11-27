# -*- coding: utf-8 -*-
"""5174 管理页公网入口（独立进程，**只监听 127.0.0.1**，默认 5176）。

## 为什么单独一个进程

5174 上挂着全部管理页和 API，几乎没有鉴权。用户要在面试时把它映射到公网
演示，又不能裸奔。这个进程就是那道门：未登录只看到登录页，登录后把所有
请求原样转给 5174。5174 一行都不用改。

## 认证

固定账号密码（用户拍板，比 QQ 验证码稳、不依赖机器人在线）。凭据在
`state/admin_auth.json`，由 `tools/admin_passwd.py` 生成，public 仓库里
没有任何明文密码。登录成功后发 HMAC 签名 cookie（见 `app/admin_auth.py`）。

## 为什么转发要剔除 CF 头

5174 的 `_tunnel_guard` 见 `CF-Connecting-IP` / `CF-Ray` 就当公网隧道请求，
只放行群友页几个接口、挡掉管理页。我们的网关跑在本机、转发目标也是本机，
5174 看到的 remote_addr 是 127.0.0.1（合法）。所以转发时**必须**把浏览器
带来的 CF 头剔掉，否则管理页会被 5174 自己挡回来。

## 启动

    <venv>\\python.exe tools/admin_passwd.py        # 先建号（只做一次）
    <venv>\\python.exe admin_app.py                 # 起网关，监听 5176

端口用 `ADMIN_PORT` 覆盖；后端用 `ADMIN_BACKEND` 覆盖。
公网访问需另起一条 Cloudflare Tunnel 指向 127.0.0.1:5176（本仓库的
friend_tunnel.py 是给 5175 用的，不要复用那条的地址）。
"""

import logging
import os
import threading
import time

import requests
from flask import (Flask, Response, g, jsonify, make_response, redirect, request,
                   stream_with_context)

from app.core import admin_auth
from app.core.logsetup import setup as log_setup

log = logging.getLogger("admin_app")

PORT = int(os.getenv("ADMIN_PORT", "5176"))
BACKEND = os.getenv("ADMIN_BACKEND", "http://127.0.0.1:5174").rstrip("/")
COOKIE_TTL = admin_auth.COOKIE_TTL

app = Flask(__name__, static_folder=None)
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0

# ─── 限流：防暴力破解 + 防转发被当跳板 ───────────────────
# 内存计数即可，进程重启清零。
_RL = {}
_RL_LOCK = threading.Lock()
LIMITS = {
    "login": (10, 600),     # 登录校验：每 IP 每 10 分钟 10 次
    "all": (600, 60),       # 转发：每 IP 每分钟 600 次
}


def _client_ip():
    """公网来源取 CF 头第一段，本机调试退回 remote_addr。"""
    cf = request.headers.get("CF-Connecting-IP")
    if cf:
        return cf.split(",")[0].strip()
    xff = request.headers.get("X-Forwarded-For")
    if xff:
        return xff.split(",")[0].strip()
    return request.remote_addr or "?"


def rate_ok(bucket):
    limit, window = LIMITS[bucket]
    ip = _client_ip()
    now = time.time()
    key = (bucket, ip)
    with _RL_LOCK:
        hits = [t for t in _RL.get(key, []) if now - t < window]
        if len(hits) >= limit:
            _RL[key] = hits
            return False
        hits.append(now)
        _RL[key] = hits
    return True


# ─── 登录 ────────────────────────────────────────────────

def _cookie_secure():
    return request.headers.get("X-Forwarded-Proto") == "https"


LOGIN_HTML = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>管理页登录</title>
<style>
*{box-sizing:border-box}html,body{margin:0;min-height:100%;background:#06111a;color:#e8e4d8;font-family:-apple-system,"Microsoft YaHei",sans-serif;-webkit-text-size-adjust:100%}
body{display:grid;place-items:center;padding:1.5rem}
.card{width:min(24rem,100%);background:#0e2331;border:1px solid rgba(151,185,199,.4);border-radius:6px;padding:1.8rem}
h1{margin:0 0 .3rem;font-size:1.25rem}.sub{margin:0 0 1.4rem;color:#91a7b3;font-size:.85rem}
label{display:block;margin:.9rem 0 .35rem;font-size:.85rem;color:#cfd6d2}
input{width:100%;padding:.7rem .8rem;background:#061019;border:1px solid rgba(151,185,199,.4);border-radius:4px;color:#e8e4d8;font-size:1rem}
button{width:100%;margin-top:1.4rem;padding:.75rem;background:#f3b35f;border:0;border-radius:4px;color:#07131d;font-weight:700;font-size:1rem;cursor:pointer}
button:disabled{opacity:.5;cursor:not-allowed}.err{margin-top:1rem;color:#ff9684;font-size:.85rem;min-height:1.2rem}
.foot{margin-top:1.4rem;color:#91a7b3;font-size:.75rem;line-height:1.6}
</style></head><body>
<div class="card">
<h1>管理页登录</h1>
<p class="sub">输入账号密码后进入管理后台</p>
<form id="f">
<label for="u">账号</label><input id="u" name="user" autocomplete="username" autofocus>
<label for="p">密码</label><input id="p" name="password" type="password" autocomplete="current-password">
<button id="b" type="submit">登录</button>
<div class="err" id="e"></div>
</form>
<div class="foot">仅限站主访问 · 登录状态保留 12 小时</div>
</div>
<script>
var f=document.getElementById('f'),e=document.getElementById('e'),b=document.getElementById('b');
f.addEventListener('submit',function(ev){
ev.preventDefault();e.textContent='';b.disabled=true;b.textContent='登录中…';
fetch('/login/check',{method:'POST',headers:{'Content-Type':'application/json'},
body:JSON.stringify({user:document.getElementById('u').value,password:document.getElementById('p').value})})
.then(function(r){return r.json().then(function(j){return {ok:r.ok,j:j}})})
.then(function(x){if(x.ok&&x.j.ok){location.href='/'}else{e.textContent=x.j.error||'登录失败';b.disabled=false;b.textContent='登录'}})
.catch(function(){e.textContent='网络错误，稍后再试';b.disabled=false;b.textContent='登录'});
});
</script></body></html>"""


@app.route("/login")
def login_page():
    if _logged_in():
        return redirect("/")
    return Response(LOGIN_HTML, content_type="text/html; charset=utf-8")


@app.route("/login/check", methods=["POST"])
def login_check():
    if not admin_auth.configured():
        return jsonify({"ok": False, "error": "还没设置管理员账号，请先运行初始化脚本"}), 503
    if not rate_ok("login"):
        return jsonify({"ok": False, "error": "试太多次了，等一会儿再试"}), 429
    data = request.get_json(silent=True) or {}
    ok, msg = admin_auth.verify_password(data.get("user"), data.get("password"))
    if not ok:
        return jsonify({"ok": False, "error": msg}), 401
    resp = make_response(jsonify({"ok": True}))
    resp.set_cookie(admin_auth.COOKIE_NAME, admin_auth.issue_cookie(data["user"]),
                    max_age=COOKIE_TTL, httponly=True, samesite="Lax",
                    secure=_cookie_secure(), path="/")
    return resp


@app.route("/logout", methods=["POST", "GET"])
def logout():
    resp = make_response(jsonify({"ok": True}))
    resp.delete_cookie(admin_auth.COOKIE_NAME, path="/")
    return resp


@app.route("/health")
def health():
    return jsonify({"ok": True, "configured": admin_auth.configured()})


def _logged_in():
    return bool(admin_auth.check_cookie(
        request.cookies.get(admin_auth.COOKIE_NAME)))


# ─── 转发 ────────────────────────────────────────────────

# 这几个头绝不能带给 5174：带了会被它的 _tunnel_guard 当成公网隧道请求，
# 只放行群友页接口、挡掉管理页（见模块注释）。
_DROP_HEADERS = ("CF-Connecting-IP", "CF-Ray", "CF-IPCountry", "CF-Visitor")

# 图片 / 大文件走流式，避免整包读进网关内存。
_STREAM_PREFIXES = (
    "/api/image/", "/api/gal/img/", "/api/moss/img", "/api/comic/img",
    "/api/runbench/cover/", "/api/review/image/", "/api/showcase/image",
)


def _pass_headers():
    """挑出要转给 5174 的请求头，剔掉 CF 系与逐跳头。

    Werkzeug 的 `request.headers` 迭代出的名字可能是小写（`cf-connecting-ip`），
    所以排除集合统一按小写比，别让大小写不一致把 CF 头漏过去——那会让 5174
    的 _tunnel_guard 把这当公网隧道请求，管理页直接 404。
    """
    skip = {h.lower() for h in _DROP_HEADERS} | {
        "host", "content-length", "connection", "accept-encoding"}
    return {k: v for k, v in request.headers if k.lower() not in skip}


@app.route("/", defaults={"sub": ""}, methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
@app.route("/<path:sub>", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
def proxy(sub):
    if not _logged_in():
        # 页面请求跳登录页；API 请求回 401（前端 fetch 不会跟着跳 HTML）。
        if request.method == "GET" and not request.path.startswith("/api/"):
            return redirect("/login")
        return jsonify({"ok": False, "error": "未登录"}), 401
    if not rate_ok("all"):
        return jsonify({"ok": False, "error": "请求太频繁"}), 429

    url = "%s/%s" % (BACKEND, sub)
    headers = _pass_headers()
    # 5174 的部分 Blueprint（friend_api / gallery_api）只认内部令牌。
    # 用与 5175 同一个 secret，网关才有资格调这些接口。
    try:
        from app.gateways import friend_auth
        headers["X-Friend-Token"] = friend_auth.secret()
    except Exception:
        pass

    params = dict(request.args)
    body = None
    if request.method not in ("GET", "HEAD"):
        if request.mimetype == "multipart/form-data":
            files = [(k, (f.filename, f.stream, f.mimetype))
                     for k, f in request.files.items(multi=True)]
            body = dict(request.form)
            sess = _new_session()
            try:
                r = sess.request(request.method, url, params=params,
                                 data=body, files=files, headers=headers,
                                 timeout=(10, 300), stream=False)
                return _respond(r, sess)
            except requests.RequestException as e:
                sess.close()
                log.warning("转发失败 %s：%s", sub, e)
                return jsonify({"ok": False, "error": "后台没响应"}), 502
        else:
            body = request.get_json(silent=True)
            if body is None:
                body = request.get_data() or None

    sess = _new_session()
    streaming = (request.method == "GET"
                 and request.path.startswith(_STREAM_PREFIXES))
    try:
        r = sess.request(request.method, url, params=params, json=body
                         if isinstance(body, dict) else None,
                         data=None if isinstance(body, dict) else body,
                         headers=headers, timeout=(10, 600), stream=streaming)
        return _respond(r, sess, streaming=streaming)
    except requests.RequestException as e:
        sess.close()
        log.warning("转发失败 %s：%s", sub, e)
        return jsonify({"ok": False, "error": "后台没响应，稍后再试"}), 502


def _new_session():
    s = requests.Session()
    s.trust_env = False          # 走系统代理只会绕远路
    return s


def _respond(r, sess, streaming=False):
    ctype = r.headers.get("Content-Type") or "application/octet-stream"
    keep = ("Content-Length", "Content-Range", "Accept-Ranges",
            "ETag", "Last-Modified", "Cache-Control", "Content-Disposition")
    if streaming and ctype.startswith(("image/", "video/", "application/pdf")):
        def _pump():
            try:
                for chunk in r.iter_content(64 * 1024):
                    if chunk:
                        yield chunk
            finally:
                r.close()
                sess.close()
        resp = Response(stream_with_context(_pump()), status=r.status_code,
                        content_type=ctype)
        for h in keep:
            if r.headers.get(h):
                resp.headers[h] = r.headers[h]
        return resp
    try:
        content = r.content
    except Exception:
        sess.close()
        return jsonify({"ok": False, "error": "后台返回看不懂"}), 502
    sess.close()
    resp = Response(content, status=r.status_code, content_type=ctype)
    for h in keep:
        if r.headers.get(h):
            resp.headers[h] = r.headers[h]
    return resp


if __name__ == "__main__":
    log_setup("admin_app")
    if not admin_auth.configured():
        log.warning("还没设置管理员账号，先运行：python tools/admin_passwd.py")
    app.run(host="127.0.0.1", port=PORT, debug=False, threaded=True)
