"""新界面入口接入的静态守卫（2026-11-23 用户拍板）。

约定：
- 5174 旧路由 /gallery /comic /moss /gen 直接 302 到 /workspace，旧页面文件保留
  （回退只需把 redirect 换回 send_from_directory）。
- 5175 也接：/workspace 未登录发登录页、登录后发聊天页；且必须有一条静态资源
  路由——5175 是 static_folder=None，不会自己发 css/js。
- 页面里的旧跳转改成指向 /workspace，不再开到老界面。
测试只看源码结构，不启服务。
"""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def _func_body(src, name):
    """取 def name(...) 到下一个顶层 def/@ 之前的正文。"""
    m = re.search(r"\ndef %s\(" % name, src)
    if not m:
        return ""
    rest = src[m.end():]
    end = re.search(r"\n(?:@|def |[A-Za-z_]+ = )", rest)
    return rest[:end.start()] if end else rest


class TestOldRoutesRedirect(unittest.TestCase):
    """5174 的四个旧路由全指向新界面。"""

    def test_pages_modules_redirect(self):
        pages = _read("app/web/pages.py")
        for fn in ("gallery_page", "comic_page", "moss_comic_page"):
            with self.subTest(fn=fn):
                body = _func_body(pages, fn)
                self.assertIn('redirect("/workspace/")', body,
                              fn + " 应 302 到新界面（带尾斜杠）")

    def test_gen_route_redirects(self):
        src = _read("app/features/image/routes.py")
        body = _func_body(src, "page")
        self.assertIn('redirect("/workspace/")', body, "/gen 应 302 到新界面")

    def test_redirect_is_imported(self):
        for rel in ("app/web/pages.py", "app/features/image/routes.py"):
            with self.subTest(f=rel):
                self.assertIn("redirect", _read(rel).split("\n")[0:60].__str__())


class TestWorkspaceRoute(unittest.TestCase):
    """5174 的 /workspace 只发聊天页壳（单页 App）。"""

    def test_serves_chat_shell(self):
        body = _func_body(_read("app/web/pages.py"), "workspace_index")
        self.assertIn('"chat.html"', body)

    def test_bare_path_redirects_to_trailing_slash(self):
        """页面里是相对路径 css/xxx，停在 /workspace 会让它们全部 404。"""
        body = _func_body(_read("app/web/pages.py"), "workspace_page")
        self.assertIn('redirect("/workspace/")', body)

    def test_assets_route_exists(self):
        src = _read("app/web/pages.py")
        self.assertIn('@bp.route("/workspace/<path:sub>")', src)


class TestGatewayRoute(unittest.TestCase):
    """5175：登录门 + 静态资源。"""

    def setUp(self):
        self.src = _read("friend_app.py")

    def test_login_gate(self):
        body = _func_body(self.src, "workspace_index")
        self.assertIn("_logged_in()", body, "未登录要发登录页")
        self.assertIn('"index.html"', body)
        self.assertIn('"chat.html"', body)

    def test_serves_static_assets(self):
        """5175 是 static_folder=None，不补这条页面就是没样式的白纸。"""
        self.assertIn('@app.route("/workspace/<path:sub>")', self.src)
        body = _func_body(self.src, "workspace_asset")
        self.assertIn("send_from_directory", body,
                      "send_from_directory 自带 ../ 防护，别自己拼路径")

    def test_solo_gate_lets_workspace_through(self):
        """带 fr_solo 的浏览器点 QQ 给的入口，不能被挡回 /play。"""
        self.assertIn('"/workspace",', self.src)
        # 静态资源也不能被挡回 /play，否则 index 发出去但 css/js 全变 HTML。
        # 2026-11-23 起统一由 `_ws_page` 一个判据覆盖 `/workspace` 与
        # `/workspace/<子路径>`，所以断言改成钉住这个判据本身。
        self.assertIn("_ws_page = p ==", self.src)
        self.assertIn('p.startswith("/workspace/")', self.src)
        self.assertIn("and not _ws_page", self.src)


class TestPageLinks(unittest.TestCase):
    """页面里的跳转都指向新界面。"""

    def test_home_buttons_open_workspace_in_place(self):
        html = _read("web/index.html")
        for label in ("漫画", "moss 漫画", "跑图台", "画廊"):
            with self.subTest(btn=label):
                m = re.search(r'<button[^>]*>' + re.escape(label) + r'</button>', html)
                self.assertIsNotNone(m, "首页缺少按钮: " + label)
                tag = m.group(0)
                self.assertIn("location.href='/workspace/'", tag,
                              label + " 应同页跳到新界面")
                self.assertNotIn("_blank", tag, label + " 不该再开新标签")

    def test_persona_entry_points_to_workspace(self):
        html = _read("web/persona.html")
        self.assertIn('id="btnGallery" href="/workspace/"', html)
        self.assertNotIn('location.href = "/gallery" + q', html,
                         "旧的双端口探测已无必要：两端都有新界面")

    def test_old_pages_link_to_workspace(self):
        self.assertIn('href="/workspace/"', _read("web/gallery.html"))
        self.assertIn('href="/workspace/"', _read("web/comic_studio.html"))


if __name__ == "__main__":
    unittest.main()
