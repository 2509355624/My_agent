# -*- coding: utf-8 -*-
"""LLLite 第二轮：姿势补丁 + 极端强度对照。

第一轮结论：`any-test-like-v2` + Canny，strength 0.92 时**压不过提示词**——
控制图是「抱膝坐」、提示词是「站着叉腰」，出图仍然是站着叉腰（控制图只影响了
裙色/取景这类表层）。所以这轮验两件事：

  C: 用作者专门的 `pose-1` 补丁 + 骨架图直连（无 Canny），strength 1.2 / end 0.75
     —— 完全照抄 `nuomitianshi-anima-modular-v4-pose-api.json` 的接法。
     如果 C 出来是抱膝坐 → 姿势补丁**能**压过提示词，路线 2 成立。

  D: 同一个「抱膝坐」线稿，但 strength 拉到 3.0 / end 1.0
     —— 排除「LLLite 根本没接上」的可能。如果 3.0 还是跟 0.92 差不多，
     说明这层本身就是软偏置，不是硬控制。

提示词两次仍是骨架自带的「standing, hands on hips / cowboy shot」。
"""
import json
import os
import sys
import time
import urllib.request

HOST = "http://127.0.0.1:8188"
BUNDLE = r"D:\AI\temp\anima-private-assets\workflows\api"
SKELETON = os.path.join(BUNDLE, "nuomitianshi-anima-modular-v4-api.json")

LOCAL_UNET = "miaomiaoRealskin_anima13.safetensors"
DEADLINE = 900.0
POLL = 3.0

CASES = [
    # label, 补丁, 控制图, 走不走 Canny, strength, end
    ("C_pose_hugging_knees", "anima-lllite-pose-1.safetensors",
     "nuomi_ctrl_hugging_knees_pose.png", False, 1.2, 0.75),
    ("D_any_extreme_3.0", "anima-lllite-any-test-like-v2.safetensors",
     "nuomi_ctrl_hugging_knees_lineart.png", True, 3.0, 1.0),
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
    except Exception as e:
        return {"_err": str(e)}


def build(label, patch, ctrl_image, use_canny, strength, end_pct):
    with open(SKELETON, encoding="utf-8") as f:
        wf = json.load(f)

    wf["1"]["inputs"]["unet_name"] = LOCAL_UNET
    wf["29"] = {"class_type": "ModelPatchLoader", "inputs": {"name": patch}}
    wf["30"] = {"class_type": "LoadImage", "inputs": {"image": ctrl_image}}

    if use_canny:
        wf["32"] = {"class_type": "Canny",
                    "inputs": {"image": ["30", 0], "low_threshold": 0.12,
                               "high_threshold": 0.3}}
        wf["33"] = {"class_type": "ImageInvert", "inputs": {"image": ["32", 0]}}
        ctrl_src = ["33", 0]
    else:
        ctrl_src = ["30", 0]      # 骨架直连，作者就是这么接的

    wf["31"] = {"class_type": "AnimaLLLiteApply",
                "inputs": {"model": ["16", 0], "model_patch": ["29", 0],
                           "image": ctrl_src, "strength": strength,
                           "start_percent": 0.0, "end_percent": end_pct}}
    wf["11"]["inputs"]["model"] = ["31", 0]
    wf["13"]["inputs"]["filename_prefix"] = "nuomitianshi/lllite2_" + label
    return wf


def run_case(label, patch, ctrl_image, use_canny, strength, end_pct):
    print("--- %s" % label, flush=True)
    print("      补丁 %s / 控制图 %s / Canny %s / strength %.2f / end %.2f"
          % (patch, ctrl_image, "是" if use_canny else "否", strength, end_pct),
          flush=True)
    wf = build(label, patch, ctrl_image, use_canny, strength, end_pct)

    t0 = time.time()
    wipes = 0
    pid = None
    while time.time() - t0 < DEADLINE:
        if pid is None:
            r = _post("/prompt", {"prompt": wf, "client_id": "verify_lllite2"})
            if "_err" in r or "prompt_id" not in r:
                print("      提交失败: %s" % json.dumps(r, ensure_ascii=False)[:400],
                      flush=True)
                return False
            pid = r["prompt_id"]
            print("      [%5.1fs] 已提交 %s" % (time.time() - t0, pid[:8]), flush=True)
        else:
            hist = _get("/history/" + pid)
            if hist and pid in hist:
                entry = hist[pid]
                st = entry.get("status", {})
                if st.get("status_str") == "error":
                    print("      执行报错: %s"
                          % json.dumps(st, ensure_ascii=False)[:700], flush=True)
                    return False
                names = []
                for node in entry.get("outputs", {}).values():
                    for im in node.get("images", []):
                        names.append("%s/%s" % (im.get("subfolder", ""),
                                                im.get("filename", "")))
                print("      [%5.1fs] success" % (time.time() - t0), flush=True)
                for n in names:
                    print("      输出 %s" % n, flush=True)
                return True

            q = _get("/queue")
            if q is not None:
                ids = [x[1] for x in q.get("queue_running", [])] + \
                      [x[1] for x in q.get("queue_pending", [])]
                if pid not in ids:
                    wipes += 1
                    print("      [%5.1fs] 被清空（第 %d 次），重提" % (time.time() - t0, wipes),
                          flush=True)
                    pid = None
        time.sleep(POLL)

    print("      超时", flush=True)
    return False


def main():
    q = _get("/queue")
    if q is None:
        print("连不上 ComfyUI（8188）")
        return 1
    busy = len(q.get("queue_running", [])) + len(q.get("queue_pending", []))
    if busy and "--force" not in sys.argv:
        print("队列非空（%d），先等；要强行排队加 --force" % busy)
        return 1

    ok = True
    for case in CASES:
        ok = run_case(*case) and ok
    print()
    print("完成：%s" % ("全部成功" if ok else "有失败，见上面日志"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
