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
from urllib.parse import urlencode

import requests
from flask import (Flask, Response, g, jsonify, make_response, redirect, request,
                   send_from_directory, stream_with_context)

from app.gateways import friend_auth
from app.gateways import guest_view
from app.gateways import visitor
from app.config import WEB_DIR, WORKSHOP_HTML
from app.core.logsetup import setup as log_setup

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
    # 产品展示页：聊天与状态共用一个宽松桶，真实生图单独限流。
    # 状态轮询 1.6s/次（生图全程 1~3 分钟 ≈ 200+ 次），600/10分钟才够轮询，
    # 原先 120 会在轮询 3 分钟后被自己掐成 429。
    "showcase": (600, 600),
    "showimg": (12, 600),
    # 文档问答（2026-11-15）：一次完整演示 = warm + clean + split + ingest ≈ 4 次，
    # 之后每问一次 1 次。60/10 分钟够 8~10 轮演示；再紧会被自己的流程卡住。
    # 真正的成本闸在 5174：单文件 1 万字、最多 200 块、同时最多 3 个临时库。
    "raglab": (60, 600),
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
    path = request.path or ""
    if ctype.startswith("image/"):
        resp.headers["Cache-Control"] = (
            "private, max-age=31536000, immutable")
    elif path.startswith("/workspace/") and (
            ctype.startswith("text/css") or "javascript" in ctype
            or ctype.startswith("font/")):
        # workspace 的 css / js（2026-11-23 晚）：原来跟着页面一起 no-store，
        # 等于**每进一次页面就把十几个文件重新穿过隧道拉一遍**——这条线路
        # 往返 Cloudflare 边缘就要 0.8~2 秒，十来个小文件叠起来就是用户说
        # 的「太慢、老是卡」。
        #
        # 这里给 no-cache（不是 no-store）：**允许存，但每次用之前必须
        # 回请求校验**。文件没改就走 304（几百字节），改了立刻拿到新的，
        # 不存在「改了代码群友还看旧样式」的坑。要不要放开成 max-age 是
        # 另一回事，前端目前还靠 `?v=` 手改版本号，先只做最稳的这一层。
        resp.headers["Cache-Control"] = "no-cache"
    elif path.startswith("/workshop"):
        # 提示词工坊是 4.6MB 的单文件，no-store 等于每次刷新都从这台机器
        # 重新传一遍。文件换没换由 ETag 判（mtime 变 → ETag 变），这里只
        # 负责别让同一个人每进一次就重下一遍。
        resp.headers["Cache-Control"] = "private, max-age=600"
    else:
        resp.headers["Cache-Control"] = "no-store"
    return resp


