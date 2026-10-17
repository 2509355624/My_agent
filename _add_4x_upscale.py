# -*- coding: utf-8 -*-
"""给 qwen-charsheet / qwen-restore 末尾挂 4x 超分链（照 qwen-hd 的接法），
并把角色设定表的画布改回 3072×2048。

节点（两个 skill 的 VAEDecode 都在 10、SaveImage 都在 11）：
  12 UpscaleModelLoader      model_name = 4x-AnimeSharp.pth
  13 SplitImageWithAlpha     image      = ["10", 0]      ← VAEDecode
  14 ImageUpscaleWithModel   upscale_model = ["12",0] / image = ["13",0]
  11 SaveImage.images        ["10",0] → ["14", 0]

⚠️ seed 是**未加引号的** `__SEED__`（非法 JSON）。按 §19 的套路：换成一个
   **字符串**占位符再 parse，写回前还原成裸 token。**绝不**用
   `replace('__SEED__', 数字)` —— 那会把占位符永久烘成字面量（见 DETAILS.md
   的「陷阱」一节，10-10 刚踩过）。

⚠️ 换行按各文件现状保留：charsheet 两份是 CRLF、qwen-restore 两份是 LF。
"""
import json
import os

ROOT = os.path.dirname(os.path.abspath(__file__))
PLACE = "__SEED_PLACEHOLDER__"
UPSCALER = "4x-AnimeSharp.pth"

CHARSHEET = [("skills/qwen-charsheet/workflow.json", (3072, 2048)),
             ("skills/qwen-charsheet/workflow_i2i.json", (3072, 2048))]
RESTORE = [("skills/qwen-restore/workflow.json", None),
           ("skills/qwen-restore/workflow_i2i.json", None)]


def load(path):
    with open(path, encoding="utf-8", newline="") as f:
        raw = f.read()
    crlf = "\r\n" in raw
    wf = json.loads(raw.replace("__SEED__", '"%s"' % PLACE))
    return wf, crlf


def save(path, wf, crlf):
    out = json.dumps(wf, ensure_ascii=False, indent=2)
    out = out.replace('"%s"' % PLACE, "__SEED__")     # 还原成裸 token
    if crlf:
        out = out.replace("\n", "\r\n")
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(out)


def add_upscale(wf):
    """挂上 4x 链，并把 SaveImage 的输入接到链尾。"""
    vae = [nid for nid, n in wf.items() if n["class_type"] == "VAEDecode"]
    save_img = [nid for nid, n in wf.items() if n["class_type"] == "SaveImage"]
    assert len(vae) == 1 and len(save_img) == 1, (vae, save_img)
    vae, save_img = vae[0], save_img[0]

    wf["12"] = {"class_type": "UpscaleModelLoader",
                "inputs": {"model_name": UPSCALER}}
    wf["13"] = {"class_type": "SplitImageWithAlpha",
                "inputs": {"image": [vae, 0]}}
    wf["14"] = {"class_type": "ImageUpscaleWithModel",
                "inputs": {"upscale_model": ["12", 0], "image": ["13", 0]}}
    wf[save_img]["inputs"]["images"] = ["14", 0]
    return vae, save_img


for path, canvas in CHARSHEET + RESTORE:
    p = os.path.join(ROOT, path)
    wf, crlf = load(p)
    assert "12" not in wf, "%s 已经有 12 号节点了，先看一眼再跑" % path

    if canvas:
        assert wf["7"]["class_type"] == "EmptyLatentImage", path
        wf["7"]["inputs"]["width"], wf["7"]["inputs"]["height"] = canvas

    vae, save_img = add_upscale(wf)
    save(p, wf, crlf)

    kinds = [n["class_type"] for n in wf.values()]
    steps = [n["inputs"].get("steps") for n in wf.values()
             if n["class_type"] == "KSampler"]
    size = (wf["7"]["inputs"].get("width"), wf["7"]["inputs"].get("height")) \
        if wf["7"]["class_type"] == "EmptyLatentImage" else "n/a"
    print("%-42s CRLF=%-5s 画布=%-12s 步数=%s VAEDecode=%s SaveImage=%s←%s  4x=%s"
          % (path, crlf, size, steps, vae, save_img,
             wf[save_img]["inputs"]["images"], kinds.count("ImageUpscaleWithModel")))
