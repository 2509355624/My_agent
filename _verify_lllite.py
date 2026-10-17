# -*- coding: utf-8 -*-
"""LLLite「路线 1」最小验证：控制图到底能不能压过提示词。

设计（单变量）：
  基线 = `_nuomi_test_hands_on_hips.png`（同工作流、同 seed 248613、同提示词、
         同底模 miaomiaoRealskin_anima13，纯文生图，站着叉腰）

  A: 控制图 = `nuomi_ctrl_hands_on_hips.png`（站着叉腰 —— 跟提示词**一致**）
  B: 控制图 = `nuomi_ctrl_hugging_knees.png`（抱膝坐 —— 跟提示词**矛盾**）

提示词两次完全一样（骨架自带的「standing, hands on hips / cowboy shot」）。
如果 B 出来是抱膝坐 → 控制图压过了提示词 → 姿态复刻成立。
如果 A/B 都跟基线差不多 → LLLite 没吃进去。

改动只在骨架（24 节点）上加了 5 个节点，并把 KSampler.model 从 16 改到 31：
  29 ModelPatchLoader(anima-lllite-any-test-like-v2)
  30 LoadImage(控制图)
  32 Canny(30, 0.12, 0.3)
  33 ImageInvert(32)
  31 AnimaLLLiteApply(model<-16, model_patch<-29, image<-33, 0.92 / 0.0 / 0.92)
"""
import json
import os
import sys
import time
import urllib.request

HOST = "http://127.0.0.1:8188"
BUNDLE = r"D:\AI\temp\anima-private-assets\workflows\api"
SKELETON = os.path.join(BUNDLE, "nuomitianshi-anima-modular-v4-api.json")

LOCAL_UNET = "miaomiaoRealskin_anima13.safetensors"   # = hd_3_clear / hd_3_soft 的底模
PATCH_NAME = "anima-lllite-any-test-like-v2.safetensors"
STRENGTH = 0.92
START_PCT = 0.0
END_PCT = 0.92

DEADLINE = 900.0
POLL = 3.0

CASES = [
    ("A_match_hands_on_hips", "nuomi_ctrl_hands_on_hips.png"),
    ("B_conflict_hugging_knees", "nuomi_ctrl_hugging_knees.png"),
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


def build(label, ctrl_image):
    with open(SKELETON, encoding="utf-8") as f:
        wf = json.load(f)

    # 底模换成本机的 Anima（原包用的是 waiANIMA_v10Base10，本机没有）
    wf["1"]["inputs"]["unet_name"] = LOCAL_UNET

    # 插入 LLLite 控制链
    wf["29"] = {"class_type": "ModelPatchLoader", "inputs": {"name": PATCH_NAME}}
    wf["30"] = {"class_type": "LoadImage", "inputs": {"image": ctrl_image}}
    wf["32"] = {"class_type": "Canny",
                "inputs": {"image": ["30", 0], "low_threshold": 0.12,
                           "high_threshold": 0.3}}
    wf["33"] = {"class_type": "ImageInvert", "inputs": {"image": ["32", 0]}}
    wf["31"] = {"class_type": "AnimaLLLiteApply",
                "inputs": {"model": ["16", 0], "model_patch": ["29", 0],
                           "image": ["33", 0], "strength": STRENGTH,
                           "start_percent": START_PCT, "end_percent": END_PCT}}

    # KSampler 改用打过补丁的模型
    wf["11"]["inputs"]["model"] = ["31", 0]

    wf["13"]["inputs"]["filename_prefix"] = "nuomitianshi/lllite_" + label
    return wf


def run_case(label, ctrl_image):
    print("--- %s（控制图 %s）" % (label, ctrl_image), flush=True)
    wf = build(label, ctrl_image)
    print("      节点数 %d，strength %.2f / end %.2f"
          % (len(wf), STRENGTH, END_PCT), flush=True)

    t0 = time.time()
    submits = 0
    wipes = 0
    pid = None
    while time.time() - t0 < DEADLINE:
        if pid is None:
            r = _post("/prompt", {"prompt": wf, "client_id": "verify_lllite"})
            if "_err" in r or "prompt_id" not in r:
                print("      提交失败: %s" % json.dumps(r, ensure_ascii=False)[:300],
                      flush=True)
                return False
            pid = r["prompt_id"]
            submits += 1
            print("      [%5.1fs] 已提交 %s" % (time.time() - t0, pid[:8]), flush=True)
        else:
            hist = _get("/history/" + pid)
            if hist and pid in hist:
                entry = hist[pid]
                st = entry.get("status", {})
                if st.get("status_str") == "error":
                    print("      执行报错: %s"
                          % json.dumps(st, ensure_ascii=False)[:600], flush=True)
                    return False
                outs = entry.get("outputs", {})
                names = []
                for node in outs.values():
                    for im in node.get("images", []):
                        names.append("%s/%s" % (im.get("subfolder", ""),
                                                im.get("filename", "")))
                print("      [%5.1fs] success" % (time.time() - t0), flush=True)
                for n in names:
                    print("      输出 %s" % n, flush=True)
                return True

            # 掉线/被内存重启清空 → 重新提交
            q = _get("/queue")
            if q is not None:
                ids = [x[1] for x in q.get("queue_running", [])] + \
                      [x[1] for x in q.get("queue_pending", [])]
                if pid not in ids:
                    wipes += 1
                    print("      [%5.1fs] 任务被清空（第 %d 次），重新提交"
                          % (time.time() - t0, wipes), flush=True)
                    pid = None
        time.sleep(POLL)

    print("      超时（提交 %d 次 / 被清 %d 次）" % (submits, wipes), flush=True)
    return False


def main():
    q = _get("/queue")
    if q is None:
        print("连不上 ComfyUI（8188）")
        return 1
    busy = len(q.get("queue_running", [])) + len(q.get("queue_pending", []))
    if busy and "--force" not in sys.argv:
        print("队列非空（%d 个任务），先等它跑完；要强行排队加 --force" % busy)
        return 1

    ok = True
    for label, ctrl in CASES:
        ok = run_case(label, ctrl) and ok
    print()
    print("完成：%s" % ("全部成功" if ok else "有失败，见上面日志"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
