# -*- coding: utf-8 -*-
"""群友自定义预设（`zdy-`）的规则测试。

钉的是 2026-10-30 用户拍板的那套规则：每人 1 公开 / 5 私有、公开名全站唯一、
换公开自动顶替、`zdy-` 的各种连字符写法、群聊只认公开池、私聊优先自己。

用 `lp._path()` 直接清文件，**不碰真实 state**——`unittest` 在 sys.modules 里时
`state_path` 会自动切到临时目录（见 app/config.py 的 state_path）。
"""

import os
import unittest

from app import lora_presets as lp


def _loras(n=1, name="moss-nai5.safetensors", strength=0.8):
    return [{"slot": i + 1, "on": True, "strength": strength, "name": name}
            for i in range(n)]


class _Base(unittest.TestCase):
    def setUp(self):
        try:
            os.remove(lp._path())
        except OSError:
            pass


class ParseTest(unittest.TestCase):
    """`zdy-` 令牌怎么从一句话里认出来。"""

    def test_half_width(self):
        hit = lp.extract("zdy-光影，一个女孩站在窗边")
        self.assertIsNotNone(hit)
        self.assertEqual(hit[0], "光影")

    def test_full_width_minus(self):
        # 中文输入法把 - 打成全角是最常见的失败，认不出来群友会以为是自己打错
        hit = lp.extract("zdy－光影 画一个女孩")
        self.assertIsNotNone(hit)
        self.assertEqual(hit[0], "光影")

    def test_dash_and_uppercase(self):
        for text in ("zdy—光影", "ZDY-光影", "Zdy–光影", "zdy‐光影"):
            hit = lp.extract(text)
            self.assertIsNotNone(hit, text)
            self.assertEqual(hit[0], "光影", text)

    def test_spaces_around_dash(self):
        hit = lp.extract("zdy - 光影")
        self.assertEqual(hit[0], "光影")

    def test_token_in_middle_of_sentence(self):
        hit = lp.extract("帮我用 zdy-光影 画个女孩")
        self.assertIsNotNone(hit)
        self.assertEqual(hit[0], "光影")

    def test_bare_word_is_not_a_token(self):
        self.assertIsNone(lp.extract("自定义预设怎么用"))
        self.assertIsNone(lp.extract("zdy 是什么"))
        self.assertIsNone(lp.extract("zdy_光影"))
        self.assertIsNone(lp.extract("azdy-光影"))


class KnownNameSplitTest(_Base):
    """中文没有词边界：`zdy-233跑一版` 要切在 `233`（2026-10-30 定格群实录）。

    不切的话正则会把「跑一版」一起当成名字，resolve 报「没有这个公开预设」，
    用户只是想「用 233 再画一版」。切法不是猜：拿**库里真实存在**的名字做
    最长前缀匹配；一个都对不上就原样返回，让 resolve 照实报错。
    """

    def setUp(self):
        super().setUp()
        lp.save("10000001", "233", _loras(1), public=True)

    def test_verb_suffix_is_cut_off(self):
        hit = lp.extract("zdy-233跑一版")
        self.assertEqual(hit[0], "233")
        # 令牌本身（text[start:end]）必须正好是 `zdy-233`，剩下的字留在原话里
        self.assertEqual("zdy-233跑一版"[hit[1]:hit[2]], "zdy-233")

    def test_longest_prefix_wins(self):
        lp.save("10000001", "233跑", _loras(1))
        hit = lp.extract("zdy-233跑一版")
        self.assertEqual(hit[0], "233跑")

    def test_exact_name_is_untouched(self):
        hit = lp.extract("zdy-233，1girl, solo")
        self.assertEqual(hit[0], "233")
        self.assertEqual("zdy-233，1girl, solo"[hit[1]:hit[2]], "zdy-233")

    def test_unknown_name_is_returned_as_is(self):
        """库里没有的名字不硬凑——照实返回，让 resolve 报「没有这个预设」。"""
        hit = lp.extract("zdy-999画一张")
        self.assertEqual(hit[0], "999画一张")


