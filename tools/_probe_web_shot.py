"""验证网页出图的两个关键修复（2026-11-03）。

修的两个 bug：
  1. `moss_bot` 拿假 target_id 往 QQ 发消息 → 每次出图刷「moss 回话失败」
  2. `moss_bot.start()` 是先起线程再返回，系列/页**还没落盘**，`shot` 立刻
     查 `current()` 拿到空串 → 前端报「没拿到页号」

    python tools/_probe_web_shot.py            # 只验接线（mock 出图）
    python tools/_probe_web_shot.py --draw     # 真跑一次（100 秒+）
"""
import argparse
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import flask                                       # noqa: E402

from app import moss_series, persona, persona_api  # noqa: E402

QQ = "2509355624"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--draw", action="store_true", help="真出图")
    a = ap.parse_args()

    app = flask.Flask(__name__)
    app.register_blueprint(persona_api.bp)
    c = app.test_client()

    # 进角色
    d = c.post("/api/persona/switch",
               json={"name": "和泉纱雾", "qq": QQ}).get_json()
    print("切角色:", d.get("reply"))

    # 说一句（真要历史，出图要靠它定位场景）
    r = c.post("/api/persona/chat",
               json={"text": "我回来了。", "qq": QQ}).get_json()
    print("对话:", (r.get("reply") or "")[:40], "...")

    print("\n=== 出图 ===")
    t0 = time.time()
    d = c.post("/api/persona/shot",
               json={"qq": QQ, "msgIndex": 0}).get_json()
    print("shot 返回:", d)
    if not d.get("ok"):
        print("!! 出图请求失败")
        return 1
    sid, page = d.get("sid"), d.get("page")
    if not sid or not page:
        print("!! 还是没拿到 sid/page")
        return 1
    print("✓ 拿到系列 %s 第 %s 页（耗时 %.1fs）" % (sid, page, time.time() - t0))

    print("\n=== 轮询状态 ===")
    for i in range(120):
        st = c.get("/api/persona/comic?sid=%s&page=%s&qq=%s"
                   % (sid, page, QQ)).get_json()
        print("  [%02d] status=%s %s" % (i, st.get("status"),
                                         st.get("url") or st.get("error") or ""))
        if st.get("status") in ("done", "failed"):
            if st.get("status") == "done":
                print("\n✓ 图出来了:", st.get("url"))
            return 0
        time.sleep(3)
    print("!! 等太久了")
    return 1


if __name__ == "__main__":
    sys.exit(main())
