# -*- coding: utf-8 -*-
"""单变量实测：只改提示词槽位，姿势跟不跟。

基线：`_nuomi_test_hands_on_hips.png`（modular-v4-api.json，seed 248613，站着叉腰）。
本次：同工作流、同 seed、同身份/服装/表情/场景，**只改槽位 04 动作 + 06 镜头**。
跑两种措辞，看姿势是不是真跟着提示词走。
"""
import json
import os
import sys
import time
import urllib.request

API = "http://127.0.0.1:8188"
SRC = r"D:\AI\temp\anima-private-assets\workflows\api\nuomitianshi-anima-modular-v4-api.json"
LOCAL_UNET = "miaomiaoRealskin_anima13.safetensors"
HERE = os.path.dirname(os.path.abspath(__file__))

CASES = {
    # A：包里自己的措辞风格（自然语言句）
    "A_seiza_sentence": {
        "6": "She sits in seiza on the floor, kneeling with her legs folded under her, "
             "both hands resting flat on her thighs. Solo.",
        "18": "Portrait, full body, eye level, centered composition.",
    },
    # B：booru 标签风格（更短、更标签化）
    "B_seiza_tags": {
        "6": "sitting, seiza, kneeling, hands on thighs, solo",
        "18": "full body, eye level, front view",
    },
}


def req(path, payload=None, timeout=20):
    try:
        url = API + path
        r = (urllib.request.Request(url) if payload is None else
             urllib.request.Request(url, data=json.dumps(payload).encode(),
                                    headers={"Content-Type": "application/json"}))
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return json.load(resp)
    except Exception as exc:                           # noqa: BLE001
        print("      (%s → %s)" % (path, type(exc).__name__))
        return None


def run(label, overrides):
    wf = json.load(open(SRC, encoding="utf-8"))
    for node in wf.values():
        if node.get("class_type") == "UNETLoader":
            node["inputs"]["unet_name"] = LOCAL_UNET
        if node.get("class_type") == "SaveImage":
            node["inputs"]["filename_prefix"] = "nuomitianshi/pose_test_%s" % label
    for nid, text in overrides.items():
        wf[nid]["inputs"]["value"] = text
    # 确认 seed 没被动过
    seed = [n["inputs"]["seed"] for n in wf.values()
            if n.get("class_type") == "KSampler"][0]
    print("--- %s（seed=%s）" % (label, seed))
    for nid in overrides:
        print("      槽位 %s → %s" % (nid, overrides[nid][:90]))

    res = req("/prompt", {"prompt": wf, "client_id": "pose-test"})
    if not res or "prompt_id" not in res:
        print("      提交失败：%s" % json.dumps(res)[:400])
        return False
    pid = res["prompt_id"]
    t0 = time.time()
    while time.time() - t0 < 900:
        hist = req("/history/" + pid) or {}
        if pid in hist:
            st = hist[pid].get("status", {})
            print("      [%5.1fs] %s" % (time.time() - t0, st.get("status_str")))
            if st.get("status_str") != "success":
                print(json.dumps(st, ensure_ascii=False)[:1500])
                return False
            for out in hist[pid].get("outputs", {}).values():
                for img in out.get("images", []):
                    print("      输出 %s/%s" % (img.get("subfolder"), img.get("filename")))
            return True
        time.sleep(8)
    print("      超时")
    return False


ok = all(run(label, ov) for label, ov in CASES.items())
sys.exit(0 if ok else 1)
