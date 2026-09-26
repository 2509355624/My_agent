# -*- coding: utf-8 -*-
"""会话管理测试：列出 / 删除某条会话线。

对应 app/agents.py 的 list_sessions / delete_session，以及 app/main.py 里
挂在管理页上的两个路由。重点在四处：
1. 统计口径——首行是 system 头，它不是「聊过的内容」，不能算进消息数；
2. 路径安全——会话 key 直接来自 URL，必须挡住 ../ 之类的穿越；
3. 降级——群名要问 QQ 协议端，问不到也只能影响显示，不能影响删除；
4. 权限——删除是不可逆的写操作，默认只允许本机。
"""

import json
import os
import tempfile
import unittest
from unittest import mock

import app.agents as agents
import app.config as config
import app.main as main
import app.qq_api as qq_api


class SessionsTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.join(self.tmp.name, "agents")
        os.makedirs(self.root, exist_ok=True)
        p = mock.patch.object(agents, "AGENTS_DIR", self.root)
        p.start()
        self.addCleanup(p.stop)
        agents.clear_cache()
        self.addCleanup(agents.clear_cache)
        self.client = main.app.test_client()

    def session_path(self, aid, key=None):
        d = os.path.join(self.root, aid)
        if key is None:
            return os.path.join(d, "session.jsonl")
        return os.path.join(d, "sessions", key + ".jsonl")

    def write_session(self, aid, key=None, lines=("u1", "a1"), raw=None):
        """写一条会话线：首行 system 头 + lines 里每条一行。"""
        path = self.session_path(aid, key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            if raw is not None:
                f.write(raw)
                return path
            f.write(json.dumps({"role": "system", "content": "S"},
                               ensure_ascii=False) + "\n")
            for x in lines:
                f.write(json.dumps({"role": "user", "content": x},
                                   ensure_ascii=False) + "\n")
        return path

    def by_key(self, items):
        return {i["key"]: i for i in items}


class ListSessionsTest(SessionsTestBase):
    def test_lists_main_and_every_sub_session(self):
        self.write_session("qq", key=None)
        self.write_session("qq", key="group_111")
        self.write_session("qq", key="private_222")
        keys = {i["key"] for i in agents.list_sessions("qq")}
        self.assertEqual(keys, {"main", "group_111", "private_222"})

    def test_kind_and_target_id_parsed_from_key(self):
        self.write_session("qq", key="group_111")
        self.write_session("qq", key="private_222")
        items = self.by_key(agents.list_sessions("qq"))
        self.assertEqual(items["group_111"]["kind"], "group")
        self.assertEqual(items["group_111"]["target_id"], "111")
        self.assertEqual(items["private_222"]["kind"], "private")
        self.assertEqual(items["private_222"]["target_id"], "222")

    def test_system_header_not_counted_as_message(self):
        """首行是 system 头，两条会话内容 = 2 条消息，不是 3 条。"""
        self.write_session("qq", key="group_111", lines=("u1", "a1"))
        items = self.by_key(agents.list_sessions("qq"))
        self.assertEqual(items["group_111"]["messages"], 2)

    def test_empty_file_reports_zero_not_negative(self):
        self.write_session("qq", key="group_111", raw="")
        items = self.by_key(agents.list_sessions("qq"))
        self.assertEqual(items["group_111"]["messages"], 0)

    def test_blank_lines_ignored(self):
        self.write_session("qq", key="group_111",
                           raw='\n{"role": "system", "content": "S"}\n\n\n')
        items = self.by_key(agents.list_sessions("qq"))
        self.assertEqual(items["group_111"]["messages"], 0)

    def test_skips_non_jsonl_and_illegal_keys(self):
        """sessions/ 里的杂物不能出现在列表里——尤其点开头的东西。"""
        self.write_session("qq", key="group_111")
        d = os.path.join(self.root, "qq", "sessions")
        for name in ("readme.txt", ".hidden.jsonl", "bad.key.jsonl", "x.tmp"):
            with open(os.path.join(d, name), "w", encoding="utf-8") as f:
                f.write("{}")
        keys = {i["key"] for i in agents.list_sessions("qq")}
        self.assertEqual(keys, {"group_111"})

    def test_newest_activity_first(self):
        old = self.write_session("qq", key="group_111")
        new = self.write_session("qq", key="group_222")
        os.utime(old, (1000, 1000))
        os.utime(new, (2000, 2000))
        order = [i["key"] for i in agents.list_sessions("qq")]
        self.assertEqual(order, ["group_222", "group_111"])

    def test_agent_without_sessions_dir(self):
        os.makedirs(os.path.join(self.root, "qq"), exist_ok=True)
        self.assertEqual(agents.list_sessions("qq"), [])

    def test_illegal_agent_id_returns_empty(self):
        self.assertEqual(agents.list_sessions("../etc"), [])
        self.assertEqual(agents.list_sessions(None), [])

    def test_main_session_named_for_display(self):
        self.write_session("qq", key=None)
        items = self.by_key(agents.list_sessions("qq"))
        self.assertTrue(items["main"]["name"])
        self.assertEqual(items["main"]["target_id"], "")


class DeleteSessionTest(SessionsTestBase):
    def test_deletes_sub_session(self):
        path = self.write_session("qq", key="group_111")
        ok, msg = agents.delete_session("qq", "group_111")
        self.assertTrue(ok, msg)
        self.assertFalse(os.path.exists(path))

    def test_deletes_main_session(self):
        path = self.write_session("qq", key=None)
        ok, msg = agents.delete_session("qq", "main")
        self.assertTrue(ok, msg)
        self.assertFalse(os.path.exists(path))

    def test_leaves_other_sessions_alone(self):
        keep = self.write_session("qq", key="group_222")
        self.write_session("qq", key="group_111")
        agents.delete_session("qq", "group_111")
        self.assertTrue(os.path.exists(keep))

    def test_rejects_traversal_key(self):
        """key 来自 URL，挡不住 ../ 就等于允许删任意文件。"""
        self.write_session("qq", key=None)
        for bad in ("../../../secret", "..", "a/b", "a\\b", ".hidden", ""):
            ok, msg = agents.delete_session("qq", bad)
            self.assertFalse(ok, "不该接受 key=%r" % bad)
        self.assertTrue(os.path.exists(self.session_path("qq")))

    def test_missing_key_reports_not_found(self):
        self.write_session("qq", key=None)
        ok, msg = agents.delete_session("qq", "group_999")
        self.assertFalse(ok)
        self.assertEqual(msg, "会话不存在")

    def test_illegal_agent_id(self):
        ok, msg = agents.delete_session("../etc", "group_1")
        self.assertFalse(ok)

    def test_main_key_never_targets_sub_file(self):
        """sessions/main.jsonl 不能被 "main" 这个保留名删掉。"""
        sub = self.write_session("qq", key="main")
        main_path = self.write_session("qq", key=None)
        ok, _ = agents.delete_session("qq", "main")
        self.assertTrue(ok)
        self.assertFalse(os.path.exists(main_path))
        self.assertTrue(os.path.exists(sub))


class SessionApiTest(SessionsTestBase):
    def test_get_returns_sessions(self):
        self.write_session("qq", key="group_111")
        d = self.client.get("/api/agent/qq/sessions").get_json()
        self.assertEqual(d["agent"], "qq")
        self.assertEqual(len(d["sessions"]), 1)
        self.assertEqual(d["sessions"][0]["key"], "group_111")

    def test_get_decorates_names_from_protocol_side(self):
        self.write_session("qq", key="group_111")
        self.write_session("qq", key="private_222")
        groups = [{"group_id": 111, "group_name": "摸鱼群"}]
        friends = [{"user_id": 222, "nickname": "小明", "remark": "阿明"}]
        with mock.patch.object(qq_api, "get_group_list", lambda *a, **k: groups), \
             mock.patch.object(qq_api, "get_friend_list", lambda *a, **k: friends):
            d = self.client.get("/api/agent/qq/sessions").get_json()
        self.assertTrue(d["names_ok"])
        names = {s["key"]: s["name"] for s in d["sessions"]}
        self.assertEqual(names["group_111"], "摸鱼群（111）")
        self.assertEqual(names["private_222"], "阿明（222）")

    def test_get_falls_back_when_protocol_side_down(self):
        """NapCat 没开时列表照给，只是名字退回号——不能整个接口失败。"""
        self.write_session("qq", key="group_111")

        def boom(*a, **k):
            raise RuntimeError("connection refused")

        with mock.patch.object(qq_api, "get_group_list", boom):
            resp = self.client.get("/api/agent/qq/sessions")
        self.assertEqual(resp.status_code, 200)
        d = resp.get_json()
        self.assertFalse(d["names_ok"])
        self.assertEqual(d["sessions"][0]["name"], "群（111）")

    def test_get_skips_network_when_only_main_session(self):
        """只有主会话（不接 QQ 的 agent）时不该去问协议端。"""
        self.write_session("main", key=None)
        with mock.patch.object(qq_api, "get_group_list",
                               side_effect=AssertionError("不该被调用")):
            d = self.client.get("/api/agent/main/sessions").get_json()
        self.assertTrue(d["names_ok"])
        self.assertEqual(len(d["sessions"]), 1)

    def test_bad_agent_id_rejected(self):
        self.assertEqual(self.client.get("/api/agent/bad..id/sessions").status_code, 400)

    def test_delete_removes_file(self):
        path = self.write_session("qq", key="group_111")
        resp = self.client.delete("/api/agent/qq/sessions/group_111")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()["ok"])
        self.assertFalse(os.path.exists(path))

    def test_delete_main_session(self):
        path = self.write_session("qq", key=None)
        self.assertEqual(
            self.client.delete("/api/agent/qq/sessions/main").status_code, 200)
        self.assertFalse(os.path.exists(path))

    def test_delete_unknown_key_returns_404(self):
        self.write_session("qq", key=None)
        resp = self.client.delete("/api/agent/qq/sessions/group_999")
        self.assertEqual(resp.status_code, 404)

    def test_delete_bad_key_returns_400(self):
        resp = self.client.delete("/api/agent/qq/sessions/bad.key")
        self.assertEqual(resp.status_code, 400)

    def test_delete_remote_blocked_by_default(self):
        self.write_session("qq", key="group_111")
        resp = self.client.delete("/api/agent/qq/sessions/group_111",
                                  environ_base={"REMOTE_ADDR": "8.8.8.8"})
        self.assertEqual(resp.status_code, 403)
        self.assertTrue(os.path.exists(self.session_path("qq", "group_111")))

    def test_delete_remote_allowed_when_opted_in(self):
        self.write_session("qq", key="group_111")
        with mock.patch.object(main, "ADMIN_ALLOW_REMOTE", True):
            resp = self.client.delete("/api/agent/qq/sessions/group_111",
                                      environ_base={"REMOTE_ADDR": "8.8.8.8"})
        self.assertEqual(resp.status_code, 200)


