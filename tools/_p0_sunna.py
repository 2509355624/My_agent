# -*- coding: utf-8 -*-
"""P0：验证 anima-line 挂载「绝区零·千夏」LoRA 能否出图（2026-11-06）。

只投一张，走 5174 的 `/api/gen/submit`——它入的是同一个 `image_jobs` FIFO，
并且和 QQ 共用 `comfy_gate` 的排队票（先来先跑，不会饿死群友，也不会掐别人的图）。

用法：py -3 tools/_p0_sunna.py
"""
import io
import json
import sys
import time
import urllib.request

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                              errors="replace")

BASE = "http://127.0.0.1:5174"
SKILL = "anima-line"
# 带名字（别名会命中索引）+ 一点衣服和取景，避免裸出被判定违规；主要看脸。
PROMPT = ("1girl, solo, upper body, looking at viewer, gentle smile, "
          "千夏, white collared shirt, red necktie, black frilled layered miniskirt")


def post(path, body):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    return json.load(urllib.request.urlopen(req, timeout=30))


def get(path):
    return json.load(urllib.request.urlopen(BASE + path, timeout=30))


def main():
    print("渠道:", SKILL)
    print("提示词:", PROMPT)
    r = post("/api/gen/submit", {"skill": SKILL, "prompt": PROMPT})
    print("提交回包:", json.dumps(r, ensure_ascii=False)[:300])
    if not r.get("ok") and not r.get("job"):
        return
    jid = str((r.get("job") or {}).get("id") or r.get("id") or r.get("job") or "")
    print("任务 id:", jid)
    t0 = time.time()
    while time.time() - t0 < 240:
        time.sleep(4)
        st = get("/api/gen/status")
        job = next((j for j in (st.get("jobs") or [])
                    if str(j.get("id")) == str(jid)), None)
        if job is None:
            print("  …队列里还没出现（%.0fs）" % (time.time() - t0))
            continue
        state = str(job.get("state") or "")
        print("  %-8s 第 %.0f 秒  队列深度=%s"
              % (state, time.time() - t0, st.get("queue_depth")))
        if state in ("done", "error", "failed"):
            print(json.dumps(job, ensure_ascii=False)[:800])
            break
    else:
        print("超时（4 分钟），去 /api/gen/status 看一眼")


if __name__ == "__main__":
    main()
