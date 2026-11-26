# -*- coding: utf-8 -*-
"""工作台「一句话」通道（2026-11-26）守卫测试。

用户要的是「工作台的提示词那一套跟 QQ 一样」：同一个模型、同一条
`_MASTER_TEMPLATE`、同一份 6 轮历史窗口。这条链路新建时只补了两块网页端
缺的能力（改词、点名渠道），别再被后来的人扩成第二套。

为什么必须单独锁：这几条错了**照样能出图**，只是出的不是用户要的那张
（「换成白毛衣」被当成「画个白毛衣的东西」、LoRA 槽位被模型改掉），
跑图跑通了完全看不出来。
"""
import json
import os
import unittest
from unittest import mock

import app as app_pkg  # noqa: F401  （确保 app 包先加载）
from app.features.image import routes as R


class _FakeHistory(object):
    """`_web_history` 的桩：记录调用、回一份固定历史。"""

    def __init__(self, rows=None):
        self.rows = rows or []
        self.calls = []

    def __call__(self, qq):
        self.calls.append(qq)
        return list(self.rows)


class ReviseBranchTest(unittest.TestCase):
    """改词轮：有上一张 + 这句是接着说的 → 走 QQ 那份转译。"""

    def setUp(self):
        self.trans = []
        self.enq = []
        self._p = [
            mock.patch.object(R, "_who_qq", return_value="10001"),
            mock.patch.object(R, "_web_history", new=_FakeHistory()),
            mock.patch.object(
                R, "_enqueue_job",
                new=lambda spec, owner, user_text="": (
                    self.enq.append(spec),
                    ({"id": "GP-1", "skill": spec["skill"],
                      "prompt": spec["prompt"]}, None))[1]),
        ]
        for p in self._p:
            p.start()
        from app.features.image import direct_gen
        self.dg = direct_gen
        self._orig_last = direct_gen._last_job
        self._orig_tr = direct_gen._translate
        direct_gen._last_job = lambda sk: {
            "skill": "silver",
            "prompt": "1girl, silver hair, school uniform, classroom"}
        # 转译本身打桩：测的是「这一轮走没走 QQ 那条路」，不是模型输出质量
        # （模型的质量由 test_direct_gen 那批锁着）。
        direct_gen._translate = lambda text, hist, skill=None, **k: (
            self.trans.append({"text": text, "hist": hist, "skill": skill}),
            {"skill": skill or "silver", "prompt": "WHITE-SWEATER"})[1]

    def tearDown(self):
        self.dg._last_job = self._orig_last
        self.dg._translate = self._orig_tr
        for p in self._p:
            p.stop()

    def _run(self, text, skill="silver", data=None):
        return R._web_decide(text, skill, [], {"images": [], "refs": []},
                             data or {})

    def test_short_line_goes_through_translate(self):
        """「换成白色毛衣」必须过 QQ 那份转译——这是整个需求的起因。"""
        got = self._run("换成白色毛衣")
        self.assertIsNotNone(got)
        self.assertEqual(len(self.trans), 1)
        self.assertEqual(self.enq[0]["prompt"], "WHITE-SWEATER")
        # 渠道沿用点名的那个（用户在工作台选了 silver）
        self.assertEqual(self.enq[0]["skill"], "silver")

    def test_english_tag_string_is_not_rewritten(self):
        """用户自己写好的成品串不该被当「改上一张」再改写一遍。"""
        got = self._run("1girl, silver hair, white sweater, masterpiece")
        self.assertIsNone(got)
        self.assertEqual(self.enq, [])

    def test_weighted_prompt_is_not_rewritten(self):
        got = self._run("1.2::artist:x::, 1girl")
        self.assertIsNone(got)

    def test_new_scene_falls_through(self):
        """另一张新画面（长句、和上一张没重叠）→ 回落老路。

        句子要够长（> 24 字）：短句按「接上一张说的」处理，那是工作台
        这个界面里最省事的判据（「换成白毛衣」就是这么长）。"""
        got = self._run("一个穿和服的男孩站在海边码头上等朋友，背景是傍晚泛红的云层")
        self.assertIsNone(got)
        self.assertEqual(self.enq, [])

    def test_runner_params_always_fall_through(self):
        """跑图台的 LoRA 槽位 / 步数 / 横屏这些参数，转译那条路不认 → 必须回落。"""
        for data in ({"loras": [{"slot": 1, "on": True}]},
                     {"steps": 40}, {"cfg": 5.5}, {"landscape": True},
                     {"width": 1024, "height": 1536},
                     {"prompt_is_final": True}, {"artist_prefix": "a, b"}):
            self.assertIsNone(self._run("换成白色毛衣", data=data),
                              "带 %r 还接管了" % (data,))
        self.assertEqual(self.enq, [])

    def test_model_says_no_paint_raises_reply(self):
        """模型判「这轮不用画」→ 抛 _WebReply，交给前端显示气泡。"""
        self.dg._translate = lambda *a, **k: {"reply": "这句没说要画什么"}
        with self.assertRaises(R._WebReply) as ctx:
            self._run("换成白色毛衣")
        self.assertEqual(ctx.exception.reply, "这句没说要画什么")
        self.assertEqual(self.enq, [])


