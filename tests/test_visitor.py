# -*- coding: utf-8 -*-
"""访客模式（2026-11-26）：身份 / 每天 5 张 / **只限工作台**。

用户拍板的口径（原话摘录）：

    「访客直接点就进去了，能生图」
    「访客模式只局限于工作台，其他的东西不要给我变」

⚠️ 2026-11-26 后半场最重要的一条：**访客模式只属于工作台**。它上一版按
「路径白名单」收口，结果把画廊、漫画台、角色扮演、小说、跑图台连带群聊
一起挡成了「访客模式用不了这个功能」，主业务跟着塌。现在改为按**发起方**
判：只有 5175 工作台页面发出的请求才算访客，别处带着访客 cookie 一律当
它不存在。

钉死几条容易悄悄漂掉的边界：

  1. 身份串必须**签名**：自己编一个 id 不算数（否则改个字符就换新额度）。
  2. 额度按**身份串**记而不是整条 cookie：续签一次不能白送 5 张。
  3. 第 6 张不扣、也不给过；跨天（北京时间）自动回到 5 张。
  4. **工作台以外不算访客**（见 `GuestScopeTest`）：答复跟没带 cookie 一样。
  5. 5174 侧必须先验内部令牌，再验访客头；伪造的头当没带。
  6. 用户已改口「访客不存聊天记录」：存档不做，前端只留本地一份。
"""

import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask

from app.gateways import friend_auth
from app.gateways import visitor


def _reload():
    """每个用例拿一份干净的内存账本。"""
    visitor.reset()
    visitor._CACHE = None


class _FakeReq(object):
    """给 `visitor.current()` 用的最小请求壳。"""

    def __init__(self, headers):
        self.headers = headers


class GuestIdentityTest(unittest.TestCase):
    def setUp(self):
        _reload()
        self.addCleanup(_reload)

    def test_signed_id_roundtrip(self):
        raw = visitor.new_id()
        gid = visitor.check_id(raw)
        self.assertTrue(gid)
        self.assertEqual(len(gid), 16)

    def test_forged_id_rejected(self):
        # 自己编一个 id + 编个过期时间，没有签名 → 不算
        self.assertIsNone(visitor.check_id("deadbeefdeadbeef.9999999999.nope"))
        # 签名对但身份段被换掉
        good = visitor.new_id()
        parts = good.split(".")
        self.assertIsNone(visitor.check_id("ffffffffffffffff.%s.%s"
                                           % (parts[1], parts[2])))

    def test_expired_id_rejected(self):
        gid = "abcdefgh12345678"
        exp = 1000000000                       # 2001 年
        raw = "%s.%d.%s" % (gid, exp, visitor._sign(gid, exp))
        self.assertIsNone(visitor.check_id(raw))

    def test_resign_keeps_same_identity(self):
        """再点一次「先逛逛」必须还是同一个人。

        2026-11-26 用户实测：「点登录 → 回登录页 → 再点逛逛，次数会被刷新」。
        额度按 `id` 段记账，而 `/login/visitor` 原来每次都 `new_id()`，等于
        每点一次领一张新证、永远 5 张。`resign()` 复用 id、只续期。
        """
        raw = visitor.new_id()
        gid = visitor.check_id(raw)
        for _ in range(3):
            raw = visitor.resign(raw)
            self.assertEqual(visitor.check_id(raw), gid)
        # 签名随过期时间一起换，别拿旧签名硬套新过期
        self.assertEqual(len(raw.split(".")), 3)
        self.assertNotEqual(raw, visitor.new_id())

    def test_resign_replaces_broken_cookie(self):
        """手上那张已经过期 / 被改坏 → 签一张新的，不能把人锁在门外。"""
        self.assertNotEqual(visitor.check_id(visitor.resign("garbage")),
                            visitor.check_id("garbage"))
        self.assertTrue(visitor.check_id(visitor.resign(None)))

    def test_junk_never_raises(self):
        for junk in (None, "", ".", "..", "a.b.c", "1.2.3.4.5", 123):
            self.assertIsNone(visitor.check_id(junk))

    def test_current_requires_internal_token(self):
        raw = visitor.new_id()
        good = friend_auth.secret()
        # 令牌对 → 认
        with mock.patch("flask.request",
                        _FakeReq({"X-Friend-Token": good,
                                  "X-Friend-Guest": raw})):
            got = visitor.current()
            self.assertTrue(got["id"])
            self.assertEqual(got["raw"], raw)
        # 没令牌 → 不认（哪怕访客头签名是对的）
        with mock.patch("flask.request",
                        _FakeReq({"X-Friend-Guest": raw})):
            self.assertEqual(visitor.current()["id"], "")
        # 令牌错 → 不认
        with mock.patch("flask.request",
                        _FakeReq({"X-Friend-Token": "wrong-token",
                                  "X-Friend-Guest": raw})):
            self.assertEqual(visitor.current()["id"], "")


