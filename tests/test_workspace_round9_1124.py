# -*- coding: utf-8 -*-
"""2026-11-24 第四批：状态卡片只占一行 + 群友的东西全部收进「工作台」。

用户两条原话：

1. 「群友测试账号，网络报错或者已经有一张在排队了，都是使用一个大卡片显示的，
   我点击重画也是一个大卡片，直接占据大量冗余空间……每次滑动都是一大堆的大卡片，
   影响观看。然后这些上一张还在画，或者网络出问题的小提示，直接一行字显示即可，
   然后用户还是不断地点击重画就直接闪烁一下那行字，高亮一下即可，然后逐渐消失，
   不要遮挡画面」

2. 「网页端的工作台，要单独一个文件夹存，叫做"工作台"，不要和我的 web 混淆啊，
   只要不是我这边的东西，都给我放到工作台的文件夹里面，我不是有一个群友漫画台的
   吗？这个就存群友的 moss 漫画和其他的漫画，工作台就存群友网页端的生图，
   你不要什么东西都放到我自己的文件夹里面 D:\\AI\\ComfyUI-nightly\\output\\web
   这个是我自己用的文件夹」

这两条都是「改口径」，所以下面钉的是**落点和结构**，不是文案细节。
"""
import io
import os
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
WS = os.path.join(ROOT, "web", "workspace")


def _read(rel):
    with io.open(os.path.join(WS, rel), "r", encoding="utf-8") as f:
        return f.read()


# ─────────────────────────────────────────────────────────
# 1. 状态卡片：没有图就只是一行字
# ─────────────────────────────────────────────────────────

class TestIdleCardIsOneLine(unittest.TestCase):
    """排队 / 报错 / 还没出图 —— 只有一行字，不占 340×3:4 的大壳。"""

    def test_shell_starts_idle(self):
        s = _read("js/card.js")
        i = s.index("function shell(cls)")
        seg = s[i:i + 900]
        self.assertIn('"card is-idle"', seg,
                      "卡片壳默认必须是 idle 态，否则排队时就先撑出一张大卡")

    def test_idle_css_drops_padding_and_box(self):
        css = _read("css/chat.css")
        self.assertIn(".card.is-idle", css)
        i = css.index(".card.is-idle {")
        seg = css[i:i + 420]
        for k in ("border: 0", "background: transparent", "box-shadow: none"):
            self.assertIn(k, seg, "idle 态没把壳的边框/底纹/阴影去掉")

    def test_idle_hides_placeholder_area(self):
        """没东西的时候 `.card-body` 整个不显示：那才是 3:4 那块的来源。"""
        css = _read("css/chat.css")
        self.assertIn(".card.is-idle .card-body { display: none; }", css)

    def test_status_and_stop_share_one_line(self):
        css = _read("css/chat.css")
        self.assertIn(".card-line", css)
        s = _read("js/card.js")
        self.assertIn('line.className = "card-line"', s)
        self.assertIn("line.appendChild(note)", s)
        self.assertIn("line.appendChild(stop)", s,
                      "终止没跟状态文字挤在同一行，等于多占一行")

    def test_placeholder_only_after_paint_starts(self):
        """占位块只在「真开始画」时才挂（gen_flow 的 running 分支）。"""
        s = _read("js/gen_flow.js")
        i = s.index('} else if (ph === "running")')
        seg = s[i:i + 320]
        self.assertIn("H.spawn(card)", seg)

    def test_new_card_has_no_placeholder(self):
        s = _read("js/chat.js")
        i = s.index("function newCard(prompt, skill, rec)")
        seg = s[i:i + 520]
        self.assertIn("ph: null", seg)
        self.assertNotIn('className = "ph"', seg,
                         "新建卡片时还顺手挂了一块 3:4 的占位")

    def test_fail_drops_the_placeholder(self):
        s = _read("js/chat.js")
        i = s.index("function fail(card, msg, stuck)")
        seg = s[i:i + 1400]
        self.assertIn("card.ph.remove()", seg,
                      "失败后没把占位块撤掉，又会留一张空壳")
        self.assertIn('classList.add("is-idle")', seg)

    def test_archive_replay_has_no_big_shell(self):
        s = _read("js/archive.js")
        i = s.index('} else if (m.k === "img") {')
        seg = s[i:s.index('} else if (m.k === "comic")', i)]
        self.assertNotIn('className = "ph"', seg,
                         "重放没跑完的那条还在铺 3:4 的大灰块")


