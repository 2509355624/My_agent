# -*- coding: utf-8 -*-
"""Web 端上传的图，服务端到底看不看得见？——一次端到端实测。

走的是**真实的接口**，不 mock 识图：上传 → 拿 ref_id → 按工作台那条
提交路径把 refs 交上去 → 看 `_describe_refs` 读出来的描述，以及最终
拼进去的提示词长什么样。

用法（在仓库根目录）：
    D:/AI/confyui_env/Scripts/python.exe tests/check_ref_visible.py
    D:/AI/confyui_env/Scripts/python.exe tests/check_ref_visible.py <图片路径>
"""
import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

QQ = "2509355624"


def _pick_image(argv):
    """默认拿参考图库里最年轻的那张（它就是用户真上传过的）。"""
    if len(argv) > 1 and os.path.isfile(argv[1]):
        return argv[1]
    import sqlite3
    db = os.path.join("state", "gallery_refs.db")
    if os.path.isfile(db):
        con = sqlite3.connect(db)
        con.row_factory = sqlite3.Row
        row = con.execute("SELECT path FROM refs ORDER BY created DESC"
                          " LIMIT 1").fetchone()
        if row and os.path.isfile(row["path"]):
            return row["path"]
    return ""


def main(argv):
    img = _pick_image(argv)
    if not img:
        print("没有可用的图：要么传一个路径，要么 refs 库里有货")
        return 2
    raw = open(img, "rb").read()
    print("测试用图：%s（%.0f KB）" % (img, len(raw) / 1024.0))

    import app.main as M

    # 拿着 5175 网关转发时用的那把密钥，按「群友从工作台进来」的身份请求：
    # 这才是真实路径（本机直连会被认成站主，看到的是另一套口径）。
    from app.gateways import friend_auth
    HDR = {"X-Friend-Token": friend_auth.secret()}

    with M.app.test_client() as c:
        # ① 上传（跟 ref.js 的 upload() 一个口：/api/gal/ref，use=temp）
        r = c.post("/api/gal/ref?__jf=1&qq=%s" % QQ,
                   headers=HDR,
                   data={"file": (io.BytesIO(raw), os.path.basename(img)),
                         "use": "temp"},
                   content_type="multipart/form-data")
        print("① 上传：", r.status_code, r.get_data(as_text=True)[:200])
        up = r.get_json() or {}
        if not up.get("ok"):
            return 1
        ref_id = up["ref_id"]

        # ② 识图（前端点发送时会先调这个；失败也不拦提交）
        r = c.get("/api/gal/ref/describe?__jf=1&qq=%s&ref_id=%s" % (QQ, ref_id),
                  headers=HDR)
        print("② 识图：", r.status_code)
        de = r.get_json() or {}
        print("   描述 %d 字：%s" % (len(de.get("text") or ""),
                                     (de.get("text") or "")[:120]))

        # ③ 服务端读图（提交时真正走的那一步）
        with M.app.test_request_context("/api/gen/submit?__jf=1&qq=%s" % QQ,
                                        method="POST", json={"refs": [ref_id]}):
            from app.features.image import routes as R
            text = R._describe_refs([ref_id])
        print("③ _describe_refs：%d 字" % len(text))
        print("   %s" % text[:200])
        shown = ("%s\n\n画面参考（来自用户上传的图，请照着画）：%s"
                 % ("反推", text)) if text else "（没读到，按纯文字出图）"
        print("④ 最终进模型的提示词共 %d 字，尾部：\n   ...%s"
              % (len(shown), shown[-160:]))
    ok = bool(text)
    print("\n结论：服务端%s读到 Web 端上传的这张图。" % ("**能**" if ok else "**读不到**"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
