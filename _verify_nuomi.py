# -*- coding: utf-8 -*-
"""跑通验证：把 modular v4 的底模换成本机 anima，直连 ComfyUI 出一张，看 LoRA 兼不兼容。

只读资产包、只 POST /prompt，不改仓库任何文件。

⚠️ 两个坑（都踩过）：
1. 本机 16GB，`image_jobs._maybe_restart_for_ram` 会在**每张图跑完后**检查水位，
   低于 `COMFY_MIN_FREE_RAM_GB`(3.0) 就重启 ComfyUI —— 重启会**清空 ComfyUI 的
   队列和 history**，直连提交的会被静默吞掉。所以：判「pid 既不在 /queue 也不在
   /history」= 被吞 → 重投。
2. 重启期间 8188 会回 **502 Bad Gateway**，任何一次请求都可能抛异常 —— 所有 HTTP
   都要 try/except，不能让它把循环打断。
3. **别在机器人忙的时候跑**：会被反复吞、还抢线上排队。先看 /queue 空不空。
"""
import json
import os
import sys
import time
import urllib.request

API = "http://127.0.0.1:8188"
SRC = r"D:\AI\temp\anima-private-assets\workflows\api\nuomitianshi-anima-modular-v4-api.json"
LOCAL_UNET = "miaomiaoRealskin_anima13.safetensors"   # 顶替 waiANIMA_v10Base10
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_verify_nuomi.json")
MAX_ATTEMPTS = 6
DEADLINE = 900                                        # 最多守 15 分钟


def _req(path, payload=None, timeout=20):
    """返回解析后的 JSON；ComfyUI 在重启 / 不在线时返回 None（不抛）。"""
    url = API + path
    try:
        if payload is None:
            req = urllib.request.Request(url)
        else:
            req = urllib.request.Request(
                url, data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except Exception as exc:                           # noqa: BLE001
        print("      (%s → %s)" % (path, type(exc).__name__))
        return None


def wait_online(limit=240):
    start = time.time()
    while time.time() - start < limit:
        if _req("/system_stats") is not None:
            return True
        time.sleep(4)
    return False


wf = json.load(open(SRC, encoding="utf-8"))
for node in wf.values():
    if node.get("class_type") == "UNETLoader":
        print("底模 %s → %s" % (node["inputs"]["unet_name"], LOCAL_UNET))
        node["inputs"]["unet_name"] = LOCAL_UNET
json.dump(wf, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

q = _req("/queue") or {}
if q.get("queue_running") or q.get("queue_pending"):
    print("⚠️ ComfyUI 队列非空（running=%d pending=%d）——机器人正在用，先别跑。"
          % (len(q.get("queue_running", [])), len(q.get("queue_pending", []))))
    print("   等线上空了再跑，或加 --force 硬上。")
    if "--force" not in sys.argv:
        sys.exit(3)

t0 = time.time()
pid = None
wipes = 0
submits = 0
while time.time() - t0 < DEADLINE:
    if pid is None:
        if not wait_online():
            print("ComfyUI 一直没回来")
            sys.exit(2)
        submits += 1
        res = _req("/prompt", {"prompt": wf, "client_id": "verify-nuomi"})
        if not res or "prompt_id" not in res:
            print("[%6.1fs] 提交被拒：%s" % (time.time() - t0, json.dumps(res)[:400]))
            time.sleep(5)
            continue
        pid = res["prompt_id"]
        print("[%6.1fs] 第 %d 次提交 pid=%s" % (time.time() - t0, submits, pid))

    hist = _req("/history/" + pid) or {}
    if pid in hist:
        entry = hist[pid]
        status = entry.get("status", {})
        print("[%6.1fs] status=%s" % (time.time() - t0, status.get("status_str")))
        if status.get("status_str") != "success":
            print(json.dumps(status, ensure_ascii=False)[:3000])
            sys.exit(1)
        for out in entry.get("outputs", {}).values():
            for img in out.get("images", []):
                print("  输出 %s" % img)
        print("总耗时 %.1f 秒（提交 %d 次，被吞 %d 次）"
              % (time.time() - t0, submits, wipes))
        sys.exit(0)

    queue = _req("/queue")
    if queue is not None:
        ids = [x[1] for x in queue.get("queue_running", []) + queue.get("queue_pending", [])]
        if pid not in ids:
            wipes += 1
            print("[%6.1fs] pid 不在队列也不在 history —— 被重启吞了（第 %d 次），重投"
                  % (time.time() - t0, wipes))
            if wipes >= MAX_ATTEMPTS:
                print("被吞 %d 次，放弃" % wipes)
                sys.exit(2)
            pid = None
            time.sleep(3)
            continue
    time.sleep(5)

print("超时（%.0f 秒）" % (time.time() - t0))
sys.exit(2)
