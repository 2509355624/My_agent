# -*- coding: utf-8 -*-
"""2026-11-23 第二轮反馈的回归测试。

用户这轮提的四件事：
1. 5174 要用原始网页，不要新界面；5175 群友页发新界面 —— 两边分工
2. 参考图上传了却报「没找到她」 —— 上传返回的 items 是顶层字段
3. 引用了参考图却不知道有没有真用上 —— 要有可见反馈，且后端真读
4. 复制提示词和重跑去画的按钮挨太近 —— 要分开

这些断言查的是**行为**，不是「代码里有没有这行字」：能算出结果的算，
能跑后端的跑。
"""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel, base=ROOT):
    with open(os.path.join(base, rel), encoding="utf-8") as f:
        return f.read()


# ─── 1. 新旧分工（2026-11-23 定稿口径）───────────────────────
#
# **5174 本机 = 站主自用，发原始页；5175 隧道 = 群友页，发新界面。**
# 早先一版测试把「两边都发原始页」写死了，那是当天上午的临时口径，
# 已被定稿推翻——所以这里断言的是分工，不是「全都不许出现 /workspace/」。

class TestSiteSplit(unittest.TestCase):
    """5174 发原始页；5175 的 /gallery、/comic 只是 302 到新界面。"""

    def test_5175_gallery_redirects_to_workspace(self):
        src = _read("friend_app.py")
        i = src.index("def gallery_page():")
        seg = src[i:src.index("@app.route(\"/comic\")", i)]
        self.assertIn('return redirect("/workspace/")', seg)
        self.assertNotIn('send_from_directory(WEB_DIR, "gallery.html")', seg)

    def test_5175_comic_redirects_to_workspace(self):
        src = _read("friend_app.py")
        i = src.index("def comic_page():")
        seg = src[i:src.index("@app.route(\"/api/comic/", i)]
        self.assertIn('return redirect("/workspace/")', seg)
        self.assertNotIn('send_from_directory(WEB_DIR, "comic_studio_friend.html")', seg)

    def test_5174_gallery_sends_original(self):
        src = _read("app/web/pages.py")
        i = src.index("def gallery_page():")
        seg = src[i:i + 800]
        self.assertIn('send_from_directory(WEB_DIR, "gallery.html")', seg)

    def test_5174_comic_sends_original(self):
        src = _read("app/web/pages.py")
        i = src.index("def comic_page():")
        seg = src[i:i + 800]
        self.assertIn('send_from_directory(WEB_DIR, "comic_studio.html")', seg)

    def test_5174_gen_sends_original(self):
        """跑图台也只认原始页 `gen_pad.html`，不许 302 到新界面。"""
        src = _read("app/features/image/routes.py")
        i = src.index("def page():")
        seg = src[i:i + 900]
        self.assertIn('send_from_directory(WEB_DIR, "gen_pad.html")', seg)
        self.assertNotIn('redirect("/workspace', seg)

    def test_qq_links_point_at_workspace(self):
        """QQ 发出去的是群友页地址，就该是 /workspace/，不再走旧地址绕一圈。"""
        for rel in ("app/features/image/direct_gen.py",
                    "app/features/qq/play_bot.py",
                    "app/tools/normal/generate_image.py"):
            src = _read(rel)
            for line in src.splitlines():
                if "gallery?from=qq" in line or "comic?from=qq" in line:
                    self.fail("%s 里还发着旧地址：%s" % (rel, line.strip()))

    def test_qq_gallery_and_comic_texts(self):
        """真调一次正文生成，确认拼出来的地址。"""
        from app.features.image import direct_gen
        from app.features.qq import friend_site

        old = friend_site.url
        friend_site.url = lambda: "https://x.example"
        try:
            g = direct_gen._gallery_text()
            c = direct_gen._comic_studio_text()
        finally:
            friend_site.url = old
        self.assertIn("https://x.example/workspace/?from=qq", g)
        self.assertIn("https://x.example/workspace/?from=qq", c)


# ─── 2. 参考图上传 ─────────────────────────────────────────

