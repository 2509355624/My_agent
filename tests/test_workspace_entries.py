"""入口路由的静态守卫（2026-11-23，口径当天改过两次）。

定稿分工：**5174 本机 = 站主自用，发原始页；5175 隧道 = 群友页，发新界面。**

约定：
- 5174 的 /gallery /comic /moss **以及 /gen（跑图台）**都发原始页面。
  上午一度全改成 302 到 /workspace，用户明确要求「5174 使用原始的网页，
  不要使用新版的」，于是全部改回 send_from_directory；/workspace/ 仍可
  直达，只是不再从这些旧地址跳过去。
- 5175 的 /gallery /comic 只是 302 跳板，落地是新界面。
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


class TestOldRoutesServeOriginal(unittest.TestCase):
    """5174 的 /gallery /comic /moss 发原始页面，不再跳新界面。"""

    def test_pages_modules_serve_original(self):
        pages = _read("app/web/pages.py")
        want = {"gallery_page": "gallery.html",
                "comic_page": "comic_studio.html",
                "moss_comic_page": "moss_studio.html"}
        for fn, html in want.items():
            with self.subTest(fn=fn):
                body = _func_body(pages, fn)
                self.assertIn('send_from_directory(WEB_DIR, "%s")' % html, body,
                              fn + " 应发原始页面")
                self.assertNotIn('redirect("/workspace/"', body,
                                 fn + " 不该再跳新界面")

    def test_gen_route_serves_original(self):
        src = _read("app/features/image/routes.py")
        body = _func_body(src, "page")
        self.assertIn('send_from_directory(WEB_DIR, "gen_pad.html")', body,
                      "/gen 应发原始跑图台")
        self.assertNotIn('redirect("/workspace/"', body,
                         "/gen 不该再跳新界面")

    def test_5175_old_links_redirect_to_workspace(self):
        """群友页那边反过来：旧地址只当跳板，落地是新界面。"""
        src = _read("friend_app.py")
        for fn in ("gallery_page", "comic_page"):
            with self.subTest(fn=fn):
                body = _func_body(src, fn)
                self.assertIn('return redirect("/workspace/")', body)


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

    def test_home_buttons_open_the_old_pages(self):
        """5174 首页「更多」菜单的四个按钮必须开**原始页面**。

        2026-11-23 三次返工都栽在这：页面路由改回来了，但 `web/index.html`
        里这四个按钮还写死 `location.href='/workspace/'`，点进去永远是白的新
        界面。旧断言钉的恰恰是「跳到新界面」，所以测试全绿、需求没做。
        现在反过来钉：必须是本机旧页，且不许出现 /workspace/。
        """
        html = _read("web/index.html")
        want = {"漫画": "/comic", "moss 漫画": "/moss",
                "跑图台": "/gen", "画廊": "/gallery"}
        for label, path in want.items():
            with self.subTest(btn=label):
                m = re.search(r'<button[^>]*>' + re.escape(label) + r'</button>', html)
                self.assertIsNotNone(m, "首页缺少按钮: " + label)
                tag = m.group(0)
                self.assertIn("'%s'" % path, tag,
                              "%s 应开原始页 %s" % (label, path))
                self.assertNotIn("/workspace/", tag, label + " 不该再跳新界面")

    def test_persona_entry_points_to_old_gallery(self):
        """角色扮演页顶栏「← 创作」回的是本机旧画廊。"""
        html = _read("web/persona.html")
        self.assertIn('id="btnGallery" href="/gallery"', html)
        self.assertNotIn('href="/workspace/"', html)
        self.assertIn('location.href = "/gallery"', html)

    def test_old_pages_do_not_push_new_ui(self):
        """原始页面里不该再把入口指向新界面（2026-11-23 改回）。

        画廊的「更多」菜单原来放的是「AI 创作 → /workspace/」，用户要求
        5174 用原始网页之后，这里恢复成「漫画台 → /comic」。
        """
        gal = _read("web/gallery.html")
        self.assertNotIn('href="/workspace/"', gal)
        self.assertIn('href="/comic"', gal, "画廊菜单要能进漫画台")


if __name__ == "__main__":
    unittest.main()
