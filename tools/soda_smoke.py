"""Soda Storyboard 冒烟测试：官方「起身舒展」五格示例跑一遍。

前提（都已就位）：
  · 节点包在 ComfyUI-nightly/custom_nodes/ComfyUI-Soda-Storyboard
  · confyui_env 装了 soda-storyboard 0.5.0
  · 分镜包在 ComfyUI input/soda-storyboard/stretch-color.storyboard.json
  · 三个模型与示例同名，nightly 经 extra_model_paths 读得到

用法： D:/AI/confyui_env/Scripts/python.exe tools/soda_smoke.py
流程： 等 ComfyUI 就绪 → 确认 Soda 节点已注册 → POST /prompt → 轮询 → 存成图。
"""
import json
import sys
import time
import urllib.request

HOST = "http://127.0.0.1:8188"
OUT = r"D:\AI\tmp\soda_stretch_color.png"

# 与官方 stretch-color.workflow.json 逐节点对齐（widget 值照抄）。
GRAPH = {
    "1": ("SodaLoadStoryboard", {"filename": "stretch-color.storyboard.json",
                                 "dialogue_mode": "跟随页面", "slant_mode": "cut",
                                 "include_triggers": True, "width": 0, "height": 0}),
    "2": ("UNETLoader", {"unet_name": "moss_image_sft_int8_convrot.safetensors",
                         "weight_dtype": "default"}),
    "4": ("ModelSamplingAuraFlow", {"model": ["2", 0], "shift": 5}),
    "5": ("CLIPLoader", {"clip_name": "qwen3.5_9b_w4a8_gptq.safetensors",
                         "type": "moss_image", "device": "default"}),
    "6": ("VAELoader", {"vae_name": "flux2-vae.safetensors"}),
    "7": ("EmptyFlux2LatentImage", {"width": 1024, "height": 1536, "batch_size": 1}),
    # text 接导入节点的 slot 0 = 整页 MOSS JSON；slot 6 = negative。
    "8": ("CLIPTextEncode", {"clip": ["5", 0], "text": ["1", 0]}),
    "9": ("CLIPTextEncode", {"clip": ["5", 0], "text": ["1", 6]}),
    "10": ("KSampler", {"model": ["15", 0], "positive": ["8", 0],
                        "negative": ["9", 0], "latent_image": ["7", 0],
                        "seed": 20261022, "steps": 50, "cfg": 3.0,
                        "sampler_name": "euler", "scheduler": "simple",
                        "denoise": 1.0}),
    "11": ("VAEDecode", {"samples": ["10", 0], "vae": ["6", 0]}),
    "13": ("SodaLetterPage", {"image": ["11", 0], "page_info": ["1", 7], "font": ""}),
    "14": ("SaveImage", {"images": ["13", 0],
                         "filename_prefix": "SodaStoryboard/stretch-color"}),
    "15": ("SkipLayerGuidanceDiTSimple", {"model": ["4", 0], "double_layers": "",
                                          "single_layers": "22",
                                          "start_percent": 0.0, "end_percent": 1.0}),
}


def _get(path, timeout=5):
    with urllib.request.urlopen(HOST + path, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def wait_ready(minutes=6):
    """等 ComfyUI 就绪；顺带确认 Soda 节点真的注册进来了。"""
    deadline = time.time() + minutes * 60
    while time.time() < deadline:
        try:
            _get("/system_stats", timeout=2)
            info = _get("/object_info/SodaLoadStoryboard", timeout=5)
            if info.get("SodaLoadStoryboard"):
                print("[ok] ComfyUI 就绪，Soda 节点已注册")
                return True
            print("[..] ComfyUI 在线但 Soda 节点没注册，检查 custom_nodes")
            return False
        except Exception:
            time.sleep(5)
    print("[fail] %d 分钟内 ComfyUI 没起来（看门狗是否在跑？）" % minutes)
    return False


def submit():
    body = json.dumps({"prompt": {k: {"class_type": t, "inputs": i}
                                  for k, (t, i) in GRAPH.items()},
                       "client_id": "soda-smoke"}).encode("utf-8")
    req = urllib.request.Request(HOST + "/prompt", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        res = json.loads(r.read().decode("utf-8"))
    if res.get("error"):
        print("[fail] 提交被拒：", json.dumps(res, ensure_ascii=False)[:800])
        return None
    pid = res.get("prompt_id")
    print("[ok] 已入队 prompt_id=%s" % pid)
    return pid


def wait_done(pid, minutes=15):
    deadline = time.time() + minutes * 60
    while time.time() < deadline:
        try:
            h = _get("/history/%s" % pid, timeout=5)
        except Exception:
            h = {}
        rec = (h or {}).get(pid)
        if rec:
            st = rec.get("status") or {}
            if st.get("completed"):
                if st.get("status_str") == "error":
                    msgs = [m for m in (st.get("messages") or [])
                            if m[0] == "execution_error"]
                    print("[fail] 执行出错：",
                          json.dumps(msgs, ensure_ascii=False)[:1500])
                    return None
                print("[ok] 完成，用时见 history")
                return rec
        time.sleep(5)
    print("[fail] %d 分钟没跑完" % minutes)
    return None


def fetch(rec):
    outs = rec.get("outputs") or {}
    for nid, o in outs.items():
        for im in o.get("images") or []:
            fn, sub = im.get("filename"), im.get("subfolder", "")
            typ = im.get("type", "output")
            url = "%s/view?filename=%s&subfolder=%s&type=%s" % (
                HOST, urllib.request.quote(fn), urllib.request.quote(sub),
                urllib.request.quote(typ))
            with urllib.request.urlopen(url, timeout=30) as r:
                data = r.read()
            with open(OUT, "wb") as f:
                f.write(data)
            print("[ok] 成图已存：%s（%d KB，节点 %s）" % (OUT, len(data) // 1024, nid))
            return True
    print("[fail] 输出里没有图片：", json.dumps(outs, ensure_ascii=False)[:600])
    return False


def main():
    if not wait_ready():
        return 1
    pid = submit()
    if not pid:
        return 1
    rec = wait_done(pid)
    if not rec:
        return 1
    return 0 if fetch(rec) else 1


if __name__ == "__main__":
    sys.exit(main())
