# -*- coding: utf-8 -*-
"""
RAG 常驻进程：保持 BGE-M3 模型加载，通过 stdin/stdout JSON Lines 通信。

请求格式: {"id":"1","cmd":"ping|search|ingest|list_kb|delete_kb|delete_entry|delete_entries|list_entries", ...}
响应格式: {"id":"1","ok":true,...} 或 {"id":"1","ok":false,"error":"..."}
"""

import json
import os
import sys
import traceback
from pathlib import Path

from dotenv import load_dotenv

HERE = Path(__file__).parent
ROOT = HERE.parent.parent
load_dotenv(ROOT / ".env")

# 把项目根加入 sys.path，确保子进程直接运行脚本时也能导入
import sys as _sys
if str(ROOT) not in _sys.path:
    _sys.path.insert(0, str(ROOT))

from app.rag.local_embeddings import LocalEmbeddings, DEFAULT_MODEL  # noqa: E402
from app.rag.vector_store import VectorStore  # noqa: E402


def _force_utf8_stdio() -> None:
    """把 stdin/stdout/stderr 三个流都钉成 UTF-8（2026-11-15 修）。

    这次是真的「必须」：Windows 上进程的 stdio 连到管道时，Python 用的是系统
    本地编码（简体中文机器 = cp936/GBK），**不是 UTF-8**。于是 daemon 把中文
    JSON 按 GBK 写进管道，父进程 `RagClient` 按 UTF-8 读、`errors="replace"`，
    结果是：

    * 轻的：正文全变成 `\\ufffd`，检索回来的片段是乱码，模型只能照着乱码编；
    * 重的：GBK 的双字节序列被 UTF-8 解码器当成多字节字符，**把紧跟其后的
      ASCII 引号/逗号一起吃掉**，JSON 结构当场坏掉 —— 父进程 `json.loads`
      失败、`continue`，那个请求的 Event 永远等不到 → 30 秒超时 → 页面显示
      「请求失败（HTTP 502）」。

    以前只 reconfigure 了 stdin（那是另一半：父进程按 UTF-8 写中文，daemon
    按 GBK 读会 UnicodeDecodeError 直接退出），stdout 一直漏着。
    """
    for name in ("stdin", "stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None:
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:                                # noqa: BLE001
            pass


def _reply(msg: dict) -> None:
    line = json.dumps(msg, ensure_ascii=False)
    try:
        sys.stdout.write(line + "\n")
    except UnicodeEncodeError:
        # 双保险：真遇到编码不了的东西，退化成纯 ASCII 的 \uXXXX 转义。
        # 宁可正文难看，也不能让 daemon 在写响应时炸掉、连带后面的请求全超时。
        sys.stdout.write(json.dumps(msg, ensure_ascii=True) + "\n")
    sys.stdout.flush()


def _resolve_data_dir() -> Path:
    """向量库数据目录：项目根 vector_store/chroma"""
    raw = os.environ.get("RAG_DATA_DIR", "").strip()
    if raw:
        p = Path(raw)
        if not p.is_absolute():
            p = (ROOT / p).resolve()
        return p
    return ROOT / "vector_store" / "chroma"


def _model_cache_hit(model_name: str) -> str | None:
    """检查模型是否已在本地。未命中返回 None。"""
    if os.path.isdir(model_name):
        return model_name
    try:
        from modelscope import snapshot_download
        try:
            return snapshot_download(model_name, local_files_only=True)
        except Exception:
            return None
    except Exception:
        return None


def _load_services():
    data_dir = _resolve_data_dir()
    print(f"[RAG] data_dir={data_dir}", file=sys.stderr, flush=True)

    device = os.environ.get("RAG_EMBED_DEVICE", "cuda").strip().lower()
    model_name = os.environ.get("LOCAL_EMBEDDING_MODEL", DEFAULT_MODEL)
    auto_download = os.environ.get("RAG_AUTO_DOWNLOAD", "0").strip().lower() in ("1", "true", "yes")

    local = _model_cache_hit(model_name)
    if local is None and not auto_download:
        hint = (
            f"向量模型未找到（{model_name}），RAG 功能不可用。\n"
            "启用方式（任选其一）：\n"
            "  1) 设 LOCAL_EMBEDDING_MODEL 指向本地已下载的模型目录；\n"
            "  2) 设 RAG_AUTO_DOWNLOAD=1 让 daemon 自动联网下载（首次需网络）；\n"
            "  3) 手动下载：python -c \"from modelscope import snapshot_download; "
            "snapshot_download('BAAI/bge-m3')\""
        )
        print(f"[RAG] {hint}", file=sys.stderr, flush=True)
        _reply({"id": "0", "ok": False, "event": "model_missing", "model": model_name, "hint": hint})
        sys.exit(1)

    # **把「已经解析好的本地目录」传下去，而不是模型名**（2026-10-01 修）。
    # 传模型名的话，`LocalEmbeddings._resolve_model_path` 会再调一次
    # `snapshot_download(model_name)`——那个**没有 local_files_only**，每次都联网
    # 核对，本地缓存里只要有一个文件没下全（bge-m3 的 `onnx/model.onnx` 就永远
    # 拿不到，HTTP 416），它就要把重试跑满，实测**每次启动白烧 30 秒**。
    # 这里 `_model_cache_hit` 已经确认模型在本地了，直接给目录就一步到位。
    embeddings = LocalEmbeddings(model_name=local or model_name, device=device)
    store = VectorStore(data_dir)
    return embeddings, store


def _handle(req: dict, embeddings: LocalEmbeddings, store: VectorStore) -> dict:
    req_id = req.get("id")
    cmd = req.get("cmd")
    try:
        if cmd == "ping":
            return {"id": req_id, "ok": True, "ready": True}

        if cmd == "list_kb":
            cols = store.list_collections()
            counts = {}
            for c in cols:
                try:
                    counts[c] = store.client.get_collection(c).count()
                except Exception:
                    counts[c] = 0
            return {"id": req_id, "ok": True, "collections": cols, "counts": counts}

        if cmd == "search":
            kb_name = str(req.get("kb_name") or "").strip()
            query = str(req.get("query") or "").strip()
            k = int(req.get("k") or os.environ.get("RAG_RETRIEVE_K", "4"))
            if not kb_name or not query:
                raise ValueError("kb_name and query are required")
            q_vec = embeddings.embed_query(query)
            where = req.get("where") or None
            docs = store.query(kb_name, q_vec, k=k, where=where)
            return {"id": req_id, "ok": True, "docs": docs}

        if cmd == "ingest":
            kb_name = str(req.get("kb_name") or "").strip()
            entries = req.get("entries") or []
            if not kb_name:
                raise ValueError("kb_name is required")
            prepared = []
            texts = []
            for entry in entries:
                content = str(entry.get("content") or "").strip()
                if not content:
                    continue
                texts.append(content)
                prepared.append(entry)
            if not texts:
                return {"id": req_id, "ok": True, "count": 0}
            vectors = embeddings.embed_documents(texts)
            upsert_rows = []
            for entry, vec in zip(prepared, vectors):
                upsert_rows.append({
                    "id": entry["id"],
                    "content": entry["content"],
                    "metadata": entry.get("metadata") or {},
                    "embedding": vec,
                })
            count = store.upsert_entries(kb_name, upsert_rows)
            # 2026-11-15：演示页要展示「一段文字变成了什么样的数字」。
            # 这里顺手回每块的维度、模长和前 8 维——向量已经在内存里，
            # 回传这几个数零额外计算。老调用方只读 count，多出的字段不影响。
            preview = []
            for vec in vectors[:50]:
                try:
                    norm = sum(float(x) * float(x) for x in vec) ** 0.5
                    head = [round(float(x), 4) for x in vec[:8]]
                except Exception:                       # noqa: BLE001
                    norm, head = 0.0, []
                preview.append({"dim": len(vec), "norm": round(norm, 4),
                                "head": head})
            return {"id": req_id, "ok": True, "count": count,
                    "dim": len(vectors[0]) if vectors else 0,
                    "preview": preview}

        if cmd == "delete_kb":
            kb_name = str(req.get("kb_name") or "").strip()
            if not kb_name:
                raise ValueError("kb_name is required")
            deleted = store.delete_kb(kb_name)
            return {"id": req_id, "ok": True, "deleted": deleted}

        if cmd == "delete_entry":
            kb_name = str(req.get("kb_name") or "").strip()
            entry_id = str(req.get("entry_id") or "").strip()
            if not kb_name or not entry_id:
                raise ValueError("kb_name and entry_id are required")
            deleted = store.delete_entry(kb_name, entry_id)
            return {"id": req_id, "ok": True, "deleted": deleted}

        if cmd == "delete_entries":
            kb_name = str(req.get("kb_name") or "").strip()
            ids = req.get("ids") or []
            if not kb_name or not ids:
                raise ValueError("kb_name and ids are required")
            deleted = store.delete_entries(kb_name, ids)
            return {"id": req_id, "ok": True, "deleted": deleted}

        if cmd == "list_entries":
            kb_name = str(req.get("kb_name") or "").strip()
            if not kb_name:
                raise ValueError("kb_name is required")
            entries = store.get_all(kb_name)
            return {"id": req_id, "ok": True, "entries": entries}

        raise ValueError(f"unknown cmd: {cmd}")
    except Exception as exc:
        return {"id": req_id, "ok": False, "error": str(exc)}


def main():
    _force_utf8_stdio()
    embeddings, store = _load_services()
    _reply({"id": "0", "ok": True, "event": "ready"})
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError as exc:
            _reply({"id": None, "ok": False, "error": f"invalid json: {exc}"})
            continue
        resp = _handle(req, embeddings, store)
        _reply(resp)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc(file=sys.stderr)
        sys.exit(1)
