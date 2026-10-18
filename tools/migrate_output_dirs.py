"""一次性：把 output 根目录里的老图按账本归进会话子目录（2026-10-18）。

## 背景

新图从 2026-10-18 起由 `app/comfy_out.py` 直接写进
`output/qq/群_<号>/<渠道>/`，但之前攒下两千多张还平铺在 `output/` 根下。
账本 `state/image_log.jsonl` 里每张图都记了「哪个会话、哪个渠道」，所以能补归。

## 用法

```
python tools/migrate_output_dirs.py                 # 只报告，一个文件都不动
python tools/migrate_output_dirs.py --apply         # 真搬，并写清单
python tools/migrate_output_dirs.py --undo          # 按清单搬回去
```

## 取舍

- **搬不认识的**：账本里查不到的（网页侧出的图、手动跑的图）留在根目录
  ——图名不带任何会话信息，猜一个目录塞进去比留在原地更难查。
- **同名冲突不覆盖**：目标已存在就跳过并计数，宁可少归一张，也不能盖掉别人。
- **写清单**：`state/output_migrate_<日期>.json` 记 `旧位置 → 新位置`，
  `--undo` 就靠它搬回。搬错了能退，这是唯一的安全网。
"""

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import comfy_out                       # noqa: E402
from app.config import COMFYUI_OUTPUT_DIR, state_path   # noqa: E402


def _ledger_map():
    """file → (target, target_id, skill)。同一张图多条记录时以最后一条为准。"""
    path = state_path("image_log.jsonl")
    out = {}
    if not os.path.isfile(path):
        return out
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            name = str(rec.get("file") or "").strip().replace("\\", "/")
            if not name or "/" in name:
                continue            # 已经是相对路径（新图），不参与归档
            target = rec.get("target")
            if target not in ("group", "private"):
                continue
            out[name] = (target, str(rec.get("target_id") or ""),
                         str(rec.get("skill") or ""))
    return out


def _manifest_path():
    return state_path("output_migrate_%s.json"
                      % time.strftime("%Y%m%d-%H%M%S"))


def _plan(root, known):
    """返回 (moves, missing, conflict)，moves 是 [(old_abs, new_abs)]。"""
    moves, missing, conflict = [], [], []
    for name in sorted(os.listdir(root)):
        src = os.path.join(root, name)
        if not os.path.isfile(src):
            continue
        hit = known.get(name)
        if not hit:
            continue                # 查不到主人 → 留在根目录
        target, target_id, skill = hit
        rel_dir = comfy_out.session_base(target, target_id)
        if skill:
            rel_dir = comfy_out._join(rel_dir, skill)
        dst_dir = os.path.join(root, *rel_dir.split("/"))
        dst = os.path.join(dst_dir, name)
        if os.path.exists(dst):
            conflict.append(dst)
        else:
            moves.append((src, dst))
    total = sum(1 for n in os.listdir(root) if os.path.isfile(os.path.join(root, n)))
    missing = max(0, total - len(moves) - len(conflict))
    return moves, missing, conflict


def main(argv=None):
    ap = argparse.ArgumentParser(description="按账本把 output 老图归进会话目录")
    ap.add_argument("--apply", action="store_true", help="真搬（默认只报告）")
    ap.add_argument("--undo", action="store_true", help="按清单搬回去")
    args = ap.parse_args(argv)

    root = (COMFYUI_OUTPUT_DIR or "").strip()
    if not root or not os.path.isdir(root):
        print("COMFYUI_OUTPUT_DIR 没配或目录不存在：%r" % root)
        return 2

    if args.undo:
        return _undo(root)

    known = _ledger_map()
    moves, unknown, conflict = _plan(root, known)
    print("output: %s" % root)
    print("账本认得的老图：%d 张" % len(known))
    print("可归档：%d 张   账本查不到（留原地）：%d 张   目标已存在跳过：%d 张"
          % (len(moves), unknown, len(conflict)))
    for src, dst in moves[:5]:
        print("  %s → %s" % (os.path.basename(src),
                             os.path.relpath(dst, root).replace("\\", "/")))
    if len(moves) > 5:
        print("  …… 共 %d 张" % len(moves))
    if not args.apply:
        print("\n这是预演。确认后加 --apply 真搬。")
        return 0

    done = []
    for src, dst in moves:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        os.replace(src, dst)
        done.append([os.path.relpath(src, root).replace("\\", "/"),
                     os.path.relpath(dst, root).replace("\\", "/")])
    mp = _manifest_path()
    with open(mp, "w", encoding="utf-8") as f:
        json.dump({"root": root, "at": time.time(), "moved": done}, f,
                  ensure_ascii=False, indent=1)
    print("\n已搬 %d 张，清单：%s" % (len(done), mp))
    print("退回去：python tools/migrate_output_dirs.py --undo")
    return 0


def _undo(root):
    files = sorted(n for n in os.listdir(os.path.dirname(
        state_path("x"))) if n.startswith("output_migrate_") and n.endswith(".json"))
    if not files:
        print("找不到归清单，没得退")
        return 1
    mp = state_path(files[-1])
    with open(mp, "r", encoding="utf-8") as f:
        data = json.load(f)
    back = 0
    for old, new in data.get("moved") or []:
        src = os.path.join(root, *new.split("/"))
        dst = os.path.join(root, *old.split("/"))
        if os.path.isfile(src) and not os.path.exists(dst):
            os.replace(src, dst)
            back += 1
    print("已搬回 %d 张（清单 %s）" % (back, os.path.basename(mp)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
