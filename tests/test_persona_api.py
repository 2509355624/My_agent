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

from app import agents, llm, persona, persona_api

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

    def test_hidden_cards_are_not_listed(self):
        """暂下架的卡不进选人列表（卡池页 /api/characters 仍能看到）。"""
        cards = persona.load_cards()
        for c in cards:
            c["hidden"] = c["id"] == "rem"
        persona.save_cards(cards)
        agents.clear_cache()
        names = [c["name"] for c in self.get("/api/persona/cards")["cards"]]
        self.assertIn("纱雾", names)
        self.assertNotIn("雷姆", names)


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


class NameTest(PersonaApiBase):
    """右栏「名字」那行（2026-11-09）：只改当前存档，不影响别的档。"""

    def _enter(self, name="纱雾"):
        self.post("/api/persona/switch", name=name)
        return name

    def test_set_and_read_back(self):
        self._enter()
        d = self.post("/api/persona/name", name="苏暖")
        self.assertTrue(d["ok"])
        self.assertEqual(d["name"], "苏暖")
        self.assertEqual(self.get("/api/persona/session")["charName"], "苏暖")

    def test_other_saves_keep_the_card_name(self):
        self._enter("纱雾")
        self.post("/api/persona/name", name="苏暖")
        self._enter("雷姆")
        self.assertEqual(self.get("/api/persona/session")["charName"], "雷姆")
        self._enter("纱雾")
        self.assertEqual(self.get("/api/persona/session")["charName"], "苏暖")

    def test_blank_name_falls_back_to_the_card(self):
        self._enter()
        self.post("/api/persona/name", name="苏暖")
        d = self.post("/api/persona/name", name="   ")
        self.assertTrue(d["ok"])
        self.assertEqual(d["name"], "纱雾")

    def test_without_a_save_is_400(self):
        r = self.c.post("/api/persona/name", json={"qq": QQ, "name": "苏暖"})
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

    def test_chain_exhaustion_returns_relay_down_not_an_empty_reply(self):
        """2026-11-09 真机：中转站 key 被停用 → PersonaChainExhausted。

        `run_turn` 原来用 `except Exception` 把它吞成空 Turn，接口只回 502
        「她这轮没说话」，前端拿不到 relay_down、引导弹窗永远不弹。链路耗尽
        必须冒到 blueprint 的 errorhandler。
        """
        self._enter()
        err = llm.PersonaChainExhausted("api key disabled", has_custom=False)
        with mock.patch.object(persona, "_llm_call", side_effect=err):
            r = self.c.post("/api/persona/chat", json={"text": "在吗", "qq": QQ})
        self.assertEqual(r.status_code, 503)
        d = r.get_json()
        self.assertEqual(d["code"], "relay_down")
        self.assertFalse(d["has_custom"])

    def test_ordinary_llm_failure_still_degrades_quietly(self):
        """别把普通超时也升级成引导弹窗：那会每次抖动都弹一次。"""
        self._enter()
        with mock.patch.object(persona, "_llm_call",
                               side_effect=RuntimeError("read timeout")):
            r = self.c.post("/api/persona/chat", json={"text": "在吗", "qq": QQ})
        self.assertEqual(r.status_code, 502)
        self.assertNotIn("code", r.get_json())


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
    """「出图」= anima-line 单张横屏 CG（2026-11-04 换线，原 moss 五格漫画）。

    shot() 把真正的出图放**后台线程**，接口只落 pending 记录就返回。用例里
    把 threading.Thread 换成**立刻执行 target**，整条流程就同步了。
    """

    @staticmethod
    def _inline_thread():
        import threading as _t
        real_thread = _t.Thread       # 先抓真身：patch 之后 Thread 是 MagicMock

        def fake(*_a, **kw):
            fn = kw.get("target")
            if fn:
                fn()
            return real_thread(target=lambda: None)

        return mock.patch("app.persona_api.threading.Thread",
                          side_effect=fake)

    def test_shot_records_file_and_status(self):
        import tempfile
        name = self.get("/api/persona/cards")["cards"][0]["name"]
        self.post("/api/persona/switch", name=name)
        with tempfile.TemporaryDirectory() as td:
            with mock.patch("app.persona.shot_scene",
                            return_value="standing, smile"), \
                 mock.patch("app.persona.draw_shot",
                            return_value=("a.png", "")), \
                 self._inline_thread(), \
                 mock.patch("app.persona_api._pic_map_path",
                            return_value=os.path.join(td, "m.json")):
                d = self.post("/api/persona/shot", msgIndex=4)
                self.assertTrue(d["ok"])
                self.assertEqual(d["msgIndex"], 4)
                # 归档行号当锚（`n4`），恢复映射和状态轮询都能查到
                pics = self.get("/api/persona/comics")["pics"]
                st = self.get("/api/persona/shot_status?n=4")
        self.assertEqual(pics.get("n4"), {"file": "a.png"})
        self.assertEqual(st["status"], "done")
        self.assertEqual(st["url"], "/api/image/a.png")

    def test_shot_file_with_backslashes_becomes_a_usable_url(self):
        """Windows 反斜杠必须转成正斜杠，否则前端 `img src` 取不到图。

        2026-11-04 用户报「网页端出图没有显示成果」：`image_jobs` 交回的是
        `web\\生图页\\anima-line/Anima_x.png`，直接拼 URL 时浏览器那一段
        一个取图请求都发不出来。落盘、轮询、恢复三条出口都要是正斜杠。
        """
        import tempfile
        name = self.get("/api/persona/cards")["cards"][0]["name"]
        self.post("/api/persona/switch", name=name)
        rel = os.path.join("web", "生图页", "anima-line") + "/a.png"
        with tempfile.TemporaryDirectory() as td:
            with mock.patch("app.persona.shot_scene",
                            return_value="standing, smile"), \
                 mock.patch("app.persona.draw_shot",
                            return_value=(rel, "")), \
                 self._inline_thread(), \
                 mock.patch("app.persona_api._pic_map_path",
                            return_value=os.path.join(td, "m.json")):
                self.post("/api/persona/shot", msgIndex=4)
                pics = self.get("/api/persona/comics")["pics"]
                st = self.get("/api/persona/shot_status?n=4")
        want = "web/生图页/anima-line/a.png"
        self.assertEqual(pics.get("n4"), {"file": want})
        self.assertEqual(st["url"], "/api/image/" + want)

    def test_shot_without_scene_is_rejected(self):
        """没有画面行 → 400 直接拒绝（用户拍板不做 LLM 兜底翻译）。"""
        name = self.get("/api/persona/cards")["cards"][0]["name"]
        self.post("/api/persona/switch", name=name)
        with mock.patch("app.persona.shot_scene", return_value=""), \
             mock.patch("app.persona.draw_shot") as ds:
            r = self.c.post("/api/persona/shot",
                            json={"qq": QQ, "msgIndex": 2})
        self.assertEqual(r.status_code, 400)
        self.assertIn("没有可画的画面", r.get_json()["error"])
        ds.assert_not_called()

    def test_shot_failure_is_recorded(self):
        """后台出图失败 → pic_map 记 error，轮询回 failed 带原因。"""
        import tempfile
        name = self.get("/api/persona/cards")["cards"][0]["name"]
        self.post("/api/persona/switch", name=name)
        with tempfile.TemporaryDirectory() as td:
            with mock.patch("app.persona.shot_scene",
                            return_value="standing, smile"), \
                 mock.patch("app.persona.draw_shot",
                            return_value=("", "排队满")), \
                 self._inline_thread(), \
                 mock.patch("app.persona_api._pic_map_path",
                            return_value=os.path.join(td, "m.json")):
                self.post("/api/persona/shot", msgIndex=4)
                st = self.get("/api/persona/shot_status?n=4")
        self.assertEqual(st["status"], "failed")
        self.assertIn("排队满", st["error"])

    def test_shot_without_card_is_rejected(self):
        d = self.post("/api/persona/shot")
        self.assertFalse(d["ok"])


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


