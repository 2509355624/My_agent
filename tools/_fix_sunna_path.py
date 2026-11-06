# -*- coding: utf-8 -*-
"""把 sunna 的文件名改成 ComfyUI 实际列表里的写法（Windows 单反斜杠）。"""
import io
import json
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                              errors="replace")

P = "skills/anima-line/char_loras.json"
BS = chr(92)                      # 单个反斜杠，避免转义歧义
NAME = "anima" + BS + "sunna-anima_20260216_2230.safetensors"

d = json.load(open(P, encoding="utf-8"))
for c in d["characters"]:
    if c.get("key") == "sunna":
        c["file"] = NAME
        print("写入:", repr(c["file"]))
json.dump(d, open(P, "w", encoding="utf-8", newline="\n"),
          ensure_ascii=False, indent=2)
print("回读:", repr(json.load(open(P, encoding="utf-8"))["characters"][0]["file"]))