@app.before_request
def _gate():
    path = request.path or ""
    if path == "/api/showcase/image/submit":
        bucket = "showimg"
    elif path.startswith("/api/showcase/"):
        bucket = "showcase"
    elif path.startswith("/api/raglab/"):
        bucket = "raglab"
    else:
        bucket = "img" if (path.startswith("/api/gal/img/")
                           or path.startswith("/api/comic/img")
                           or path.startswith("/api/outfit/img")
                           or path == "/api/showcase/image") else "all"
    if not rate_ok(bucket):
        return _secure(jsonify({"ok": False, "error": "请求太频繁，歇一下"})), 429
    # 独立游客页的隔离：只圈「没登录 + 进过 /play」的人，且只挡页面导航。
    # API 一律放行——聊天要靠它，其余接口本来就要求登录（见 _forward）。
    # 2026-11-23「AI 创作」那几个子页也要能发图，所以排第二。
    p = (request.path or "/").rstrip("/") or "/"
    # 带子路径的也一起放行（/workspace/js/chat.js），否则子页里点开新界面
    # 再刷资源仍会被自己的页面锁挡回 /play。
    _ws_page = p == "/workspace" or p.startswith("/workspace/")
    # QQ「/画廊」「/漫画台」发出来的正式入口：先把 /play 的页面锁解开再按
    # 正常鉴权走。2026-11-06 回归：站主自己点过一次 /play 后，浏览器带着
    # 7 天的 fr_solo，再点 QQ 给的 /gallery 会被下面这条挡回游客页，看起来
    # 像「链接发错了」。带 from=qq 只可能是刚从 QQ 指令拿到的链接；访客在
    # /play 里手改地址栏到 /gallery（不带参数）仍然挡回，独立页的孤立性不变。
    # 2026-11-11 把 /comic 一并收进来：QQ 新增「/漫画台」指令，同一个坑别再踩。
    # 2026-11-12 /novel 同理：小说页上架后若加 QQ 直达链接，别让 fr_solo 挡回游客页。
    # 2026-11-13 /persona 同理：任务回执尾巴现在带「角色扮演」入口，被 fr_solo
    # 挡回 /play 就会变成「点了没反应」。
    # 2026-11-23 /workspace 同理：新界面是 5175 群友页的正牌落地页，QQ
    # 「/工作台」直接发 `/workspace/`。
    # **2026-11-25 指向纠正后**：`/gallery`、`/comic` 不再 302 到工作台，各自
    # 直接发作品墙 / 群友版漫画台，所以它们也进了下面那段「绕一趟清标记」——
    # 原来那三行 `return None` 是靠「这两个视图自己 302」才成立的，现在它们
    # 直接出页面，再 return None 就等于标记没清、cookie 还挂着 fr_solo。
    # 注意这里的 p 已经 rstrip("/")，所以只能写不带斜杠的形式。
    if (p in ("/gallery", "/gallery.html", "/comic", "/comic.html",
              "/novel", "/novel.html", "/runbench", "/runbench.html",
              "/workspace", "/persona", "/persona.html")
            and request.args.get("from") == "qq" and _solo_in()):
        g.clear_solo = True
        # clear_solo 靠**回写 cookie** 生效，只在重定向上有效。凡是不打算
        # 重定向的路径，先绕一趟去掉 from=qq 的同一个地址，让浏览器把
        # Set-Cookie 收下再回来（第二趟 _solo_in() 已为假）。
        clean = {k: v for k, v in request.args.items() if k != "from"}
        qs = urlencode(clean)
        return redirect((request.path or "/") + ("?" + qs if qs else ""))
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
                          "/char_intros.json", "/outfit", "/outfit.html",
                          "/runbench", "/runbench.html",
                          "/workspace",
                          "/showcase", "/showcase.html")
            # 新界面的样式和脚本也在 /workspace/ 下，不能一起挡回 /play，
            # 否则 index.html 发出去但 css/js 全被重定向成 HTML。
            # `_ws_page` 同时覆盖 `/workspace` 与 `/workspace/<任意子路径>`。
            and not _ws_page):
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
    # `_solo_in()` 直接读 cookie 头，删 cookie 不会让它在同一个请求里变假，
    # 所以这里必须自己把标记置回，否则「那趟自指重定向」会一直绕下去。
    if getattr(g, "clear_solo", False):
        resp.delete_cookie(SOLO_COOKIE, path="/")
        g.clear_solo = False
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


@app.route("/login/visitor", methods=["POST"])
def login_visitor():
    """访客进入：不发码、不要 QQ，直接签一个 24 小时的访客 cookie。

    用户 2026-11-26 拍板：「访客直接点就进去了，能生图」「每天 5 张，
    换浏览器就随便他换」。所以这里只做两件事：签身份、下发 cookie。
    额度在 5174 那边按身份串记（`app/gateways/visitor.py`），5175 不管账。
    """
    if not rate_ok("send"):
        return jsonify({"ok": False, "error": "发太多次了，等一会儿"}), 429
    gid = visitor.new_id()
    resp = make_response(jsonify({"ok": True, "role": "visitor",
                                  "quota": visitor.snapshot(gid)}))
    resp.set_cookie(visitor.COOKIE_NAME, gid,
                    max_age=visitor.COOKIE_TTL, httponly=True,
                    samesite="Lax", secure=_cookie_secure(), path="/")
    # 之前登录过的浏览器如果还留着 fr_sess，访客身份会被忽略（_logged_in 优先），
    # 于是「点了访客还是全权限」。这里先把它清掉，确认用户是真的要走访客这条。
    resp.delete_cookie(friend_auth.COOKIE_NAME, path="/")
    resp.delete_cookie(SOLO_COOKIE, path="/")
    return resp


