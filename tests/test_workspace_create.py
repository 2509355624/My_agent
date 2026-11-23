"""创作面板（「＋」里 图片生成 / 漫画生成）的静态守卫。

用户 2026-11-23 定下的形态：两个入口平级；漫画生成 = 一句话剧情就直接开画，
不看剧本、不插确认这一步。测试只看结构与接口路径，不跑浏览器。
"""
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WS = os.path.join(ROOT, "web", "workspace")

FILES = ("js/create.js", "css/create.css")


def _read(rel):
    with open(os.path.join(WS, rel), encoding="utf-8") as f:
        return f.read()


class TestCreateFiles(unittest.TestCase):

    def test_files_exist_and_stay_small(self):
        for rel in FILES:
            path = os.path.join(WS, rel)
            self.assertTrue(os.path.isfile(path), "缺少模块文件: " + rel)
            with open(path, encoding="utf-8") as f:
                lines = sum(1 for _ in f)
            self.assertLess(lines, 600, rel + " 超过 600 行，需拆分")

    def test_loaded_by_chat_page(self):
        html = _read("chat.html")
        self.assertIn("css/create.css", html)
        self.assertIn("js/create.js", html)
        self.assertIn("WSCreate.init()", html)


class TestCreatePanel(unittest.TestCase):

    def test_two_entries_are_siblings(self):
        html = _read("chat.html")
        grid = html[html.index('class="pm-grid"'):]
        grid = grid[:grid.index("pm-row")]
        self.assertIn("图片生成", grid)
        self.assertIn("漫画生成", grid)
        self.assertEqual(grid.count('class="pm-tile'), 2, "两个入口必须平级且只有两个")

    def test_mode_tag_hidden_by_default(self):
        html = _read("chat.html")
        i = html.index('id="mode-tag"')
        self.assertIn("hidden", html[i - 40:i + 40])

    def test_switching_mode_rewrites_placeholder(self):
        js = _read("js/create.js")
        self.assertIn("placeholder", js)
        self.assertIn("focus", js, "切完模式把光标放回输入框")


class TestCreateComicFlow(unittest.TestCase):

    def test_comic_paths_are_same_origin(self):
        js = _read("js/create.js")
        for path in ("/api/comic/script", "/api/comic/script_status",
                     "/api/comic/start", "/api/comic/status",
                     "/api/comic/download"):
            self.assertIn(path, js, "缺少接口: " + path)
        self.assertNotIn("http://", js, "接口不能写死域名")
        self.assertNotIn("https://", js, "接口不能写死域名")

    def test_no_script_review_step(self):
        """用户不看剧本：剧本好了直接开跑，不能出现「确认开始」。"""
        js = _read("js/create.js")
        self.assertIn("start(", js)
        self.assertNotIn("确认开始", js)
        self.assertNotIn("先看剧本", js)

    def test_polling_can_stop(self):
        js = _read("js/create.js")
        self.assertIn("setTimeout", js)
        self.assertIn("clearTimeout", js, "轮询必须能停，不能无限打接口")
        self.assertIn("MAX_FAIL", js)

    def test_chat_routes_send_by_mode(self):
        js = _read("js/chat.js")
        self.assertIn("WSCreate.mode()", js)
        self.assertIn("runComic", js)

    def test_card_has_open_and_download(self):
        js = _read("js/create.js")
        self.assertIn("打开整本", js)
        self.assertIn("下载全部", js)


class TestCreateStyles(unittest.TestCase):

    def test_uses_tokens_and_does_not_crop_grid(self):
        css = _read("css/create.css")
        self.assertIn("var(--card", css)
        self.assertIn("var(--brand-soft)", css)
        i = css.index(".card-comic .ccell img")
        self.assertIn("object-fit: contain", css[i:i + 240])

    def test_plus_menu_row_scoped(self):
        """面板通用按钮样式不能把两个大入口压扁，只作用在 pm-row 上。"""
        css = _read("css/chat.css")
        self.assertIn(".plus-menu .pm-row button", css)
        self.assertNotIn(".plus-menu button {", css)


if __name__ == "__main__":
    unittest.main()
