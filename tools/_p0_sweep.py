# -*- coding: utf-8 -*-
"""P0 连续验证：逐个 anima 渠道挂「绝区零·千夏」，一次一张，等结果再下一张。

规矩（用户定的）：
- 走 5174 的 `image_jobs` 队列（与 QQ 共用 comfy_gate 排队票），**串行**，
  一张没跑完不投下一张——不能把群友的队列占满。
- 只有 `CHAR_LORA_SKILLS` 里的渠道才会挂角色 LoRA，所以跑之前要重启 agent。

用法：py -3 tools/_p0_sweep.py [渠道1 渠道2 ...]
"""
import io
import json
import sys
import time
import urllib.request

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                              errors="replace")

BASE = "http://127.0.0.1:5174"
PROMPT = ("1girl, solo, upper body, looking at viewer, gentle smile, "
          "千夏, white collared shirt, red necktie, black frilled layered miniskirt")
DEFAULT = ["anima-line", "anima-real", "hd_3_clear", "hd_3_curvy",
           "hd_3_gloss", "hd_3_soft", "silver", "silver-hd"]

OUT_ROOT = r"D:\AI\ComfyUI-nightly\output"


def post(path, body):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    return json.load(urllib.request.urlopen(req, timeout=30))


def get(path):
    return json.load(urllib.request.urlopen(BASE + path, timeout=30))


def run_one(skill, wait_max=300):
    r = post("/api/gen/submit", {"skill": skill, "prompt": PROMPT})
    job = r.get("job") or {}
    jid = job.get("id")
    if not jid:
        return {"skill": skill, "state": "submit-failed",
                "error": json.dumps(r, ensure_ascii=False)[:160]}
    t0 = time.time()
    while time.time() - t0 < wait_max:
        time.sleep(4)
        st = get("/api/gen/status")
        cur = next((j for j in (st.get("jobs") or [])
                    if str(j.get("id")) == str(jid)), None)
        if cur and str(cur.get("state")) in ("done", "error", "failed"):
            return {"skill": skill, "id": jid, "state": cur.get("state"),
                    "secs": cur.get("secs"), "file": cur.get("file"),
                    "error": (cur.get("error") or "")[:160],
                    "depth": st.get("queue_depth")}
    return {"skill": skill, "id": jid, "state": "timeout"}


def main():
    skills = sys.argv[1:] or DEFAULT
    rows = []
    for s in skills:
        print("\n=== %s ===" % s, flush=True)
        row = run_one(s)
        print(json.dumps(row, ensure_ascii=False), flush=True)
        rows.append(row)
        print("---- 已跑 %d/%d ----" % (len(rows), len(skills)), flush=True)
    print("\n===== 汇总 =====")
    for r in rows:
        print("%-12s %-8s %-6s %s" % (r.get("skill"), r.get("state"),
                                      r.get("secs") or "", r.get("file") or
                                      r.get("error") or ""))
    with open("state/p0_sweep.json", "w", encoding="utf-8", newline="\n") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
