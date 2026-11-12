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
import shutil
import tempfile
import unittest
from unittest import mock

import flask

from app import novel, novel_api, persona_rag

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

    def test_broken_json_error_carries_length_and_tail(self):
        """被 max_tokens 砍断时，detail 要能看出「原文多长、结尾停在哪」。"""
        raw = ('{"places":[{"name":"男生宿舍 304","detail":"柜门半开",'
               '"kind":"static"}],"item_pool":["晾衣杆","胶')
        with self.assertRaises(novel.NoHomework) as cm:
            novel._parse_json(raw)
        msg = str(cm.exception)
        self.assertIn("原文 %d 字" % len(raw), msg)
        self.assertIn("item_pool", msg)
        self.assertIn("返回的 JSON 解析不了", msg)

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
            {"id", "play", "play_name", "chapter", "chapter_goal", "clock",
             "ending", "player", "present", "cast", "route", "events", "log",
             "discovered", "last_death", "actions", "can_undo",
             "identity_choices", "history"},
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
        # 开场写作 AI 拿到的事实就是开场 AI 交的那几条（在最后一条 user 消息里）
        self.assertIn("林岸在 304 醒来",
                      m.call_args_list[1].args[0][-1]["content"])

    def test_unknown_play_is_value_error(self):
        with self.assertRaises(ValueError):
            novel.open_world("nope", "林岸", "体育生")

    def test_state_block_is_complete_and_not_a_summary(self):
        """写作 AI 拿到的必须是代码直接读出的原始状态，不是编剧的摘要。"""
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
        w["log"] = ["林岸试过那把刀，能用"]
        w["player"]["items"] = ["一袋薯片",
                                "{'name': '钢刀', 'from': '床底铁皮箱'}"]
        s = novel._state_block(w)
        for needle in ("林岸", "文弱书生", "男生宿舍 304", "一袋薯片", "钢刀",
                       "安保室门后挂着一把枪", "警察局那边刚起过尸潮",
                       "已经演过的事", "不许凭空多出别的东西",
                       "未经任何人压缩", "禁止重写"):
            self.assertIn(needle, s)
        self.assertNotIn("{'name'", s,
                         "老存档的 Python 字典字面量必须清洗掉")
        self.assertIn("周砚", s)
        self.assertIn("李二", s.split("遇到过的人")[1].split("\n")[0])
        self.assertNotIn("李二", s.split("在场的人：")[1].split("\n")[0])
        self.assertNotIn("主角团里有过谁", s,
                         "2026-11-12 真机：这行把死掉的丧尸也算成「主角团」")


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
                # 显式 done=False 才是「还留着尾巴」；没写 done 又有 result 的
                # 按当拍已兑现算（见 PresenceAndEventsTest）。
                {"trigger": "广播在重复一句话", "result": "还没查", "done": False,
                 "kind": "dynamic"},
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
    def test_llm_call_falls_back_like_1v1_and_uses_the_shared_provider(self):
        """链头跟扮演共用；中转站 402 时允许顺链降级，不再一击即败。"""
        with mock.patch.object(novel.llm, "call_llm",
                               return_value='{"ruling":"ok"}') as m:
            novel._call([{"role": "user", "content": "x"}], 100, 0.5)
        kw = m.call_args.kwargs
        self.assertFalse(kw["strict"])
        self.assertEqual(kw["provider"], novel.PERSONA_PROVIDER or None)
        self.assertEqual(kw["model"], novel.PERSONA_MODEL or None)
        self.assertEqual(kw["max_tokens"], 100)

    def test_three_ais_have_room_for_full_output(self):
        """真机 2400 token 把开场世界书草稿砍断（finish_reason=length），别改回去。"""
        self.assertGreaterEqual(novel.OPEN_MAX_TOKENS, 8192)
        self.assertGreaterEqual(novel.DIRECTOR_MAX_TOKENS, 4096)
        self.assertGreaterEqual(novel.WRITER_MAX_TOKENS, 4096)
        out, m = self.open_a_world()
        self.assertEqual(m.call_args_list[0].args[1], novel.OPEN_MAX_TOKENS)
        self.assertEqual(m.call_args_list[1].args[1], novel.WRITER_MAX_TOKENS)
        w = novel.get_world(out["world_id"])
        with mock.patch.object(novel, "_call",
                               return_value='{"ruling":"ok"}') as dm:
            novel._director(w, "检查柜子")
        self.assertEqual(dm.call_args.args[1], novel.DIRECTOR_MAX_TOKENS)

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
        writer_msg = m.call_args_list[1].args[0][-1]["content"]
        self.assertIn("柜门卡住了", writer_msg)
        self.assertIn("晾衣杆", writer_msg)
        # 而且拿到完整上下文：开场白正文、原始状态块、玩家原话、尾注
        self.assertIn(OPENING_TEXT, m.call_args_list[1].args[0][1]["content"],
                      "写作 AI 必须看到自己上一轮写的正文，否则会重演")
        self.assertIn("此刻的世界", writer_msg)
        self.assertIn("检查柜子", writer_msg)
        self.assertIn("上一段停在", writer_msg)

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

    def test_head_contract_is_third_person_and_state_driven(self):
        c = novel._NOVEL_CONTRACT
        self.assertIn("第三人称", c)
        self.assertIn("一律写主角的名字", c)
        self.assertIn("用「你」", c)
        self.assertIn("不要重演", c)
        self.assertIn("照着状态写", c)
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


# ─── 自建玩法：填一张表，AI 补成世界书 ───────────────────

CUSTOM_DRAFT = {
    "name": "废土城",
    "tagline": "义体比人命值钱",
    "brief": "三家公司把城市切成三块。",
    "rules": ["义体里装着定位芯片", "下雨天没人出门", "止痛药比子弹贵"],
    "identity_choices": [
        {"name": "黑医", "desc": "会换脸，手稳，但自己一身的病"},
        {"name": "跑腿的", "desc": "认识所有人"},
        {"name": "清道夫", "desc": "能打，但走不了远路"},
    ],
    "start": {"place": "旧维修巷二楼", "condition": "刚做完一台手术",
              "items": ["螺丝刀"]},
    "places": [
        {"name": "旧维修巷", "detail": "你的诊所", "search": "螺丝刀",
         "kind": "static"},
        {"name": "中央车站", "detail": "人最多的地方", "search": "什么都有人卖",
         "kind": "dynamic"},
    ],
    "item_pool": ["螺丝刀", "止痛药"],
    "cast_seed": [
        {"name": "老周", "identity": "收废品的", "relation": "欠你两次手术钱",
         "willing": "想赖账", "tier": "主体", "present": False},
    ],
    "chapter_goal": "弄到一批干净的止痛药",
    "opening_facts": ["你在诊所里给人拆线", "门外有人砸门"],
    "actions": ["开门", "从后窗往下看", "先把灯关了"],
    "discovered": [{"fact": "中央车站归三号公司管", "kind": "static"}],
}

# 玩家填的表：规则只写了两条、身份写了两个、地点写了两个。
CUSTOM_TABLE = {
    "name": "废土城",
    "rules": "义体里装着定位芯片\n下雨天没人出门",
    "places": "旧维修巷\n中央车站",
    "start_place": "旧维修巷二楼",
    "start_items": "螺丝刀",
    "identities": "黑医 | 会换脸，手稳\n跑腿的 | 认识所有人",
    "cast": "老周 · 收废品的 · 欠你两次手术钱",
    "goal": "弄到一批干净的止痛药",
}


