# -*- coding: utf-8 -*-
"""访客模式的作用域体检（2026-11-26 用户：「访客模式只局限于工作台」）。

不连真实后端，只打 5175 的路由层：看带着访客 cookie 时，
  · 工作台发出来的请求 → 认定访客（带 X-Friend-Guest），额度照扣；
  · 别的页面发出来的请求 → 完全按「没登录」走，跟没带 cookie 时一模一样。
真后端打不通（5174 没起）时状态码会是 502，这里只比对两组的**差异**，
所以那也能跑。
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import friend_app as fa            # noqa: E402
from app.gateways import visitor    # noqa: E402

HOST = "localhost"          # test_request_context 下的 request.host
WS = "http://localhost/workspace/chat.html"

# 别的页面的接口：一个都不该被访客模式影响
OTHERS = [
    ("画廊列表", "/api/gal/list?size=1", "GET", "http://localhost/gallery"),
    ("我的漫画", "/api/comic/tasks", "GET", "http://localhost/comic"),
    ("漫画剧本", "/api/comic/prompt?task=x", "GET", "http://localhost/comic"),
    ("生图渠道", "/api/gen/channels", "GET", "http://localhost/runbench"),
    ("连续多图", "/api/gen/batch", "POST", "http://localhost/runbench"),
    ("跑图台检查", "/api/runbench/check?k=x", "GET", "http://localhost/runbench"),
    ("角色扮演", "/api/persona/sessions", "GET", "http://localhost/persona"),
    ("小说", "/api/novel/list", "GET", "http://localhost/novel"),
    ("群友额度", "/api/friend/quota", "GET", "http://localhost/runbench"),
    ("聊天存档", "/api/chat/archive", "GET", "http://localhost/gallery"),
]

ok_all = True


def check(cond, label, note=""):
    global ok_all
    ok_all = ok_all and bool(cond)
    print("  %s %s %s" % ("[OK]" if cond else "[XX]", label, note))


def one(client, url, method, ref, body=None):
    h = {"Referer": ref} if ref else {}
    r = client.open(url, method=method, headers=h,
                    json=body if body else None)
    return r.status_code, r.get_data(as_text=True)


def main():
    app = fa.app
    app.config["TESTING"] = True
    gid = visitor.new_id()

    c_guest = app.test_client()
    c_guest.set_cookie(visitor.COOKIE_NAME, gid, domain="localhost")
    c_plain = app.test_client()

    print("== 1. 别的页面：带访客 cookie 和什么都不带，必须完全一样")
    same = True
    for name, url, method, ref in OTHERS:
        a = one(c_guest, url, method, ref, {"skill": "anima-2s",
                                           "prompt": "a cat", "count": 1})
        b = one(c_plain, url, method, ref, {"skill": "anima-2s",
                                           "prompt": "a cat", "count": 1})
        same = same and (a == b)
        print("     %-10s 带访客=%s / 不带=%s" % (name, a[0], b[0]))
        if "访客模式用不了" in a[1]:
            check(False, name, "还在被访客白名单挡")
    check(same, "别的页面结果一字不差（访客 cookie 等于不存在）")

    print("== 2. 「我是谁」：工作台里是 visitor，别的地方是 guest")
    code, body = one(c_guest, "/me", "GET", WS)
    d = json.loads(body) if body.startswith("{") else {}
    check(d.get("role") == "visitor", "/me（工作台发）→ visitor", "(role=%r)" % d.get("role"))
    code, body = one(c_guest, "/me", "GET", "http://localhost/gallery")
    d = json.loads(body) if body.startswith("{") else {}
    check(d.get("role") == "guest", "/me（画廊发）→ guest", "(role=%r)" % d.get("role"))
    code, body = one(c_guest, "/me", "GET", "")
    d = json.loads(body) if body.startswith("{") else {}
    check(d.get("role") == "guest", "/me（无 Referer）→ guest", "(role=%r)" % d.get("role"))

    print("== 3. 工作台里访客照样能用（身份被送过去）")
    code, body = one(c_guest, "/api/gal/list?size=1", "GET", WS)
    check(code != 401, "工作台发画廊列表不是 401", "(状态 %s)" % code)
    code, body = one(c_guest, "/api/gen/batch", "POST", WS,
                     {"skill": "anima-2s", "prompt": "a cat", "count": 1})
    check("访客模式用不了" not in body, "工作台的连续多图不被白名单挡",
          "(状态 %s)" % code)

    print("== 4. 判定函数本身")
    for ref, want, label in (
            (WS, True, "工作台 chat.html"),
            ("http://localhost/workspace/", True, "工作台首页"),
            ("http://localhost/workspace/js/chat.js", True, "工作台脚本"),
            ("http://localhost/gallery", False, "画廊"),
            ("http://localhost/comic", False, "漫画台"),
            ("http://localhost/runbench", False, "跑图台"),
            ("", False, "没有 Referer"),
            ("https://evil.example/workspace/x", False, "外站冒充（路径里带 workspace）")):
        with app.test_request_context("/x", headers={"Referer": ref} if ref else {},
                                     base_url="http://localhost"):
            got = fa._jf_here()
        check(got == want, "工作台判定：%s" % label, "(得到 %s)" % got)

    print()
    print("汇总：%s" % ("全部通过" if ok_all else "有失败项"))
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
