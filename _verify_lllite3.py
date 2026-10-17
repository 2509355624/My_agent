# -*- coding: utf-8 -*-
"""LLLite 第三轮：把姿势描述从提示词里拿掉，让控制图独自决定。

前两轮结论：控制图和提示词**冲突**时，提示词赢。
  A/B: any + Canny(0.92)  —— 控制图「抱膝坐」 vs 提示词「站着叉腰」→ 出图站着
  C  : pose-1 直连(1.2)   —— 同上 → 出图还是站着
（但 A/B/C 三张图彼此不同、也都不等于纯文生图基线，说明补丁**确实在起作用**，
  只是压不过一句写得很死的姿势描述。）

所以这轮把**槽位 6（动作）和槽位 18（镜头）清空**，其余槽位不动，
再看姿势是不是跟着控制图走。这才是用户真实用法：
「别用提示词描述姿势，改用参考图」。

  E: 控制图 = 抱膝坐骨架
  F: 控制图 = 站着叉腰骨架

如果 E 出抱膝坐、F 出站着叉腰 → 控制图能独立决定姿势，路线 2 成立。
如果 E/F 差不多 → 这层是装饰性的，得如实告诉用户。
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
PATCH = "anima-lllite-pose-1.safetensors"
STRENGTH = 1.2
END_PCT = 0.75
WIDTH, HEIGHT = 832, 1216          # 跟作者姿势工作流一致，也跟控制图同尺寸

DEADLINE = 900.0
POLL = 3.0

CASES = [
    ("E_nopose_hugging_knees", "nuomi_ctrl_hugging_knees_pose.png"),
    ("F_nopose_hands_on_hips", "nuomi_ctrl_hands_on_hips_pose.png"),
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
        return {"_err": "HTTP %s: %s" % (e.code, e.read().decode("utf-8", "replace")[:600])}
    except Exception as e:
        return {"_err": str(e)}


def build(label, ctrl_image):
    with open(SKELETON, encoding="utf-8") as f:
        wf = json.load(f)

    wf["1"]["inputs"]["unet_name"] = LOCAL_UNET
    # 把姿势描述从提示词里拿掉，交给控制图
    wf["6"]["inputs"]["value"] = ""
    wf["18"]["inputs"]["value"] = ""
    # 尺寸跟控制图对齐
    wf["10"]["inputs"]["width"] = WIDTH
    wf["10"]["inputs"]["height"] = HEIGHT

    wf["29"] = {"class_type": "ModelPatchLoader", "inputs": {"name": PATCH}}
    wf["30"] = {"class_type": "LoadImage", "inputs": {"image": ctrl_image}}
    wf["31"] = {"class_type": "AnimaLLLiteApply",
                "inputs": {"model": ["16", 0], "model_patch": ["29", 0],
                           "image": ["30", 0], "strength": STRENGTH,
                           "start_percent": 0.0, "end_percent": END_PCT}}
    wf["11"]["inputs"]["model"] = ["31", 0]
    wf["13"]["inputs"]["filename_prefix"] = "nuomitianshi/lllite3_" + label
    return wf


def run_case(label, ctrl_image):
    print("--- %s（控制图 %s）" % (label, ctrl_image), flush=True)
    wf = build(label, ctrl_image)
    print("      提示词槽 6/18 已清空；%dx%d；pose-1 %.2f / end %.2f"
          % (WIDTH, HEIGHT, STRENGTH, END_PCT), flush=True)

    t0 = time.time()
    wipes = 0
    pid = None
    while time.time() - t0 < DEADLINE:
        if pid is None:
            r = _post("/prompt", {"prompt": wf, "client_id": "verify_lllite3"})
            if "_err" in r or "prompt_id" not in r:
                print("      提交失败: %s" % json.dumps(r, ensure_ascii=False)[:600],
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
    for label, ctrl in CASES:
        ok = run_case(label, ctrl) and ok
    print()
    print("完成：%s" % ("全部成功" if ok else "有失败，见上面日志"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
