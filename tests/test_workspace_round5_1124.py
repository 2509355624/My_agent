"""2026-11-24 第三轮：图片 1MB 档 / 查看器接管返回键 / 空白横栏 / 发完回聊天页。

用户这一天又提的几条，能钉成「看得见的行为」的都钉在这儿：

  1. 「我感觉现在只有 102k 还是太小了……直接压到 1m 即可，不管什么图片，
     只要是工作台的，服务端压缩到 1m 再传过来，点击图片不需要重新传 10m 的
     原图，我们默认就只给 1m 即可，我们下载也是下载 1m 的」
     → `_IMG_FIT_CAP` = 1MB；`_IMG_FIT_W` 从大往小试；前端点开只放大不换地址；
       下载拿同一档。
  2. 「打开某个具体画廊的图片详情，按下手机的返回键时，没有关闭当前的详情
     页面，而是底下的界面正常回退」
     → 查看器自己压一条 `{gv:1}` 历史，popstate 里收拾它。
  3. 「AI 创作的界面，头顶有一行多余的横栏，明明什么都没有，但是还留着」/
     「我的漫画作品，点进去，依旧是头顶的白色横栏的问题」
     → `.vtitle` 为空时不占位。
  4. 「我在 AI 创作界面，上传图片，然后说需求，然后点击发送的时候，没有自动
     回到初始的聊天界面」
     → send() 里退掉借了输入栏的那一页。
  5. 「moss 漫画名字很长时横向直接延伸出去，多个漫画会混杂在一起」
     → 顶栏历史那一组允许收缩 + 选中项显示简写。

这里只钉事实和前端结构，不钉具体像素。
"""
import io
import os
import unittest

from app.features.image import routes as gen_api


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(*parts):
    with io.open(os.path.join(ROOT, *parts), encoding="utf-8", newline="") as f:
        return f.read()


class ImgFitOneMegabyteTest(unittest.TestCase):
    """1. 图片档位：1MB 上限，且从大往小挑。"""

    def test_cap_is_one_megabyte(self):
        self.assertEqual(gen_api._IMG_FIT_CAP, 1024 * 1024)

    def test_widths_descending(self):
        """从大往小试。从小往大试的第一张往往才 50KB 就「合格」，
        永远出不来高清图——「只有 102k」就是这么来的。"""
        ws = tuple(gen_api._IMG_FIT_W)
        self.assertEqual(ws, tuple(sorted(ws, reverse=True)))

    def test_fit_accepts_1m_and_legacy_512k(self):
        cap = gen_api._IMG_FIT_CAP
        self.assertEqual(gen_api._img_fit("1m"), cap)
        self.assertEqual(gen_api._img_fit("1M"), cap)
        self.assertEqual(gen_api._img_fit("512k"), cap, "旧链接不该碎")
        self.assertEqual(gen_api._img_fit("999k"), 0, "不认识的仍退回原图")

    def test_gallery_route_accepts_1m(self):
        src = _read("app", "features", "gallery", "routes.py")
        self.assertIn('"1m"', src)


class ViewerBackButtonTest(unittest.TestCase):
    """2. 查看器要接管手机返回键。"""

    def setUp(self):
        self.src = _read("web", "workspace", "js", "gallery.js")

    def test_viewer_pushes_history(self):
        self.assertIn("history.pushState({ gv: 1", self.src)

    def test_popstate_closes_viewer(self):
        i = self.src.index('addEventListener("popstate"')
        seg = self.src[i:i + 700]
        self.assertIn("hide()", seg, "返回键退到查看器那条时要收起来")

    def test_self_close_pops_extra_entry(self):
        """自己点 ✕ 关掉时要把多压的那条退掉，否则下次返回是空退。"""
        i = self.src.index("function closeViewer")
        seg = self.src[i:i + 600]
        self.assertIn("history.back()", seg)


class EmptyTopBarTest(unittest.TestCase):
    """3. 中段没标题时不许撑出一条空白横栏。"""

    def setUp(self):
        self.css = _read("web", "workspace", "css", "sheet.css")

    def test_empty_title_collapses(self):
        self.assertIn("> .vtitle:empty", self.css)
        i = self.css.index("> .vtitle:empty")
        seg = self.css[i:i + 320]
        self.assertIn("flex: 0 0 0", seg)

    def test_book_has_no_title(self):
        """「我的漫画作品」的 TITLES 是空串，正是用户报的那一条。"""
        src = _read("web", "workspace", "js", "views.js")
        self.assertIn('book: ""', src)
        self.assertIn('comic: "我的漫画作品"', src)


class SendReturnsToChatTest(unittest.TestCase):
    """4. 在子页里发完话要退回聊天页。"""

    def test_send_pops_docked_view(self):
        src = _read("web", "workspace", "js", "chat.js")
        i = src.index("function send()")
        seg = src[i:src.index("function copyText", i)]
        self.assertIn("WSStack.back()", seg)
        self.assertIn('dataset.dock', seg, "只退借了输入栏的那一页")

    def test_gallery_is_the_docked_page(self):
        views = _read("web", "workspace", "js", "views.js")
        self.assertIn("CHATTY = { gallery: true }", views)


class MossHistoryOverflowTest(unittest.TestCase):
    """5. moss 顶栏：长名字不许把横向撑出去。"""

    def setUp(self):
        self.html = _read("web", "moss_studio.html")

    def test_hist_box_can_shrink(self):
        # 主规则那条（媒体查询里还有一条 `#hist { flex: 1 1 100% }`，别取错）
        i = self.html.index("#hist { display: flex;")
        seg = self.html[i:i + 300]
        self.assertIn("min-width: 0", seg, "flex 子项默认 min-width:auto 会把顶栏撑开")
        self.assertIn("#hist select", self.html)

    def test_selected_option_shows_short(self):
        self.assertIn("function shortLabel", self.html)
        self.assertIn("function polishHistSel", self.html)

    def test_wrap_clips_overflow(self):
        i = self.html.index(".wrap {")
        self.assertIn("overflow-x: clip", self.html[i:i + 200])


if __name__ == "__main__":
    unittest.main()
