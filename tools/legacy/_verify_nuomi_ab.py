# -*- coding: utf-8 -*-
"""A/B：同 seed 同提示词，只把角色 LoRA 强度归零，看身份是不是塌掉。

目的：确认「跑通」不只是提示词在起作用，而是 LoRA 真的接管了角色。
用法：python _verify_nuomi_ab.py 0.0
"""
import json
import os
import sys
import time
import urllib.request

API = "http://127.0.0.1:8188"
SRC = r"D:\AI\temp\anima-private-assets\workflows\api\nuomitianshi-anima-modular-v4-api.json"
LOCAL_UNET = "miaomiaoRealskin_anima13.safetensors"
STRENGTH = float(sys.argv[1]) if len(sys.argv) > 1 else 0.0
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                   "_verify_nuomi_ab_%s.json" % STRENGTH)


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


wf = json.load(open(SRC, encoding="utf-8"))
for node in wf.values():
    if node.get("class_type") == "UNETLoader":
        node["inputs"]["unet_name"] = LOCAL_UNET
    if node.get("class_type") == "LoraLoaderModelOnly" and \
            "nuomitianshi" in node["inputs"]["lora_name"]:
        print("角色 LoRA 强度 %s → %s" % (node["inputs"]["strength_model"], STRENGTH))
        node["inputs"]["strength_model"] = STRENGTH
    if node.get("class_type") == "SaveImage":
        node["inputs"]["filename_prefix"] = "nuomitianshi/verify_ab_s%s" % STRENGTH
json.dump(wf, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

t0 = time.time()
res = req("/prompt", {"prompt": wf, "client_id": "verify-nuomi-ab"})
if not res or "prompt_id" not in res:
    print("提交失败：%s" % json.dumps(res)[:500])
    sys.exit(1)
pid = res["prompt_id"]
print("pid=%s" % pid)
while time.time() - t0 < 900:
    hist = req("/history/" + pid) or {}
    if pid in hist:
        st = hist[pid].get("status", {})
        print("[%6.1fs] status=%s" % (time.time() - t0, st.get("status_str")))
        if st.get("status_str") != "success":
            print(json.dumps(st, ensure_ascii=False)[:2000])
            sys.exit(1)
        for out in hist[pid].get("outputs", {}).values():
            for img in out.get("images", []):
                print("  输出 %s/%s" % (img.get("subfolder"), img.get("filename")))
        sys.exit(0)
    time.sleep(8)
print("超时")
sys.exit(2)