@app.route("/logout", methods=["POST"])
def logout():
    resp = make_response(jsonify({"ok": True}))
    resp.delete_cookie(friend_auth.COOKIE_NAME, path="/")
    resp.delete_cookie(GUEST_COOKIE, path="/")
    resp.delete_cookie(visitor.COOKIE_NAME, path="/")
    # 退出登录顺手解开 /play 的页面锁：站主想回自己的画廊不用去清浏览器 cookie。
    resp.delete_cookie(SOLO_COOKIE, path="/")
    return resp


@app.route("/me")
def me():
    """我是谁：已登录给 qq，其余一律算游客。

    ⚠️ 2026-11-25 修 bug：原来 `guest` 是「没登录 **且** 带 fr_guest cookie」，
    而 fr_guest 只有点 `/guest` 那个入口才会发。QQ 群友点「/画廊」拿到的链接
    是 `/gallery?from=qq`，服务端只清 solo 标记、**不发 fr_guest**，于是这里
    回 `guest: false` → `gallery.html` 的 `applyGuest()` 不执行 → 登录按钮
    一直是 `display:none`。用户看到的就是「有登录墙，但找不到登录按钮」。

    现在口径改成「没登录就等于游客」：画廊本身对未登录者就是只读的
    （写接口在 5174 一律要 qq），登录与否只决定**前端收不收「我的」「自定义
    预设」、露不露登录入口**。`fr_guest` 那个 cookie 仍然保留——它管的是
    「走 /guest 进来的只读会话」，和这里的身份判定是两件事。
    """
    qq = friend_auth.check_cookie(request.cookies.get(friend_auth.COOKIE_NAME))
    gid = "" if qq else _visitor_id()
    out = {"ok": True, "qq": qq, "guest": bool(not qq),
           # 2026-11-26 访客模式：前端靠 `role` 决定露不露「漫画生成」、
           # 「我的」，以及顶栏那行「访客 · 今日还剩 N 张」。
           "role": "user" if qq else ("visitor" if gid else "guest")}
    if gid:
        out["quota"] = visitor.snapshot(gid)
    return jsonify(out)


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


def _visitor_id():
    """这次请求是不是**访客模式** → 身份串；不是 → ""。

    访客是登录页「先逛逛」那条路发出来的 `fr_visit`，不需要 QQ 号、不进
    好友白名单（用户 2026-11-26：「访客直接点就进去了」）。真登录之后
    `_logged_in()` 为真，访客身份自然让位——两套 cookie 同时在也不打架。
    """
    raw = request.cookies.get(visitor.COOKIE_NAME)
    if not raw:
        return ""
    return visitor.check_id(raw) or ""


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


# ─── 新界面（聊天优先 workspace）────────────────────────────
#
# 2026-11-23 用户拍板：5175 也接新界面，登录走现有验证码。
# 注意 5175 是 **static_folder=None**，它从不发静态文件——只一个个
# send_from_directory 发页面。而新界面把样式和脚本拆成了 css/、js/ 多个文件，
# 所以这里必须补一条资源路由，否则页面发出去是一张没样式的白纸。

WORKSPACE_DIR = os.path.join(WEB_DIR, "workspace")


@app.route("/workspace")
def workspace_page():
    """新界面入口（无尾斜杠）：先 302 到 /workspace/ 再判断登录态。

    页面里都是相对路径 `css/tokens.css`。若停在 `/workspace`，浏览器会把它
    解析成 `/css/tokens.css`——5175 没有静态服务，那个地址只有 404，页面就
    成了没样式的白纸。所以入口统一带斜杠。
    """
    return redirect("/workspace/")


@app.route("/workspace/")
def workspace_index():
    """新界面：没登录发登录页，登录后发聊天页。

    登录页是 `workspace/index.html`（两步：QQ 号 → 验证码），验证成功后前端
    自己跳到 `/workspace/`，服务器这时才看到 cookie，于是发聊天页。
    旧的 `/gallery`、`/comic` 两个地址都 302 到这里（2026-11-23），
    所以它也是 QQ 那些老链接的落地页。
    """
    if not _logged_in() and not _visitor_id():
        return send_from_directory(WORKSPACE_DIR, "index.html")
    return send_from_directory(WORKSPACE_DIR, "chat.html")