class CustomPlayTest(NovelBase):
    def _open_custom(self, table=None, identity="", name="阿七"):
        """自建玩法开场：一次 LLM 交世界书；给了身份就再写一次正文。"""
        side = ([json.dumps(CUSTOM_DRAFT, ensure_ascii=False),
                 OPENING_TEXT] if identity
                else [json.dumps(CUSTOM_DRAFT, ensure_ascii=False)])
        with mock.patch.object(novel, "_call", side_effect=side) as m:
            out = novel.open_world(None, name, identity,
                                   table=table or CUSTOM_TABLE)
        return out, m

    # ── 表格的优先级：玩家填了的就是玩家说了算 ──
    def test_table_lines_win_over_the_model(self):
        p = novel._preset_from_draft(CUSTOM_DRAFT, CUSTOM_TABLE)
        self.assertEqual(p["name"], "废土城")
        # AI 给了三条规则，玩家只写了两条：只收玩家那两条。
        self.assertEqual(p["rules"], ["义体里装着定位芯片", "下雨天没人出门"])
        self.assertEqual(p["start"]["place"], "旧维修巷二楼")
        self.assertEqual(p["start"]["items"], ["螺丝刀"])
        self.assertEqual(p["start"]["chapter_goal"], "弄到一批干净的止痛药")
        # 玩家点了名要的两个身份，描述用他自己写的；AI 的第三个不收。
        self.assertEqual([c["name"] for c in p["identity_choices"]],
                         ["黑医", "跑腿的"])
        self.assertIn("会换脸", p["identity_choices"][0]["desc"])
        self.assertEqual(p["identity_choices"][1]["desc"], "认识所有人")

    def test_blank_field_is_filled_by_the_model(self):
        table = dict(CUSTOM_TABLE)
        table["rules"] = ""
        table["identities"] = ""
        p = novel._preset_from_draft(CUSTOM_DRAFT, table)
        self.assertEqual(len(p["rules"]), 3, "留空要由 AI 补")
        self.assertEqual(len(p["identity_choices"]), 3)
        text = novel._table_text(table)
        self.assertIn("（没填，你定）", text, "没填的要在提示词里标出来")

    def test_promise_that_played_fields_are_not_rewritten(self):
        self.assertIn("填了的是硬事实", novel._CUSTOM_PROMPT)
        self.assertIn("一条都不许改", novel._CUSTOM_PROMPT)
        self.assertNotIn("{{", novel._CUSTOM_PROMPT,
                         "这份提示词用 replace 不 format，花括号不该转义")

    # ── 内嵌：不进 novels/，不改全局玩法列表 ──
    def test_custom_world_embeds_preset_and_keeps_novels_clean(self):
        before = len(novel.list_plays())
        out, _ = self._open_custom(identity="黑医")
        w = novel.get_world(out["world_id"])
        self.assertEqual(w["preset"]["name"], "废土城")
        self.assertEqual(w["play"], "custom")
        self.assertEqual(w["play_name"], "废土城")
        self.assertEqual(len(novel.list_plays()), before,
                         "自建玩法绝不能混进全局玩法列表")
        custom_dir = os.path.join(novel.NOVELS_DIR, "custom")
        self.assertFalse(os.path.isdir(custom_dir),
                         "自建玩法不落 novels/ 目录（那里不认 owner）")
        self.assertEqual(out["reply"], OPENING_TEXT)
        self.assertEqual(w["player"]["identity"], "黑医")

    def test_open_returns_only_candidates_when_identity_is_blank(self):
        out, m = self._open_custom()
        self.assertTrue(out["need_identity"])
        self.assertEqual(out["play_name"], "废土城")
        self.assertEqual(len(out["identity_choices"]), 2)
        self.assertEqual(m.call_count, 1, "身份没定之前不该写正文")
        w = novel.get_world(out["world_id"])
        self.assertIsNotNone(w, "世界书要先落盘，别让一轮模型白烧")
        self.assertEqual(w["opening_reply"], "")
        self.assertEqual(w["history"], [])

    def test_one_identity_means_no_question_at_all(self):
        """只写了一个身份，就是已经选好了：一次点完直接出正文。"""
        table = dict(CUSTOM_TABLE)
        table["identities"] = "黑医 | 会换脸，手稳"
        with mock.patch.object(
                novel, "_call",
                side_effect=[json.dumps(CUSTOM_DRAFT, ensure_ascii=False),
                             OPENING_TEXT]) as m:
            out = novel.open_world(None, "阿七", "", table=table)
        self.assertNotIn("need_identity", out)
        self.assertEqual(out["reply"], OPENING_TEXT)
        self.assertEqual(m.call_count, 2)
        self.assertEqual(
            novel.get_world(out["world_id"])["player"]["identity"], "黑医")

    def test_picking_identity_seals_without_regenerating_the_world(self):
        out, _ = self._open_custom()
        with mock.patch.object(novel, "_call",
                               side_effect=[OPENING_TEXT]) as m:
            res = novel.open_world(None, None, "跑腿的",
                                   world_id=out["world_id"])
        self.assertEqual(m.call_count, 1,
                         "选身份只该再跑写作，不该重跑整本世界观")
        self.assertEqual(res["reply"], OPENING_TEXT)
        w = novel.get_world(out["world_id"])
        self.assertEqual(w["player"]["identity"], "跑腿的")
        self.assertIn("认识所有人", w["player"]["identity_desc"])
        self.assertEqual(len(w["history"]), 1)

    def test_sealed_world_is_idempotent(self):
        out, _ = self._open_custom(identity="黑医")
        with mock.patch.object(novel, "_call") as m:
            again = novel.open_world(None, None, "清道夫",
                                     world_id=out["world_id"])
        self.assertEqual(m.call_count, 0, "封过样的世界不能再跑模型")
        self.assertEqual(again["reply"], OPENING_TEXT)
        self.assertEqual(novel.get_world(out["world_id"])["player"]["identity"],
                         "黑医")

    # ── 世界书只读视图 ──
    def test_world_book_lists_rules_places_and_people(self):
        out, _ = self._open_custom(identity="黑医")
        w = novel.get_world(out["world_id"])
        novel._apply(w, {"place": "中央车站"}, "")
        novel._save_world(w)
        b = novel.world_book(novel.get_world(out["world_id"]))
        self.assertEqual(b["name"], "废土城")
        self.assertTrue(b["custom"])
        self.assertEqual(len(b["rules"]), 2)
        names = [p["name"] for p in b["places"]]
        self.assertIn("旧维修巷", names)
        self.assertIn("中央车站", names)
        here = [p for p in b["places"] if p["here"]]
        self.assertEqual([p["name"] for p in here], ["中央车站"])
        seen = [p["name"] for p in b["places"] if p["visited"]]
        self.assertIn("旧维修巷", seen, "走过的地方要留在世界书里")
        self.assertEqual(b["person"]["place"], "中央车站")
        # 设定里的人还没打过交道，只出现在「设定里的人」那一栏，不能混进
        # 「遇到过的人」（这条在真机上踩过：遇到过的人里蹲着一只丧尸）。
        self.assertEqual(b["cast"], [])
        self.assertIn("老周", [c["name"] for c in b["cast_seed"]])

    def test_world_book_has_no_write_side_effect(self):
        out, _ = self._open_custom(identity="黑医")
        before = json.dumps(novel.get_world(out["world_id"]),
                            ensure_ascii=False, sort_keys=True)
        novel.world_book(novel.get_world(out["world_id"]))
        after = json.dumps(novel.get_world(out["world_id"]),
                           ensure_ascii=False, sort_keys=True)
        self.assertEqual(before, after)

    def test_restart_keeps_the_embedded_preset(self):
        out, _ = self._open_custom(identity="黑医")
        with mock.patch.object(novel, "_call"):
            novel.restart(out["world_id"], "阿八", "跑腿的")
        w = novel.get_world(out["world_id"])
        self.assertEqual(w["preset"]["name"], "废土城",
                         "重开不能掉回磁盘预设")
        self.assertEqual(w["play_name"], "废土城")
        self.assertEqual(w["player"]["identity"], "跑腿的")
        self.assertEqual(w["restarts"], 1)

    def test_custom_world_belongs_to_its_owner_only(self):
        out, _ = self._open_custom(identity="黑医")
        self.assertIsNotNone(novel.get_world(out["world_id"], ""))
        self.assertIsNone(novel.get_world(out["world_id"], "99990001"))
        self.assertEqual(novel.list_worlds("99990001"), [])

    def test_state_card_carries_the_choices_for_the_restart_panel(self):
        out, _ = self._open_custom()
        s = novel.state_card(novel.get_world(out["world_id"]))
        self.assertEqual([c["name"] for c in s["identity_choices"]],
                         ["黑医", "跑腿的"])


# ─── 复用 1v1 内核：完整上下文 / 状态直达 / RAG ───────────

