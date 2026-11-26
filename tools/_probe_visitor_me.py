# -*- coding: utf-8 -*-
"""访客在 AI 画廊里点右上角人形，看到的应该是「还没登录 + 登录按钮」。

2026-11-26 用户报：「访客模式登录之后，进到 AI 画廊，点右上角头像，
返回一个空白界面」。空白会让人以为一直在加载。

这里在 5199 起一个只读的临时站（工作台静态页照发，/me 用假数据顶替成
访客），无头 Edge 走一遍：

  1. 顶栏那个人形是隐掉的（apply 收口），但强行点也进得去 → 不能空白
  2. 「我的」这页里有一句「还没登录」和一颗蓝按钮「登录」
  3. 这颗按钮真的带主色（不是白底黑字）
  4. 页面不横向溢出、按钮落在手机屏内

跑法：python tools/_probe_visitor_me.py
"""
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
TMP = tempfile.mkdtemp(prefix="vme_")
WS = os.path.join(HERE, "web", "workspace")
HITS = []          # 服务端真收到的请求（reload 会丢掉页面里的记录）


class CDP:
    def __init__(self, ws):
        self.ws = ws
        self.n = 0
        self.hits = []
        self.logs = []

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

    def drain(self):
        """把这段时间收到的 CDP 事件都收进来（请求 / 异常）。"""
        self.ws.settimeout(0.05)
        try:
            while True:
                msg = json.loads(self.ws.recv())
                m = msg.get("method")
                if m == "Network.requestWillBeSent":
                    self.hits.append(msg["params"]["request"]["url"])
                elif m in ("Runtime.exceptionThrown", "Network.loadingFailed"):
                    self.logs.append(str(msg.get("params"))[:200])
        except Exception:
            pass
        finally:
            try:
                self.ws.settimeout(30)
            except Exception:
                pass

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
        time.sleep(0.25)
    return False


TYPES = {".css": "text/css", ".js": "application/javascript",
         ".html": "text/html; charset=utf-8"}


def make_app():
    app = Flask(__name__)

    @app.route("/me")
    def me():
        return jsonify({"ok": True, "role": "visitor", "qq": "",
                        "session": "v-probe",
                        "quota": {"left": 5, "limit": 5}})

    @app.route("/login/visitor", methods=["POST"])
    def visitor():
        return jsonify({"ok": True})

    @app.route("/logout", methods=["POST"])
    def logout():
        HITS.append("/logout")
        return jsonify({"ok": True})

    @app.route("/")
    def root():
        return redirect_ws()

    def redirect_ws():
        return Response("", status=302, headers={"Location": "/workspace/chat.html"})

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

    # 工作台会打几个接口，给空壳就够（不验数据，只验「我的」这块渲染）
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
            print("  ✓ %s %s" % (msg, extra))
        else:
            bad += 1
            print("  ✗ %s %s" % (msg, extra))

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
        c.call("Console.enable")
        c.call("Network.enable")

        c.call("Page.navigate", {"url": BASE + "/workspace/chat.html"})
        wait_for(c, "document.readyState==='complete'", 25)
        check(wait_for(c, "window.WSVisitor && window.WSVisitor.state().loaded", 25),
              "访客身份判定完成")
        check(c.js("window.WSVisitor.state().role==='visitor'"), "识别为 visitor")

        # 进 AI 画廊 → 强点右上角人形（顶栏那个按钮对访客是隐的，直接 push）
        print("\n== 进 AI 画廊点人形 ==")
        c.js("window.WSStack && 0; 1")
        c.js("(function(){var b=document.querySelector('.chip[data-chip=\"gallery\"]')"
             "||document.querySelector('[data-chip=\"gallery\"]');"
             "if(b){b.click();return 'chip';}"
             "return 'none'})()")
        time.sleep(1.0)
        opened = c.js("!!document.querySelector('.view[data-view=\"gallery\"]')")
        if not opened:
            c.js("window.WSStack||null")
        check(opened, "AI 画廊子页打开了")

        c.js("(function(){var v=document.querySelector('.view[data-view=\"gallery\"]');"
             "var m=v&&v.querySelector('[data-me]');"
             "if(m){m.hidden=false;m.click();return 1}"
             "return 0})()")
        time.sleep(1.0)

        me_view = "document.querySelector('.view[data-view=\"me\"]')"
        check(c.js("!!" + me_view), "「我的」子页建出来了")

        print("\n== 「我的」里是什么 ==")
        n = c.js("(function(){var v=%s;return v?v.querySelector('.vbody').children.length:0})()" % me_view)
        check(n and n > 0, "内容区不是空的", "(%s 个子节点)" % n)
        txt = c.js("(function(){var v=%s;return v?v.querySelector('.vbody').innerText:''})()" % me_view) or ""
        check("还没登录" in txt, "写着「还没登录」", "(%r)" % txt.replace("\n", " / ")[:60])
        check(c.js("(function(){var v=%s;var b=v&&v.querySelector('.vguest-btn');"
                   "return b?b.textContent:''})()" % me_view) == "登录",
              "有一颗「登录」按钮")
        bg = c.js("(function(){var v=%s;var b=v&&v.querySelector('.vguest-btn');"
                  "return b?getComputedStyle(b).backgroundColor:''})()" % me_view)
        check(bg.startswith("rgb(61, 123, 245"), "按钮是品牌蓝", "(%s)" % bg)
        fg = c.js("(function(){var v=%s;var b=v&&v.querySelector('.vguest-btn');"
                  "return b?getComputedStyle(b).color:''})()" % me_view)
        check("255" in fg, "字是白的", "(%s)" % fg)
        check(c.js("document.documentElement.scrollWidth<=window.innerWidth+1"),
              "页面没有横向溢出",
              "(%d / %d)" % (c.js("document.documentElement.scrollWidth"),
                             c.js("window.innerWidth")))
        inbox = c.js("(function(){var v=%s;var b=v&&v.querySelector('.vguest-btn');"
                     "if(!b)return false;var r=b.getBoundingClientRect();"
                     "return r.height>20 && r.bottom<=window.innerHeight+2"
                     " && r.right<=window.innerWidth+2})()" % me_view)
        check(inbox, "按钮在手机屏内且看得见")

        try:
            shot = c.call("Page.captureScreenshot", {"format": "png"})
            import base64
            out = os.path.join(HERE, "state", "_probe_visitor_me.png")
            open(out, "wb").write(base64.b64decode(shot["data"]))
            print("  截图：%s" % out)
        except Exception as e:
            print("  截图失败：%s" % e)

        # 点登录：应该先打 /logout 再 reload。reload 之后 JS 变量全没了，
        # 所以请求从 CDP 的 Network 事件里看。
        c.hits = []
        HITS[:] = []
        c.js("window.__oldDoc = 1; 1")
        c.js("(function(){var v=%s;var b=v&&v.querySelector('.vguest-btn');"
             "if(b)b.click();return 1})()" % me_view)
        time.sleep(1.0)
        c.drain()
        check("/logout" in HITS, "点「登录」先退出访客身份",
              "(%s)" % (HITS or "服务端没收到"))
        # 退出之后回的是站首登录页：这里临时站没有登录页，只验「页面确实
        # 重新加载过」——旧文档上一个标记在 reload 之后就没了。
        check(not c.js("!!window.__oldDoc"), "页面重新加载了")

        print("\n%d 通过 / %d 失败" % (ok, bad))
        return 0 if bad == 0 else 1
    finally:
        try:
            proc.terminate()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