class SessionAffTest(PersonaApiBase):
    """刷新后气泡下面的状态条（2026-11-04）。

    历史里没存每轮的 aff 快照，手机端一刷新整条状态就没了——而手机端没有
    右栏，那是唯一能看到「到哪一步」的地方。所以后端把**当前**状态挂到
    最后一条她的回复上：那条之后没再发生过任何事，它就是此刻。
    """

    def _line(self):
        name = self.get("/api/persona/cards")["cards"][0]["name"]
        self.post("/api/persona/switch", name=name)
        return persona_api._active_key(persona_api._key(QQ))

    def _seed(self, key):
        persona._save_hist(key, [
            {"role": "user", "content": "在吗"},
            {"role": "assistant", "content": "嗯，在的。"},
        ])

    def test_last_assistant_message_carries_status(self):
        key = self._line()
        from app import affection
        affection.set_mode(key, True)
        self._seed(key)
        msgs = self.get("/api/persona/session")["messages"]
        self.assertTrue(msgs[-1].get("aff"), msgs)
        self.assertIn("levelName", msgs[-1]["aff"])
        self.assertIn("levelMax", msgs[-1]["aff"])
        self.assertNotIn("aff", msgs[0])          # 只有最后一条挂，别每条都挂

    def test_free_mode_has_no_status(self):
        """自由模式下数值是封存的，别在气泡下面摆一条不动的进度。"""
        key = self._line()
        from app import affection
        affection.set_mode(key, True)
        affection.set_mode(key, False)
        self._seed(key)
        d = self.get("/api/persona/session")
        self.assertFalse([m for m in d["messages"] if m.get("aff")])
        self.assertIsNotNone(d.get("affection"))   # 档案本身还给，只是封存


