"""直接向 ComfyUI 提交 moss8，**不设 180 秒闸门**，把一轮完整跑完。

用途只有一个：量真实耗时、拿一张图。走常驻队列（`submit_moss8_test.py`）
在 >180 秒的配置上会被 `image_jobs.wait_done` 每 180 秒掐一次并重启 ComfyUI，
永远跑不完（2026-10-23 实测连掐三轮）。

⚠️ 这里的任务**不在** QQ 队列里，进程退出、ComfyUI 重启都会丢。只做诊断用。
"""
import argparse
import json
import os
import sys
import time
import urllib.request

os.environ["NO_PROXY"] = "*"
for _k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
    os.environ.pop(_k, None)

COMFY = "http://127.0.0.1:8188"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.skills import load_workflow           # noqa: E402
from app.tools.normal.generate_image import apply_prompt_placeholders   # noqa: E402


def post(path, payload):
    req = urllib.request.Request(
        COMFY + path,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def get(path):
    with urllib.request.urlopen(COMFY + path, timeout=30) as r:
        return json.loads(r.read())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--skill", default="moss8")
    ap.add_argument("--steps", type=int, default=0, help="0 = 用工作流里的值")
    ap.add_argument("--slg", default="keep", choices=["keep", "off"],
                    help="off = 把 SLG 区间压成 0~0（等价于不跑第二遍前向）")
    ap.add_argument("--wide", type=int, default=0)
    ap.add_argument("--high", type=int, default=0)
    ap.add_argument("--prompt-file", default="")
    ap.add_argument("--cfg", type=float, default=0.0)
    ap.add_argument("--dtype", default="",
                    help="覆盖 MOSSImageUNETLoader 的 weight_dtype（bf16 会反量化）")
    ap.add_argument("--no-lora", action="store_true",
                    help="把 UNET 直接接到 ModelSamplingAuraFlow，绕过 LoRA 那一环")
    ap.add_argument("--timeout", type=int, default=900)
    args = ap.parse_args()

    wf = load_workflow("skills/%s/workflow.json" % args.skill)
    if not wf:
        raise SystemExit("工作流读不出来")

    ks = next(v for v in wf.values() if v["class_type"] == "KSampler")
    lat = next(v for v in wf.values() if v["class_type"] == "EmptyFlux2LatentImage")
    if args.steps:
        ks["inputs"]["steps"] = args.steps
    if args.cfg:
        ks["inputs"]["cfg"] = args.cfg
    if args.wide:
        lat["inputs"]["width"] = args.wide
    if args.high:
        lat["inputs"]["height"] = args.high
    if args.slg == "off":
        for v in wf.values():
            if v["class_type"] == "SkipLayerGuidanceDiTSimple":
                v["inputs"]["start_percent"] = 0.0
                v["inputs"]["end_percent"] = 0.0

    if args.dtype:
        for v in wf.values():
            if v["class_type"] == "MOSSImageUNETLoader":
                v["inputs"]["weight_dtype"] = args.dtype
        print("  [dtype] weight_dtype =", args.dtype, flush=True)

    if args.no_lora:
        # 把 UNET 直接接到 ModelSamplingAuraFlow，整条 LoRA 链不参与计算，
        # 用来隔离「int8 模型挂 LoRA 是不是把速度拖垮了」。
        unet = next(k for k, v in wf.items()
                    if v["class_type"] == "MOSSImageUNETLoader")
        for v in wf.values():
            if v["class_type"] == "ModelSamplingAuraFlow":
                v["inputs"]["model"] = [unet, 0]
        print("  [no-lora] 已绕过 LoraLoader", flush=True)

    prompt = ""
    if args.prompt_file:
        prompt = open(args.prompt_file, encoding="utf-8").read().strip()
    else:
        ui = r"D:/AI/ComfyUI_v037/user/default/workflows/moss8步长.json"
        n = {x["id"]: x for x in json.load(open(ui, encoding="utf-8"))["nodes"]}
        prompt = n[41]["widgets_values"][0].strip()

    import random
    seed = random.randint(1, 2 ** 31 - 1)
    wf = apply_prompt_placeholders(
        json.dumps(wf, ensure_ascii=False), prompt, seed)
    if isinstance(wf, str):
        wf = json.loads(wf)

    print("提交 steps=%s slg=%s %sx%s seed=%s"
          % (ks["inputs"]["steps"], args.slg, lat["inputs"]["width"],
             lat["inputs"]["height"], seed), flush=True)
    t0 = time.time()
    r = post("/prompt", {"prompt": wf, "client_id": "moss8-diag"})
    pid = r["prompt_id"]
    print("prompt_id", pid, flush=True)

    while time.time() - t0 < args.timeout:
        time.sleep(5)
        try:
            h = get("/history/" + pid)
        except Exception as e:
            print("  history 查询失败 %s" % type(e).__name__, flush=True)
            continue
        if pid in h:
            e = h[pid]
            st = e.get("status", {})
            print("完成 用时 %.1f 秒 status=%s"
                  % (time.time() - t0, st.get("status_str")), flush=True)
            if st.get("status_str") != "success":
                print("  ⚠️ 非 success：%s" % json.dumps(st, ensure_ascii=False)[:600])
            for node, out in (e.get("outputs") or {}).items():
                for img in out.get("images", []):
                    print("  图 %s → %s/%s"
                          % (node, img.get("subfolder", ""), img.get("filename")))
            return
        q = get("/queue")
        running = len(q.get("queue_running", []))
        print("  %5.0fs 排队/运行 running=%s" % (time.time() - t0, running),
              flush=True)
    raise SystemExit("超时 %s 秒仍未完成" % args.timeout)


if __name__ == "__main__":
    main()