class SettingsStoreTest(SessionsTestBase):
    """settings.json：agent 级运行时开关的存取。"""

    def test_missing_file_means_empty_settings(self):
        self.assertEqual(agents.load_settings("qq"), {})

    def test_save_then_load_roundtrip(self):
        self.assertTrue(agents.save_settings("qq", {"interject_muted": ["111"]}))
        self.assertEqual(agents.load_settings("qq"),
                         {"interject_muted": ["111"]})

    def test_save_invalidates_cache(self):
        agents.save_settings("qq", {"a": 1})
        agents.save_settings("qq", {"a": 2})
        self.assertEqual(agents.load_settings("qq"), {"a": 2})

    def test_corrupt_file_degrades_to_empty(self):
        os.makedirs(os.path.join(self.root, "qq"), exist_ok=True)
        with open(os.path.join(self.root, "qq", "settings.json"), "w",
                  encoding="utf-8") as f:
            f.write("{broken")
        self.assertEqual(agents.load_settings("qq"), {})

    def test_non_dict_file_degrades_to_empty(self):
        os.makedirs(os.path.join(self.root, "qq"), exist_ok=True)
        with open(os.path.join(self.root, "qq", "settings.json"), "w",
                  encoding="utf-8") as f:
            f.write("[1, 2]")
        self.assertEqual(agents.load_settings("qq"), {})

    def test_illegal_agent_id_rejected(self):
        self.assertIsNone(agents.settings_path("../etc"))
        self.assertFalse(agents.save_settings("../etc", {"a": 1}))
        self.assertEqual(agents.load_settings("../etc"), {})

    def test_rejects_non_dict_payload(self):
        self.assertFalse(agents.save_settings("qq", ["not", "a", "dict"]))