class OpeningApiTest(PersonaApiBase):
    """开场导读接口（2026-11-04）。

    新线第一屏要有块灰底说明「现在是什么情况」，不然打开是空的不知道
    怎么开口。这里只测接线：有缓存就直接给、没有就去生成一次。
    """

    def _line(self):
        name = self.get("/api/persona/cards")["cards"][0]["name"]
        self.post("/api/persona/switch", name=name)
        return persona_api._active_key(persona_api._key(QQ))

    def test_returns_cached_intro(self):
        key = self._line()
        st = persona._load_state()
        st[key]["intro"] = "早上，她在二楼画画。"
        persona._save_state(st)
        d = self.get("/api/persona/opening")
        self.assertEqual(d["intro"], "早上，她在二楼画画。")

    def test_generates_once_then_cached(self):
        self._line()
        with mock.patch.object(persona.llm, "call_llm",
                               return_value="早上，她在二楼画画。") as m:
            self.assertEqual(self.get("/api/persona/opening")["intro"],
                             "早上，她在二楼画画。")
            self.get("/api/persona/opening")
            self.assertEqual(m.call_count, 1)

    def test_session_carries_intro(self):
        key = self._line()
        st = persona._load_state()
        st[key]["intro"] = "门关着。"
        persona._save_state(st)
        self.assertEqual(self.get("/api/persona/session").get("intro"), "门关着。")

    def test_no_card_returns_empty(self):
        self.assertEqual(self.get("/api/persona/opening")["intro"], "")


