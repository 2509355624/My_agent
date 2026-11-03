"""内置卡保护（2026-11-03 用户要求「创建角色不能动我的默认角色」）。

事故背景：`persona.save_cards()` 是**整体覆盖**写。一个没做隔离的测试调了
`save_cards([一张卡])`，用户 11 张默认卡当场只剩 1 张。这里钉住三件事：

  1. 内置卡不能改内容
  2. 内置卡不会因为「传进来的列表里没有它」而被删掉
  3. 普通卡照常可增删改
"""
import json
import os
import tempfile
import unittest
from unittest import mock

from app import agents, persona


BUILTIN = "izumi_sagiri"


class BuiltinBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = os.path.join(self.tmp.name, "agents")
        os.makedirs(root, exist_ok=True)
        p = mock.patch.object(agents, "AGENTS_DIR", root)
        p.start()
        self.addCleanup(p.stop)
        agents.clear_cache()
        self.addCleanup(agents.clear_cache)

    def seed(self):
        persona.save_cards([
            {"id": BUILTIN, "name": "纱雾", "systemPrompt": "原版"},
            {"id": "rem", "name": "雷姆", "systemPrompt": "原版"},
            {"id": "K1", "name": "我的卡", "systemPrompt": "x"},
        ])

    def ids(self):
        return sorted(c["id"] for c in persona.load_cards())


class BuiltinDetectTest(unittest.TestCase):

    def test_config_ids_are_builtin(self):
        for cid in ("izumi_sagiri", "rem", "sister", "junior"):
            self.assertTrue(persona.is_builtin_card(cid), cid)

    def test_new_card_is_not_builtin(self):
        self.assertFalse(persona.is_builtin_card("K20260101-000000"))

    def test_builtin_flag_on_card(self):
        self.assertTrue(persona.is_builtin_card({"id": "whatever",
                                                 "builtin": True}))


class SaveCardsTest(BuiltinBase):

    def test_builtin_survives_partial_save(self):
        """只传一张普通卡进来，内置卡必须补回去。"""
        self.seed()
        persona.save_cards([{"id": "K1", "name": "我的卡", "systemPrompt": "x"}])
        self.assertIn(BUILTIN, self.ids(), "内置卡被删掉了")
        self.assertIn("rem", self.ids())

    def test_builtin_content_cannot_change(self):
        self.seed()
        persona.save_cards([{"id": BUILTIN, "name": "篡改",
                             "systemPrompt": "改了"}])
        c = [x for x in persona.load_cards() if x["id"] == BUILTIN][0]
        self.assertEqual(c["systemPrompt"], "原版")

    def test_normal_card_can_change(self):
        self.seed()
        persona.save_cards([{"id": "K1", "name": "改名了",
                             "systemPrompt": "y"}])
        c = [x for x in persona.load_cards() if x["id"] == "K1"][0]
        self.assertEqual(c["name"], "改名了")

    def test_empty_save_keeps_builtins(self):
        """传空列表也不能把内置卡清空（最极端的那种误用）。"""
        self.seed()
        persona.save_cards([])
        self.assertIn(BUILTIN, self.ids())
        self.assertNotIn("K1", self.ids())      # 普通卡该没就没


class GuardTest(BuiltinBase):
    """批量删除闸（2026-11-04 深夜）。

    事故背景：卡库从 25 张变成 12 张，丢了 14 张，含用户的原版伊蕾娜卡。
    内置卡那道补丁只护得住内置卡，用户自己建的卡一张都没护住。
    这里钉住：整表覆盖时「少了一半以上」必须拒绝，除非显式 force。
    """

    def test_truncated_list_is_rejected(self):
        """25 张 → 3 张：判成列表被截断，抛异常，磁盘不动。"""
        persona.save_cards([{"id": "K%d" % i, "name": "卡%d" % i}
                            for i in range(25)], force=True)
        before = self.ids()
        with self.assertRaises(persona.SaveCardsRejected):
            persona.save_cards([{"id": "K0", "name": "卡0"}])
        self.assertEqual(self.ids(), before, "被拒绝时不该动磁盘")

    def test_rejection_says_what_would_be_lost(self):
        """异常里要带得出「会丢哪些」——不然用户不知道该去救谁。"""
        persona.save_cards([{"id": "K%d" % i, "name": "卡%d" % i}
                            for i in range(10)], force=True)
        with self.assertRaises(persona.SaveCardsRejected) as ctx:
            persona.save_cards([{"id": "K0", "name": "卡0"}])
        e = ctx.exception
        self.assertEqual(e.old_n, 10)
        self.assertIn("K1", e.lost)
        self.assertIn("10", str(e))

    def test_normal_edit_still_passes(self):
        """正常改卡（张数不变）不能被闸误伤。"""
        self.seed()
        persona.save_cards([
            {"id": BUILTIN, "name": "纱雾", "systemPrompt": "原版"},
            {"id": "rem", "name": "雷姆", "systemPrompt": "原版"},
            {"id": "K1", "name": "改过了", "systemPrompt": "y"},
        ])
        c = [x for x in persona.load_cards() if x["id"] == "K1"][0]
        self.assertEqual(c["name"], "改过了")

    def test_deleting_a_few_is_allowed(self):
        """删一两张是正常操作，不该被拦（闸的线是一半）。"""
        persona.save_cards([{"id": "K%d" % i, "name": "卡%d" % i}
                            for i in range(6)], force=True)
        persona.save_cards([{"id": "K%d" % i, "name": "卡%d" % i}
                            for i in range(5)])
        self.assertEqual(len(self.ids()), 5)

    def test_force_allows_bulk_delete(self):
        """带 force 就该真删——闸是防手滑，不是防用户真的想删。"""
        persona.save_cards([{"id": "K%d" % i, "name": "卡%d" % i}
                            for i in range(10)], force=True)
        persona.save_cards([{"id": "K0", "name": "卡0"}], force=True)
        self.assertEqual(self.ids(), ["K0"])

    def test_empty_library_is_not_guarded(self):
        """空库写进去不受影响：没有「旧的一批」可丢，闸不该误拦。"""
        persona.save_cards([{"id": "K1", "name": "卡1"}])
        self.assertEqual(self.ids(), ["K1"])


if __name__ == "__main__":
    unittest.main()