class InterjectToggleApiTest(SessionsTestBase):
    """群聊「主动发言」开关：管理页切换 → settings.json → 热生效。"""

    def test_sessions_carry_interject_state_default_on(self):
        self.write_session("qq", key="group_111")
        d = self.client.get("/api/agent/qq/sessions").get_json()
        self.assertTrue(d["sessions"][0]["interject"])

    def test_put_mute_persists_and_reflects(self):
        self.write_session("qq", key="group_111")
        resp = self.client.put("/api/agent/qq/interject/111",
                               json={"enabled": False})
        self.assertEqual(resp.status_code, 200)
        d = self.client.get("/api/agent/qq/sessions").get_json()
        self.assertFalse(d["sessions"][0]["interject"])
        self.assertEqual(agents.load_settings("qq")["interject_muted"],
                         ["111"])

    def test_put_unmute_removes_from_list(self):
        agents.save_settings("qq", {"interject_muted": ["111"]})
        self.client.put("/api/agent/qq/interject/111", json={"enabled": True})
        self.assertEqual(agents.load_settings("qq")["interject_muted"], [])

    def test_put_requires_enabled_bool(self):
        self.write_session("qq", key="group_111")
        for bad in ({}, {"enabled": "yes"}, {"enabled": 1}):
            resp = self.client.put("/api/agent/qq/interject/111", json=bad)
            self.assertEqual(resp.status_code, 400)

    def test_put_remote_blocked_by_default(self):
        resp = self.client.put("/api/agent/qq/interject/111",
                               json={"enabled": False},
                               environ_base={"REMOTE_ADDR": "8.8.8.8"})
        self.assertEqual(resp.status_code, 403)

    def test_put_remote_allowed_when_opted_in(self):
        with mock.patch.object(main, "ADMIN_ALLOW_REMOTE", True):
            resp = self.client.put("/api/agent/qq/interject/111",
                                   json={"enabled": False},
                                   environ_base={"REMOTE_ADDR": "8.8.8.8"})
        self.assertEqual(resp.status_code, 200)


