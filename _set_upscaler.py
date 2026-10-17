# -*- coding: utf-8 -*-
"""换某个渠道末尾超分链用的模型（`UpscaleModelLoader.model_name`）。

⚠️ 纯字节级替换，**不** json.loads / json.dump —— 理由同 `_set_canvas.py`：
   用 `json.loads(raw.replace('__SEED__','1'))` 再 dump 回去会把 `__SEED__`
   永久烘成字面量（skills/ 被 gitignore，git diff 看不见）——2026-10-10 §30 的坑。

场里的模型（`D:\\AI\\ComfyUI-backup\\models\\upscale_models\\`）：
  2x_Ani4Kv2_G6i2_Compact_107500.pth   2.3 MB   ← 全渠道通用（22 个 workflow）
  4x-AnimeSharp.pth                     63.9 MB  带锐化

用法：  python _set_upscaler.py 2x_Ani4Kv2_G6i2_Compact_107500.pth
        python _set_upscaler.py 4x-AnimeSharp.pth
"""
import re
import sys

TARGETS = [
    "skills/qwen-charsheet/workflow.json",
    "skills/qwen-charsheet/workflow_i2i.json",
    "skills/qwen-restore/workflow.json",
    "skills/qwen-restore/workflow_i2i.json",
]

PAT = re.compile(
    rb'("class_type"\s*:\s*"UpscaleModelLoader"\s*,\s*"inputs"\s*:\s*\{\s*'
    rb'"model_name"\s*:\s*)("[^"]*")'
)


def patch(path, model):
    with open(path, "rb") as f:
        raw = f.read()
    crlf = raw.count(b"\r\n")
    hits = PAT.findall(raw)
    if len(hits) != 1:
        raise SystemExit("!! %s 匹配到 %d 处 UpscaleModelLoader（应为 1）" % (path, len(hits)))
    old = PAT.search(raw).group(2)
    new = PAT.sub(lambda m: m.group(1) + b'"' + model.encode() + b'"', raw)
    with open(path, "wb") as f:
        f.write(new)
    assert b"__SEED__" in new, "%s 里 __SEED__ 丢了！" % path
    print("  %-46s %s -> %s   CRLF=%d  __SEED__=OK"
          % (path, old.decode(), model, crlf))


def main():
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    model = sys.argv[1]
    print("[超分模型] -> %s" % model)
    for p in TARGETS:
        patch(p, model)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
