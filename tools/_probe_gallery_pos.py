# -*- coding: utf-8 -*-
"""画廊「翻页 + 记住看到哪儿」的体检（只读，不改任何数据）。

真的开一个无头 Edge 连本机 5174，按手机宽度走一遍：
  1. 墙面加载出来没有、翻页条在不在
  2. 往下滑 → localStorage 有没有记「第几页 / 第几个」
  3. 点「下一页」→ 是不是真的翻过去
  4. 刷新页面 → 是不是回到刚才那一页
  5. 进一个多图作品集 → 滚到中间 → 返回 → 再进 → 是不是回到刚才那张

    python tools/_probe_gallery_pos.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

import websocket

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
PORT = 9333
BASE = "http://127.0.0.1:5174"
TMP = tempfile.mkdtemp(prefix="galpos_")


class CDP:
    def __init__(self, ws):
        self.ws = ws
        self.n = 0

    def call(self, method, params=None, timeout=20):
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

    def js(self, expr, timeout=20):
        r = self.call("Runtime.evaluate", {
            "expression": expr, "returnByValue": True, "awaitPromise": True,
            "userGesture": True}, timeout=timeout)
        res = r.get("result", {})
        if r.get("exceptionDetails"):
            raise RuntimeError(str(r["exceptionDetails"])[:300])
        return res.get("value")


def wait_for(c, expr, secs=15):
    t0 = time.time()
    while time.time() - t0 < secs:
        try:
            if c.js(expr):
                return True
        except Exception:
            pass
        time.sleep(0.25)
    return False


def main():
    if not os.path.isfile(EDGE):
        raise SystemExit("找不到 Edge：%s" % EDGE)
    try:
        urllib.request.urlopen(BASE + "/gallery", timeout=5).read()
    except Exception as e:
        raise SystemExit("本机 5174 没起来：%s" % e)

    proc = subprocess.Popen([
        EDGE, "--headless=new", "--disable-gpu", "--no-first-run",
        "--remote-debugging-port=%d" % PORT, "--user-data-dir=" + TMP,
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
        for _ in range(40):
            try:
                d = json.loads(urllib.request.urlopen(
                    "http://127.0.0.1:%d/json/list" % PORT, timeout=2).read())
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
            raise SystemExit("连不上 Edge 的调试端口")

        ws = websocket.create_connection(ws_url, timeout=25)
        c = CDP(ws)
        c.call("Page.enable")
        c.call("Runtime.enable")
        # 记下每个新文档发出的接口请求 + 每次读 gal_pos_v1 读到了什么：
        # 排「刷新后为什么只加载了一页」
        c.call("Page.addScriptToEvaluateOnNewDocument", {"source":
            "window.__API=[];(function(){var of=window.fetch;"
            "window.fetch=function(u){try{window.__API.push(String(u))}catch(e){}"
            ";return of.apply(this,arguments)}})();"
            "(function(){var SP=Storage.prototype,og=SP.getItem;"
            "SP.getItem=function(k){var v=og.call(this,k);"
            "if(k==='gal_pos_v1'){(window.__POSREADS=window.__POSREADS||[])"
            ".push(String(v).slice(0,160))}return v}})();"})

        print("\n== 墙面 ==")
        c.call("Page.navigate", {"url": BASE + "/gallery"})
        wait_for(c, "document.readyState==='complete'", 20)
        got = wait_for(c, "document.querySelectorAll('#grid .card').length>0", 25)
        check(got, "首屏出卡片")
        n0 = c.js("document.querySelectorAll('#grid .card').length") or 0
        check(n0 >= 24, "首屏至少一页", "(%d 张卡)" % n0)
        check(c.js("!!document.getElementById('pager')"), "翻页条在 DOM 里")
        check(c.js("document.getElementById('pager').classList.contains('on')"),
              "翻页条已显示")
        check(c.js("(function(){var v=+document.getElementById('pgIn').value;"
                   "return v===1})()"), "初始页码是 1")

        # 往下滑两屏，等节流写一次
        c.js("window.scrollTo(0, document.body.scrollHeight*0.75)")
        time.sleep(1.4)
        saved = c.js("localStorage.getItem('gal_pos_v1')")
        try:
            pos = json.loads(saved or "{}")
        except Exception:
            pos = {}
        wall = [v for k, v in pos.items() if k.startswith("w|")]
        check(bool(wall), "滑过之后记下了墙的位置", str(wall[-1] if wall else ""))
        check(bool(wall) and wall[-1].get("p", 0) >= 2, "记下的页数 > 1")

        # 下一页
        before = c.js("window.scrollY") or 0
        c.js("document.getElementById('pgNext').click()")
        time.sleep(0.8)
        after = c.js("window.scrollY") or 0
        pg = c.js("+document.getElementById('pgIn').value") or 0
        check(after > before + 200, "点下一页确实往下翻了",
              "(%.0f → %.0f, 第 %d 页)" % (before, after, pg))

        # 刷新，看回不回来
        target_y = after
        c.call("Page.reload")
        wait_for(c, "document.querySelectorAll('#grid .card').length>0", 30)
        time.sleep(3.2)                     # 等它把缺的页补齐再滚
        y2 = c.js("window.scrollY") or 0
        pg2 = c.js("+document.getElementById('pgIn').value") or 0
        if abs(y2 - target_y) >= 900 and pg2 < pg:
            print("   debug pos:", c.js("localStorage.getItem('gal_pos_v1')"))
            print("   debug cards:", c.js("document.querySelectorAll('#grid .card').length"))
            print("   debug pages:", c.js("document.getElementById('pgSum').textContent"))
            print("   debug api:", c.js(
                "JSON.stringify((window.__API||[]).filter(function(u){"
                "return u.indexOf('/api/gal/')>=0}))"))
            print("   debug reads:", c.js("JSON.stringify(window.__POSREADS||[])"))
            print("   debug more:", c.js(
                "fetch('/api/gal/list?scope=owner&page=1&size=24')"
                ".then(function(r){return r.json()})"
                ".then(function(d){return d.has_more+':'+(d.works||[]).length})"))
            print("   debug firstcard:", c.js(
                "JSON.stringify((function(){var r=document.querySelector('#grid .card')"
                ".getBoundingClientRect();return [r.top,r.height,window.scrollY]})())"))
        check(abs(y2 - target_y) < 900 or pg2 >= pg,
              "刷新后回到刚才那一页",
              "(%.0f → %.0f, 第 %d 页)" % (target_y, y2, pg2))

        print("\n== 作品集里看到第几张 ==")
        # 直接问接口哪一本图最多：这一屏卡片里最大的可能只有 3 张，
        # 「看到第 60 张」这种场景得用真的几百张那本
        info = c.js("fetch('/api/gal/list?scope=owner&grp=new&page=1&size=120')"
                    ".then(function(r){return r.json()}).then(function(d){"
                    "var b=null;(d.works||[]).forEach(function(w){"
                    "if(!b||w.n>b.n)b=w});"
                    "return b?JSON.stringify([b.id,b.n]):''})")
        wid, wn = (json.loads(info) if info else (0, 0))
        if not wid or wn < 20:
            print("  （没有图多的作品集，跳过详情检查）")
        else:
            c.js("location.hash='#w=%d'" % wid)
            wait_for(c, "document.getElementById('detail').classList.contains('on')", 20)
            nshot = c.js("document.querySelectorAll('#dgrid .shot').length") or 0
            check(nshot >= 20, "作品集网格打开了", "(#%d, %d 张)" % (wid, nshot))
            if nshot >= 20:
                c.js("(function(){var g=document.getElementById('dgrid');"
                     "g.scrollTop=g.scrollHeight*0.6;})()")
                time.sleep(1.2)
                seen = c.js("(function(){var p=JSON.parse(localStorage.getItem"
                            "('gal_pos_v1')||'{}');var k=Object.keys(p).filter("
                            "function(x){return x.indexOf('d|')===0});"
                            "return k.length?k[0]+'='+JSON.stringify(p[k[0]]):''})()")
                check(bool(seen), "详情页记下了「第几张」", seen)
                c.js("document.getElementById('dback').click()")
                time.sleep(0.6)
                c.js("location.hash='#w=%d'" % wid)
                wait_for(c, "document.getElementById('detail').classList.contains('on')", 20)
                time.sleep(3.0)             # 等图撑开 + 几次校正
                top = c.js("document.getElementById('dgrid').scrollTop") or 0
                check(top > 200, "再进来回到了刚才的位置", "(scrollTop=%.0f)" % top)
                c.js("document.getElementById('dback').click()")
                time.sleep(0.4)

        print("\n== 手机端有没有被翻页条挡住/撑宽 ==")
        w = c.js("document.documentElement.scrollWidth") or 0
        vw = c.js("window.innerWidth") or 0
        check(w <= vw + 1, "页面没有被撑出横向滚动", "(%d / %d)" % (w, vw))
        box = c.js("(function(){var p=document.getElementById('pager');"
                   "var r=p.getBoundingClientRect();"
                   "return JSON.stringify([r.left,r.right,window.innerWidth])})()")
        if box:
            l, r, vw2 = json.loads(box)
            check(l >= 0 and r <= vw2, "翻页条完整落在屏幕内", box)

        print("\n结果：%d 过 / %d 不过" % (ok, bad))
        ws.close()
        return 0 if bad == 0 else 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
        shutil.rmtree(TMP, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