class CleanNameTest(unittest.TestCase):
    def test_strip_prefix(self):
        # 直接粘 zdy-光影 存进去，名字不该变成 zdy-光影（否则越用越长）
        self.assertEqual(lp.clean_name("zdy-光影"), "光影")
        self.assertEqual(lp.clean_name("zdy－光影"), "光影")
        self.assertEqual(lp.clean_name("  夜景  "), "夜景")

    def test_name_rules(self):
        self.assertEqual(lp.check_name("光影"), "")
        self.assertEqual(lp.check_name("abc_123"), "")
        self.assertTrue(lp.check_name(""))
        self.assertTrue(lp.check_name("有 空格"))     # 空格会被当成断词边界
        self.assertTrue(lp.check_name("x" * 17))      # 超长
        self.assertTrue(lp.check_name("带-横线"))


class QuotaTest(_Base):
    def test_private_cap_is_five(self):
        for i in range(lp.MAX_PRIVATE):
            ok, _ = lp.save("1001", "私%d" % i, _loras())
            self.assertTrue(ok)
        ok, msg = lp.save("1001", "第六个", _loras())
        self.assertFalse(ok)
        self.assertIn("上限", msg)

    def test_public_does_not_eat_private_quota(self):
        ok, _ = lp.save("1001", "公开的", _loras(), public=True)
        self.assertTrue(ok)
        for i in range(lp.MAX_PRIVATE):
            ok, msg = lp.save("1001", "私%d" % i, _loras())
            self.assertTrue(ok, msg)
        q = lp.quota("1001")
        self.assertEqual((q["public"], q["private"]), (1, 5))

    def test_public_quota_is_three(self):
        for n in ("甲", "乙", "丙"):
            ok, msg = lp.save("1001", n, _loras(), public=True)
            self.assertTrue(ok, msg)
        ok, msg = lp.save("1001", "丁", _loras(), public=True)
        self.assertFalse(ok)
        self.assertIn("上限", msg)
        self.assertEqual(lp.quota("1001")["public"], 3)
        self.assertEqual(len(lp.public_pool()), 3)
        # 满了不自动顶：前三个必须还在
        self.assertEqual([n for n, _ in lp.public_pool()], ["丙", "乙", "甲"])

    def test_resave_public_does_not_eat_quota(self):
        # 已经是公开的那条改槽位重存，是「改内容」不是「新增」
        for n in ("甲", "乙", "丙"):
            lp.save("1001", n, _loras(), public=True)
        ok, msg = lp.save("1001", "丙", _loras(strength=0.5), public=True)
        self.assertTrue(ok, msg)
        self.assertEqual(lp.quota("1001")["public"], 3)
        self.assertEqual(lp.for_qq("1001")["丙"]["loras"][0]["strength"], 0.5)


class PublicUniqueTest(_Base):
    def test_public_name_is_globally_unique(self):
        lp.save("1001", "光影", _loras(), public=True)
        ok, msg = lp.save("1002", "光影", _loras(), public=True)
        self.assertFalse(ok)
        self.assertIn("已经被别人公开", msg)

    def test_private_can_share_name_with_public(self):
        lp.save("1001", "光影", _loras(), public=True)
        ok, msg = lp.save("1002", "光影", _loras())    # 私有，撞了也没事
        self.assertTrue(ok, msg)

    def test_two_privates_same_name_do_not_interfere(self):
        self.assertTrue(lp.save("1001", "光影", _loras(strength=0.5))[0])
        self.assertTrue(lp.save("1002", "光影", _loras(strength=0.9))[0])
        self.assertEqual(lp.for_qq("1001")["光影"]["loras"][0]["strength"], 0.5)
        self.assertEqual(lp.for_qq("1002")["光影"]["loras"][0]["strength"], 0.9)

    def test_promote_own_private_ok(self):
        lp.save("1001", "光影", _loras())
        ok, msg = lp.set_public("1001", "光影", True)
        self.assertTrue(ok, msg)
        self.assertEqual(lp.quota("1001")["public"], 1)


