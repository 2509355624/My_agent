# -*- coding: utf-8 -*-
"""工作台私有参考图：独立元数据和文件存储，不进入 gallery works/items。"""

import contextlib
import os
import re
import secrets
import sqlite3
import time
from io import BytesIO

from PIL import Image

from app.config import state_path

REF_DB = state_path("gallery_refs.db")
REF_DIR = state_path("gallery_refs")
REF_TTL = 24 * 3600
REF_ID_RE = re.compile(r"^[A-Za-z0-9_-]{24,128}$")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS refs (
    ref_id TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    path TEXT NOT NULL,
    name TEXT DEFAULT '',
    created REAL NOT NULL,
    expires REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_refs_expiry ON refs(expires);
CREATE INDEX IF NOT EXISTS idx_refs_owner ON refs(owner, expires);
"""


@contextlib.contextmanager
def connect():
    os.makedirs(os.path.dirname(REF_DB) or ".", exist_ok=True)
    con = sqlite3.connect(REF_DB, timeout=15)
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
    with connect() as con:
        con.executescript(_SCHEMA)
    os.makedirs(REF_DIR, exist_ok=True)


def valid_ref_id(value):
    return bool(REF_ID_RE.fullmatch(str(value or "")))


def _new_id():
    return secrets.token_urlsafe(32)


def _clean_name(name):
    name = os.path.basename(str(name or "").strip())
    return name[:120]


def purge(now=None):
    now = time.time() if now is None else float(now)
    removed = 0
    with connect() as con:
        rows = con.execute("SELECT ref_id, path FROM refs WHERE expires<=?",
                           (now,)).fetchall()
        con.execute("DELETE FROM refs WHERE expires<=?", (now,))
    for row in rows:
        try:
            os.remove(row["path"])
        except OSError:
            pass
        removed += 1
    return removed


def create(owner, name, raw, ttl=REF_TTL, now=None):
    """校验并压缩图片，返回私有引用元数据。"""
    now = time.time() if now is None else float(now)
    owner = str(owner)
    with Image.open(BytesIO(raw)) as im:
        im.load()
        if "A" in im.getbands():
            out = im.convert("RGBA")
        else:
            out = im.convert("RGB")
        out.thumbnail((1600, 1600), Image.LANCZOS)
        ref_id = _new_id()
        os.makedirs(REF_DIR, exist_ok=True)
        path = os.path.join(REF_DIR, ref_id + ".webp")
        out.save(path, "WEBP", quality=88, method=4)
    expires = now + max(1, int(ttl))
    try:
        with connect() as con:
            con.execute("INSERT INTO refs(ref_id, owner, path, name, created, expires)"
                        " VALUES(?,?,?,?,?,?)",
                        (ref_id, owner, path, _clean_name(name), now, expires))
    except Exception:
        try:
            os.remove(path)
        except OSError:
            pass
        raise
    return {"ref_id": ref_id, "path": path, "created": now,
            "expires": expires, "name": _clean_name(name)}


def get(ref_id, owner, now=None):
    """只返回指定所有者仍在有效期内的引用。"""
    if not valid_ref_id(ref_id):
        return None
    now = time.time() if now is None else float(now)
    with connect() as con:
        row = con.execute(
            "SELECT * FROM refs WHERE ref_id=? AND owner=? AND expires>?",
            (str(ref_id), str(owner), now)).fetchone()
    if not row:
        return None
    if not os.path.isfile(row["path"]):
        return None
    return row


def delete(ref_id, owner=None):
    """删除引用及对应文件；owner 不为空时同时校验所有权。"""
    if not valid_ref_id(ref_id):
        return False
    with connect() as con:
        sql = "SELECT path FROM refs WHERE ref_id=?"
        args = [str(ref_id)]
        if owner is not None:
            sql += " AND owner=?"
            args.append(str(owner))
        row = con.execute(sql, args).fetchone()
        if not row:
            return False
        con.execute("DELETE FROM refs WHERE ref_id=?", (str(ref_id),))
    try:
        os.remove(row["path"])
    except OSError:
        pass
    return True