@app.route("/workspace/<path:sub>")
def workspace_asset(sub):
    """workspace 下的静态资源（css/、js/）。

    `send_from_directory` 自带路径穿越防护（`safe_join`），`../` 会被拒。
    """
    return send_from_directory(WORKSPACE_DIR, sub)


@app.route("/gallery")
@app.route("/gallery.html")
def gallery_page():
    """画廊 = 真正的作品墙（2026-11-25 用户纠正指向）。

    以前这里 302 到 `/workspace/`，理由是「5175 只发新界面」。但用户把三个
    入口的定位理清了：**画廊是画廊、漫画台是漫画台、工作台才是新界面**。
    画廊那一侧才是自定义预设 / 提示词工坊 / 角色扮演 / 漫画台 / 小说模式的
    集合地，工作台是另一件事。所以 `/gallery` 必须真的发作品墙。

    `gallery.html` 只用 `/api/gal/*`，5175 已经在反代那一套，页面直接能跑；
    不用登录也能看（和 5174 本机同口径，登录与否由前端自己按 `/me` 判断）。
    """
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
             post_timeout=60, jf=False):
    """把请求原样转给 5174，并补上登录态里的 QQ 号。

    `jf`（2026-11-24）= 这是**工作台**发来的请求，转发时补 `__jf=1`。
    5174 用它区分「群友版工作台」和「管理页」：同一个站长身份，在工作台
    里只能看到普通用户该看的东西（见 `gallery.routes._jf`）。

    `qq` 一律由这里写死塞进去（参数或表单），**不信客户端传来的 qq**——
    否则谁都能改个 qq 就看别人的东西。

    `allow_guest`（2026-11-03）：角色扮演那条要放游客进来。游客没有 qq，
    转发时不塞 `qq`——5174 那边收到空 qq 会走游客分支（见
    `persona_api._who()`），身份由它自己签发的 `gs_id` cookie 认。
    cookie 要**整条带过去**，不然 5174 认不出同一个游客。

    `force_guest`（2026-11-06）：独立游客页 `/play` 的专用代理用。哪怕浏览器
    里有登录 cookie 也不注入 QQ，保证从分享链接进去的人一定是游客、看不到
    站主自己的线。只作用于 `/api/solo/*`，普通 `/persona` 页不受影响。

    `vis`（2026-11-26 访客模式）：访客没有 QQ，先按路径白名单挡（工作台
    真的会调的四组放行，其余 403），再带 `X-Friend-Guest` 头把身份送过去。
    **不带 `qq`、不带 `__jf`**：5174 那边认这个头就当访客算，画廊的
    `_seer()` 直接判不能看隐藏内容；额度也在 5174 扣（它才有提交那一刻）。
    """
    qq = friend_auth.check_cookie(request.cookies.get(friend_auth.COOKIE_NAME))
    if force_guest:
        qq = ""
    gid = "" if (qq or force_guest) else _visitor_id()
    if gid and not visitor.path_allowed(request.path, request.method):
        return jsonify({"ok": False,
                        "error": "访客模式用不了这个功能"}), 403
    if not qq and not gid and not allow_guest:
        return jsonify({"ok": False, "error": "还没登录"}), 401
    url = "%s%s/%s" % (BACKEND, prefix, sub)
    headers = {"X-Friend-Token": friend_auth.secret()}
    params = dict(request.args)
    if gid:
        # 访客：身份走专用头，绝不塞 `qq`——塞了就到不了访客分支，会被当成
        # 某个 QQ 号的登录态，配额和隐藏内容全按那个人算。
        headers["X-Friend-Guest"] = request.cookies.get(visitor.COOKIE_NAME) or ""
    elif jf:
        params["__jf"] = "1"

    # 游客身份 cookie 必须透传：5175 不解析它，只负责原样送到 5174。
    # 访客模式（gid）不带它：那条路 5174 只认 `X-Friend-Guest`，混两个身份
    # 会让 persona 那套游客线误把访客当成 /play 进来的人。
    gs = "" if gid else request.cookies.get("gs_id")
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
                      or (request.path or "").startswith("/api/runbench/cover/")
                      or (request.path or "").startswith("/api/image/web/")
                      or (request.path or "") == "/api/showcase/image"
                      or (request.path or "").startswith(
                          "/api/image/角色扮演生图/")
                      # 跑图台出图（2026-11-24）：原来不在这里，于是整张图先被
                      # `r.content` 读进 5175 内存再发出——浏览器收不到
                      # Content-Length，体感就是「卡住很久然后图突然蹦出来」。
                      # 一张 2~3MB 的图看不出多大差别，但和下面的缓存头丢失
                      # 叠在一起，就是用户说的「生成之后要加载非常慢」。
                      or (request.path or "") == "/api/gen/img"))
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
        # 少了 Content-Range 断点续传直接坏。Cache-Control 也要透传，否则
        # 5174 给封面图下的 immutable 长期缓存到 5175 就丢了（2026-11-13
        # 跑图台封面缩略图需要它让浏览器刷新不重拉）。
        for h in ("Content-Length", "Content-Range", "Accept-Ranges",
                  "ETag", "Last-Modified", "Cache-Control"):
            if r.headers.get(h):
                resp.headers[h] = r.headers[h]
        return resp

    try:
        resp = Response(r.content, status=r.status_code, content_type=ctype)
    except Exception:
        sess.close()
        return jsonify({"ok": False, "error": "后台返回看不懂"}), 502
    # 非流式分支也要照抄缓存头（2026-11-24）：上面 stream 那一支有，这里原来
    # 一个都没带——5174 给 `/api/gen/img` 下的 `private, max-age=31536000,
    # immutable` 到了 5175 就被丢掉，浏览器每次刷新都得整张重下，白走一遍
    # 家用上行。ETag / Last-Modified 一起带上，才能协商出 304。
    for h in ("Content-Length", "ETag", "Last-Modified", "Cache-Control",
              "Accept-Ranges", "Content-Range"):
        if r.headers.get(h):
            resp.headers[h] = r.headers[h]
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