class ResolveTest(_Base):
    def setUp(self):
        super().setUp()
        lp.save("1001", "公开甲", _loras(name="a.safetensors"), public=True)
        lp.save("1001", "私有乙", _loras(name="b.safetensors"))

    def test_group_uses_public_pool(self):
        p, err = lp.resolve("公开甲", qq="1002", in_group=True)
        self.assertEqual(err, "")
        self.assertEqual(p["_owner"], "1001")

    def test_group_rejects_others_private(self):
        p, err = lp.resolve("私有乙", qq="1002", in_group=True)
        self.assertIsNone(p)
        self.assertIn("公开预设", err)

    def test_group_tells_owner_it_is_private(self):
        # 自己都点名了，直接说「没有」会让人以为打错了
        p, err = lp.resolve("私有乙", qq="1001", in_group=True)
        self.assertIsNone(p)
        self.assertIn("私有预设", err)
        self.assertIn("群里用不了", err)

    def test_private_chat_prefers_own(self):
        # 同名时私聊要自己的那套，不是别人的公开版
        lp.save("1002", "公开甲", _loras(name="other.safetensors"))
        p, err = lp.resolve("公开甲", qq="1001", in_group=False)
        self.assertEqual(err, "")
        self.assertEqual(p["_owner"], "1001")

    def test_private_chat_falls_back_to_public(self):
        p, err = lp.resolve("公开甲", qq="1002", in_group=False)
        self.assertEqual(err, "")
        self.assertEqual(p["_owner"], "1001")

    def test_missing_gives_hints(self):
        p, err = lp.resolve("不存在", qq="1001", in_group=False)
        self.assertIsNone(p)
        self.assertIn("zdy-不存在", err)
        self.assertIn("你自己存的", err)

    def test_group_missing_mentions_command(self):
        p, err = lp.resolve("不存在", qq="1002", in_group=True)
        self.assertIsNone(p)
        self.assertIn("自定义预设", err)


class SetPublicTest(_Base):
    def test_publish_until_full_then_reject(self):
        for n in ("甲", "乙", "丙"):
            lp.save("1001", n, _loras(), public=True)
        lp.save("1001", "丁", _loras())
        ok, msg = lp.set_public("1001", "丁", True)
        self.assertFalse(ok)
        self.assertIn("上限", msg)
        # 满了不顶替：前面三个都还在公开池里
        self.assertEqual(lp.quota("1001")["public"], 3)
        self.assertEqual(len(lp.public_pool()), 3)

    def test_unpublish(self):
        lp.save("1001", "甲", _loras(), public=True)
        ok, msg = lp.set_public("1001", "甲", False)
        self.assertTrue(ok)
        self.assertEqual(lp.quota("1001")["public"], 0)
        self.assertEqual(lp.public_pool(), [])

    def test_publish_missing_is_rejected(self):
        ok, msg = lp.set_public("1001", "没有的", True)
        self.assertFalse(ok)


class PoolTextTest(_Base):
    def test_empty_pool(self):
        self.assertIn("还没有人公开", lp.describe_pool())

    def test_lists_owner_and_usage(self):
        lp.save("1001", "光影", _loras(), public=True)
        text = lp.describe_pool(nick=lambda q: "小明" if q == "1001" else "")
        self.assertIn("zdy-光影", text)
        self.assertIn("小明", text)
        self.assertIn("用法", text)


