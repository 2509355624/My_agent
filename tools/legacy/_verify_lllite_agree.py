# -*- coding: utf-8 -*-
"""提示词 + 参考图**一致**时，能不能复刻参考图的构图？

前面测的全是「控制图 vs 提示词**冲突**」（提示词赢）。这轮换成作者自己的用法：
  参考图 = `reference02.png`（作者自己画的那张成品：绿发女孩站着叉腰、道观烛光）
  提示词 = 包里 `hands_on_hips` 那套预设（**就是作者给这张图配的提示词**）
  → 这是「反推提示词」能做到的最好情况：图和词本来就是一对。

三组对照，只差 LLLite 这一层：
  G: 纯文生图（不加 LLLite）
  H: + Canny(reference02) → any-test-like-v2 @0.92 / end 0.92
  I: + Canny(reference02) → any-test-like-v2 @1.00 / end 1.00   （作者用过的最高档）

跑完算三张各自与 reference02 的**边缘图相关性**（构图接近程度）。
如果 H/I 明显比 G 更接近参考图 → 说明「词+图」确实能复刻；
如果三者差不多 → 说明参考图这一路基本没贡献，复刻靠的是提示词本身。

⚠️ 前面几轮用的控制图是**线稿/骨架**（稀疏），这轮用的是**渲染图**（密集边缘），
对 `any-test-like` 这个补丁来说分布更接近它训练时的样子，所以这轮才是它的公平考场。
"""
import glob
import json
import os
import sys
import time
import urllib.request

HOST = "http://127.0.0.1:8188"
BUNDLE = r"D:\AI\temp\anima-private-assets\workflows\api"
SKELETON = os.path.join(BUNDLE, "nuomitianshi-anima-modular-v4-api.json")
REFERENCE = r"D:\AI\temp\anima-private-assets\workflow-v4\controls\reference02.png"
LOCAL_UNET = "miaomiaoRealskin_anima13.safetensors"
CTRL_NAME = "nuomi_ref02.png"
OUTDIR = r"D:\AI\ComfyUI_v037\output"
WIDTH, HEIGHT = 832, 1216
DEADLINE = 900.0
POLL = 3.0

# label, 用不用 LLLite, strength, end
CASES = [
    ("G_pure_t2i", False, 0.0, 0.0),
    ("H_lllite_0.92", True, 0.92, 0.92),
    ("I_lllite_1.00", True, 1.00, 1.00),
]


def _get(path, timeout=10):
    try:
        with urllib.request.urlopen(HOST + path, timeout=timeout) as r:
            return json.load(r)
    except Exception:
        return None


def _post(path, payload, timeout=30):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(HOST + path, data=data,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        return {"_err": "HTTP %s: %s" % (e.code, e.read().decode("utf-8", "replace")[:500])}
    except Exception as e:
        return {"_err": str(e)}


def build(label, use_lllite, strength, end_pct):
    with open(SKELETON, encoding="utf-8") as f:
        wf = json.load(f)
    wf["1"]["inputs"]["unet_name"] = LOCAL_UNET
    wf["10"]["inputs"]["width"] = WIDTH
    wf["10"]["inputs"]["height"] = HEIGHT
    if use_lllite:
        wf["29"] = {"class_type": "ModelPatchLoader",
                    "inputs": {"name": "anima-lllite-any-test-like-v2.safetensors"}}
        wf["30"] = {"class_type": "LoadImage", "inputs": {"image": CTRL_NAME}}
        wf["32"] = {"class_type": "Canny",
                    "inputs": {"image": ["30", 0], "low_threshold": 0.12,
                               "high_threshold": 0.3}}
        wf["33"] = {"class_type": "ImageInvert", "inputs": {"image": ["32", 0]}}
        wf["31"] = {"class_type": "AnimaLLLiteApply",
                    "inputs": {"model": ["16", 0], "model_patch": ["29", 0],
                               "image": ["33", 0], "strength": strength,
                               "start_percent": 0.0, "end_percent": end_pct}}
        wf["11"]["inputs"]["model"] = ["31", 0]
    wf["13"]["inputs"]["filename_prefix"] = "nuomitianshi/agree_" + label
    return wf


def run(label, use_lllite, strength, end_pct):
    print("--- %s" % label, flush=True)
    print("      LLLite=%s strength=%.2f end=%.2f"
          % ("是" if use_lllite else "否", strength, end_pct), flush=True)
    wf = build(label, use_lllite, strength, end_pct)
    t0 = time.time()
    pid = None
    while time.time() - t0 < DEADLINE:
        if pid is None:
            r = _post("/prompt", {"prompt": wf, "client_id": "agree"})
            if "_err" in r or "prompt_id" not in r:
                print("      提交失败: %s" % json.dumps(r, ensure_ascii=False)[:500], flush=True)
                return None
            pid = r["prompt_id"]
        else:
            hist = _get("/history/" + pid)
            if hist and pid in hist:
                entry = hist[pid]
                if entry.get("status", {}).get("status_str") == "error":
                    print("      报错", flush=True)
                    return None
                for node in entry.get("outputs", {}).values():
                    for im in node.get("images", []):
                        base = os.path.basename(im.get("filename", ""))
                        cand = os.path.join(OUTDIR, im.get("subfolder", ""), base)
                        if not os.path.exists(cand):
                            hits = glob.glob(os.path.join(OUTDIR, "**", base), recursive=True)
                            cand = max(hits, key=os.path.getmtime) if hits else cand
                        print("      [%5.1fs] %s" % (time.time() - t0, cand), flush=True)
                        return cand
            q = _get("/queue")
            if q is not None:
                ids = [x[1] for x in q.get("queue_running", [])] + \
                      [x[1] for x in q.get("queue_pending", [])]
                if pid not in ids:
                    pid = None
        time.sleep(POLL)
    print("      超时", flush=True)
    return None


def edge_map(path, size=(256, 374)):
    """下采样成灰度 → 梯度幅值，当『构图/边缘』指纹用。"""
    from PIL import Image
    import numpy as np
    im = Image.open(path).convert("RGB").resize(size, Image.LANCZOS)
    g = np.asarray(im).astype(np.float32).mean(axis=2)
    gy, gx = np.gradient(g)
    e = np.sqrt(gx * gx + gy * gy)
    return e


def similarity(a_path, b_path):
    import numpy as np
    a, b = edge_map(a_path), edge_map(b_path)
    a = (a - a.mean()) / (a.std() + 1e-6)
    b = (b - b.mean()) / (b.std() + 1e-6)
    return float((a * b).mean())


def main():
    q = _get("/queue")
    if q is None:
        print("连不上 ComfyUI")
        return 1
    if len(q.get("queue_running", [])) + len(q.get("queue_pending", [])) and "--force" not in sys.argv:
        print("队列非空，先等；要强行排队加 --force")
        return 1

    outs = {}
    for case in CASES:
        p = run(*case)
        if p is None:
            print("失败，中止")
            return 1
        outs[case[0]] = p

    print()
    print("与参考图 reference02.png 的**边缘图相关性**（越高越像它的构图）:")
    for label, path in outs.items():
        print("   %-16s %.4f" % (label, similarity(path, REFERENCE)))
    print()
    print("输出文件：")
    for label, path in outs.items():
        print("   %-16s %s" % (label, path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