class CooldownApiTest(SessionsTestBase):
    """主动发言频率：全局秒数 + 单群覆盖，settings.json 层热生效。"""

    def test_sessions_carry_effective_global(self):
        self.write_session("qq", key="group_111")
        agents.save_settings("qq", {"interject_cooldown": 300})
        d = self.client.get("/api/agent/qq/sessions").get_json()
        self.assertEqual(d["interject_cooldown"], 300)
        self.assertIsNone(d["sessions"][0]["cooldown_override"])

    def test_put_global_persists(self):
        resp = self.client.put("/api/agent/qq/interject_cooldown",
                               json={"cooldown": 120})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(agents.load_settings("qq")["interject_cooldown"], 120)
        # 立即反映到 GET
        d = self.client.get("/api/agent/qq/sessions").get_json()
        self.assertEqual(d["interject_cooldown"], 120)

    def test_put_global_zero_means_unlimited(self):
        self.client.put("/api/agent/qq/interject_cooldown",
                        json={"cooldown": 0})
        self.assertEqual(agents.load_settings("qq")["interject_cooldown"], 0)

    def test_put_global_rejects_bad_values(self):
        for bad in ({}, {"cooldown": "120"}, {"cooldown": True},
                    {"cooldown": -1}, {"cooldown": 3601}, {"cooldown": None}):
            resp = self.client.put("/api/agent/qq/interject_cooldown", json=bad)
            self.assertEqual(resp.status_code, 400, bad)

    def test_put_group_override_and_clear(self):
        self.write_session("qq", key="group_111")
        resp = self.client.put("/api/agent/qq/interject_cooldown/111",
                               json={"cooldown": 600})
        self.assertEqual(resp.status_code, 200)
        s = agents.load_settings("qq")["interject_cooldown_overrides"]
        self.assertEqual(s["111"], 600)
        d = self.client.get("/api/agent/qq/sessions").get_json()
        self.assertEqual(d["sessions"][0]["cooldown_override"], 600)
        # null = 删覆盖，回落全局
        self.client.put("/api/agent/qq/interject_cooldown/111",
                        json={"cooldown": None})
        self.assertEqual(
            agents.load_settings("qq")["interject_cooldown_overrides"], {})
        d = self.client.get("/api/agent/qq/sessions").get_json()
        self.assertIsNone(d["sessions"][0]["cooldown_override"])

    def test_put_group_rejects_bad_values(self):
        for bad in ({}, {"cooldown": "x"}, {"cooldown": 4000}):
            resp = self.client.put("/api/agent/qq/interject_cooldown/111",
                                   json=bad)
            self.assertEqual(resp.status_code, 400, bad)

    def test_put_remote_blocked_by_default(self):
        resp = self.client.put("/api/agent/qq/interject_cooldown",
                               json={"cooldown": 60},
                               environ_base={"REMOTE_ADDR": "8.8.8.8"})
        self.assertEqual(resp.status_code, 403)


