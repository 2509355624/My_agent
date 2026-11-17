# -*- coding: utf-8 -*-
"""文档问答实验室（`/api/raglab/*`）：把 RAG 的每一步摊开给访客看。

## 为什么单独一个模块

展示页要的是**过程数据**：清洗删了哪些行、大模型怎么切的、每块被压成什么样
的数字、问题凭什么命中这一条。现成的 `app/tools/rag/kb_tools.py` 是给 Agent
用的——`chunk_document` 只回前 5 块预览，`ingest` 只回条数，中间全丢。所以这里
直接和 `RagClient` / `llm.call_llm` 对话，把中间件原样交给前端。

## 和角色扮演那套向量库的关系

**完全隔离**：独立的 daemon、独立的数据目录（`vector_store/chroma_lab`）、
小模型 `bge-small-zh-v1.5`（512 维）。理由（2026-11-15 用户拍板）：bge-m3 加载
十几秒、1024 维、内存大，本机还要跑 ComfyUI 生图，扛不住。小模型实测加载
126 ms（`logs/agent.log` 00:43:56,530 → 00:43:56,656），512 token 上限 → 中文
约 400~500 字，所以 chunk 目标就是 300~450 字，这条约束也正好是演示页要讲的点。

## 切分交给大模型

1 万字一次性出不来（输出约 1.5 万 token，会被服务端上限截在半句话上），按
2400 字分批。提示词原样回给前端展示——这是这个演示页最想让人看到的东西。

## 会话与临时库

`demo_<8位>` 一个会话一个库，最多同时 3 个（超了淘汰最久未用的），30 分钟
无人访问惰性删除（每个请求顺带扫一遍，不起后台线程）。
"""

import base64
import binascii
import io
import json
import logging
import os
import re
import threading
import time
import uuid

from flask import Blueprint, jsonify, request

from app import llm
from app.integrations import vision
from app.config import DEEPSEEK_MODEL, ROOT_DIR, state_path
from app.features.rag import rerank

log = logging.getLogger("rag_lab")

bp = Blueprint("raglab", __name__)

# ─── 硬上限（2026-11-15）──────────────────────────────
MAX_CHARS = 10000          # 一个演示文档最多 1 万字，超出直接拒
BATCH_CHARS = 2400         # 每次交给大模型的正文字数（输出约 4000 token）
CHUNK_TARGET = (300, 450)  # 小模型 512 token ≈ 中文 400~500 字
CHUNK_HARD_MAX = 500       # 超过就标 over，让页面显示「会被截断」
MAX_CHUNKS = 200           # 再多向量化就会明显拖慢本机
MAX_LIBS = 3               # 同时最多 3 个临时库
TTL_SECONDS = 1800         # 30 分钟无人访问就删
RATE_LIMIT = 12            # 每分钟每会话最多 12 次检索/问答
MAX_ANSWER_TOKENS = 1200

# ─── 上传与抽取（2026-11-15，PDF / Word）───────────────────
# 两条路：有文字层的直接抽（pypdf / python-docx，零 token 零幻觉）；没有文字层
# 的扫描页渲染成图交视觉模型抄成 Markdown（见下方 OCR_* 常量）。顺序永远是
# **先文本层再视觉**——能免费拿到的正文绝不花钱去认。
MAX_UPLOAD = 15 * 1024 * 1024  # 15MB：文本型 PDF 40 页也就 1~3MB，但手机拍的
                               # 扫描件一页就 2~4MB，8MB 会把人挡在门外（2026-11-15）
MAX_PAGES = 40                 # 再多 1 万字也装不下，纯属浪费解析时间
SCAN_PAGE_CHARS = 50           # 一页抽出来不到这么多字 → 按「没有文字层」算

LAB_EMBED_MODEL = os.getenv("RAG_LAB_MODEL", "BAAI/bge-small-zh-v1.5").strip()
LAB_DATA_DIR = os.getenv(
    "RAG_LAB_DATA_DIR",
    os.path.join(ROOT_DIR, "vector_store", "chroma_lab"))

_STATE = state_path("rag_lab_sessions.json")
_LOCK = threading.Lock()
_HITS = {}                 # sid -> [ts, ...]（进程内，重启即清）
_CLIENT = None
_CLIENT_LOCK = threading.Lock()


# ══════════════════════════════════════════════════════
# 会话簿
# ══════════════════════════════════════════════════════
def _now():
    return int(time.time())


def _load():
    try:
        with open(_STATE, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("sessions"), dict):
            return data
    except FileNotFoundError:
        pass
    except Exception as exc:                            # noqa: BLE001
        log.warning("rag_lab：会话簿读不出来（%s），按空处理", exc)
    return {"sessions": {}}


def _save(data):
    """落盘。先写 .tmp 再 replace——并发写不会留下半截 JSON。"""
    tmp = _STATE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, _STATE)
    except Exception as exc:                            # noqa: BLE001
        log.warning("rag_lab：会话簿写不进去（%s）", exc)


def _client():
    """演示专用向量 daemon：小模型 + 独立目录，绝不碰主 RAG。"""
    global _CLIENT
    with _CLIENT_LOCK:
        if _CLIENT is None:
            from app.features.rag.rag_client import RagClient
            _CLIENT = RagClient(env_extra={
                "LOCAL_EMBEDDING_MODEL": LAB_EMBED_MODEL,
                "RAG_DATA_DIR": LAB_DATA_DIR,
                "RAG_AUTO_DOWNLOAD": "0",
            })
            _CLIENT.start()
            # 小模型 100 多毫秒，给 60 秒纯粹是给「第一次要下载」留余地
            for _ in range(600):
                if _CLIENT.is_ready():
                    break
                time.sleep(0.1)
            log.info("rag_lab：向量 daemon ready=%s（%s）",
                     _CLIENT.is_ready(), LAB_EMBED_MODEL)
        return _CLIENT


