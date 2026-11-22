# -*- coding: utf-8 -*-
"""画廊「隐藏单本 / 清空一个文件夹」的体检（不动真实数据）。

跑法：

    python tools/_probe_gallery_hide.py

自己在 5199 起一个临时服务，库用 `state/gallery.db` 的**副本**，
缩略图缓存也导到临时目录 —— 隐藏和清空都会真写库，绝不能碰 5174 那份。
然后用无头 Edge 按手机宽度走一遍：

  1. 站长 tab 每张卡下面都有「隐藏」按钮，页面不横向溢出
  2. 点隐藏 → 卡从墙面消失、接口里也没有了
  3. 更多 → 看已隐藏 → 它在那儿，按钮变成「恢复」
  4. 恢复 → 回到正常墙面
  5. 选一个分类 → 更多里出现「清空「xxx」」→ 点掉 → 只有这个分类空
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

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
PORT = 5199
CDP_PORT = 9334
BASE = "http://127.0.0.1:%d" % PORT
TMP = tempfile.mkdtemp(prefix="galhide_")
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


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
    """临时服务：库和缓存都指向副本，本机 127.0.0.1:5199 直接算站主。"""
    db = os.path.join(TMP, "gallery.db")
    shutil.copy2(os.path.join(HERE, "state", "gallery.db"), db)
    gallery.DB = db
    gallery.CACHE = os.path.join(TMP, "cache")
    os.makedirs(gallery.CACHE, exist_ok=True)
    G.AGENT_PORT = PORT
    G.GALLERY_OWNER_QQ = "10001"

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

        # ── 1、每张卡下面都有隐藏按钮，手机不横向溢出 ──
        print("\n== 卡片上的隐藏按钮 ==")
        ncard = c.js("document.querySelectorAll('#grid .card').length") or 0
        nhb = c.js("document.querySelectorAll('#grid .card .hb').length") or 0
        check(nhb == ncard and ncard > 0, "每张卡都有按钮",
              "(%d 张卡 / %d 个按钮)" % (ncard, nhb))
        check(c.js("(function(){var b=document.querySelector('#grid .card .hb');"
                   "return b?b.textContent:''})()") == "隐藏", "按钮写着「隐藏」")
        check(c.js("(function(){var b=document.querySelector('#grid .card .hb');"
                   "if(!b)return false;var r=b.getBoundingClientRect();"
                   "return r.bottom<=window.innerHeight+2 && r.right<=window.innerWidth+2"
                   "})()"), "按钮落在卡片可视区域内")
        check(c.js("document.documentElement.scrollWidth<=window.innerWidth+1"),
              "页面没有横向溢出",
              "(%d / %d)" % (c.js("document.documentElement.scrollWidth"),
                             c.js("window.innerWidth")))

        # ── 2、点隐藏 ──
        print("\n== 隐藏一本 ==")
        title = c.js("document.querySelector('#grid .card .t').textContent")
        c.js("document.querySelector('#grid .card .hb').click()")
        time.sleep(1.2)
        check(c.js("document.querySelector('#grid .card').style.display") == "none",
              "卡片从墙面消失")
        shown = c.js("document.getElementById('toast').textContent") or ""
        check("已隐藏" in shown, "给了提示", "(%s)" % shown)
        gone = c.js("fetch('/api/gal/list?scope=owner&size=60')"
                    ".then(function(r){return r.json()})"
                    ".then(function(d){return (d.works||[]).filter("
                    "function(w){return w.title===%s}).length})"
                    % json.dumps(title))
        check(gone == 0, "接口里也没有了", "(%s)" % title)

        # ── 3、看已隐藏 ──
        print("\n== 更多 → 看已隐藏 ==")
        c.js("document.querySelector('.tool-more-btn').click()")
        time.sleep(0.4)
        labels = c.js("[].map.call(document.querySelectorAll('#toolMore button'),"
                      "function(b){return b.textContent})")
        check(any("看已隐藏" in x for x in labels), "菜单里有「看已隐藏」",
              "(%s)" % " / ".join(labels))
        c.js("[].forEach.call(document.querySelectorAll('#toolMore button'),"
             "function(b){if(b.textContent.indexOf('看已隐藏')>=0)b.click()})")
        wait_for(c, "document.querySelectorAll('#grid .card').length>0", 25)
        time.sleep(0.8)
        hb_now = c.js("(function(){var b=document.querySelector('#grid .card .hb');"
                      "return b?b.textContent:''})()")
        check(hb_now == "恢复", "按钮变成「恢复」", "(%s)" % hb_now)
        in_list = c.js("fetch('/api/gal/list?scope=owner&hidden=1&size=60')"
                       ".then(function(r){return r.json()})"
                       ".then(function(d){return (d.works||[]).filter("
                       "function(w){return w.title===%s}).length})" % json.dumps(title))
        check(in_list == 1, "它在已隐藏列表里")

        # ── 4、恢复 ──
        print("\n== 恢复 ==")
        c.js("document.querySelector('#grid .card .hb').click()")
        time.sleep(1.0)
        check(c.js("document.querySelector('#grid .card').style.display") == "none",
              "已隐藏列表里也拿掉了")
        c.js("document.querySelector('.tool-more-btn').click()")
        time.sleep(0.4)
        c.js("[].forEach.call(document.querySelectorAll('#toolMore button'),"
             "function(b){if(b.textContent.indexOf('回到未隐藏')>=0)b.click()})")
        wait_for(c, "document.querySelectorAll('#grid .card').length>0", 25)
        time.sleep(1.0)
        back = c.js("fetch('/api/gal/list?scope=owner&size=60')"
                    ".then(function(r){return r.json()})"
                    ".then(function(d){return (d.works||[]).filter("
                    "function(w){return w.title===%s}).length})" % json.dumps(title))
        check(back == 1, "回到正常墙面", "(%s)" % title)

        # ── 5、清空一个文件夹 ──
        print("\n== 清空一个分类 ==")
        chips = c.js("[].map.call(document.querySelectorAll('#grps button'),"
                     "function(b){return b.textContent})") or []
        print("   分类条：%s" % " / ".join(chips))
        target = next((x for x in chips if x and x != "全部"), None)
        if not target:
            check(False, "分类条上有可选分类")
        else:
            c.js("[].forEach.call(document.querySelectorAll('#grps button'),"
                 "function(b){if(b.textContent===%s)b.click()})" % json.dumps(target))
            wait_for(c, "document.querySelectorAll('#grid .card').length>0", 25)
            time.sleep(0.8)
            before = c.js("document.querySelectorAll('#grid .card').length") or 0
            c.js("document.querySelector('.tool-more-btn').click()")
            time.sleep(0.4)
            labels = c.js("[].map.call(document.querySelectorAll('#toolMore button'),"
                          "function(b){return b.textContent})")
            want = "清空「%s」" % target
            check(any(want in x for x in labels), "菜单里出现「%s」" % want,
                  "(%s)" % " / ".join(labels))
            c.js("[].forEach.call(document.querySelectorAll('#toolMore button'),"
                 "function(b){if(b.textContent.indexOf('清空「')>=0)b.click()})")
            time.sleep(2.5)
            t2 = c.js("document.getElementById('toast').textContent") or ""
            check("清掉了" in t2, "给了结果提示", "(%s)" % t2)
            # 清完回到「全部」：那个分类已经没有内容了，分类条上也不该再留着它
            left_in_grp = c.js(
                "fetch('/api/gal/list?scope=owner&size=60&grp='+"
                "encodeURIComponent(%s))"
                ".then(function(r){return r.json()})"
                ".then(function(d){return (d.works||[]).length})" % json.dumps(target))
            check(left_in_grp == 0, "这个分类清了", "(清之前 %d 张卡)" % before)
            chips2 = c.js("[].map.call(document.querySelectorAll('#grps button'),"
                          "function(b){return b.textContent})") or []
            check(target not in chips2, "分类条上那个标签跟着消失",
                  "(%s)" % " / ".join(chips2))
            left = c.js("fetch('/api/gal/list?scope=owner&size=60')"
                        ".then(function(r){return r.json()})"
                        ".then(function(d){return (d.works||[]).length})")
            check(left and left > 0, "别的分类还剩着", "(还剩 %s 个)" % left)
            grps = c.js("fetch('/api/gal/list?scope=owner&size=60')"
                        ".then(function(r){return r.json()})"
                        ".then(function(d){return (d.grps||[]).length})")
            check(grps and grps > 0, "分类条没被清空", "(%s 个)" % grps)
    finally:
        try:
            proc.terminate()
        except Exception:
            pass
        shutil.rmtree(TMP, True)

    print("\n结果：%d 通过，%d 失败" % (ok, bad))
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