@app.route("/api/chat/<path:sub>", methods=["GET", "POST", "PATCH", "DELETE"])
def proxy_chat(sub):
    """工作台聊天记录（2026-11-24）：`/api/chat/archive` 的 GET/POST/PATCH/DELETE。

    走 `_forward` 而不是自己读档：QQ 号必须由网关**注入**（`params["qq"]`），
    客户端传什么 qq 都不作数——否则改个参数就能读写别人的聊天记录。
    没登录 → 401（`_forward` 自带），前端据此退回「只用本地那份」。
    """
    return _forward("/api/chat", sub)


@app.route("/api/gen/<path:sub>", methods=["GET", "POST"])
def proxy_gen(sub):
    """跑图台那套接口（渠道清单 / 提交 / 轮询 / 取图）给新界面用。

    新界面的聊天页和 `AI 创作` 子页都在 5175 下，而 5175 只反代白名单里的
    前缀——`/api/gen/*` 原来根本不存在（群友页那边直接 404），于是聊天页
    永远「没拿到渠道列表」，渠道抽屉也是空的（2026-11-23 用户反馈）。

    读接口放行，提交必须登录：提交能直接占 GPU 队列，不能匿名打。
    不套 `/api/friend` 那一份 per-QQ 额度——那是群友页 LoRA 面板专用的，
    它要求请求里带 qq；新界面这条只借用「提清单 + 提交 + 轮询」的最小面。

    `imgsign`（2026-11-24）：工作区要把图存进浏览器本地缓存，取字节时用的是
    `no-cors` 请求（带不了 cookie）。所以这个签名口子**不要求登录**——它只
    下发一个「本站页面」的签名，不带任何身份；没有它，登录态之外的取图请求
    全都会被 5174 的令牌校验挡掉，缓存那套就白做了。
    """
    # `img`（2026-11-24）：只读一张图，和 `/api/gal/img/<id>` 同性质，
    # 所以登录态和「站点签名（`k=`）」都放行。要它放行签名，是因为工作区
    # 要把图存进浏览器本地缓存——那条路用 `no-cors` 取字节，带不了 cookie。
    # 缓存换域名后还能命中，全靠这个（见 web/workspace/js/imgcache.js）。
    if sub == "imgsign":
        return _forward("/api/gen", sub)
    if sub == "img" and request.args.get("k"):
        return _forward("/api/gen", sub)
    if request.method == "POST" and not _logged_in():
        return _secure(jsonify({"ok": False, "error": "没登录"})), 401
    return _forward("/api/gen", sub)


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
    #
    # 2026-11-24：这一条**带 `__jf=1`**——工作台（群友版）和访问同一批接口，
    # 但站主在工作台里必须和普通用户看到的一模一样（藏起来的图不出现、
    # 没有「隐藏」按钮）。5174 那边靠这个标记区分「这是工作台」和「这是
    # 管理页」，见 `gallery.routes._jf`。漏带这个标记，站长一登录工作台就会
    # 看见半透明的已隐藏大图、左右滑到它、下载也拿得到。
    return _forward("/api/gal", sub, allow_guest=_guest_in(), jf=True)


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
    """漫画台 = 群友版漫画台（2026-11-25 用户纠正指向）。

    以前这里 302 到 `/workspace/`，等于把「漫画台」和「工作台」混成一个
    页面。用户把定位理清了：漫画台就是漫画台，工作台是新界面。

    发的是 `comic_studio_friend.html`（由 `tools/comic_friend_patch.py` 从
    `comic_studio.html` 生成的群友版）——这文件一直在仓库里，但此前**没有
    任何路由发它**，所以群友其实从没见过真正的群友版漫画台。

    它除了 `/api/comic/*` 还要 `/api/providers`、`/api/models` 拉模型下拉，
    这两条 5175 原来没反代（见下面两条只读代理）。页面本身不需要也不能传
    qq：`_forward` 会把登录 Cookie 解出的 QQ 写死塞进请求。
    """
    return send_from_directory(WEB_DIR, "comic_studio_friend.html")