class GuestQuotaTest(unittest.TestCase):
    def setUp(self):
        _reload()
        self.addCleanup(_reload)
        self.gid = visitor.check_id(visitor.new_id())

    def test_five_then_blocked(self):
        self.assertEqual(visitor.remaining(self.gid), 5)
        for i in range(5):
            ok, left = visitor.consume(self.gid)
            self.assertTrue(ok, "第 %d 张应该放行" % (i + 1))
            self.assertEqual(left, 4 - i)
        ok, left = visitor.consume(self.gid)
        self.assertFalse(ok)
        self.assertEqual(left, 0)
        # 第 6 张被拒之后，计数不能再涨（不能扣成负数）
        self.assertEqual(visitor.snapshot(self.gid)["used"], 5)

    def test_refund_never_negative(self):
        visitor.refund(self.gid, 3)
        self.assertEqual(visitor.remaining(self.gid), 5)

    def test_quota_keyed_by_identity_not_cookie(self):
        """同一个人续签一次 cookie，额度不能被重置。"""
        visitor.consume(self.gid)
        again = visitor.check_id(visitor.new_id())     # 换一条 cookie
        self.assertNotEqual(again, self.gid)
        self.assertEqual(visitor.remaining(self.gid), 4)
        # 换个 id 才是新额度
        self.assertEqual(visitor.remaining(again), 5)

    def test_day_rollover(self):
        visitor.consume(self.gid)
        self.assertEqual(visitor.remaining(self.gid), 4)
        with mock.patch.object(visitor, "_today", return_value="2099-01-01"):
            self.assertEqual(visitor.remaining(self.gid), 5)
            ok, left = visitor.consume(self.gid)
            self.assertTrue(ok)
            self.assertEqual(left, 4)

    def test_unknown_identity_gets_nothing(self):
        self.assertEqual(visitor.remaining(""), 0)
        self.assertEqual(visitor.consume(""), (False, 0))


