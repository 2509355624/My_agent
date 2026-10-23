"""用官方 `rapid-moss-4step` 适配器跑 miaomiao，验证「少步适配器能不能套在别的底模上」。

模板直接抄 `skills/moss/workflow.json`（那条链路实测 4 步 31 秒），
只换 UNET 和画布/提示词。目的：在没有 lodestones 完整适配器包的情况下，
先让 miaomiao 出一张能看的图。
"""
import json
import os
import random
import sys
import time
import urllib.request

os.environ["NO_PROXY"] = "*"
for _k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
    os.environ.pop(_k, None)

COMFY = "http://127.0.0.1:8188"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from app.skills import load_workflow           # noqa: E402
from app.tools.normal.generate_image import apply_prompt_placeholders   # noqa: E402

UI_WF = r"D:/AI/ComfyUI_v037/user/default/workflows/moss8步长.json"


def post(path, payload):
    req = urllib.request.Request(
        COMFY + path,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def get(path):
    with urllib.request.urlopen(COMFY + path, timeout=30) as r:
        return json.loads(r.read())


def main():
    wf = load_workflow(os.path.join(ROOT, "skills", "moss", "workflow.json"))
    for v in wf.values():
        if v["class_type"] == "MOSSImageUNETLoader":
            v["inputs"]["unet_name"] = "miaomiaoHaremMOSS_baseV10_int8.safetensors"
        if v["class_type"] == "RapidMOSSAdapter":
            v["inputs"]["steps"] = 4
        if v["class_type"] == "EmptyFlux2LatentImage":
            v["inputs"]["width"], v["inputs"]["height"] = 1024, 1536
        if v["class_type"] == "SaveImage":
            v["inputs"]["filename_prefix"] = "MOSS-Image/miaoHarem_4step_rapid"

    n = {x["id"]: x for x in json.load(open(UI_WF, encoding="utf-8"))["nodes"]}
    prompt = n[41]["widgets_values"][0].strip()
    seed = random.randint(1, 2 ** 31 - 1)
    wf = apply_prompt_placeholders(json.dumps(wf, ensure_ascii=False), prompt, seed)
    if isinstance(wf, str):
        wf = json.loads(wf)

    print("提交 miaomiao + rapid-moss-4step, 1024x1536, seed=%s" % seed, flush=True)
    t0 = time.time()
    pid = post("/prompt", {"prompt": wf, "client_id": "miao4"})["prompt_id"]
    print("prompt_id", pid, flush=True)
    while time.time() - t0 < 300:
        time.sleep(5)
        try:
            h = get("/history/" + pid)
        except Exception:
            print("  查询失败", flush=True)
            continue
        if pid in h:
            e = h[pid]
            print("完成 %.1f 秒 status=%s" % (time.time() - t0,
                                            e.get("status", {}).get("status_str")), flush=True)
            for node, out in (e.get("outputs") or {}).items():
                for img in out.get("images", []):
                    print("  图 → %s/%s" % (img.get("subfolder", ""), img.get("filename")))
            return
    raise SystemExit("超时")


if __name__ == "__main__":
    main()
