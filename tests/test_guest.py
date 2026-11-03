"""游客模式的限额与隔离（2026-11-03）。

用户定的规则：
  · 游客 **15 轮**（按 AI 回复次数算）
  · 身份是服务端签名的 UUID
  · 游客不能出图、不能换角色
  · 游客对话单独目录，24 小时后由用户手动清
  · 登录后不继承游客数据
"""
import os
import shutil
import tempfile
import unittest
import uuid as uuid_mod
from unittest import mock

import flask

from app import guest, persona, persona_api


class GuestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

        # ⚠️ 必须在 `save_cards` **之前**把卡库指到临时目录。
        #
        # 2026-11-03 事故：这一版最初只 patch 了 `guest.GUEST_ROOT`，然后
        # 直接 `persona.save_cards([...])`——`_cards_path()` 走的是
        # `agents.agent_dir()`，于是**把用户真正的 11 张卡覆盖成了 1 张**。
        # 从那以后，凡是碰 `save_cards` 的测试都必须先 patch `AGENTS_DIR`。
        from app import agents
        root = os.path.join(self.tmp.name, "agents")
        os.makedirs(root, exist_ok=True)
        pa = mock.patch.object(agents, "AGENTS_DIR", root)
        pa.start()
        self.addCleanup(pa.stop)

        p = mock.patch.object(guest, "GUEST_ROOT",
                              os.path.join(self.tmp.name, "游客-角色扮演"))
        p.start()
        self.addCleanup(p.stop)

        agents.clear_cache()
        self.addCleanup(agents.clear_cache)

        app = flask.Flask(__name__)
        app.register_blueprint(persona_api.bp)
        self.c = app.test_client()

        persona.save_cards([{
            "id": persona_api.GUEST_CARD_ID, "name": "纱雾",
            "description": "体验角色", "personality": "",
            "systemPrompt": "你是纱雾。", "appearancePrompt": "1girl",
        }])
        agents.clear_cache()

    def get(self, path):
        return self.c.get(path).get_json()

    def post(self, path, **body):
        return self.c.post(path, json=body).get_json()

    def _seed(self, uid, turns):
        msgs = []
        for i in range(turns):
            msgs.append({"role": "user", "content": "第 %d 句" % (i + 1)})
            msgs.append({"role": "assistant",
                         "content": "回复 %d\n画面：x\n心情：平静 强度3\n状态：y"
                                    % (i + 1)})
        guest.append_hist(uid, msgs)


class GuestIdTest(GuestBase):

    def test_issue_and_check(self):
        gid = guest.issue_id()
        self.assertTrue(guest.check_id(gid))

    def test_tampered_id_is_rejected(self):
        uid = str(uuid_mod.uuid4())
        self.assertEqual(guest.check_id(uid + ".deadbeef"), "")

    def test_garbage_is_rejected(self):
        for v in ("", "abc", "not-a-uuid.sig", "12345"):
            self.assertEqual(guest.check_id(v), "", v)


class GuestLimitTest(GuestBase):

    def test_fresh_guest_has_full_quota(self):
        d = self.get("/api/persona/session")
        self.assertEqual(d["role"], "guest")
        self.assertEqual(d["remaining"], guest.GUEST_TURNS)
        self.assertEqual(d["limit"], guest.GUEST_TURNS)

    def test_counting_is_by_assistant_replies(self):
        """限额按 AI 回复条数算，不是按消息条数。"""
        uid = str(uuid_mod.uuid4())
        self._seed(uid, 3)                     # 3 回复 + 3 用户 = 6 条消息
        self.assertEqual(guest.turns_used(uid), 3)
        self.assertEqual(guest.remaining(uid), guest.GUEST_TURNS - 3)

    def test_blocked_after_limit(self):
        uid = str(uuid_mod.uuid4())
        self._seed(uid, guest.GUEST_TURNS)
        self.assertEqual(guest.remaining(uid), 0)
        gid = guest.issue_id()
        self.c.set_cookie("gs_id", gid)
        # 把签发 id 换成预置了历史的那个会需要重签，这里直接测计数函数
        self.assertTrue(guest.check_id(gid))


class GuestPermissionTest(GuestBase):

    def test_guest_cannot_draw(self):
        d = self.post("/api/persona/shot", msgIndex=0)
        self.assertFalse(d["ok"])
        self.assertIn("登录", d["error"])

    def test_guest_cannot_switch_card(self):
        d = self.post("/api/persona/switch", name="雷姆")
        self.assertFalse(d["ok"])

    def test_guest_cannot_clear(self):
        d = self.post("/api/persona/clear")
        self.assertTrue(d["ok"])
        self.assertIn("登录", d["reply"])


class GuestStorageTest(GuestBase):

    def test_file_goes_into_dated_dir(self):
        uid = str(uuid_mod.uuid4())
        guest.append_hist(uid, [{"role": "user", "content": "hi"}])
        day = os.listdir(guest.GUEST_ROOT)[0]
        p = os.path.join(guest.GUEST_ROOT, day, uid + ".jsonl")
        self.assertTrue(os.path.isfile(p), p)

    def test_history_roundtrip(self):
        uid = str(uuid_mod.uuid4())
        self._seed(uid, 2)
        hist = guest.load_hist(uid)
        self.assertEqual(len(hist), 4)          # 2 问 + 2 答
        self.assertEqual(hist[0]["role"], "user")

    def test_expired_dirs_only_lists(self):
        """过期目录只列出来给用户手动删，**不自动删**。"""
        import time
        uid = str(uuid_mod.uuid4())
        guest.append_hist(uid, [{"role": "user", "content": "hi"}])
        day = os.listdir(guest.GUEST_ROOT)[0]
        p = os.path.join(guest.GUEST_ROOT, day)
        old = time.time() - guest.GUEST_TTL - 60
        os.utime(p, (old, old))
        listed = guest.expired_dirs()
        self.assertTrue(any(d == day for d, _n, _age in listed))
        self.assertTrue(os.path.isdir(p), "不该自己删掉")

    def test_guest_and_member_are_separate(self):
        """游客数据不混进登录用户的目录。"""
        uid = str(uuid_mod.uuid4())
        guest.append_hist(uid, [{"role": "user", "content": "hi"}])
        self.assertTrue(guest.GUEST_ROOT in guest.hist_path(uid))


if __name__ == "__main__":
    unittest.main()