@app.route("/api/providers")
def comic_providers():
    """漫画台的模型渠道下拉——**只给安全的三个字段**。

    不能直接转发 5174 的 `/api/providers`：那边会带上 `base_url` 和自定义
    渠道密钥的尾 4 位（`provider_store.public_view`），那是站主的私货。
    群友只需要知道「有哪些渠道、叫什么、默认哪个」。

    `scope=comic` 照原样透传，否则漫画台专用的那几个中转站不会出现。
    """
    try:
        r = requests.get(BACKEND + "/api/providers",
                         params={"scope": "comic"}, timeout=10)
        d = r.json()
    except Exception:                                    # noqa: BLE001
        return jsonify({"providers": [], "default": ""}), 502
    items = [{"id": p.get("id", ""), "label": p.get("label", ""),
              "model": p.get("model", "")}
             for p in (d.get("providers") or [])]
    return jsonify({"providers": items, "default": d.get("default", "")})


@app.route("/api/models")
def comic_models():
    """`/api/models?provider=xxx` 的只读代理。

    只放行带 `provider` 的那种（返回该渠道当前可用的对话模型）。不带
    provider 的用法是「代理本机 Ollama 的 /api/tags」，那个会按请求里的
    `baseUrl` 去连任意地址，不能开给公网。
    """
    pid = (request.args.get("provider") or "").strip()
    if not pid:
        return jsonify({"ok": False, "error": "缺少 provider"}), 400
    try:
        r = requests.get(BACKEND + "/api/models",
                         params={"provider": pid}, timeout=15)
        return jsonify(r.json())
    except Exception as e:                               # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)}), 502


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
    """小说接口反代。POST 单独放宽到 600s。

    原来是全场统一 60s，而 LLM_REQUEST_TIMEOUT 单次就是 60s —— 小说一轮
    串行两次，群友很容易收到「后台没响应」但正文其实已经落盘了。前端每轮
    带 turn_token，超时重发同一个 token 会拿到上一轮结果，不会重复写。

    2026-11-12 再放大：小说三段现在各有各的读超时（开场 150 / 写作 120 /
    编剧 90，见 novel.OPEN_TIMEOUT），群友最坏那条路要串四次 LLM —— 免费层
    编剧+写作（210s）失败后再扣次数走全局链重跑一遍（又是 210s）= 420s。
    网关还留 300s 的话，后端明明还在跑就被这里掐成 502，「正文已落盘、
    用户看到报错」的老毛病会原样复发。600s 是 420s 之上再留一成半余量。
    """
    return _forward("/api/novel", sub, post_timeout=600)


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


# ── 产品展示页 ───────────────────────────────────────────
# 匿名访问独立干净页面；API 不注入 QQ，也不使用内部扮演的游客会话。