class KernelReuseTest(NovelBase):
    def test_messages_are_system_plus_full_history_plus_one_user(self):
        out, _ = self.open_a_world()
        w = novel.get_world(out["world_id"])
        w["history"].append({"role": "user", "content": "检查柜子"})
        w["history"].append({"role": "assistant", "content": "林岸拽开柜门"})
        msgs = novel._messages(w, facts=["摸到一根晾衣杆"], said="检查柜子")
        self.assertEqual([m["role"] for m in msgs],
                         ["system", "assistant", "user", "assistant", "user"])
        self.assertIn("第三人称", msgs[0]["content"])
        self.assertIn("林岸拽开柜门", msgs[-2]["content"],
                      "写作 AI 必须看到自己上一轮写的完整正文")
        self.assertIn("玩家当时写下的剧情骨架", msgs[-3]["content"],
                      "历史里的玩家话要标明「是让你扩写的骨架」，不是一句待回复的话")

    def test_director_and_writer_share_the_same_raw_state(self):
        """用户点名的错误：两个 AI 看到的东西不一样。"""
        out, _ = self.open_a_world()
        w = novel.get_world(out["world_id"])
        w["player"]["items"] = ["钢刀", "两包薯片"]
        d = novel._messages(w, said="去食堂", for_director=True)
        r = novel._messages(w, facts=["钢刀还在手上"], said="去食堂")
        ds = [m["content"] for m in d if m["role"] == "user"][-1]
        ws = [m["content"] for m in r if m["role"] == "user"][-1]
        for needle in ("钢刀", "两包薯片", "此刻的世界", "未经任何人压缩"):
            self.assertIn(needle, ds)
            self.assertIn(needle, ws)
        self.assertIn("裁定", d[0]["content"], "编剧拿的是裁定规则")
        self.assertIn("叙述者", r[0]["content"], "写作拿的是叙述契约")
        self.assertIn("这一拍必须写到的节点", ws)

    def test_writer_gets_facts_on_top_of_full_context_not_instead_of_it(self):
        out, _ = self.open_a_world()
        w = novel.get_world(out["world_id"])
        u = novel._user_block(w, facts=["摸到晾衣杆"], said="检查柜子")
        self.assertIn("此刻的世界", u)
        self.assertIn("玩家这一轮写下的剧情", u)
        self.assertIn("摸到晾衣杆", u)
        self.assertTrue(u.rstrip().endswith("）"),
                        "近因位尾注必须是最后一句")
        self.assertIn("不要重演", novel._head(w))

    def test_no_1v1_pronoun_rewrite_in_history(self):
        """persona._render_hist 会把「他」改成「你」，小说不能用。"""
        out, _ = self.open_a_world()
        w = novel.get_world(out["world_id"])
        w["history"].append({"role": "assistant",
                             "content": "他把刀横在桌上，试了一下。"})
        hist = novel._hist(w)
        self.assertIn("他把刀横在桌上", hist[-1]["content"])
        self.assertNotIn("你把刀横在桌上", hist[-1]["content"])

    def test_rag_is_reused_with_world_line(self):
        out, _ = self.open_a_world()
        w = novel.get_world(out["world_id"])
        with mock.patch.object(novel.persona_rag, "enabled_for",
                               return_value=True), \
                mock.patch.object(novel.persona_rag, "build_block",
                                  return_value="【回忆】安保室有枪") as m:
            block = novel._rag_block(w, "安保室有枪吗")
        self.assertIn("安保室有枪", block)
        self.assertEqual(m.call_args.args[0], novel._rag_line(w))
        self.assertTrue(m.call_args.kwargs.get("context") is not None)

    def test_rag_line_changes_on_restart(self):
        out, _ = self.open_a_world()
        wid = out["world_id"]
        before = novel._rag_line(novel.get_world(wid))
        with mock.patch.object(novel, "_call"):
            novel.restart(wid)
        after = novel._rag_line(novel.get_world(wid))
        self.assertNotEqual(before, after,
                            "重开后必须换 RAG 线，否则上一周目的尸潮会被检索回来")

    def test_legacy_dict_items_are_cleaned_on_read(self):
        """用户真机看到的「name from Name from」必须消失。"""
        out, _ = self.open_a_world()
        wid = out["world_id"]
        w = novel.get_world(wid)
        w["player"]["items"] = [
            "一袋薯片",
            "{'name': '两个满电充电宝', 'from': '书桌抽屉最里层'}",
            {"name": "登山包", "qty": "一个"},
        ]
        novel._save_world(w)
        got = novel.get_world(wid)
        self.assertEqual(got["player"]["items"],
                         ["一袋薯片", "两个满电充电宝", "一个登山包"])
        self.assertNotIn("from", "".join(got["player"]["items"]))

    def test_director_returns_full_item_list_and_code_only_normalizes(self):
        """数量变化 / 放下 / 刀不消失，全由编剧交完整清单裁定。"""
        w = self._world_if_any()
        novel._apply(w, {
            "items": [{"name": "钢刀", "qty": "一把"},
                      "一袋薯片", "铁晾衣杆"],
        }, "收拾东西")
        self.assertEqual(w["player"]["items"],
                         ["一把钢刀", "一袋薯片", "铁晾衣杆"])
        novel._apply(w, {
            "items": [{"name": "钢刀", "qty": "一把"}, "两包薯片"],
            "place": "宿舍楼外",
        }, "两天后离开宿舍楼")
        self.assertEqual(w["player"]["items"], ["一把钢刀", "两包薯片"],
                         "薯片消耗了、晾衣杆没带走，刀必须还在")
        self.assertEqual(w["player"]["place"], "宿舍楼外")

    def _world_if_any(self):
        out, _ = self.open_a_world()
        return novel.get_world(out["world_id"])

    def test_opening_is_idempotent_when_world_id_is_given(self):
        """站主怕「再点一次开场封样又重新跑一遍」。"""
        out, _ = self.open_a_world()
        with mock.patch.object(novel, "_call") as m:
            again = novel.open_world("zombie", "改个名", "体育生",
                                     world_id=out["world_id"])
        m.assert_not_called()
        self.assertTrue(again["sealed"])
        self.assertEqual(again["world_id"], out["world_id"])
        self.assertEqual(again["state"]["player"]["identity"], "文弱书生",
                         "身份已封样，不许被重复点开时改掉")

    def test_rename_only_changes_the_name(self):
        out, _ = self.open_a_world()
        wid = out["world_id"]
        r = novel.rename(wid, "沈行")
        self.assertTrue(r["ok"])
        w = novel.get_world(wid)
        self.assertEqual(w["player"]["name"], "沈行")
        self.assertEqual(w["player"]["identity"], "文弱书生")
        self.assertEqual(w["opening_reply"], OPENING_TEXT,
                         "已写下的正文留着当时的名字，不篡改历史")
        with self.assertRaises(ValueError):
            novel.rename(wid, "   ")


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

    def test_no_homework_leaves_a_log_line(self):
        """2026-11-12：这一支原来不打日志，连撞三次 502 也只能靠猜。"""
        with mock.patch.object(novel, "open_world",
                               side_effect=novel.NoHomework("模型没有返回内容")):
            with self.assertLogs("novel_api", level="WARNING") as cm:
                r = self.c.post("/api/novel/open", json={"play": "zombie"})
        self.assertEqual(r.status_code, 502)
        line = "\n".join(cm.output)
        self.assertIn("/api/novel/open", line, "日志要能看出是哪个接口")
        self.assertIn("模型没有返回内容", line, "日志要带真实原因")

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
        self.assertIn("upstream down", r.get_json()["detail"],
                      "具体原因必须回给前端，红条才不会只说一句空话")

    def test_all_routes_are_registered(self):
        app = flask.Flask(__name__)
        app.register_blueprint(novel_api.bp)
        paths = {r.rule for r in app.url_map.iter_rules()}
        expected = {
            "/api/novel/plays", "/api/novel/worlds", "/api/novel/state",
            "/api/novel/book", "/api/novel/open", "/api/novel/turn",
            "/api/novel/restart",
        }
        self.assertTrue(expected <= paths, "少注册了：%r" % (expected - paths))

    def test_open_route_accepts_a_self_made_table(self):
        with mock.patch.object(
                novel, "_call",
                side_effect=[json.dumps(CUSTOM_DRAFT, ensure_ascii=False),
                             OPENING_TEXT]):
            r = self.c.post("/api/novel/open", json={
                "table": CUSTOM_TABLE, "name": "阿七", "identity": "黑医"})
        self.assertEqual(r.status_code, 200)
        d = r.get_json()
        self.assertTrue(d["ok"])
        self.assertEqual(d["reply"], OPENING_TEXT)
        self.assertEqual(d["state"]["play_name"], "废土城")
        self.assertEqual(d["state"]["player"]["identity"], "黑医")

    def test_book_route_is_read_only(self):
        with mock.patch.object(
                novel, "_call",
                side_effect=[json.dumps(CUSTOM_DRAFT, ensure_ascii=False),
                             OPENING_TEXT]):
            d = self.c.post("/api/novel/open", json={
                "table": CUSTOM_TABLE, "name": "阿七",
                "identity": "黑医"}).get_json()
        r = self.c.get("/api/novel/book?id=" + d["world_id"])
        self.assertEqual(r.status_code, 200)
        book = r.get_json()["book"]
        self.assertEqual(book["name"], "废土城")
        self.assertEqual(len(book["rules"]), 2)
        self.assertEqual(self.c.get("/api/novel/book?id=nv_nope").status_code,
                         404)

    def test_open_with_table_and_no_identity_asks_one_question(self):
        with mock.patch.object(
                novel, "_call",
                side_effect=[json.dumps(CUSTOM_DRAFT, ensure_ascii=False)]):
            d = self.c.post("/api/novel/open", json={
                "table": CUSTOM_TABLE, "name": "阿七"}).get_json()
        self.assertTrue(d["ok"])
        self.assertTrue(d["need_identity"])
        self.assertEqual([c["name"] for c in d["identity_choices"]],
                         ["黑医", "跑腿的"])

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
        """用户反馈「没看到入口」：入口必须在角色扮演页里常驻可见。

        2026-11-12 上架 5175 后改成相对地址：5174 和 5175 各自都有 /novel，
        按端口改写会把公网访客送到不存在的 127.0.0.1:5174。
        """
        p = _read(os.path.join(ROOT, "web", "persona.html"))
        self.assertIn('id="btnNovel"', p)
        self.assertIn('href="/novel"', p)
        self.assertIn("小说模式", p)
        self.assertIn("function bindNovelLink()", p)
        self.assertIn('a.href = "/novel"', p)
        self.assertNotIn(':5174/novel', p,
                         "上架 5175 后不能再按端口跳本机，公网访客会吃死链")
        self.assertNotIn('if (!local) { a.style.display = "none"', p,
                         "公网访客不该再被整个藏掉")
        self.assertNotIn('id="btnNovel" style="display:none', p,
                         "入口不许被样式藏起来")

    def test_engine_reuses_the_1v1_kernel_without_the_state_bars(self):
        """2026-11-12 站主纠正「别从 0 造轮子」：小说复用扮演内核。

        允许 import persona（只读纯函数），但不许接好感 / 跨天 / 服饰那些
        数值状态条。persona.py 本身一行不改（1v1 正在跑那个文件）。
        """
        for banned in ("from app import affection", "from app import galgame",
                       "import affection", "import galgame", "pregnancy"):
            self.assertNotIn(banned, self.src,
                             "小说引擎接回 1v1 状态条了：%s" % banned)
        for needle in ("from app import llm, persona, persona_rag, writing_style",
                       "persona.cache_rows", "persona_rag.build_block",
                       "persona_rag.remember", "writing_style.LENGTH_ANCHOR"):
            self.assertIn(needle, self.src, "没复用扮演内核：%s" % needle)
        self.assertIn('o.get("strict", False)', self.src,
                      "站主默认不 strict，402 才能顺链降级；群友的免费层由接口层传严格模式")
        body = re.search(r"def _call\((.*?)\n\ndef ", self.src, re.S).group(1)
        self.assertNotIn("strict=True", body,
                         "服务层不许硬编码 strict=True：中转站余额归零时站主开场会直接 502")

    def test_error_box_shows_the_real_reason_instead_of_wait_forever(self):
        """402 余额不足时前端必须给可操作提示，并把 detail 原样显示。"""
        self.assertIn("function nvErr(msg, detail)", self.html)
        self.assertIn("nv-err-sub", self.html)
        self.assertIn("模型额度不足", self.html)
        self.assertIn("不是等一下就好", self.html)
        self.assertIn("Insufficient Balance", self.html)
        # 开场 / 选完身份再封样 / 一轮 / 重开 / 改名 / 回退
        self.assertEqual(self.html.count("nvErr(d.error, d.detail)"), 6,
                         "六个接口失败分支都要带 detail")

    def test_page_has_the_self_made_play_and_world_book(self):
        """自建玩法（填表生成）和世界书查看，两块入口都不能悄悄没了。"""
        for token in ('id="nvMakeBtn"', 'id="nvMakeMask"', 'id="nvMkGo"',
                      'id="nvMkRules"', 'id="nvMkIds"', 'id="nvPickMask"',
                      'id="nvBookBtn"', 'id="nvBookMask"'):
            self.assertIn(token, self.html, "页面缺少：%s" % token)
        self.assertIn("/api/novel/book", self.html)
        self.assertIn("table: table", self.html, "自建玩法的表格没随请求发出去")
        self.assertIn("need_identity", self.html, "AI 给身份候选时要能接住")
        self.assertIn("你自己写的玩法", self.html)

    def test_director_prompt_forbids_rejection_and_free_items(self):
        p = novel._DIRECTOR_PROMPT
        self.assertIn("裁定", p)
        self.assertIn("意图", p)
        self.assertIn("只有你发放的东西才会进主角的口袋", p)
        self.assertIn("拆成两三段能走的路", p)
        self.assertIn("地图结构和固定物资是", novel._OPEN_PROMPT)
        self.assertIn("重开变化的是 dynamic", novel._OPEN_PROMPT)
        self.assertIn("只有这些会留下来", novel._opening_doc("zombie"))