def _gc(data=None):
    """惰性清理：过期会话删库，超过 MAX_LIBS 的按最久未用淘汰。"""
    data = data or _load()
    sessions = data["sessions"]
    now = _now()
    changed = False
    expired = [sid for sid, s in sessions.items()
               if now - int(s.get("last") or 0) > TTL_SECONDS]
    for sid in expired:
        _drop(sessions, sid)
        changed = True
    if len(sessions) > MAX_LIBS:
        order = sorted(sessions.items(), key=lambda kv: int(kv[1].get("last") or 0))
        for sid, _ in order[:len(sessions) - MAX_LIBS]:
            _drop(sessions, sid)
            changed = True
    # 2026-11-15：这里必须用 changed 判断，不能回头再看 len(sessions)——淘汰完
    # 刚好等于上限，条件不再成立，删掉的会话就不会落盘（单测逮到过）。
    if changed:
        _save(data)
    return data


def _drop(sessions, sid):
    rec = sessions.pop(sid, None)
    if not rec:
        return
    try:
        _client().delete_kb(rec.get("kb") or "")
    except Exception as exc:                            # noqa: BLE001
        log.warning("rag_lab：删库 %s 失败（%s）", rec.get("kb"), exc)


def _new_session(filename):
    data = _gc()
    sid = uuid.uuid4().hex[:8]
    data["sessions"][sid] = {
        "kb": "demo_" + sid,
        "created": _now(),
        "last": _now(),
        "filename": filename or "未命名文档",
        "chars": 0,
        "chunks": 0,
        "ingested": False,
    }
    _save(data)
    return sid, data["sessions"][sid]


def _touch(sid):
    data = _gc()
    rec = data["sessions"].get(sid)
    if rec is None:
        return None
    rec["last"] = _now()
    _save(data)
    return rec


def _rate_ok(sid):
    """每会话每分钟 12 次检索/问答。内存计数，进程重启即清。"""
    now = time.time()
    bucket = [t for t in _HITS.get(sid, []) if now - t < 60]
    if len(bucket) >= RATE_LIMIT:
        _HITS[sid] = bucket
        return False
    bucket.append(now)
    _HITS[sid] = bucket
    return True


# ══════════════════════════════════════════════════════
# ① 清洗
# ══════════════════════════════════════════════════════
_PAGE_RES = (
    re.compile(r"^\s*[-—–]?\s*\d{1,4}\s*[-—–]?\s*$"),
    re.compile(r"^\s*第\s*\d{1,4}\s*页(?:\s*[/／,，]\s*共?\s*\d{1,4}\s*页)?\s*$"),
    re.compile(r"^\s*\d{1,4}\s*[/／]\s*\d{1,4}\s*$"),
    # 英文 PDF 的页脚（2026-11-15：抽英文白皮书时发现 "Page 3 of 12" 不被删）
    re.compile(r"^\s*(?:page|p\.)\s*\d{1,4}\s*(?:of\s*\d{1,4})?\s*$", re.I),
)
_ZERO_WIDTH = re.compile(r"[\u200b\u200c\u200d\ufeff]")
_BLANK = re.compile(r"^\s*$")


def clean_text(raw):
    """规则清洗。返回 (clean_text, report)。

    只做**确定性**的事：去零宽、去行尾空白、去页码行、去重复页眉页脚、
    压缩连续空行。语义判断一律不在这里做——那是下一步大模型的活。
    """
    raw = (raw or "").replace("\r\n", "\n").replace("\r", "\n")
    report = {
        "raw_chars": len(raw),
        "removed": {"zero_width": 0, "page": 0, "repeat": 0,
                    "blank": 0, "trimmed": 0},
        "samples": [],
    }

    zero = len(_ZERO_WIDTH.findall(raw))
    if zero:
        report["removed"]["zero_width"] = zero
        raw = _ZERO_WIDTH.sub("", raw)

    lines = raw.split("\n")
    trimmed = 0
    step1 = []
    for line in lines:
        stripped = line.rstrip()
        if stripped != line:
            trimmed += 1
        step1.append(stripped)
    report["removed"]["trimmed"] = trimmed

    # 重复短行：长度 ≤ 40 且出现 ≥ 3 次 → 页眉/页脚/水印
    counter = {}
    for line in step1:
        s = line.strip()
        if s and len(s) <= 40:
            counter[s] = counter.get(s, 0) + 1
    repeats = {s for s, n in counter.items() if n >= 3}

    kept = []
    for line in step1:
        s = line.strip()
        if s and any(p.match(s) for p in _PAGE_RES):
            report["removed"]["page"] += 1
            if len(report["samples"]) < 12:
                report["samples"].append({"line": s, "reason": "页码行"})
            continue
        if s and s in repeats:
            report["removed"]["repeat"] += 1
            if len(report["samples"]) < 12:
                report["samples"].append({"line": s, "reason": "重复出现 ≥3 次的短行（页眉/页脚/水印）"})
            continue
        kept.append(line)

    # 连续空行压成一行
    out = []
    blank_run = 0
    for line in kept:
        if _BLANK.match(line):
            blank_run += 1
            if blank_run > 1:
                report["removed"]["blank"] += 1
                continue
        else:
            blank_run = 0
        out.append(line)

    text = "\n".join(out).strip()
    report["clean_chars"] = len(text)
    report["kept_lines"] = sum(1 for line in out if line.strip())
    report["drop_chars"] = report["raw_chars"] - report["clean_chars"]
    return text, report