class GuestScopeTest(unittest.TestCase):
    """访客模式只属于工作台（2026-11-26 用户拍死的一条）。

    以前这里是「工作台能打哪些接口」的白名单。问题就出在这个思路：按**路径**
    判，判不出「是谁在调」，于是同一批 `/api/gal/*` 被工作台的规则扣到了画廊、
    跑图台、漫画台上，把主业务一起挡了。现在改判**发起方**——看这次请求是不是
    工作台发出来的。所以这里钉的是「工作台以外一律不算访客」。
    """

    def setUp(self):
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        import friend_app
        from app.gateways import visitor as v
        self.fa = friend_app
        self.v = v
        self.app = friend_app.app
        self.app.config["TESTING"] = True

    def _here(self, referer, host="localhost"):
        h = {"Referer": referer} if referer else {}
        with self.app.test_request_context("/x", headers=h,
                                           base_url="http://" + host):
            return self.fa._jf_here()

    def test_workspace_counts(self):
        for ref in ("http://localhost/workspace", "http://localhost/workspace/",
                    "http://localhost/workspace/chat.html",
                    "http://localhost/workspace/js/chat.js"):
            self.assertTrue(self._here(ref), ref)

    def test_everything_else_does_not(self):
        """画廊 / 漫画台 / 跑图台 / 角色扮演 / 小说 / 外站冒充。

        「没 Referer」不在这一组了（2026-11-26 用户拍板：先按工作台认）。
        原因写在 `friend_app._jf_here` 的注释里：`location.reload()`、favicon、
        手机浏览器省 Referer，这些**页面自己**的请求全都没有 Referer，判成
        别处就会让访客在登录页与工作台之间无限来回。见下面
        `test_no_referer_counts_as_workspace`。
        """
        for ref in ("http://localhost/gallery", "http://localhost/comic",
                    "http://localhost/runbench", "http://localhost/persona",
                    "http://localhost/novel", "http://localhost/showcase"):
            self.assertFalse(self._here(ref), ref)
        # 外站路径里带 workspace 也不行（主机名要跟这次请求一致）
        self.assertFalse(self._here("https://evil.example/workspace/x"))
        self.assertFalse(self._here("http://other.host/workspace/chat.html"))

    def test_no_referer_counts_as_workspace(self):
        """完全没 Referer → 认作工作台（2026-11-26 用户拍板）。

        真实浏览器里这条对应：地址栏直接打开、书签、`location.reload()`、
        `/favicon.ico`、以及省掉 Referer 的手机浏览器。上一版把它判成「不是
        工作台」，直接造成用户报的那一串：`/me` 回 guest → 前端发登录页 →
        又跳回访客身份；`/api/gen/channels` 被 401 → 「没拿到渠道列表」。
        """
        self.assertTrue(self._here(""))
        self.assertTrue(self._here(None))

    def test_gateway_treats_visitor_as_absent_outside_workspace(self):
        """别的页面带着访客 cookie 打进来，必须跟「什么都没带」一模一样的答复。"""
        gid = self.v.new_id()
        c_guest = self.app.test_client()
        c_guest.set_cookie(self.v.COOKIE_NAME, gid, domain="localhost")
        c_plain = self.app.test_client()
        cases = [("/api/gal/list?size=1", "GET", "http://localhost/gallery"),
                 ("/api/comic/tasks", "GET", "http://localhost/comic"),
                 ("/api/gen/channels", "GET", "http://localhost/runbench"),
                 ("/api/novel/list", "GET", "http://localhost/novel"),
                 ("/api/friend/quota", "GET", "http://localhost/runbench"),
                 ("/api/chat/archive", "GET", "http://localhost/gallery")]
        for url, method, ref in cases:
            h = {"Referer": ref}
            a = c_guest.open(url, method=method, headers=h)
            b = c_plain.open(url, method=method, headers=h)
            self.assertEqual(a.status_code, b.status_code, url)
            self.assertEqual(a.get_data(), b.get_data(), url)
            # 更不能再出现那句把人挡回去的话
            self.assertNotIn("访客模式用不了", a.get_data(as_text=True), url)

    def test_me_answers_visitor_only_inside_workspace(self):
        gid = self.v.new_id()
        c = self.app.test_client()
        c.set_cookie(self.v.COOKIE_NAME, gid, domain="localhost")
        inside = c.get("/me", headers={"Referer": "http://localhost/workspace/chat.html"})
        self.assertEqual(inside.get_json().get("role"), "visitor")
        outside = c.get("/me", headers={"Referer": "http://localhost/gallery"})
        self.assertEqual(outside.get_json().get("role"), "guest")
        # 没 Referer = 浏览器省掉了（页面自己的请求），仍算工作台里的访客
        bare = c.get("/me")
        self.assertEqual(bare.get_json().get("role"), "visitor")

    def test_path_whitelist_is_gone(self):
        """白名单那两个函数别再回来——它就是「按路径扣规则」的老毛病。"""
        self.assertFalse(hasattr(self.v, "path_allowed"))
        self.assertFalse(hasattr(self.v, "comic_allowed"))

    def test_channels_work_in_workspace_only(self):
        """渠道清单：工作台必须拿得到，别处照旧 401（2026-11-26 用户报的
        「没拿到渠道列表」就是这里在工作台里被误挡）。"""
        gid = self.v.new_id()
        c = self.app.test_client()
        c.set_cookie(self.v.COOKIE_NAME, gid, domain="localhost")
        for ref in ("http://localhost/workspace/chat.html", None):
            h = {"Referer": ref} if ref else {}
            r = c.get("/api/gen/channels", headers=h)
            self.assertEqual(r.status_code, 200, ref)
            self.assertTrue(r.get_json().get("channels"), ref)
        # 别处一字不差
        plain = self.app.test_client()
        for ref in ("http://localhost/gallery", "http://localhost/runbench"):
            a = c.get("/api/gen/channels", headers={"Referer": ref})
            b = plain.get("/api/gen/channels", headers={"Referer": ref})
            self.assertEqual(a.status_code, b.status_code, ref)
            self.assertEqual(a.get_data(), b.get_data(), ref)

    def test_no_identity_only_public_reads_pass(self):
        """工作台放行只许覆盖 `_JF_PUBLIC` 里那几个读接口。

        2026-11-26 踩过的坑：按「工作台发来的就放行」写，结果没有 Referer 的
        浏览器从 `/api/comic/tasks` 读到了站主的 21 条漫画任务标题。这条把
        口子钉死：白名单之外的一律还是 401。
        """
        plain = self.app.test_client()          # 没有任何 cookie
        allowed = {("/api/gen", "channels"), ("/api/gen", "imgsign")}
        for prefix, sub in self.fa._JF_PUBLIC:
            self.assertIn((prefix, sub), allowed, "白名单被放宽了")
        for path in ("/api/comic/tasks", "/api/gal/mine", "/api/chat/archive",
                     "/api/moss/img?x=1", "/api/friend/quota"):
            self.assertEqual(plain.get(path).status_code, 401, path)
        # 写也一律挡
        self.assertEqual(plain.post("/api/gen/submit",
                                    json={"prompt": "x"}).status_code, 401)

    def test_login_page_is_reachable_with_visitor_cookie(self):
        """`/workspace/login` 不论身份都发登录页；`/workspace/` 才是回工作台。

        2026-11-26 用户实测的循环：访客带着证点「登录」→ `/workspace/` 直接
        把他发回聊天页 → 只能清状态回到登录页 → 再点「先逛逛」额度换新了。
        """
        c = self.app.test_client()
        self.assertEqual(c.post("/login/visitor").status_code, 200)
        page = c.get("/workspace/login").get_data(as_text=True)
        self.assertIn("btn-visitor", page, "访客带着证也要能进登录页")
        # `/workspace/` 仍是「有身份就回工作台」，聊天页里带着访客脚本
        chat = c.get("/workspace/").get_data(as_text=True)
        self.assertIn("visitor.js", chat)
        # 没身份的人两边都是登录页
        plain = self.app.test_client()
        self.assertIn("btn-visitor", plain.get("/workspace/login").get_data(as_text=True))
        self.assertIn("btn-visitor", plain.get("/workspace/").get_data(as_text=True))

    def test_second_visitor_login_keeps_identity(self):
        """同一个浏览器再点一次「先逛逛」→ 同一个人、同一份额度。"""
        c = self.app.test_client()
        c.post("/login/visitor")
        first = c.get("/me").get_json()
        c.post("/login/visitor")
        again = c.get("/me").get_json()
        self.assertEqual(again.get("role"), "visitor")
        self.assertEqual(again.get("session"), first.get("session"))
        self.assertEqual(again.get("quota"), first.get("quota"))


