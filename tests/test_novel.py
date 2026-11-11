# -*- coding: utf-8 -*-
"""小说模式（第三套）的守卫测试（2026-11-11）。

这版最容易被悄悄改坏的不是语法，是**设计边界**：

- 裁定不是拒绝 —— 代码绝不能替玩家审「合不合理」，只能登记编剧交回来的事实；
- 重开不是整本清空、也不是金手指 —— 静态事实继承，动态事件不继承；
- 小说模式不是 1v1 —— 不许冒出好感 / 跨天 / 服饰 / 数值条；
- 三个 AI 各跑各的 —— 开场一次封样，每轮先裁定再写正文；
- 模型「没交作业」和「链路不通」是两种错误，提示语不能混。

全部 mock `novel._call`，不调真模型、不起服务；`novel._FILE` 每个用例
指到独立临时目录，不碰 `state/novel_worlds.json`。
"""
import collections
import json
import os
import re
import tempfile
import unittest
from unittest import mock

import flask

from app import novel, novel_api

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGE = os.path.join(ROOT, "web", "novel.html")
MAIN = os.path.join(ROOT, "app", "main.py")
INDEX = os.path.join(ROOT, "web", "index.html")
NOVEL_SRC = os.path.join(ROOT, "app", "novel.py")

OPENING_TEXT = "林岸睁开眼，宿舍里只剩灰白的天光。"

SEED = {
    "places": [
        {"name": "男生宿舍 304", "detail": "四张床，柜门半开",
         "search": "晾衣杆、胶带、课本", "kind": "static"},
        {"name": "三楼走廊", "detail": "声控灯坏了",
         "search": "消防栓", "kind": "static"},
    ],
    "item_pool": ["晾衣杆", "胶带", "课本"],
    "cast_seed": [
        {"name": "周砚", "identity": "隔壁宿舍的男生", "relation": "陌生人",
         "willing": "观望", "tier": "主体", "present": False},
    ],
    "chapter_goal": "摸清三楼走廊能往哪走",
    "opening_facts": ["林岸在 304 醒来", "窗外传来拖行的声音", "身上只有睡衣"],
    "actions": ["检查柜子", "掀开床板", "贴着门听走廊"],
    "discovered": [
        {"fact": "三楼走廊尽头有安全通道", "kind": "static"},
    ],
}


def _seed_text():
    return json.dumps(SEED, ensure_ascii=False)


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