class HistoryArchiveApiTest(PersonaApiBase):
    """回看完整历史（2026-11-04）。

    用户原话：「我主要是要我可以回到看从开始到现在的聊天记录」。
    以前 `/session` 只喂 12 条窗口，聊过 6 轮就翻不到更早的——数据一直在
    归档里。这里测「首屏给最后一页 + 往回翻一直到第 0 条」。
    """

    def _line(self):
        self.post("/api/persona/switch", name="纱雾")
        return persona_api._active_key(persona_api._key(QQ))

    def _seed(self, key, turns):
        rows = []
        for i in range(turns):
            rows.append({"role": "user", "content": "问%d" % i})
            rows.append({"role": "assistant",
                         "content": "答%d\n画面：s%d\n心情：平静 强度1\n状态：坐着"
                                    % (i, i)})
        persona._save_hist(key, rows)
        persona._append_archive(key, [], seed=rows)

    def test_session_returns_last_page_with_ids(self):
        key = self._line()
        self._seed(key, 30)                            # 60 条
        d = self.get("/api/persona/session")
        self.assertEqual(d["total"], 60)
        self.assertTrue(d["more"])
        self.assertEqual(len(d["messages"]), persona.ARCHIVE_PAGE)
        self.assertEqual(d["messages"][-1]["n"], 59)
        self.assertEqual(d["messages"][0]["n"], 60 - persona.ARCHIVE_PAGE)

    def test_history_reaches_the_beginning(self):
        key = self._line()
        self._seed(key, 30)
        start = 60 - persona.ARCHIVE_PAGE
        before, got = start, list(range(start, 60))
        for _ in range(10):                            # 防止死循环
            d = self.get("/api/persona/history?before=%d" % before)
            ids = [m["n"] for m in d["messages"]]
            got = ids + got
            if not d["more"] or not ids:
                break
            before = ids[0]
        self.assertEqual(got, list(range(60)))         # 一条不重不漏

    def test_session_turns_is_total_not_note_beat(self):
        """轮次显示累计交流轮数（2026-11-06）：压缩清零 `turns` 不影响它。"""
        key = self._line()
        self._seed(key, 12)                       # 归档 12 轮
        st = persona._load_state()
        st[key]["turns"] = 0                      # 模拟刚压过小记
        st[key]["turn_total"] = 12
        persona._save_state(st)
        d = self.get("/api/persona/session")
        self.assertEqual(d["turns"], 12)

    def test_legacy_ai_keys_are_not_returned(self):
        """老的 `ai<窗口序号>` 已经对不上任何消息了，回给前端只会挂错位置。"""
        key = self._line()
        import os as _os
        with mock.patch("app.persona_api._pic_map_path",
                        return_value=_os.path.join(self.tmp.name, "pic.json")):
            persona_api._pic_map_set(key, "ai3", {"sid": "S", "page": 1})
            persona_api._pic_map_set(key, "n3", {"sid": "S", "page": 2})
            pics = self.get("/api/persona/comics")["pics"]
        self.assertEqual(pics, {"n3": {"sid": "S", "page": 2}})


class RollbackApiTest(PersonaApiBase):
    """POST /api/persona/rollback（2026-11-04）：web 端回退上一轮。"""

    def _setup_line(self):
        d = self.post("/api/persona/switch", id="izumi_sagiri")
        self.assertTrue(d["ok"])
        return persona.resolve_line(
            persona_api._active_key(persona_api._key(QQ)))

    def test_rollback_requires_a_turn(self):
        self._setup_line()
        d = self.post("/api/persona/rollback")
        self.assertFalse(d["ok"])
        self.assertIn("没有可回退", d["error"])

    def test_rollback_drops_last_turn_and_pic_slots(self):
        """回退要连带抹掉被删两行的图映射——归档行号是复用的，
        旧映射不清，上一张图会「鬼影」到新一轮的气泡上。"""
        key = self._setup_line()
        rows = [{"role": "user", "content": "你好"},
                {"role": "assistant", "content": "嗨"}]
        persona._save_hist(key, rows)
        persona._save_archive(key, [dict(r) for r in rows])
        persona_api._pic_map_set(key, "n0", {"file": "a.png"})
        persona_api._pic_map_set(key, "n1", {"file": "b.png"})
        persona_api._pic_map_set(key, "n9", {"file": "keep.png"})
        d = self.post("/api/persona/rollback")
        self.assertTrue(d["ok"])
        self.assertEqual(persona._load_hist(key), [])
        slots = persona_api._pic_map_load().get(key, {})
        self.assertNotIn("n0", slots)
        self.assertNotIn("n1", slots)
        self.assertIn("n9", slots)          # 更早的映射不动


    def test_rollback_decrements_total_turns(self):
        """回退时累计轮次跟着 -1（2026-11-06 用户要求）。"""
        key = self._setup_line()
        rows = [{"role": "user", "content": "你好"},
                {"role": "assistant", "content": "嗨"}]
        persona._save_hist(key, rows)
        persona._save_archive(key, [dict(r) for r in rows])
        st = persona._load_state()
        st[key]["turn_total"] = 1
        persona._save_state(st)
        d = self.post("/api/persona/rollback")
        self.assertTrue(d["ok"])
        self.assertEqual(persona.turn_total(key), 0)


