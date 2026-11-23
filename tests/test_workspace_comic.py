"""workspace「我的漫画作品」模块的静态守卫。

守住用户 2026-11-23 定的几条：列表顶部有红字提醒（图不长期保存、建议下载）、
作品卡只放封面 + 标题 + 页数进度、点进去是整本翻页（底部缩略图条）、
单页能重画 / 加气泡 / 下载这页、整本能打包下载。
接口全部同源复用 `/api/comic/*`，不另起后端。测试只看文件结构与调用路径。
"""
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WS = os.path.join(ROOT, "web", "workspace")

FILES = ("css/comic.css", "js/comic.js")


def _read(rel):
    with open(os.path.join(WS, rel), encoding="utf-8") as f:
        return f.read()


class TestWorkspaceComicFiles(unittest.TestCase):

    def test_module_files_exist_and_stay_small(self):
        for rel in FILES:
            path = os.path.join(WS, rel)
            self.assertTrue(os.path.isfile(path), "缺少模块文件: " + rel)
            with open(path, encoding="utf-8") as f:
                lines = sum(1 for _ in f)
            self.assertLess(lines, 600, rel + " 超过 600 行，需拆分")

    def test_chat_page_loads_module(self):
        html = _read("chat.html")
        self.assertIn("css/comic.css", html)
        self.assertIn("js/comic.js", html)
        self.assertLess(html.index("js/views.js"), html.index("js/comic.js"))

    def test_registers_both_views(self):
        js = _read("js/comic.js")
        self.assertIn('register("comic"', js)
        self.assertIn('register("book"', js)

    def test_book_opens_without_stacking(self):
        """单本页不叠在列表上：左上角 ✕ 要直接回聊天页（用户 2026-11-23 定）。"""
        js = _read("js/comic.js")
        stack = _read("js/stack.js")
        self.assertIn('WSStack.replace("book"', js)
        self.assertNotIn('WSStack.push("book"', js)
        self.assertIn("replace: replace", stack, "栈要导出 replace，否则点了没反应")


class TestWorkspaceComicApi(unittest.TestCase):

    def test_only_same_origin_comic_paths(self):
        js = _read("js/comic.js")
        for path in ("/api/comic/tasks", "/api/comic/status", "/api/comic/img",
                     "/api/comic/rerender", "/api/comic/bubble",
                     "/api/comic/download"):
            self.assertIn(path, js, "缺少接口: " + path)
        self.assertNotIn("http://", js)
        self.assertNotIn("https://", js)

    def test_image_url_carries_rev(self):
        js = _read("js/comic.js")
        self.assertIn("withRev", js)
        self.assertIn('"&v="', js, "图变了要换 URL，否则浏览器拿旧缓存")

    def test_poll_stops_when_page_is_gone(self):
        js = _read("js/comic.js")
        self.assertIn("document.body.contains", js)
        self.assertIn("clearInterval", js)


class TestWorkspaceComicUi(unittest.TestCase):

    def test_list_warns_about_retention_in_red(self):
        js = _read("js/comic.js")
        css = _read("css/comic.css")
        self.assertIn("cwarn", js)
        self.assertIn("下载", js)
        self.assertIn("var(--danger)", css)

    def test_card_shows_title_and_progress_only(self):
        js = _read("js/comic.js")
        self.assertIn("t.title", js)
        self.assertIn("已画", js)
        # 剧本正文、点赞、作者不进卡片
        self.assertNotIn("likes", js)
        self.assertNotIn("script", js)

    def test_book_has_strip_and_four_actions(self):
        js = _read("js/comic.js")
        self.assertIn("cstrip", js)
        for text in ("重画这页", "加气泡", "下载这页", "下载全部"):
            self.assertIn(text, js)

    def test_styles_use_tokens(self):
        css = _read("css/comic.css")
        self.assertIn("var(--card", css)
        self.assertIn("var(--text", css)
        self.assertIn("var(--shadow", css)

    def test_pictures_are_not_cropped(self):
        """封面和整页都得整张显示（用户 2026-11-23：比例不对，要完整显示全图）。"""
        css = _read("css/comic.css")
        for block in (".cpic img", ".cslide img"):
            i = css.index(block)
            seg = css[i:i + 260]
            self.assertIn("object-fit: contain", seg, block + " 不能裁边")

    def test_strip_scrolls_sideways(self):
        css = _read("css/comic.css")
        self.assertIn("overflow-x: auto", css)
        self.assertIn("scrollbar-width: none", css)


if __name__ == "__main__":
    unittest.main()
