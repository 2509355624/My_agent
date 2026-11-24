"""2026-11-23 第二轮：画同款 / 格数 / 刷新恢复 / 登录入口 / 不叠层。

用户这一轮的原话是「没有图生图啊，画廊的图片点击做同款的意思是直接反推
提示词生成」——所以这里的每条断言都在守一个具体行为，不是守文案。
"""
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WS = os.path.join(ROOT, "web", "workspace")


def _read(rel):
    with open(os.path.join(WS, rel), encoding="utf-8") as f:
        return f.read()


class TestSameAsIsPromptReverse(unittest.TestCase):
    """画同款 = 反推提示词，不是图生图。"""

    def test_gallery_backs_to_chat_and_hands_over_the_image(self):
        js = _read("js/gallery.js")
        i = js.index("function sameAs")
        seg = js[i:js.index("function current()", i)]
        self.assertIn("closeViewer()", seg, "点完要把查看器关掉")
        self.assertIn("WSStack.back()", seg, "要退回聊天页")
        self.assertIn("useSame(work, it.id)", seg, "要把这一张交给聊天页当参考")
        self.assertIn("var it = current()", seg, "取的是当前这张图，不是整本")

    def test_use_same_records_the_id_not_the_title(self):
        """作品名是「1」「2」这种编号，拿它当提示词等于什么都没传。"""
        js = _read("js/ref.js")
        i = js.index("function useSame")
        seg = js[i:i + 900]
        self.assertIn("addRef(", seg)
        self.assertIn("itemId", seg)
        self.assertNotIn("work.title", seg)

    def test_refs_reach_the_submit_call(self):
        """参考图要真带上，不能只挂个缩略图好看。"""
        # 提交请求体 2026-11-24 拆到了 gen_flow.js；取列表还在 chat.js
        js = _read("js/gen_flow.js")
        self.assertIn("body.refs = refs", js, "发送时要把参考图 id 一起交过去")
        chat = _read("js/chat.js")
        self.assertIn("WSRef.list()", chat)


class TestComicPanelsComeFromAdmin(unittest.TestCase):
    """格数沿用 5174 管理页的配置，前端不许写死。"""

    def test_frontend_does_not_hardcode_panels(self):
        js = _read("js/create.js")
        self.assertNotIn("panels:", js, "前端不该再传死格数")
        self.assertNotIn("var PANELS", js, "去掉写死的 PANELS 常量")

    def test_server_uses_account_limit(self):
        """不传 panels 时由后端按账号上限定——静态核对服务端确实这么收口。"""
        with open(os.path.join(ROOT, "app", "features", "comic",
                               "routes.py"), encoding="utf-8") as f:
            py = f.read()
        self.assertIn('comic_quota.limits(qq)["max_panels"]', py)

    def test_panels_limit_is_admin_editable(self):
        with open(os.path.join(ROOT, "state", "comic_friend.json"),
                  encoding="utf-8") as f:
            self.assertIn("max_panels", f.read())


class TestChatPersists(unittest.TestCase):

    def test_store_module_exists_and_is_loaded_first(self):
        html = _read("chat.html")
        self.assertIn("js/store.js", html)
        self.assertLess(html.index("js/store.js"), html.index("js/chat.js"))
        js = _read("js/store.js")
        self.assertIn("localStorage", js)
        self.assertIn("MAX", js, "要有条数上限，不能无限长")

    def test_messages_are_logged_and_replayed(self):
        chat = _read("js/chat.js")
        self.assertIn("function log(", chat)
        self.assertIn("WSStore.save", chat)
        arch = _read("js/archive.js")
        self.assertIn("WSStore.load", arch)
        for k in ('"user"', '"note"', '"img"', '"comic"', '"ref"'):
            self.assertIn(k, arch, "重放要覆盖这一类消息: " + k)

    def test_replay_does_not_write_again(self):
        """重放不能再写存档，否则每刷新一次记录翻倍。"""
        arch = _read("js/archive.js")
        self.assertNotIn("WSStore.save", arch)

    def test_unfinished_jobs_are_rejoined(self):
        # 轮询表 2026-11-24 拆到了 gen_flow.js（`S().pending[...]` 取代 `S.pending[...]`）
        flow = _read("js/gen_flow.js")
        self.assertIn("S().pending[card.job] = card", flow)
        arch = _read("js/archive.js")
        self.assertIn("WSChat.pending[a.rec.job]", arch, "生图任务要接回轮询")
        self.assertIn("WSCreate.resume", arch, "漫画任务要接回轮询")
        create = _read("js/create.js")
        self.assertIn("function resume", create)

    def test_job_missing_is_reported_not_spinning(self):
        """进程重启后任务号没了：要说清楚，不能一直转圈。"""
        # 「任务号查不到」这个判定跟着轮询一起搬到了 gen_flow.js
        flow = _read("js/gen_flow.js")
        self.assertIn("服务重启过", flow)


class TestLoginEntry(unittest.TestCase):

    def test_me_page_has_account_bar(self):
        js = _read("js/me.js")
        self.assertIn("mountAccount", js)
        self.assertIn('"/me"', js)
        self.assertIn('"/logout"', js)
        self.assertIn("退出登录", js)

    def test_logout_clears_local_archive(self):
        js = _read("js/me.js")
        i = js.index("logout")
        seg = js[i:i + 600]
        self.assertIn("WSStore.clear()", seg, "退出要清掉这台设备的聊天记录")
        self.assertIn("location.reload()", seg)


class TestNoSelfStacking(unittest.TestCase):

    def test_push_blocks_same_view_on_top(self):
        js = _read("js/stack.js")
        i = js.index("function push(")
        seg = js[i:i + 700]
        self.assertIn('S.top.dataset.view === name', seg)
        self.assertIn("return S.top", seg)

    def test_gallery_click_reuses_top(self):
        """连点「AI 创作」只剩一层，不是「点几次叠几层」。"""
        js = _read("js/stack.js")
        self.assertNotIn('if (view.dataset.view !== "me") push("me");', js)


class TestSkillPickedSilently(unittest.TestCase):

    def test_no_toast_when_switching_skill(self):
        """换画风不许弹「已更换 XX 渠道」，名字显示在按钮上。"""
        js = _read("js/chat.js")
        i = js.index("function pickSkill")
        seg = js[i:js.index("function syncSkillName", i)]
        self.assertNotIn("sysNote", seg, "选画风不该弹系统提示")
        self.assertIn("syncSkillName", seg)

    def test_page_has_skill_name_slot(self):
        html = _read("chat.html")
        self.assertIn('id="skill-name"', html)
        self.assertIn('id="ref-bar"', html)


class TestRefUpload(unittest.TestCase):

    def test_album_upload_goes_through_gallery_api(self):
        js = _read("js/ref.js")
        self.assertIn("/api/gal/upload/one", js)
        self.assertNotIn("P1", js, "「等 P1 接上」那句占位提示要删掉")

    def test_upload_shrinks_before_sending(self):
        js = _read("js/ref.js")
        self.assertIn("1600", js)
        self.assertIn("toBlob", js)


if __name__ == "__main__":
    unittest.main()
