# -*- coding: utf-8 -*-
"""把旧项目里群友上传的作品集迁到当前项目。

为什么不能只搬库：`works.path` 和 `items.path` 存的是**绝对路径**，
库里那行搬到新库、文件却还躺在旧项目目录下，画廊取图就是一片 404。
所以这件事必须两半一起做：文件复制到新项目的 `state/gallery_upload/`，
库里的路径同步改写成新位置。

迁什么：
- `src='user'` 的作品集（站主磁盘扫描出来的 owner 作品由扫描器自己重建，
  搬过来只会和现有 370 本重复）
- 每个作品集的 items（顺序、字节数原样保留）
- 点赞/收藏（`marks`）**不搬**：旧库里这 19 条全挂在 owner 的作品上，
  迁过来 item id 对不上，搬了就是给别人的图点错赞。

幂等：按 `works.key`（`user:<qq>/<旧id>`）判重，跑第二遍只打印「已存在」。

    python tools/migrate_gallery_upload.py                      # 只算不写
    python tools/migrate_gallery_upload.py --apply               # 复制文件 + 写库
    python tools/migrate_gallery_upload.py --apply --warm        # 顺便把缩略图先生成好
"""
import argparse
import os
import shutil
import sqlite3
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app.features.gallery import gallery as G  # noqa: E402


def _ro(db):
    con = sqlite3.connect("file:" + db.replace("\\", "/") + "?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--old", default=r"D:\AI\agent_my_test",
                    help="旧项目根目录（含 state/gallery.db）")
    ap.add_argument("--apply", action="store_true", help="真写：复制文件 + 插库")
    ap.add_argument("--warm", action="store_true", help="顺便把缩略图先生成好")
    a = ap.parse_args()

    old_db = os.path.join(a.old, "state", "gallery.db")
    old_up = os.path.join(a.old, "state", "gallery_upload")
    if not os.path.isfile(old_db):
        raise SystemExit("旧库不存在：%s" % old_db)

    G.init()
    old = _ro(old_db)
    works = old.execute("SELECT * FROM works WHERE src='user' ORDER BY id").fetchall()
    print("旧库群友作品集 %d 个，上传目录 %s" % (len(works), old_up))
    print("新库上传目录 %s" % G.UPLOAD)

    with G.connect() as con:
        have = {r["key"] for r in con.execute("SELECT key FROM works")}

    todo, skip, total_imgs = [], [], 0
    for w in works:
        items = old.execute("SELECT * FROM items WHERE work_id=? ORDER BY idx",
                            (w["id"],)).fetchall()
        if w["key"] in have:
            skip.append((w["key"], w["title"], len(items)))
            continue
        todo.append((w, items))
        total_imgs += len(items)

    for k, t, n in skip:
        print("  已存在，跳过：%s  %s  %d 张" % (k, t, n))
    print("\n待迁 %d 个作品集 / %d 张图" % (len(todo), total_imgs))
    for w, items in todo:
        print("  %-24s %-16s %3d 张  %s" % (w["key"], w["title"], len(items), w["path"]))

    if not a.apply:
        print("\n（只算不写。加 --apply 才复制文件并写库）")
        return 0

    os.makedirs(G.UPLOAD, exist_ok=True)
    new_ids = []
    done_imgs = 0
    for w, items in todo:
        src_dir = w["path"]
        try:
            rel = os.path.relpath(src_dir, old_up)
        except ValueError:
            rel = os.path.join(str(w["owner"]), str(w["id"]))
        if rel.startswith(".."):
            rel = os.path.join(str(w["owner"]), str(w["id"]))
        dst_dir = os.path.join(G.UPLOAD, rel)

        if os.path.isdir(src_dir):
            shutil.copytree(src_dir, dst_dir, dirs_exist_ok=True)
        else:
            print("  ⚠ 源目录不在，跳过：%s" % src_dir)
            continue

        rows = []
        for it in items:
            name = os.path.basename(it["path"])
            p = os.path.join(dst_dir, name)
            if not os.path.isfile(p):
                continue
            rows.append((len(rows), p, it["bytes"] or 0))
        if not rows:
            print("  ⚠ 复制后一张都没找到，跳过：%s" % src_dir)
            continue

        cover = rows[0][1]
        now = time.time()
        with G.connect() as con:
            cur = con.execute(
                "INSERT INTO works(key, src, owner, grp, title, path, cover, n,"
                " hidden, fp, sort, ord, ts, seen)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (w["key"], "user", str(w["owner"]), w["grp"] or "群友",
                 w["title"] or "未命名作品", dst_dir, cover, len(rows),
                 int(w["hidden"] or 0), w["fp"] or "", int(w["sort"] or 0),
                 w["ord"] or "", w["ts"] or now, now))
            wid = cur.lastrowid
            con.executemany(
                "INSERT INTO items(work_id, idx, path, bytes) VALUES(?,?,?,?)",
                [(wid, i, p, b) for i, p, b in rows])
        new_ids.append(wid)
        done_imgs += len(rows)
        print("  ✓ %-18s → #%d  %2d 张  %s" % (w["title"], wid, len(rows), dst_dir))

    print("\n完成：%d 个作品集 / %d 张图" % (len(new_ids), done_imgs))

    if a.warm and new_ids:
        print("开始预生成缩略图（%d 张，几十秒）…" % done_imgs)
        with G.connect() as con:
            marks = ",".join("?" * len(new_ids))
            paths = [r["path"] for r in con.execute(
                "SELECT path FROM items WHERE work_id IN (%s)" % marks, new_ids)]
        t0 = time.time()
        for i, p in enumerate(paths, 1):
            G.thumb(p, "t")
            if i % 40 == 0:
                print("  %d/%d  %.0fs" % (i, len(paths), time.time() - t0))
        print("缩略图完成，%.1fs" % (time.time() - t0))

    old.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
