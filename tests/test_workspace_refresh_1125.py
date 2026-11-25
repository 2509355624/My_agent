# -*- coding: utf-8 -*-
"""2026-11-25：刷新页面后「任务跑失败了」+「图出来了还在转圈」+ 存档被撑大。

用户原话（三段）：

1. 「当我带参考图的时候，我点击发送，然后我刷新一下那个网页，它就会直接说
   该任务跑失败了。好奇怪，它不应该出现这样一个提示，因为我们任务已经提交
   到那一个服务端了……我刷新网页回来，它应该说它正在跑才对。」

2. 「我的图已经跑出来了，然后它那个卡片那里还会显示正在跑……还在转圈圈，
   但图片已经出来了，它还在转圈圈。」

3. 「参考图那个又消失了」—— 表面是参考图，根子是记录里存了不该存的东西。

下面钉的是**事实**，不是文案：记录里不许出现 DOM 节点、陈年记录不许去问
服务端、同一张卡一轮只许处理一次、认领窗口有上界。
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


def _js(rel):
    return _read(os.path.join("js", rel))


def _strip_comments(s):
    """去掉 // 行注释和 /* */ 块注释，只留代码。

    断言「某字段名不许出现在代码里」时得先把注释摘掉——否则一句解释性的
    注释就能让测试失败，而它恰恰是要说明这件事的。"""
    import re
    s = re.sub(r"/\*.*?\*/", "", s, flags=re.S)
    s = re.sub(r"(?m)^\s*//.*$", "", s)
    return s


class TestRecordCarriesNoDomNode(unittest.TestCase):
    """记录会被整个 JSON.stringify 推给 /api/chat/archive。

    把一个气泡 DOM 节点挂上去，序列化之后就是
    `{"_wsUserId":…,"_wsUserText":…,"_failLine":null}`——那串 _wsUserText
    正是整段英文 tags，于是同一条记录里同一段提示词存了两份。
    """

    def test_submit_does_not_write_node_into_record(self):
        s = _js("gen_flow.js")
        self.assertNotIn("rec.userNode =", s,
                         "又把气泡节点写进记录了：存档会被撑成一倍")
        # 只允许留这两个字符串字段——认人气泡用它俩就够
        self.assertIn("rec.userId =", s)
        self.assertIn("rec.userText =", s)

    def test_restart_does_not_read_node_from_record(self):
        s = _js("gen_flow.js")
        # 注释里会提到这个字段名（说明它是怎么来的），只看代码本身
        code = _strip_comments(s)
        self.assertNotIn("rec.userNode", code,
                         "还在从旧记录里取节点：那是 DOM 不是数据")

    def test_replay_strips_legacy_node(self):
        s = _js("archive.js")
        self.assertIn("delete m.userNode", s,
                      "旧存档里已经写进去的节点没人清，历史会一直胖着")


class TestRefreshDoesNotBlameTheUser(unittest.TestCase):
    """点了发送、页面在回填 job 号之前刷新 —— 记录里没有 job 号。

    以前一律判「这条没跑完就断了」，可服务端明明还排着队。
    """

    def test_only_fresh_orphans_ask_the_server(self):
        s = _js("archive.js")
        self.assertIn("function willCheck", s)
        self.assertIn("FRESH_MS", s)
        # 陈年记录不该白打接口
        i = s.index("function willCheck")
        body = s[i:i + 400]
        self.assertIn("FRESH_MS", body, "没有时间上界：所有陈年记录都会去问")

    def test_client_calls_adopt(self):
        self.assertIn("adopt", _js("gen.js"), "前端没接认领接口")
        self.assertIn("WSGen.adopt", _js("archive.js"))

    def test_orphan_goes_through_adopt_before_being_failed(self):
        s = _js("archive.js")
        i = s.index("a.orphan")
        body = s[i:i + 400]
        self.assertIn("promise(a.rec)", body,
                      "认领了却没真去问，等于没认")


class TestAdoptWindow(unittest.TestCase):
    """认领靠时间，窗口必须够紧——宽了会把前后两次提交认成同一条。"""

    def test_window_is_bounded(self):
        with io.open(os.path.join(
                ROOT, "app", "features", "image", "routes.py"),
                encoding="utf-8") as f:
            s = f.read()
        self.assertIn("ADOPT_WINDOW", s)
        i = s.index("ADOPT_WINDOW = ")
        val = s[i:i + 40].split("=")[1].split()[0]
        self.assertLessEqual(float(val), 300.0,
                             "窗口放到 5 分钟以上就会认错人")

    def test_only_running_jobs_are_adopted(self):
        with io.open(os.path.join(
                ROOT, "app", "features", "image", "routes.py"),
                encoding="utf-8") as f:
            s = f.read()
        i = s.index("def api_adopt")
        body = s[i:i + 2000]
        self.assertIn('("queued", "running")', body,
                      "把已结束的任务也认领回来，前端会替别的记录收尾")
        self.assertIn('"owner"', body, "没按人隔离，会认到别人的任务")


class TestImageArrivedStopsSpinning(unittest.TestCase):
    """图出来了，状态行必须彻底收掉（不只是 hidden）。"""

    def test_show_image_clears_running_class(self):
        s = _js("gen_flow.js")
        i = s.index("function showImage")
        body = s[i:i + 2000]
        self.assertIn("setNote", body,
                      "只 hidden 不摘动画类，下一轮又被画回「转圈」")
        self.assertIn("run: false", body)

    def test_one_card_handled_once_per_tick(self):
        s = _js("gen_flow.js")
        i = s.index("function tick")
        body = s[i:i + 1500]
        self.assertIn("seenCard", body,
                      "同一张卡挂在两个键下会被处理两遍：出完图又写回「正在跑」")

    def test_forget_clears_by_identity(self):
        s = _js("gen_flow.js")
        self.assertIn("function forget", s)
        i = s.index("function forget")
        body = s[i:i + 500]
        self.assertIn("pending[k] === card", body,
                      "只按一个键删，别的键还挂着这张卡")


if __name__ == "__main__":
    unittest.main()