class FriendApiTest(_Base):
    """friend_api 那几个口的最小闭环：令牌、存、查、切公开、配额回传。"""

    def setUp(self):
        super().setUp()
        from app import main, friend_auth
        self.c = main.app.test_client()
        self.h = {"X-Friend-Token": friend_auth.secret()}
        self.loras = [{"slot": 1, "on": True, "strength": 0.8,
                       "name": "moss-nai5.safetensors"}]

    def _save(self, name, public=False):
        return self.c.post("/api/friend/preset", headers=self.h,
                           json={"qq": "10000001", "name": name,
                                 "loras": self.loras, "public": public})

    def test_roundtrip(self):
        r = self._save("光影", public=True)
        self.assertEqual(r.status_code, 200, r.get_json())
        d = self.c.get("/api/friend/presets?qq=10000001", headers=self.h).get_json()
        self.assertEqual(d["rows"][0]["name"], "光影")
        self.assertTrue(d["rows"][0]["public"])
        self.assertEqual(d["quota"]["public"], 1)
        self.assertEqual(d["prefix"], "zdy-")

    def test_toggle_public(self):
        self._save("光影")
        r = self.c.post("/api/friend/preset/public", headers=self.h,
                        json={"qq": "10000001", "name": "光影", "public": True})
        self.assertTrue(r.get_json()["ok"])
        self.assertEqual(r.get_json()["quota"]["public"], 1)
        r = self.c.post("/api/friend/preset/public", headers=self.h,
                        json={"qq": "10000001", "name": "光影", "public": False})
        self.assertEqual(r.get_json()["quota"]["public"], 0)

    def test_quota_reaches_frontend(self):
        for i in range(lp.MAX_PRIVATE):
            self.assertTrue(self._save("私%d" % i).get_json()["ok"])
        r = self._save("第六个")
        self.assertEqual(r.status_code, 400)
        self.assertIn("上限", r.get_json()["error"])

    def test_without_token_is_forbidden(self):
        r = self.c.get("/api/friend/presets?qq=10000001")
        self.assertEqual(r.status_code, 403)

    def test_multi_slot_keeps_names(self):
        # 前端**必须**带 name 提交（2026-10-30 修）。漏了它，库里落下来的就是
        # 一串空名字：QQ 侧 `lora_string` 拼不出串，一句「LoRA 全关着」把
        # 明明开着 2/5/13 号的预设挡在外面。
        self.loras = [
            {"slot": 2, "on": True, "strength": 0.4,
             "name": "moss-maliker_moss_base_style_v3_e40.safetensors"},
            {"slot": 8, "on": False, "strength": 0.8,
             "name": "moss-nai5.safetensors"},
        ]
        self.assertTrue(self._save("组合").get_json()["ok"])
        row = self.c.get("/api/friend/presets?qq=10000001",
                         headers=self.h).get_json()["rows"][0]
        got = {s["slot"]: s for s in row["loras"]}
        self.assertEqual(got[2]["name"],
                         "moss-maliker_moss_base_style_v3_e40.safetensors")
        self.assertEqual(got[8]["name"], "moss-nai5.safetensors")
        # 关掉的默认槽连「关」一起存，套用时才不会被重新打开
        self.assertFalse(got[8]["on"])

    def test_delete(self):
        self._save("光影")
        r = self.c.delete("/api/friend/preset?qq=10000001&name=%E5%85%89%E5%BD%B1",
                          headers=self.h)
        self.assertTrue(r.get_json()["ok"])
        self.assertEqual(lp.for_qq("10000001"), {})


class SlotSnapshotTest(_Base):
    """槽位快照要能还原网页上看到的那一套，包括「把默认开的槽关掉」。"""

    def test_off_slots_are_kept(self):
        # 模板里 8 号槽默认开着，群友在网页上把它关了——这个「关」必须一起存，
        # 只存开着的槽会让套用时 8 号又活过来
        slots = [{"slot": 1, "on": True, "strength": 0.8, "name": "a.safetensors"},
                 {"slot": 8, "on": False, "strength": 0.8, "name": "b.safetensors"}]
        ok, msg = lp.save("10000001", "光影", slots, skill="moss-qunyou")
        self.assertTrue(ok, msg)
        row = lp.for_qq("10000001")["光影"]
        self.assertEqual(row["skill"], "moss-qunyou")
        off = [s for s in row["loras"] if s["slot"] == 8]
        self.assertEqual(len(off), 1)
        self.assertFalse(off[0]["on"])

    def test_all_off_is_rejected(self):
        ok, _ = lp.save("10000001", "空的",
                        [{"slot": 1, "on": False, "strength": 0.8, "name": "a"}])
        self.assertFalse(ok)

    def test_lora_string_skips_off_slots(self):
        lp.save("10000001", "光影", [
            {"slot": 1, "on": True, "strength": 0.8, "name": "a.safetensors"},
            {"slot": 8, "on": False, "strength": 0.8, "name": "b.safetensors"}])
        p = lp.for_qq("10000001")["光影"]
        self.assertEqual(lp.lora_string(p), "a.safetensors:0.80")


