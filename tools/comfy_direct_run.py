#!/usr/bin/env python
"""直接向 ComfyUI 提交 workflow 跑单张图（不走 5174 队列）。

用途：5174 常驻服务不在线时的应急实测。一次只提一个任务、等它彻底跑完
（图片落盘）再返回，避免「连画 2 张触发 ComfyUI 自动重启把队列清空」。

用法示例：
  python tools/comfy_direct_run.py \
      --workflow skills/silver/workflow.json \
      --prompt "1girl, ..." --seed 1445863681 \
      --prefix TEST1_silver --out web/实测_sf脚趾/T1_silver.png

可选改动（都只改提交副本，不落盘改 skill 文件）：
  --cfg 2                 两个 KSampler 的 cfg 全改
  --steps-first 10 --steps-second 5
  --lora-chain            按 silver 同款链序插 6 条 LoraLoaderModelOnly（只挂第一段）
  --single                删二采样（节点 25/27），VAEDecode 直连第一段

⚠️ 占位符替换规则与 app/tools/normal/generate_image.py 对齐：裸 token
__MULTI_PROMPTS__ 按字符串内部转义替换（兼容独占式和嵌入式两种写法），
__SEED__ 连引号版落数字、裸版兜底。改规则前先读那边的注释。
"""
import argparse
import json
import sys
import time
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8188"
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

SILVER_LORA_CHAIN = [
    ("16", "anima_context_detailer_base10.safetensors", 0.35),
    ("15", "star cat-000022.safetensors", 0.35),
    ("28", "ANIMA_MODARE_V2.safetensors", 0.3),
    ("29", "_cowani_epoch_10.safetensors", 0.8),
    ("30", "Anima_in_real_update_v2_epoch_20.safetensors", 1),
    ("31", "reina_v1_epoch14.safetensors", 0.8),
]


def http_get(path, timeout=30):
    return OPENER.open(BASE + path, timeout=timeout).read()


def http_post(path, obj, timeout=60):
    data = json.dumps(obj).encode("utf-8")
    req = urllib.request.Request(
        BASE + path, data=data, headers={"Content-Type": "application/json"}
    )
    return json.loads(OPENER.open(req, timeout=timeout).read())


def wait_ready(deadline=180):
    """ComfyUI 可能正在自动重启，等到 /system_stats 有响应为止。"""
    t0 = time.time()
    while time.time() - t0 < deadline:
        try:
            http_get("/system_stats", timeout=5)
            return True
        except Exception:
            time.sleep(3)
    return False


def build_workflow(args):
    # ⚠️ 占位符替换规则必须与 app/tools/normal/generate_image.py 的
    # _apply_prompt_placeholders 保持一致（2026-10-25 踩坑：本工具原先只替
    # 带引号的 '"__MULTI_PROMPTS__"'，而 moss8 的文本是嵌入式
    # "...quality, __MULTI_PROMPTS__"（逗号+空格开头），引号版永远匹配不上，
    # 导致提示词原样提交、前三张 moss 图全是废提示词跑的，且同 seed 同文本
    # 命中 ComfyUI 执行缓存 0.07s 出图）。正确做法：裸 token + 内部转义。
    raw = open(args.workflow, "r", encoding="utf-8").read()
    prompt_escaped = (args.prompt.replace('\\', '\\\\').replace('"', '\\"')
                      .replace('\n', '\\n').replace('\r', ''))
    raw = raw.replace("__MULTI_PROMPTS__", prompt_escaped)
    raw = raw.replace('"__SEED__"', str(args.seed))
    raw = raw.replace("__SEED__", str(args.seed))  # 兜底：裸占位符
    w = json.loads(raw)

    for node in w.values():
        if node.get("class_type") == "SaveImage":
            node["inputs"]["filename_prefix"] = args.prefix

    if args.cfg is not None:
        for node in w.values():
            if node.get("class_type") == "KSampler":
                node["inputs"]["cfg"] = args.cfg

    if args.steps_first is not None:
        w["2"]["inputs"]["steps"] = args.steps_first
    if args.steps_second is not None:
        w["27"]["inputs"]["steps"] = args.steps_second

    if args.single:
        # 删二采样：LatentUpscaleBy(25) + KSampler(27)，VAEDecode 接回第一段
        for nid in ("25", "27"):
            w.pop(nid, None)
        w["3"]["inputs"]["samples"] = ["2", 0]

    if args.lora_chain:
        prev = "5"
        for nid, lora, strength in SILVER_LORA_CHAIN:
            w[nid] = {
                "class_type": "LoraLoaderModelOnly",
                "inputs": {
                    "model": [prev, 0],
                    "lora_name": lora,
                    "strength_model": strength,
                },
            }
            prev = nid
        w["2"]["inputs"]["model"] = [prev, 0]

    return w


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workflow", required=True)
    ap.add_argument("--prompt", required=True)
    ap.add_argument("--seed", type=int, default=1445863681)
    ap.add_argument("--prefix", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--label", default="run")
    ap.add_argument("--cfg", type=float, default=None)
    ap.add_argument("--steps-first", type=int, default=None)
    ap.add_argument("--steps-second", type=int, default=None)
    ap.add_argument("--lora-chain", action="store_true")
    ap.add_argument("--single", action="store_true")
    ap.add_argument("--timeout", type=int, default=3600)
    args = ap.parse_args()

    if not wait_ready():
        print("[%s] ComfyUI 未就绪（可能正在重启），放弃" % args.label)
        sys.exit(2)

    wf = build_workflow(args)
    try:
        r = http_post("/prompt", {"prompt": wf, "client_id": "direct-run"})
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        print("[%s] 提交被拒: HTTP %s %s" % (args.label, e.code, body[:2000]))
        sys.exit(3)
    pid = r["prompt_id"]
    print("[%s] submitted prompt_id=%s" % (args.label, pid))

    t0 = time.time()
    last_msg = ""
    while time.time() - t0 < args.timeout:
        time.sleep(3)
        try:
            h = json.loads(http_get("/history/" + pid, timeout=15))
        except Exception:
            continue
        entry = h.get(pid)
        if not entry:
            continue
        status = entry.get("status", {})
        if status.get("status_str") == "error":
            print("[%s] 执行出错:" % args.label)
            for m in status.get("messages", []):
                print("   ", json.dumps(m, ensure_ascii=False)[:500])
            sys.exit(4)
        images = []
        for out in entry.get("outputs", {}).values():
            images.extend(out.get("images", []))
        if images:
            for i, img in enumerate(images):
                fn = img["filename"]
                sub = img.get("subfolder", "")
                typ = img.get("type", "output")
                url = "/view?filename=%s&subfolder=%s&type=%s" % (
                    urllib.parse.quote(fn),
                    urllib.parse.quote(sub),
                    urllib.parse.quote(typ),
                )
                data = http_get(url, timeout=180)
                out_path = args.out if len(images) == 1 else args.out.replace(
                    ".png", "_%d.png" % (i + 1)
                )
                with open(out_path, "wb") as f:
                    f.write(data)
            print(
                "[%s] 完成，墙上时钟 %.1fs，共 %d 张 -> %s"
                % (args.label, time.time() - t0, len(images), args.out)
            )
            return
    print("[%s] 超时 %ds 未完成" % (args.label, args.timeout))
    sys.exit(5)


if __name__ == "__main__":
    main()
