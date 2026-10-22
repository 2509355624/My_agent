"""把本地文件夹里的表情包批量导入某个 agent 的表情包库。

为什么需要它：机器人**只在「被 @ / 命中唤醒词」的那一轮**才收图
（收藏点见 `qq_bot._run_turn`，而没被触发的消息在 `_dispatch` 里就 return 了）。
所以「把图发进群，它自己会捡」这句话是不成立的——每张都得配一句 @。
手上已经有几百张图的人，用这个脚本一次导完。

用法（在项目根，用项目自己的 venv）：

    # 先只看会导入哪些、哪些会被挡掉，不落盘
    python tools/import_stickers.py D:\\我的表情包 --dry-run

    # 真导入到 agents/qq/stickers/
    python tools/import_stickers.py D:\\我的表情包

    # 没有识图 Key 的机器：跳过打标签（快很多，但模型挑图只能靠编号）
    python tools/import_stickers.py D:\\我的表情包 --no-tag

    # 导入到别的 agent
    python tools/import_stickers.py D:\\我的表情包 --agent main

判定规则和线上完全一样（直接调 `app.stickers`，不另写一份）：
  · GIF 一律算表情包；
  · 静态图最长边 ≤ STICKER_MAX_EDGE(640) 才算，群相册那种照片会被挡；
  · 库上限 STICKER_LIMIT(50) 张，满了就停，不覆盖已有的。
"""

import argparse
import base64
import hashlib
import os
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
if os.path.basename(ROOT) == "tools":      # 也允许直接放在项目根跑
    ROOT = os.path.dirname(ROOT)
sys.path.insert(0, ROOT)

from app import stickers  # noqa: E402

EXTS = (".gif", ".png", ".jpg", ".jpeg", ".webp")
MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
        ".gif": "image/gif", ".webp": "image/webp"}


def _live_count(agent_id):
    return len([r for r in stickers._live(stickers._load_index(agent_id))])  # noqa: SLF001


def import_dir(src, agent_id, dry_run=False, tag=True):
    d = stickers._dir(agent_id)          # noqa: SLF001
    os.makedirs(d, exist_ok=True)
    index = stickers._load_index(agent_id)   # noqa: SLF001
    have_md5 = {r.get("md5") for r in index}
    n_ok = n_dup = n_skip = n_full = 0
    added = []

    files = sorted(f for f in os.listdir(src)
                   if os.path.splitext(f)[1].lower() in EXTS)
    for name in files:
        path = os.path.join(src, name)
        try:
            with open(path, "rb") as f:
                raw = f.read()
        except OSError as exc:
            print("  跳过（读不出）%s：%s" % (name, exc))
            n_skip += 1
            continue
        ok, w, h = stickers._is_sticker(raw)   # noqa: SLF001
        if not ok:
            print("  跳过（太大，%dx%d > 640）%s" % (w, h, name))
            n_skip += 1
            continue
        md5 = hashlib.md5(raw).hexdigest()
        if md5 in have_md5:
            print("  跳过（库里已有）%s" % name)
            n_dup += 1
            continue
        if _live_count(agent_id) >= stickers.STICKER_LIMIT:
            print("  库已满（%d 张），剩下的没导" % stickers.STICKER_LIMIT)
            n_full += 1
            break

        ext = os.path.splitext(name)[1].lower()
        out_name = md5[:8] + (".jpg" if ext in (".jpg", ".jpeg") else ext)
        desc, tags = "", []
        if tag:
            data_url = "data:%s;base64,%s" % (
                MIME.get(ext, "image/png"), base64.b64encode(raw).decode())
            desc, tags = stickers._tag(data_url)   # noqa: SLF001

        rec = {"md5": md5, "file": out_name, "desc": desc, "tags": tags,
               "sender": "本地导入", "t": int(time.time()), "url": "",
               "w": w, "h": h, "deleted": False}
        print("  + %s  %s %s" % (name, desc or "（无描述）",
                                 "/".join(tags[:3])))
        if not dry_run:
            with open(os.path.join(d, out_name), "wb") as f:
                f.write(raw)
            index.append(rec)
            have_md5.add(md5)
        added.append(rec)
        n_ok += 1

    if added and not dry_run:
        stickers._write_index(agent_id, index)   # noqa: SLF001
    return {"added": n_ok, "dup": n_dup, "skip": n_skip, "full": n_full,
            "dir": d}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src", help="表情包所在文件夹")
    ap.add_argument("--agent", default="qq", help="导入到哪个 agent（默认 qq）")
    ap.add_argument("--dry-run", action="store_true", help="只看不写")
    ap.add_argument("--no-tag", action="store_true",
                    help="跳过识图打标签（没有识图 Key 时用）")
    args = ap.parse_args()

    if not os.path.isdir(args.src):
        raise SystemExit("找不到文件夹：%s" % args.src)
    print("来源：%s" % os.path.abspath(args.src))
    print("目标：agents/%s/stickers/（当前 %d 张，上限 %d）"
          % (args.agent, _live_count(args.agent), stickers.STICKER_LIMIT))
    if args.dry_run:
        print("— dry run，不写任何文件 —")
    r = import_dir(args.src, args.agent, args.dry_run, tag=not args.no_tag)
    print("\n新增 %d · 重复 %d · 跳过 %d · 因满未处理 %d"
          % (r["added"], r["dup"], r["skip"], r["full"]))
    if args.dry_run:
        print("（dry run：以上都没有真正写入）")
    else:
        print("落盘：%s" % r["dir"])
        print("让机器人重新读一遍库：重启机器人进程即可（索引是每次现读的）。")


if __name__ == "__main__":
    main()
