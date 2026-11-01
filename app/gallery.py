# -*- coding: utf-8 -*-
"""画廊的索引层（挂在 5174 主进程里）。

## 只做映射，不做存储

站主那 60G 图本来就躺在磁盘上，画廊只登记**路径**，一个字节都不复制。
所以「作品集」不是一个新概念——`Pictures\\new\\35` 这个目录本来就是一个
作品集，扫描器只是把它翻译成数据库里的一行。

群友上传是另一回事：文件确实要落一份，但落的是压过的 1600px WebP
（一张 150KB 左右），不是原图。两种来源在库里是同两张表，只在 `src` 上
分叉，前端完全不用关心图到底在站主盘上还是在 `state/gallery_upload/` 里。

## 扫描规则只有一句话

**目录里直接有图，这个目录就是作品集；只有子目录，它就是合集入口。**

所以 `new\\35`（578 张平铺）是一个作品集；`小漫画\\silver\\便利店` 自己
有 457 张、底下还有 3 个角色目录，那它同时是作品集和合集——两边都成立，
前端分两块显示即可。不做「猜哪个是主」这种判断。

## 缩略图怎么来

13285 张全量转一遍要 1~4 小时，所以不会在请求里现算一整墙。取图时缺的
当场补（每张 0.1~0.3 秒），另外**扫描/上传之后挂一个后台线程把缺的补完**：
站主扫完一万张就去干别的了，等群友点进来时缓存已经备好。

后台线程**队列空了就退出**，不是常驻——没人访问的时候不该有东西在跑。

## 磁盘上的图被删了怎么办

不处理。画廊读的是「登记过的路径」，文件没了 `img` 接口返回 404，前端
退回灰底占位。**绝不因为文件消失就把作品集删掉**——站主会周期性手工删图，
一删就自动清库的话，画廊会自己慢慢变成空架子，和现在那个历史页一个下场。
"""

import contextlib
import hashlib
import logging
import os
import re
import shutil
import sqlite3
import threading
import time
from collections import deque

from PIL import Image

from app.config import GALLERY_ROOTS, state_path

log = logging.getLogger("gallery")

DB = state_path("gallery.db")
CACHE = state_path("gallery_cache")
UPLOAD = state_path("gallery_upload")

IMG_EXT = (".png", ".jpg", ".jpeg", ".webp", ".bmp")

# 一个作品集最多存几张。群友会分几次往同一个作品集里加图（一次传不完
# 十张就先传一批），所以上限得管在「作品集」这一层，而不是只管一次请求。
WORK_MAX = 100


class WorkFull(Exception):
    """这个作品集已经到上限，不能再加图。"""

