# -*- coding: utf-8 -*-
"""确定性对照：**同一个工作流连跑两遍**，看采样器本身是不是可复现。

为什么必须先做这个：
  代码层面查到 `post_input` 这个 hook 全仓库只有定义、没人消费，而
  `comfy/ldm/anima/model.py` 也不走 `transformer_options["patches_replace"]` ——
  也就是说 `AnimaLLLiteApply` 很可能**完全空转**。
  可如果它空转，控制图不同的 E/F 两张图就该**像素级相同**，实测却差 11.6。

  两种解释只能二选一：
    (1) 补丁确实有一点点作用（11.6 的差就是它造成的）
    (2) 采样器 `dpmpp_2m_sde_gpu` 本身不确定（11.6 是随机噪声）
  跑两遍**完全相同**的工作流就能分辨：相同 → (1)，不同 → (2)。

跑完直接算像素差。
"""
import json
import os
import sys
import time
import glob
import urllib.request

HOST = "http://127.0.0.1:8188"
BUNDLE = r"D:\AI\temp\anima-private-assets\workflows\api"
SKELETON = os.path.join(BUNDLE, "nuomitianshi-anima-modular-v4-api.json")
LOCAL_UNET = "miaomiaoRealskin_anima13.safetensors"
CTRL_A = "nuomi_ctrl_hugging_knees_pose.png"
CTRL_B = "nuomi_ctrl_hugging_knees_pose_copy.png"
OUTDIR = r"D:\AI\ComfyUI_v037\output\nuomitianshi"
DEADLINE = 900.0
POLL = 3.0


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


def build(label, ctrl):
    with open(SKELETON, encoding="utf-8") as f:
        wf = json.load(f)
    wf["1"]["inputs"]["unet_name"] = LOCAL_UNET
    wf["6"]["inputs"]["value"] = ""
    wf["18"]["inputs"]["value"] = ""
    wf["10"]["inputs"]["width"] = 832
    wf["10"]["inputs"]["height"] = 1216
    wf["29"] = {"class_type": "ModelPatchLoader",
                "inputs": {"name": "anima-lllite-pose-1.safetensors"}}
    wf["30"] = {"class_type": "LoadImage", "inputs": {"image": ctrl}}
    wf["31"] = {"class_type": "AnimaLLLiteApply",
                "inputs": {"model": ["16", 0], "model_patch": ["29", 0],
                           "image": ["30", 0], "strength": 1.2,
                           "start_percent": 0.0, "end_percent": 0.75}}
    wf["11"]["inputs"]["model"] = ["31", 0]
    wf["13"]["inputs"]["filename_prefix"] = "nuomitianshi/repeat_" + label
    return wf


def run(label, ctrl):
    print("--- %s" % label, flush=True)
    wf = build(label, ctrl)
    t0 = time.time()
    pid = None
    while time.time() - t0 < DEADLINE:
        if pid is None:
            r = _post("/prompt", {"prompt": wf, "client_id": "repeat"})
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
                        cand = os.path.join(OUTDIR, im.get("subfolder", ""),
                                            os.path.basename(im.get("filename", "")))
                        if not os.path.exists(cand):
                            hits = glob.glob(os.path.join(OUTDIR, "**",
                                                          os.path.basename(im.get("filename", ""))),
                                             recursive=True)
                            cand = max(hits, key=os.path.getmtime) if hits else cand
                        p = cand
                        print("      [%5.1fs] %s" % (time.time() - t0, p), flush=True)
                        return p
            q = _get("/queue")
            if q is not None:
                ids = [x[1] for x in q.get("queue_running", [])] + \
                      [x[1] for x in q.get("queue_pending", [])]
                if pid not in ids:
                    pid = None
        time.sleep(POLL)
    return None


def main():
    paths = []
    for label, ctrl in (("run1", CTRL_A), ("run2", CTRL_B)):
        p = run(label, ctrl)
        if p is None:
            print("失败，中止")
            return 1
        paths.append(p)

    from PIL import Image
    import numpy as np
    a = np.asarray(Image.open(paths[0]).convert("RGB")).astype(np.float32)
    b = np.asarray(Image.open(paths[1]).convert("RGB")).astype(np.float32)
    diff = float(np.abs(a - b).mean())
    print()
    print("两次完全相同的工作流，平均绝对像素差 = %.4f" % diff)
    print("判定：%s" % ("采样可复现 → 之前的对比有效"
                     if diff < 0.5 else
                     "采样不可复现 → 之前 A/B/C/E/F 的差异里混了随机噪声，结论要重估"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