# ─── 2026-11-12：复用 1v1 内核之后，接口层与七项 UI 的守卫 ─────────

class WarmWorldTest(NovelBase):
    """老世界「打开即能用」：不用等下一轮写完才把 history 回填进 RAG。"""

    def setUp(self):
        super().setUp()
        app = flask.Flask(__name__)
        app.register_blueprint(novel_api.bp)
        self.c = app.test_client()

    def test_opening_an_old_world_queues_rag_backfill(self):
        out, _ = self.open_a_world()
        with mock.patch.object(novel.persona_rag, "enabled_for",
                               return_value=True), \
                mock.patch.object(novel.persona_rag, "warm") as warm, \
                mock.patch.object(novel.persona_rag,
                                  "schedule_backfill") as bf:
            r = self.c.get("/api/novel/state?id=" + out["world_id"])
        self.assertEqual(r.status_code, 200)
        warm.assert_called()
        self.assertTrue(bf.called)
        line, factory = bf.call_args.args
        self.assertEqual(line, out["world_id"] + "#0")
        self.assertEqual(len(factory()), len(out["state"]["history"]),
                         "回填的必须是已写下的全部 history，不是切片")

    def test_idempotent_open_also_warms_the_existing_world(self):
        out, _ = self.open_a_world()
        with mock.patch.object(novel, "_call") as call, \
                mock.patch.object(novel, "warm_world") as warm:
            d = novel.open_world("zombie", "别的名字", "体育生",
                                 out["world_id"])
        call.assert_not_called()          # 重复点封样不再跑 LLM
        warm.assert_called_once()
        self.assertTrue(d["sealed"])
        self.assertEqual(novel.get_world(out["world_id"])["player"]["identity"],
                         "文弱书生", "身份不能被重复封样改掉")

    def test_warm_world_does_not_mutate_the_world(self):
        out, _ = self.open_a_world()
        w = novel.get_world(out["world_id"])
        before = json.dumps(w, ensure_ascii=False, sort_keys=True)
        with mock.patch.object(novel.persona_rag, "enabled_for",
                               return_value=True), \
                mock.patch.object(novel.persona_rag, "warm"), \
                mock.patch.object(novel.persona_rag, "schedule_backfill"):
            novel.warm_world(w)
        self.assertEqual(json.dumps(w, ensure_ascii=False, sort_keys=True),
                         before)