@app.route("/showcase")
@app.route("/showcase.html")
def showcase():
    return send_from_directory(WEB_DIR, "showcase.html")


@app.route("/raglab-sample.md")
def raglab_sample():
    """展示页「载入示例文档」用的样本（2026-11-15）。

    网关只反代 `/api/*`，页面自己去 `fetch("/raglab-sample.md")` 不会命中
    转发规则，必须单独给一条静态路由——这个坑之前踩过（页面里取 json 在手机
    上 404）。文件本身可以公开：里面没有任何密钥，就是一份技术说明。
    """
    return send_from_directory(WEB_DIR, "raglab-sample.md")


# ── 跑图台页（2026-11-13）────────────────────────────────
#
# 「/跑图台」QQ 命令回的链接指向这里。和 /outfit 同款**免登录令牌制**：令牌
# 本身就是凭证（含来源 + 过期 + HMAC 签名，签名域 runbench:），网页提交时带
# 上，5174 解出「结果推回哪个会话」出图。页面路由直接发 HTML，接口反代走
# allow_guest=True（不注入 qq、不带登录 cookie 也放行）——跑图台不需要账号
# 体系，分享给别人也能用，滥用由令牌时效 + 接口限流兜。

@app.route("/runbench")
@app.route("/runbench.html")
def runbench_page():
    return send_from_directory(WEB_DIR, "runbench.html")


@app.route("/api/runbench/<path:sub>", methods=["GET", "POST", "DELETE"])
def proxy_runbench(sub):
    """跑图台接口反代。allow_guest=True：令牌即凭证，不注入 qq。"""
    return _forward("/api/runbench", sub, allow_guest=True)


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


@app.route("/api/showcase/<path:sub>", methods=["GET", "POST", "DELETE"])
def proxy_showcase(sub):
    """产品展示接口匿名开放，但不注入 QQ，也不透传内部扮演身份。"""
    return _forward("/api/showcase", sub, allow_guest=True, force_guest=True,
                    post_timeout=180)


@app.route("/api/raglab/<path:sub>", methods=["GET", "POST"])
def proxy_raglab(sub):
    """文档问答（2026-11-15）：匿名开放，游客各用各的临时库（sid 由 5174 发）。

    `split` 要分批调大模型、`answer` 要检索再加生成，60 秒的默认超时不够——
    宿主机实测一篇 4 千字文档切 2 批约 40 秒，1 万字打满要更久。
    """
    # extract 现在会走视觉识别扫描件（一页一次调用，最多 10 页 ≈ 40~90 秒），
    # 60 秒的默认值会在第 4 页就 502，所以和 split/answer 一样放长。
    timeout = 300 if sub in ("split", "answer", "extract") else 60
    return _forward("/api/raglab", sub, allow_guest=True, post_timeout=timeout)


@app.route("/api/persona/<path:sub>", methods=["GET", "POST", "DELETE"])
def proxy_persona(sub):
    # 语音合成在 3060 上要跑 100 秒上下；默认 60 秒会在 5175 先掐断，
    # 5174 还在生成，群友侧只会看到 502。只把 tts 这条放长。
    return _forward("/api/persona", sub, allow_guest=True,
                    post_timeout=300 if sub == "tts" else 60)


@app.route("/api/solo/<path:sub>", methods=["GET", "POST", "DELETE"])
def proxy_solo(sub):
    """独立游客页专用：一律游客，哪怕浏览器里带着登录 cookie。"""
    return _forward("/api/persona", sub, allow_guest=True, force_guest=True,
                    post_timeout=300 if sub == "tts" else 60)


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
    # 0.0.0.0 绝对不要：一开就等于绕过验证码直连。
    # 例外（2026-11-14）：Docker 容器里必须监听 0.0.0.0，否则容器内 127.0.0.1
    # 只有容器自己够得着，docker 端口映射进来会 Connection refused。容器里
    # 网络是隔离的，外界唯一入口是 compose 里显式声明的端口映射，验证码防线
    # 不受影响。真机不设 FRIEND_HOST，行为与原来完全一致。
    host = os.getenv("FRIEND_HOST", "127.0.0.1")
    app.run(host=host, port=PORT, debug=False, threaded=True)


if __name__ == "__main__":
    run()
