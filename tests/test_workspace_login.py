"""全新界面登录页的静态结构守卫。

登录页在 web/workspace/，纯静态、无构建。这里用 HTMLParser + 文本断言
守住「分模块、同源接口、两步流程、明暗主题、手机优先」这几条硬约束，
避免后续改成单文件屎山或把接口写死到域名。
"""
import os
import re
import unittest
from html.parser import HTMLParser

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WS = os.path.join(ROOT, "web", "workspace")


def _read(rel):
    with open(os.path.join(WS, rel), encoding="utf-8") as f:
        return f.read()


class _TagBalance(HTMLParser):
    """统计 div/section 开合是否配平。"""

    def __init__(self):
        super().__init__()
        self.stack = []

    def handle_starttag(self, tag, attrs):
        if tag in ("div", "section"):
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if tag in ("div", "section") and self.stack:
            self.stack.pop()


class TestWorkspaceLoginLayout(unittest.TestCase):

    def test_module_files_exist_and_stay_small(self):
        for rel in ("index.html", "css/tokens.css", "css/base.css",
                    "css/login.css", "js/api.js", "js/login.js", "README.md"):
            path = os.path.join(WS, rel)
            self.assertTrue(os.path.isfile(path), "缺少模块文件: " + rel)
            with open(path, encoding="utf-8") as f:
                lines = sum(1 for _ in f)
            self.assertLess(lines, 600, rel + " 超过 600 行，需拆分")

    def test_html_tags_balanced(self):
        p = _TagBalance()
        p.feed(_read("index.html"))
        self.assertEqual(p.stack, [], "div/section 标签未配平")

    def test_two_steps_with_correct_buttons(self):
        html = _read("index.html")
        for marker in ('id="step-qq"', 'id="step-code"',
                       'id="btn-next"', 'id="btn-send"', 'id="btn-login"'):
            self.assertIn(marker, html, "缺少步骤标记: " + marker)
        self.assertIn("下一步", html)
        self.assertIn("获取验证码", html)
        self.assertIn("登录", html)

    def test_inputs_and_validation_hints(self):
        html = _read("index.html")
        self.assertIn('id="qq"', html)
        self.assertIn('id="code"', html)
        self.assertIn("输入 QQ 号", html)
        self.assertIn("输入验证码", html)
        self.assertIn("登录即同意", html)

    def test_mobile_viewport_and_one_primary_action(self):
        html = _read("index.html")
        self.assertIn('name="viewport"', html)
        self.assertIn("width=device-width", html)
        # 同时可见的主按钮只有一个：验证码页初始隐藏
        self.assertIn('id="step-code"', html)
        self.assertRegex(html, r'id="step-code"[^>]*style="[^"]*display:\s*none')


class TestWorkspaceLoginTheme(unittest.TestCase):

    def test_light_dark_tokens_exist(self):
        css = _read("css/tokens.css")
        self.assertIn("[data-theme=", css)
        self.assertIn("--bg", css)
        self.assertIn("--card", css)
        self.assertIn("--text", css)
        self.assertIn("prefers-color-scheme", css, "应跟随系统深浅色偏好")

    def test_theme_script_before_paint(self):
        html = _read("index.html")
        self.assertIn("data-theme", html)
        # 首屏前置脚本要在 <body> 前定好主题，避免闪色
        self.assertLess(html.index("data-theme"), html.index("<main"))


class TestWorkspaceLoginApi(unittest.TestCase):

    def test_same_origin_paths_only(self):
        js = _read("js/api.js")
        self.assertIn("/login/send", js)
        self.assertIn("/login/verify", js)
        self.assertNotIn("http://", js, "接口不能写死 http 域名")
        self.assertNotIn("https://", js, "接口不能写死 https 域名")
        self.assertIn("credentials", js, "必须带 cookie 才能登录")

    def test_qq_regex_matches_backend(self):
        js = _read("js/login.js")
        self.assertIn(r"[1-9][0-9]{4,11}", js, "QQ 校验需与后端一致")

    def test_no_listener_left_running(self):
        js = _read("js/login.js")
        self.assertIn("setInterval", js)
        self.assertIn("clearInterval", js, "倒计时必须能清理，避免泄漏")


if __name__ == "__main__":
    unittest.main()
