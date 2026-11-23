"""workspace 画廊模块（AI 创作）的静态守卫。

守住用户 2026-11-23 定的几条：顶部只有文件夹横滑（无搜索、无「画廊」标题行）、
卡片只放缩略图 + 名字 + 编号、两列、点开有大图和收藏/下载、接口全部同源复用
`/api/gal/*`。测试只看文件结构与调用路径，不跑浏览器。
"""
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WS = os.path.join(ROOT, "web", "workspace")

FILES = ("css/gallery.css", "js/gallery.js")


def _read(rel):
    with open(os.path.join(WS, rel), encoding="utf-8") as f:
        return f.read()


class TestWorkspaceGalleryFiles(unittest.TestCase):

    def test_module_files_exist_and_stay_small(self):
        for rel in FILES:
            path = os.path.join(WS, rel)
            self.assertTrue(os.path.isfile(path), "缺少模块文件: " + rel)
            with open(path, encoding="utf-8") as f:
                lines = sum(1 for _ in f)
            self.assertLess(lines, 600, rel + " 超过 600 行，需拆分")

    def test_chat_page_loads_module(self):
        html = _read("chat.html")
        self.assertIn("css/gallery.css", html)
        self.assertIn("js/gallery.js", html)
        # views.js 只提供注册口，gallery 自己注册进来
        self.assertLess(html.index("js/views.js"), html.index("js/gallery.js"))


class TestWorkspaceGalleryApi(unittest.TestCase):

    def test_only_same_origin_gallery_paths(self):
        js = _read("js/gallery.js")
        for path in ("/api/gal/list", "/api/gal/work/", "/api/gal/img/",
                     "/api/gal/mark"):
            self.assertIn(path, js, "缺少接口: " + path)
        self.assertNotIn("http://", js)
        self.assertNotIn("https://", js)

    def test_paging_uses_has_more(self):
        js = _read("js/gallery.js")
        self.assertIn("has_more", js)
        self.assertIn("page", js)

    def test_folder_filter_is_a_param_not_a_reload(self):
        js = _read("js/gallery.js")
        self.assertIn("grp=", js, "分类要作为 grp 参数传给列表接口")


class TestWorkspaceGalleryUi(unittest.TestCase):

    def test_card_shows_name_and_number_only(self):
        js = _read("js/gallery.js")
        self.assertIn("work.title", js)
        self.assertIn('"#"', js, "卡片要带作品编号")
        # 来源 / 时间 / 点赞数不进卡片
        self.assertNotIn("work.by", js)
        self.assertNotIn("likes", js)

    def test_no_search_box_in_topbar(self):
        js = _read("js/gallery.js")
        self.assertNotIn("search", js.lower())
        self.assertNotIn("搜索", js)

    def test_viewer_has_download_and_fav(self):
        js = _read("js/gallery.js")
        self.assertIn("下载", js)
        self.assertIn("收藏", js)
        self.assertIn("?s=v", js, "大图要用 v 尺寸")

    def test_two_column_grid(self):
        css = _read("css/gallery.css")
        self.assertIn("grid-template-columns", css)
        self.assertIn("repeat(2", css)

    def test_styles_use_tokens(self):
        css = _read("css/gallery.css")
        self.assertIn("var(--card", css)
        self.assertIn("var(--text", css)
        self.assertIn("var(--shadow", css)


if __name__ == "__main__":
    unittest.main()
