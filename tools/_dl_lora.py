# -*- coding: utf-8 -*-
"""把 LoRA 直接下到库目录，避开「先到 Downloads 再搬一次」。

用法：

    py -3 tools/_dl_lora.py URL [文件名]
    py -3 tools/_dl_lora.py URL --name foo.safetensors
    py -3 tools/_dl_lora.py --dir          # 只看库在哪、有什么

库目录：`COMFYUI_ROOT/models/loras/anima`（默认 D:/AI/ComfyUI_v037，
可用环境变量 `LORA_LIB_DIR` 直接指到别处）。这个位置 ComfyUI 本来就扫，
子目录名会显示成 `anima/xxx.safetensors`，所以下完不用再动。

Civitai 需要 token 时先设 `CIVITAI_TOKEN`（下载页上的 API Token）。
"""
import io
import os
import sys
import urllib.request

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                              errors="replace")

ROOT = os.getenv("COMFYUI_ROOT", r"D:\AI\ComfyUI_v037")
LIB = os.getenv("LORA_LIB_DIR") or os.path.join(ROOT, "models", "loras", "anima")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def show():
    print("库目录:", LIB)
    if not os.path.isdir(LIB):
        print("（还不存在，第一次下载会自动建）")
        return
    files = sorted(os.listdir(LIB))
    if not files:
        print("（空的）")
        return
    for n in files:
        p = os.path.join(LIB, n)
        if os.path.isfile(p):
            print("  %-52s %8.1f MB" % (n, os.path.getsize(p) / 1048576))


def name_from(url, given):
    if given:
        return given
    base = url.split("?")[0].rstrip("/").split("/")[-1]
    if not base or "." not in base:
        raise SystemExit("拿不到文件名，请显式给一个，例如 foo.safetensors")
    if not base.lower().endswith((".safetensors", ".pt", ".ckpt", ".bin")):
        base += ".safetensors"
    return base


def download(url, name):
    os.makedirs(LIB, exist_ok=True)
    dst = os.path.join(LIB, name)
    if os.path.exists(dst):
        print("已存在，跳过:", dst, "(%.1f MB)" % (os.path.getsize(dst) / 1048576))
        return dst
    part = dst + ".part"
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    tok = os.getenv("CIVITAI_TOKEN")
    if tok:
        req.add_header("Authorization", "Bearer " + tok)
    print("下载:", url)
    with urllib.request.urlopen(req, timeout=60) as r, open(part, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            if total:
                sys.stdout.write("\r  %5.1f%%  %.0f/%.0f MB"
                                 % (done * 100.0 / total, done / 1048576,
                                    total / 1048576))
                sys.stdout.flush()
    print()
    os.replace(part, dst)
    print("完成:", dst, "(%.1f MB)" % (os.path.getsize(dst) / 1048576))
    return dst


def main():
    args = [a for a in sys.argv[1:] if a != "--dir"]
    if not args or "--dir" in sys.argv:
        show()
        if not args:
            return
    url = args[0]
    given = ""
    if "--name" in args:
        given = args[args.index("--name") + 1]
    elif len(args) > 1:
        given = args[1]
    download(url, name_from(url, given))


if __name__ == "__main__":
    main()