# 只跳过明确的系统垃圾目录，不做「名字像缓存就跳过」这种猜测——
# 站主自己的文件夹爱叫什么就叫什么。
SKIP_DIRS = {"@eadir", "$recycle.bin", "system volume information",
             ".thumbnails", ".git", "__pycache__"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS works (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    key     TEXT UNIQUE,          -- 稳定标识：owner:<绝对路径> / user:<qq>/<slug>
    src     TEXT NOT NULL,        -- owner / user
    owner   TEXT NOT NULL,        -- 站主为 ''，群友为 QQ 号
    grp     TEXT DEFAULT '',      -- 来源分组（根目录显示名）
    title   TEXT DEFAULT '',
    path    TEXT DEFAULT '',      -- 目录绝对路径
    cover   TEXT DEFAULT '',      -- 封面图绝对路径
    n       INTEGER DEFAULT 0,
    hidden  INTEGER DEFAULT 0,    -- 站主下架
    fp      TEXT DEFAULT '',      -- 扫描指纹，没变就不重写 items
    sort    INTEGER DEFAULT 0,    -- 发现顺序，扫描时按自然序赋，翻页靠它
    ts      REAL DEFAULT 0,
    seen    REAL DEFAULT 0        -- 最近一次被扫描看到的时间
);
CREATE TABLE IF NOT EXISTS items (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    work_id INTEGER NOT NULL,
    idx     INTEGER NOT NULL,
    path    TEXT NOT NULL,
    bytes   INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_items_work ON items(work_id, idx);
CREATE TABLE IF NOT EXISTS marks (
    qq      TEXT NOT NULL,
    item_id INTEGER NOT NULL,
    kind    TEXT NOT NULL,        -- like / fav
    ts      REAL DEFAULT 0,
    UNIQUE(qq, item_id, kind)
);
CREATE INDEX IF NOT EXISTS idx_marks_item ON marks(item_id, kind);
"""

_NUM = re.compile(r"(\d+)")

_LOCKS = {}
_LOCKS_LOCK = threading.Lock()


# ─── 小工具 ──────────────────────────────────────────────

def _natkey(s):
    """自然排序：`2` 排在 `10` 前面，`P01` 排在 `P10` 前面。"""
    out = []
    for part in _NUM.split(str(s)):
        if not part:
            continue
        out.append((0, int(part)) if part.isdigit() else (1, part.lower()))
    return out


@contextlib.contextmanager
def connect():
    """一次连接一个事务。用 generator 而不是裸 Connection：`with con:` 只提交
    **不关闭**，扫描 + 缩略图每秒几十次读写，漏连接迟早把句柄吃光。

    调用方永远这样用：`with gallery.connect() as con:`  —— 正常退出提交，
    抛异常回滚，两种都关连接。
    """
    con = sqlite3.connect(DB, timeout=15)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def init():
    """建库建表。幂等，进程启动和每次扫描前都可以调。"""
    os.makedirs(os.path.dirname(DB) or ".", exist_ok=True)
    with connect() as con:
        con.executescript(SCHEMA)
        con.execute("PRAGMA journal_mode=WAL")
    return DB


def _lock_for(key):
    with _LOCKS_LOCK:
        lk = _LOCKS.get(key)
        if lk is None:
            lk = _LOCKS[key] = threading.Lock()
        return lk


def safe_inside(base, path):
    """path 是否确实在 base 里面（防止 `..` 和盘符跳跃读到不该读的）。"""
    try:
        b = os.path.abspath(base)
        p = os.path.abspath(path)
    except (TypeError, ValueError):
        return False
    return p == b or p.startswith(b + os.sep)


# ─── 扫描（站主磁盘）──────────────────────────────────────

def _drop_noise_tail(found, min_count=5):
    """把「几乎每个作品集都多出来的同一层目录名」从标题里去掉。

    ComfyUI 的漫画输出是「任务名/moss-qunyou/图」，67 个任务底下都顶着
    同一个 `moss-qunyou`——它不是内容，是导出时固定塞进来的渠道目录，
    挂在墙上纯噪音。

    只在**够多作品集共享同一层**时才动手。一两个目录重名很可能真是内容
    （`角色A/便利店`、`角色B/便利店`），抹掉就把信息弄没了；而 67 个里
    有 67 个都叫这个名字，那它就是结构，不是内容。
    """
    tails = {}
    multi = 0
    for _grp, title, _p, _f in found:
        if "/" in title:
            multi += 1
            tail = title.rsplit("/", 1)[1]
            tails[tail] = tails.get(tail, 0) + 1
    if not multi:
        return found
    noisy = {t for t, n in tails.items()
             if n >= min_count and n >= multi * 0.8}
    if not noisy:
        return found

    out = []
    for grp, title, path, files in found:
        if "/" in title:
            head, _sep, tail = title.rpartition("/")
            if head and tail in noisy:
                title = head
        out.append((grp, title, path, files))
    return out


def _title_for(root, cur, root_title):
    """作品集在墙上的名字。

    扫的是配置根 → 用分组名；站主临时选的子目录 → 用它自己的名字
    （不然选 `new\\35` 会冒出一个叫「图库」的作品集）；再往下取相对路径。
    """
    rel = os.path.relpath(cur, root)
    if rel == ".":
        return root_title or os.path.basename(os.path.abspath(cur))
    return rel.replace(os.sep, "/")


def _walk(grp, root, cur, out, depth=0, root_title=None):
    try:
        entries = list(os.scandir(cur))
    except OSError:
        return
    files, dirs = [], []
    for e in entries:
        name = e.name
        if name.startswith("."):
            continue
        try:
            if e.is_dir(follow_symlinks=False):
                if name.lower() in SKIP_DIRS:
                    continue
                dirs.append(e)
            elif e.is_file(follow_symlinks=False) and name.lower().endswith(IMG_EXT):
                files.append(e)
        except OSError:
            continue
    if files:
        files.sort(key=lambda x: _natkey(x.name))
        out.append((grp, _title_for(root, cur, root_title),
                    os.path.abspath(cur),
                    [(f.name, _size(f)) for f in files]))
    if depth >= 8:
        return
    for d in sorted(dirs, key=lambda x: _natkey(x.name)):
        _walk(grp, root, d.path, out, depth + 1, root_title)


def _size(entry):
    try:
        return entry.stat(follow_symlinks=False).st_size
    except OSError:
        return 0


def _fingerprint(files):
    """张数 + 总字节 + 最新一张的名字。改名、增删、换图都会变，纯读目录。"""
    newest = max((f[0] for f in files), key=_natkey, default="")
    return "%d|%d|%s" % (len(files), sum(s for _n, s in files), newest)


def scan(roots=None):
    """扫站主目录，登记所有「直接含图」的目录。

    roots 的元素是 `(分组名, 绝对路径)`，或者 `(分组名, 绝对路径, 根标题)`——
    第三项只在「根目录自己也直接放图」时决定那个作品集叫什么。

    → {"works": 新增+更新的作品集数, "items": 图片数, "skipped": 没变的目录数,
       "seconds": 耗时}
    """
    roots = tuple(roots if roots is not None else GALLERY_ROOTS)
    if not roots:
        return {"works": 0, "items": 0, "skipped": 0, "seconds": 0.0,
                "error": "没有配置 GALLERY_ROOTS"}

    t0 = time.time()
    found = []
    for item in roots:
        grp, root = item[0], item[1]
        root_title = item[2] if len(item) > 2 else grp
        if not os.path.isdir(root):
            log.warning("画廊根目录不存在：%s", root)
            continue
        _walk(grp, root, root, found, 0, root_title)
    found = _drop_noise_tail(found)

    now = time.time()
    added = changed = skipped = total_items = 0
    with connect() as con:
        con.execute("PRAGMA journal_mode=WAL")
        old = {r["key"]: r for r in con.execute(
            "SELECT key, id, fp FROM works WHERE src='owner'")}
        seen_keys = set()
        # sort 只决定墙上的先后顺序。已有作品集**绝不重排**——站主单独选一个
        # 子目录扫一遍，不该让它从「图库 · 35」瞬移到整个墙的最前面。
        # 新发现的才排到末尾。
        row = con.execute("SELECT MAX(sort) m FROM works").fetchone()
        next_sort = (row["m"] or 0) + 1

        for grp, title, abspath, files in found:
            key = "owner:" + abspath.lower()
            seen_keys.add(key)
            fp = _fingerprint(files)
            row = old.get(key)
            if row and row["fp"] == fp:
                skipped += 1
                con.execute("UPDATE works SET seen=? WHERE id=?",
                            (now, row["id"]))
                total_items += len(files)
                continue

            cover = os.path.join(abspath, files[0][0])
            if row:
                con.execute(
                    "UPDATE works SET grp=?, title=?, path=?, cover=?, n=?, "
                    "fp=?, seen=? WHERE id=?",
                    (grp, title, abspath, cover, len(files), fp, now,
                     row["id"]))
                wid = row["id"]
                con.execute("DELETE FROM items WHERE work_id=?", (wid,))
                changed += 1
            else:
                cur = con.execute(
                    "INSERT INTO works(key, src, owner, grp, title, path, cover,"
                    " n, fp, sort, ts, seen) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (key, "owner", "", grp, title, abspath, cover, len(files),
                     fp, next_sort, now, now))
                wid = cur.lastrowid
                next_sort += 1
                added += 1

            con.executemany(
                "INSERT INTO items(work_id, idx, path, bytes) VALUES(?,?,?,?)",
                [(wid, i, os.path.join(abspath, name), size)
                 for i, (name, size) in enumerate(files)])
            total_items += len(files)

        # 磁盘上已经没有的目录：只标记，不删。站主手删是常态，删库会让
        # 收藏和点赞一起没掉；等他自己在管理页点清理。
        gone = [k for k in old if k not in seen_keys]
        if gone:
            log.info("画廊：有 %d 个作品集的目录不见了（保留记录）", len(gone))

    return {"works": added + changed, "added": added, "changed": changed,
            "skipped": skipped, "items": total_items,
            "seconds": round(time.time() - t0, 1)}


def grp_for(path):
    """扫描单个目录时该归到哪个分组。

    落在已配置的根里面就沿用它原来的分组名（选 `new\\35` 还是归「图库」），
    否则用目录自己的名字。不这么做的话，站主随便选一个子目录扫一遍，
    就会把那个作品集的分组从「图库」改成「35」，墙上凭空多出一个分组。
    """
    p = os.path.abspath(path)
    for grp, root in GALLERY_ROOTS:
        if safe_inside(root, p):
            return grp
    name = os.path.basename(p.rstrip("\\/"))
    return name or p


def scan_path(path):
    """只扫站主指定的那一个目录（含子目录）。站主在网页上自己挑的路径。"""
    p = os.path.abspath(str(path or "").strip())
    if not os.path.isdir(p):
        return {"error": "目录不存在：%s" % p}
    name = os.path.basename(p.rstrip("\\/")) or p
    out = scan([(grp_for(p), p, name)])
    out["path"] = p
    return out


def browse(path=""):
    """列一层目录，给站主点选要扫描的文件夹。

    和扫描一样只读元数据：`scandir` 一层，不递归、不开图。每个子目录后面
    带一个「直接含多少张图」，站主一眼就能认出哪个是他要的作品集目录。

    `path` 为空时给「已经配置好的根」**加**盘符列表。以前只给根，结果配
    完根之后站主就被锁在那两个目录里了——ComfyUI 的输出目录在别的盘，
    想扫一下得先去改 .env 再重启，这没道理。
    """
    p = str(path or "").strip()
    if not p:
        dirs = []
        for grp, root in GALLERY_ROOTS:
            if os.path.isdir(root):
                dirs.append({"name": grp, "path": root,
                             "n": _count_images(root), "hint": root})
        if os.name == "nt":
            for ch in "CDEFGHIJKLMNOPQRSTUVWXYZ":
                d = ch + ":\\"
                if os.path.isdir(d):
                    dirs.append({"name": d.rstrip("\\"), "path": d,
                                 "n": _count_images(d), "hint": "整块盘"})
        if dirs:
            return {"path": "", "parent": "", "dirs": dirs, "images": 0}
        if os.name == "nt":
            p = "C:\\"
        else:
            p = "/"

    p = os.path.abspath(p)
    if not os.path.isdir(p):
        return {"error": "目录不存在：%s" % p}

    dirs, images = [], 0
    try:
        entries = list(os.scandir(p))
    except OSError as e:
        return {"error": "读不了这个目录：%s" % e}

    for e in entries:
        if e.name.startswith("."):
            continue
        try:
            if e.is_dir(follow_symlinks=False):
                if e.name.lower() in SKIP_DIRS:
                    continue
                dirs.append({"name": e.name, "path": os.path.join(p, e.name),
                             "n": _count_images(e.path)})
            elif e.is_file(follow_symlinks=False) and e.name.lower().endswith(IMG_EXT):
                images += 1
        except OSError:
            continue

    dirs.sort(key=lambda x: _natkey(x["name"]))
    # 盘根（D:\）的上一级是空字符串，前端拿它当「已经到顶」。
    parent = os.path.dirname(p.rstrip("\\/") or p)
    return {"path": p, "parent": parent, "dirs": dirs, "images": images}


def _count_images(d):
    """一个目录里**直接**放了多少张图（不算子目录）。给选文件夹时看的。"""
    n = 0
    try:
        for e in os.scandir(d):
            try:
                if (not e.name.startswith(".")
                        and e.is_file(follow_symlinks=False)
                        and e.name.lower().endswith(IMG_EXT)):
                    n += 1
            except OSError:
                continue
    except OSError:
        return 0
    return n


# ─── 缩略图 / 预览图 ────────────────────────────────────

def _norm_mode(im):
    """转 RGB。带透明通道的合成到白底——直接 convert("RGB") 会把透明变黑。"""
    if im.mode in ("RGBA", "LA", "P"):
        im = im.convert("RGBA")
        bg = Image.new("RGB", im.size, (255, 255, 255))
        bg.paste(im, mask=im.split()[-1])
        return bg
    return im.convert("RGB")


def reuse_direct(src, edge=1600):
    """已经是我们自己压过的 webp、且不比 edge 大 —— 直接发源文件。

    群友上传的图在落盘时就是 1600px WebP，再压一次只掉画质、还多占一份缓存。
    只认 `.webp`：站主那边的小 PNG/JPEG 转 webp 仍然划算（体积能差十倍）。
    """
    if not str(src).lower().endswith(".webp"):
        return False
    try:
        with Image.open(src) as im:
            return max(im.size) <= edge
    except Exception:
        return False


def cache_path(src, kind):
    """某张图的缓存落在哪。文件名用路径的哈希：同一个文件改了名就该重算，
    所以只认路径不认内容。"""
    key = hashlib.sha1(src.encode("utf-8", "replace")).hexdigest()[:20]
    return os.path.join(CACHE, key + "_" + kind + ".webp")


def thumb(src, kind="t"):
    """按需生成缓存图。→ 缓存文件绝对路径，失败返回 None。

    kind: `t` 网格缩略（360px）、`v` 预览（1600px）。画廊只发这两种，
    原图不出服务端。
    """
    edge = {"t": 360, "v": 1600}.get(kind)
    if not edge:
        return src if os.path.isfile(src) else None
    if not os.path.isfile(src):
        return None

    out = cache_path(src, kind)
    try:
        if os.path.exists(out) and os.path.getmtime(out) >= os.path.getmtime(src):
            return out
    except OSError:
        return None

    with _lock_for(out):
        try:
            if os.path.exists(out) and os.path.getmtime(out) >= os.path.getmtime(src):
                return out
        except OSError:
            return None
        try:
            os.makedirs(CACHE, exist_ok=True)
            with Image.open(src) as im:
                im = _norm_mode(im)
                im.thumbnail((edge, edge), Image.LANCZOS)
                tmp = "%s.%d.%d.tmp" % (out, os.getpid(), threading.get_ident())
                im.save(tmp, "WEBP", quality=85, method=4)
            os.replace(tmp, out)
            return out
        except Exception:
            log.warning("生成预览失败 %s", src, exc_info=True)
            return None


# ─── 后台预生成缩略图 ────────────────────────────────────
# 扫完一万三千张，等群友点进来时才现算就晚了：一张 0.1~0.3 秒，一屏十几张
# 就是两三秒纯 CPU。这里挂一个后台线程，扫描/上传之后把缺的补上。
#
# 队列空了线程就退出，不常驻——画廊没人看的时候不该有东西在占 CPU。

_warm_q = deque()
_warm_seen = set()          # 已在队列里的 (path, kinds)，别重复排
_warm_cv = threading.Condition()
_warm_alive = False
WARM_QUEUE_MAX = 30000      # 队列上限，防止一次塞进几十万条


def warm(paths, kinds=("t",), front=False):
    """把「生成缩略图」排队，后台线程慢慢消化。→ 排进去几条。

    已经在队列里、或者刚刚生成过的都不算新增（`thumb` 自己会认缓存，
    重复排只是白跑一次 stat）。

    `front=True` 插队到最前面：刚上传的那几张必须马上好——站主扫完一万张
    的队列可能还在跑，群友传完立刻进作品集，不该排在人家后面等。
    """
    global _warm_alive
    added = 0
    with _warm_cv:
        for p in paths:
            if not p:
                continue
            key = (p, kinds)
            if key in _warm_seen:
                continue
            if len(_warm_q) >= WARM_QUEUE_MAX:
                break
            _warm_seen.add(key)
            if front:
                _warm_q.appendleft(key)
            else:
                _warm_q.append(key)
            added += 1
        if added and not _warm_alive:
            _warm_alive = True
            threading.Thread(target=_warm_loop, name="gal-warm",
                             daemon=True).start()
        _warm_cv.notify()
    return added


def _warm_loop():
    global _warm_alive
    while True:
        with _warm_cv:
            if not _warm_q:
                _warm_alive = False       # 队列空了就退出
                return
            src, kinds = _warm_q.popleft()
        try:
            for kind in kinds:
                thumb(src, kind)
        except Exception:
            log.warning("预生成缩略图失败 %s", src, exc_info=True)
        finally:
            with _warm_cv:
                _warm_seen.discard((src, kinds))
        # 让一下 GIL：这个线程和「正在取图的请求」抢 CPU，图再快点也没用，
        # 人家等的是首屏。每张挪 5ms，一万三千张也只多花一分多钟。
        time.sleep(0.005)


def warm_ids(wids, kinds=("t",)):
    """按作品集排队。扫描完只排刚扫的那几个，不必全库重来。"""
    wids = [int(w) for w in wids]
    if not wids:
        return 0
    marks = ",".join("?" * len(wids))
    with connect() as con:
        rows = con.execute("SELECT path FROM items WHERE work_id IN (%s)" % marks,
                           wids).fetchall()
    return warm([r["path"] for r in rows], kinds)


def missing_cache(paths, kind="t"):
    """挑出还没有缓存图的那些。文件没了的也跳过——排了也是白跑。"""
    todo = []
    for p in paths:
        if not p:
            continue
        try:
            if not os.path.isfile(p):
                continue
            if os.path.exists(cache_path(p, kind)):
                continue
        except OSError:
            continue
        todo.append(p)
    return todo


def warm_all(kinds=("t",)):
    """全库补一遍：把还没有缩略图的条目排队。

    放在线程里查库——一万三千行 + 一万三千次 stat 要几百毫秒，不该让
    「扫描」这个请求等它。
    """
    def job():
        try:
            with connect() as con:
                rows = [r["path"] for r in con.execute("SELECT path FROM items")]
        except Exception:
            log.warning("预生成：读图列表失败", exc_info=True)
            return
        n = warm(missing_cache(rows, kinds[0]), kinds)
        if n:
            log.info("预生成缩略图：排了 %d 张", n)

    threading.Thread(target=job, name="gal-warm-scan", daemon=True).start()


# ─── 群友上传 ────────────────────────────────────────────

def upload_dir(qq, wid):
    return os.path.join(UPLOAD, str(qq), str(wid))


def create_work(qq, title=""):
    """先开一个空的作品集壳，返回 wid。

    逐张上传要用它：前端压好一张就传一张，第一张落地时后面九张可能还没
    解压完。所以壳必须先于图存在，否则每张都得等上一张。

    中途关掉页面最多留一个空壳，`abort_upload` 负责回收。
    """
    now = time.time()
    with connect() as con:
        cur = con.execute(
            "INSERT INTO works(key, src, owner, grp, title, path, cover, n,"
            " sort, ts, seen) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            ("", "user", str(qq), "群友", title or "未命名作品", "",
             "", 0, 0, now, now))
        wid = cur.lastrowid
        con.execute("UPDATE works SET key=? WHERE id=?",
                    ("user:%s/%d" % (qq, wid), wid))
    return wid


def own_work(qq, wid):
    """这个作品集是不是他自己的。→ (是, 已经存了几张)"""
    with connect() as con:
        row = con.execute(
            "SELECT n FROM works WHERE id=? AND src='user' AND owner=?",
            (wid, str(qq))).fetchone()
    return (True, int(row["n"] or 0)) if row else (False, 0)


def append_upload(qq, wid, name, raw, idx=None):
    """往已有作品集里追加一张，压成 1600px WebP 落盘。→ 存下的序号（1 基）

    上传一律重新编码，不信原文件名、也不保留原图：一张 8MB 的 PNG 压完
    是 150KB，留着原图等于没做这件事。

    例外：浏览器已经压过的（WebP 且长边 ≤1600）原样落盘。前端上传前会
    在浏览器里缩一遍再传——不这么做，几十 MB 的原图要顺着家宽上行推过
    隧道，一张传十几秒；压完只有 150KB。既然已经压到位，服务端再编一次
    只是白白丢画质，体积也省不下几个字节。
    """
    from io import BytesIO

    d = upload_dir(qq, wid)
    os.makedirs(d, exist_ok=True)
    with connect() as con:
        row = con.execute("SELECT n FROM works WHERE id=?", (wid,)).fetchone()
        used = int(row["n"] or 0) if row else 0
    # 上限拦在这里而不是只拦在接口层：换个入口（旧批量上传）也绕不过去。
    # 同一个序号的重传（上一张失败后重试）不算新增，放行。
    if used >= WORK_MAX and (not idx or idx > used):
        raise WorkFull("这个作品集已经 %d 张了" % WORK_MAX)
    i = idx if idx and idx > 0 else used + 1

    p = os.path.join(d, "%03d.webp" % i)
    with Image.open(BytesIO(raw)) as im:
        already = ((im.format or "").upper() == "WEBP"
                   and max(im.size) <= 1600)
        if already:
            with open(p, "wb") as f:
                f.write(raw)
        else:
            out = _norm_mode(im)
            out.thumbnail((1600, 1600), Image.LANCZOS)
            out.save(p, "WEBP", quality=88, method=4)

    size = os.path.getsize(p)
    with connect() as con:
        # 同一个序号重传（上一张失败后重试）就顶掉旧行，不留重复
        con.execute("DELETE FROM items WHERE work_id=? AND idx=?", (wid, i - 1))
        con.execute("INSERT INTO items(work_id, idx, path, bytes)"
                    " VALUES(?,?,?,?)", (wid, i - 1, p, size))
        con.execute(
            "UPDATE works SET path=?,"
            " cover=CASE WHEN cover='' OR cover IS NULL THEN ? ELSE cover END,"
            " n=(SELECT COUNT(*) FROM items WHERE work_id=works.id)"
            " WHERE id=?", (d, p, wid))
    # 顺手把这一张的缩略图排上，插队到头里：群友传完立刻进作品集看，
    # 不该排在站主扫描那一万张的后面等。
    warm([p], front=True)
    return i


def abort_upload(qq, wid):
    """回收空壳：一张都没存下的作品集别留在墙上。→ 真的删了为 True"""
    ok, n = own_work(qq, wid)
    if not ok or n:
        return False
    delete_work(wid, owner=qq)
    return True


def save_upload(qq, files):
    """一次传多张：开壳 + 逐张落盘。→ (work_id, 存了几张)

    files: [(filename, bytes)]，已由调用方做过大小检查。
    逐张上传的接口走 create_work + append_upload，这里是批量兜底。
    """
    wid = create_work(qq)
    n = 0
    for i, (name, raw) in enumerate(files, 1):
        try:
            append_upload(qq, wid, name, raw, idx=i)
            n += 1
        except Exception:
            log.warning("上传的第 %d 张转码失败：%s", i, name, exc_info=True)
    if not n:
        delete_work(wid)
        return None, 0
    return wid, n


def load_upload_image(raw):
    """校验一张上传的图能不能打开。→ PIL 尺寸或 None。"""
    from io import BytesIO
    try:
        with Image.open(BytesIO(raw)) as im:
            im.verify()
        with Image.open(BytesIO(raw)) as im:
            return im.size
    except Exception:
        return None


def delete_work(wid, owner=None):
    """删一个作品集。群友只能删自己的（owner 传 QQ 号时强校验）。"""
    with connect() as con:
        row = con.execute("SELECT * FROM works WHERE id=?", (wid,)).fetchone()
        if not row:
            return False, "没有这个作品集"
        if owner is not None and row["src"] == "user" and row["owner"] != str(owner):
            return False, "只能删自己的"
        if row["src"] == "owner":
            return False, "站主作品集不能从网页删（去磁盘删，再重新扫描）"
        con.execute("DELETE FROM items WHERE work_id=?", (wid,))
        con.execute("DELETE FROM marks WHERE item_id NOT IN "
                    "(SELECT id FROM items)", ())
        con.execute("DELETE FROM works WHERE id=?", (wid,))
    try:
        shutil.rmtree(upload_dir(row["owner"], wid), ignore_errors=True)
    except OSError:
        pass
    return True, ""


def drop_orphans():
    """清掉文件已经不存在的 items（站主手工删图之后用）。"""
    gone = []
    with connect() as con:
        for row in con.execute(
                "SELECT i.id, i.path FROM items i JOIN works w ON w.id=i.work_id"
                " WHERE w.src='owner'"):
            if not os.path.isfile(row["path"]):
                gone.append(row["id"])
        for i in range(0, len(gone), 500):
            chunk = gone[i:i + 500]
            con.execute("DELETE FROM items WHERE id IN (%s)"
                        % ",".join("?" * len(chunk)), chunk)
        con.execute("UPDATE works SET n=(SELECT COUNT(*) FROM items"
                    " WHERE work_id=works.id) WHERE src='owner'")
    return len(gone)


# ─── 展示名 ──────────────────────────────────────────────

def alias(qq):
    """QQ 号 → 匿名代号。稳定映射：同一个人永远是同一个字母组合。

    不落库也不落盘：按 QQ 号取 sha1 再折算成两位字母，同号恒同值，
    换个库也是同一个结果。这样「公开墙上看不出是谁」和「同一人作品
    能连起来看」两件事同时成立。
    """
    qq = str(qq or "")
    if not qq:
        return "匿名用户"
    h = hashlib.sha1(("moss-gal:" + qq).encode()).digest()
    first = chr(ord("A") + h[0] % 26)
    second = chr(ord("A") + h[1] % 26)
    return "匿名用户 %s%s" % (first, second)
