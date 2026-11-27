"""workspace 画廊模块（AI 创作）的静态守卫。

守住用户 2026-11-23 定的几条：顶部只有文件夹横滑（无搜索、无「画廊」标题行）、
卡片只放缩略图 + 名字 + 编号、两列、点开有大图和收藏/下载、接口全部同源复用
`/api/gal/*`。测试只看文件结构与调用路径，不跑浏览器。
"""
import json
import os
import re
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WS = os.path.join(ROOT, "web", "workspace")

FILES = ("css/gallery.css", "js/gallery.js", "js/hide.js",
         "js/gallery_shell.js")


def _read(rel):
    with open(os.path.join(WS, rel), encoding="utf-8") as f:
        return f.read()


class TestWorkspaceGalleryFiles(unittest.TestCase):

    def test_module_files_exist_and_stay_small(self):
        for rel in FILES:
            path = os.path.join(WS, rel)
            self.assertTrue(os.path.isfile(path), "缺少模块文件: " + rel)
            with open(path, encoding="utf-8") as f:
                lines = sum(1 for _ in f)
            self.assertLess(lines, 600, rel + " 超过 600 行，需拆分")

    def test_chat_page_loads_module(self):
        html = _read("chat.html")
        self.assertIn("css/gallery.css", html)
        self.assertIn("js/gallery.js", html)
        # views.js 只提供注册口，gallery 自己注册进来
        self.assertLess(html.index("js/views.js"), html.index("js/gallery.js"))


class TestWorkspaceGalleryApi(unittest.TestCase):

    def test_only_same_origin_gallery_paths(self):
        js = _read("js/gallery.js")
        for path in ("/api/gal/list", "/api/gal/work/", "/api/gal/img/",
                     "/api/gal/mark"):
            self.assertIn(path, js, "缺少接口: " + path)
        self.assertNotIn("http://", js)
        self.assertNotIn("https://", js)

    def test_paging_uses_has_more(self):
        js = _read("js/gallery.js")
        self.assertIn("has_more", js)
        self.assertIn("page", js)

    def test_folder_filter_is_a_param_not_a_reload(self):
        js = _read("js/gallery.js")
        self.assertIn("grp=", js, "分类要作为 grp 参数传给列表接口")


class TestWorkspaceGalleryUi(unittest.TestCase):

    def test_card_shows_name_and_number_only(self):
        js = _read("js/gallery.js")
        self.assertIn("work.title", js)
        self.assertIn('"#"', js, "卡片要带作品编号")
        # 来源 / 时间 / 点赞数不进卡片
        self.assertNotIn("work.by", js)
        self.assertNotIn("likes", js)

    def test_no_search_box_in_topbar(self):
        js = _read("js/gallery.js")
        self.assertNotIn("search", js.lower())
        self.assertNotIn("搜索", js)

    def test_viewer_has_download_and_fav(self):
        js = _read("js/gallery.js")
        self.assertIn("下载", js)
        self.assertIn("收藏", js)
        self.assertIn("?s=v", js, "大图要用 v 尺寸")

    def test_two_column_grid(self):
        css = _read("css/gallery.css")
        self.assertIn("grid-template-columns", css)
        self.assertIn("repeat(2", css)

    def test_viewer_image_shows_whole_picture(self):
        """查看器大图要整张缩进一屏，不能被裁成脸部特写（用户 2026-11-23）。"""
        css = _read("css/gallery.css")
        i = css.index(".gv-slide img")
        seg = css[i:i + 380]
        self.assertIn("height: calc(100% - 12px)", seg,
                      "必须给图片框一个确定高度，百分比在 grid 里会失效")
        self.assertIn("object-fit: contain", seg)
        self.assertNotIn("object-fit: cover", seg)
        j = css.index(".gv-slide {")
        self.assertIn("position: relative", css[j:i])

    def test_styles_use_tokens(self):
        css = _read("css/gallery.css")
        self.assertIn("var(--card", css)
        self.assertIn("var(--text", css)
        self.assertIn("var(--shadow", css)


