# -*- coding: utf-8 -*-
"""扮演记录「长消息折叠」的体检（只读）。

用无头 Edge 真的打开 /persona_admin，按手机宽度点进一个会话：
  1. 长的那几条是不是收起成 8 行（.b.fold）
  2. 点「展开全文」是不是真的展开、再点收回去
  3. 切到桌面宽度刷新后，是不是不折（宽屏一条占不满屏）

    python tools/_probe_persona_fold.py
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
PORT = 9334
BASE = "http://127.0.0.1:5174"
TMP = tempfile.mkdtemp(prefix="personafold_")


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
        if r.get("exceptionDetails"):
            raise RuntimeError(str(r["exceptionDetails"])[:300])
        return r.get("result", {}).get("value")


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
    proc = subprocess.Popen([
        EDGE, "--headless=new", "--disable-gpu", "--no-first-run",
        "--remote-debugging-port=%d" % PORT, "--user-data-dir=" + TMP,
        "--window-size=390,844", "about:blank",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    ok = bad = 0

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
            raise SystemExit("连不上 Edge 调试端口")
        ws = websocket.create_connection(ws_url, timeout=25)
        c = CDP(ws)
        c.call("Page.enable")
        c.call("Runtime.enable")
        c.call("Emulation.setDeviceMetricsOverride",
               {"width": 390, "height": 844, "deviceScaleFactor": 2, "mobile": True})

        print("\n== 手机宽度 ==")
        c.call("Page.navigate", {"url": BASE + "/persona_admin"})
        wait_for(c, "document.readyState==='complete'", 20)
        got = wait_for(c, "document.querySelectorAll('#list .row, #list > *').length>0", 25)
        check(got, "会话列表出来了")
        # 点第一个会话
        c.js("(function(){var el=document.querySelector('#list .row')||"
             "document.querySelector('#list > *');if(el)el.click()})()")
        got = wait_for(c, "document.querySelectorAll('#log .msg').length>0", 25)
        check(got, "会话里读出消息")
        n = c.js("document.querySelectorAll('#log .msg').length") or 0
        nf = c.js("document.querySelectorAll('#log .b.fold').length") or 0
        nlong = c.js("(function(){var k=0;document.querySelectorAll('#log .b')"
                     ".forEach(function(b){if(b.textContent.length>400)k++});return k})()")
        check(nf > 0, "长消息被收起了", "(%d 条消息里 %d 条折了，长消息 %d 条)" % (n, nf, nlong))
        h = c.js("(function(){var b=document.querySelector('#log .b.fold');"
                 "return b?b.getBoundingClientRect().height:0})()") or 0
        check(0 < h < 400, "收起后高度就是几行", "(%.0f px)" % h)
        # 展开
        c.js("(function(){var m=document.querySelector('#log .b.fold');"
             "if(m&&m.nextSibling)m.nextSibling.click()})()")
        time.sleep(0.3)
        h2 = c.js("(function(){var b=document.querySelector('#log .b.fold.open');"
                  "return b?b.getBoundingClientRect().height:0})()") or 0
        check(h2 > h + 20, "点「展开全文」真的展开了", "(%.0f → %.0f px)" % (h, h2))
        label = c.js("(function(){var m=document.querySelector('#log .b.fold.open');"
                     "return m&&m.nextSibling?m.nextSibling.textContent:''})()")
        check(label == "收起", "按钮变成「收起」", "(%r)" % (label,))
        # 自动刷新每 5 秒拉一次：展开的那条不许自己收回去（这是最容易翻车的地方）
        time.sleep(7)
        still = c.js("document.querySelectorAll('#log .b.fold.open').length") or 0
        check(still > 0, "自动刷新跑过一轮，展开状态还在", "(%d 条还开着)" % still)
        c.js("(function(){var m=document.querySelector('#log .b.fold.open');"
             "if(m&&m.nextSibling)m.nextSibling.click()})()")
        time.sleep(0.2)
        h3 = c.js("(function(){var b=document.querySelector('#log .b.fold');"
                  "return b?b.getBoundingClientRect().height:0})()") or 0
        check(abs(h3 - h) < 8, "再点一下收回去了", "(%.0f px)" % h3)

        print("\n== 桌面宽度 ==")
        c.call("Emulation.setDeviceMetricsOverride",
               {"width": 1280, "height": 900, "deviceScaleFactor": 1, "mobile": False})
        c.call("Page.reload")
        wait_for(c, "document.querySelectorAll('#log .msg').length>0", 25)
        time.sleep(1.0)
        nf2 = c.js("document.querySelectorAll('#log .b.fold').length") or 0
        check(nf2 == 0, "宽屏不折（一条占不满屏，折了反而少看）", "(折了 %d 条)" % nf2)

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