class CooldownResolveTest(SessionsTestBase):
    """agents.interject_cooldown 的三层取值与收敛。"""

    def test_falls_back_to_env_default(self):
        self.assertEqual(agents.interject_cooldown("qq", "111"),
                         config.QQ_INTERJECT_COOLDOWN)

    def test_global_then_override(self):
        agents.save_settings("qq", {"interject_cooldown": 100,
                                    "interject_cooldown_overrides":
                                        {"111": 5, "222": 99999}})
        self.assertEqual(agents.interject_cooldown("qq", "333"), 100)
        self.assertEqual(agents.interject_cooldown("qq", "111"), 5)
        # 越界值收敛到上限
        self.assertEqual(agents.interject_cooldown("qq", "222"), 3600)

    def test_bad_types_degrade_to_env(self):
        agents.save_settings("qq", {"interject_cooldown": "abc",
                                    "interject_cooldown_overrides":
                                        {"111": "x"}})
        self.assertEqual(agents.interject_cooldown("qq", "111"),
                         config.QQ_INTERJECT_COOLDOWN)
        self.assertEqual(agents.interject_cooldown("qq", "222"),
                         config.QQ_INTERJECT_COOLDOWN)

    def test_zero_in_settings_means_unlimited(self):
        agents.save_settings("qq", {"interject_cooldown": 0})
        self.assertEqual(agents.interject_cooldown("qq", "111"), 0)

    def test_private_sessions_carry_no_interject_field(self):
        self.write_session("qq", key="private_222")
        d = self.client.get("/api/agent/qq/sessions").get_json()
        self.assertNotIn("interject", d["sessions"][0])


class ChanceApiTest(SessionsTestBase):
    """触发概率：全局百分比 + 单群覆盖，settings.json 层热生效。"""

    def test_default_is_twelve_percent(self):
        self.assertEqual(agents.interject_chance("qq", "111"),
                         agents.DEFAULT_INTERJECT_CHANCE)
        self.assertEqual(agents.DEFAULT_INTERJECT_CHANCE, 12)

    def test_global_then_override(self):
        agents.save_settings("qq", {"interject_chance": 30,
                                    "interject_chance_overrides": {"111": 0}})
        self.assertEqual(agents.interject_chance("qq", "333"), 30)
        self.assertEqual(agents.interject_chance("qq", "111"), 0)

    def test_out_of_range_is_clamped(self):
        agents.save_settings("qq", {"interject_chance": 500})
        self.assertEqual(agents.interject_chance("qq", "111"), 100)
        agents.save_settings("qq", {"interject_chance": -5})
        self.assertEqual(agents.interject_chance("qq", "111"), 0)

    def test_bad_types_fall_back_to_default(self):
        agents.save_settings("qq", {"interject_chance": "abc",
                                    "interject_chance_overrides":
                                        {"111": True}})
        self.assertEqual(agents.interject_chance("qq", "111"),
                         agents.DEFAULT_INTERJECT_CHANCE)

    def test_sessions_carry_global_and_override(self):
        self.write_session("qq", key="group_111")
        agents.save_settings("qq", {"interject_chance": 25,
                                    "interject_chance_overrides": {"111": 50}})
        d = self.client.get("/api/agent/qq/sessions").get_json()
        self.assertEqual(d["interject_chance"], 25)
        self.assertEqual(d["sessions"][0]["chance_override"], 50)

    def test_put_global_persists(self):
        resp = self.client.put("/api/agent/qq/interject_chance",
                               json={"chance": 8})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(agents.load_settings("qq")["interject_chance"], 8)

    def test_put_global_rejects_bad_values(self):
        for bad in ({}, {"chance": "8"}, {"chance": -1}, {"chance": 101},
                    {"chance": None}, {"chance": True}):
            resp = self.client.put("/api/agent/qq/interject_chance", json=bad)
            self.assertEqual(resp.status_code, 400, bad)

    def test_put_group_override_and_clear(self):
        self.client.put("/api/agent/qq/interject_chance/111",
                        json={"chance": 100})
        s = agents.load_settings("qq")["interject_chance_overrides"]
        self.assertEqual(s["111"], 100)
        # null = 删覆盖，回落全局
        self.client.put("/api/agent/qq/interject_chance/111",
                        json={"chance": None})
        self.assertEqual(
            agents.load_settings("qq")["interject_chance_overrides"], {})

    def test_put_group_rejects_bad_values(self):
        for bad in ({}, {"chance": "x"}, {"chance": 200}):
            resp = self.client.put("/api/agent/qq/interject_chance/111",
                                   json=bad)
            self.assertEqual(resp.status_code, 400, bad)

    def test_put_remote_blocked_by_default(self):
        resp = self.client.put("/api/agent/qq/interject_chance",
                               json={"chance": 50},
                               environ_base={"REMOTE_ADDR": "8.8.8.8"})
        self.assertEqual(resp.status_code, 403)


