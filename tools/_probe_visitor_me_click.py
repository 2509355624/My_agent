# -*- coding: utf-8 -*-
"""访客登录后落到的「我的」页，以及之后 AI 创作里的人形按钮。

2026-11-26 用户报（第二次）：站主 → 点「登录」→ 登录页「先以访客身份逛逛」
→ 落在「我的」、有颗「重试」、闪一下进 AI 创作 → **点右上角人形没反应**；
彻底退回聊天页再进 AI 创作，人形又正常。

根因（本探针复现的就是这两条）：

1. **reload 之后页面恢复出「我的」，但身份还没回来时 `visitor.js` 的 role
   默认是 `guest`。** `WSViews.build` 已经给了正常的 me 页（带账号条 + 作品
   列表），`apply()` 里的 `dropMeView()` 却把它摘掉——用户看到的是
   「参数不对 / 重试」那一屏，而且这页**没有访客卡**。
2. 同一时刻点人形无效：`stack.js` 的 push 去重按 `dataset.view` 比，栈里
   留着一页同名 `me`，点人形走 `push("me")` 直接被当成「栈顶已经是 me」吃掉。

断言（改动前应红、改动后应全绿）：

  A. 身份还没回来时恢复出来的 me 页仍在（不能被摘成空白）
  B. 身份确认为 visitor 之后，那页换成访客卡（不是「参数不对」）
  C. 此时点 gallery 顶栏人形，栈顶必须变成 me，且眼前这页有「登录」按钮
  D. 栈里不会堆出一摞同名 me

跑法：python tools/_probe_visitor_me_click.py
"""
import base64
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

import websocket
from flask import Flask, Response, jsonify

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
PORT = 5199
CDP_PORT = 9334
BASE = "http://127.0.0.1:%d" % PORT
TMP = tempfile.mkdtemp(prefix="mvme_")
WS = os.path.join(HERE, "web", "workspace")
STATE = {}         # 假 /me 的字段（站主 → 访客）

TYPES = {".css": "text/css", ".js": "application/javascript",
         ".html": "text/html; charset=utf-8"}


class CDP:
    def __init__(self, ws):
        self.ws = ws
        self.n = 0

    def call(self, method, params=None, timeout=25):
        self.n += 1
        mid = self.n
        self.ws.send(json.dumps({"id": mid, "method": method,
                                 "params": params or {}}))
        t0 = time.time()
        while time.time() - t0 < timeout:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(msg["error"])
                return msg.get("result", {})
        raise TimeoutError(method)

    def js(self, expr, timeout=25):
        r = self.call("Runtime.evaluate", {
            "expression": expr, "returnByValue": True, "awaitPromise": True,
            "userGesture": True}, timeout=timeout)
        if r.get("exceptionDetails"):
            raise RuntimeError(str(r["exceptionDetails"])[:300])
        return r.get("result", {}).get("value")


def wait_for(c, expr, secs=20):
    t0 = time.time()
    while time.time() - t0 < secs:
        try:
            if c.js(expr):
                return True
        except Exception:
            pass
        time.sleep(0.2)
    return False


def make_app():
    app = Flask(__name__)

    @app.route("/me")
    def me():
        # 手机上这趟往返就是几百毫秒（用户那次落点判定空了 697ms）
        time.sleep(0.7)
        d = {"ok": True, "role": "user", "qq": "10086",
             "quota": {"left": 5, "limit": 5}}
        d.update(STATE)
        return jsonify(d)

    @app.route("/login/visitor", methods=["POST"])
    def visitor():
        return jsonify({"ok": True})

    @app.route("/logout", methods=["POST"])
    def logout():
        return jsonify({"ok": True})

    @app.route("/workspace/chat.html")
    def chat():
        with open(os.path.join(WS, "chat.html"), encoding="utf-8") as f:
            return Response(f.read(), mimetype="text/html")

    @app.route("/workspace/<path:sub>")
    def ws_static(sub):
        fp = os.path.join(WS, sub)
        if not os.path.isfile(fp):
            return Response("nope", status=404)
        ext = os.path.splitext(fp)[1]
        with open(fp, "rb") as f:
            return Response(f.read(), mimetype=TYPES.get(ext, "text/plain"))

    @app.route("/api/gal/mine")
    def mine():
        return jsonify({"ok": False, "error": "参数不对"}), 400

    @app.route("/api/<path:sub>")
    def api(sub):
        return jsonify({"ok": True})

    return app


