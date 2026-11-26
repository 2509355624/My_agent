# -*- coding: utf-8 -*-
"""工作台访客那条链路的回归探针（2026-11-26）。

用户报的三件事：
  1. 「没拿到渠道列表，先去跑图台看看，回来再试」
  2. 「点 AI 创作显示还未登录，点重试依旧如此」
  3. 「点登录有反应，但一直加载，又弹回原来的我的页」

共同根因：访客身份只靠 `Referer` 判，而**页面自己**的请求（reload、
favicon、手机浏览器省 Referer）统统没有 Referer → 被判成「不是工作台」
→ 身份变 guest → 渠道接口 401 / 前端发登录页。

修法（用户 2026-11-26 拍板的口径）：
  · Referer 指向 `/workspace/...` → 认访客
  · 完全没有 Referer              → 认访客（页面自己的请求）
  · Referer 指向别处              → 不算访客，**和现在一行不差**

用 Flask 测试客户端跑，不碰真进程。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

FAIL = []


def check(name, cond, extra=""):
    print(("  OK   " if cond else "  FAIL ") + name + (("  " + extra) if extra else ""))
    if not cond:
        FAIL.append(name)


def main():
    import friend_app

    friend_app.app.config["TESTING"] = True

    print("[1] 访客登录")
    c = friend_app.app.test_client()
    r = c.post("/login/visitor")
    raw = "\n".join(r.headers.getlist("Set-Cookie"))
    check("登录返回 200", r.status_code == 200)
    check("拿到 fr_visit", friend_app.visitor.COOKIE_NAME in raw)

    print("[2] /me 的三种 Referer")
    cases = [
        ("http://localhost/workspace/chat.html", "visitor", "工作台页面"),
        ("http://localhost/workspace/", "visitor", "工作台根"),
        ("http://localhost/workspace/index.html", "visitor", "登录页"),
        ("", "visitor", "★ 页面自己发的（用户报的就是这条）"),
        ("http://localhost/gallery", "guest", "画廊"),
        ("http://localhost/runbench", "guest", "跑图台"),
        ("https://evil.example/workspace/x", "guest", "外站冒充"),
    ]
    for ref, want, why in cases:
        h = {"Referer": ref} if ref else {}
        got = (c.get("/me", headers=h).get_json() or {}).get("role")
        check("%-22s → %-8s" % (why, want), got == want, "实际 %s" % got)

    print("[3] 渠道清单：工作台能拿到，别处照旧被挡")
    for ref, want, why in [(None, 200, "工作台无 Referer"),
                           ("http://localhost/workspace/chat.html", 200, "工作台"),
                           ("http://localhost/gallery", 401, "画廊")]:
        h = {"Referer": ref} if ref else {}
        rr = c.get("/api/gen/channels", headers=h)
        check("%-18s → %s" % (why, want), rr.status_code == want,
              "实际 %s" % rr.status_code)

    print("[4] 别处的行为必须和「没有访客证」一字不差")
    c2 = friend_app.app.test_client()          # 干净的浏览器
    for path in ("/api/gal/mine", "/api/gen/channels", "/api/chat/archive",
                 "/api/comic/list"):
        a = c.get(path, headers={"Referer": "http://localhost/gallery"})
        b = c2.get(path, headers={"Referer": "http://localhost/gallery"})
        check("%-20s 访客 vs 无证" % path,
              (a.status_code, a.get_data()) == (b.status_code, b.get_data()),
              "%s vs %s" % (a.status_code, b.status_code))

    print("\n失败 %d 项" % len(FAIL))
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