class GapApiTest(SessionsTestBase):
    """判断间隔：全局秒数 + 单群覆盖，settings.json 层热生效。"""

    def test_default_is_env_value(self):
        self.assertEqual(agents.interject_min_gap("qq", "111"),
                         config.QQ_INTERJECT_MIN_GAP)

    def test_global_then_override(self):
        agents.save_settings("qq", {"interject_min_gap": 30,
                                    "interject_min_gap_overrides": {"111": 0}})
        self.assertEqual(agents.interject_min_gap("qq", "333"), 30)
        self.assertEqual(agents.interject_min_gap("qq", "111"), 0)

    def test_out_of_range_is_clamped(self):
        agents.save_settings("qq", {"interject_min_gap": 99999})
        self.assertEqual(agents.interject_min_gap("qq", "111"), 3600)

    def test_bad_types_fall_back_to_env(self):
        agents.save_settings("qq", {"interject_min_gap": "abc"})
        self.assertEqual(agents.interject_min_gap("qq", "111"),
                         config.QQ_INTERJECT_MIN_GAP)

    def test_sessions_carry_global_and_override(self):
        self.write_session("qq", key="group_111")
        agents.save_settings("qq", {"interject_min_gap": 20,
                                    "interject_min_gap_overrides": {"111": 5}})
        d = self.client.get("/api/agent/qq/sessions").get_json()
        self.assertEqual(d["interject_min_gap"], 20)
        self.assertEqual(d["sessions"][0]["gap_override"], 5)

    def test_put_global_persists(self):
        resp = self.client.put("/api/agent/qq/interject_min_gap",
                               json={"min_gap": 45})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(agents.load_settings("qq")["interject_min_gap"], 45)

    def test_put_global_rejects_bad_values(self):
        for bad in ({}, {"min_gap": "45"}, {"min_gap": -1},
                    {"min_gap": 3601}, {"min_gap": None}):
            resp = self.client.put("/api/agent/qq/interject_min_gap", json=bad)
            self.assertEqual(resp.status_code, 400, bad)

    def test_put_group_override_and_clear(self):
        self.client.put("/api/agent/qq/interject_min_gap/111",
                        json={"min_gap": 5})
        ov = agents.load_settings("qq")["interject_min_gap_overrides"]
        self.assertEqual(ov["111"], 5)
        self.client.put("/api/agent/qq/interject_min_gap/111",
                        json={"min_gap": None})
        self.assertEqual(
            agents.load_settings("qq")["interject_min_gap_overrides"], {})

    def test_put_group_rejects_bad_values(self):
        for bad in ({}, {"min_gap": "x"}, {"min_gap": 4000}):
            resp = self.client.put("/api/agent/qq/interject_min_gap/111",
                                   json=bad)
            self.assertEqual(resp.status_code, 400, bad)

    def test_put_remote_blocked_by_default(self):
        resp = self.client.put("/api/agent/qq/interject_min_gap",
                               json={"min_gap": 30},
                               environ_base={"REMOTE_ADDR": "8.8.8.8"})
        self.assertEqual(resp.status_code, 403)


class ImageGenToggleApiTest(SessionsTestBase):
    """生图开关：全局总闸 + 单群名单，settings.json 热生效。

    判定逻辑在 agents.image_gen_allowed，执行闸在 generate_image 工具入口
    （这里测 API 与持久化，工具闸的线程上下文测试见 test_tools_registry）。
    """

    def test_sessions_carry_state_and_global_default_on(self):
        self.write_session("qq", key="group_111")
        d = self.client.get("/api/agent/qq/sessions").get_json()
        self.assertTrue(d["image_gen_on"])           # 没配过 = 开
        self.assertTrue(d["sessions"][0]["image_gen"])

    def test_global_off_persists_and_reflects(self):
        resp = self.client.put("/api/agent/qq/image_gen",
                               json={"enabled": False})
        self.assertEqual(resp.status_code, 200)
        d = self.client.get("/api/agent/qq/sessions").get_json()
        self.assertFalse(d["image_gen_on"])
        self.assertIs(agents.load_settings("qq")["image_gen"], False)

    def test_global_route_requires_enabled_bool(self):
        for bad in ({}, {"enabled": "yes"}, {"enabled": 1}):
            resp = self.client.put("/api/agent/qq/image_gen", json=bad)
            self.assertEqual(resp.status_code, 400)

    def test_group_mute_persists_and_reflects(self):
        self.write_session("qq", key="group_111")
        resp = self.client.put("/api/agent/qq/image_gen/111",
                               json={"enabled": False})
        self.assertEqual(resp.status_code, 200)
        d = self.client.get("/api/agent/qq/sessions").get_json()
        self.assertFalse(d["sessions"][0]["image_gen"])
        self.assertEqual(agents.load_settings("qq")["image_gen_muted"], ["111"])

    def test_group_unmute_removes_from_list(self):
        agents.save_settings("qq", {"image_gen_muted": ["111"]})
        self.client.put("/api/agent/qq/image_gen/111", json={"enabled": True})
        self.assertEqual(agents.load_settings("qq")["image_gen_muted"], [])

    def test_routes_remote_blocked_by_default(self):
        resp = self.client.put("/api/agent/qq/image_gen",
                               json={"enabled": False},
                               environ_base={"REMOTE_ADDR": "8.8.8.8"})
        self.assertEqual(resp.status_code, 403)
        resp = self.client.put("/api/agent/qq/image_gen/111",
                               json={"enabled": False},
                               environ_base={"REMOTE_ADDR": "8.8.8.8"})
        self.assertEqual(resp.status_code, 403)

    def test_policy_global_switch_beats_group_list(self):
        # 总闸优先于单群名单；私聊只看总闸；名单里没有的群照常
        self.assertTrue(agents.image_gen_allowed("qq", "group", "111")[0])
        agents.save_settings("qq", {"image_gen_muted": ["111"]})
        self.assertFalse(agents.image_gen_allowed("qq", "group", "111")[0])
        self.assertTrue(agents.image_gen_allowed("qq", "group", "222")[0])
        self.assertTrue(agents.image_gen_allowed("qq", "private", "111")[0])
        agents.save_settings("qq", {"image_gen": False,
                                    "image_gen_muted": ["111"]})
        self.assertFalse(agents.image_gen_allowed("qq", "private", "999")[0])
        # 拒绝时给理由（模型要能转述），放行时理由为空
        self.assertTrue(agents.image_gen_allowed("qq", "group", "111")[1])


