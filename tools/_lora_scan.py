# -*- coding: utf-8 -*-
"""P1-a：盘点 LoRA 目录，和索引比对，列出「待补 / 缺失 / 没触发词」。

只读，不改任何文件。输出一张表，人工决定要不要补、怎么补。
"""
import io
import json
import os
import re
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                              errors="replace")

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LORA_DIR = r"D:\AI\ComfyUI_v037\models\loras\anima"
INDEX = os.path.join(BASE, "config", "char_loras.json")


def index_entries():
    try:
        d = json.load(open(INDEX, encoding="utf-8"))
        return {str(c.get("file") or "").replace("/", "\\"): c
                for c in (d.get("characters") or [])}
    except Exception:                                  # noqa: BLE001
        return {}


def scan():
    rows = []
    for root, _dirs, files in os.walk(LORA_DIR):
        for fn in files:
            if not fn.lower().endswith(".safetensors"):
                continue
            p = os.path.join(root, fn)
            rel = os.path.relpath(p, os.path.dirname(LORA_DIR))
            stem = os.path.splitext(fn)[0]
            txt = os.path.join(root, stem + ".txt")
            png = os.path.join(root, stem + ".png")
            trig = ""
            if os.path.isfile(txt):
                try:
                    lines = [x.strip() for x in
                             open(txt, encoding="utf-8").read().splitlines()
                             if x.strip()]
                    trig = lines[0] if lines else ""
                except Exception:                      # noqa: BLE001
                    trig = ""
            rows.append({
                "rel": rel,
                "dir": os.path.relpath(root, LORA_DIR).replace(".", "根目录"),
                "mb": round(os.path.getsize(p) / 1048576, 1),
                "txt": bool(os.path.isfile(txt)),
                "png": bool(os.path.isfile(png)),
                "trigger": trig,
            })
    return rows


def main():
    idx = index_entries()
    rows = scan()
    print("目录:", LORA_DIR)
    print("LoRA 文件:", len(rows), "| 索引条目:", len(idx))
    print()

    have, miss = [], []
    for r in rows:
        key = r["rel"].replace("/", "\\")
        (have if key in idx else miss).append(r)

    print("== 已进索引 (%d) ==" % len(have))
    for r in have:
        print("  %-42s %5.1fMB" % (r["rel"], r["mb"]))

    print("\n== 待补条目 (%d) ==" % len(miss))
    miss.sort(key=lambda x: (x["dir"], x["rel"]))
    cur = None
    for r in miss:
        if r["dir"] != cur:
            cur = r["dir"]
            print("  [%s]" % cur)
        print("    %-44s %5.1fMB  txt=%-5s png=%-5s 触发词=%s"
              % (os.path.basename(r["rel"]), r["mb"], r["txt"], r["png"],
                 r["trigger"] or "—"))

    ghost = [k for k in idx if k not in {r["rel"].replace("/", "\\")
                                         for r in rows}]
    print("\n== 索引里有、磁盘上没有 (%d) ==" % len(ghost))
    for g in ghost:
        print("  ", g)

    notrig = [r for r in rows if not r["txt"]]
    print("\n== 缺 .txt 触发词 (%d) ==" % len(notrig))
    for r in notrig:
        print("  ", r["rel"])

    # 文件名像不像角色名：纯 hash / 带 step / 带版本号的最难认
    odd = [r for r in rows
           if re.search(r"(step|epoch|_v\d|\d{6,}|lora_?\d)", r["rel"], re.I)]
    print("\n== 文件名带训练步数/版本号，建议规范化 (%d) ==" % len(odd))
    for r in odd:
        print("  ", r["rel"])


if __name__ == "__main__":
    main()
