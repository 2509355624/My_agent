# -*- coding: utf-8 -*-
"""真机冒烟：按改造后的 Production 工作流跑一张角色设定表。
直接用 ComfyUI 的 HTTP API（不经过 qq_bot 的队列），跑完打印输出文件名。
"""
import json
import os
import time
import uuid
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8188"
SRC_IMG = r"D:\AI\ComfyUI_v037\output\Anima_00091_.png"
WF = r"D:\AI\temp\QwenImage21Character_qwenImage21V30\Character_Sheet_Production.local.json"
UNET = "qwen-image-2.1-UC-Q4_K_M.gguf"
CLIP = "qwen3vl_8b_w4a8.safetensors"
VAE = "qwen_image_2.1_vae_bf16.safetensors"
# 直接照搬 217 s 那次 25 步跑法，只把步数砍到 18，看质量能不能保住。
W, H = 2304, 1536
STEPS = 15


def get(path, timeout=30):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
        return json.loads(r.read().decode())


def post(path, payload):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        print("[!] POST %s 失败 HTTP %s" % (path, e.code), flush=True)
        print(e.read().decode("utf-8", "replace")[:4000], flush=True)
        raise


# ---- 0. 队列得是空的，别跟机器人抢 ----
q = get("/queue")
print("[0] 队列 running=%d pending=%d" % (len(q.get("queue_running", [])),
                                          len(q.get("queue_pending", []))), flush=True)

# ---- 1. 取静态提示词 ----
d = json.load(open(WF, encoding="utf-8"))
static = next(n["widgets_values"][0] for n in d["nodes"]
              if n["type"] == "PrimitiveStringMultiline")
print("[1] 静态提示词 %d 字" % len(static), flush=True)


# ---- 2. 上传参考图 ----
def upload(path):
    b = "----wb" + uuid.uuid4().hex
    name = os.path.basename(path)
    data = open(path, "rb").read()
    body = b"".join([
        ("--%s\r\n" % b).encode(),
        ('Content-Disposition: form-data; name="image"; filename="%s"\r\n' % name).encode(),
        b"Content-Type: image/png\r\n\r\n",
        data, b"\r\n",
        ("--%s\r\n" % b).encode(),
        b'Content-Disposition: form-data; name="overwrite"\r\n\r\ntrue\r\n',
        ("--%s--\r\n" % b).encode(),
    ])
    req = urllib.request.Request(
        BASE + "/upload/image", data=body,
        headers={"Content-Type": "multipart/form-data; boundary=%s" % b})
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.loads(r.read().decode())


up = upload(SRC_IMG)
img = up.get("name") or os.path.basename(SRC_IMG)
if up.get("subfolder"):
    img = up["subfolder"] + "/" + img
print("[2] 参考图已上传：%s  (%.1f MB)" % (img, os.path.getsize(SRC_IMG) / 1e6), flush=True)

# ---- 3. 组 API prompt（单段降步数版本）----
prompt = {
    "1": {"class_type": "UnetLoaderGGUF",
          "inputs": {"unet_name": UNET}},
    "2": {"class_type": "ModelAttentionBackend",
          "inputs": {"model": ["1", 0], "attention": "comfy kitchen attention"}},
    "3": {"class_type": "QwenImage21Cache",
          "inputs": {"model": ["2", 0], "device": "auto", "dtype": "default"}},
    "4": {"class_type": "CLIPLoader",
          "inputs": {"clip_name": CLIP, "type": "qwen_image", "device": "default"}},
    "5": {"class_type": "VAELoader", "inputs": {"vae_name": VAE}},
    "6": {"class_type": "LoadImage", "inputs": {"image": img, "upload": "image"}},
    "7": {"class_type": "EmptyLatentImage",
          "inputs": {"width": W, "height": H, "batch_size": 1}},
    "8": {"class_type": "TextEncodeQwenImage21",
          "inputs": {"clip": ["4", 0], "prompt": static, "negative_prompt": "",
                     "resolution": 1536, "images.image_1": ["6", 0],
                     "vae": ["5", 0]}},
    "9": {"class_type": "KSampler",
          "inputs": {"model": ["3", 0], "positive": ["8", 0], "negative": ["8", 1],
                     "latent_image": ["7", 0], "seed": 42, "steps": STEPS, "cfg": 1.0,
                     "sampler_name": "res_multistep", "scheduler": "beta",
                     "denoise": 1.0}},
    "10": {"class_type": "VAEDecode", "inputs": {"samples": ["9", 0], "vae": ["5", 0]}},
    "11": {"class_type": "SaveImage",
           "inputs": {"images": ["10", 0],
                      "filename_prefix": "CharSheet_%dstep" % STEPS}},
}
print("[3] 提交 %d 节点，%d×%d / %d 步 res_multistep+beta（单段）"
      % (len(prompt), W, H, STEPS), flush=True)
def submit():
    return post("/prompt", {"prompt": prompt, "client_id": uuid.uuid4().hex})["prompt_id"]


def in_queue(p):
    q = get("/queue")
    for e in list(q.get("queue_running", [])) + list(q.get("queue_pending", [])):
        try:
            if e[1] == p:
                return True
        except Exception:
            continue
    return False


pid = submit()
print("    prompt_id =", pid, flush=True)

# ⚠️ image_jobs 的「内存水位」看门狗会在提交前后重启 ComfyUI，而重启会清空 in-memory
# 队列 —— 那个 prompt_id 就永远等不到结果了（10-10 白跑两次都是这个）。所以提交后
# 要确认它真的进了队列，没进就重提，别傻等 1800 秒。
for attempt in range(3):
    seen = False
    for _ in range(8):
        time.sleep(2.5)
        try:
            if in_queue(pid):
                seen = True
                break
        except Exception:
            continue
    if seen:
        print("    已确认进队列（第 %d 次提交）" % (attempt + 1), flush=True)
        break
    print("    [!] 提交后 20 秒仍未进队列 —— 多半被看门狗重启冲掉，重提一次", flush=True)
    pid = submit()
    print("    prompt_id =", pid, flush=True)

# ---- 4. 轮询 ----
t0 = time.time()
while True:
    time.sleep(5)
    el = time.time() - t0
    try:
        h = get("/history/" + pid)
    except Exception as e:
        print("   ... %.0fs 查 history 出错 %r" % (el, e), flush=True)
        continue
    if pid in h:
        e = h[pid]
        st = e.get("status", {})
        print("[4] 结束：%.1f 秒  status=%s" % (el, st.get("status_str")), flush=True)
        if st.get("status_str") != "success":
            for m in st.get("messages", [])[-4:]:
                print("     ", json.dumps(m, ensure_ascii=False)[:1500], flush=True)
        for nid, out in (e.get("outputs") or {}).items():
            print("    输出节点 %s → %s" % (nid, out.get("images")), flush=True)
        break
    if el > 1800:
        print("[!] 30 分钟还没完，放弃等待", flush=True)
        break
    q = get("/queue")
    print("   ... %.0fs  running=%d pending=%d"
          % (el, len(q.get("queue_running", [])), len(q.get("queue_pending", []))),
          flush=True)
print("[5] 结束", flush=True)
