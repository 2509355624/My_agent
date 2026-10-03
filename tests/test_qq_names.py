"""名字解析缓存测试（app/qq_names.py）。

跟状态后台测试一样：NapCat 全 mock、不打 3000 口、不读真实 state/ 文件。
模块级缓存字典在每个测试里手动重置，避免被上一次跑残留污染。
"""

import os
import tempfile
import time
import unittest
from unittest import mock

import app.config as config
from app import qq_names


class StateIsolationTest(unittest.TestCase):
    """跑测试不许拿夹具盖掉真实群名缓存（2026-10-03）。

    实测：全量测试把 state/qq_names.json 写成了
    {"group":{"1":"旧群名"},"private":{"1":"张三"}}，机器人从此把 1 号群叫
    「旧群名」直到下次刷新列表。模块默认路径已改由 config.state_path 兜住。
    """

    def test_default_path_is_not_the_real_cache(self):
        self.assertNotEqual(
            os.path.dirname(qq_names.PATH),
            os.path.join(config.BASE_DIR, "state"),
            "测试进程的缓存路径落在真实 state/ 下了，跑测试会污染真实数据")


def _reset():
    qq_names._group = {}
    qq_names._private = {}
    qq_names._last_refresh = 0.0


class QqNamesTest(unittest.TestCase):

    def setUp(self):
        _reset()

    def test_name_for_unknown_returns_none(self):
        self.assertIsNone(qq_names.name_for("group", "999"))
        self.assertIsNone(qq_names.name_for("private", "888"))

    def test_note_private_caches_and_persists(self):
        with mock.patch.object(qq_names, "_persist") as pp:
            qq_names.note_private("100", "张三")
            qq_names.note_private("100", "张三")
            qq_names.note_private("200", "李四")
        self.assertEqual(qq_names.name_for("private", "100"), "张三")
        self.assertEqual(qq_names.name_for("private", "200"), "李四")
        # 第二次同名字是 noop，只触发两次写盘
        self.assertEqual(pp.call_count, 2)

    def test_note_private_ignores_empty(self):
        with mock.patch.object(qq_names, "_persist") as pp:
            qq_names.note_private("", "x")
            qq_names.note_private("100", "")
            qq_names.note_private(None, "x")
        self.assertEqual(pp.call_count, 0)
        self.assertEqual(qq_names._private, {})

    def test_refresh_lists_populates_from_api(self):
        qq_names._group = {}
        qq_names._private = {}
        with mock.patch("app.qq_api.get_group_list",
                        return_value=[{"group_id": 1, "group_name": "我的群"},
                                      {"group_id": "2", "group_name": "另一群"}]), \
             mock.patch("app.qq_api.get_friend_list",
                        return_value=[{"user_id": 100, "nickname": "小张",
                                       "remark": "老张"},
                                      {"user_id": 200, "nickname": "小李",
                                     "remark": ""}]), \
             mock.patch.object(qq_names, "_persist"):
            ok = qq_names.refresh_lists(force=True)
        self.assertTrue(ok)
        self.assertEqual(qq_names.name_for("group", "1"), "我的群")
        self.assertEqual(qq_names.name_for("group", "2"), "另一群")
        # friend_list 优先 remark，没有就昵称
        self.assertEqual(qq_names.name_for("private", "100"), "老张")
        self.assertEqual(qq_names.name_for("private", "200"), "小李")

    def test_refresh_lists_rate_limited(self):
        qq_names._last_refresh = time.time()
        with mock.patch("app.qq_api.get_group_list") as gl:
            ok = qq_names.refresh_lists(force=False)
        self.assertFalse(ok)
        gl.assert_not_called()

    def test_refresh_lists_force_bypasses_rate_limit(self):
        qq_names._last_refresh = time.time()
        with mock.patch("app.qq_api.get_group_list", return_value=[]), \
             mock.patch("app.qq_api.get_friend_list", return_value=[]), \
             mock.patch.object(qq_names, "_persist"):
            ok = qq_names.refresh_lists(force=True)
        self.assertTrue(ok)

    def test_refresh_lists_survives_api_failure(self):
        """NapCat 不在线时静默失败，缓存保留原样。"""
        qq_names._group = {"1": "旧群名"}
        qq_names._last_refresh = 0.0
        with mock.patch("app.qq_api.get_group_list",
                        side_effect=RuntimeError("napcat down")):
            ok = qq_names.refresh_lists(force=True)
        self.assertFalse(ok)
        self.assertEqual(qq_names.name_for("group", "1"), "旧群名")

    def test_persist_and_reload_roundtrip(self):
        """写盘 → 重置内存 → _load 能读回来（重启不丢）。"""
        tmp = tempfile.mkdtemp()
        with mock.patch.object(qq_names, "PATH", os.path.join(tmp, "names.json")):
            qq_names.note_private("100", "小张")
            _reset()
            qq_names._load()
        self.assertEqual(qq_names.name_for("private", "100"), "小张")