class TestListLoadClaim(unittest.TestCase):
    """列表的「谁的回包算数」（`js/gallery_load.js`）。

    2026-11-26 用户实测：「上边的滚动条滑动再点击，内容跟标签对不上」。
    上一版只修了「换分类被 busy 吞掉」，漏了**续页那一发沿用当前序号**：
    翻页还在飞的时候点新分类，旧翻页的包回来序号仍等于当前号，`owns()`
    放它进来，它按自己那一页的 `page` 往新分类的墙里塞卡片，`S.page` 同时
    被改，下一屏还会跳页。
    """

    def test_rest_claim_is_one_shot(self):
        j = _read("js/gallery_load.js")
        self.assertIn("function owns(n, rest)", j)
        self.assertIn("if (!busy) return false;", j)
        # 换分类那发回来要放掉 busy（见下面那条实证），不能直接 return true
        self.assertNotIn("if (!rest) return true;", j)

    def _machine(self, steps):
        """真跑一遍发号机，把每步结果串回来。

        光断言源码拼不出状态机的问题——`owns()` 少清一个 `busy`，代码长得
        完全正常，只有按顺序走一遍才露出来。没有 node 就跳过（这台机器有）。
        """
        script = (
            "const fs=require('fs');const w={};"
            "new Function('window',fs.readFileSync(process.argv[1],'utf8'))(w);"
            "const m=w.WSTake.it();const out=[];"
            + "".join("out.push(%s);" % s for s in steps) +
            "process.stdout.write(JSON.stringify(out));"
        )
        try:
            got = subprocess.check_output(
                ["node", "-e", script, os.path.join(WS, "js", "gallery_load.js")],
                stderr=subprocess.STDOUT, timeout=60)
        except (OSError, subprocess.SubprocessError) as e:
            self.skipTest("没有 node，跳过状态机实证：" + str(e))
        return json.loads(got.decode("utf-8").strip())

    def test_first_page_response_releases_the_slot(self):
        """首屏那发回来必须放掉 busy，否则续页永远发不出去。

        2026-11-27 用户实测：「MOSS 漫画的加载更多按钮失效了」，登录态和游客
        一样，换哪个分类都一样。`owns()` 原来对 `!rest` 直接 `return true`、
        一个字都不清，`done()` 全仓又没人调 —— 首屏一回 busy 就永久为真，
        `claim(false)` 从此全部返回 null，按钮点了没反应、滚到底也不加页。
        """
        self.assertEqual(self._machine([
            "m.claim(true)",      # 进页面 / 换分类：1
            "m.owns(1,false)",    # 首屏回包：认领
            "m.claim(false)",     # 点「加载更多」：必须发得出去
        ]), [1, True, 1])

    def test_rest_response_is_still_one_shot(self):
        """翻页回包认领一次就关；换成新分类后旧翻页那发照样作废。"""
        self.assertEqual(self._machine([
            "m.claim(true)",      # 首屏 1
            "m.owns(1,false)",
            "m.claim(false)",     # 续页（沿用号 1，busy 占上）
            "m.claim(false)",     # 再点：不许并发
            "m.owns(1,true)",     # 续页回包：认领
            "m.owns(1,true)",     # 重复认领：不算
            "m.claim(true)",      # 换分类 → 号 2
            "m.owns(1,true)",     # 旧续页回包：号对不上，作废
            "m.claim(false)",     # 新分类首屏还没回：不许抢跑
            "m.owns(2,false)",    # 新分类回包
            "m.claim(false)",     # 又发得出去
        ]), [1, True, 1, None, True, False, 2, False, None, True, 2])

    def test_head_guard_does_not_block_new_category(self):
        j = _read("js/gallery.js")
        head = j.split("function load(reset)")[1].split("var page = S.page;")[0]
        self.assertNotIn("if (S.busy) return;", head,
                         "换分类不能继续被 busy 挡回去")

    def test_load_passes_rest_flag(self):
        self.assertIn("S.take.owns(seq, !reset)", _read("js/gallery.js"))

    def test_machine_is_callable(self):
        """发号机必须是**函数**，不能是 `{claim: …}` 这样的对象字面量。

        2026-11-26 生产事故：字面量挂在 `S.take` 上之后，`S.take(reset)` 在
        那台机器的运行环境里取出来变成了对象本身，`load()` 在发请求前就抛
        TypeError，用户看到的是「点 AI 创作什么都没有」，换浏览器也一样。
        现在 `it()` 返回函数本体（调用走 `.claim()`），这条断言钉住形态。"""
        j = _read("js/gallery_load.js")
        self.assertIn("claim.claim = claim;", j)
        self.assertIn("return claim;", j)
        self.assertNotIn("return { claim: claim", j)
        self.assertIn("S.take.claim(reset)", _read("js/gallery.js"))

    def test_load_script_precedes_gallery(self):
        html = _read("chat.html")
        i = html.index("js/gallery_load.js")
        self.assertLess(i, html.index("js/gallery.js?"))
        self.assertEqual(re.search(r"gallery_load\.js\?v=(\w+)", html).group(1),
                         re.search(r"gallery\.js\?v=(\w+)", html).group(1),
                         "两个文件同版本号，否则只更新一半")


if __name__ == "__main__":
    unittest.main()
