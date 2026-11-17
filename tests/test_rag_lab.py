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

from app.features.rag import lab as rag_lab

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
    """分批只按空行聚合做展示，绝不按句子/\\n 二次硬切（会切碎 Markdown 表格）。"""

    def test_batches_respect_limit_and_keep_every_paragraph(self):
        paras = ["第%d段。" % i + "内容" * 120 for i in range(12)]
        text = "\n\n".join(paras)
        batches = rag_lab._split_batches(text, 800)
        self.assertGreater(len(batches), 1)
        joined = "\n\n".join(batches)
        for p in paras:
            self.assertIn(p, joined)

    def test_long_table_row_is_never_split_across_batches(self):
        # 一张 12 行的 Markdown 表格，整体作为一个段落，不该被 \n 切散
        rows = ["| 指标 | 值 |", "| --- | --- |"] + \
               ["| 第%d项 | %s |" % (i, "x" * 60) for i in range(10)]
        table = "\n".join(rows)
        text = "开头一段。\n\n" + table + "\n\n结尾一段。"
        batches = rag_lab._split_batches(text, 100)
        # 每一行都完整地待在同一个 batch 里
        for row in rows:
            owners = [b for b in batches if row in b]
            self.assertEqual(len(owners), 1, "表格行 %r 被切散/重复了" % row)
        # 且整张表格没被拆到两个 batch
        self.assertEqual(sum(1 for b in batches if "| 指标 | 值 |" in b), 1)


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

    def test_whole_document_sent_to_llm_once(self):
        # 1 万字内的文档整篇一次送模型，不再按 BATCH_CHARS 物理分批
        text = "这是一段有表格的正文。" * 200  # 2400+ 字，旧实现会切成 2 批
        reply = json.dumps([{"heading": "甲", "content": "完整一段。", "why": "语义完整"}],
                           ensure_ascii=False)
        with mock.patch.object(rag_lab, "_call_split_llm", return_value=reply) as call:
            r = self.post("/api/raglab/split", {"text": text})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(call.call_count, 1)          # 只调一次
        self.assertEqual(call.call_args[0][0], text)  # 且送进去的是完整原文
        self.assertTrue(r.get_json()["batches"][0].get("whole"))

    def test_split_prompt_keeps_table_whole_and_requires_overlap(self):
        # 提示词必须写死「表格整块保留」和「10~20% 重叠」两条规则
        self.assertIn("表格必须整块保留", rag_lab.SPLIT_SYSTEM)
        self.assertIn("重叠", rag_lab.SPLIT_SYSTEM)
        self.assertIn("10%~20%", rag_lab.SPLIT_SYSTEM)

    def test_overlap_is_added_by_code_not_trusted_to_model(self):
        # 模型只给干净块，代码机械补上一块结尾做引子（确定性，不靠模型照做）
        chunks = [
            {"id": "b1_01", "content": "第一段讲需求。" * 20, "chars": 0, "over": False},
            {"id": "b1_02", "content": "第二段讲方案。", "chars": 0, "over": False},
        ]
        out = rag_lab._add_overlap(chunks)
        self.assertTrue(out[1]["content"].startswith("第一段讲需求"))
        self.assertTrue(out[1].get("overlap"))
        self.assertGreater(out[1]["chars"], len("第二段讲方案。"))
        self.assertNotIn("overlap", out[0])   # 第一块不加引子

    def test_overlap_never_copies_table_row_as_lead(self):
        # 上一块以表格结尾时，引子不能抄表格行，否则下一块开头冒出孤立 |...|
        body = "结论：这是正文段落。" * 20
        tail_table = "| 指标 | 值 |\n| --- | --- |\n| 甲 | 1 |"
        chunks = [
            {"id": "b1_01", "content": body + "\n" + tail_table, "chars": 0, "over": False},
            {"id": "b1_02", "content": "## 下一节\n\n新的正文。", "chars": 0, "over": False},
        ]
        out = rag_lab._add_overlap(chunks)
        lead_lines = out[1]["content"].split("\n")[:3]
        self.assertFalse(any(l.strip().startswith("|") for l in lead_lines),
                         "引子里混进了表格行")

    def test_overlap_never_snowballs(self):
        # 引子基于原始内容算，第二块的引子不会把第一块的引子再抄一遍
        chunks = [
            {"id": "b1_01", "content": "甲" * 100, "chars": 100, "over": False},
            {"id": "b1_02", "content": "乙" * 100, "chars": 100, "over": False},
            {"id": "b1_03", "content": "丙" * 100, "chars": 100, "over": False},
        ]
        out = rag_lab._add_overlap(chunks)
        self.assertNotIn("甲", out[2]["overlap"])  # 第三块引子只来自第二块（乙）
        self.assertIn("乙", out[2]["overlap"])


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

    def test_home_shell_and_four_feature_tabs_are_declared(self):
        """2026-11-17：展示页改成「首页四个入口，一功能一页」，角色扮演下线。"""
        import re
        tabs = re.search(r"const TABS = \[(.*?)\];", self.src, re.S).group(1)
        ids = re.findall(r'"([a-z]+)"', tabs)
        self.assertEqual(ids, ["home", "image", "comic", "chain", "rag"])
        for tid in ids:
            self.assertIn('id="%s"' % tid, self.src)
            self.assertIn('href="#%s"' % tid, self.src)

    def test_every_visible_section_has_a_home_or_tab_entry(self):
        import re
        sections = re.findall(r'<section class="feature[^"]*" id="([a-z]+)"', self.src)
        self.assertEqual(sorted(sections),
                         sorted(["home", "image", "guide", "comic", "chain", "rag"]))
        # 角色扮演只留隐藏 DOM 给旧脚本取字段，不再作为可见页签/入口。
        self.assertRegex(
            self.src,
            r'<section class="feature" id="guide"[^>]*\bhidden\b',
        )

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

    def test_switching_a_tab_keeps_the_home_shell_stable(self):
        """首页只做入口：任何 #xxx 都走 showTab，回首页时页脚才出现。

        主页不再是长页 + hero + scroll-cue 的旧结构；切换时仍然瞬时归零，
        避免 CSS 的 scroll-behavior: smooth 让用户以为没切过去。
        """
        self.assertIn('const TABS = ["home", "image", "comic", "chain", "rag"];',
                      self.src)
        self.assertIn('if (!TABS.includes(id)) id = "home";', self.src)
        self.assertIn('siteFooter.hidden = id !== "home";', self.src)
        self.assertIn("""target.closest('a[href^="#"]')""", self.src)
        self.assertIn('root.style.scrollBehavior = "auto"', self.src)
        self.assertNotIn('"instant" in window', self.src)

    def test_homepage_has_four_entry_cards_and_no_roleplay_link(self):
        """首页只做四张入口卡；角色扮演入口已下线。"""
        self.assertIn('id="home"', self.src)
        self.assertEqual(self.src.count('class="home-card"'), 4)
        self.assertIn("四个功能，点开就能试", self.src)
        self.assertNotIn('class="home-card" href="#guide"', self.src)
        self.assertIn('class="home-grid"', self.src)

    def test_no_optional_chaining_for_old_mobile_engines(self):
        """2026-11-16 用户反馈：小米自带浏览器打开是纯黑屏。

        根因：script[2] 里有 ES2020 可选链，老内核整段解析失败 → bindTabs()
        没执行 → head 里的 .js 把板块全藏了 → 黑屏。两条防线：
        1) 脚本里不能有可选链 / 空值合并；2) head 加看门狗，主脚本没跑也会
        在 2 秒后摘掉 .js，宁可内容堆着也不黑屏。
        """
        import re
        scripts = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>",
                             self.src, re.S)
        for i, block in enumerate(scripts):
            self.assertNotRegex(block, r"\?\.", "script[%d] 出现可选链" % i)
            self.assertNotRegex(block, r"\?\?", "script[%d] 出现空值合并" % i)
        self.assertIn("__showcaseReady", self.src)
        self.assertIn("setTimeout", self.src)
        self.assertIn("window.__showcaseReady", self.src)

    def test_mobile_font_scaling_is_locked(self):
        """国产手机浏览器会放大网页字号，撑爆手机端排版。锁定 text-size-adjust。"""
        self.assertIn("-webkit-text-size-adjust: 100%", self.src)
        self.assertIn("text-size-adjust: 100%", self.src)

    def test_custom_prompt_box_lives_under_image_stage(self):
        """2026-11-16 用户反馈：输入框放在左侧场景列表底部不好用，
        改到中间出图框（.image-stage）里、信息条下方，跟出图框同宽。"""
        self.assertIn('<div class="image-stage">', self.src)
        # 输入框必须出现在 image-stage 之后、guide section 之前，
        # 且 image-stage-foot 在它前面（即出图框 → 信息条 → 输入框的顺序）。
        stage = self.src.index('<div class="image-stage">')
        guide = self.src.index('<section class="feature" id="guide"')
        foot = self.src.index('class="image-stage-foot"')
        box = self.src.index('class="custom-prompt-box" id="customPromptBox"')
        self.assertTrue(stage < foot < box < guide)

    def test_scrolling_panels_do_not_trap_the_wheel(self):
        """固定高度的滚动容器别写 overscroll-behavior: contain。

        2026-11-15 用户反馈「滚了一下再滚就滚不动了」：`.chat-log` 在 0 轮对话时
        根本没有溢出、自己滚不了，但 contain 照样把滚轮吃在自己身上，鼠标停在
        聊天面板上整页就卡住。模态弹窗里那块保留（弹窗开着本来就不该滚页面）。
        """
        import re
        for cls in (".chat-log", ".code-block"):
            block = re.search(re.escape(cls) + r"\s*\{([^}]*)\}", self.src).group(1)
            # 注释里会写「不要加 overscroll-behavior」，先剥掉注释再断言
            block = re.sub(r"/\*.*?\*/", "", block, flags=re.S)
            self.assertNotIn("overscroll-behavior", block, cls)
        self.assertIn("dialog:modal { overscroll-behavior: contain; }", self.src)

    def test_retrieve_step_sits_next_to_the_question(self):
        """04 检索要跟着「提问」走，别挂在左侧建库流水线的末尾。

        2026-11-15 用户反馈：问完一句，得一直滑到页面最底下才看到检索命中了
        什么，等于「不知道检索有没有生效」。它本来就只在提问时发生，属于「问」
        不属于「建库」，所以渲染进右栏 `#ragRetrieve`、贴在标题下面。
        """
        import re
        self.assertIn('const QUERY_STEP', self.src)
        self.assertIn('id="ragRetrieve"', self.src)
        # 三步建库流水线里不能再有 query
        steps = re.search(r"const STEPS = \[(.*?)\];", self.src, re.S).group(1)
        self.assertNotIn('"query"', steps)
        # 容器顺序：标题 → 检索 → 对话 → 输入
        i_head = self.src.index('id="ragChatTitle"')
        i_ret = self.src.index('id="ragRetrieve"')
        i_log = self.src.index('id="ragLog"')
        i_form = self.src.index('id="ragForm"')
        self.assertLess(i_head, i_ret)
        self.assertLess(i_ret, i_log)
        self.assertLess(i_log, i_form)
        # 提问后自动展开，不用再点一下
        self.assertIn('setStep("query", hits.length ? "done" : "error"', self.src)

    def test_steps_are_snapshotted_so_a_refresh_keeps_the_detail(self):
        """刷新后四步明细要原样回来，不能只剩一句「明细随刷新丢失」。

        库在 localStorage 的钥匙里（上一版加的），明细跟着库一起存：
        键 `showcase.ragSteps.v1`，按 sid 匹配，销毁库 / 过期时一起删。
        存的是当时渲染出来的那段 DOM，不是重新编一份。
        """
        self.assertIn('const STEPS_KEY = "showcase.ragSteps.v1"', self.src)
        self.assertIn("function rememberStep(", self.src)
        self.assertIn("function restoreSteps(sid)", self.src)
        self.assertIn("const restored = restoreSteps(S.sid)", self.src)
        # 有快照时不能再写那句「明细没能留住」
        self.assertIn("if (!restored) {", self.src)
        # 销毁库 / 后端说没这个会话，都要把快照一起清掉
        self.assertGreaterEqual(self.src.count("clearSteps()"), 4)

    def test_sample_is_served_by_the_gateway(self):
        """网关只反代 /api/*，示例文档必须单独给静态路由，否则手机端 404。"""
        with open(FRIEND, encoding="utf-8") as f:
            src = f.read()
        self.assertIn('"/raglab-sample.md"', src)
        self.assertIn('"/api/raglab/<path:sub>"', src)
        self.assertIn('"raglab": (60, 600)', src)
        self.assertIn('sub in ("split", "answer", "extract")', src)


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
    """PDF / Word：有文字层直接抽（A 档），扫描页走视觉识别（B 档）。

    A 档的价值是「零 token 零幻觉」，B 档的价值是「扫描件也能用」。两边共同的
    底线是**抽不到就明说**——不拿空内容去切块，也不把模型猜的话当成原文。
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
        self.assertNotIn("source", d["meta"])      # 有文字层就一页都不 OCR

    def test_scanned_pages_go_through_vision(self):
        """没有文字层的页 → 渲染成图交视觉模型抄。meta 要如实报是哪几页。"""
        with mock.patch.object(rag_lab.vision, "describe",
                               side_effect=["第一页抄下来的正文", "第二页抄下来的正文"]) as d, \
                mock.patch.object(rag_lab.vision, "active_choice",
                                  return_value=("deepseek", "deepseek-flash")):
            r = self._extract("scan.pdf", make_pdf(["", ""]))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        body = r.get_json()
        self.assertIn("第一页抄下来的正文", body["text"])
        self.assertIn("第二页抄下来的正文", body["text"])
        self.assertEqual(body["meta"]["source"], "vision")
        self.assertEqual(body["meta"]["ocr_pages"], [1, 2])
        self.assertEqual(body["meta"]["ocr_failed"], [])
        self.assertEqual(body["meta"]["ocr_model"], "deepseek/deepseek-flash")
        # 每页一次，prompt 里带页码（模型才知道自己在抄第几页）
        self.assertEqual(d.call_count, 2)
        self.assertIn("第 2 页", d.call_args.kwargs["prompt"])

    def test_only_pages_without_text_layer_are_ocred(self):
        """混排 PDF：有字的页一个字都不 OCR，省 token 也不碰原文。"""
        with mock.patch.object(rag_lab.vision, "describe",
                               return_value="抄出来的内容") as d, \
                mock.patch.object(rag_lab.vision, "active_choice",
                                  return_value=("deepseek", "x")):
            r = self._extract("mix.pdf", make_pdf(["text layer page", ""]))
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertIn("text layer page", body["text"])
        self.assertEqual(body["meta"]["ocr_pages"], [2])
        self.assertEqual(d.call_count, 1)

    def test_vision_failure_falls_back_to_a_clear_error(self):
        """视觉也读不出来时必须报错，不能回一份空正文。"""
        with mock.patch.object(rag_lab.vision, "describe",
                               side_effect=RuntimeError("vision down")), \
                mock.patch.object(rag_lab.vision, "active_choice",
                                  return_value=("deepseek", "x")):
            r = self._extract("scan.pdf", make_pdf(["", ""]))
        self.assertEqual(r.status_code, 400)
        self.assertIn("视觉识别也没读出内容", r.get_json()["error"])

    def test_ocr_is_capped_and_says_so(self):
        """最多识别 OCR_MAX_PAGES 页，超出的页在 meta.warning 里说明白。"""
        pages = [""] * (rag_lab.OCR_MAX_PAGES + 2)
        with mock.patch.object(rag_lab.vision, "describe",
                               return_value="抄出来的内容") as d, \
                mock.patch.object(rag_lab.vision, "active_choice",
                                  return_value=("deepseek", "x")):
            r = self._extract("big-scan.pdf", make_pdf(pages))
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertEqual(d.call_count, rag_lab.OCR_MAX_PAGES)
        self.assertIn("只识别了前", body["meta"]["warning"])

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
        """页面上得写清楚「扫描页走视觉识别」，否则用户以为这类文件传不了。"""
        self.assertIn("扫描页自动识别", self.src)


class LibraryPersistenceTest(unittest.TestCase):
    """刷新网页后要能认回上次的临时库（2026-11-15 用户反馈）。

    库在后端其实没丢（30 分钟 TTL），丢的是 sid —— 它以前只活在页面内存里。
    这些断言守三件事：钥匙存 localStorage、页面打开先问 /state、销毁时删钥匙。
    """

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(ROOT, "web", "showcase.html"), encoding="utf-8") as f:
            cls.src = f.read()

    def test_sid_is_kept_in_local_storage(self):
        self.assertIn('const LIB_KEY = "showcase.ragLib.v1"', self.src)
        self.assertIn("localStorage.setItem(LIB_KEY", self.src)
        self.assertIn("localStorage.removeItem(LIB_KEY)", self.src)

    def test_page_asks_the_backend_on_load(self):
        self.assertIn("async function restoreLib", self.src)
        self.assertIn("/api/raglab/state?sid=", self.src)
        # 必须在 bindRag()（它会 renderSteps）之后调，否则四步卡片会被冲掉
        tail = self.src[self.src.rindex("bindRag();"):]
        self.assertIn("restoreLib();", tail)

    def test_expired_library_clears_the_key(self):
        body = self.src[self.src.index("async function restoreLib"):]
        body = body[:body.index("function bindRag()")]
        # ok=false（后端明确说没这个会话）时才删钥匙；网络错误要留着下次再试
        self.assertIn("if (!data || !data.ok)", body)
        self.assertIn("localStorage.removeItem(LIB_KEY)", body)


class DaemonStdioEncodingTest(unittest.TestCase):
    """daemon 与父进程之间的管道，两边都必须是 UTF-8。

    Windows 上 stdio 连管道时 Python 用系统 ANSI（简体中文 = cp936），中文 JSON
    按 GBK 写出去、父进程按 UTF-8 读：轻则正文一片 \\ufffd（检索回来的是乱码，
    模型照着乱码编），重则 GBK 双字节把紧跟的引号一起吃掉、JSON 结构坏掉，
    请求的 Event 永远等不到 → 30 秒超时 → 页面显示「请求失败（HTTP 502）」。
    """

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(ROOT, "app", "features", "rag", "rag_daemon.py"), encoding="utf-8") as f:
            cls.src = f.read()
        with open(os.path.join(ROOT, "app", "features", "rag", "rag_client.py"), encoding="utf-8") as f:
            cls.client = f.read()

    def test_all_three_streams_are_pinned_to_utf8(self):
        body = self.src[self.src.index("def _force_utf8_stdio"):]
        body = body[:body.index("def _reply")]
        for name in ("stdin", "stdout", "stderr"):
            self.assertIn('"%s"' % name, body)
        self.assertIn('encoding="utf-8"', body)

    def test_called_before_the_model_loads(self):
        main = self.src[self.src.index("def main():"):]
        self.assertLess(main.index("_force_utf8_stdio()"),
                        main.index("_load_services()"))

    def test_reply_can_still_fall_back_to_ascii(self):
        self.assertIn("ensure_ascii=True", self.src)
        self.assertIn("UnicodeEncodeError", self.src)

    def test_ready_flag_resets_when_daemon_is_respawned(self):
        """新进程还在加载模型时，start() 不能因为旧 _ready 还是 True 就放行。"""
        start = self.client[self.client.index("def start(self)"):]
        start = start[:start.index("def _read_loop")]
        self.assertIn("self._ready = False", start)


if __name__ == "__main__":
    unittest.main()
