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


class TestNoDescribeOnSend(unittest.TestCase):
    """1. 识图时机（2026-11-27 改口径）：前端不再识图。

    用户拍板：图跟着这句话一起交给后端多模态模型，**一次调用**出词。
    以前那套「上传等发送才读 / 点发送并行读 / 画同款先填描述」全部下线——
    它们加起来是两次模型调用，而第二次根本没看到图。
    """

    def test_upload_only_attaches(self):
        js = _read("js/ref.js")
        i = js.index("function upload(")
        seg = js[i:js.index("function init()", i)]
        self.assertIn("addRef(", seg, "上传只负责挂缩略图")
        self.assertNotIn("describeInto(", seg)

    def test_send_does_not_describe(self):
        js = _read("js/chat.js")
        i = js.index("function send()")
        seg = js[i:js.index("function sendDirect", i)]
        self.assertNotIn("ensureDescribed(", seg, "发送前不许再读图")
        self.assertIn("submitJob", seg)

    def test_ref_js_has_no_describe_helpers(self):
        js = _read("js/ref.js")
        for dead in ("ensureDescribed", "describeOnly", "describeInto"):
            self.assertNotIn(dead, js, "前端识图残留：%s" % dead)

    def test_use_same_does_not_pre_read(self):
        """「画同款」只挂图 + 弹键盘，不再立刻发一次识图请求。"""
        js = _read("js/ref.js")
        i = js.index("function useSame")
        seg = js[i:i + 1200]
        self.assertIn("addRef(", seg)
        self.assertNotIn("describeInto", seg)


class TestBubbleKeepsRefs(unittest.TestCase):
    """2. 发出去的消息带着参考图。"""

    def test_paint_user_takes_refs(self):
        # 气泡长相层 2026-11-24 拆到了 card.js（chat.js 只留一层转发）
        js = _read("js/card.js")
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
    """3. 复制旁边原来要有下载；2026-11-27 用户拍板换成「引用」。"""

    def test_three_buttons_in_order(self):
        """复制 | 引用 | 重画，顺序不许乱（下载按钮已换成引用）。"""
        # 三个按钮的拼装 2026-11-24 拆到了 card.js
        js = _read("js/card.js")
        i = js.index("function buildActs")
        seg = js[i:js.index("function copyText", i)]
        self.assertIn("引用", seg)
        self.assertNotIn("下载到本机", seg, "下载入口已按用户要求撤掉")
        self.assertLess(seg.index("acts.appendChild(copy)"),
                        seg.index("acts.appendChild(quote)"))
        self.assertLess(seg.index("acts.appendChild(quote)"),
                        seg.index("acts.appendChild(again)"))

    def test_quote_hands_over_the_prompt_not_the_image(self):
        """引用交出去的是**那张图当初的提示词**，不是图的字节（2026-11-27）。

        用户原话：「引用的其实是那一个提示词……AI 就直接去改提示词，然后
        重新跑一遍就可以了」。所以 `quoteCard` 不取图、不上传、不进 refs，
        只把 `{file, prompt}` 记进 `S.quote`；发送时当 anchor 交给后端。
        这条路不碰图生图，也就不会撞上「渠道没有 i2i 骨架」那条报错。"""
        js = _read("js/ref.js")
        i = js.index("function quoteCard")
        seg = js[i:js.index("function init", i)]
        self.assertIn("S.quote = {", seg)
        self.assertIn("file: file", seg)
        self.assertIn("prompt: prompt", seg)
        self.assertNotIn("chatUpload", seg, "引用不该把图传上去")
        self.assertNotIn("addRef", seg, "引用不进参考图列表")
        self.assertNotIn("pre: true", seg, "引用不再需要识图标记")
        self.assertIn("say.focus()", seg, "点引用要立刻把键盘叫起来")
        self.assertNotIn("el.say.value =", seg, "不许把原 tags 灌进输入框")

    def test_download_uses_anchor(self):
        """下载**落在本机**这一步用 `<a download>`；取字节走本地缓存优先。

        2026-11-24 口径变了（用户：「点下载得等七八秒」）：下载的是列表上
        那张压缩档，字节本地缓存里就有，所以取字节从 `download()`（shot.js）
        改成「缓存 → fetch」两步；`<a download>` 只在 `saveBlob()` 里。
        """
        js = _read("js/shot.js")
        i = js.index("function download")
        seg = js[i:js.index("function saveBlob", i)] if "function saveBlob" in js[i:] else js[i:]
        self.assertIn("global.WSGen.img(file, FIT)", seg, "下载拿的就是列表那张（同一个 fit）")
        self.assertIn("global.WSImg", seg, "先查本地缓存，别重下 8MB 原图")
        # 存盘那步仍是 <a download>，不走 canvas
        j = js.index("function saveBlob")
        blob_seg = js[j:j + 600]
        self.assertIn("a.download =", blob_seg, "用 <a download> 存文件，不走 canvas")

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
        # 2026-11-24：口径升级——不再「清空再写回 scrollLeft」，而是**根本不
        # 清空容器**（手指正按着的那一刻，连清空再写回都来不及）。
        # 现在算通过的不是「先记后还」，而是「不出现 innerHTML = \"\"」。
        self.assertNotIn('el.tabs.innerHTML = ""', seg,
                         "fillTabs 不许整条重建标签栏（会把横向滚动位置清零）")
        self.assertIn("dataset.key", seg)

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
