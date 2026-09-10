# -*- coding: utf-8 -*-
"""
RAG 常驻进程：保持 BGE-M3 模型加载，通过 stdin/stdout JSON Lines 通信。

请求格式: {"id":"1","cmd":"ping|search|ingest|list_kb|delete_kb|delete_entry", ...}
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


def _reply(msg: dict) -> None:
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
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

    if _model_cache_hit(model_name) is None and not auto_download:
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

    embeddings = LocalEmbeddings(model_name=model_name, device=device)
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
            docs = store.query(kb_name, q_vec, k=k)
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
            return {"id": req_id, "ok": True, "count": count}

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

        raise ValueError(f"unknown cmd: {cmd}")
    except Exception as exc:
        return {"id": req_id, "ok": False, "error": str(exc)}


def main():
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