class ImageSendFormatApiTest(SessionsTestBase):
    """发图格式开关：全局 jpg/png + 单群覆盖，settings.json 热生效。

    只决定「发出去那一张」怎么编码；执行点落在 image_out.prepare_for_send
    （那条路测在 test_image_out），这里测取值层级、持久化与接口。
    """

    def test_default_is_jpg(self):
        self.assertEqual(agents.image_send_format("qq", "group", "111"), "jpg")
        self.assertEqual(agents.image_send_format("qq", None, None), "jpg")
        self.assertEqual(agents.IMAGE_SEND_FORMAT_DEFAULT, "jpg")

    def test_global_then_group_override(self):
        agents.save_settings("qq", {"image_send_format": "png",
                                    "image_send_format_overrides":
                                        {"111": "jpg"}})
        self.assertEqual(agents.image_send_format("qq", None, None), "png")
        self.assertEqual(agents.image_send_format("qq", "group", "111"), "jpg")
        self.assertEqual(agents.image_send_format("qq", "group", "222"), "png")

    def test_private_has_its_own_override(self):
        """私聊也能单独设：跟群共用一张覆盖表，键就是会话号。

        这跟本模块早先「覆盖只认群」的口径不同（当时照抄 image_gen_muted）。
        改的原因：格式是**给对方看**的，私聊里对方一样嫌 jpg 糊；生图开关
        只管自己，所以两者不必同口径。
        """
        agents.save_settings("qq", {"image_send_format": "jpg",
                                    "image_send_format_overrides":
                                        {"111": "png"}})
        self.assertEqual(agents.image_send_format("qq", "private", "111"),
                         "png")
        # 没覆盖过的私聊照样吃全局
        self.assertEqual(agents.image_send_format("qq", "private", "999"),
                         "jpg")

    def test_illegal_values_degrade_to_jpg(self):
        """手改坏 settings.json 不能把野值透给 PIL（save 会直接抛错卡住发图）。"""
        agents.save_settings("qq", {"image_send_format": "webp",
                                    "image_send_format_overrides":
                                        {"111": "PNG "}})
        self.assertEqual(agents.image_send_format("qq", "group", "111"), "jpg")
        self.assertEqual(agents.image_send_format("qq", "group", "222"), "jpg")

    def test_sessions_carry_global_and_override(self):
        self.write_session("qq", key="group_111")
        d = self.client.get("/api/agent/qq/sessions").get_json()
        self.assertEqual(d["image_send_format"], "jpg")
        self.assertIsNone(d["sessions"][0]["image_send_format"])

    def test_put_global_persists_and_reflects(self):
        resp = self.client.put("/api/agent/qq/image_send_format",
                               json={"format": "png"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(agents.load_settings("qq")["image_send_format"], "png")
        d = self.client.get("/api/agent/qq/sessions").get_json()
        self.assertEqual(d["image_send_format"], "png")

    def test_put_global_accepts_uppercase(self):
        self.client.put("/api/agent/qq/image_send_format",
                        json={"format": "PNG"})
        self.assertEqual(agents.load_settings("qq")["image_send_format"], "png")

    def test_put_global_rejects_bad_values(self):
        for bad in ({}, {"format": "webp"}, {"format": None}, {"format": 1},
                    {"format": True}, {"format": ""}):
            resp = self.client.put("/api/agent/qq/image_send_format", json=bad)
            self.assertEqual(resp.status_code, 400, bad)

    def test_put_group_override_and_clear(self):
        self.write_session("qq", key="group_111")
        resp = self.client.put("/api/agent/qq/image_send_format/111",
                               json={"format": "png"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            agents.load_settings("qq")["image_send_format_overrides"]["111"],
            "png")
        d = self.client.get("/api/agent/qq/sessions").get_json()
        self.assertEqual(d["sessions"][0]["image_send_format"], "png")
        # null = 删覆盖，回落全局
        self.client.put("/api/agent/qq/image_send_format/111",
                        json={"format": None})
        self.assertEqual(
            agents.load_settings("qq")["image_send_format_overrides"], {})
        d = self.client.get("/api/agent/qq/sessions").get_json()
        self.assertIsNone(d["sessions"][0]["image_send_format"])

    def test_put_group_rejects_bad_values(self):
        for bad in ({}, {"format": "webp"}, {"format": 1}):
            resp = self.client.put("/api/agent/qq/image_send_format/111",
                                   json=bad)
            self.assertEqual(resp.status_code, 400, bad)

    def test_routes_remote_blocked_by_default(self):
        for path in ("/api/agent/qq/image_send_format",
                     "/api/agent/qq/image_send_format/111"):
            resp = self.client.put(path, json={"format": "png"},
                                   environ_base={"REMOTE_ADDR": "8.8.8.8"})
            self.assertEqual(resp.status_code, 403, path)

    def test_private_session_carries_override_field(self):
        """列表接口要给私聊行也带上格式字段，管理页才画得出那个按钮。"""
        self.write_session("qq", key="private_222")
        agents.save_settings("qq", {"image_send_format_overrides":
                                        {"222": "png"}})
        d = self.client.get("/api/agent/qq/sessions").get_json()
        self.assertEqual(d["sessions"][0]["image_send_format"], "png")

    def test_put_override_accepts_private_id(self):
        """写接口不区分会话类型：同一段 URL，私聊号照样能写能删。"""
        self.write_session("qq", key="private_222")
        resp = self.client.put("/api/agent/qq/image_send_format/222",
                               json={"format": "png"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(agents.image_send_format("qq", "private", "222"),
                         "png")
        self.client.put("/api/agent/qq/image_send_format/222",
                        json={"format": None})
        self.assertEqual(agents.image_send_format("qq", "private", "222"),
                         "jpg")


class RecentGroupMergeTest(SessionsTestBase):
    """只收过消息、没回复过的群也要进管理页列表（统一开关主动发言）。"""

    def write_recent(self, aid, gid, lines=("m1", "m2")):
        d = os.path.join(self.root, aid, "recent")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, "group_%s.jsonl" % gid)
        with open(path, "w", encoding="utf-8") as f:
            for x in lines:
                f.write(json.dumps({"t": 1, "n": "甲", "x": x}, ensure_ascii=False)
                        + "\n")
        return path

    def test_recent_only_group_is_merged_with_session_flag(self):
        self.write_recent("qq", "333")
        d = self.client.get("/api/agent/qq/sessions").get_json()
        row = self.by_key(d["sessions"])["group_333"]
        self.assertFalse(row["session"])
        self.assertEqual(row["target_id"], "333")
        self.assertEqual(row["messages"], 2)
        self.assertTrue(row["interject"])

    def test_sessions_with_history_are_not_duplicated(self):
        self.write_session("qq", key="group_111")
        self.write_recent("qq", "111")
        d = self.client.get("/api/agent/qq/sessions").get_json()
        rows = [i for i in d["sessions"] if i["key"] == "group_111"]
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0].get("session", True))

    def test_non_group_or_non_numeric_files_are_ignored(self):
        d = os.path.join(self.root, "qq", "recent")
        os.makedirs(d, exist_ok=True)
        for fname in ("private_1.jsonl", "group_abc.jsonl", "group_.jsonl",
                      "notes.txt"):
            with open(os.path.join(d, fname), "w", encoding="utf-8") as f:
                f.write("{}\n")
        d = self.client.get("/api/agent/qq/sessions").get_json()
        self.assertEqual([i for i in d["sessions"] if i.get("session") is False],
                         [])

    def test_recent_stats_helper_counts_groups(self):
        self.write_recent("qq", "333")
        self.write_recent("qq", "444", lines=())
        stats = agents.recent_group_stats("qq")
        self.assertEqual(sorted(stats), ["333", "444"])
        self.assertEqual(stats["333"]["messages"], 2)


if __name__ == "__main__":
    unittest.main()
