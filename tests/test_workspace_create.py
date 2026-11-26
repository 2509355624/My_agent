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


class TestCreateBookBugfix1125(unittest.TestCase):
    """用户 2026-11-25 报的两个 bug：

    ① 「打开整本」应该先落一张卡片，点卡片才进整本；
    ② 退出漫画模式（✕ / 刷新）之后回不去了，找不到进漫画台的路。
    """

    def test_open_book_drops_a_card_first(self):
        js = _read("js/create.js")
        i = js.index("function openBook(")
        # 边界取到 enterBook 为止（书卡那张封面框 2026-11-26 撤了，
        # 原来的 `function bookCover(` 已经不存在）
        body = js[i:js.index("function enterBook(")]
        # 铺卡在前、进整本只能由点卡片触发
        self.assertIn("drawBook(rec)", body, "打开整本必须先落一张书卡")
        self.assertNotIn('WSStack.push("book"', body,
                         "打开整本不能直接推子页（会盖住聊天页里那张卡）")
        self.assertIn("enterBook", body)

    def test_book_card_is_clickable_and_saved(self):
        js = _read("js/create.js")
        self.assertIn('k: "book"', js, "书卡必须进存档，刷新后还在")
        i = js.index("function drawBook(")
        body = js[i:i + 1200]
        self.assertIn("enterBook", body, "点书卡要能进整本")
        self.assertIn("看整本", body)

    def test_book_card_is_replayed(self):
        js = _read("js/archive.js")
        self.assertIn('m.k === "book"', js, "存档重放要认得书卡")
        self.assertIn("restoreBook", js)

    def test_same_book_does_not_duplicate_cards(self):
        js = _read("js/create.js")
        self.assertIn("S.books.indexOf(task)", js)

    def test_comic_mode_survives_reload(self):
        js = _read("js/create.js")
        self.assertIn("ws_create_mode", js, "模式要存盘，否则退出后回不去")
        self.assertIn("savedMode()", js)
        i = js.index("function init()")
        self.assertIn("savedMode()", js[i:i + 800], "init 时要把上次的模式还原")

    def test_book_card_has_style(self):
        css = _read("css/create.css")
        self.assertIn(".card-book", css)
        self.assertIn(".bcover", css)


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
