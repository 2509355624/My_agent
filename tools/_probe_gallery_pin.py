# -*- coding: utf-8 -*-
"""画廊「看已隐藏」密码门的界面体检（不动真实数据）。

跑法：
    python tools/_probe_gallery_pin.py

自己在 5199 起临时服务，库用副本、缩略图导临时目录，密码文件也在临时
目录里设成 839125。无头 Edge 按手机宽度走一遍：

  1. 更多 → 看已隐藏 → 弹密码框，墙面没变（还没进）
  2. 输错 6 位 → 留在原墙、显示错误、输入框清空
  3. 输对 839125 → 进隐藏墙、按钮变「恢复」、顶部有「← 返回画廊」
  4. 点「← 返回画廊」→ 回正常墙
  5. 再点看已隐藏 → 又要重新输（不记住验证态）
  6. 密码框本身手机不横向溢出
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

import websocket
from flask import Flask, Response

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from app.features.gallery import gallery           # noqa: E402
from app.features.gallery import routes as G      # noqa: E402
from app.features.gallery import pin              # noqa: E402

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
PORT = 5199
CDP_PORT = 9336
BASE = "http://127.0.0.1:%d" % PORT
TMP = tempfile.mkdtemp(prefix="galpin_")


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
        time.sleep(0.25)
    return False


def make_app():
    """临时服务：库、缓存、密码文件全指向临时目录。"""
    db = os.path.join(TMP, "gallery.db")
    shutil.copy2(os.path.join(HERE, "state", "gallery.db"), db)
    gallery.DB = db
    gallery.CACHE = os.path.join(TMP, "cache")
    os.makedirs(gallery.CACHE, exist_ok=True)
    G.AGENT_PORT = PORT
    G.GALLERY_OWNER_QQ = "10001"
    pin._AUTH = os.path.join(TMP, "gallery_pin.json")
    pin._cache = None
    pin._fails = 0
    pin._locked_until = 0.0
    pin.set_pin("839125")

    app = Flask(__name__)
    app.register_blueprint(G.bp)

    @app.route("/gallery")
    def gallery_page():
        with open(os.path.join(HERE, "web", "gallery.html"),
                  encoding="utf-8") as f:
            return Response(f.read(), mimetype="text/html")

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
            urllib.request.urlopen(BASE + "/gallery", timeout=2).read(1)
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

    def open_menu(c):
        c.js("document.querySelector('.tool-more-btn').click()")
        time.sleep(0.35)

    def click_hidden(c):
        c.js("[].forEach.call(document.querySelectorAll('#toolMore button'),"
             "function(b){if(b.textContent.indexOf('看已隐藏')>=0)b.click()})")

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

        c.call("Page.navigate", {"url": BASE + "/gallery"})
        wait_for(c, "document.readyState==='complete'", 25)
        check(wait_for(c, "document.querySelectorAll('#grid .card').length>0", 30),
              "墙面出卡片")

        # ── 1、点看已隐藏 → 弹密码框，墙没变 ──
        print("\n== 点「看已隐藏」==")
        before = c.js("document.querySelector('#grid .card .hb').textContent")
        open_menu(c)
        labels = c.js("[].map.call(document.querySelectorAll('#toolMore button'),"
                      "function(b){return b.textContent})") or []
        check(any("看已隐藏" in x and "密码" in x for x in labels),
              "菜单标注需密码", "(%s)" % " / ".join(labels))
        click_hidden(c)
        time.sleep(0.5)
        check(c.js("document.getElementById('pinbox').classList.contains('on')"),
              "弹出了密码框")
        check(c.js("document.querySelector('#grid .card .hb').textContent") == before,
              "没输密码前墙面没变")
        check(c.js("document.documentElement.scrollWidth<=window.innerWidth+1"),
              "密码框手机不横向溢出",
              "(%d / %d)" % (c.js("document.documentElement.scrollWidth"),
                             c.js("window.innerWidth")))

        # ── 2、输错 ──
        print("\n== 输错密码 ==")
        c.js("document.getElementById('pininput').value='000000';"
             "document.getElementById('pinok').click()")
        time.sleep(0.8)
        err = c.js("document.getElementById('pinerr').textContent") or ""
        check("密码" in err or "不对" in err, "显示错误", "(%s)" % err)
        check(c.js("document.getElementById('pininput').value") == "",
              "输入框清空了")
        check(c.js("document.getElementById('pinbox').classList.contains('on')"),
              "还停在密码框")
        check(c.js("document.querySelector('#grid .card .hb').textContent") == before,
              "墙面仍是正常那面")

        # ── 3、输对 ──
        print("\n== 输对 839125 ==")
        c.js("document.getElementById('pininput').value='839125';"
             "document.getElementById('pinok').click()")
        wait_for(c, "!document.getElementById('pinbox').classList.contains('on')", 20)
        time.sleep(1.0)
        check(not c.js("document.getElementById('pinbox').classList.contains('on')"),
              "密码框关掉了")
        hb = c.js("(function(){var b=document.querySelector('#grid .card .hb');"
                  "return b?b.textContent:''})()")
        check(hb == "恢复", "进隐藏墙了，按钮变「恢复」", "(%s)" % hb)
        back = c.js("[].map.call(document.querySelectorAll('#tools button'),"
                    "function(b){return b.textContent}).join('|')")
        check("返回画廊" in back, "顶部有醒目的「← 返回画廊」", "(%s)" % back)

        # ── 4、返回画廊 ──
        print("\n== 返回画廊 ==")
        c.js("[].forEach.call(document.querySelectorAll('#tools button'),"
             "function(b){if(b.textContent.indexOf('返回画廊')>=0)b.click()})")
        wait_for(c, "(function(){var b=document.querySelector('#grid .card .hb');"
                    "return b&&b.textContent==='隐藏'})()", 25)
        time.sleep(0.6)
        hb2 = c.js("(function(){var b=document.querySelector('#grid .card .hb');"
                   "return b?b.textContent:''})()")
        check(hb2 == "隐藏", "回到正常墙了", "(%s)" % hb2)

        # ── 5、再进要重输 ──
        print("\n== 再点要重新输 ==")
        open_menu(c)
        click_hidden(c)
        time.sleep(0.5)
        check(c.js("document.getElementById('pinbox').classList.contains('on')"),
              "第二次进来又要输密码")

        print("\n结果：%d 过 / %d 挂" % (ok, bad))
        return 1 if bad else 0
    finally:
        try:
            proc.terminate()
        except Exception:
            pass
        shutil.rmtree(TMP, True)


if __name__ == "__main__":
    raise SystemExit(main())