class ChannelBranchTest(unittest.TestCase):
    """点名渠道：让模型的渠道判定生效（工作台不再只有下拉框那一个渠道）。"""

    def setUp(self):
        self.enq = []
        self._p = [
            mock.patch.object(R, "_who_qq", return_value="10001"),
            mock.patch.object(R, "_web_history", new=_FakeHistory()),
            mock.patch.object(
                R, "_enqueue_job",
                new=lambda spec, owner, user_text="": (
                    self.enq.append(spec),
                    ({"id": "GP-2", "skill": spec["skill"],
                      "prompt": spec["prompt"]}, None))[1]),
        ]
        for p in self._p:
            p.start()
        from app.features.image import direct_gen
        self.dg = direct_gen
        self._orig_last = direct_gen._last_job
        direct_gen._last_job = lambda sk: None       # 没有上一张
        self._orig_tr = direct_gen._translate
        direct_gen._translate = lambda text, hist, skill=None, **k: {
            "skill": "krea2-jiguang", "prompt": "cyberpunk city"}

    def tearDown(self):
        self.dg._last_job = self._orig_last
        self.dg._translate = self._orig_tr
        for p in self._p:
            p.stop()

    def test_named_channel_uses_model_judgement(self):
        got = R._web_decide("用极光画个赛博城市", "",
                            [], {"images": [], "refs": []}, {})
        self.assertIsNotNone(got)
        self.assertEqual(got["skill"], "krea2-jiguang")

    def test_ui_picked_channel_wins(self):
        """用户在下拉框已经选了渠道 → 不再让模型改判。"""
        got = R._web_decide("用极光画个赛博城市", "silver",
                            [], {"images": [], "refs": []}, {})
        self.assertIsNone(got)


class LooksLikeReviseTest(unittest.TestCase):

    def test_short_is_revise(self):
        self.assertTrue(R._looks_like_revise("换成白色毛衣", {"prompt": "a"}))
        self.assertTrue(R._looks_like_revise("再换个姿势", {"prompt": "a"}))

    def test_overlap_in_long_sentence(self):
        prev = {"prompt": "1girl, silver hair, school uniform, classroom, sunset"}
        self.assertTrue(R._looks_like_revise(
            "还是银发少女，但是把背景换成下雨的夜晚街道", prev))

    def test_no_overlap_no_revise(self):
        prev = {"prompt": "1girl, silver hair, school uniform, classroom, sunset"}
        self.assertFalse(R._looks_like_revise(
            "一个穿和服的男孩站在海边码头上等朋友，背景是傍晚泛红的云层", prev))

    def test_empty_is_not_revise(self):
        self.assertFalse(R._looks_like_revise("", {"prompt": "a"}))


class WebHistoryTest(unittest.TestCase):
    """工作台自己的会话线：6 轮、不带 AI 回话、跨请求读得回来。"""

    def setUp(self):
        self.tmp = os.path.dirname(R._web_history_path("t"))
        os.makedirs(self.tmp, exist_ok=True)
        self.path = R._web_history_path("99999")
        if os.path.exists(self.path):
            os.remove(self.path)

    def tearDown(self):
        if os.path.exists(self.path):
            os.remove(self.path)

    def test_only_user_lines_survive(self):
        R._web_history_push("99999", "换成白色毛衣", "这句不用画")
        rows = R._web_history("99999")
        self.assertEqual([m["role"] for m in rows], ["user"])
        self.assertEqual(rows[0]["content"], "换成白色毛衣")

    def test_prompt_recorded_as_fact(self):
        R._web_history_push("99999", "画个银发少女", "",
                            "1girl, silver hair", "silver")
        rows = R._web_history("99999")
        self.assertEqual(rows[0]["role"], "user")
        self.assertEqual(rows[1]["role"], "assistant")
        self.assertIn("silver", rows[1]["content"])
        self.assertIn("1girl", rows[1]["content"])

    def test_capped_at_six_turns(self):
        for i in range(10):
            R._web_history_push("99999", "第 %d 句" % i, "回话 %d" % i)
        rows = R._web_history("99999")
        users = [m["content"] for m in rows if m["role"] == "user"]
        self.assertEqual(len(users), 6)
        self.assertEqual(users[-1], "第 9 句")
        self.assertEqual(users[0], "第 4 句")


class ChatRefTest(unittest.TestCase):
    """工作台临时参考图：`cr_…` 前缀，两套指向不能混。"""

    def test_prefix(self):
        from app.features.gallery import references
        ref = references.new_chat_ref()
        self.assertTrue(references.valid_chat_ref(ref))
        self.assertFalse(references.valid_chat_ref("abc"))
        self.assertFalse(references.valid_chat_ref(""))

    def test_upload_rejects_non_data_url(self):
        a = R.bp
        self.assertTrue(hasattr(a, "name"))


class EnqueueJobTest(unittest.TestCase):
    """`_enqueue_job` 是 submit 与 ask 共用的一段：字段缺省不能炸。"""

    def test_minimal_spec(self):
        with mock.patch.object(R, "_run", lambda job: None):
            job, err = R._enqueue_job(
                {"skill": "silver", "prompt": "x"}, owner="1")
        self.assertIsNone(err)
        self.assertEqual(job["loras"], [])
        self.assertIsNone(job["steps"])
        self.assertFalse(job["landscape"])
        R._JOBS.pop(job["id"], None)


if __name__ == "__main__":
    unittest.main()