class GuestGallerySeerTest(unittest.TestCase):
    """画廊的隐藏内容判定**不再看访客头**（2026-11-26 用户：「其他的不要变」）。

    上一版这里钉的是「访客一律看不见隐藏内容」。那是把工作台的规则塞进了
    画廊，连带把站主/好友在画廊里的正常视角也改掉了。现在画廊回到原来的
    口径：站主看得见、工作台里看不见、访客头不参与判断。数据一行都不动。
    """

    def setUp(self):
        from app.features.gallery import routes as gal
        self.gal = gal
        _reload()
        self.addCleanup(_reload)

    def test_owner_sees_hidden_even_with_guest_header(self):
        """站主带着访客头进画廊（工作台以外的请求），照旧看得见隐藏内容。"""
        raw = visitor.new_id()
        good = friend_auth.secret()
        with mock.patch("flask.request",
                        _FakeReq({"X-Friend-Token": good,
                                  "X-Friend-Guest": raw})):
            with mock.patch.object(self.gal, "_jf", return_value=False):
                with mock.patch.object(self.gal, "_is_owner",
                                       return_value=True):
                    self.assertTrue(self.gal._seer("10001"))

    def test_workspace_still_hides(self):
        """工作台里连站主也是普通用户视角（这条是 2026-11-24 的老口径，不动）。"""
        with mock.patch("flask.request", _FakeReq({})):
            with mock.patch.object(self.gal, "_jf", return_value=True):
                with mock.patch.object(self.gal, "_is_owner",
                                       return_value=True):
                    self.assertFalse(self.gal._seer("10001"))