class TurnOutAffectionTest(PersonaApiBase):
    """跨天/收场三段回包要带最新 affection（2026-11-06 群友反馈）。

    `finish_night` / `next_morning` 会 `affection.sleep` 收场，冲动和高潮值
    都变了；不带回去，网页右栏要手动刷新才更新。
    """

    def test_turn_out_carries_affection(self):
        self.post("/api/persona/switch", id="izumi_sagiri")
        key = persona.resolve_line(
            persona_api._active_key(persona_api._key(QQ)))
        turn = persona.Turn("早", "", "平静", 1, "床边", None)
        out = persona_api._turn_out(key, turn)
        self.assertIn("affection", out)
        self.assertIn("day", out)
        self.assertIn("turns", out)


class ManualChatApiTest(PersonaApiBase):
    """精确输入（2026-11-06）：主输入框空、只填分栏也能发。"""

    def test_fields_without_text_is_accepted(self):
        self.post("/api/persona/switch", id="izumi_sagiri")
        turn = persona.Turn("好", "", "平静", 1, "房间", None)
        with mock.patch.object(persona, "run_turn",
                               return_value=turn) as rt:
            d = self.post("/api/persona/chat",
                          fields={"action": "拿起杯子"})
        self.assertTrue(d.get("ok"))
        self.assertEqual(rt.call_args.kwargs.get("manual"),
                         {"action": "拿起杯子"})


class _InlineThread:
    """把后台线程改成同步跑。

    出图接口是「起线程 → 立刻返回」，断言必须等线程跑完才有意义。用
    `join` 也行，但接口不把 Thread 交出来，所以直接替换构造函数最省事。
    """

    def __init__(self, target=None, daemon=None, name=None, **kw):
        self._target = target

    def start(self):
        if self._target:
            self._target()


class ShotOrientTest(PersonaApiBase):
    """出图画幅（2026-11-09）：网页点「出图」可选横/竖，缺省竖屏。

    只钉**接线**——`body.orient` 有没有变成 `draw_shot(landscape=)`。
    宽高对调本身在 `image_jobs.landscape_workflow`，那边已有一整套用例；
    真出图更不能在单测里跑。
    """

    def setUp(self):
        super().setUp()
        self.post("/api/persona/switch", id="izumi_sagiri")
        # 「这一轮有画面可画」：不打真模型
        p = mock.patch.object(persona, "shot_scene",
                              return_value="1girl, sitting")
        p.start()
        self.addCleanup(p.stop)
        p2 = mock.patch.object(persona_api.threading, "Thread", _InlineThread)
        p2.start()
        self.addCleanup(p2.stop)
        # 图映射不落盘：否则写进真实 state/pic_map.json
        p3 = mock.patch.object(persona_api, "_pic_map_set")
        p3.start()
        self.addCleanup(p3.stop)
        self.seen = {}

    def _shoot(self, **body):
        def fake(card, key, msg_index=None, reply_to="", on_submit=None,
                 landscape=False):
            self.seen["landscape"] = landscape
            self.seen["msg_index"] = msg_index
            return "", "假装没画出来"

        p = mock.patch.object(persona, "draw_shot", side_effect=fake)
        p.start()
        self.addCleanup(p.stop)
        return self.post("/api/persona/shot", **body)

    def test_default_is_tall(self):
        d = self._shoot()
        self.assertTrue(d["ok"])
        self.assertFalse(self.seen["landscape"])
        self.assertEqual(d["orient"], "tall")
        self.assertIn("竖屏", d["reply"])

    def test_wide_is_forwarded(self):
        d = self._shoot(orient="wide")
        self.assertTrue(self.seen["landscape"])
        self.assertEqual(d["orient"], "wide")
        self.assertIn("横屏", d["reply"])

    def test_unknown_orient_falls_back_to_tall(self):
        """拼错 / None / 空串都当竖屏——不猜「窗口宽就是横屏」，
        手机横过来时窗口也是宽的，那不是她要的背景。"""
        self._shoot(orient="随便写")
        self.assertFalse(self.seen["landscape"])


if __name__ == "__main__":
    unittest.main()