class TestRepeatClickOnlyBlinks(unittest.TestCase):
    """连着点重画：那一行字闪一下，不许越点越多张卡。"""

    def test_submit_reuses_twin(self):
        s = _read("js/gen_flow.js")
        i = s.index("function submit(prompt, skill, refs, described)")
        seg = s[i:s.index("function findTwin", i)]
        self.assertIn("findTwin(prompt, skill)", seg)
        self.assertIn("WSCard.flash", seg, "还在跑的那张只该闪一下")
        self.assertIn("restart(", seg, "已经失败的那张要复用，不能新开")

    def test_twin_identity_is_the_users_own_words(self):
        """认「用户那句话」(`ask`)，不是被服务端换掉后的最终 tags。

        2026-11-24 实测：提交成功后 `rec.prompt` 被换成翻译/拼好的英文 tags，
        再按它比就永远对不上，连点三次能叠三张卡——正是用户报的现象。
        """
        s = _read("js/gen_flow.js")
        self.assertIn("ask: prompt", s, "记录里没留用户原话，认不出同一次请求")
        i = s.index("function findTwin")
        seg = s[i:i + 900]
        self.assertIn("(m.ask || m.prompt) === prompt", seg)
        self.assertNotIn("m.prompt === prompt", seg,
                         "又按最终 tags 比了，连点会叠卡")

    def test_blink_animation_exists(self):
        css = _read("css/chat.css")
        self.assertIn("@keyframes ws-blink", css)
        self.assertIn(".card-line .note.blink", css)

    def test_blink_restarts_on_each_click(self):
        s = _read("js/card.js")
        i = s.index("function flash(card)")
        seg = s[i:i + 700]
        self.assertIn('classList.remove("blink")', seg)
        self.assertIn("void n.offsetWidth", seg,
                      "不强制重排，连点时动画不会重播")

    def test_reduced_motion_still_highlights(self):
        css = _read("css/chat.css")
        i = css.index("prefers-reduced-motion")
        self.assertIn(".card-line .note.blink", css[i:],
                      "关动画之后连高亮都没了，等于点了没反应")


# ─────────────────────────────────────────────────────────
# 2. 目录：站主的 web 只放站主的，群友全进「工作台」
# ─────────────────────────────────────────────────────────

class TestFriendOutputGoesToWorkbench(unittest.TestCase):

    def test_web_target_lands_under_workbench(self):
        from app.features.image import comfy_out
        self.assertEqual(comfy_out.session_base(None, None), "工作台/生图页")
        self.assertEqual(comfy_out.session_base(None, "123"), "工作台/生图页")

    def test_qq_targets_are_untouched(self):
        from app.features.image import comfy_out
        self.assertEqual(comfy_out.session_base("group", "1"), "qq/群_1")
        self.assertEqual(comfy_out.session_base("private", "1"), "qq/私聊_1")

    def test_friend_comic_book_fits_five_segments(self):
        """`工作台/群友漫画台/<QQ>/<作品>/<渠道>` 五段，不能被截掉渠道。"""
        from app.features.image import comfy_out
        self.assertGreaterEqual(comfy_out._MAX_DEPTH, 5)
        body = comfy_out._join("工作台", "群友漫画台", "10086", "某本", "silver")
        self.assertEqual(body.split("/")[-1], "silver")

    def test_friend_moss_root_under_workbench(self):
        from app.features.comic import moss_api
        self.assertTrue(moss_api.OUT_ROOT_FRIEND.startswith("工作台/"),
                        "群友 moss 还没进工作台")
        self.assertEqual(moss_api.OUT_ROOT, "web/moss漫画",
                         "站主自己的 moss 根不该被动")

    def test_friend_moss_falls_back_to_old_root(self):
        """白天那版出过的图在 `群友moss漫画/`，打开那本不能一片空白。"""
        from app.features.comic import moss_api
        self.assertEqual(moss_api.OUT_ROOT_FRIEND_OLD, "群友moss漫画")
        self.assertIn("def out_roots", io.open(
            moss_api.__file__, encoding="utf-8").read())

    def test_friend_web_gen_dir(self):
        import io as _io
        from app.features.qq import friend_api
        src = _io.open(friend_api.__file__, encoding="utf-8").read()
        self.assertIn('out_dir="工作台/群友页_" + qq', src,
                      "群友网页生图还没进工作台")

    def test_comic_zip_goes_with_the_book(self):
        from app.features.comic import delivery, quota
        src = io.open(delivery.__file__, encoding="utf-8").read()
        self.assertIn('"工作台"', src)
        self.assertIn("comic_quota.FRIEND_COMIC_ROOT", src,
                      "zip 和成图必须拼同一棵目录树")
        self.assertEqual(quota.FRIEND_COMIC_ROOT, "群友漫画台")

    def test_status_labels_workbench_jobs(self):
        from app.features.image import jobs

        class _J(object):
            target = None
            out_dir = "工作台/群友页_123"
            batch = ""
        self.assertEqual(jobs._src_label(_J()), "工作台·群友页_123")


class TestMigrationTool(unittest.TestCase):
    """旧图不会自己走动，得有一把可回滚的搬迁工具。"""

    def test_tool_exists_and_is_readonly_by_default(self):
        p = os.path.join(ROOT, "tools", "migrate_friend_output.py")
        self.assertTrue(os.path.isfile(p))
        s = io.open(p, encoding="utf-8").read()
        self.assertIn("--apply", s)
        self.assertIn("--undo", s, "搬错了退不回去不行")

    def test_plan_leaves_owner_web_alone(self):
        import sys
        sys.path.insert(0, os.path.join(ROOT, "tools"))
        import migrate_friend_output as m
        olds = [r[0] for r in m.RULES]
        self.assertFalse([o for o in olds if o == "web" or o.startswith("web/")],
                         "迁移规则里出现了站主自己的 web/")


if __name__ == "__main__":
    unittest.main()
