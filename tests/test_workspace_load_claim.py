"""画廊发号机（`web/workspace/js/gallery_load.js`）的口径。

2026-11-26 用户实测：「上边的滚动条滑动再点击，内容跟标签对不上」。
上一版把「换分类」修成可打断、可拦截，但**续页那一发没换号**：
翻页请求飞着的时候点新分类，旧翻页的包回来序号仍等于当前号，
`owns()` 放它进来，它就以自己的 `page` 往新分类的网格里塞卡片
（`S.page` 同时被改，下一屏还会跳页）。

这里只钉三件事，不碰 DOM：
  1. 换分类永远发得出去，并且把旧号作废；
  2. 翻页不许并发；
  3. 翻页的回包只认领一次 —— 旧翻页撞上新分类时必须整个丢掉。
"""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS = os.path.join(ROOT, "web", "workspace", "js", "gallery_load.js")
GALLERY = os.path.join(ROOT, "web", "workspace", "js", "gallery.js")


def _read(path):
    with open(path, "r", encoding="utf-8", newline="") as f:
        return f.read()


class ClaimMachineTest(unittest.TestCase):
    def setUp(self):
        self.src = _read(JS)

    def test_take_is_exported(self):
        self.assertIn("global.WSTake = { it: it, watch: watch };", self.src)

    def test_rest_claim_is_one_shot(self):
        """翻页回包带 rest 才认领，且一个号只放一次。"""
        self.assertIn("function owns(n, rest)", self.src)
        self.assertIn("if (!busy) return false;", self.src)

    def test_rest_keeps_sequence(self):
        """翻页不换号：换号就等于把还没回来的那次翻页放进来。"""
        body = self.src.split("function claim(reset)")[1].split("function owns")[0]
        self.assertNotIn("seq += 1", body.split("if (busy) return null;")[1])

    def test_new_category_bumps_sequence(self):
        self.assertIn("seq += 1", self.src.split("function claim(reset)")[1])


class GalleryCallSiteTest(unittest.TestCase):
    def test_load_passes_rest_flag(self):
        g = _read(GALLERY)
        self.assertIn("S.claim.owns(seq, !reset)", g)

    def test_head_guard_keeps_new_category(self):
        """换分类不能继续被 busy 挡回去（那正是「标签亮、内容不换」的来源）。"""
        g = _read(GALLERY)
        head = g.split("function load(reset)")[1].split("var page = S.page;")[0]
        self.assertNotIn("if (S.busy) return;", head)

    def test_script_order(self):
        html = _read(os.path.join(ROOT, "web", "workspace", "chat.html"))
        i = html.find("js/gallery_load.js")
        j = html.find("js/gallery.js?")
        self.assertTrue(0 < i < j, "gallery_load.js 必须排在 gallery.js 前面")

    def test_versions_match(self):
        html = _read(os.path.join(ROOT, "web", "workspace", "chat.html"))
        a = re.search(r"gallery_load\.js\?v=(\w+)", html).group(1)
        b = re.search(r"gallery\.js\?v=(\w+)", html).group(1)
        self.assertEqual(a, b, "两个文件必须同版本号，否则只更新一半")


if __name__ == "__main__":
    unittest.main()