class NovelBase(unittest.TestCase):
    """每个用例一套独立的世界书文件。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch.object(
            novel, "_FILE", os.path.join(self.tmp.name, "novel_worlds.json"))
        p.start()
        self.addCleanup(p.stop)

    def open_a_world(self, name="林岸", identity="文弱书生"):
        """开场封样：两次 LLM（开场 JSON + 正文），返回 (结果, mock)。"""
        with mock.patch.object(
                novel, "_call", side_effect=[_seed_text(), OPENING_TEXT]) as m:
            out = novel.open_world("zombie", name, identity)
        return out, m


# ─── 预设包 ──────────────────────────────────────────

class PresetTest(unittest.TestCase):
    def test_list_plays_has_zombie_only(self):
        plays = novel.list_plays()
        ids = [p["id"] for p in plays]
        self.assertIn("zombie", ids)
        self.assertEqual(len(ids), len(set(ids)))
        z = next(p for p in plays if p["id"] == "zombie")
        self.assertEqual(z["name"], "丧尸末日")
        self.assertEqual(len(z["identity_choices"]), 3)
        self.assertTrue(z["tagline"])
        self.assertTrue(z["opening_note"])

    def test_preset_start_is_bare_handed_in_dorm(self):
        p = novel.load_preset("zombie")
        self.assertIsInstance(p, dict)
        self.assertEqual(p["start"]["items"], [])
        self.assertIn("宿舍", p["start"]["place"])
        self.assertEqual(len(p["opening_actions"]), 3)
        self.assertTrue(any("会死" in r for r in p["rules"]))
        self.assertEqual(
            {c["name"] for c in p["identity_choices"]},
            {"体育生", "文弱书生", "医学生"})

    def test_bad_play_id_cannot_escape_novels_dir(self):
        for bad in ("", "../config", "zombie/../config", "a/b", "..", "z ombie"):
            self.assertIsNone(novel.load_preset(bad), bad)
            self.assertEqual(novel._opening_doc(bad), "")
        self.assertIsNone(novel.load_preset("does_not_exist"))

    def test_opening_doc_states_the_rules(self):
        doc = novel._opening_doc("zombie")
        self.assertIn("枪不在宿舍里", doc)
        self.assertIn("静态事实", doc)
        self.assertIn("身份", doc)
        self.assertIn("主角手上现在什么都没有", doc)


# ─── JSON / 错误分层 ──────────────────────────────────

class ParseJsonTest(unittest.TestCase):
    def test_empty_is_no_homework(self):
        for bad in ("", None, "   "):
            with self.assertRaises(novel.NoHomework):
                novel._parse_json(bad)

    def test_fenced_json_and_surrounding_text(self):
        raw = '```json\n{"ruling":"摸到晾衣杆"}\n```'
        self.assertEqual(novel._parse_json(raw)["ruling"], "摸到晾衣杆")
        self.assertEqual(
            novel._parse_json('先说一句 {"facts":["a"]} 后面没了')["facts"], ["a"])

    def test_broken_json_and_non_object_are_no_homework(self):
        for bad in ("没有对象", "{不是 json}", "[1, 2, 3]"):
            with self.assertRaises(novel.NoHomework):
                novel._parse_json(bad)

    def test_no_homework_is_not_value_error(self):
        """参数错误 400、没交作业 502 —— 两者绝不能是同一个异常类。"""
        self.assertFalse(issubclass(novel.NoHomework, ValueError))


# ─── 世界书 / 场景切片 ────────────────────────────────

class WorldBookTest(NovelBase):
    def test_state_card_is_facts_only(self):
        out, _ = self.open_a_world()
        s = out["state"]
        self.assertEqual(s["player"]["name"], "林岸")
        self.assertEqual(s["player"]["identity"], "文弱书生")
        self.assertTrue(s["player"]["identity_desc"])
        self.assertEqual(s["player"]["items"], [])
        self.assertEqual(s["chapter"], 1)
        self.assertIsNone(s["ending"])
        self.assertEqual(
            {"id", "play", "play_name", "chapter", "chapter_goal", "ending",
             "player", "present", "cast", "route", "events", "log",
             "discovered", "last_death", "actions", "history"},
            set(s.keys()))
        for banned in ("affection", "score", "tier", "gameday", "outfit",
                       "mood", "intensity", "day", "hp", "depth"):
            self.assertNotIn(banned, s, "状态卡混进了 1v1 的数值字段：%s" % banned)

    def test_opening_seals_seed_and_writes_history(self):
        out, m = self.open_a_world()
        self.assertEqual(m.call_count, 2)
        self.assertEqual(out["reply"], OPENING_TEXT)
        w = novel.get_world(out["world_id"])
        self.assertEqual(w["seed"]["chapter_goal"], SEED["chapter_goal"])
        self.assertEqual(w["chapter_goal"], SEED["chapter_goal"])
        self.assertEqual(w["places"], SEED["places"])
        self.assertEqual(w["item_pool"], SEED["item_pool"])
        self.assertEqual(w["actions"], SEED["actions"])
        self.assertEqual(w["opening_reply"], OPENING_TEXT)
        self.assertEqual(w["player"]["items"], [], "开场不许凭空发道具")
        self.assertEqual([r["role"] for r in w["history"]], ["assistant"])
        # 开场写作 AI 拿到的事实就是开场 AI 交的那几条
        self.assertIn("林岸在 304 醒来", m.call_args_list[1].args[0][0]["content"])

    def test_unknown_play_is_value_error(self):
        with self.assertRaises(ValueError):
            novel.open_world("nope", "林岸", "体育生")

    def test_slice_only_injects_absent_cast_filter_and_static_facts(self):
        out, _ = self.open_a_world()
        w = novel.get_world(out["world_id"])
        w["cast"] = [
            {"name": "周砚", "identity": "同学", "relation": "队友",
             "willing": "愿意", "present": True},
            {"name": "李二", "identity": "路人", "relation": "陌生人",
             "willing": "观望", "present": False},
        ]
        w["discovered"] = [
            {"fact": "安保室门后挂着一把枪", "kind": "static"},
            {"fact": "警察局那边刚起过尸潮", "kind": "dynamic"},
        ]
        s = novel._slice(w)
        self.assertIn("林岸", s)
        self.assertIn("文弱书生", s)
        self.assertIn("男生宿舍 304", s)
        self.assertIn("空手", s)
        self.assertIn("安保室门后挂着一把枪", s)
        self.assertNotIn("警察局那边刚起过尸潮", s, "动态事实不该进每轮切片")
        self.assertIn("周砚", s)
        self.assertIn("李二", s.split("主角团：")[1].split("\n")[0])
        self.assertNotIn("李二", s.split("在场的人：")[1].split("\n")[0])


# ─── 裁定登记：代码只合并，不判断 ──────────────────────

class ApplyTest(NovelBase):
    def _world(self):
        out, _ = self.open_a_world()
        return novel.get_world(out["world_id"])

    def test_items_gain_use_lose_and_dedup(self):
        w = self._world()
        novel._apply(w, {
            "ruling": "柜子里只有晾衣杆和胶带",
            "facts": ["林岸拉开柜门"],
            "items_gain": ["晾衣杆", "胶带", "胶带"],
            "items_use": ["胶带"],
            "items_lose": [],
        }, "检查柜子")
        self.assertEqual(w["player"]["items"], ["晾衣杆"])
        novel._apply(w, {"items_gain": ["课本"], "items_use": ["晾衣杆"]},
                     "拿晾衣杆去够门")
        self.assertEqual(w["player"]["items"], ["课本"])

    def test_cast_events_route_chapter_and_log(self):
        w = self._world()
        novel._apply(w, {
            "ruling": "走廊里撞见周砚",
            "facts": ["林岸推开防火门", "周砚举着消防斧"],
            "cast_new": [
                {"name": "周砚", "identity": "隔壁宿舍", "relation": "陌生人",
                 "willing": "观望", "tier": "主体", "present": True},
            ],
            "events_add": [
                {"trigger": "三楼有人拍门", "result": "还没查", "kind": "dynamic"},
                {"trigger": "广播在重复一句话", "result": "还没查", "kind": "dynamic"},
            ],
            "events_done": ["三楼有人拍门"],
            "discovered": [
                {"fact": "安保室门后挂着一把枪", "kind": "static"},
                {"fact": "安保室门后挂着一把枪", "kind": "static"},
                {"fact": "警察局那边刚起过尸潮"},       # 漏标 kind → 动态
                "今晚走廊特别安静",
            ],
            "route": {"goal": "警察局", "nodes": ["宿舍楼后门", "操场", "校门"],
                      "at": 1},
            "chapter_goal": "先摸到校门",
            "chapter_advance": True,
            "actions": ["贴着墙走", "先回宿舍", "喊一声"],
        }, "我要去警察局拿枪")
        self.assertEqual(len(w["cast"]), 1)
        self.assertEqual(w["cast"][0]["tier"], "主体")
        self.assertEqual(w["chapter"], 2)
        self.assertEqual(w["chapter_goal"], "先摸到校门")
        self.assertEqual(w["route"]["goal"], "警察局")
        self.assertEqual(w["route"]["at"], 1)
        self.assertEqual([e["done"] for e in w["events"]], [True, False])
        facts = [d["fact"] for d in w["discovered"]]
        self.assertEqual(facts.count("安保室门后挂着一把枪"), 1, "静态事实没去重")
        kinds = {d["fact"]: d["kind"] for d in w["discovered"]}
        self.assertEqual(kinds["警察局那边刚起过尸潮"], "dynamic")
        self.assertEqual(kinds["今晚走廊特别安静"], "dynamic")
        self.assertIn("走廊里撞见周砚", w["log"])
        self.assertIn("林岸推开防火门", w["log"])
        self.assertEqual(w["actions"], ["贴着墙走", "先回宿舍", "喊一声"])
        self.assertEqual(w["turns"], 1)
        self.assertEqual(w["history"][-1],
                         {"role": "user", "content": "我要去警察局拿枪"})

    def test_cast_update_merges_by_name(self):
        w = self._world()
        novel._apply(w, {"cast_new": [
            {"name": "周砚", "relation": "陌生人", "present": False,
             "tier": "主体"}]}, "先看看")
        novel._apply(w, {"cast_update": [
            {"name": "周砚", "relation": "队友", "willing": "愿意跟",
             "present": True}]}, "说服他一起走")
        self.assertEqual(len(w["cast"]), 1)
        self.assertEqual(w["cast"][0]["relation"], "队友")
        self.assertEqual(w["cast"][0]["tier"], "主体", "更新不该抹掉已有字段")
        self.assertTrue(w["cast"][0]["present"])

    def test_ruling_is_not_a_rejection(self):
        """玩家说掏枪 → 编剧落成「摸到晾衣杆」，代码原样登记、不退回。"""
        w = self._world()
        ruling, facts = novel._apply(w, {
            "ruling": "林岸伸手去摸腰间，那里什么都没有；指尖碰到靠在柜边的晾衣杆",
            "facts": ["宿舍里没有枪", "林岸把晾衣杆攥在手里"],
            "items_gain": ["晾衣杆"],
        }, "我直接掏出手枪打丧尸")
        self.assertIn("晾衣杆", ruling)
        self.assertIn("没有枪", "".join(w["log"]))
        self.assertEqual(w["history"][-1]["content"],
                         "我直接掏出手枪打丧尸", "玩家原话必须留档")
        self.assertEqual(w["player"]["items"], ["晾衣杆"])
        self.assertIn("宿舍里没有枪", w["log"])
        self.assertNotIn("不行", "".join(w["log"]), "裁定不许写成拒绝输入")

    def test_ending_writes_death_log(self):
        w = self._world()
        w["player"]["items"] = ["课本"]
        novel._apply(w, {
            "ruling": "林岸想从窗子下去，体力不够，摔了下去",
            "facts": ["床帘没吃住力"],
            "place": "宿舍楼外侧",
            "ending": {"cause": "文弱书生翻窗体力不支坠楼",
                       "summary": "楼下那几只听见了动静"},
        }, "用床帘从窗户下去")
        self.assertEqual(w["ending"]["cause"], "文弱书生翻窗体力不支坠楼")
        self.assertEqual(w["ending"]["at"], "宿舍楼外侧")
        self.assertEqual(len(w["death_log"]), 1)
        self.assertEqual(w["death_log"][0]["identity"], "文弱书生")
        self.assertEqual(w["death_log"][0]["items"], ["课本"])


# ─── 三个 AI 的调用顺序 ───────────────────────────────

class ChainTest(NovelBase):
    def test_llm_call_is_strict_and_uses_the_shared_provider(self):
        """strict 保证不滑到付费官方降级链；链头跟扮演共用。"""
        with mock.patch.object(novel.llm, "call_llm",
                               return_value='{"ruling":"ok"}') as m:
            novel._call([{"role": "user", "content": "x"}], 100, 0.5)
        kw = m.call_args.kwargs
        self.assertTrue(kw["strict"])
        self.assertEqual(kw["provider"], novel.PERSONA_PROVIDER or None)
        self.assertEqual(kw["model"], novel.PERSONA_MODEL or None)
        self.assertEqual(kw["max_tokens"], 100)

    def test_run_turn_director_first_writer_second(self):
        out, _ = self.open_a_world()
        wid = out["world_id"]
        director = json.dumps({
            "ruling": "林岸在柜子里摸到一根晾衣杆",
            "facts": ["柜门卡住了，他拽了两下才开"],
            "items_gain": ["晾衣杆"],
            "actions": ["贴着门听走廊", "掀开床板", "检查书桌"],
        }, ensure_ascii=False)
        with mock.patch.object(
                novel, "_call", side_effect=[director, "林岸拽开柜门……"]) as m:
            res = novel.run_turn(wid, "检查柜子")
        self.assertEqual(m.call_count, 2)
        self.assertEqual(res["ruling"], "林岸在柜子里摸到一根晾衣杆")
        self.assertEqual(res["reply"], "林岸拽开柜门……")
        self.assertEqual(res["state"]["player"]["items"], ["晾衣杆"])
        self.assertEqual(res["state"]["actions"][0], "贴着门听走廊")
        w = novel.get_world(wid)
        self.assertEqual([r["role"] for r in w["history"]][-2:],
                         ["user", "assistant"])
        # 写作 AI 拿到的是已裁定事实，不是「检查柜子」这句意图本身
        writer_msg = m.call_args_list[1].args[0][0]["content"]
        self.assertIn("柜门卡住了", writer_msg)
        self.assertIn("晾衣杆", writer_msg)

    def test_ended_world_does_not_call_llm(self):
        out, _ = self.open_a_world()
        w = novel.get_world(out["world_id"])
        w["ending"] = {"cause": "坠楼", "summary": "……"}
        novel._save_world(w)
        with mock.patch.object(novel, "_call") as m:
            res = novel.run_turn(out["world_id"], "再试一次")
        self.assertFalse(res["ok"])
        self.assertIn("重开", res["error"])
        m.assert_not_called()

    def test_director_no_homework_is_not_swallowed(self):
        out, _ = self.open_a_world()
        with mock.patch.object(novel, "_call", side_effect=["今天不想写", ""]):
            with self.assertRaises(novel.NoHomework):
                novel.run_turn(out["world_id"], "去食堂")

    def test_writer_empty_body_is_no_homework(self):
        out, _ = self.open_a_world()
        d = json.dumps({"ruling": "摸了个空", "facts": []}, ensure_ascii=False)
        with mock.patch.object(novel, "_call", side_effect=[d, "  "]):
            with self.assertRaises(novel.NoHomework):
                novel.run_turn(out["world_id"], "检查柜子")

    def test_writer_contract_is_third_person_with_second_person_ui(self):
        c = novel._WRITER_CONTRACT
        self.assertIn("第三人称", c)
        self.assertIn("一律写主角的名字", c)
        self.assertIn("用「你」", c)
        self.assertIn("400 到 700 字", c)
        self.assertNotIn("好感", c)
        self.assertNotIn("亲密度", c)


# ─── 死亡重开：静态继承、动态不继承 ────────────────────

class RestartTest(NovelBase):
    def test_restart_keeps_static_facts_and_death_but_resets_the_person(self):
        out, _ = self.open_a_world()
        wid = out["world_id"]
        d = json.dumps({
            "ruling": "林岸一路摸到警察局，撞上尸潮",
            "facts": ["安保室门后确实挂着一把枪"],
            "items_gain": ["手枪", "钢筋"],
            "place": "警察局",
            "cast_new": [{"name": "周砚", "relation": "队友",
                          "tier": "主体", "present": True}],
            "events_add": [{"trigger": "李二引来尸潮", "kind": "dynamic"}],
            "discovered": [
                {"fact": "安保室门后挂着一把枪", "kind": "static"},
                {"fact": "食堂里有一只不动的怪物", "kind": "static"},
                {"fact": "警察局门口会突然起尸潮", "kind": "dynamic"},
            ],
            "route": {"goal": "警察局", "nodes": ["后门", "操场", "校门"],
                      "at": 3},
            "chapter_goal": "拿到枪",
            "chapter_advance": True,
            "ending": {"cause": "在警察局门口被尸潮围住", "summary": "没跑出去"},
        }, ensure_ascii=False)
        with mock.patch.object(novel, "_call",
                               side_effect=[d, "林岸没跑出去……"]):
            novel.run_turn(wid, "一路去警察局")
        old = novel.get_world(wid)
        self.assertTrue(old["ending"])

        with mock.patch.object(novel, "_call") as m:
            res = novel.restart(wid)
        m.assert_not_called()                      # 重开不重跑开场 AI
        self.assertTrue(res["ok"])
        self.assertEqual(res["reply"], OPENING_TEXT)

        w = novel.get_world(wid)
        self.assertEqual(w["id"], wid, "还是同一个世界、同一条线")
        self.assertEqual(w["seed"], old["seed"], "封样草稿复用")
        self.assertIsNone(w["ending"])
        self.assertEqual(w["chapter"], 1)
        self.assertEqual(w["player"]["place"], "男生宿舍 304")
        self.assertEqual(w["player"]["items"], [], "道具归零")
        self.assertEqual(w["cast"], [], "人物归零")
        self.assertEqual(w["events"], [], "事件归零")
        self.assertEqual(w["route"], {"goal": "", "nodes": [], "at": 0})
        self.assertEqual(w["turns"], 0)
        self.assertEqual(w["chapter_goal"], SEED["chapter_goal"])

        facts = [(x["fact"], x["kind"]) for x in w["discovered"]]
        self.assertIn(("安保室门后挂着一把枪", "static"), facts)
        self.assertIn(("食堂里有一只不动的怪物", "static"), facts)
        self.assertIn(("三楼走廊尽头有安全通道", "static"), facts)
        self.assertNotIn("警察局门口会突然起尸潮",
                         [x[0] for x in facts], "动态危险不能预知")
        self.assertEqual(len(w["death_log"]), 1)
        self.assertEqual(w["death_log"][0]["cause"],
                         "在警察局门口被尸潮围住")
        self.assertEqual(len(w["history"]), 1)     # 只剩开场白
        self.assertEqual(w["history"][0]["role"], "assistant")
        self.assertEqual(res["state"]["last_death"]["cause"],
                         "在警察局门口被尸潮围住")
        self.assertEqual(res["state"]["discovered"][0]["kind"], "static")

    def test_restart_can_rename_and_repick_identity(self):
        out, _ = self.open_a_world()
        with mock.patch.object(novel, "_call"):
            novel.restart(out["world_id"], "沈行", "体育生")
        w = novel.get_world(out["world_id"])
        self.assertEqual(w["player"]["name"], "沈行")
        self.assertEqual(w["player"]["identity"], "体育生")
        self.assertIn("短跑", w["player"]["identity_desc"])

    def test_restart_unknown_world_is_value_error(self):
        with self.assertRaises(ValueError):
            novel.restart("nv_nope")


# ─── HTTP 接口：三种错误分开说 ─────────────────────────

class ApiTest(NovelBase):
    def setUp(self):
        super().setUp()
        app = flask.Flask(__name__)
        app.register_blueprint(novel_api.bp)
        self.c = app.test_client()

    def test_plays_and_state_routes(self):
        r = self.c.get("/api/novel/plays")
        self.assertEqual(r.status_code, 200)
        self.assertIn("zombie", [p["id"] for p in r.get_json()["plays"]])
        self.assertEqual(self.c.get("/api/novel/worlds")
                         .get_json()["worlds"], [])
        self.assertEqual(self.c.get("/api/novel/state?id=nv_nope").status_code,
                         404)

    def test_open_route_returns_a_world(self):
        with mock.patch.object(novel, "_call",
                               side_effect=[_seed_text(), OPENING_TEXT]):
            r = self.c.post("/api/novel/open", json={
                "play": "zombie", "name": "林岸", "identity": "医学生"})
        self.assertEqual(r.status_code, 200)
        d = r.get_json()
        self.assertTrue(d["ok"])
        self.assertTrue(d["world_id"].startswith("nv_"))
        self.assertEqual(d["reply"], OPENING_TEXT)
        self.assertEqual(d["state"]["player"]["identity"], "医学生")

    def test_no_homework_is_502_retry(self):
        with mock.patch.object(novel, "open_world",
                               side_effect=novel.NoHomework("返回不了 JSON")):
            r = self.c.post("/api/novel/open", json={"play": "zombie"})
        self.assertEqual(r.status_code, 502)
        self.assertIn("再点一次", r.get_json()["error"])

    def test_bad_argument_is_400_with_the_reason(self):
        r = self.c.post("/api/novel/open", json={"play": "nope"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("没有这个玩法", r.get_json()["error"])
        r = self.c.post("/api/novel/turn", json={"id": "nv_x", "text": "  "})
        self.assertEqual(r.status_code, 400)
        self.assertIn("没有写意图", r.get_json()["error"])

    def test_link_failure_is_502_wait_instead_of_rewrite(self):
        with mock.patch.object(novel, "open_world",
                               side_effect=RuntimeError("upstream down")), \
                mock.patch.object(novel_api.log, "exception"):
            r = self.c.post("/api/novel/open", json={"play": "zombie"})
        self.assertEqual(r.status_code, 502)
        msg = r.get_json()["error"]
        self.assertIn("等一下再试", msg)
        self.assertNotIn("再点一次", msg)

    def test_all_six_routes_are_registered(self):
        app = flask.Flask(__name__)
        app.register_blueprint(novel_api.bp)
        paths = {r.rule for r in app.url_map.iter_rules()}
        expected = {
            "/api/novel/plays", "/api/novel/worlds", "/api/novel/state",
            "/api/novel/open", "/api/novel/turn", "/api/novel/restart",
        }
        self.assertTrue(expected <= paths, "少注册了：%r" % (expected - paths))

    def test_fail_helper_keeps_three_branches_distinct(self):
        app = flask.Flask(__name__)
        with app.test_request_context():
            a = novel_api._fail(novel.NoHomework("x"))[0].get_json()
            b = novel_api._fail(ValueError("y"))[0].get_json()
            with mock.patch.object(novel_api.log, "exception"):
                c = novel_api._fail(RuntimeError("z"))[0].get_json()
        self.assertEqual(len({a["error"], b["error"], c["error"]}), 3)


# ─── 静态体检：页面 / 接线 / 与 1v1 隔离 ────────────────

class WiringTest(unittest.TestCase):
    def setUp(self):
        self.html = _read(PAGE)
        self.main = _read(MAIN)
        self.index = _read(INDEX)
        self.src = _read(NOVEL_SRC)

    def test_no_duplicate_js_functions_or_top_level_vars(self):
        """同名函数 / 变量会静默覆盖（persona.html 开场白事故）。"""
        names = re.findall(
            r"^\s*(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(",
            self.html, re.M)
        dup = {k: v for k, v in collections.Counter(names).items() if v > 1}
        self.assertEqual(dup, {}, "小说页有同名函数：%r" % dup)
        self.assertGreater(len(names), 10)
        variables = re.findall(r"^(?:let|const|var)\s+([A-Za-z_$][\w$]*)\s*=",
                               self.html, re.M)
        dup_v = {k: v for k, v in collections.Counter(variables).items()
                 if v > 1}
        self.assertEqual(dup_v, {}, "小说页有重名顶层变量：%r" % dup_v)

    def test_page_has_no_1v1_numeric_state(self):
        for banned in ("affection", "好感", "gameday", "跨天", "服饰",
                       "怀孕", "outfitPrompt", "renderPanel", "depth_hint"):
            self.assertNotIn(banned, self.html,
                             "小说页混进了 1v1 状态条：%s" % banned)
        self.assertIn("状态卡", self.html)
        self.assertIn("已经探明的固定事实", self.html)
        self.assertIn("上一次怎么死的", self.html)

    def test_page_state_machine_guards_busy_and_ending(self):
        for needle in ("nvBusySet", "nvEnded = !!s.ending", "nvApplyState",
                       "nvRenderActions", "nvRestartBtn", "nvSendBtn",
                       "nvOpenBtn", "/api/novel/turn", "/api/novel/restart",
                       "/api/novel/open", "/api/novel/state",
                       "/api/novel/plays", "/api/novel/worlds"):
            self.assertIn(needle, self.html, "缺少 %s" % needle)
        self.assertIn("b.disabled = on || nvEnded", self.html)

    def test_page_is_a_standalone_three_column_file(self):
        for el in ("nvLeft", "nvMain", "nvRight", "nvPlayList",
                   "nvWorldList", "nvCard", "nvLog", "nvActions"):
            self.assertIn('id="%s"' % el, self.html)
        self.assertIn("@media (max-width:900px)", self.html)
        self.assertIn("/persona", self.html)

    def test_main_registers_blueprint_page_and_no_store(self):
        self.assertIn("from app import novel_api", self.main)
        self.assertIn("app.register_blueprint(novel_api.bp)", self.main)
        self.assertIn('@app.route("/novel")', self.main)
        self.assertIn("def _no_cache_novel", self.main)
        self.assertIn('request.path == "/novel"', self.main)
        self.assertIn('request.path.startswith("/api/novel/")', self.main)
        self.assertIn('"no-store, must-revalidate"', self.main)

    def test_index_has_the_third_mode_entry(self):
        self.assertIn("window.open('/novel','_blank')", self.index)
        self.assertIn("小说模式", self.index)

    def test_novel_page_uses_the_persona_skin(self):
        """不许再画一套 UI：变量、三栏宽度、按钮、消息流都跟 persona.html 同源。"""
        for needle in ("--bg-elev:", "--accent: #f472b6", "--r: 12px",
                       'class="app"', 'class="side" id="nvLeft"',
                       'class="mid" id="nvMain"', 'class="side2 nv-card"',
                       'class="btn pri"', 'class="log" id="nvLog"',
                       'class="composer nv-input"',
                       "grid-template-columns: 230px 1fr 288px"):
            self.assertIn(needle, self.html, "小说页没用共用皮肤：%s" % needle)
        self.assertNotIn("--nv-bg", self.html, "旧的独立配色还在")
        self.assertNotIn("prefers-color-scheme", self.html,
                         "persona 是固定黑底，小说页不该自带亮暗双主题")

    def test_persona_page_has_the_novel_entry(self):
        """用户反馈「没看到入口」：入口必须在角色扮演页里常驻可见。"""
        p = _read(os.path.join(ROOT, "web", "persona.html"))
        self.assertIn('id="btnNovel"', p)
        self.assertIn('href="/novel"', p)
        self.assertIn("小说模式", p)
        self.assertIn("function bindNovelLink()", p)
        self.assertIn('location.port === "5174"', p)
        self.assertIn(':5174/novel', p, "5175 打开同一份 HTML 时要能跳到 5174")
        self.assertNotIn('id="btnNovel" style="display:none', p,
                         "入口不许被样式藏起来")

    def test_engine_does_not_import_1v1_modules(self):
        for banned in ("from app import persona", "from app import affection",
                       "from app import galgame", "import persona",
                       "import affection"):
            self.assertNotIn(banned, self.src,
                             "小说引擎接回 1v1 了：%s" % banned)
        self.assertIn("from app import llm", self.src)
        self.assertIn("strict=True", self.src)

    def test_director_prompt_forbids_rejection_and_free_items(self):
        p = novel._DIRECTOR_PROMPT
        self.assertIn("裁定", p)
        self.assertIn("意图", p)
        self.assertIn("只有你发放的东西才会进主角的口袋", p)
        self.assertIn("拆成两三段能走的路", p)
        self.assertIn("地图结构和固定物资是", novel._OPEN_PROMPT)
        self.assertIn("重开变化的是 dynamic", novel._OPEN_PROMPT)
        self.assertIn("只有这些会留下来", novel._opening_doc("zombie"))


if __name__ == "__main__":
    unittest.main()
