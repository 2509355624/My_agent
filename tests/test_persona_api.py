"""`app/persona_api.py` 的接线测试（2026-11-03）。

只测「请求进、JSON 出」这一层：引擎本身在 `tests/test_persona.py` 里，
这里不重复测。跑 Flask test_client，不启动服务、不调真模型。
"""
import json
import os
import tempfile
import unittest
from unittest import mock

import flask

from app import agents, persona, persona_api

QQ = "2509355624"


class PersonaApiBase(unittest.TestCase):
    """每个用例一套独立的临时状态/历史文件。

    2026-11-03：第一版直接读写真实的 `persona_sessions.json`，用例之间互相
    污染（`test_clear_keeps_card` 单独跑过、全跑时挂，因为前面的用例把状态
    换成别的卡了）。这里照 `test_persona.PersonaBase` 的做法，把
    `_STATE_PATH` 指到临时目录。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        # cleanup 是**后进先出**：先把 clear_cache 挂上，再挂 tmp.cleanup，
        # 这样收尾时先清缓存、再删目录。反过来的话，下一个用例拿到的缓存
        # 指向已删除的临时目录，`load_cards` 返回空、`find_card` 全找不到。
        self.addCleanup(self.tmp.cleanup)
        agents.clear_cache()
        self.addCleanup(agents.clear_cache)

        root = os.path.join(self.tmp.name, "agents")
        os.makedirs(root, exist_ok=True)
        p = mock.patch.object(agents, "AGENTS_DIR", root)
        p.start()
        self.addCleanup(p.stop)
        q = mock.patch.object(
            persona, "_STATE_PATH",
            os.path.join(self.tmp.name, "state", "persona_sessions.json"))
        q.start()
        self.addCleanup(q.stop)

        # 临时目录是空的，先放两张卡进去（不依赖仓库里那份 characters.json，
        # 那份是 gitignore 的，换台机器跑测试就没了）
        #
        # ⚠️ 顺序要紧：`save_cards` 之前**先清一次缓存**，否则上一个用例的
        # 卡列表还挂在模块级缓存里，`find_card` 会去旧列表里找刚写的卡，
        # 找不到（2026-11-03 踩过：test_clear_keeps_card 全跑时挂、单跑时过）。
        agents.clear_cache()
        persona.save_cards([
            {"id": "izumi_sagiri", "name": "纱雾",
             "description": "害羞的插画师", "personality": "内向",
             "systemPrompt": "你是纱雾。",
             "appearancePrompt": "1girl, silver hair"},
            {"id": "rem", "name": "雷姆",
             "description": "鬼族女仆", "personality": "温柔",
             "systemPrompt": "你是雷姆。",
             "appearancePrompt": "1girl, blue hair"},
        ])
        # ⚠️ 顺序要紧：先清缓存再写卡，写完**再清一次**——`save_cards` 落盘后
        # `load_cards` 可能还握着旧的空缓存，`find_card` 就找不到刚写的卡
        # （2026-11-03 踩过：test_clear_keeps_card 全跑时挂、单跑时过）。
        agents.clear_cache()
        self.addCleanup(agents.clear_cache)

        app = flask.Flask(__name__)
        app.register_blueprint(persona_api.bp)
        self.c = app.test_client()

    def get(self, path):
        # 必须带上 qq，否则 `_qq()` 取不到人、`_key()` 退化成 `web_local`，
        # 和 `post()` 用的会话不是同一条（2026-11-03 踩过：单看代码都对，
        # 就是读不到刚切好的角色）。
        sep = "&" if "?" in path else "?"
        return self.c.get(path + sep + "qq=" + QQ).get_json()

    def post(self, path, **body):
        body.setdefault("qq", QQ)
        return self.c.post(path, json=body).get_json()


class CardListTest(PersonaApiBase):

    def test_lists_cards(self):
        d = self.get("/api/persona/cards")
        self.assertTrue(d["ok"])
        self.assertTrue(d["cards"])
        self.assertTrue(all("name" in c for c in d["cards"]))

    def test_card_fields_are_safe(self):
        """只回前端要用的字段，不把整张卡（含 systemPrompt）甩出去。"""
        d = self.get("/api/persona/cards")
        c = d["cards"][0]
        self.assertNotIn("systemPrompt", c)
        self.assertNotIn("appearancePrompt", c)


class SessionKeyTest(unittest.TestCase):

    def test_web_key_is_separate_from_qq(self):
        self.assertNotEqual(persona_api._key(QQ), "private_" + QQ)
        self.assertTrue(persona_api._key(QQ).startswith("web_"))

    def test_no_qq_falls_back_to_local(self):
        self.assertEqual(persona_api._key(""), "web_local")


class SwitchTest(PersonaApiBase):

    def test_switch_by_name(self):
        name = self.get("/api/persona/cards")["cards"][0]["name"]
        d = self.post("/api/persona/switch", name=name)
        self.assertTrue(d["ok"])
        self.assertEqual(d["card"]["name"], name)

    def test_switch_by_id(self):
        card = self.get("/api/persona/cards")["cards"][0]
        d = self.post("/api/persona/switch", id=card["id"])
        self.assertTrue(d["ok"])

    def test_unknown_card_is_404(self):
        r = self.c.post("/api/persona/switch",
                        json={"name": "不存在的人", "qq": QQ})
        self.assertEqual(r.status_code, 404)

    def test_empty_name_is_400(self):
        r = self.c.post("/api/persona/switch", json={"qq": QQ})
        self.assertEqual(r.status_code, 400)


class ChatTest(PersonaApiBase):

    def _enter(self):
        name = self.get("/api/persona/cards")["cards"][0]["name"]
        self.post("/api/persona/switch", name=name)
        return name

    def test_chat_without_card_is_rejected(self):
        d = self.post("/api/persona/chat", text="在吗")
        self.assertFalse(d["ok"])

    def test_chat_returns_reply_and_mood(self):
        self._enter()
        turn = persona.Turn("她抬眼看了你一下。", "1girl", "害羞", 6, "坐在桌前")
        with mock.patch.object(persona, "run_turn", return_value=turn):
            d = self.post("/api/persona/chat", text="在吗")
        self.assertTrue(d["ok"])
        self.assertEqual(d["reply"], "她抬眼看了你一下。")
        self.assertEqual(d["mood"], "害羞")
        self.assertEqual(d["intensity"], 6)

    def test_empty_text_is_400(self):
        self._enter()
        r = self.c.post("/api/persona/chat", json={"text": "  ", "qq": QQ})
        self.assertEqual(r.status_code, 400)


class ComicStatusTest(PersonaApiBase):
    """查某一页出完没有（2026-11-03）。

    用户报「网页端出图有问题」——查下来是页号错配：每个系列页有自己的任务
    目录，每次渲染只出一页，所以那个目录的 `state.json` 里页号**永远是 "1"**。
    拿系列第 2 页的 2 去查，永远查不到 → 前端一直 pending。
    """

    def _make_task(self, name, status="done", file="web/x/a.png"):
        d = os.path.join(self.tmp.name, "moss_comic", name)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "state.json"), "w", encoding="utf-8") as fh:
            json.dump({"pages": {"1": {"status": status, "file": file}}}, fh)
        return d

    def test_series_page_2_resolves(self):
        """系列第 2 页要能查到——这正是修之前坏掉的地方。"""
        d = self._make_task("20261103_1530_test")
        series = {"id": "SID", "pages": [
            {"n": 1, "task": d},
            {"n": 2, "task": d},
        ]}
        with mock.patch("app.moss_series.load", return_value=series), \
             mock.patch("app.moss_series.by_n",
                        side_effect=lambda s, n, owner="": (
                            [p for p in series["pages"] if p["n"] == n] or [None])[0]):
            d2 = self.get("/api/persona/comic?sid=SID&page=2")
        self.assertEqual(d2["status"], "done")
        self.assertIn("test", d2["url"])

    def test_url_uses_task_page_one(self):
        """取图地址里的 `n` 必须是任务目录内的 1，不是系列页号。"""
        d = self._make_task("20261103_1600_t")
        series = {"id": "S", "pages": [{"n": 3, "task": d}]}
        with mock.patch("app.moss_series.load", return_value=series), \
             mock.patch("app.moss_series.by_n",
                        side_effect=lambda s, n, owner="": {"n": 3, "task": d}):
            d2 = self.get("/api/persona/comic?sid=S&page=3")
        self.assertTrue(d2["url"].endswith("n=1"), d2["url"])

    def test_missing_state_reports_pending(self):
        series = {"id": "S", "pages": [{"n": 1, "task": ""}]}
        with mock.patch("app.moss_series.load", return_value=series), \
             mock.patch("app.moss_series.by_n",
                        side_effect=lambda s, n, owner="": {"n": 1, "task": ""}):
            d = self.get("/api/persona/comic?sid=S&page=1")
        self.assertEqual(d["status"], "pending")


class PerCardSessionTest(PersonaApiBase):
    """切角色 = 换一条独立会话线（2026-11-03 用户要求）。

    原话：「选择其他的角色就是另一个会话了，就是相同 QQ 账号的其他角色的会话」。

    所以每条线的身份是 `(会话线, 角色)`：
        web_2509355624__izumi_sagiri
        web_2509355624__rem
    """

    def test_card_key_format(self):
        k = persona.card_key("web_123", "rem")
        self.assertEqual(k, "web_123__rem")
        # 幂等：带过了不再叠
        self.assertEqual(persona.card_key(k, "rem"), k)

    def test_card_key_without_card_is_unchanged(self):
        self.assertEqual(persona.card_key("web_123", ""), "web_123")

    def test_switch_creates_separate_lines(self):
        self.post("/api/persona/switch", name="纱雾")
        self.post("/api/persona/switch", name="雷姆")
        st = persona._load_state()
        self.assertIn("web_2509355624__izumi_sagiri", st)
        self.assertIn("web_2509355624__rem", st)

    def test_active_key_follows_current_card(self):
        self.post("/api/persona/switch", name="雷姆")
        key = persona_api._active_key(persona_api._key(QQ))
        self.assertTrue(key.endswith("__rem"), key)

    def test_histories_are_separate(self):
        """两条线各写各的历史，互不混。"""
        turn = persona.Turn("回复", "1girl", "平静", 3, "坐着")
        self.post("/api/persona/switch", name="纱雾")
        with mock.patch.object(persona, "run_turn", return_value=turn):
            self.post("/api/persona/chat", text="给纱雾的话")
        self.post("/api/persona/switch", name="雷姆")
        with mock.patch.object(persona, "run_turn", return_value=turn):
            self.post("/api/persona/chat", text="给雷姆的话")

        # `run_turn` 被 mock 了，不会自己写真历史——按线上行为补写一条，
        # 验的是「键分开之后，两个角色落在不同文件」。
        k1 = persona.card_key(persona_api._key(QQ), "izumi_sagiri")
        k2 = persona.card_key(persona_api._key(QQ), "rem")
        persona._save_hist(k1, [{"role": "user", "content": "给纱雾的话"}])
        persona._save_hist(k2, [{"role": "user", "content": "给雷姆的话"}])

        self.assertNotEqual(k1, k2)
        self.assertIn("纱雾", persona._load_hist(k1)[0]["content"])
        self.assertIn("雷姆", persona._load_hist(k2)[0]["content"])


class StuckComicTest(PersonaApiBase):
    """僵尸 running 要判失败，不能让前端无限轮询（2026-11-03）。

    进程重启后 `state.json` 会永远停在 running，出图线程死了没人回写。
    实测有过一页从 13:16 卡到 16:30。
    """

    def test_old_running_becomes_failed(self):
        import time
        d = os.path.join(self.tmp.name, "moss_comic", "stuck")
        os.makedirs(d, exist_ok=True)
        old = time.strftime("%Y-%m-%d %H:%M:%S",
                            time.localtime(time.time() - 3600))
        with open(os.path.join(d, "state.json"), "w", encoding="utf-8") as fh:
            json.dump({"pages": {"1": {"status": "running", "error": ""}},
                       "updated": old}, fh)
        series = {"id": "S", "pages": [{"n": 1, "task": d}]}
        with mock.patch("app.moss_series.load", return_value=series), \
             mock.patch("app.moss_series.by_n",
                        side_effect=lambda s, n, owner="": {"n": 1, "task": d}):
            r = self.get("/api/persona/comic?sid=S&page=1")
        self.assertEqual(r["status"], "failed")
        self.assertIn("断", r.get("error") or "")


class ShotTest(PersonaApiBase):

    @staticmethod
    def _latest_advancing(first, then):
        """latest 的替身：第一次返回旧页号，之后返回新页号。

        shot() 要等「页号 > 起跑前」——旧版等「系列有页」会把老系列的
        旧页号立刻返回给前端（17:35 实测显示旧图），2026-11-03 改的。
        """
        calls = {"n": 0}

        def fake(series, owner=""):
            calls["n"] += 1
            return {"n": first if calls["n"] == 1 else then}

        return fake

    def test_shot_waits_for_new_page(self):
        name = self.get("/api/persona/cards")["cards"][0]["name"]
        self.post("/api/persona/switch", name=name)
        with mock.patch("app.moss_bot.start", return_value=(True, "")) as ms, \
             mock.patch("app.moss_series.current", return_value="SID"), \
             mock.patch("app.moss_series.load", return_value={"pages": []}), \
             mock.patch("app.moss_series.latest",
                        side_effect=self._latest_advancing(2, 3)), \
             mock.patch("app.persona_api.time.sleep"):
            d = self.post("/api/persona/shot")
        self.assertTrue(d["ok"])
        self.assertEqual(d["sid"], "SID")
        # 返回的是**新**页号，不是起跑前的旧页（旧版这里会返回 2）
        self.assertEqual(d["page"], 3)
        self.assertTrue(ms.called)
        brief = ms.call_args[0][3]
        self.assertIn("女主角", brief)
        self.assertIn("男主角", brief)

    def test_shot_timeout_returns_504(self):
        """页号一直不涨（编剧慢 / 上一张还在画）→ 504，不返回旧页号。"""
        name = self.get("/api/persona/cards")["cards"][0]["name"]
        self.post("/api/persona/switch", name=name)
        with mock.patch("app.moss_bot.start", return_value=(True, "")), \
             mock.patch("app.moss_series.current", return_value="SID"), \
             mock.patch("app.moss_series.load", return_value={"pages": []}), \
             mock.patch("app.moss_series.latest",
                        side_effect=lambda s, owner="": {"n": 2}), \
             mock.patch("app.persona_api.time.sleep"):
            r = self.c.post("/api/persona/shot", json={"qq": QQ})
        self.assertEqual(r.status_code, 504)
        self.assertFalse(r.get_json()["ok"])

    def test_shot_records_pic_map(self):
        """出图后「回复↔页」映射要落盘，/api/persona/comics 能查回来。"""
        import tempfile
        name = self.get("/api/persona/cards")["cards"][0]["name"]
        self.post("/api/persona/switch", name=name)
        with tempfile.TemporaryDirectory() as td:
            with mock.patch("app.moss_bot.start", return_value=(True, "")), \
                 mock.patch("app.moss_series.current", return_value="SID"), \
                 mock.patch("app.moss_series.load", return_value={"pages": []}), \
                 mock.patch("app.moss_series.latest",
                            side_effect=self._latest_advancing(2, 3)), \
                 mock.patch("app.persona_api.time.sleep"), \
                 mock.patch("app.persona_api._pic_map_path",
                            return_value=os.path.join(td, "m.json")):
                d = self.post("/api/persona/shot", msgIndex=4)
                self.assertTrue(d["ok"])
                # GET 也必须在补丁里：读的是同一份临时映射文件
                pics = self.get("/api/persona/comics")["pics"]
        self.assertEqual(pics.get("ai4"), {"sid": "SID", "page": 3})

    def test_shot_without_card_is_rejected(self):
        d = self.post("/api/persona/shot")
        self.assertFalse(d["ok"])

    def test_busy_returns_429(self):
        name = self.get("/api/persona/cards")["cards"][0]["name"]
        self.post("/api/persona/switch", name=name)
        with mock.patch("app.moss_bot.start", return_value=(False, "在画了")):
            r = self.c.post("/api/persona/shot", json={"qq": QQ})
        self.assertEqual(r.status_code, 429)


class LeaveClearTest(PersonaApiBase):

    def test_leave_when_idle(self):
        d = self.post("/api/persona/leave")
        self.assertTrue(d["ok"])

    def test_leave_after_switch(self):
        name = self.get("/api/persona/cards")["cards"][0]["name"]
        self.post("/api/persona/switch", name=name)
        d = self.post("/api/persona/leave")
        self.assertIn("出戏", d["reply"])

    def test_clear_keeps_card(self):
        name = self.get("/api/persona/cards")["cards"][0]["name"]
        r = self.post("/api/persona/switch", name=name)
        self.assertTrue(r["ok"], r)
        d = self.post("/api/persona/clear")
        self.assertTrue(d["reply"])
        s = self.get("/api/persona/session")
        # 清空后角色必须还在——测试里显式断言，别只看 reply 文案
        self.assertEqual((s.get("card") or {}).get("name"), name,
                         "清空后角色丢了：%r" % s.get("card"))


if __name__ == "__main__":
    unittest.main()
