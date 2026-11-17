# -*- coding: utf-8 -*-
"""把 qwen-charsheet 的 UI 格式 workflow.json 转成 Bot 能提交的 API 格式。

- 从当前（已带「右下角无文字」修正的）UI workflow.json 里抽 PrimitiveStringMultiline
  的静态提示词（7 分区角色设定表指令，约 2508 字）。
- 照 _run_charsheet_test.py 实测通过的节点图（UnetLoaderGGUF → ModelAttentionBackend
  → QwenImage21Cache → CLIPLoader → VAELoader → LoadImage → EmptyLatentImage →
  TextEncodeQwenImage21 → KSampler(15步/res_multistep+beta/denoise 1.0) → VAEDecode
  → SaveImage）写成 API 格式 dict。
- 静态提示词 + "__MULTI_PROMPTS__"（用户描述占位符）拼进 TextEncodeQwenImage21.prompt；
  seed 写成裸 `__SEED__`（与全仓库其它工作流约定一致，load_workflow 会补引号）。
- 同时产出 workflow.json 与 workflow_i2i.json：qwen-charsheet 没有纯文生图模式，
  两个文件都从参考图取角色/画风，i2i 那份把 LoadImage.image 标成 "__SOURCE_IMAGE__"。
"""
import json
import os
import shutil

SKILL = r"D:\AI\agent_my_test\skills\qwen-charsheet"
SRC = os.path.join(SKILL, "workflow.json")          # 当前 UI 格式
BACKUP = os.path.join(SKILL, "workflow.ui_backup.json")

# 先把当前 UI 版存一份，免得哪天想要回去看（skills/ 在 gitignore 里，不进仓库）。
if not os.path.exists(BACKUP):
    shutil.copy(SRC, BACKUP)
    print("[*] 已备份当前 UI 版到", BACKUP)

data = json.load(open(SRC, encoding="utf-8"))
static = next(n["widgets_values"][0] for n in data["nodes"]
              if n["type"] == "PrimitiveStringMultiline")
print("[*] 抽到的静态提示词长度:", len(static))

UNET = "qwen-image-2.1-UC-Q4_K_M.gguf"
CLIP = "qwen3vl_8b_w4a8.safetensors"
VAE = "qwen_image_2.1_vae_bf16.safetensors"
W, H = 2304, 1536

# 静态 7 分区指令 + 用户描述占位符（用户没给描述时 __MULTI_PROMPTS__ 落空串）。
combined = static + "\n\n" + "__MULTI_PROMPTS__"


def build():
    return {
        "1": {"class_type": "UnetLoaderGGUF", "inputs": {"unet_name": UNET}},
        "2": {"class_type": "ModelAttentionBackend",
              "inputs": {"model": ["1", 0], "attention": "comfy kitchen attention"}},
        "3": {"class_type": "QwenImage21Cache",
              "inputs": {"model": ["2", 0], "device": "auto", "dtype": "default"}},
        "4": {"class_type": "CLIPLoader",
              "inputs": {"clip_name": CLIP, "type": "qwen_image", "device": "default"}},
        "5": {"class_type": "VAELoader", "inputs": {"vae_name": VAE}},
        "6": {"class_type": "LoadImage",
              "inputs": {"image": "__SOURCE_IMAGE__", "upload": "image"}},
        "7": {"class_type": "EmptyLatentImage",
              "inputs": {"width": W, "height": H, "batch_size": 1}},
        "8": {"class_type": "TextEncodeQwenImage21",
              "inputs": {"clip": ["4", 0], "prompt": combined, "negative_prompt": "",
                         "resolution": 1536, "images.image_1": ["6", 0],
                         "vae": ["5", 0]}},
        "9": {"class_type": "KSampler",
              "inputs": {"model": ["3", 0], "positive": ["8", 0], "negative": ["8", 1],
                         "latent_image": ["7", 0], "seed": "__SEED__", "steps": 15,
                         "cfg": 1.0, "sampler_name": "res_multistep",
                         "scheduler": "beta", "denoise": 1.0}},
        "10": {"class_type": "VAEDecode",
                "inputs": {"samples": ["9", 0], "vae": ["5", 0]}},
        "11": {"class_type": "SaveImage",
                "inputs": {"images": ["10", 0], "filename_prefix": "QwenCharsheet"}},
    }


wf = build()
# 落盘：seed 写成裸 `__SEED__`（load_workflow 会补成 "__SEED__"），与全仓库约定一致。
s = json.dumps(wf, ensure_ascii=False, indent=2)
s = s.replace('"__SEED__"', "__SEED__")

with open(os.path.join(SKILL, "workflow.json"), "w", encoding="utf-8") as f:
    f.write(s)
with open(os.path.join(SKILL, "workflow_i2i.json"), "w", encoding="utf-8") as f:
    f.write(s)
print("[*] 已写入 workflow.json 与 workflow_i2i.json（API 格式，i2i 占位 __SOURCE_IMAGE__）")
