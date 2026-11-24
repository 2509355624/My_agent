"""一次性：把「不是站主自己的」那几棵图树挪进 `工作台/`（2026-11-24）。

## 背景

2026-11-24 用户拍板：`D:\\AI\\ComfyUI-nightly\\output\\web` 是他**自己用的**
文件夹，群友的东西一律进 `工作台/`。代码那几处已经改好了（见
`comfy_out.session_base`、`comic_quota.output_base`、`moss_api.OUT_ROOT_FRIEND`、
`friend_api` 的 out_dir），但**已经画好在旧目录里的图不会自己走动**——
画廊和漫画台就不认它们了，群里那几本会看起来「全没了」。

## 挪什么、不挪什么

| 旧位置                          | 新位置                              | 是谁的 |
|---------------------------------|-------------------------------------|--------|
| `output/群友漫画台/<QQ>/<作品>`  | `output/工作台/群友漫画台/<QQ>/<作品>` | 群友漫画台 |
| `output/群友moss漫画/<任务>`     | `output/工作台/群友漫画台/moss/<任务>` | 群友 moss |
| `output/群友页_<QQ>`             | `output/工作台/群友页_<QQ>`         | 群友网页生图 |

`output/web/` **一动不动**——那是站主自己的（跑图台、漫画台、moss 台出的图）。

## 用法

```
python tools/migrate_friend_output.py            # 只报告，一个文件都不动
python tools/migrate_friend_output.py --apply    # 真搬，并写清单
python tools/migrate_friend_output.py --undo     # 按清单搬回去
```

## 取舍

- **整目录搬**（`shutil.move`），不是逐个文件：这几棵树内部结构一模一样，
  整体搬不会撞名，也不存在「搬了一半」的中间态。
- **目标已存在就跳过**并报出来，绝不合并、绝不覆盖——同名目录大概率是
  新代码已经在那儿出过图了，合并会把两本书混成一堆。
- **写清单**：`state/friend_output_migrate_<日期>.json` 记 `旧位置 → 新位置`，
  `--undo` 就靠它搬回。搬错了能退。
- 跑之前**把 5174 停掉**（或者在没人跑图的时候跑）：出图线程正往老目录里写
  的时候搬它，那一张会丢。
"""

import argparse
import json
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import COMFYUI_OUTPUT_DIR, state_path   # noqa: E402

# (旧位置, 新位置) —— 相对 output 根的完整路径，一对一。
# `群友moss漫画` 是白天那版给 moss 单独起的根，并进 `工作台/群友漫画台/moss`。
RULES = (
    ("群友漫画台", "工作台/群友漫画台"),
    ("群友moss漫画", "工作台/群友漫画台/moss"),
    ("群友页_*", "工作台"),          # 通配：每个群友的目录各算一条，同名落进工作台
)


def _abs(rel):
    return os.path.join(COMFYUI_OUTPUT_DIR, *rel.replace("/", os.sep).split("/"))


def _plan():
    """列出该搬的 (旧绝对路径, 新绝对路径)；只读磁盘，不改任何东西。"""
    root = (COMFYUI_OUTPUT_DIR or "").strip()
    if not root or not os.path.isdir(root):
        return [], "没配 COMFYUI_OUTPUT_DIR（或目录不存在）：%r" % (root,)
    todo = []
    for old, new in RULES:
        if old.endswith("*"):
            prefix = old[:-1]
            try:
                names = sorted(os.listdir(root))
            except OSError as exc:
                return [], str(exc)
            for n in names:
                if n.startswith(prefix):
                    todo.append((_abs(n), _abs(new + "/" + n)))
            continue
        src = _abs(old)
        if os.path.isdir(src):
            todo.append((src, _abs(new)))
    return todo, ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真搬")
    ap.add_argument("--undo", action="store_true", help="按清单搬回去")
    args = ap.parse_args()

    if args.undo:
        return _undo()

    todo, err = _plan()
    if err:
        print(err)
        return 2
    if not todo:
        print("没有需要搬的目录（已经是新结构了）。")
        return 0

    moved, skipped = [], []
    for src, dst in todo:
        rel_src = os.path.relpath(src, COMFYUI_OUTPUT_DIR)
        if os.path.exists(dst):
            skipped.append(rel_src)
            print("跳过（目标已存在）：%s" % rel_src)
            continue
        print("%s → %s" % (rel_src, os.path.relpath(dst, COMFYUI_OUTPUT_DIR)))
        moved.append((src, dst))

    if not args.apply:
        print("\n只报告，什么都没动。加 --apply 才会真搬（共 %d 个，跳过 %d 个）。"
              % (len(moved), len(skipped)))
        return 0

    os.makedirs(os.path.dirname(moved[0][1]), exist_ok=True)
    done = []
    for src, dst in moved:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.move(src, dst)
        done.append((src, dst))
    log = state_path("friend_output_migrate_%s.json"
                     % time.strftime("%Y%m%d_%H%M%S"))
    with open(log, "w", encoding="utf-8") as f:
        json.dump([{"from": a, "to": b} for a, b in done], f,
                  ensure_ascii=False, indent=2)
    print("\n搬完 %d 个，清单：%s" % (len(done), log))
    print("搬错了跑 --undo 退回去；画廊要重新扫一次才认新目录。")
    return 0


def _undo():
    logs = []
    d = os.path.dirname(state_path("x"))
    try:
        logs = sorted(n for n in os.listdir(d)
                      if n.startswith("friend_output_migrate_"))
    except OSError:
        pass
    if not logs:
        print("没有找到迁移清单，没得退。")
        return 1
    last = os.path.join(d, logs[-1])
    with open(last, encoding="utf-8") as f:
        items = json.load(f)
    n = 0
    for it in reversed(items):
        src, dst = it["to"], it["from"]
        if not os.path.isdir(src) or os.path.exists(dst):
            print("跳过：%s" % src)
            continue
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.move(src, dst)
        n += 1
    print("按 %s 退回 %d 个。" % (os.path.basename(last), n))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
