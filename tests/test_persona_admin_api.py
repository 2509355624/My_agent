"""角色扮演查看台的接口测试（2026-11-03）。

只测「列会话 / 读详情 / 删除」这一层，引擎本身在 test_persona.py 里。
"""
import json
import os
import tempfile
import unittest
from unittest import mock

import flask

from app import agents, guest, persona, persona_admin_api


class AdminBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = os.path.join(self.tmp.name, "agents")
        self.sess = os.path.join(root, "qq", "sessions")
        os.makedirs(self.sess, exist_ok=True)

        # 卡库和状态都要隔离，否则会写到用户真实数据里
        pa = mock.patch.object(agents, "AGENTS_DIR", root)
        pa.start()
        self.addCleanup(pa.stop)
        pq = mock.patch.object(
            persona, "_STATE_PATH",
            os.path.join(self.tmp.name, "state", "persona_sessions.json"))
        pq.start()
        self.addCleanup(pq.stop)
        pg = mock.patch.object(guest, "GUEST_ROOT",
                               os.path.join(self.tmp.name, "游客-角色扮演"))
        pg.start()
        self.addCleanup(pg.stop)
        agents.clear_cache()
        self.addCleanup(agents.clear_cache)

        # 模块里 SESS_DIR 是 import 时算出来的，也得指到临时目录
        ps = mock.patch.object(persona_admin_api, "SESS_DIR", self.sess)
        ps.start()
        self.addCleanup(ps.stop)

        persona.save_cards([{"id": "izumi_sagiri", "name": "纱雾",
                             "description": "", "personality": "",
                             "systemPrompt": "你是纱雾。",
                             "appearancePrompt": "1girl"}])

        app = flask.Flask(__name__)
        app.register_blueprint(persona_admin_api.bp)
        self.c = app.test_client()

    def write_hist(self, key, msgs):
        p = os.path.join(self.sess, "persona_%s.jsonl" % key)
        with open(p, "w", encoding="utf-8") as fh:
            for m in msgs:
                fh.write(json.dumps(m, ensure_ascii=False) + "\n")
        return p

    def get(self, path):
        return self.c.get(path).get_json()


class ListTest(AdminBase):

    def test_lists_file_sessions(self):
        self.write_hist("private_123", [
            {"role": "user", "content": "在吗"},
            {"role": "assistant",
             "content": "嗯。\n画面：x\n心情：平静 强度3\n状态：y"},
        ])
        d = self.get("/api/persona_admin/list")
        self.assertTrue(d["ok"])
        self.assertEqual(d["total"], 1)
        r = d["sessions"][0]
        self.assertEqual(r["key"], "private_123")
        self.assertEqual(r["kind"], "私聊")
        self.assertEqual(r["turns"], 1)
        self.assertEqual(r["messages"], 2)

    def test_labels_are_readable(self):
        cases = {
            "private_123": "QQ 123",
            "group_9_123": "群 9 · QQ 123",
            "web_123": "网页 · 123",
            "web_local": "网页（未登录）",
        }
        for key, want in cases.items():
            self.write_hist(key, [{"role": "user", "content": "x"}])
        d = self.get("/api/persona_admin/list")
        got = {r["key"]: r["label"] for r in d["sessions"]}
        for key, want in cases.items():
            self.assertEqual(got[key], want, key)

    def test_guest_sessions_are_merged_across_days(self):
        uid = "11111111-2222-3333-4444-555555555555"
        for day, n in (("2026-11-02", 1), ("2026-11-03", 2)):
            os.makedirs(os.path.join(guest.GUEST_ROOT, day), exist_ok=True)
            p = os.path.join(guest.GUEST_ROOT, day, uid + ".jsonl")
            with open(p, "w", encoding="utf-8") as fh:
                for i in range(n):
                    fh.write(json.dumps({"role": "assistant",
                                         "content": "a"}, ensure_ascii=False) + "\n")
        d = self.get("/api/persona_admin/list")
        g = [r for r in d["sessions"] if r["source"] == "guest"]
        self.assertEqual(len(g), 1, "同一个游客跨天应该合成一条")
        self.assertEqual(g[0]["turns"], 3)


class DetailTest(AdminBase):

    def test_detail_splits_meta_lines(self):
        self.write_hist("private_123", [
            {"role": "user", "content": "在吗"},
            {"role": "assistant",
             "content": "正文。\n画面：1girl\n心情：害羞 强度7\n状态：坐着"},
        ])
        d = self.get("/api/persona_admin/detail?key=private_123&source=file")
        self.assertTrue(d["ok"])
        a = d["messages"][1]
        self.assertEqual(a["text"], "正文。")
        self.assertEqual(a["mood"], "害羞")
        self.assertEqual(a["intensity"], 7)

    def test_missing_session_is_404(self):
        r = self.c.get("/api/persona_admin/detail?key=nope&source=file")
        self.assertEqual(r.status_code, 404)

    def test_path_traversal_is_rejected(self):
        r = self.c.get("/api/persona_admin/detail?key=../x&source=file")
        self.assertEqual(r.status_code, 400)

    def test_guest_detail_works(self):
        uid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        os.makedirs(os.path.join(guest.GUEST_ROOT, "2026-11-03"), exist_ok=True)
        p = os.path.join(guest.GUEST_ROOT, "2026-11-03", uid + ".jsonl")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"role": "user", "content": "你好"},
                                ensure_ascii=False) + "\n")
        d = self.get("/api/persona_admin/detail?key=%s&source=guest" % uid)
        self.assertTrue(d["ok"])
        self.assertEqual(d["messages"][0]["text"], "你好")


class DeleteTest(AdminBase):

    def test_delete_file_session(self):
        p = self.write_hist("private_123", [{"role": "user", "content": "x"}])
        d = self.c.post("/api/persona_admin/delete",
                        json={"key": "private_123", "source": "file"}).get_json()
        self.assertTrue(d["ok"])
        self.assertFalse(os.path.isfile(p), "文件该删掉")

    def test_delete_clears_state(self):
        self.write_hist("private_123", [{"role": "user", "content": "x"}])
        st = persona._load_state()
        st["private_123"] = {"card": "izumi_sagiri"}
        persona._save_state(st)
        self.c.post("/api/persona_admin/delete",
                    json={"key": "private_123", "source": "file"})
        self.assertNotIn("private_123", persona._load_state())

    def test_delete_guest_removes_all_days(self):
        uid = "bbbbbbbb-cccc-dddd-eeee-ffffffffffff"
        for day in ("2026-11-02", "2026-11-03"):
            os.makedirs(os.path.join(guest.GUEST_ROOT, day), exist_ok=True)
            p = os.path.join(guest.GUEST_ROOT, day, uid + ".jsonl")
            with open(p, "w", encoding="utf-8") as fh:
                fh.write("{}\n")
        d = self.c.post("/api/persona_admin/delete",
                        json={"key": uid, "source": "guest"}).get_json()
        self.assertEqual(d["deleted"], 2)


if __name__ == "__main__":
    unittest.main()
