"""端到端测网页扮演接口（不起服务、不调真模型，只测接线）。

    python tools/_probe_persona_api.py
"""
import json
import os
import sys
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import flask                                       # noqa: E402

from app import persona, persona_api               # noqa: E402

QQ = "2509355624"


def build():
    app = flask.Flask(__name__)
    app.register_blueprint(persona_api.bp)
    return app.test_client()


def j(r):
    try:
        return r.get_json()
    except Exception:                              # noqa: BLE001
        return {"_raw": r.get_data(as_text=True)[:200]}


def main():
    c = build()
    ok = 0
    fail = 0

    def chk(name, cond, extra=""):
        nonlocal ok, fail
        if cond:
            ok += 1
            print("  ✓ %s" % name)
        else:
            fail += 1
            print("  ✗ %s %s" % (name, extra))

    print("== 1. 角色列表 ==")
    d = j(c.get("/api/persona/cards"))
    chk("返回 ok", d.get("ok") is True)
    chk("有角色", len(d.get("cards") or []) > 0,
        "拿到 %d 个" % len(d.get("cards") or []))
    names = [x["name"] for x in (d.get("cards") or [])]
    print("    名单:", "、".join(names[:6]))

    print("== 2. 没选角色时聊天被拒 ==")
    d = j(c.post("/api/persona/chat", json={"text": "在吗", "qq": QQ}))
    chk("应当失败", d.get("ok") is False, d)

    print("== 3. 切角色 ==")
    target = names[0]
    d = j(c.post("/api/persona/switch", json={"name": target, "qq": QQ}))
    chk("切成功", d.get("ok") is True, d)
    chk("回执含名字", target in str(d.get("reply")), d.get("reply"))

    print("== 4. 会话状态 ==")
    d = j(c.get("/api/persona/session?qq=" + QQ))
    chk("ok", d.get("ok") is True)
    chk("当前角色对", (d.get("card") or {}).get("name") == target, d.get("card"))

    print("== 5. 聊天（mock 模型）==")
    fake = persona.Turn("她抬眼看了你一下。", "1girl, sitting",
                        "害羞", 6, "坐在桌前 · 白T恤")
    with mock.patch.object(persona, "run_turn", return_value=fake):
        d = j(c.post("/api/persona/chat",
                     json={"text": "在吗", "qq": QQ}))
    chk("有回复", d.get("reply") == "她抬眼看了你一下。", d)
    chk("带情绪", d.get("mood") == "害羞" and d.get("intensity") == 6, d)

    print("== 6. 空文本被拒 ==")
    d = j(c.post("/api/persona/chat", json={"text": "  ", "qq": QQ}))
    chk("应当失败", d.get("ok") is False, d)

    print("== 7. 出图（mock moss）==")
    with mock.patch("app.moss_bot.start", return_value=(True, "")) as ms, \
         mock.patch("app.moss_series.current", return_value="TEST"), \
         mock.patch("app.moss_series.load",
                    return_value={"pages": [{"n": 3}]}), \
         mock.patch("app.moss_series.latest", return_value={"n": 3}):
        d = j(c.post("/api/persona/shot", json={"qq": QQ}))
    chk("出图 ok", d.get("ok") is True, d)
    chk("带系列号", d.get("sid") == "TEST", d)
    chk("带页号", d.get("page") == 3, d)
    if ms.called:
        brief = ms.call_args[0][3] if len(ms.call_args[0]) > 3 else ""
        chk("brief 含女主判据", "女主角" in brief, (brief or "")[:80])
        chk("brief 含男主判据", "男主角" in brief, (brief or "")[:80])

    print("== 8. 清空 / 出戏 ==")
    d = j(c.post("/api/persona/clear", json={"qq": QQ}))
    chk("清空有回执", bool(d.get("reply")), d)
    d = j(c.post("/api/persona/leave", json={"qq": QQ}))
    chk("出戏有回执", bool(d.get("reply")), d)

    print("== 9. 会话键隔离 ==")
    chk("web 与 private 不同",
        persona_api._key(QQ) != ("private_" + QQ),
        persona_api._key(QQ))

    print()
    print("通过 %d，失败 %d" % (ok, fail))
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
