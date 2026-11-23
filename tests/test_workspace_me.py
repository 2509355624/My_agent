"""「我的」页静态守卫。

用户 2026-11-23 定：只有「作品 / 收藏」两个 tab，不做头像昵称，也不要点赞。
作品 = /api/gal/mine 的作品集，收藏 = /api/gal/favs 的单张图，点开都复用画廊查看器。
测试只看结构，不跑浏览器。
"""
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WS = os.path.join(ROOT, "web", "workspace")

FILES = ("js/me.js", "css/me.css")


def _read(rel):
    with open(os.path.join(WS, rel), encoding="utf-8") as f:
        return f.read()


class TestMeFiles(unittest.TestCase):

    def test_files_exist_and_stay_small(self):
        for rel in FILES:
            path = os.path.join(WS, rel)
            self.assertTrue(os.path.isfile(path), "缺少模块文件: " + rel)
            with open(path, encoding="utf-8") as f:
                lines = sum(1 for _ in f)
            self.assertLess(lines, 600, rel + " 超过 600 行，需拆分")

    def test_loaded_by_chat_page(self):
        html = _read("chat.html")
        self.assertIn("css/me.css", html)
        self.assertIn("js/me.js", html)

    def test_registers_me_view(self):
        js = _read("js/me.js")
        self.assertIn('WSViews.register("me"', js)


class TestMeShape(unittest.TestCase):

    def test_only_two_tabs(self):
        js = _read("js/me.js")
        i = js.index('[["works"')
        seg = js[i:js.index("].forEach", i)]
        self.assertEqual(seg.count('["'), 2, "只准作品 / 收藏两个 tab")
        self.assertIn("作品", seg)
        self.assertIn("收藏", seg)

    def test_no_avatar_nickname_or_likes(self):
        js = _read("js/me.js")
        css = _read("css/me.css")
        for token in ("头像", "昵称", "点赞", "likes", "avatar", "nickname"):
            self.assertNotIn(token, js + css, "个人页不该出现: " + token)

    def test_empty_states(self):
        js = _read("js/me.js")
        self.assertIn("还没有作品", js)
        self.assertIn("还没收藏", js)


class TestMeApi(unittest.TestCase):

    def test_same_origin_paths(self):
        js = _read("js/me.js")
        self.assertIn("/api/gal/mine", js)
        self.assertIn("/api/gal/favs", js)
        self.assertNotIn("http://", js)
        self.assertNotIn("https://", js)

    def test_pagination(self):
        js = _read("js/me.js")
        self.assertIn("has_more", js)
        self.assertIn("IntersectionObserver", js)

    def test_both_open_gallery_viewer(self):
        js = _read("js/me.js")
        self.assertEqual(js.count("WSGallery.open("), 2,
                         "作品和收藏都复用画廊查看器")


class TestMeStack(unittest.TestCase):

    def test_enter_me_pushes_a_layer(self):
        """右上角人形进「我的」要真叠一层，✕ 退回刚才那页，不跳回聊天页。"""
        js = _read("js/stack.js")
        self.assertIn('push("me")', js)
        self.assertNotIn('replace("me")', js)

    def test_me_does_not_stack_itself(self):
        """已经在「我的」里再点人形不得再压一层「我的」。

        连点会叠出一串一模一样的页，✕ 按几次看着都像没反应
        （用户 2026-11-23：一直点头像就一直进我的）。判据必须看当前栈顶，
        不能看创建节点时的那一页——「我的」页里的 view 也是 "me"。
        """
        js = _read("js/stack.js")
        self.assertIn('if (S.top && S.top.dataset.view === "me") return;', js)


class TestGalleryJump(unittest.TestCase):

    def test_viewer_can_jump_to_a_faved_item(self):
        js = _read("js/gallery.js")
        self.assertIn("function open(work, itemId)", js)
        self.assertIn("v.list[i].id === v.jump", js,
                      "按 item id 定位，不能信各来源口径不一的 idx")

    def test_viewer_builds_lazily(self):
        """"我的"页可以直接 push，不一定先逛过画廊，查看器得能自己建。"""
        js = _read("js/gallery.js")
        self.assertIn("if (!v.node) buildViewer();", js)


class TestMeStyles(unittest.TestCase):

    def test_uses_tokens(self):
        css = _read("css/me.css")
        for token in ("var(--card", "var(--text", "var(--brand",
                      "var(--shadow", "var(--hair"):
            self.assertIn(token, css, "缺少设计变量: " + token)


if __name__ == "__main__":
    unittest.main()
