# -*- coding: utf-8 -*-
"""anima（hd_3_*）第一段步数 A/B —— 同一 seed、同一提示词，只改 node2 的 steps。

背景（2026-10-11 用户反馈）：anima 有时出现「多脚 / 肢体错位」和「瞳孔渲染成纯色块」。
用户怀疑是第一段 10 步太少，要求把第一段翻到 20 步做对照。

做法（关键：不改任何生产文件、不复刻任何逻辑）：
  1. 从**已有的成品 PNG** 里读回 ComfyUI 写进元数据的 `prompt` 块
     —— 那就是当时提交给 ComfyUI 的**原始 payload**，逐字节一致。
  2. 只把 `payload["2"]["inputs"]["steps"]` 从 10 改成 20，其余一个字段都不动。
  3. 直接 POST /prompt。
  ⇒ 10 步那一半的基线**已经在磁盘上**（原图），不用重跑。

用法：
  python _anima_steps_ab.py queue                 # 只看队列
  python _anima_steps_ab.py run 00479:20 00475:20 ...
  python _anima_steps_ab.py run 00479:10          # 10 步可用来验「同 seed 能复现」
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid

BASE = "http://127.0.0.1:8188"
OUT = r"D:\AI\ComfyUI_v037\output"
STAGE1_NODE = "2"          # 第一段 KSampler
STAGE2_NODE = "19"         # 第二段 KSampler（本脚本不碰）


def _get(path, timeout=30):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _post(path, payload, timeout=120):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        raise SystemExit("!! POST %s -> %s\n%s" % (path, e.code, body[:2000]))


def payload_of(tag):
    """tag 形如 '00479' → 读 Anima_00479_.png 的元数据，返回原始 payload。"""
    from PIL import Image
    p = os.path.join(OUT, "Anima_%s_.png" % tag)
    if not os.path.exists(p):
        raise SystemExit("!! 找不到 %s" % p)
    raw = Image.open(p).info.get("prompt")
    if not raw:
        raise SystemExit("!! %s 没有 prompt 元数据" % p)
    return json.loads(raw), p


def show_queue():
    d = _get("/queue")
    print("running=%d pending=%d" % (len(d["queue_running"]), len(d["queue_pending"])))
    for it in d["queue_pending"][:10]:
        print("   pending", it[0])


def run_one(tag, steps):
    payload, src = payload_of(tag)
    old = payload[STAGE1_NODE]["inputs"]["steps"]
    seed = payload[STAGE1_NODE]["inputs"]["seed"]
    payload[STAGE1_NODE]["inputs"]["steps"] = steps
    if payload[STAGE2_NODE]["inputs"]["steps"] != 10:
        raise SystemExit("!! 第二段步数不是 10，脚本按「只改第一段」写的，先看代码")

    print("=" * 70)
    print("%s  seed=%s  第一段 %s -> %s 步（第二段固定 10 步）" % (src, seed, old, steps))
    n4 = [v["inputs"]["text"] for v in payload.values()
          if v.get("class_type") == "CLIPTextEncode"
          and v["inputs"].get("text", "").startswith("@kibro")]
    print("   提示词:", (n4[0] if n4 else "?")[:160].replace("\n", " "))

    cid = str(uuid.uuid4())
    t0 = time.time()
    res = _post("/prompt", {"prompt": payload, "client_id": cid})
    pid = res["prompt_id"]
    print("   已提交 prompt_id=%s，等待…" % pid, flush=True)

    while True:
        time.sleep(3)
        try:
            h = _get("/history/%s" % pid)
        except Exception:
            continue
        if pid in h:
            entry = h[pid]
            st = entry.get("status", {})
            if st.get("completed") or st.get("status_str") in ("success", "error"):
                break
        if time.time() - t0 > 600:
            raise SystemExit("!! 等太久，放弃")

    outs = []
    for node_out in h[pid].get("outputs", {}).values():
        for im in node_out.get("images", []):
            outs.append(os.path.join(OUT, im.get("subfolder", ""), im["filename"]))
    el = time.time() - t0
    print("   完成，耗时 %.1f s" % el)
    for o in outs:
        print("   产物:", o)
    return outs


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "queue"
    if cmd == "queue":
        show_queue()
    elif cmd == "run":
        for spec in sys.argv[2:]:
            tag, steps = spec.split(":")
            run_one(tag, int(steps))
    else:
        raise SystemExit(__doc__)
