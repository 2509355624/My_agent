# -*- coding: utf-8 -*-
"""文档问答实验室（`app/rag_lab.py`）的守卫测试（2026-11-15）。

这里最容易悄悄坏掉的是**展示出来的数字**：清洗删了几行、切了几块、每块多少字。
数字错了，页面上看着一切正常，面试时被人一问就露馅。所以全部用可复算的
小样本文本，逐项对数字。

不调真模型、不起向量 daemon、不写生产 state：
- `_call_split_llm` 一律 patch；
- `_client` 换成假对象（只实现 ingest / search / delete_kb）；
- `_STATE` 指到临时目录。
"""
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

import flask

from app import rag_lab

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLE = os.path.join(ROOT, "web", "raglab-sample.md")
MAIN = os.path.join(ROOT, "app", "main.py")


class FakeClient:
    def __init__(self):
        self.ingested = []
        self.deleted = []
        self.docs = [
            {"id": "demo_1_b1_01", "content": "大大怪跑在单台工作站上。",
             "metadata": {"heading": "系统概览"}, "distance": 0.23},
            {"id": "demo_1_b1_02", "content": "生图链路串行执行。",
             "metadata": {"heading": "生图链路"}, "distance": 0.41},
        ]

    def ingest(self, kb, entries):
        self.ingested.append((kb, entries))
        return {"ok": True, "count": len(entries), "dim": 512,
                "preview": [{"dim": 512, "norm": 1.0, "head": [0.1] * 8}
                            for _ in entries]}

    def search(self, kb, query, k=4):
        return {"ok": True, "docs": self.docs[:k]}

    def delete_kb(self, kb):
        self.deleted.append(kb)
        return {"ok": True, "deleted": True}


class LabBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="raglab_")
        self._old_state = rag_lab._STATE
        rag_lab._STATE = os.path.join(self.tmp, "sessions.json")
        rag_lab._HITS.clear()
        self.client = FakeClient()
        self.patches = [
            mock.patch.object(rag_lab, "_client", return_value=self.client),
            mock.patch.object(rag_lab, "_CLIENT", None),
        ]
        for p in self.patches:
            p.start()
        app = flask.Flask(__name__)
        app.register_blueprint(rag_lab.bp)
        self.c = app.test_client()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        rag_lab._STATE = self._old_state
        shutil.rmtree(self.tmp, ignore_errors=True)

    def post(self, path, body):
        return self.c.post(path, json=body)

    def make_session(self, text="正文。" * 30):
        r = self.post("/api/raglab/clean", {"text": text, "filename": "a.md"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return r.get_json()


class CleanTest(LabBase):
    """清洗只做确定性的事，删了什么必须数得出来。"""

    def test_removes_page_numbers_and_repeated_headers(self):
        raw = "\n".join([
            "页眉公司",
            "第一段正文，讲一件事。",
            "12",
            "第二段正文，讲另一件事。",
            "页眉公司",
            "- 3 -",
            "第三段正文。",
            "页眉公司",
            "",
            "",
            "第四段正文。",
        ])
        text, report = rag_lab.clean_text(raw)
        self.assertEqual(report["removed"]["page"], 2)      # "12" 与 "- 3 -"
        self.assertEqual(report["removed"]["repeat"], 3)    # 三行「页眉公司」
        self.assertNotIn("页眉公司", text)
        self.assertNotIn("\n\n\n", text)
        self.assertIn("第四段正文。", text)
        self.assertTrue(all(isinstance(s["line"], str) for s in report["samples"]))
        self.assertEqual(report["raw_chars"] - report["clean_chars"],
                         report["drop_chars"])

    def test_banner_title_at_top_is_kept_once(self):
        """只出现一次的长标题不能被当成页眉删掉。"""
        raw = "大大怪技术白皮书 · 内部资料 v1.2\n\n正文开始。"
        text, report = rag_lab.clean_text(raw)
        self.assertIn("大大怪技术白皮书", text)
        self.assertEqual(report["removed"]["repeat"], 0)

    def test_zero_width_and_trailing_space(self):
        raw = "第一行\u200b\n第二行   \n"
        text, report = rag_lab.clean_text(raw)
        self.assertEqual(report["removed"]["zero_width"], 1)
        self.assertGreaterEqual(report["removed"]["trimmed"], 1)
        self.assertNotIn("\u200b", text)

    def test_empty_input(self):
        text, report = rag_lab.clean_text("")
        self.assertEqual(text, "")
        self.assertEqual(report["raw_chars"], 0)


class BatchTest(unittest.TestCase):
    """分批是为了让模型输出不被截断，切分点必须在段落边界上。"""

    def test_batches_respect_limit_and_keep_every_paragraph(self):
        paras = ["第%d段。" % i + "内容" * 120 for i in range(12)]
        text = "\n\n".join(paras)
        batches = rag_lab._split_batches(text, 800)
        self.assertGreater(len(batches), 1)
        for b in batches:
            self.assertLessEqual(len(b), 800 * 1.6)
        joined = "\n\n".join(batches)
        for p in paras:
            self.assertIn(p, joined)

    def test_no_blank_line_falls_back_to_sentence_split(self):
        text = "这是一句话。" * 400
        batches = rag_lab._split_batches(text, 500)
        self.assertGreater(len(batches), 1)
        self.assertEqual("".join(batches).count("。"), 400)


class JsonTest(unittest.TestCase):
    def test_parses_fenced_json(self):
        reply = '```json\n[{"heading": "a", "content": "x"}]\n```'
        self.assertEqual(rag_lab._extract_json_array(reply)[0]["content"], "x")

    def test_parses_json_with_leading_talk(self):
        reply = '好的，切分结果如下：\n[{"content": "y"}]\n以上。'
        self.assertEqual(rag_lab._extract_json_array(reply)[0]["content"], "y")

    def test_raises_with_reply_tail(self):
        with self.assertRaises(ValueError) as ctx:
            rag_lab._extract_json_array("抱歉，我不能做这个")
        self.assertIn("抱歉", str(ctx.exception))


class SplitTest(LabBase):
    def test_chunks_carry_facts_not_judgements(self):
        reply = json.dumps([
            {"heading": "一", "content": "短段落。", "why": "主题结束"},
            {"heading": "二", "content": "长" * 620, "why": "超了"},
            {"heading": "三", "content": "   ", "why": "空块应被丢弃"},
        ], ensure_ascii=False)
        with mock.patch.object(rag_lab, "_call_split_llm", return_value=reply):
            r = self.post("/api/raglab/split", {"text": "正文" * 300})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        data = r.get_json()
        self.assertEqual(data["stats"]["count"], 2)          # 空块丢了
        self.assertEqual(data["stats"]["over"], 1)           # 620 字被标超限
        self.assertIn("JSON", data["prompt"])
        self.assertEqual(len(data["chunks"][0]["id"]), 5)    # b1_01

    def test_llm_error_is_reported_not_swallowed(self):
        with mock.patch.object(rag_lab, "_call_split_llm",
                               side_effect=RuntimeError("boom")):
            r = self.post("/api/raglab/split", {"text": "正文" * 100})
        self.assertEqual(r.status_code, 502)
        self.assertIn("boom", r.get_json()["error"])


class LimitsTest(LabBase):
    def test_clean_rejects_document_over_limit(self):
        r = self.post("/api/raglab/clean", {"text": "字" * (rag_lab.MAX_CHARS + 1)})
        self.assertEqual(r.status_code, 400)
        self.assertIn(str(rag_lab.MAX_CHARS), r.get_json()["error"])

    def test_empty_text_rejected(self):
        self.assertEqual(self.post("/api/raglab/clean", {"text": "  "}).status_code, 400)

    def test_too_many_chunks_rejected(self):
        chunks = [{"id": "c%d" % i, "content": "内容"} for i in range(rag_lab.MAX_CHUNKS + 1)]
        reply = json.dumps([{"content": "内容"} for _ in chunks], ensure_ascii=False)
        with mock.patch.object(rag_lab, "_call_split_llm", return_value=reply):
            r = self.post("/api/raglab/split", {"text": "正文" * 300})
        self.assertEqual(r.status_code, 400)
        self.assertIn("上限", r.get_json()["error"])


class FlowTest(LabBase):
    """完整四步：clean → ingest → query → answer。"""

    def test_end_to_end_numbers(self):
        s = self.make_session("第一段。\n\n12\n\n第二段。")
        sid = s["sid"]
        self.assertTrue(s["kb"].startswith("demo_"))

        r = self.post("/api/raglab/ingest", {
            "sid": sid,
            "chunks": [{"id": "b1_01", "content": "第一段。", "heading": "甲"},
                       {"id": "b1_02", "content": "第二段。", "heading": "乙"}],
        })
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        ing = r.get_json()
        self.assertEqual(ing["count"], 2)
        self.assertEqual(ing["dim"], 512)
        self.assertEqual(len(ing["preview"]), 2)

        r = self.post("/api/raglab/query", {"sid": sid, "question": "讲了什么", "k": 2})
        self.assertEqual(r.status_code, 200)
        q = r.get_json()
        self.assertEqual(len(q["hits"]), 2)
        self.assertAlmostEqual(q["hits"][0]["score"], 0.77, places=3)

    def test_answer_includes_prompt_and_hits(self):
        s = self.make_session()
        sid = s["sid"]
        self.post("/api/raglab/ingest", {"sid": sid, "chunks": [{"id": "b1_01", "content": "正文。"}]})
        with mock.patch.object(rag_lab.llm, "call_llm", return_value="答案 [1]") as call:
            r = self.post("/api/raglab/answer", {"sid": sid, "question": "问题", "k": 2})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        data = r.get_json()
        self.assertEqual(data["answer"], "答案 [1]")
        self.assertIn("[1]", data["prompt"])
        self.assertIn("问题", data["prompt"])
        kwargs = call.call_args.kwargs
        self.assertEqual(kwargs["provider"], "deepseek")
        self.assertTrue(kwargs["strict"])

    def test_query_before_ingest_is_rejected(self):
        s = self.make_session()
        r = self.post("/api/raglab/query", {"sid": s["sid"], "question": "x"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("入库", r.get_json()["error"])

    def test_unknown_session(self):
        r = self.post("/api/raglab/query", {"sid": "nope", "question": "x"})
        self.assertEqual(r.status_code, 404)

    def test_reset_drops_the_library(self):
        s = self.make_session()
        r = self.post("/api/raglab/reset", {"sid": s["sid"]})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["dropped"])
        self.assertIn(s["kb"], self.client.deleted)


class CapTest(LabBase):
    """最多 3 个临时库，第 4 个进来要淘汰最久没用的那个。"""

    def test_oldest_session_is_evicted(self):
        sids = []
        base = rag_lab._now() - 100          # 都没过期，只是先后不同
        for i in range(rag_lab.MAX_LIBS + 1):
            s = self.make_session("第%d份。" % i)
            sids.append(s["sid"])
            data = rag_lab._load()
            data["sessions"][s["sid"]]["last"] = base + i
            rag_lab._save(data)
        rag_lab._gc()
        data = rag_lab._load()
        self.assertEqual(len(data["sessions"]), rag_lab.MAX_LIBS)
        self.assertNotIn(sids[0], data["sessions"])
        self.assertIn("demo_" + sids[0], self.client.deleted)


class SampleDocTest(unittest.TestCase):
    """示例文档是演示的门面：太长会被限流拒，太干净则清洗步骤没东西可看。"""

    def test_sample_exists_and_is_under_limit(self):
        self.assertTrue(os.path.exists(SAMPLE))
        with open(SAMPLE, encoding="utf-8") as f:
            text = f.read()
        self.assertLessEqual(len(text), rag_lab.MAX_CHARS)
        self.assertGreater(len(text), 2000)
        self.assertGreaterEqual(text.count("第 1 页"), 1)
        self.assertGreaterEqual(text.count("大大怪技术白皮书"), 3)
        text_clean, report = rag_lab.clean_text(text)
        self.assertGreaterEqual(report["removed"]["page"], 8)
        self.assertGreaterEqual(report["removed"]["repeat"], 8)
        self.assertIn("系统概览", text_clean)


class WiringTest(unittest.TestCase):
    """公网入口少一处注册，访客就会看到 404。"""

    def test_blueprint_registered_and_whitelisted(self):
        with open(MAIN, encoding="utf-8") as f:
            src = f.read()
        self.assertIn("rag_lab.bp", src)
        self.assertIn("/api/raglab/", src)


SHOWCASE = os.path.join(ROOT, "web", "showcase.html")
FRIEND = os.path.join(ROOT, "friend_app.py")


class PageTest(unittest.TestCase):
    """展示页改成标签页之后，最怕的是「点了没反应」和「切过去一片空白」。"""

    @classmethod
    def setUpClass(cls):
        with open(SHOWCASE, encoding="utf-8") as f:
            cls.src = f.read()

    def test_six_tabs_declared_and_matched_by_sections(self):
        import re
        tabs = re.search(r"const TABS = \[(.*?)\];", self.src, re.S).group(1)
        ids = re.findall(r'"([a-z]+)"', tabs)
        self.assertEqual(ids, ["image", "roleplay", "memory", "comic", "chain", "rag"])
        for tid in ids:
            self.assertIn('id="%s"' % tid, self.src)
            self.assertIn('href="#%s"' % tid, self.src)

    def test_every_section_has_a_tab(self):
        import re
        sections = re.findall(r'<section class="feature" id="([a-z]+)"', self.src)
        self.assertEqual(sorted(sections),
                         sorted(["image", "roleplay", "memory", "comic", "chain", "rag"]))

    def test_rag_panel_has_every_hook_the_script_uses(self):
        import re
        tail = self.src[self.src.rfind("<script>"):]
        used = set(re.findall(r'\$\("([A-Za-z]+)"\)', tail)) | \
               set(re.findall(r'getElementById\("([A-Za-z]+)"\)', tail))
        declared = set(re.findall(r'id="([A-Za-z]+)"', self.src))
        self.assertEqual(sorted(used - declared), [])

    def test_tags_are_balanced(self):
        for tag in ("section", "div", "aside", "form", "article"):
            self.assertEqual(self.src.count("<" + tag), self.src.count("</" + tag + ">"), tag)

    def test_sample_is_served_by_the_gateway(self):
        """网关只反代 /api/*，示例文档必须单独给静态路由，否则手机端 404。"""
        with open(FRIEND, encoding="utf-8") as f:
            src = f.read()
        self.assertIn('"/raglab-sample.md"', src)
        self.assertIn('"/api/raglab/<path:sub>"', src)
        self.assertIn('"raglab": (60, 600)', src)
        self.assertIn('timeout = 300 if sub in ("split", "answer")', src)


def make_pdf(pages):
    """手搓一个最小 PDF（每页一行 Helvetica 文本）。

    不引第三方写库：测的是**读**，只要文件结构合法就够。空字符串页 = 没有
    文字层的扫描页，用来测「抽不出正文时如实报错」。
    """
    n = len(pages)
    page_ids = [4 + 2 * i for i in range(n)]
    objs = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: ("<< /Type /Pages /Kids [%s] /Count %d >>"
            % (" ".join("%d 0 R" % p for p in page_ids), n)).encode(),
        3: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    }
    for i, text in enumerate(pages):
        pid, cid = page_ids[i], 5 + 2 * i
        objs[pid] = ("<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
                     "/Resources << /Font << /F1 3 0 R >> >> /Contents %d 0 R >>"
                     % cid).encode()
        stream = ("BT /F1 12 Tf 72 720 Td (%s) Tj ET"
                  % text.replace("\\", r"\\").replace("(", r"\(")
                        .replace(")", r"\)")).encode("latin-1", "replace")
        objs[cid] = (b"<< /Length " + str(len(stream)).encode()
                     + b" >>\nstream\n" + stream + b"\nendstream")
    out, offsets = bytearray(b"%PDF-1.4\n"), {}
    for oid in sorted(objs):
        offsets[oid] = len(out)
        out += ("%d 0 obj\n" % oid).encode() + objs[oid] + b"\nendobj\n"
    xref_at, maxid = len(out), max(objs) + 1
    out += ("xref\n0 %d\n" % maxid).encode() + b"0000000000 65535 f \n"
    for oid in range(1, maxid):
        out += (("%010d 00000 n \n" % offsets[oid]).encode() if oid in offsets
                else b"0000000000 65535 f \n")
    out += ("trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n"
            % (maxid, xref_at)).encode()
    return bytes(out)


class ExtractTest(LabBase):
    """PDF / Word 只读**文本层**（2026-11-15 用户拍板先做 A 档）。

    这条路的全部价值在于「抽不到就明说」：不 OCR、不猜、不拿图片硬凑，
    否则演示页会拿一堆空内容去切块，看着跑通了其实什么都没有。
    """

    def _extract(self, name, raw):
        import base64
        return self.post("/api/raglab/extract",
                         {"name": name, "data": base64.b64encode(raw).decode()})

    def test_pdf_text_layer(self):
        pdf = make_pdf(["Hello RAG page one", "second page text"])
        r = self._extract("doc.pdf", pdf)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        d = r.get_json()
        self.assertIn("Hello RAG page one", d["text"])
        self.assertIn("second page text", d["text"])
        self.assertEqual(d["meta"]["kind"], "pdf")
        self.assertEqual(d["meta"]["pages"], 2)
        self.assertEqual(d["meta"]["empty_pages"], [])
        self.assertFalse(d["meta"]["truncated"])

    def test_scanned_pdf_is_reported_not_faked(self):
        """空白页 = 没有文字层。必须报错，不能回一段空正文让后面全跑空。"""
        r = self._extract("scan.pdf", make_pdf(["", ""]))
        self.assertEqual(r.status_code, 400)
        self.assertIn("扫描件", r.get_json()["error"])

    def test_docx_keeps_heading_and_table_order(self):
        import docx
        import io
        doc = docx.Document()
        doc.add_heading("第一章 概览", level=1)
        doc.add_paragraph("大大怪跑在单台工作站上。")
        tbl = doc.add_table(rows=2, cols=2)
        tbl.cell(0, 0).text, tbl.cell(0, 1).text = "指标", "值"
        tbl.cell(1, 0).text, tbl.cell(1, 1).text = "延迟", "1.2s"
        doc.add_paragraph("表格之后的一段。")
        buf = io.BytesIO()
        doc.save(buf)
        d = self._extract("a.docx", buf.getvalue()).get_json()
        self.assertEqual(d["meta"]["kind"], "docx")
        self.assertEqual(d["meta"]["tables"], 1)
        text = d["text"]
        self.assertIn("# 第一章 概览", text)          # 标题层级保留，切块时有用
        self.assertIn("指标 | 值", text)
        # 表格必须夹在两段之间，不能被甩到文末
        self.assertLess(text.index("单台工作站"), text.index("指标 | 值"))
        self.assertLess(text.index("指标 | 值"), text.index("表格之后"))

    def test_long_text_is_truncated_at_paragraph_break(self):
        raw = ("第一段落内容。\n" * 2000).encode("utf-8")
        d = self._extract("long.txt", raw).get_json()
        self.assertTrue(d["meta"]["truncated"])
        self.assertLessEqual(len(d["text"]), rag_lab.MAX_CHARS)
        self.assertGreater(len(d["text"]), rag_lab.MAX_CHARS * 0.6)
        self.assertFalse(d["text"].endswith("第一段"))   # 断在换行处，不留半句

    def test_old_doc_points_at_save_as(self):
        r = self._extract("old.doc", b"\xd0\xcf\x11\xe0abcdef")
        self.assertEqual(r.status_code, 400)
        self.assertIn("另存为", r.get_json()["error"])

    def test_unsupported_extension(self):
        r = self._extract("sheet.xlsx", b"PK\x03\x04whatever")
        self.assertEqual(r.status_code, 400)
        self.assertIn("只支持", r.get_json()["error"])

    def test_missing_payload(self):
        r = self.post("/api/raglab/extract", {"name": "a.pdf"})
        self.assertEqual(r.status_code, 400)


class ExtractPageTest(unittest.TestCase):
    """页面侧：文件选择框要真的放开 pdf/docx，且必须走 extract 接口。"""

    def setUp(self):
        with open(os.path.join(ROOT, "web", "showcase.html"), encoding="utf-8") as f:
            self.src = f.read()

    def test_accepts_pdf_and_docx(self):
        self.assertIn('accept=".txt,.md,.markdown,.pdf,.docx', self.src)

    def test_binary_goes_through_extract(self):
        self.assertIn("/api/raglab/extract", self.src)
        self.assertIn("readAsDataURL", self.src)       # 二进制转 base64
        self.assertIn("function clipText", self.src)   # 1 万字截断

    def test_scanned_hint_is_shown(self):
        """页面上得写清楚「扫描件抽不出字」，否则用户以为是 bug。"""
        self.assertIn("扫描件", self.src)


if __name__ == "__main__":
    unittest.main()