class CustomModelApiTest(NovelBase):
    """自定义模型不能把「402 顺链降级」修好的链路又切断。"""

    def setUp(self):
        super().setUp()
        app = flask.Flask(__name__)
        app.register_blueprint(novel_api.bp)
        self.c = app.test_client()

    def test_no_custom_models_does_not_switch_the_chain(self):
        with mock.patch.object(novel_api.llm, "set_custom_override") as setter:
            self.c.post("/api/novel/turn", json={"id": "nv_x", "text": "走"})
        attached = [c.args[0] for c in setter.call_args_list
                    if c.args and c.args[0] is not None]
        self.assertEqual(attached, [],
                         "挂 set_custom_override([]) 会让 candidates 丢掉 "
                         "LLM_FALLBACK_CHAIN，中转站一挂就无处可降")

    def test_custom_models_are_attached_for_the_whole_request(self):
        custom = [{"base_url": "https://x/v1", "model": "m", "api_key": "k"}]
        with mock.patch.object(novel_api.llm, "set_custom_override") as setter:
            self.c.post("/api/novel/turn",
                        json={"id": "nv_x", "text": "走",
                              "custom_models": custom})
        attached = [c.args[0] for c in setter.call_args_list
                    if c.args and c.args[0] is not None]
        self.assertEqual(attached, [custom])

    def test_every_json_reply_carries_model_used_and_confirm(self):
        d = self.c.get("/api/novel/plays").get_json()
        self.assertIn("model_used", d)
        self.assertEqual(d["model_used"]["kind"], "chain_head")
        self.assertEqual(d["model_used"]["name"], novel_api.PERSONA_MODEL)
        self.assertEqual(d["restart_confirm"], novel_api.RESTART_CONFIRM)
        self.assertEqual(d["chain_head"]["model"], novel_api.PERSONA_MODEL)

    def test_model_used_does_not_leak_from_a_previous_request(self):
        with mock.patch.object(novel_api.llm, "persona_last_model",
                               return_value={"kind": "custom",
                                             "name": "上一个请求残留"}):
            d = self.c.get("/api/novel/plays").get_json()
        self.assertEqual(d["model_used"]["kind"], "chain_head")

    def test_open_route_forwards_world_id_for_idempotency(self):
        with mock.patch.object(novel, "open_world",
                               return_value={"ok": True, "sealed": True}) as m:
            self.c.post("/api/novel/open",
                        json={"play": "zombie", "world_id": "nv_1"})
        self.assertEqual(m.call_args.args[3], "nv_1")

    def test_restart_needs_the_typed_confirmation(self):
        out, _ = self.open_a_world()
        wid = out["world_id"]
        self.assertEqual(self.c.post("/api/novel/restart",
                                     json={"id": wid}).status_code, 400)
        r = self.c.post("/api/novel/restart",
                        json={"id": wid, "confirm": "我同意"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("确认句", r.get_json()["error"])
        ok = self.c.post("/api/novel/restart",
                         json={"id": wid,
                               "confirm": novel_api.RESTART_CONFIRM})
        self.assertEqual(ok.status_code, 200)
        self.assertTrue(ok.get_json()["ok"])

    def test_rename_route_only_changes_the_name(self):
        out, _ = self.open_a_world()
        wid = out["world_id"]
        r = self.c.post("/api/novel/rename",
                        json={"id": wid, "name": "阿岸"})
        self.assertEqual(r.get_json()["state"]["player"]["name"], "阿岸")
        self.assertEqual(novel.get_world(wid)["player"]["identity"],
                         "文弱书生")

    def test_quota_reports_the_owner_fact(self):
        d = self.c.get("/api/novel/quota").get_json()
        self.assertTrue(d["ok"])
        self.assertFalse(d["limited"])
        self.assertEqual(d["mode"], "owner")

    def test_custom_test_validates_the_draft(self):
        self.assertEqual(self.c.post("/api/novel/custom_test",
                                     json={"base_url": "x"}).status_code, 400)
        with mock.patch.object(novel_api.llm, "test_custom",
                               return_value=(True, 0.4, "pong")):
            d = self.c.post("/api/novel/custom_test", json={
                "base_url": "https://x/v1", "model": "m",
                "api_key": "k"}).get_json()
        self.assertTrue(d["ok"])
        self.assertEqual(d["info"], "pong")

    def test_new_routes_are_registered(self):
        app = flask.Flask(__name__)
        app.register_blueprint(novel_api.bp)
        paths = {r.rule for r in app.url_map.iter_rules()}
        for p in ("/api/novel/rename", "/api/novel/quota",
                  "/api/novel/custom_test"):
            self.assertIn(p, paths)


class UiSevenTest(unittest.TestCase):
    """七项 UI 的静态体检（页面不跑浏览器，只锁住关键接线）。"""

    def setUp(self):
        self.html = _read(PAGE)
        self.api = _read(os.path.join(ROOT, "app", "novel_api.py"))

    def test_restart_has_typed_confirmation(self):
        for needle in ("nvWipeMask", "nvWipeInput", "nvWipeOk", "nvConfirm",
                       "我同意重开这一本", "confirm:nvConfirm",
                       "原样打一遍"):
            self.assertIn(needle, self.html, "缺少 %s" % needle)
        self.assertEqual(novel_api.RESTART_CONFIRM, "我同意重开这一本")
        self.assertIn("RESTART_CONFIRM", self.api)

    def test_custom_model_current_model_and_quota_are_near_the_send_row(self):
        for needle in ("nvCustomBtn", "nvModelNow", "nvQuotaBtn", "nvQuotaText",
                       "/api/novel/quota", "/api/novel/custom_test",
                       "persona_custom_models", "custom_models = nvCM",
                       "function nvModelUsed"):
            self.assertIn(needle, self.html, "缺少 %s" % needle)

    def test_identity_is_locked_and_name_stays_editable(self):
        for needle in ("function nvLockSeal", "nvIdentity').disabled = nvSealed",
                       "已封样", "nvRenameBtn", "/api/novel/rename",
                       "world_id:nvWorldId", "nvWipeIdentity"):
            self.assertIn(needle, self.html, "缺少 %s" % needle)

    def test_typing_dots_feedback(self):
        for needle in ("nv-typing", "@keyframes nvBounce", "function nvTyping",
                       "正在写…", "nvTyping(nvBusy)"):
            self.assertIn(needle, self.html, "缺少 %s" % needle)

    def test_mobile_drawer_matches_persona(self):
        for needle in ("--vh", "nv-burger", "nv-stats", "nvDrawer",
                       "nvCloseDrawer", "safe-area-inset-bottom",
                       "font-size: 16px", "@media (max-width:1000px)",
                       "translateX(-100%)", "translateX(100%)",
                       "env(safe-area-inset-top)"):
            self.assertIn(needle, self.html, "缺少 %s" % needle)

    def test_custom_override_does_not_break_the_fallback_chain(self):
        # 模块注释里会引用 `set_custom_override([])` 解释为什么不能那么写，
        # 断言只看真正的代码部分。
        code = re.sub(r'^""".*?"""', '', self.api, count=1, flags=re.S)
        self.assertIn("if customs:", code)
        self.assertIn("g.novel_has_custom = bool(customs)", code)
        self.assertNotIn("set_custom_override([])", code)


# ─── 上架 5175（2026-11-12）：归属隔离 / 链策略 / 幂等 / 计费 / 网关 ───

class OwnerIsolationTest(NovelBase):
    """世界书原来是全局单文件，上架前必须把「这是谁的书」钉死。"""

    def test_owner_is_written_when_a_world_is_opened(self):
        with mock.patch.object(novel, "_call",
                               side_effect=[_seed_text(), OPENING_TEXT,
                                            _seed_text(), OPENING_TEXT]):
            a = novel.open_world("zombie", "站主", "体育生", owner="")
            b = novel.open_world("zombie", "群友", "医学生", owner="123456")
        self.assertEqual(novel.get_world(a["world_id"])["owner"], "")
        self.assertEqual(novel.get_world(b["world_id"], "123456")["owner"],
                         "123456")

    def test_each_viewer_only_lists_their_own_worlds(self):
        with mock.patch.object(novel, "_call",
                               side_effect=[_seed_text(), OPENING_TEXT,
                                            _seed_text(), OPENING_TEXT,
                                            _seed_text(), OPENING_TEXT]):
            novel.open_world("zombie", "站主", "体育生", owner="")
            novel.open_world("zombie", "甲", "医学生", owner="111")
            novel.open_world("zombie", "乙", "文弱书生", owner="222")
        self.assertEqual([w["name"] for w in novel.list_worlds("")], ["站主"])
        self.assertEqual([w["name"] for w in novel.list_worlds("111")], ["甲"])
        self.assertEqual([w["name"] for w in novel.list_worlds("222")], ["乙"])
        self.assertEqual(novel.list_worlds("333"), [])

    def test_cross_owner_read_and_all_writes_are_rejected(self):
        out, _ = self.open_a_world()
        wid = out["world_id"]
        self.assertIsNone(novel.get_world(wid, "999"))
        with self.assertRaisesRegex(ValueError, "没有这个世界"):
            novel.run_turn(wid, "摸一下门", owner="999")
        with self.assertRaisesRegex(ValueError, "没有这个世界"):
            novel.rename(wid, "偷改的名字", owner="999")
        with self.assertRaisesRegex(ValueError, "没有这个世界"):
            novel.restart(wid, owner="999")
        self.assertEqual(novel.get_world(wid)["player"]["name"], "林岸")

    def test_legacy_world_without_owner_belongs_to_the_station_owner(self):
        """老存档没有 owner 字段，读的时候归站主，群友一个也看不到。"""
        out, _ = self.open_a_world()
        w = novel.get_world(out["world_id"])
        del w["owner"]
        db = {"worlds": {w["id"]: w}}
        with open(novel._FILE, "w", encoding="utf-8") as f:
            json.dump(db, f, ensure_ascii=False)
        self.assertEqual(novel.get_world(w["id"])["owner"], "")
        self.assertIsNone(novel.get_world(w["id"], "999"))
        self.assertEqual(len(novel.list_worlds("999")), 0)


class ChainOptsTest(NovelBase):
    """服务层不认 QQ，只吃接口层给的链策略。"""

    def test_default_call_keeps_the_owner_fallback_chain(self):
        with mock.patch.object(novel.llm, "call_llm", return_value="x") as m:
            novel._call([{"role": "user", "content": "hi"}], 100, 0.5)
        kw = m.call_args.kwargs
        self.assertFalse(kw["strict"])
        self.assertEqual(kw["provider"], novel.PERSONA_PROVIDER)
        self.assertEqual(kw["model"], novel.PERSONA_MODEL)

    def test_free_relay_opts_only_hit_the_one_public_provider(self):
        opts = {"provider": "relay", "model": "v4", "strict": True}
        with mock.patch.object(novel.llm, "call_llm", return_value="x") as m:
            novel._call([{"role": "user", "content": "hi"}], 100, 0.5, opts)
        kw = m.call_args.kwargs
        self.assertEqual((kw["provider"], kw["model"], kw["strict"]),
                         ("relay", "v4", True))

    def test_paid_opts_start_from_the_global_chain(self):
        opts = {"provider": None, "model": None, "strict": False}
        with mock.patch.object(novel.llm, "call_llm", return_value="x") as m:
            novel._call([{"role": "user", "content": "hi"}], 100, 0.5, opts)
        kw = m.call_args.kwargs
        self.assertIsNone(kw["provider"])
        self.assertFalse(kw["strict"])

    def test_open_and_turn_pass_opts_to_both_llm_calls(self):
        opts = {"provider": "relay", "model": "v4", "strict": True}
        with mock.patch.object(novel, "_call",
                               side_effect=[_seed_text(), OPENING_TEXT]) as m:
            out = novel.open_world("zombie", "林岸", "医学生", opts=opts)
        self.assertEqual([c.args[3] for c in m.call_args_list], [opts, opts])
        ruling = json.dumps({"ruling": "他摸到晾衣杆", "facts": ["柜子里有晾衣杆"],
                             "items": ["晾衣杆"]}, ensure_ascii=False)
        with mock.patch.object(novel, "_call",
                               side_effect=[ruling, "他把晾衣杆握紧。"]) as m2:
            novel.run_turn(out["world_id"], "检查柜子", opts=opts)
        self.assertEqual([c.args[3] for c in m2.call_args_list], [opts, opts])


class TimeoutBudgetTest(NovelBase):
    """输出量不一样，读超时就不能共用一个值（2026-11-12 真机超时）。

    全局 `LLM_REQUEST_TIMEOUT` 是 60 秒，那是对话抢答的口径。开场一次要吐
    3000+ 字的世界书草稿，实测 22.5 秒是缓存命中时的运气，未命中就顶到
    60 秒线上。三段各给各的，谁也不许退回全局默认。
    """

    def test_three_stages_get_their_own_read_timeout(self):
        self.assertGreater(novel.OPEN_TIMEOUT, novel.WRITER_TIMEOUT)
        self.assertGreater(novel.WRITER_TIMEOUT, novel.DIRECTOR_TIMEOUT)
        self.assertGreater(novel.DIRECTOR_TIMEOUT, 60.0,
                           "三段都必须比全局 60 秒宽，否则等于没改")

    def test_call_without_timeout_leaves_the_global_default(self):
        with mock.patch.object(novel.llm, "call_llm", return_value="x") as m:
            novel._call([{"role": "user", "content": "hi"}], 100, 0.5)
        self.assertIsNone(m.call_args.kwargs["timeout"],
                          "不传就交给 llm 的全局默认，别在服务层写死一个")

    def test_call_passes_timeout_through(self):
        with mock.patch.object(novel.llm, "call_llm", return_value="x") as m:
            novel._call([{"role": "user", "content": "hi"}], 100, 0.5,
                        timeout=123.0)
        self.assertEqual(m.call_args.kwargs["timeout"], 123.0)

    def test_opening_and_its_body_use_their_own_timeouts(self):
        with mock.patch.object(novel, "_call",
                               side_effect=[_seed_text(), OPENING_TEXT]) as m:
            novel.open_world("zombie", "林岸", "医学生")
        self.assertEqual([c.kwargs["timeout"] for c in m.call_args_list],
                         [novel.OPEN_TIMEOUT, novel.WRITER_TIMEOUT])

    def test_turn_uses_director_then_writer_timeout(self):
        out, _ = self.open_a_world()
        ruling = json.dumps({"ruling": "他摸到晾衣杆", "facts": ["柜子里有晾衣杆"],
                             "items": ["晾衣杆"]}, ensure_ascii=False)
        with mock.patch.object(novel, "_call",
                               side_effect=[ruling, "他把晾衣杆握紧。"]) as m:
            novel.run_turn(out["world_id"], "检查柜子")
        self.assertEqual([c.kwargs["timeout"] for c in m.call_args_list],
                         [novel.DIRECTOR_TIMEOUT, novel.WRITER_TIMEOUT])

    def test_custom_open_uses_the_opening_timeout(self):
        with mock.patch.object(
                novel, "_call",
                side_effect=[json.dumps(CUSTOM_DRAFT, ensure_ascii=False)]) as m:
            out = novel.open_world(None, "阿七", "", table=CUSTOM_TABLE)
        self.assertTrue(out.get("need_identity"))
        self.assertEqual(m.call_args_list[0].kwargs["timeout"],
                         novel.OPEN_TIMEOUT)


class TurnTokenTest(NovelBase):
    def test_same_token_does_not_rerun_the_model_or_advance_a_turn(self):
        out, _ = self.open_a_world()
        wid = out["world_id"]
        ruling = json.dumps({"ruling": "他摸到晾衣杆", "facts": ["柜子里有晾衣杆"],
                             "items": ["晾衣杆"]}, ensure_ascii=False)
        with mock.patch.object(novel, "_call",
                               side_effect=[ruling, "他把晾衣杆握紧。"]) as m:
            a = novel.run_turn(wid, "检查柜子", turn_token="tok1")
            b = novel.run_turn(wid, "检查柜子", turn_token="tok1")
        self.assertEqual(a["reply"], b["reply"])
        self.assertEqual(m.call_count, 2, "同一 token 只能跑一次编剧 + 一次写作")
        self.assertEqual(novel.get_world(wid)["turns"], 1)

    def test_a_new_token_still_plays_a_new_turn(self):
        out, _ = self.open_a_world()
        wid = out["world_id"]
        ruling = json.dumps({"ruling": "", "facts": [], "items": []},
                            ensure_ascii=False)
        with mock.patch.object(novel, "_call",
                               side_effect=[ruling, "第一拍。", ruling, "第二拍。"]):
            novel.run_turn(wid, "检查柜子", turn_token="tok1")
            b = novel.run_turn(wid, "掀开床板", turn_token="tok2")
        self.assertEqual(b["reply"], "第二拍。")
        self.assertEqual(novel.get_world(wid)["turns"], 2)


class FriendBillingTest(NovelBase):
    def setUp(self):
        super().setUp()
        app = flask.Flask(__name__)
        app.register_blueprint(novel_api.bp)
        self.app = app
        self.c = app.test_client()

    def _as_friend(self, qq="123456"):
        p = mock.patch.object(novel_api.comic_quota, "friend_qq",
                              return_value=qq)
        p.start()
        self.addCleanup(p.stop)
        return qq

    def test_station_owner_does_not_go_through_billing(self):
        with mock.patch.object(novel_api.comic_quota, "consume") as consume:
            self.assertIsNone(novel_api._friend_run("", lambda o: o))
        consume.assert_not_called()

    def test_free_relay_success_charges_nothing(self):
        with mock.patch.object(novel_api._cfg, "PERSONA_PROVIDER", "relay"), \
                mock.patch.object(novel_api.comic_quota, "consume") as consume:
            opts = novel_api._friend_run("123", lambda o: o)
        self.assertTrue(opts["strict"])
        self.assertEqual(opts["provider"], "relay")
        consume.assert_not_called()

    def test_relay_failure_consumes_one_quota_and_reruns_on_paid_chain(self):
        calls = []
        with self.app.test_request_context():
            with mock.patch.object(novel_api._cfg, "PERSONA_PROVIDER", "relay"), \
                    mock.patch.object(novel_api.comic_quota, "remaining",
                                      return_value=3), \
                    mock.patch.object(novel_api.comic_quota, "consume",
                                      return_value=True) as consume, \
                    mock.patch.object(novel_api.comic_quota, "refund") as refund:
                def run(o):
                    calls.append(o)
                    if len(calls) == 1:
                        raise RuntimeError("402")
                    return "ok"
                got = novel_api._friend_run("123", run)
        self.assertEqual(got, "ok")
        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[0]["strict"])
        self.assertFalse(calls[1]["strict"])
        self.assertIsNone(calls[1]["provider"])
        consume.assert_called_once_with("123", "script")
        refund.assert_not_called()

    def test_no_quota_asks_the_player_to_find_the_station_owner(self):
        with self.app.test_request_context():
            with mock.patch.object(novel_api._cfg, "PERSONA_PROVIDER", "relay"), \
                    mock.patch.object(novel_api.comic_quota, "remaining",
                                      return_value=0), \
                    mock.patch.object(novel_api.comic_quota, "consume") as consume:
                with self.assertRaisesRegex(ValueError, "找站长加次数"):
                    novel_api._friend_run(
                        "123", lambda o: (_ for _ in ()).throw(RuntimeError("402")))
        consume.assert_not_called()

    def test_paid_failure_refunds_the_quota(self):
        with self.app.test_request_context():
            with mock.patch.object(novel_api._cfg, "PERSONA_PROVIDER", "relay"), \
                    mock.patch.object(novel_api.comic_quota, "remaining",
                                      return_value=3), \
                    mock.patch.object(novel_api.comic_quota, "consume",
                                      return_value=True), \
                    mock.patch.object(novel_api.comic_quota, "refund") as refund:
                with self.assertRaises(RuntimeError):
                    novel_api._friend_run(
                        "123", lambda o: (_ for _ in ()).throw(RuntimeError("down")))
        refund.assert_called_once_with("123", "script")

    def test_no_homework_is_not_mistaken_for_a_relay_outage(self):
        with mock.patch.object(novel_api._cfg, "PERSONA_PROVIDER", "relay"), \
                mock.patch.object(novel_api.comic_quota, "consume") as consume:
            with self.assertRaises(novel.NoHomework):
                novel_api._friend_run("123", lambda o: (_ for _ in ()).throw(
                    novel.NoHomework("空返回")))
        consume.assert_not_called()

    def test_quota_route_reports_the_friend_ledger_and_owner_mode(self):
        d = self.c.get("/api/novel/quota").get_json()
        self.assertEqual(d["mode"], "owner")
        self.assertFalse(d["limited"])
        self._as_friend("123")
        with mock.patch.object(novel_api.comic_quota, "snapshot",
                               return_value={"left": {"script": 2}}) as snap:
            d = self.c.get("/api/novel/quota").get_json()
        self.assertEqual(d["mode"], "friend")
        self.assertEqual(d["remaining"], 2)
        self.assertIn("免费", d["text"])
        snap.assert_called_once_with("123")

    def test_worlds_route_only_lists_the_friends_world(self):
        self.open_a_world(name="站主那本")
        self._as_friend("123")
        with mock.patch.object(novel, "_call",
                               side_effect=[_seed_text(), OPENING_TEXT]):
            self.c.post("/api/novel/open", json={
                "play": "zombie", "name": "群友那本", "identity": "医学生"})
        rows = self.c.get("/api/novel/worlds").get_json()["worlds"]
        self.assertEqual([w["name"] for w in rows], ["群友那本"])

    def test_custom_test_is_closed_for_friends(self):
        self._as_friend("123")
        r = self.c.post("/api/novel/custom_test", json={
            "base_url": "https://x", "model": "m", "api_key": "k"})
        self.assertEqual(r.status_code, 403)
        self.assertIn("站主自用", r.get_json()["error"])

    def test_turn_route_passes_owner_and_token_through(self):
        self._as_friend("123")
        with mock.patch.object(novel_api._cfg, "PERSONA_PROVIDER", "relay"), \
                mock.patch.object(novel, "run_turn",
                                  return_value={"ok": True, "reply": "正文"}) as m:
            self.c.post("/api/novel/turn", json={
                "id": "nv_x", "text": "检查柜子", "turn_token": "tok1"})
        self.assertEqual(m.call_args.kwargs["owner"], "123")
        self.assertEqual(m.call_args.kwargs["turn_token"], "tok1")


class FriendGatewayTest(unittest.TestCase):
    def setUp(self):
        self.gw = _read(os.path.join(ROOT, "friend_app.py"))
        self.main = _read(MAIN)

    def test_page_and_api_routes_exist_and_require_login(self):
        self.assertIn('@app.route("/novel")', self.gw)
        self.assertIn("@app.route(\"/api/novel/<path:sub>\"", self.gw)
        self.assertIn("if not _logged_in():", self.gw)
        self.assertIn('BACKEND + "/novel"', self.gw)
        self.assertIn("_forward(\"/api/novel\", sub", self.gw)

    def test_novel_post_timeout_is_widened(self):
        self.assertIn("post_timeout=60", self.gw)
        self.assertIn("timeout=post_timeout", self.gw)
        self.assertIn("post_timeout=600", self.gw)
        # 群友最坏要串两整轮（免费层失败 → 扣次数走全局链重跑），
        # 网关若比后端窄，后端还在跑就被掐成 502，正文已落盘但用户看到报错。
        self.assertGreater(
            600, 2 * (novel.WRITER_TIMEOUT + novel.DIRECTOR_TIMEOUT),
            "网关超时必须宽于「编剧+写作」跑两遍")

    def test_tunnel_guard_does_not_whitelist_novel(self):
        """隧道错指 5174 时，小说接口必须继续 404，不能绕开 QQ 注入变站主态。

        只看 `_tunnel_guard` 那段函数体：`main.py` 另有 no-cache 路由合法地
        提到 `/api/novel/`，全文件扫会误报。
        """
        seg = re.search(r"def _tunnel_guard\(\):(.*?)(?=\n@app\.|\ndef |\n# )",
                        self.main, re.S).group(1)
        self.assertNotIn("/api/novel/", seg)
        self.assertIn('p.startswith("/api/friend/")', seg)

    def test_solo_qq_link_unlock_also_covers_novel(self):
        self.assertIn('"/novel.html"', self.gw)


class FriendUiTest(unittest.TestCase):
    def setUp(self):
        self.html = _read(PAGE)

    def test_turn_token_is_bound_to_the_same_text(self):
        self.assertIn("turn_token:token", self.html)
        self.assertIn("nvTurnText === text", self.html)
        self.assertIn("nvTurnToken = ''", self.html)

    def test_boot_asks_quota_before_anything_else(self):
        self.assertIn("await nvQuota();", self.html)
        self.assertIn("nvFriendMode = d.mode === 'friend'", self.html)
        self.assertIn("cb.style.display = nvFriendMode ? 'none' : ''", self.html)

    def test_friend_posts_do_not_carry_custom_models(self):
        self.assertIn("if(!nvFriendMode) body.custom_models = nvCM;", self.html)


class ExpansionContractTest(unittest.TestCase):
    """2026-11-12 真机：用户写「往高层走到 402 遇到苏然」，AI 直接写「退回 402
    门口把门插上」。根因是整条链把用户的话当成「一句待回复的意图」，编剧压成
    三条事实、写作照事实写结果，过程整段被砍。这里锁住「扩写」口径。"""

    def setUp(self):
        self.src = _read(os.path.join(ROOT, "app", "novel.py"))
        self.writer = novel._NOVEL_CONTRACT
        self.director = novel._DIRECTOR_PROMPT

    def test_contract_is_expansion_not_reply(self):
        self.assertIn("扩写，不是回应", self.writer)
        self.assertIn("骨架", self.writer)
        self.assertIn("他不是在跟你说话", self.writer)

    def test_contract_forbids_skipping_the_process(self):
        self.assertIn("跳到结果", self.writer)
        self.assertIn("中间每一段路", self.writer)
        self.assertIn("就是没写完", self.writer)

    def test_contract_forbids_replacing_the_players_plot(self):
        self.assertIn("不许替玩家做决定", self.writer)
        self.assertIn("不许把「去 402 找人」改成「回 402 门口防守」", self.writer)
        self.assertIn("剧情是他的，血肉是你的", self.writer)

    def test_contract_requires_continuing_from_the_last_sentence(self):
        self.assertIn("上一轮正文的最后一句", self.writer)
        self.assertIn("别重新开场", self.writer)
        self.assertIn("不要重演", self.writer)

    def test_director_must_keep_every_node_in_order(self):
        self.assertIn("玩家写的是剧情，不是一句话", self.director)
        self.assertIn("节点一个都", self.director)
        self.assertIn("按时间顺序", self.director)
        self.assertIn("不许否决他写的走向", self.director)
        self.assertIn("别自己加戏", self.director)

    def test_director_is_told_not_to_compress_into_results(self):
        self.assertIn("不要只写结果", self.director)
        self.assertIn("「他退回 402 防守」是结果", self.director)

    def test_director_registers_clock_and_consumption(self):
        self.assertIn("每一拍都要给 `clock`", self.director)
        self.assertIn("薯片可乐一份没少，那是错的", self.director)
        self.assertIn('"clock":""', self.director, "JSON 字段里要有 clock")

    def test_user_block_labels_the_plot_as_a_skeleton(self):
        self.assertIn("玩家这一轮写下的剧情", self.src)
        self.assertIn("节点一个不许漏", self.src)
        self.assertIn("不许跳过过程直接写结果", self.src)

    def test_facts_block_is_labelled_as_nodes_not_the_whole_turn(self):
        self.assertIn("这一拍必须写到的节点", self.src)


class StoryClockTest(unittest.TestCase):
    """跨天之后薯片可乐一份没少：世界书原来没有时间字段。"""

    def setUp(self):
        tmp = tempfile.mkdtemp(prefix="novel_clock_")
        self.addCleanup(shutil.rmtree, tmp, True)
        self.file = os.path.join(tmp, "novel_worlds.json")
        p = mock.patch.object(novel, "_FILE", self.file)
        p.start()
        self.addCleanup(p.stop)

    def test_clock_is_registered_and_rendered(self):
        w = novel._blank_world("zombie", "林岸", "体育生", "", {"start": {}})
        self.assertEqual(w["clock"], "")
        novel._apply(w, {"clock": "第 3 天 傍晚", "facts": ["在 402 门口停下"]},
                     "往高层走")
        self.assertEqual(w["clock"], "第 3 天 傍晚")
        self.assertIn("第 3 天 傍晚", novel._state_block(w))

    def test_old_world_without_clock_is_normalized(self):
        w = {"id": "nv_x", "player": {"items": []}, "history": []}
        novel._normalize_world(w)
        self.assertEqual(w["clock"], "")
        self.assertIn("时间还没记", novel._state_block(w))


class RollbackTest(NovelBase):
    """回退最近一轮，而且只给一次（用户 2026-11-12 要求）。

    「只能回退最近一条，用户无法回退两次，不然全乱完了」——所以回退点只有
    一个，回退完就清空；想再退必须先往下写一轮。
    """

    def _after_one_turn(self, wid):
        d = json.dumps({
            "clock": "第 1 天 傍晚",
            "ruling": "林岸在柜子里摸到一根晾衣杆",
            "facts": ["他打开柜门", "摸到一根铁晾衣杆"],
            "items": [{"name": "铁晾衣杆", "qty": ""}],
            "place": "男生宿舍 304",
        }, ensure_ascii=False)
        with mock.patch.object(novel, "_call", side_effect=[d, "他握住了晾衣杆。"]):
            out = novel.run_turn(wid, "检查柜子", turn_token="tk-1")
        return out

    def test_turn_leaves_exactly_one_undo_point(self):
        out, _ = self.open_a_world()
        w = novel.get_world(out["world_id"])
        self.assertIsNone(w.get("undo"))
        self.assertFalse(out["state"]["can_undo"])
        self._after_one_turn(out["world_id"])
        w = novel.get_world(out["world_id"])
        self.assertIsInstance(w.get("undo"), dict)
        self.assertEqual(w["undo"]["hist_added"], 2, "一轮追加 user + assistant 两行")
        self.assertGreaterEqual(w["undo"]["log_added"], 1)
        self.assertIn("player", w["undo"]["state"])
        self.assertTrue(novel.state_card(w)["can_undo"])

    def test_rollback_restores_state_and_truncates_history(self):
        out, _ = self.open_a_world()
        wid = out["world_id"]
        before = novel.get_world(wid)
        h0, l0 = len(before["history"]), len(before["log"])
        p0 = dict(before["player"])
        self._after_one_turn(wid)
        r = novel.rollback(wid)
        self.assertTrue(r["ok"])
        self.assertEqual(r["said"], "检查柜子", "回退要把玩家那段放回输入框")
        w = novel.get_world(wid)
        self.assertEqual(len(w["history"]), h0)
        self.assertEqual(len(w["log"]), l0)
        self.assertEqual(w["player"]["items"], p0["items"])
        self.assertEqual(w["player"]["place"], p0["place"])
        self.assertEqual(w["turns"], before["turns"])
        self.assertIsNone(w["undo"])
        self.assertFalse(novel.state_card(w)["can_undo"])

    def test_cannot_rollback_twice(self):
        out, _ = self.open_a_world()
        self._after_one_turn(out["world_id"])
        novel.rollback(out["world_id"])
        with self.assertRaises(ValueError) as ctx:
            novel.rollback(out["world_id"])
        self.assertIn("已经回退过", str(ctx.exception))

    def test_new_turn_reopens_the_undo_point(self):
        out, _ = self.open_a_world()
        wid = out["world_id"]
        self._after_one_turn(wid)
        novel.rollback(wid)
        self._after_one_turn(wid)
        self.assertTrue(novel.state_card(novel.get_world(wid))["can_undo"])

    def test_rollback_drops_the_idempotency_token(self):
        """网关超时后重发同一个 token，不能把刚回退掉的那轮结果吐回来。"""
        out, _ = self.open_a_world()
        wid = out["world_id"]
        self._after_one_turn(wid)
        self.assertEqual(novel.get_world(wid).get("last_turn_token"), "tk-1")
        novel.rollback(wid)
        w = novel.get_world(wid)
        self.assertNotIn("last_turn_token", w)
        self.assertNotIn("last_turn_result", w)

    def test_rollback_forgets_that_round_in_rag(self):
        out, _ = self.open_a_world()
        wid = out["world_id"]
        self._after_one_turn(wid)
        with mock.patch.object(persona_rag, "forget_last") as forget:
            novel.rollback(wid)
        forget.assert_called_once_with("%s#0" % wid)

    def test_rollback_respects_owner(self):
        out, _ = self.open_a_world()
        self._after_one_turn(out["world_id"])
        with self.assertRaises(ValueError):
            novel.rollback(out["world_id"], owner="123456")
        self.assertTrue(novel.state_card(
            novel.get_world(out["world_id"]))["can_undo"],
            "别人的本子不能被回退，也不能因此丢掉回退点")

    def test_failed_turn_leaves_no_undo_point(self):
        out, _ = self.open_a_world()
        with mock.patch.object(novel, "_call",
                               side_effect=["{}", ""]):
            with self.assertRaises(novel.NoHomework):
                novel.run_turn(out["world_id"], "检查柜子")
        self.assertIsNone(novel.get_world(out["world_id"]).get("undo"))

    def test_state_card_exposes_can_undo_only(self):
        out, _ = self.open_a_world()
        self.assertIn("can_undo", out["state"])
        self.assertNotIn("undo", out["state"],
                         "整份快照不进前端，只给一个布尔")

    def test_rollback_keeps_a_later_rename(self):
        """名字是玩家随时改的偏好，不该跟着回退缩回去。"""
        out, _ = self.open_a_world()
        wid = out["world_id"]
        self._after_one_turn(wid)
        novel.rename(wid, "阿岸")
        novel.rollback(wid)
        self.assertEqual(novel.get_world(wid)["player"]["name"], "阿岸")

    def test_api_rollback_route_is_wired(self):
        api = _read(os.path.join(ROOT, "app", "novel_api.py"))
        self.assertIn('@bp.route("/api/novel/rollback", methods=["POST"])', api)
        self.assertIn("novel.rollback(", api)
        html = _read(PAGE)
        self.assertIn('id="nvUndoBtn"', html)
        self.assertIn("function nvUndo()", html)
        self.assertIn("nvCanUndo = !!s.can_undo", html)


class PresenceAndEventsTest(NovelBase):
    """走开的人和事不该继续挂在眼前（用户 2026-11-12 真机反馈）。

    原话：「在场的人里还是第四只丧尸」「我都已经离开男生宿舍了，怎么可能
    还是未完成的事情」。present 原来全靠编剧自己在 cast_update 里报，它漏
    一次，走掉的丧尸就永远挂在「在场的人」；events 的 done 也一样，当拍
    已经兑现的事（砍断丧尸、声音传开）被挂成未了结，主角换了三个地图它还挂着。
    """

    def _turn(self, wid, payload, text="往前赶"):
        d = json.dumps(payload, ensure_ascii=False)
        with mock.patch.object(novel, "_call",
                               side_effect=[d, "他往前赶。"]) as m:
            return novel.run_turn(wid, text), m

    def test_leaving_a_place_clears_people_who_stayed(self):
        out, _ = self.open_a_world()
        wid = out["world_id"]
        self._turn(wid, {"place": "男生宿舍 304",
                         "cast_new": [{"name": "第四只丧尸",
                                       "identity": "楼道里多出来的",
                                       "present": True}],
                         "items": []}, "看看楼道")
        self.assertEqual([c["name"] for c in
                          novel.state_card(novel.get_world(wid))["present"]],
                         ["第四只丧尸"])
        # 换到别处、没点名它 —— 它留在原地，不该还在场
        self._turn(wid, {"place": "女生宿舍二楼 空宿舍", "items": []},
                   "翻窗进女生宿舍")
        w = novel.get_world(wid)
        self.assertEqual(novel.state_card(w)["present"], [])
        self.assertIn("第四只丧尸", [c["name"] for c in w["cast"]],
                      "人被记为分开，不是从名录里删掉")
        self.assertFalse([c for c in w["cast"] if c["name"] == "第四只丧尸"][0]["present"])

    def test_person_who_comes_along_is_kept_present(self):
        out, _ = self.open_a_world()
        wid = out["world_id"]
        self._turn(wid, {"place": "男生宿舍 304",
                         "cast_new": [{"name": "周砚", "identity": "护士",
                                       "present": True}],
                         "items": []}, "救人")
        self._turn(wid, {"place": "楼梯间",
                         "cast_update": [{"name": "周砚", "present": True}],
                         "items": []}, "带她一起下楼")
        self.assertEqual([c["name"] for c in
                          novel.state_card(novel.get_world(wid))["present"]],
                         ["周砚"], "编剧点名跟着走的，不能被换地方清掉")

    def test_staying_in_the_same_place_changes_nothing(self):
        out, _ = self.open_a_world()
        wid = out["world_id"]
        self._turn(wid, {"place": "男生宿舍 304",
                         "cast_new": [{"name": "第四只丧尸", "present": True}],
                         "items": []}, "看看楼道")
        self._turn(wid, {"place": "男生宿舍 304", "items": []}, "在屋里翻柜子")
        self.assertEqual([c["name"] for c in
                          novel.state_card(novel.get_world(wid))["present"]],
                         ["第四只丧尸"], "没换地方就还在场")

    def test_event_with_result_is_closed_by_default(self):
        out, _ = self.open_a_world()
        wid = out["world_id"]
        self._turn(wid, {"place": "男生宿舍 304",
                         "events_add": [{"trigger": "老A砍断丧尸的动静传开",
                                         "result": "剩下三只被引向楼梯口",
                                         "kind": "dynamic"}],
                         "items": []}, "砍丧尸")
        e = novel.get_world(wid)["events"][-1]
        self.assertTrue(e["done"], "当场兑现的事不许挂成未了结")
        self.assertEqual(e["place"], "男生宿舍 304", "事件要记发生在哪")

    def test_event_without_result_stays_open(self):
        out, _ = self.open_a_world()
        wid = out["world_id"]
        self._turn(wid, {"place": "男生宿舍 304",
                         "events_add": [{"trigger": "楼梯口的尸群随时会摸上来",
                                         "kind": "dynamic"}],
                         "items": []}, "听动静")
        self.assertFalse(novel.get_world(wid)["events"][-1]["done"],
                         "没写结果的才是伏笔")

    def test_explicit_done_false_is_respected(self):
        out, _ = self.open_a_world()
        wid = out["world_id"]
        self._turn(wid, {"place": "男生宿舍 304",
                         "events_add": [{"trigger": "门外有东西在挠门",
                                         "result": "暂时没进来",
                                         "done": False, "kind": "dynamic"}],
                         "items": []}, "听门")
        self.assertFalse(novel.get_world(wid)["events"][-1]["done"],
                         "编剧明确说没完，代码不许替它改")

    def test_old_place_event_leaves_the_card_but_stays_in_the_world(self):
        out, _ = self.open_a_world()
        wid = out["world_id"]
        self._turn(wid, {"place": "男生宿舍 304",
                         "events_add": [{"trigger": "楼道的动静还没完",
                                         "kind": "dynamic"}],
                         "items": []}, "听动静")
        self.assertEqual(len(novel.state_card(novel.get_world(wid))["events"]), 1)
        self._turn(wid, {"place": "女生宿舍二楼 空宿舍", "items": []}, "翻窗过去")
        w = novel.get_world(wid)
        self.assertEqual(novel.state_card(w)["events"], [],
                         "挂在已经离开的地方的事，不该还摆在眼前")
        self.assertEqual(len(w["events"]), 1, "只是不显示，不是删数据")

    def test_event_without_place_still_shows(self):
        out, _ = self.open_a_world()
        wid = out["world_id"]
        w = novel.get_world(wid)
        w["events"].append({"trigger": "老存档里没有地点的一条", "result": "",
                            "done": False, "kind": "dynamic", "chapter": 1})
        novel._save_world(w)
        self.assertEqual(len(novel.state_card(novel.get_world(wid))["events"]), 1,
                         "老存档没记地点的事件不能凭空藏掉")

    def test_state_block_tells_the_director_the_event_is_behind_him(self):
        out, _ = self.open_a_world()
        w = novel.get_world(out["world_id"])
        w["player"]["place"] = "女生宿舍二楼 空宿舍"
        w["events"] = [{"trigger": "楼道的动静还没完", "result": "",
                        "done": False, "kind": "dynamic", "chapter": 1,
                        "place": "男生宿舍 304"}]
        block = novel._state_block(w)
        self.assertIn("楼道的动静还没完", block)
        self.assertIn("你已经离开那儿了", block,
                      "给编剧看全部，但要标清它挂在旧地方")


    def test_threats_are_not_listed_as_people(self):
        out, _ = self.open_a_world()
        wid = out["world_id"]
        self._turn(wid, {"place": "男生宿舍 304",
                         "cast_new": [{"name": "第四只丧尸", "kind": "威胁",
                                       "identity": "楼道里多出来的"},
                                      {"name": "周砚", "kind": "人",
                                       "identity": "护士"}],
                         "items": []}, "看看楼道")
        # 换到别处、两个都没点名 → 都记为不在场，才能看名录
        self._turn(wid, {"place": "女生宿舍二楼 空宿舍", "items": []}, "翻窗过去")
        w = novel.get_world(wid)
        self.assertEqual({c["name"]: c["kind"] for c in w["cast"]},
                         {"第四只丧尸": "威胁", "周砚": "人"})
        block = novel._state_block(w)
        people = [x for x in block.split("\n") if x.startswith("遇到过的人")][0]
        self.assertIn("周砚", people)
        self.assertNotIn("第四只丧尸", people,
                         "丧尸不是「遇到过的人」")
        threat = [x for x in block.split("\n")
                  if x.startswith("打过交道的威胁")][0]
        self.assertIn("第四只丧尸", threat)
        self.assertNotIn("周砚", threat)

    def test_cast_row_without_kind_counts_as_a_person(self):
        out, _ = self.open_a_world()
        w = novel.get_world(out["world_id"])
        w["cast"].append({"name": "老存档里的路人", "identity": "",
                          "relation": "", "willing": "", "tier": "路人甲",
                          "present": False})
        block = novel._state_block(w)
        people = [x for x in block.split("\n") if x.startswith("遇到过的人")][0]
        self.assertIn("老存档里的路人", people, "没标 kind 的一律按人算")

    def test_page_has_a_separate_threat_row(self):
        p = _read(os.path.join(ROOT, "web", "novel.html"))
        self.assertIn("c.kind !== '威胁'", p)
        self.assertIn("打过交道的威胁", p)


if __name__ == "__main__":
    unittest.main()