# ══════════════════════════════════════════════════════
# ①.5 抽取：PDF / Word 的文本层
# ══════════════════════════════════════════════════════
# A 档（2026-11-15 上午）：只抽**文本层**，覆盖 Word 导出的 PDF、电子书、正规
# 文档——这类零 token、零幻觉。
# B 档（2026-11-15 晚，用户拍板）：扫描件那一档也接上——没有文字层的页渲染成图
# 交给视觉模型抄成 Markdown。只处理**本来就没字的那几页**，有字的一页都不 OCR。
#
# 依赖：pypdf（纯 Python，无 torch）+ python-docx，合计约 10MB；扫描件另需
# PyMuPDF。都不常驻内存，只在这次请求里 import，没装也不影响别的功能。

class ExtractError(Exception):
    """抽不出正文。`str(e)` 直接展示给用户，要写成人话。"""


# ── 扫描件：渲染 + 视觉识别 ──────────────────────────────
# 三个数字的来由：
#   ① 一页一次调用、串行。10 页约 40~90 秒，再多访客等不起（网关还要放行长请求）。
#   ② 1600 长边（A4 ≈ 200dpi）是小字能认清的底线。**不能走 vision.to_data_url**
#      ——那个会压到 VISION_MAX_EDGE（1024），是给聊天截图定的，A4 正文压下去
#      只有 12px 高的字，模型会开始瞎猜。所以这里自己 base64。
#   ③ 扫描件最怕的不是认不出，是模型「顺手把句子写顺」。那样 RAG 检索到的就是
#      模型编的话而不是原文。prompt 里咬死「只抄不补、看不清写 [不清晰]」。
OCR_MAX_PAGES = 10
OCR_ZOOM = 2.7             # A4 → 1607×2274
OCR_MAX_EDGE = 1600        # 超过就重渲染小一点，省 token 不损可读性
OCR_TIMEOUT = 90           # 单页上限；VISION_TIMEOUT 是 120，这里留点余量

_OCR_PROMPT = (
    "这是一份扫描文档的第 %d 页。把页面上的文字按阅读顺序抄成 Markdown。\n"
    "只抄页面上**真实存在**的字：不要补全、不要改写、不要总结、不要翻译、"
    "不要合并段落。\n"
    "标题用 # / ##，列表保留编号，表格用 Markdown 表格（对齐不准没关系）。\n"
    "页眉、页脚、页码照抄，后面有程序统一清洗。\n"
    "看不清的字写 [不清晰]，整块是插图/照片就写 [图片]。\n"
    "直接给正文，不要加任何解释、前言、后记，不要加代码块围栏。"
)


def _render_page(doc, no):
    """PDF 第 no 页（0 基）→ JPEG 字节。渲染失败返回 None。"""
    import pymupdf
    page = doc[no]
    pix = page.get_pixmap(matrix=pymupdf.Matrix(OCR_ZOOM, OCR_ZOOM))
    long_edge = max(pix.width, pix.height)
    if OCR_MAX_EDGE > 0 and long_edge > OCR_MAX_EDGE:
        zoom = OCR_ZOOM * OCR_MAX_EDGE / float(long_edge)
        pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom))
    return pix.tobytes("jpeg")


def _ocr_scan_pages(raw, texts, scan_pages):
    """把「没有文字层」的页交给视觉模型抄成文字，就地写回 texts。

    返回 (识别成功的页码, 失败的页码, 实际用的模型)。单页失败只记页码不抛异常
    ——3 页里坏一页不该让整份文档作废；但**一页都没成功**时调用方会报错。
    """
    try:
        import pymupdf
    except ImportError:
        raise ExtractError("这台机器没装 PyMuPDF，扫描件识别跑不了"
                           "（pip install pymupdf）")
    try:
        doc = pymupdf.open(stream=raw, filetype="pdf")
    except Exception as exc:                        # noqa: BLE001
        raise ExtractError("这份 PDF 打不开（%s）" % exc.__class__.__name__)
    done, failed, model = [], [], ""
    try:
        for no in scan_pages[:OCR_MAX_PAGES]:
            try:
                jpeg = _render_page(doc, no)
                data_url = ("data:image/jpeg;base64,"
                            + base64.b64encode(jpeg).decode("ascii"))
                text = vision.describe(
                    data_url, timeout=OCR_TIMEOUT,
                    prompt=_OCR_PROMPT % (no + 1)).strip()
            except Exception as exc:                # noqa: BLE001
                log.warning("rag_lab：第 %d 页视觉识别失败 %s", no + 1, exc)
                failed.append(no + 1)
                continue
            if text:
                texts[no] = text
                done.append(no + 1)
            else:
                failed.append(no + 1)
        try:
            pid, mid = vision.active_choice()
            model = ("%s/%s" % (pid or "", mid or "")).strip("/")
        except Exception:                           # noqa: BLE001
            model = ""
    finally:
        try:
            doc.close()
        except Exception:                           # noqa: BLE001
            pass
    return done, failed, model


def _truncate(text, limit=MAX_CHARS):
    """截到 limit 字。优先在换行处断，找不到合适断点再硬切。"""
    if len(text) <= limit:
        return text, False
    cut = text.rfind("\n", 0, limit)
    if cut < int(limit * 0.6):
        cut = limit
    return text[:cut].rstrip(), True


