# -*- coding: utf-8 -*-
"""换一张参考图：这个银发绿尖双辫的精灵耳角色，能不能复刻？

跟前面几轮的关键差别：**这个角色不是 `nuomi tianshi`**。
所以必须把骨架里的 nuomi 角色 LoRA 关掉（strength 0），否则出来的一定是绿发道姑。
关掉之后，角色的身份只能靠**提示词**和**参考图**两条路。

三组只差 LLLite 这一层（同 seed / 同提示词 / 同尺寸 / 同底模）：
  J: 纯文生图（只用提示词）
  K: + Canny(参考图) → any-test-like-v2 @0.92 / end 0.92
  L: + Canny(参考图) → any-test-like-v2 @1.00 / end 1.00

参考图先缩到 832×1216 再过 Canny（跟作者 `-r4` 那份工作流一致），
避免在 3072×4608 上算 Canny 得到完全不同的边缘密度。

跑完算 J/K/L 各自与参考图的**边缘图相关性**（构图接近程度）。
内容方向：完全着装的非性化，负面词里的 `nude, explicit` 保留。
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
REFERENCE = r"D:\AI\ComfyUI_v037\output\Anima_00007_.png"
LOCAL_UNET = "miaomiaoRealskin_anima13.safetensors"
CTRL_NAME = "nuomi_ref_green.png"
OUTDIR = r"D:\AI\ComfyUI_v037\output"
WIDTH, HEIGHT = 832, 1216
DEADLINE = 900.0
POLL = 3.0

# 槽位 = scene-presets.json 的 module_nodes
SLOTS = {
    "5": "masterpiece, best quality, safe, 1girl, solo, long silver white hair with green "
         "gradient tips, twin braids, green eyes, pointy elf ears, white flower hair ornament.",
    "14": "",
    "20": "A pink and white frilled dress with a large black ribbon bow at the chest, white "
          "ruffled apron, long pink sleeves, white opaque tights, a small heart-shaped brooch. "
          "Fully clothed.",
    "6": "sitting on a sofa, legs crossed, both hands resting on her lap, leaning slightly "
         "back against the backrest. Solo.",
    "17": "looking at viewer, parted lips, calm gentle expression.",
    "18": "full body, seated pose, eye level, centered composition.",
    "19": "A bright pink room with a tufted pink leather sofa, a large window on the left, a "
          "pink balloon floating at the upper right, soft daylight, no lettering.",
}

CASES = [
    ("J_pure_t2i", False, 0.0, 0.0),
    ("K_lllite_0.92", True, 0.92, 0.92),
    ("L_lllite_1.00", True, 1.00, 1.00),
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
    wf["2"]["inputs"]["strength_model"] = 0.0          # 关掉 nuomi 角色 LoRA
    for node, text in SLOTS.items():
        wf[node]["inputs"]["value"] = text
    wf["10"]["inputs"]["width"] = WIDTH
    wf["10"]["inputs"]["height"] = HEIGHT

    if use_lllite:
        wf["29"] = {"class_type": "ModelPatchLoader",
                    "inputs": {"name": "anima-lllite-any-test-like-v2.safetensors"}}
        wf["30"] = {"class_type": "LoadImage", "inputs": {"image": CTRL_NAME}}
        wf["34"] = {"class_type": "ImageScale",
                    "inputs": {"image": ["30", 0], "upscale_method": "lanczos",
                               "width": WIDTH, "height": HEIGHT, "crop": "disabled"}}
        wf["32"] = {"class_type": "Canny",
                    "inputs": {"image": ["34", 0], "low_threshold": 0.12,
                               "high_threshold": 0.3}}
        wf["33"] = {"class_type": "ImageInvert", "inputs": {"image": ["32", 0]}}
        wf["31"] = {"class_type": "AnimaLLLiteApply",
                    "inputs": {"model": ["16", 0], "model_patch": ["29", 0],
                               "image": ["33", 0], "strength": strength,
                               "start_percent": 0.0, "end_percent": end_pct}}
        wf["11"]["inputs"]["model"] = ["31", 0]

    wf["13"]["inputs"]["filename_prefix"] = "nuomitianshi/green_" + label
    return wf


def run(label, use_lllite, strength, end_pct):
    print("--- %s" % label, flush=True)
    print("      LLLite=%s strength=%.2f end=%.2f / nuomi LoRA=0"
          % ("是" if use_lllite else "否", strength, end_pct), flush=True)
    wf = build(label, use_lllite, strength, end_pct)
    t0 = time.time()
    pid = None
    while time.time() - t0 < DEADLINE:
        if pid is None:
            r = _post("/prompt", {"prompt": wf, "client_id": "green"})
            if "_err" in r or "prompt_id" not in r:
                print("      提交失败: %s" % json.dumps(r, ensure_ascii=False)[:500], flush=True)
                return None
            pid = r["prompt_id"]
        else:
            hist = _get("/history/" + pid)
            if hist and pid in hist:
                entry = hist[pid]
                if entry.get("status", {}).get("status_str") == "error":
                    print("      报错: %s" % json.dumps(entry.get("status"), ensure_ascii=False)[:500],
                          flush=True)
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
    from PIL import Image
    import numpy as np
    im = Image.open(path).convert("RGB").resize(size, Image.LANCZOS)
    g = np.asarray(im).astype(np.float32).mean(axis=2)
    gy, gx = np.gradient(g)
    e = np.sqrt(gx * gx + gy * gy)
    return (e - e.mean()) / (e.std() + 1e-6)


def similarity(a, b):
    return float((edge_map(a) * edge_map(b)).mean())


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
    print("与参考图 Anima_00007_.png 的边缘图相关性（越高越像它的构图）:")
    for label, path in outs.items():
        print("   %-16s %.4f" % (label, similarity(path, REFERENCE)))
    print()
    print("输出文件：")
    for label, path in outs.items():
        print("   %-16s %s" % (label, path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