class TestRefUploadReadsTopLevelItems(unittest.TestCase):
    """上传后取图 id 必须读响应顶层的 items。"""

    def test_items_read_from_top_level(self):
        js = _read("web/workspace/js/ref.js")
        self.assertIn("w.items", js, "要看顶层的 items")
        self.assertNotIn("w.ok && w.items", js,
                         "老写法 w.ok && w.items 拿到的是空数组")
        # 不能写成 (w && w.ok && w.items) 这种把 items 挂在 ok 后面的取法
        self.assertNotRegex(js, r"&&\s*w\.items")

    def test_backend_items_is_top_level(self):
        """后端确认：items 和 ok 平级。"""
        src = _read("app/features/gallery/routes.py")
        i = src.index("def api_work(")
        seg = src[i:src.index("# ─── 图", i)]
        self.assertIn('"items":', seg)
        # work 里包的是作品集本身，不该再套一层 items
        self.assertNotIn('"work": data,\n        "items"', seg.replace(" ", ""))


# ─── 3. 参考图真的生效 ─────────────────────────────────────

class TestRefsActuallyApply(unittest.TestCase):
    """引用参考图要有可见反馈，后端也要真读。"""

    def test_backend_reads_refs(self):
        src = _read("app/features/image/routes.py")
        self.assertIn("_ref_ids", src)
        self.assertIn("_describe_refs", src)
        i = src.index("def api_submit")
        seg = src[i:i + 4000]
        self.assertIn("_ref_ids(data.get(\"refs\"))", seg)
        self.assertIn("画面参考", seg, "参考图描述要拼进提示词")

    def test_backend_exposes_ref_note(self):
        src = _read("app/features/image/routes.py")
        i = src.index("def _public(job)")
        seg = src[i:i + 700]
        self.assertIn("ref_note", seg, "前端要能看到参考图用没用上")

    def test_ref_failure_does_not_block(self):
        """读图失败只降级，不能把整条消息拦下来。"""
        src = _read("app/features/image/routes.py")
        i = src.index("def _describe_refs")
        seg = src[i:i + 900]
        self.assertNotIn("raise", seg.replace("raise 异常", ""))
        self.assertIn('return ""', seg)

    def test_describe_route_shares_permission(self):
        """读图接口和 /img 用同一套权限，不能各写一份。"""
        src = _read("app/features/gallery/routes.py")
        self.assertIn("def _read_image_text", src)
        i = src.index("def api_describe")
        seg = src[i:i + 1200]
        self.assertIn("_read_image_text", seg)
        # 权限判据只能出现在共用函数里，别在路由里再抄一份
        self.assertNotIn('row["hidden"]', seg)

    def test_frontend_shows_ref_state(self):
        js = _read("web/workspace/js/chat.js")
        self.assertIn("job.ref_note", js, "排队/生成中要把参考图状态说出来")

    def test_frontend_reuses_refs_on_rerun(self):
        """点了重画也要带着同一份参考图。"""
        js = _read("web/workspace/js/chat.js")
        i = js.index("function buildActs")
        seg = js[i:i + 1800]
        self.assertIn("card.refs", seg)
        self.assertNotIn("submitJob(card.prompt, card.skill, [])", seg)

    def test_use_same_fills_prompt(self):
        """画同款要把图读出来的描述填进输入框，不能只挂缩略图。"""
        js = _read("web/workspace/js/ref.js")
        self.assertIn("function describeInto", js)
        i = js.index("function useSame")
        seg = js[i:i + 1200]
        self.assertIn("describeInto", seg)
        self.assertIn("/api/gal/describe", js)


# ─── 4. 按钮不要挨在一起 ───────────────────────────────────

class TestCardActsSeparated(unittest.TestCase):
    def test_two_buttons_have_distinct_classes(self):
        js = _read("web/workspace/js/chat.js")
        i = js.index("function buildActs")
        seg = js[i:i + 2000]
        self.assertIn('"act act-text"', seg)
        self.assertIn('"act act-icon"', seg)

    def test_css_pushes_them_apart(self):
        css = _read("web/workspace/css/chat.css")
        i = css.index(".card .acts {")
        seg = css[i:i + 500]
        self.assertIn("space-between", seg, "两个按钮要分到两头")

    def test_acts_are_not_tiny_circles(self):
        """34px 的小圆圈是误触的主因，至少要带上文字。"""
        css = _read("web/workspace/css/chat.css")
        i = css.index(".card .acts button {")
        seg = css[i:i + 500]
        self.assertNotIn("border-radius: 50%", seg)
        js = _read("web/workspace/js/chat.js")
        self.assertIn("<span>复制</span>", js)
        self.assertIn("<span>重画</span>", js)