def _extract_pdf(raw):
    from pypdf import PdfReader
    try:
        reader = PdfReader(io.BytesIO(raw))
    except Exception as exc:                        # noqa: BLE001
        raise ExtractError("这份 PDF 打不开（%s），换个文件试试"
                           % exc.__class__.__name__)
    if getattr(reader, "is_encrypted", False):
        try:
            reader.decrypt("")
        except Exception:                           # noqa: BLE001
            raise ExtractError("这份 PDF 有密码保护，先取消密码再传")
    total = len(reader.pages)
    page_chars, low, texts = [], [], []
    for no in range(min(total, MAX_PAGES)):
        try:
            t = reader.pages[no].extract_text() or ""
        except Exception:                           # noqa: BLE001
            t = ""
        t = t.replace("\x00", "")
        n = len(t.strip())
        page_chars.append(n)
        if 0 < n < SCAN_PAGE_CHARS:
            low.append(no + 1)            # 只有页眉页码之类，基本没内容
        texts.append(t)

    # 一页字都没有 = 扫描页。交给视觉模型抄，最多 OCR_MAX_PAGES 页。
    scan_pages = [i for i, n in enumerate(page_chars) if n == 0]
    ocr_done, ocr_failed, ocr_model = [], [], ""
    if scan_pages:
        log.info("rag_lab：%d 页没有文字层，转视觉识别（最多 %d 页）",
                 len(scan_pages), OCR_MAX_PAGES)
        ocr_done, ocr_failed, ocr_model = _ocr_scan_pages(
            raw, texts, scan_pages)

    empty = [i + 1 for i, t in enumerate(texts) if not t.strip()]
    body = "\n\n".join(t for t in texts if t.strip()).strip()
    if not body:
        raise ExtractError(
            "这份 PDF 是扫描件：%d 页里一页都没有文字层，视觉识别也没读出内容。"
            "换一份清晰点的扫描件，或者把正文复制成文本再传。" % total)
    meta = {"kind": "pdf", "pages": total, "read_pages": len(texts),
            "page_chars": page_chars, "empty_pages": empty,
            "low_text_pages": low}
    if ocr_done or ocr_failed:
        meta["source"] = "vision"
        meta["ocr_pages"] = ocr_done
        meta["ocr_failed"] = ocr_failed
        meta["ocr_model"] = ocr_model
    if len(scan_pages) > OCR_MAX_PAGES:
        meta["warning"] = ("扫描页共 %d 页，只识别了前 %d 页"
                           % (len(scan_pages), OCR_MAX_PAGES))
    elif low and len(low) >= max(2, len(texts) // 2):
        meta["warning"] = ("有 %d 页几乎读不到文字（只有页眉页码那种），"
                           "可能是扫描件或图片页，检索质量会受影响" % len(low))
    return body, meta


def _extract_docx(raw):
    import docx
    from docx.table import Table
    from docx.text.paragraph import Paragraph
    try:
        doc = docx.Document(io.BytesIO(raw))
    except Exception as exc:                        # noqa: BLE001
        raise ExtractError("这份 Word 打不开（%s），换个文件试试"
                           % exc.__class__.__name__)
    parts, tables = [], 0
    # 按 XML 顺序遍历正文：只读 paragraphs 会把表格全部甩到文末。
    for child in doc.element.body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            para = Paragraph(child, doc)
            txt = (para.text or "").strip()
            if not txt:
                parts.append("")
                continue
            m = re.match(r"(?:Heading|标题)\s*(\d)", para.style.name or "")
            if m:
                parts.append("#" * min(6, int(m.group(1))) + " " + txt)
            else:
                parts.append(txt)
        elif tag == "tbl":
            rows = []
            for row in Table(child, doc).rows:
                cells = [(c.text or "").strip().replace("\n", " ") for c in row.cells]
                if any(cells):
                    rows.append(" | ".join(cells))
            if rows:
                tables += 1
                parts.append("\n".join(rows))
    body = "\n".join(parts).strip()
    if not body:
        raise ExtractError("这份 Word 里没读到正文（可能是只贴了图片）")
    meta = {"kind": "docx", "pages": 0, "read_pages": 0,
            "page_chars": [], "empty_pages": [], "low_text_pages": [],
            "tables": tables, "blocks": sum(1 for x in parts if x)}
    return body, meta


def _extract_plain(raw):
    for enc in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return raw.decode(enc), {"kind": "text", "pages": 0, "read_pages": 0,
                                     "page_chars": [], "empty_pages": [],
                                     "low_text_pages": [], "encoding": enc}
        except UnicodeDecodeError:
            continue
    raise ExtractError("这个文件不是 UTF-8 / GB18030 文本，读不出正文")


def extract_document(name, raw):
    """按文件头 / 扩展名抽正文。返回 (text, meta)。

    `meta` 里带页数、每页字数、哪些页没有文字层——前端靠它显示
    「PDF 20 页，抽了前 8 页」这种实话。
    """
    if not raw:
        raise ExtractError("文件是空的")
    if len(raw) > MAX_UPLOAD:
        raise ExtractError("文件 %.1fMB，超过 %dMB 上限"
                           % (len(raw) / 1048576, MAX_UPLOAD // 1048576))
    ext = os.path.splitext(name or "")[1].lower()
    head = raw[:8]
    known = (".pdf", ".docx", ".doc", ".txt", ".md", ".markdown", ".text")
    if ext in known:
        kind = ext
    elif not ext:
        # 没扩展名（QQ 存下来的「文件」常常没有）→ 按文件头认
        if head.startswith(b"%PDF"):
            kind = ".pdf"
        elif head.startswith(b"PK\x03\x04"):
            kind = ".docx"
        else:
            raise ExtractError("认不出这个文件的格式（没有扩展名，内容也不是 PDF / docx）")
    else:
        # .xlsx / .pptx 也是 PK 开头，绝不能光看文件头就当 Word
        raise ExtractError("只支持 txt / md / PDF / Word(.docx)，这个是 %s" % ext)
    if kind == ".pdf":
        text, meta = _extract_pdf(raw)
    elif kind == ".docx":
        text, meta = _extract_docx(raw)
    elif kind == ".doc":
        raise ExtractError("只支持 .docx；老版 .doc 先用 Word 另存为 .docx")
    else:
        text, meta = _extract_plain(raw)
    total = len(text)
    text, cut = _truncate(text)
    meta.update({"total_chars": total, "kept_chars": len(text),
                 "truncated": cut, "filename": name or ""})
    return text, meta


# ══════════════════════════════════════════════════════
# ② 大模型切分
# ══════════════════════════════════════════════════════
SPLIT_SYSTEM = """你是文档切分器。下面给你的正文是**完整的一篇文档**（通常 1 万字以内），
请先通读全文、看清结构，再切成若干语义完整的片段，供向量检索使用。

1. 每段目标 350 字左右，一般不要低于 250 字；硬上限 500 字（超过会被向量模型截断，尾部直接丢失）。
2. 只有在「主题确实变了」或「接着合并会超过 500 字」时才断开；同一个标题下讲同一件事的
   几段要合并成一块，不要一句话、两句话就切一刀。
3. 一段只讲一件事；不把两个主题塞进一段，也不把一句话拆到两段。
4. 原文照抄，不改写、不总结、不补内容。
5. 段落开头若是小标题，把标题留在该段第一行。

【表格必须整块保留——这是最重要的规则】
6. Markdown 表格（连续的以 | 开头、| 结尾的行，含 |---| 分隔行）是不可分割的整体：
   一张表格必须完整待在同一段里，绝不能把行拆到两段。哪怕表格超过 500 字也不要拆——
   宁可这一段超长，也不能让一张表格被腰斩成两截互不相干的碎片。
   表格前后的正文若并入会超 500 字，就让正文另起一段、表格单独成段。

【相邻段留重叠，保证上下文连贯】
7. 这不是硬切：相邻两段在交界处要有重叠。每一段开头的 10%~20%（最多 80 字）应是
   上一段末尾的原文句子，作为上下文引子——这样单独检索到任何一段都能读懂它在讲什么。
   第一段不加引子。引子必须原文照抄，不许改写。

8. 只输出 JSON 数组，不要任何解释、不要 Markdown 代码块。

输出格式：
[{"heading": "所属小节", "content": "片段原文（可含开头的重叠引子）", "why": "为什么在这里断开（≤20字）"}]
"""


def _split_batches(text, limit=BATCH_CHARS):
    """整篇文档按空行聚合成一批批，供 debug 展示；不再按句子/\n 硬切。

    2026-11-16 改：原来单段超 limit 会 re.split(r"(?<=[。！？；\\n])", b) 按
    \\n 再切一刀——Markdown 表格每行都以 \\n 结尾，长表格会被逐行切成多个 batch，
    每个 batch 单独送模型，表格就散了。现在切分整体交给大模型（见 split_with_llm
    整篇喂），这里只保留「按空行聚合」做展示，不做任何会切断表格的二次分割。
    """
    paras = [p for p in re.split(r"\n\s*\n", text) if p.strip()]
    batches, cur = [], ""
    for para in paras:
        if cur and len(cur) + len(para) + 2 > limit:
            batches.append(cur)
            cur = para
        else:
            cur = (cur + "\n\n" + para) if cur else para
    if cur:
        batches.append(cur)
    return batches or [text]


def _extract_json_array(reply):
    """从模型回复里抠出 JSON 数组。失败抛 ValueError（带结尾原文，便于排查）。"""
    text = (reply or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end <= start:
        raise ValueError("模型没有返回 JSON 数组，回复结尾：" + text[-160:])
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError as exc:
        raise ValueError("JSON 解析失败（%s），回复结尾：%s"
                         % (exc, text[-160:])) from exc
    if not isinstance(data, list):
        raise ValueError("返回的不是数组，回复结尾：" + text[-160:])
    return data


def _add_overlap(chunks, ratio=0.15, cap=80):
    """给每块补一段真正的字面重叠（上一块结尾），保证跨边界上下文不断。

    提示词里要求模型带引子，但模型常只做语义衔接、不照抄字面（实测相邻块
    相似度仅 0.1）。重叠是确定性机械操作、不涉及判断，所以代码兜底：基于
    **原始** chunks 算出每块的引子（避免滚雪球），再统一拼到 content 前。
    """
    if len(chunks) < 2:
        return chunks
    originals = [c["content"] for c in chunks]
    for i in range(1, len(chunks)):
        prev = originals[i - 1]
        n = min(cap, max(0, int(len(prev) * ratio)))
        if n < 8:                      # 上一块太短，补了没意义
            continue
        # 上一块若以表格结尾，引子不能抄表格行——否则下一块开头会冒出一行
        # 孤立的 |...|，看着像表格被拆了出去。从末尾剔除连续表格行，只从
        # 正文取引子；整块都是表格就不加。
        lines = prev.split("\n")
        end = len(lines)
        while end > 0 and lines[end - 1].strip().startswith("|"):
            end -= 1
        if end == 0:
            continue
        lead = "\n".join(lines[:end])[-n:]
        # 尽量从一个完整句子开头起，读起来是「上一句的延续」而非硬拼半句
        for mark in ("。", "！", "？", "；", "\n"):
            p = lead.find(mark)
            if 0 <= p < len(lead) - 8:
                lead = lead[p + 1:]
                break
        lead = lead.strip()
        if not lead:
            continue
        chunks[i]["overlap"] = lead
        chunks[i]["content"] = lead + "\n" + originals[i]
        chunks[i]["chars"] = len(chunks[i]["content"])
        chunks[i]["over"] = len(chunks[i]["content"]) > CHUNK_HARD_MAX
    return chunks


def _call_split_llm(batch, no, total):
    """单独一层是为了单测能直接 patch 掉，不打真模型。"""
    if total == 1:
        user = "以下是完整正文（共 %d 字），请通读后切分：\n\n%s" % (len(batch), batch)
    else:
        user = "以下是第 %d/%d 批正文（本批 %d 字）：\n\n%s" % (no, total, len(batch), batch)
    return llm.call_llm(
        [{"role": "system", "content": SPLIT_SYSTEM},
         {"role": "user", "content": user}],
        provider="deepseek", model=DEEPSEEK_MODEL, strict=True,
        max_tokens=8192, temperature=0.2, timeout=240,
    )


def split_with_llm(text):
    """整篇交给大模型做语义切分。返回 (chunks, batches_debug)。

    2026-11-16 改：1 万字正文约 8000 token，DeepSeek 完全吃得下。以前先按
    BATCH_CHARS=2400 物理分批、每批单独送模型，长表格在分批时就被拦腰切断，
    模型拿到残缺片段没法还原。    现在整篇一次喂（batches 只作展示用），表格和
    上下文由模型看全貌后自己判断边界。
    """
    batches = _split_batches(text)  # 仅用于前端展示「原文分几段」，不参与送模型
    total = len(batches)
    chunks, debug = [], []
    started = time.time()
    reply = _call_split_llm(text, 1, 1)
    secs = round(time.time() - started, 2)
    try:
        raw = _extract_json_array(reply)
        err = ""
    except ValueError as exc:
        raw, err = [], str(exc)
    debug.append({
        "no": 1, "chars": len(text), "secs": secs,
        "reply_chars": len(reply or ""), "error": err,
        "raw_tail": (reply or "")[-600:],
        "count": len(raw), "whole": True,
    })
    for idx, item in enumerate(raw, 1):
        if not isinstance(item, dict):
            continue
        content = str(item.get("content") or "").strip()
        if not content:
            continue
        chunks.append({
            "id": "b1_%02d" % idx,
            "heading": str(item.get("heading") or "").strip(),
            "content": content,
            "chars": len(content),
            "why": str(item.get("why") or "").strip(),
            # 只标事实：超了就是超了，改不改由页面告诉用户，不替模型重切
            "over": len(content) > CHUNK_HARD_MAX,
        })
    return _add_overlap(chunks), debug


# ══════════════════════════════════════════════════════
# ③ 检索 / ④ 回答
# ══════════════════════════════════════════════════════
def _hits_of(rec, question, k=4):
    # 候选池放大给重排挑：向量召回粗排 + MMR 先给一批，cross-encoder 再精排
    # 取 top k。模型没就绪时重排原样返回，行为与旧的「直接取前 k」一致。
    pool = max(k * 4, 12)
    res = _client().search(rec["kb"], question, k=pool)
    if not res.get("ok"):
        raise RuntimeError(res.get("error") or "检索失败")
    out = []
    for doc in (res.get("docs") or []):
        dist = float(doc.get("distance") or 0.0)
        out.append({
            "id": doc.get("id") or "",
            "content": (doc.get("content") or "")[:800],
            "chars": len(doc.get("content") or ""),
            "heading": (doc.get("metadata") or {}).get("heading", ""),
            "distance": round(dist, 4),
            "score": round(1.0 - dist, 4),
        })
    out = rerank.rerank(question, out, top_n=k)   # 精排后重新编号，[n] 引用才对得上
    for i, hit in enumerate(out, 1):
        hit["no"] = i
    return out


ANSWER_SYSTEM = """你在回答用户关于一份文档的问题。

- 只能依据下面给出的资料作答；资料里没有的，直接说「文档里没有提到」，不要猜。
- 每条论断后面用 [1] [2] 标注来源编号。
- 用中文，讲清楚，不要罗列无意义的条目。"""


def _build_answer_prompt(question, hits):
    lines = []
    for h in hits:
        head = ("· " + h["heading"]) if h.get("heading") else ""
        lines.append("[%d]%s\n%s" % (h["no"], head, h["content"]))
    return ("资料：\n\n" + "\n\n".join(lines) +
            "\n\n问题：%s" % question)


# ══════════════════════════════════════════════════════
# 路由
# ══════════════════════════════════════════════════════
@bp.post("/api/raglab/extract")
def api_extract():
    """PDF / Word / 文本 → 纯正文。前端拿到后走同一条 `clean` 管道。

    为什么不在浏览器里读：PDF 和 docx 都是二进制，浏览器只能读成乱码；
    而且「这页有没有文字层」得解析过才知道，正好一起在这儿判。
    """
    body = request.get_json(silent=True) or {}
    name = str(body.get("name") or "").strip()
    data = str(body.get("data") or "")
    if not data:
        return jsonify({"ok": False, "error": "没有收到文件内容"}), 400
    try:
        raw = base64.b64decode(data.split(",", 1)[-1], validate=False)
    except (binascii.Error, ValueError):
        return jsonify({"ok": False, "error": "文件内容读不出来（base64 解析失败）"}), 400
    started = time.time()
    try:
        text, meta = extract_document(name, raw)
    except ExtractError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except ImportError as exc:
        # 部署环境没装 pypdf / python-docx 时，别把 ImportError 抛成 500 白屏
        log.warning("rag_lab：缺解析依赖 %s", exc)
        return jsonify({"ok": False,
                        "error": "服务器没装解析依赖（%s），先 pip install pypdf python-docx"
                                 % (getattr(exc, "name", "") or exc)}), 503
    except Exception as exc:                        # noqa: BLE001
        log.warning("rag_lab：抽取失败 %s", exc)
        return jsonify({"ok": False, "error": "抽取失败：%s" % exc}), 500
    if not text.strip():
        return jsonify({"ok": False, "error": "抽出来的正文是空的"}), 400
    return jsonify({"ok": True, "text": text, "meta": meta,
                    "filename": name or ("未命名." + meta.get("kind", "txt")),
                    "secs": round(time.time() - started, 2),
                    "limits": {"max_chars": MAX_CHARS, "max_pages": MAX_PAGES,
                               "max_upload_mb": MAX_UPLOAD // 1048576,
                               "scan_page_chars": SCAN_PAGE_CHARS}})


@bp.post("/api/raglab/clean")
def api_clean():
    body = request.get_json(silent=True) or {}
    raw = str(body.get("text") or "")
    filename = str(body.get("filename") or "").strip()
    if not raw.strip():
        return jsonify({"ok": False, "error": "正文是空的"}), 400
    if len(raw) > MAX_CHARS:
        return jsonify({"ok": False,
                        "error": "演示只支持 %d 字以内（这份 %d 字）"
                                 % (MAX_CHARS, len(raw))}), 400
    started = time.time()
    text, report = clean_text(raw)
    sid, rec = _new_session(filename)
    data = _load()
    if sid in data["sessions"]:
        data["sessions"][sid]["chars"] = len(text)
        _save(data)
    return jsonify({
        "ok": True, "sid": sid, "kb": rec["kb"], "filename": rec["filename"],
        "text": text, "report": report,
        "secs": round(time.time() - started, 3),
        "limits": {"max_chars": MAX_CHARS, "chunk_target": list(CHUNK_TARGET),
                   "hard_max": CHUNK_HARD_MAX, "max_chunks": MAX_CHUNKS,
                   "max_libs": MAX_LIBS, "ttl": TTL_SECONDS},
    })


@bp.post("/api/raglab/split")
def api_split():
    body = request.get_json(silent=True) or {}
    text = str(body.get("text") or "").strip()
    sid = str(body.get("sid") or "").strip()
    if not text:
        return jsonify({"ok": False, "error": "正文是空的"}), 400
    if len(text) > MAX_CHARS:
        return jsonify({"ok": False, "error": "正文超过 %d 字" % MAX_CHARS}), 400
    started = time.time()
    try:
        chunks, batches = split_with_llm(text)
    except Exception as exc:                            # noqa: BLE001
        log.warning("rag_lab：切分失败 %s", exc)
        return jsonify({"ok": False, "error": "切分失败：%s" % exc}), 502
    if len(chunks) > MAX_CHUNKS:
        return jsonify({"ok": False,
                        "error": "切出 %d 块，超过上限 %d（换小一点的文档）"
                                 % (len(chunks), MAX_CHUNKS)}), 400
    if sid:
        data = _load()
        if sid in data["sessions"]:
            data["sessions"][sid]["chunks"] = len(chunks)
            data["sessions"][sid]["last"] = _now()
            _save(data)
    return jsonify({
        "ok": True, "chunks": chunks, "batches": batches,
        "prompt": SPLIT_SYSTEM,
        "secs": round(time.time() - started, 2),
        "stats": {
            "count": len(chunks),
            "chars": sum(c["chars"] for c in chunks),
            "over": sum(1 for c in chunks if c["over"]),
            "avg": round(sum(c["chars"] for c in chunks) / len(chunks), 1) if chunks else 0,
        },
    })


@bp.post("/api/raglab/ingest")
def api_ingest():
    body = request.get_json(silent=True) or {}
    sid = str(body.get("sid") or "").strip()
    chunks = body.get("chunks") or []
    rec = _touch(sid)
    if rec is None:
        return jsonify({"ok": False, "error": "会话已过期，重新上传文档"}), 404
    if not chunks:
        return jsonify({"ok": False, "error": "没有要入库的块"}), 400
    if len(chunks) > MAX_CHUNKS:
        return jsonify({"ok": False, "error": "块数超过上限"}), 400

    entries = []
    for i, c in enumerate(chunks[:MAX_CHUNKS], 1):
        content = str(c.get("content") or "").strip()
        if not content:
            continue
        entries.append({
            "id": "%s_%s" % (sid, c.get("id") or i),
            "content": content,
            "metadata": {
                "source": rec.get("filename") or "",
                "heading": str(c.get("heading") or "")[:80],
                "idx": i,
                "chars": len(content),
            },
        })
    started = time.time()
    res = _client().ingest(rec["kb"], entries)
    secs = round(time.time() - started, 2)
    if not res.get("ok"):
        return jsonify({"ok": False, "error": res.get("error") or "入库失败"}), 502
    data = _load()
    if sid in data["sessions"]:
        data["sessions"][sid]["ingested"] = True
        data["sessions"][sid]["chunks"] = len(entries)
        data["sessions"][sid]["last"] = _now()
        _save(data)
    return jsonify({
        "ok": True, "kb": rec["kb"], "count": res.get("count", 0),
        "dim": res.get("dim", 0), "preview": res.get("preview") or [],
        "model": LAB_EMBED_MODEL, "secs": secs,
    })


@bp.post("/api/raglab/query")
def api_query():
    body = request.get_json(silent=True) or {}
    sid = str(body.get("sid") or "").strip()
    question = str(body.get("question") or "").strip()
    k = int(body.get("k") or 4)
    rec = _touch(sid)
    if rec is None:
        return jsonify({"ok": False, "error": "会话已过期"}), 404
    if not question:
        return jsonify({"ok": False, "error": "问题是空的"}), 400
    if not rec.get("ingested"):
        return jsonify({"ok": False, "error": "文档还没入库"}), 400
    if not _rate_ok(sid):
        return jsonify({"ok": False, "error": "问得太快了，喘口气再来"}), 429
    started = time.time()
    try:
        hits = _hits_of(rec, question, k=max(1, min(k, 10)))
    except Exception as exc:                            # noqa: BLE001
        return jsonify({"ok": False, "error": "检索失败：%s" % exc}), 502
    return jsonify({"ok": True, "hits": hits,
                    "secs": round(time.time() - started, 3)})


@bp.post("/api/raglab/answer")
def api_answer():
    body = request.get_json(silent=True) or {}
    sid = str(body.get("sid") or "").strip()
    question = str(body.get("question") or "").strip()
    k = int(body.get("k") or 4)
    rec = _touch(sid)
    if rec is None:
        return jsonify({"ok": False, "error": "会话已过期"}), 404
    if not question:
        return jsonify({"ok": False, "error": "问题是空的"}), 400
    if not rec.get("ingested"):
        return jsonify({"ok": False, "error": "文档还没入库"}), 400
    if not _rate_ok(sid):
        return jsonify({"ok": False, "error": "问得太快了，喘口气再来"}), 429

    started = time.time()
    try:
        hits = _hits_of(rec, question, k=max(1, min(k, 10)))
    except Exception as exc:                            # noqa: BLE001
        return jsonify({"ok": False, "error": "检索失败：%s" % exc}), 502
    if not hits:
        return jsonify({"ok": True, "hits": [], "answer": "文档里没有检索到相关内容。",
                        "prompt": "", "model": DEEPSEEK_MODEL, "secs": round(time.time() - started, 2),
                        "usage": {}})

    prompt = _build_answer_prompt(question, hits)
    try:
        answer = llm.call_llm(
            [{"role": "system", "content": ANSWER_SYSTEM},
             {"role": "user", "content": prompt}],
            provider="deepseek", model=DEEPSEEK_MODEL, strict=True,
            max_tokens=MAX_ANSWER_TOKENS, temperature=0.3, timeout=120,
        )
    except Exception as exc:                            # noqa: BLE001
        log.warning("rag_lab：回答失败 %s", exc)
        return jsonify({"ok": False, "error": "模型调用失败：%s" % exc}), 502

    try:
        usage = llm.current_usage() or {}
    except Exception:                                   # noqa: BLE001
        usage = {}
    return jsonify({"ok": True, "hits": hits, "answer": answer,
                    "prompt": prompt, "system": ANSWER_SYSTEM,
                    "model": DEEPSEEK_MODEL, "usage": usage,
                    "secs": round(time.time() - started, 2)})


@bp.post("/api/raglab/reset")
def api_reset():
    body = request.get_json(silent=True) or {}
    sid = str(body.get("sid") or "").strip()
    if not sid:
        return jsonify({"ok": False, "error": "缺少 sid"}), 400
    data = _load()
    existed = sid in data["sessions"]
    _drop(data["sessions"], sid)
    _save(data)
    _HITS.pop(sid, None)
    return jsonify({"ok": True, "dropped": existed})


_WARM_LOCK = threading.Lock()
_WARM_ONCE = [False]


def _warm_bg():
    try:
        _client()
        log.info("rag_lab：预热完成，向量 daemon 已就绪")
    except Exception as exc:                            # noqa: BLE001
        log.warning("rag_lab：预热失败（%s）", exc)


@bp.post("/api/raglab/warm")
def api_warm():
    """页面打开就先把模型拉起来。

    实测冷启动 12.2 秒（`RagClient.start()` + sentence-transformers 导入），
    等用户真点上传再加载，第一步就要干等。这里起一个后台线程，接口立刻返回，
    用户读说明、选文件的这段时间正好覆盖掉。
    """
    with _WARM_LOCK:
        if not _WARM_ONCE[0]:
            _WARM_ONCE[0] = True
            threading.Thread(target=_warm_bg, daemon=True).start()
    ready = bool(_CLIENT is not None and _CLIENT.is_ready())
    return jsonify({"ok": True, "ready": ready, "model": LAB_EMBED_MODEL})


@bp.get("/api/raglab/state")
def api_state():
    """页面刷新后用 sid 认回自己的库；会话没了就返回 ok=false。"""
    sid = str(request.args.get("sid") or "").strip()
    if not sid:
        return jsonify({"ok": False, "error": "缺少 sid"}), 400
    data = _gc()
    rec = data["sessions"].get(sid)
    if not rec:
        return jsonify({"ok": False, "error": "会话已过期"})
    return jsonify({"ok": True, "sid": sid, "kb": rec["kb"],
                    "filename": rec["filename"], "chars": rec.get("chars", 0),
                    "chunks": rec.get("chunks", 0),
                    "ingested": bool(rec.get("ingested")),
                    "ttl": TTL_SECONDS})