class LoraStringTest(unittest.TestCase):
    def test_only_enabled_and_positive(self):
        preset = {"loras": [
            {"on": True, "strength": 0.8, "name": "a.safetensors"},
            {"on": False, "strength": 0.8, "name": "b.safetensors"},
            {"on": True, "strength": 0, "name": "c.safetensors"},
            {"on": True, "strength": 0.5, "name": ""},
            {"on": True, "strength": 0.65, "name": "d.safetensors"},
        ]}
        self.assertEqual(lp.lora_string(preset),
                         "a.safetensors:0.80,d.safetensors:0.65")

    def test_empty(self):
        self.assertEqual(lp.lora_string({"loras": []}), "")


class LegacySnapshotTest(_Base):
    """只有槽号/开关/强度、**没有 LoRA 名字**的老预设必须还能跑。

    2026-10-30 现场问题：`zdy-233` 一用就回「这套预设里的 LoRA 全关着」，
    实际槽 2/5/13 全开着——老版网页存预设时漏传 `name`，`lora_string()`
    拼不出串，调用方拿「串是空的」当成了「没开 LoRA」。判据得看 on。
    """

    def test_save_keeps_slot_only_layers(self):
        ok, msg = lp.save("10000001", "老预设", [
            {"slot": 2, "on": True, "strength": 0.4, "name": ""},
            {"slot": 5, "on": True, "strength": 1.5, "name": ""}])
        self.assertTrue(ok, msg)
        p = lp.for_qq("10000001")["老预设"]
        self.assertEqual([s["slot"] for s in p["loras"] if s["on"]], [2, 5])
        # 拼不出串是**预期**的——调用方不许把它当成「全关着」
        self.assertEqual(lp.lora_string(p), "")

    def test_apply_slots_falls_back_to_template_names(self):
        from app import lora_slots, skills
        wf = skills.load_workflow(lora_slots.workflow_path("moss-qunyou"))
        before = {r["slot"]: r["name"] for r in lora_slots.read_slots(wf)}
        n = lora_slots.apply_slots(wf, [
            {"slot": 2, "on": True, "strength": 0.4, "name": ""},
            {"slot": 5, "on": True, "strength": 1.5, "name": ""}])
        self.assertEqual(n, 2)
        after = {r["slot"]: r for r in lora_slots.read_slots(wf)}
        # 名字沿用模板（不能被空串抹成没名字），开关和强度按预设
        self.assertEqual(after[2]["name"], before[2])
        self.assertEqual(after[5]["name"], before[5])
        self.assertTrue(after[2]["on"])
        self.assertAlmostEqual(after[5]["strength"], 1.5)


class FriendPageWiringTest(unittest.TestCase):
    """网页上「存预设 / 套预设」这两个动作的接线。

    两处都是 2026-10-30 修的真 bug，藏在前端不动后端测不出来：
    ① `currentLoras()` 漏传 name → 存进库的全是空名字；
    ② `applyPreset()` 一律 `on = true` → 用户关掉的默认槽套回来又开了。
    """

    @classmethod
    def setUpClass(cls):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "web", "friend.html"), encoding="utf-8") as f:
            cls.html = f.read()

    def test_save_sends_lora_name(self):
        self.assertIn("name: s.name", self.html)

    def test_apply_respects_off_slots(self):
        self.assertIn("hit.on !== false", self.html)
        self.assertEqual(lp.lora_string(None), "")


if __name__ == "__main__":
    unittest.main()
