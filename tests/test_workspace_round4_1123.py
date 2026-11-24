# -*- coding: utf-8 -*-
"""2026-11-23 晚第四轮：识图时机、气泡带图、下载、标签条、图片缓存。

用户这轮报的六件事，拆成可以钉死的点：

1. **上传不许立刻识图**，点了发送才走「读图 → 拼进提示词」。
   以前 `upload()` 传完就 `describeInto()`，用户还没写完需求，读出来的
   描述先一步把输入框占了；白白烧一次视觉模型，还把用户想说的话顶掉。
2. **发出去的那条消息要带着参考图**（豆包同款），刷新后还在。
3. **卡片上要有下载**，落在复制和重画中间，能存到本机。
4. **AI 创作页头顶标签条滑不动**：`fillTabs` 每次重建都把 scrollLeft
   归零，用户手正按着滑的时候赶上一次加载回包，看起来就是卡死。
5. **页面中间右滑不该退出**（这条的断言在 test_workspace_ux_1123b）。
6. **图片别再让服务器每次重传**：`/api/gen/img` 原来是 `no-store`，
   每刷一次页面所有图都从 origin 重下一遍，走隧道就是绕地球一圈。
"""
import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def _read(rel):
    with open(os.path.join(ROOT, "web", "workspace", rel), encoding="utf-8") as f:
        return f.read()


class TestDescribeOnlyOnSend(unittest.TestCase):
    """1. 识图时机。"""

    def test_upload_no_longer_describes(self):
        js = _read("js/ref.js")
        i = js.index("function upload(")
        seg = js[i:js.index("function init()", i)]
        self.assertNotIn("describeInto(", seg,
                         "上传完不许立刻识图，要等用户点发送")
        self.assertIn("addRef(", seg, "上传只负责挂缩略图")

    def test_send_describes_before_submit(self):
        js = _read("js/chat.js")
        i = js.index("function send()")
        seg = js[i:js.index("function copyText", i)]
        self.assertIn("ensureDescribed", seg, "点发送才去读图")
        # 读图的等待必须在真正 submitJob 之前，顺序反了就白读。
        # 从最后一个 ensureDescribed 找起：函数里前面还有注释提到它。
        at = seg.rindex("ensureDescribed")
        self.assertLess(at, seg.index("submitJob", at), "读完才提交")

    def test_ensure_described_is_parallel_and_skips_read_ones(self):
        js = _read("js/ref.js")
        i = js.index("function ensureDescribed")
        seg = js[i:js.index("function drop(", i)]
        self.assertIn("!r.text", seg, "读过的跳过，别重复烧额度")
        self.assertIn("Promise.all", seg, "几张图并行读，不排队等")
        self.assertIn("busyNote", seg, "等待要有过场提示")

    def test_use_same_still_fills(self):
        """「画同款」是用户主动点的一下，仍然要立刻填描述。"""
        js = _read("js/ref.js")
        i = js.index("function useSame")
        self.assertIn("describeInto", js[i:i + 1400])


class TestBubbleKeepsRefs(unittest.TestCase):
    """2. 发出去的消息带着参考图。"""

    def test_paint_user_takes_refs(self):
        js = _read("js/chat.js")
        self.assertIn("function paintUser(text, refs)", js)
        self.assertIn("bubble-refs", js)

    def test_add_user_stores_brief_refs(self):
        js = _read("js/chat.js")
        i = js.index("function addUser(")
        seg = js[i:i + 500]
        self.assertIn("paintUser(text, refs)", seg)
        self.assertIn("refs: briefRefs(refs)", seg)

    def test_archive_replays_refs(self):
        js = _read("js/archive.js")
        self.assertIn("paintUser(m.t, m.refs || [])", js)

    def test_bubble_refs_have_style(self):
        css = _read("css/chat.css")
        self.assertIn(".bubble-refs {", css)
        self.assertIn(".bubble-refs img {", css)


class TestDownloadButton(unittest.TestCase):
    """3. 复制旁边要有下载。"""

    def test_three_buttons_in_order(self):
        js = _read("js/chat.js")
        i = js.index("function buildActs")
        seg = js[i:js.index("function downloadImage", i)]
        self.assertIn("下载", seg)
        self.assertLess(seg.index("acts.appendChild(copy)"),
                        seg.index("acts.appendChild(save)"))
        self.assertLess(seg.index("acts.appendChild(save)"),
                        seg.index("acts.appendChild(again)"))

    def test_download_uses_anchor(self):
        js = _read("js/chat.js")
        i = js.index("function downloadImage")
        seg = js[i:js.index("function shotEl", i)]
        self.assertIn("a.download =", seg, "用 <a download> 存文件，不走 canvas")
        self.assertIn("WSGen.img", seg, "地址仍走同源 /api/gen/img")
        # 下载拿的就是列表里那张（同一个 fit），不是另一份原图
        self.assertIn("WSGen.img(file, FIT)", seg)

    def test_card_keeps_its_file(self):
        """卡片得知道自己是哪张图，否则下载按钮拿不到文件名。"""
        js = _read("js/chat.js")
        i = js.index("function showImage")
        self.assertIn("card.file = job.file", js[i:i + 600])

    def test_archive_hands_file_to_acts(self):
        js = _read("js/archive.js")
        self.assertIn("file: m.file", js)


class TestTabsKeepScroll(unittest.TestCase):
    """4. 标签条重建不许把滚动位置清零。"""

    def test_fill_tabs_restores_scroll(self):
        js = _read("js/gallery.js")
        i = js.index("function fillTabs")
        seg = js[i:js.index("function markTabIntoView", i)]
        self.assertIn("scrollLeft", seg)
        # 先记后还
        self.assertLess(seg.index("var keep"), seg.index("innerHTML"))

    def test_selected_tab_scrolls_into_view(self):
        js = _read("js/gallery.js")
        self.assertIn("function markTabIntoView", js)
        self.assertIn("markTabIntoView()", js)


class TestImageCacheHeaders(unittest.TestCase):
    """6. 出图不再每次从 origin 重传。"""

    def test_gen_img_is_cacheable(self):
        from app.features.image import routes
        self.assertTrue(hasattr(routes, "_IMG_CACHE_CTL"))
        self.assertIn("max-age=", routes._IMG_CACHE_CTL)
        self.assertNotIn("no-store", routes._IMG_CACHE_CTL)

    def test_no_no_store_left_in_img_route(self):
        with open(os.path.join(ROOT, "app", "features", "image", "routes.py"),
                  encoding="utf-8") as f:
            src = f.read()
        i = src.index("@bp.route(\"/api/gen/img\")")
        seg = src[i:]
        # docstring 里可以讲历史（`no-store` 出现在反引号里），代码里不许再设
        self.assertNotIn('headers["Cache-Control"] = "no-store"', seg)
        self.assertIn("conditional=True", seg, "要能回 304")


if __name__ == "__main__":
    unittest.main()
