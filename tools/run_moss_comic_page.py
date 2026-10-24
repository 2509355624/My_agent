"""跑一张漫画页：支持 stock KSampler 的渠道（moss_page / moss50 / moss8），
也支持 RapidMOSSAdapter 的适配器渠道（moss / lodestones-8step）。

两种接线：
  stock      : UNET → [LoRA] → ModelSamplingSD3 → SLG → KSampler（cfg + 步数）
  adapter    : UNET → RapidMOSSAdapter → BasicGuider + SamplerCustomAdvanced（无 CFG）

    python tools/run_moss_comic_page.py --list
    python tools/run_moss_comic_page.py --skill moss_page --steps 30
    python tools/run_moss_comic_page.py --adapter lodestones-8step --steps 8 --width 720 --height 1280
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, ".")
from app.skills import load_workflow  # noqa: E402

BASE = "http://127.0.0.1:8188"
_OP = urllib.request.build_opener(urllib.request.ProxyHandler({}))

PAGE_PROMPT = (
    "exactly 4 panels in a 2x2 grid, manga page, separated by thin white gutters, "
    "black and white ink, screentone shading. "
    "Top left panel: close-up of a girl with long black hair sitting by a classroom window, "
    "afternoon light, she looks outside, speech bubble says \"今天又迟到了\". "
    "Top right panel: the same girl standing in the school corridor holding a stack of books, "
    "a boy in a school uniform walks past her, speech bubble says \"你的书掉了\". "
    "Bottom left panel: the girl crouching down to pick up the scattered books, "
    "embarrassed expression, speech bubble says \"不用你管\". "
    "Bottom right panel: the boy handing her the last book, small smile, "
    "speech bubble says \"下次小心点\"."
)


def get(url, timeout=20):
    with _OP.open(urllib.request.Request(url, headers={"User-Agent": "moss"}),
                  timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def post(path, obj, timeout=60):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(obj).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with _OP.open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def by_type(wf, ct):
    return [k for k, v in wf.items() if v.get("class_type") == ct]


def one(wf, ct, required=True):
    ks = by_type(wf, ct)
    if not ks:
        if required:
            raise SystemExit("工作流里没有 %s 节点" % ct)
        return None
    return ks[0]


def build(args):
    wf = load_workflow("skills/%s/workflow.json" % args.skill)

    k = one(wf, "MOSSImageUNETLoader", required=False)
    if k and args.unet:
        wf[k]["inputs"]["unet_name"] = args.unet

    if args.clip_device:
        k = one(wf, "MOSSImageCLIPLoader", required=False)
        if k:
            wf[k]["inputs"]["device"] = args.clip_device

    k = one(wf, "EmptyFlux2LatentImage")
    wf[k]["inputs"]["width"] = args.width
    wf[k]["inputs"]["height"] = args.height
    wf[k]["inputs"]["batch_size"] = 1

    # 正向提示词 = 带 __MULTI_PROMPTS__ 的那个 CLIPTextEncode；没有就取第一个
    pos = None
    for k, v in wf.items():
        if v["class_type"] == "CLIPTextEncode" and "__MULTI_PROMPTS__" in str(v["inputs"].get("text", "")):
            pos = k
            break
    if pos is None:
        pos = one(wf, "CLIPTextEncode")
    wf[pos]["inputs"]["text"] = args.prompt

    k = one(wf, "SaveImage")
    wf[k]["inputs"]["filename_prefix"] = args.prefix

    ks = one(wf, "KSampler", required=False)
    if ks:
        wf[ks]["inputs"]["seed"] = args.seed
        if args.steps:
            wf[ks]["inputs"]["steps"] = args.steps
        if args.cfg:
            wf[ks]["inputs"]["cfg"] = args.cfg
        if args.slg_off:
            n = one(wf, "SkipLayerGuidanceDiTSimple", required=False)
            if n:
                wf.pop(n)
                for v in wf.values():
                    for kk, vv in list(v["inputs"].items()):
                        if isinstance(vv, list) and str(vv[0]) == n:
                            print("  [warn] 删 SLG 后仍有节点引用它:", k, kk)
        return wf

    ad = one(wf, "RapidMOSSAdapter")
    wf[ad]["inputs"]["adapter"] = args.adapter
    wf[ad]["inputs"]["steps"] = args.steps
    wf[ad]["inputs"]["lora_strength"] = args.lora_strength
    k = one(wf, "RandomNoise", required=False)
    if k:
        wf[k]["inputs"]["noise_seed"] = args.seed
    return wf


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--skill", default="moss_page")
    ap.add_argument("--unet", default="")
    ap.add_argument("--adapter", default="lodestones-8step")
    ap.add_argument("--steps", type=int, default=0)
    ap.add_argument("--cfg", type=float, default=0.0)
    ap.add_argument("--lora-strength", type=float, default=1.0)
    ap.add_argument("--width", type=int, default=1024)
    ap.add_argument("--height", type=int, default=1536)
    ap.add_argument("--seed", type=int, default=20261023)
    ap.add_argument("--prefix", default="MOSS-Image/comicpage")
    ap.add_argument("--prompt", default=PAGE_PROMPT)
    ap.add_argument("--prompt-file", default="")
    ap.add_argument("--slg-off", action="store_true")
    ap.add_argument("--clip-device", default="", help="把文本编码器放 cpu 以腾出显存")
    ap.add_argument("--timeout", type=int, default=900)
    ap.add_argument("--list", action="store_true", help="列出可用适配器目录")
    args = ap.parse_args()

    if args.list:
        d = get(BASE + "/object_info/RapidMOSSAdapter")
        print(json.dumps(d["RapidMOSSAdapter"]["input"]["required"]["adapter"], ensure_ascii=False))
        return

    if args.prompt_file:
        with open(args.prompt_file, encoding="utf-8") as f:
            args.prompt = f.read().strip()

    wf = build(args)
    print("skill=%s steps=%s %dx%d" % (args.skill, args.steps, args.width, args.height), flush=True)
    t0 = time.time()
    try:
        r = post("/prompt", {"prompt": wf, "client_id": "moss-comic"})
    except urllib.error.HTTPError as e:
        print("提交失败 HTTP %s\n%s" % (e.code, e.read().decode("utf-8", "replace")[:1200]))
        return 1
    if "prompt_id" not in r:
        print("提交返回异常：", json.dumps(r, ensure_ascii=False)[:1200])
        return 1
    pid = r["prompt_id"]
    print("prompt_id", pid, flush=True)

    tick = 0
    while time.time() - t0 < args.timeout:
        time.sleep(3)
        try:
            h = get(BASE + "/history/" + pid)
        except Exception:
            continue
        if pid not in h:
            el = int(time.time() - t0)
            if el // 20 != tick:
                tick = el // 20
                print("   ... %ds" % el, flush=True)
            continue
        st = h[pid].get("status", {})
        imgs = []
        for node in h[pid].get("outputs", {}).values():
            imgs += node.get("images", [])
        print("用时 %.1fs  status=%s" % (time.time() - t0, st.get("status_str")), flush=True)
        for im in imgs:
            print("  图片:", im.get("subfolder"), im.get("filename"), flush=True)
        if st.get("status_str") != "success":
            for m in st.get("messages", [])[-3:]:
                print("  ", str(m)[:500])
        return 0
    print("超时 %ds 未出结果" % args.timeout)
    return 2


if __name__ == "__main__":
    sys.exit(main())