def main():
    if not os.path.isfile(EDGE):
        raise SystemExit("找不到 Edge：%s" % EDGE)

    app = make_app()
    threading.Thread(
        target=lambda: app.run(host="127.0.0.1", port=PORT, threaded=True,
                               use_reloader=False),
        daemon=True).start()
    for _ in range(40):
        try:
            urllib.request.urlopen(BASE + "/workspace/chat.html", timeout=2).read(1)
            break
        except Exception:
            time.sleep(0.25)
    else:
        raise SystemExit("临时服务没起来")

    proc = subprocess.Popen([
        EDGE, "--headless=new", "--disable-gpu", "--no-first-run",
        "--remote-debugging-port=%d" % CDP_PORT, "--user-data-dir=" + TMP,
        "--window-size=390,844", "about:blank",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    ok = 0
    bad = 0

    def check(cond, msg, extra=""):
        nonlocal ok, bad
        if cond:
            ok += 1
            print("  [OK] %s %s" % (msg, extra))
        else:
            bad += 1
            print("  [XX] %s %s" % (msg, extra))

    try:
        ws_url = None
        for _ in range(60):
            try:
                d = json.loads(urllib.request.urlopen(
                    "http://127.0.0.1:%d/json/list" % CDP_PORT, timeout=2).read())
                for t in d:
                    if t.get("type") == "page":
                        ws_url = t["webSocketDebuggerUrl"]
                        break
                if ws_url:
                    break
            except Exception:
                pass
            time.sleep(0.3)
        if not ws_url:
            raise SystemExit("连不上 Edge 调试端口")
        ws = websocket.create_connection(ws_url, timeout=30)
        c = CDP(ws)
        c.call("Page.enable")
        c.call("Runtime.enable")
        c.call("Emulation.setDeviceMetricsOverride",
               {"width": 390, "height": 844, "deviceScaleFactor": 2,
                "mobile": True})

        # ── 用户点「登录」那一刻：栈里正停着站主的「我的」 ──
        STATE.clear()
        c.call("Page.navigate", {"url": BASE + "/workspace/chat.html"})
        wait_for(c, "document.readyState==='complete'", 25)
        wait_for(c, "window.WSVisitor && window.WSVisitor.state().loaded", 25)
        c.js("window.WSStack.push('me')")
        time.sleep(0.5)

        # ── 「先以访客身份逛逛」→ reload，栈从 sessionStorage 恢复 ──
        STATE.update({"role": "visitor", "qq": ""})
        print("\n== 访客身份 reload，栈里恢复出「我的」==")
        c.call("Page.reload", {})
        wait_for(c, "!!window.WSVisitor", 15)
        role_early = c.js("window.WSVisitor.state().role")
        loaded_early = c.js("window.WSVisitor.state().loaded")
        print("    身份未回时 role=%r loaded=%r" % (role_early, loaded_early))
        check(not loaded_early, "先确认这一刻身份确实没回来")

        has_page = c.js("!!document.querySelector('.view[data-view=\"me\"]')")
        top_ok = c.js("(function(){var t=window.WSStack.top();"
                      "return !!(t&&t.parentNode)})()")
        check(has_page, "A. 恢复出来的「我的」还在（不该被摘成空白）")
        check(top_ok, "A2. 栈顶那页真在 DOM 里（top() 不能指向已摘掉的节点）")
        inner = c.js("(function(){var v=document.querySelector('.view[data-view=\"me\"]');"
                     "return v?v.innerText.replace(/\\n+/g,' / ').slice(0,44):'无页'})()")
        print("    这一刻页面文字：%r" % inner)

        wait_for(c, "window.WSVisitor.state().loaded", 15)
        time.sleep(0.3)
        check(c.js("window.WSVisitor.state().role==='visitor'"), "身份回来 = visitor")
        card = c.js("(function(){var v=document.querySelector('.view[data-view=\"me\"]');"
                    "return !!(v&&v.querySelector('.vguest-btn'))})()")
        inner = c.js("(function(){var v=document.querySelector('.view[data-view=\"me\"]');"
                     "return v?v.innerText.replace(/\\n+/g,' / ').slice(0,44):'无页'})()")
        check(card, "B. 「我的」换成访客卡（不是「参数不对」）", "(%s)" % inner)

        # ── 用户这时进了 AI 创作 ──
        print("\n== 进 AI 创作，点右上角人形 ==")
        # 用户是「点右上角头像进 AI 创作」么？不是——他从「我的」那页退出来
        # 再点 AI 创作也行。这里模拟常见的另一种：先把「我的」退掉，再进。
        c.js("window.WSStack.back()")
        time.sleep(0.6)
        c.js("window.WSStack.push('gallery')")
        time.sleep(1.0)
        check(c.js("(window.WSStack.top()||{}).dataset.view==='gallery'"), "栈顶是 gallery")

        # 真实手指：点顶栏人形的坐标，且这一下得真打在按钮上（没被谁盖住）
        hit = c.js("(function(){var v=document.querySelector('.view[data-view=\"gallery\"]');"
                   "var b=v&&v.querySelector('[data-me]');if(!b)return 'no-btn';"
                   "var r=b.getBoundingClientRect();"
                   "var el=document.elementFromPoint(r.x+r.width/2, r.y+r.height/2);"
                   "return JSON.stringify({x:r.x+r.width/2,y:r.y+r.height/2,"
                   "on:!!(el&&el.closest('[data-me]'))})})()")
        print("    人形命中：%s" % hit)
        pt = json.loads(hit)
        check(pt["on"], "人形按钮没被别的元素盖住")
        c.call("Input.dispatchTouchEvent",
               {"type": "touchStart", "touchPoints": [{"x": pt["x"], "y": pt["y"]}]})
        c.call("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
        time.sleep(0.9)
        top = c.js("(window.WSStack.top()||{}).dataset.view")
        face = c.js("(function(){var t=window.WSStack.top();"
                    "return !!(t&&t.querySelector('.vguest-btn'))})()")
        nme = c.js("document.querySelectorAll('#stack > .view[data-view=\"me\"]').length")
        check(top == "me", "C. 点人形后栈顶是 me", "(栈顶=%s)" % top)
        check(face, "C. 眼前这页有「登录」按钮")
        check((nme or 0) <= 1, "D. 栈里没有堆出多个 me", "(%s 个)" % nme)

        shot = c.call("Page.captureScreenshot", {"format": "png"})
        out = os.path.join(HERE, "state", "_probe_visitor_me_click.png")
        open(out, "wb").write(base64.b64decode(shot["data"]))
        print("    截图：%s" % out)

        print("\n%d 通过 / %d 失败" % (ok, bad))
        return 0 if bad == 0 else 1
    finally:
        try:
            proc.terminate()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
