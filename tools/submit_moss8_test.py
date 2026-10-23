"""把一张 moss8 测试图交给**常驻网页端**（5174）提交。

别自己起进程 enqueue：独立进程一退，job 就只剩内存里的一条，ComfyUI 一重启
队列清空，`job.wait()` 会一直等一个永远不来的结果（2026-10-23 实测卡了 10 分钟）。
网页端那条链路的 worker 跑在常驻 flask 里，排队、守卫、`_submit_retry` 都在，
这才是「QQ 那条队列」。

先 POST 提交，再轮询 /api/gen/status 拿结果 —— 提交完脚本就可以走，任务照跑。
"""
import json
import os
import sys
import time
import urllib.request

os.environ["NO_PROXY"] = "*"
for _k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
    os.environ.pop(_k, None)

BASE = "http://127.0.0.1:5174"
UI_WF = r"D:/AI/ComfyUI_v037/user/default/workflows/moss8步长.json"


def post(path, payload):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=20) as r:
        return json.loads(r.read())


def main():
    n = {x["id"]: x for x in json.load(open(UI_WF, encoding="utf-8"))["nodes"]}
    prompt = n[41]["widgets_values"][0].strip()

    d = post("/api/gen/submit", {"skill": "moss8", "prompt": prompt})
    if not d.get("ok"):
        raise SystemExit("提交被拒：%s" % d.get("error"))
    job = d["job"]
    print("已提交 %s | skill=%s | seed=%s | prompt %d 字"
          % (job["id"], job["skill"], job["seed"], len(prompt)), flush=True)

    deadline = time.time() + 900
    last = ""
    while time.time() < deadline:
        try:
            st = get("/api/gen/status")
        except Exception as e:
            print("  状态查询失败（进程可能重启中）：%s" % type(e).__name__, flush=True)
            time.sleep(8)
            continue
        me = next((j for j in st.get("jobs", []) if j["id"] == job["id"]), None)
        if me is None:
            print("  任务记录消失了", flush=True)
            return
        state = me.get("state")
        if state != last:
            print("  [%s] %s %s" % (time.strftime("%H:%M:%S"), state,
                                    ("队列深 %s" % st.get("queue_depth"))
                                    if state == "queued" else ""), flush=True)
            last = state
        if state == "done":
            print("完成 → %s" % me.get("file"), flush=True)
            return
        if state == "error":
            raise SystemExit("失败：%s" % me.get("error"))
        time.sleep(5)
    raise SystemExit("等了 15 分钟还没出图")


if __name__ == "__main__":
    main()
