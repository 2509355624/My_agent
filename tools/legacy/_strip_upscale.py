# -*- coding: utf-8 -*-
"""把末尾超分链整段拆掉：SaveImage 改回直接吃 VAEDecode 的输出。

纯文本操作（不 json.loads / json.dump），只做两件事：
  1. SaveImage 的 `"images": ["14", 0]` → `["10", 0]`（VAEDecode）
  2. 删掉尾部连续的 12/13/14 三个节点
行尾（CRLF/LF）与 `__SEED__` 原样保留。

用法：  python _strip_upscale.py
"""
import re
import sys

TARGETS = [
    "skills/qwen-charsheet/workflow.json",
    "skills/qwen-charsheet/workflow_i2i.json",
    "skills/qwen-restore/workflow.json",
    "skills/qwen-restore/workflow_i2i.json",
]

LINK = re.compile(rb'("images"\s*:\s*\[\s*)"1[24]"')


def strip(path):
    with open(path, "rb") as f:
        raw = f.read()
    crlf = b"\r\n" in raw
    nl = b"\r\n" if crlf else b"\n"

    # 1) SaveImage 改吃 VAEDecode
    if LINK.search(raw) is None:
        raise SystemExit("!! %s 找不到 SaveImage 的 images 链接" % path)
    old_link = LINK.search(raw).group(0)
    raw = LINK.sub(lambda m: m.group(1) + b'"10"', raw)

    # 2) 删掉尾部 12/13/14（连带 node 11 后面那个逗号）
    anchor = nl + b'  "12": {'
    idx = raw.find(anchor)
    if idx < 0:
        raise SystemExit("!! %s 找不到尾部节点 12" % path)
    head = raw[:idx].rstrip()
    if head.endswith(b","):
        head = head[:-1]
    tail_old = raw[idx:]
    raw = head + nl + b"}" + nl

    with open(path, "wb") as f:
        f.write(raw)
    assert b"__SEED__" in raw, "%s 里 __SEED__ 丢了！" % path
    for tok in (b"UpscaleModelLoader", b"SplitImageWithAlpha", b"ImageUpscaleWithModel"):
        assert tok not in raw, "%s 里还残留 %s" % (path, tok.decode())
    print("  %-46s %s -> \"10\"   删掉 %d 字节（12/13/14）  CRLF=%d  __SEED__=OK"
          % (path, old_link.decode().replace("\n", "").replace("  ", " "), len(tail_old), crlf))


def main():
    for p in TARGETS:
        strip(p)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