class WorkspaceVisitorJsTest(unittest.TestCase):
    """前端那两处容易改回去的地方（2026-11-26 用户第二次报同一个入口）。

    现场：访客登录 → 落在「我的」→ 闪一下进 AI 创作 → 点右上角人形没反应；
    彻底退回聊天页再进又好了。根因是 reload 之后身份还没回来时前端已经
    开始收口，把栈里恢复出来的「我的」摘掉（`WSStack.top()` 指向已摘除的
    节点），顺手也把那一屏留成了「参数不对 / 重试」。

    这里只钉「规则还在」，不跑浏览器（真机由 tools/_probe_visitor_me_click.py 验）。
    """

    def setUp(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.js = os.path.join(root, "web", "workspace", "js")

    def _src(self, name):
        import io
        with io.open(os.path.join(self.js, name), encoding="utf-8") as f:
            return f.read()

    def test_apply_waits_for_identity(self):
        """身份没回来之前一个字都不改：apply 必须同时看 loaded。"""
        src = self._src("visitor.js")
        self.assertIn("if (!S.loaded || !isVisitor()) return;", src)
        self.assertNotIn("dropMeView", src.split("function state()")[0])

    def test_me_page_is_repainted_not_removed(self):
        """「我的」只能换内容，不能整页摘掉（摘掉栈就对不上号了）。"""
        src = self._src("visitor.js")
        self.assertIn("paintMeViews", src)
        # 不再有「把页从 DOM 上摘下来」的那句
        self.assertNotIn("meView.parentNode.removeChild(meView)", src)

    def test_pop_ignores_child_transitions(self):
        """退场只认自己那层 transform 的 transitionend。"""
        src = self._src("stack.js")
        self.assertIn('e.target === view && e.propertyName === "transform"', src)

    def test_me_page_does_not_fetch_while_identity_pending(self):
        """身份没回来 / 是访客时，「我的」不能去拉作品列表。

        2026-11-26 用户实测：「进「我的」先闪一下作品/收藏，再变成访客卡」。
        闪的就是 `me.js` 在 mount 当刻发出的 `/api/gal/mine`。
        """
        src = self._src("me.js")
        self.assertIn("vis.pending()", src)
        self.assertIn("vis.isVisitor()", src)
        # 访客确实进来了就不要再发（发出去必定 400「参数不对」）
        head = src.split("function load(reset)")[1].split("S.busy = true;")[0]
        self.assertIn("return;", head)

    def test_visitor_store_key_is_per_day(self):
        """访客的本地存档按天分段，不能挂在身份串上。

        2026-11-26 用户实测两次：「大退再进，之前画的可莉没了」。带身份串的键
        每续签一次就换一把；按天分段之后，同一天怎么进出都看得见。
        """
        src = self._src("store.js")
        self.assertIn('var KEY_GUEST_DAY = "ws_chat_visitor_v2:";', src)
        self.assertIn("KEY_GUEST_DAY + day()", src)
        # 老键（带身份串那版）只允许出现在「搬家」那条路上
        self.assertNotIn("KEY_GUEST + \":\" + MODE.sid", src)

    def test_login_button_goes_to_login_page(self):
        """「登录」要去登录页自己的地址，不能去 `/workspace/`。

        2026-11-26 用户实测：「访客点登录 → 到登录页 → 再点先逛逛，次数被
        刷新」。`/workspace/` 是**回工作台**的地址，访客带着证去会被直接发
        回聊天页（原地画圈），只能靠清状态回去，而那条路顺手换了额度。
        """
        src = self._src("visitor.js")
        self.assertIn('global.location.replace("/workspace/login")', src)
        self.assertNotIn('global.location.replace("/workspace/")', src)
        me = self._src("me.js")
        self.assertIn('global.location.replace("/workspace/login")', me)


if __name__ == "__main__":
    unittest.main()