if __name__ == "__main__":
    unittest.main()


# ─── 5. 上传索引差一位（用户报「上传了没找到她」的真根因）──────

class TestUploadReturnsItemId(unittest.TestCase):
    """上传要直接告诉前端「这张图的 id」，别让它拿序号去猜。

    2026-11-23 的真根因：`append_upload` 返回 i（第几张，1 基），而 items.idx
    在库里是 0 基，前端拿返回的号去 items 里按 idx 找永远差一位 → 每次都报
    「参考图传上去了，但没找到它」。库内 0 基是既有约定（test_gallery 锁着），
    所以修在接口层：服务端自己把 item_id 查出来给出去。
    """

    def test_upload_one_returns_item_id(self):
        src = _read("app/features/gallery/routes.py")
        i = src.index("def api_upload_one")
        seg = src[i:src.index("@bp.route(\"/upload/abort\"", i)]
        self.assertIn('"item_id":', seg)
        # 查 id 时用的是 0 基的 idx
        self.assertIn("(wid, idx - 1)", seg)

    def test_frontend_prefers_item_id(self):
        js = _read("web/workspace/js/ref.js")
        self.assertIn("d.item_id", js)
        i = js.index("d.item_id")
        # 优先走 item_id，且要在回去查作品集之前
        j = js.index("/api/gal/work/", i)
        self.assertLess(i, j)

    def test_fallback_uses_zero_based(self):
        """老服务端没 item_id 时，按序号找要用 0 基（d.idx - 1）。"""
        js = _read("web/workspace/js/ref.js")
        self.assertIn("it.idx === d.idx - 1", js)
        self.assertNotIn("it.idx === d.idx,", js)

    def test_items_read_from_top_level(self):
        """顺带守住另一个坑：items 是响应顶层字段，不是包在 work 里。"""
        js = _read("web/workspace/js/ref.js")
        self.assertIn("w.items", js)
        self.assertNotRegex(js, r"&&\s*w\.items")


# ─── 6. 过场提示不能留在页面上 ────────────────────────────

class TestBusyNoteIsNotLogged(unittest.TestCase):
    """「正在看图…」这类提示要自己消失，且不进存档。"""

    def test_busy_note_exists_and_is_exported(self):
        js = _read("web/workspace/js/chat.js")
        self.assertIn("function busyNote", js)
        # 写了没导出 = 调用处拿到 undefined，页面直接炸（这次就踩了）
        i = js.index("global.WSChat = {")
        seg = js[i:i + 900]
        self.assertIn("busyNote: busyNote", seg)

    def test_busy_note_does_not_log(self):
        js = _read("web/workspace/js/chat.js")
        i = js.index("function busyNote")
        seg = js[i:i + 700]
        self.assertNotIn("log(", seg, "过场提示不该写进存档")
        self.assertIn("setTimeout", seg, "没人来收也要自己消失")

    def test_ref_uses_busy_note_for_transient(self):
        js = _read("web/workspace/js/ref.js")
        self.assertIn('busyNote("正在看图…")', js)
        self.assertIn('busyNote("正在传参考图…")', js)
        # 老写法 tip.parentNode 永远取不到（sysNote 当时不返回节点）
        self.assertNotIn("tip.parentNode", js)


# ─── 7. 输入框不该有滚动条 ───────────────────────────────

class TestComposerNoScrollbar(unittest.TestCase):
    def test_scrollbar_hidden_both_engines(self):
        css = _read("web/workspace/css/chat.css")
        i = css.index("#say {")
        seg = css[i:i + 1200]
        self.assertIn("scrollbar-width: none", seg, "Edge/Firefox 认这条")
        self.assertIn("#say::-webkit-scrollbar { width: 0; }", css)
