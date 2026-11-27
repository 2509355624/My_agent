# -*- coding: utf-8 -*-
"""工作台「一句话」通道（2026-11-26）守卫测试。

用户要的只有一件事：**工作台的文生图 / 图生图，走大怪那套提示词**，
渠道由画风抽屉定死。这一条锁住三件事：

1. 每一句请求都过 `direct_gen`（有图走 `_revise`，没图走 `_translate`），
   不存在「某个条件下偷偷回落到只中译英的旧路」——那是改版前的老毛病。
2. 界面上选了哪个渠道，出图就用哪个渠道，模型改判也不算数。
3. 模型说「这轮不用画」时回一句；它没交作业时提示重来。

为什么必须单独锁：这几条错了**照样能出图**，只是出的不是用户要的那张，
跑图跑通了完全看不出来。
"""
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


class _Base(unittest.TestCase):
    """公共桩：身份、历史、入队，以及两条转译管道。"""

    def setUp(self):
        self.trans = []      # _translate 调用
        self.rev = []        # _web_revise 调用（带图那条**不该**再走到它）
        self.tags = []       # _tags_with_request 调用（图 + 需求一次出词）
        self.enq = []        # 入队的 spec
        self.lowres_flags = []   # 每次入队带的 lowres_on
        self.tr_result = {"skill": "silver", "prompt": "MASTER-OUT"}
        self.tag_result = "TAGS-OUT"

        def _translate(text, hist, skill=None, **k):
            self.trans.append({"text": text, "skill": skill, "hist": hist})
            return self.tr_result

        def _revise(text, channel, hist, images):
            self.rev.append({"text": text, "channel": channel,
                             "images": images})
            return {"skill": "silver", "prompt": "REVISED"}

        def _tags(images, request=""):
            self.tags.append({"images": images, "request": request})
            return self.tag_result

        def _enq(spec, owner, user_text="", lowres_on=False):
            self.enq.append(spec)
            self.lowres_flags.append(lowres_on)
            return ({"id": "GP-1", "skill": spec["skill"],
                     "prompt": spec["prompt"],
                     "source_images": spec.get("source_images") or []}, None)

        self._p = [
            mock.patch.object(R, "_who_qq", return_value="10001"),
            mock.patch.object(R, "_web_history", new=_FakeHistory()),
            mock.patch.object(R, "_enqueue_job", new=_enq),
            mock.patch.object(R, "_web_revise", new=_revise),
            # `_owner_key()` 要读 request context，单测里没有；打桩让它能跑。
            # 以前这 10 条其实一直在 `Working outside of request context` 上红，
            # 断言根本没被执行到（2026-11-27 顺手补上）。
            mock.patch.object(R, "_owner_key", return_value="__test__"),
        ]
        for p in self._p:
            p.start()
        from app.features.image import direct_gen
        self.dg = direct_gen
        self._orig_tr = direct_gen._translate
        direct_gen._translate = _translate
        self._orig_tags = direct_gen._tags_with_request
        direct_gen._tags_with_request = _tags

    def tearDown(self):
        self.dg._translate = self._orig_tr
        self.dg._tags_with_request = self._orig_tags
        for p in self._p:
            p.stop()

    def _run(self, text, skill="silver", images=None, refs=None):
        return R._web_decide(text, skill, [],
                             {"images": images or [], "refs": refs or []})


class TextToImageTest(_Base):
    """文生图：选了渠道说一句需求，走大怪那套提示词。"""

    def test_every_sentence_goes_through_master_template(self):
        job = self._run("银发少女在雨里奔跑")
        self.assertEqual(len(self.trans), 1)
        self.assertEqual(job["prompt"], "MASTER-OUT")
        self.assertEqual(job["skill"], "silver")

    def test_short_sentence_is_not_a_special_case(self):
        """「换成白色毛衣」和长句走同一条路——不再有「短句算改词」的分支。"""
        self._run("换成白色毛衣")
        self._run("一个穿和服的男孩站在海边码头上等朋友，背景是傍晚泛红的云层")
        self.assertEqual(len(self.trans), 2)
        self.assertEqual(self.rev, [])

    def test_english_tag_string_also_goes_through(self):
        """用户自己贴的英文串照样过一遍（跟 QQ 一样：AI 必须参与决策）。"""
        self._run("1girl, silver hair, white sweater")
        self.assertEqual(len(self.trans), 1)

    def test_ui_channel_wins_over_model(self):
        """界面选了 silver，模型想画 krea2 也不算数。"""
        self.tr_result = {"skill": "krea2-jiguang", "prompt": "X"}
        job = self._run("用极光画个赛博城市", skill="silver")
        self.assertEqual(job["skill"], "silver")

    def test_no_ui_channel_falls_back_to_model(self):
        self.tr_result = {"skill": "krea2-jiguang", "prompt": "X"}
        job = self._run("用极光画个赛博城市", skill="")
        self.assertEqual(job["skill"], "krea2-jiguang")

    def test_no_channel_anywhere_asks_for_one(self):
        self.tr_result = {"skill": "", "prompt": "X"}
        with self.assertRaises(RuntimeError) as ctx:
            self._run("画只猫", skill="")
        self.assertIn("画风", str(ctx.exception))
        self.assertEqual(self.enq, [])


class ImageToImageTest(_Base):
    """上传图 / 画同款：图 + 话一起交给多模态模型，**一次**调用出词。

    2026-11-27 用户拍板。以前走 `_web_revise`（先识图、再改词、还要模型判
    要不要垫图），之后又可能有第二次转译；图在第一步之后就只剩一段中文。
    现在的口径：`_tags_with_request(images, text)` 一次搞定。
    """

    def test_upload_goes_through_one_shot(self):
        job = self._run("把这个人换到我旁边", images=["data:image/png;base64,AA"])
        self.assertEqual(len(self.tags), 1)
        self.assertEqual(self.tags[0]["request"], "把这个人换到我旁边")
        self.assertEqual(self.rev, [])          # 不再走 QQ 那条改图管道
        self.assertEqual(self.trans, [])        # 也不再多转一次
        self.assertEqual(job["prompt"], "TAGS-OUT")

    def test_no_source_image_attached(self):
        """图只是「看」，产出的是纯文生图——不把图当垫图塞进工作流。"""
        job = self._run("照着画", images=["data:image/png;base64,AA"])
        self.assertEqual(job["source_images"], [])

    def test_ui_channel_wins_over_model(self):
        job = self._run("改一下", skill="silver",
                        images=["data:image/png;base64,AA"])
        self.assertEqual(job["skill"], "silver")

    def test_read_failure_asks_to_retry(self):
        """图没读出来（视觉调用失败）要说人话重发，不静默按文字画。"""
        self.tag_result = ""
        with self.assertRaises(RuntimeError) as ctx:
            self._run("照着画", images=["data:image/png;base64,AA"])
        self.assertIn("没读出来", str(ctx.exception))
        self.assertEqual(self.enq, [])

    def test_cr_refs_pass_through(self):
        self._run("改一下", images=["data:image/png;base64,AA"],
                  refs=["cr_abc", "not-a-ref"])
        self.assertEqual(self.enq[0]["refs"], ["cr_abc"])


class NoAnswerTest(_Base):
    """模型的两类「没给图」：判不画 / 没交作业。"""

    def test_reply_raises_web_reply(self):
        self.tr_result = {"reply": "这句没说要画什么"}
        with self.assertRaises(R._WebReply) as ctx:
            self._run("嗯嗯")
        self.assertEqual(ctx.exception.reply, "这句没说要画什么")
        self.assertEqual(self.enq, [])

    def test_empty_result_asks_to_retry(self):
        """空返回 / JSON 没解析出来 = 没交作业，提示重来，不硬画。"""
        self.tr_result = None
        with self.assertRaises(RuntimeError) as ctx:
            self._run("画只猫")
        self.assertIn("再发", str(ctx.exception))
        self.assertEqual(self.enq, [])

    def test_reply_with_prompt_still_paints(self):
        """既有 prompt 又有 reply → 要画（reply 只是附带话），不能当成不画。"""
        self.tr_result = {"skill": "silver", "prompt": "P", "reply": "画了"}
        job = self._run("画只猫")
        self.assertEqual(job["prompt"], "P")

    def test_busy_surfaces_as_runtime_error(self):
        with mock.patch.object(
                R, "_enqueue_job",
                new=lambda spec, owner, user_text="", lowres_on=False:
                    (None, "同时最多画 5 张")):
            with self.assertRaises(RuntimeError) as ctx:
                self._run("画只猫")
        self.assertIn("5 张", str(ctx.exception))


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


class BuildI2iTest(unittest.TestCase):
    """图生图骨架：占位名要先填进去，真名由 worker 上传完回填。"""

    def test_data_url_decodes(self):
        raw = R._data_url_bytes(
            "data:image/png;base64," + "aGVsbG8=")
        self.assertEqual(raw, b"hello")

    def test_bad_data_url_rejected(self):
        for bad in ("", "http://x/y.png", "data:image/png,abc"):
            with self.assertRaises(RuntimeError):
                R._data_url_bytes(bad)

    def test_placeholder_matches_worker(self):
        """两处占位串必须同值，否则 worker 回填时替换不到。"""
        from app.tools.normal import generate_image as gi
        self.assertEqual(R._I2I_PLACEHOLDER, gi._I2I_SRC_PLACEHOLDER)

    def test_channel_without_i2i_skeleton_raises(self):
        with self.assertRaises(RuntimeError) as ctx:
            R._build_i2i({"skill": "moss", "prompt": "x", "seed": 1,
                          "source_images": ["data:image/png;base64,AA"]})
        self.assertIn("图生图", str(ctx.exception))


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
        self.assertEqual(job["source_images"], [])
        R._JOBS.pop(job["id"], None)

    def test_lowres_keyword_matches_signature(self):
        """`_web_decide` 传的关键字必须真在 `_enqueue_job` 的签名里。

        2026-11-26 实跑抓到：`_web_decide` 写 `lowres=...`、函数签名是
        `lowres_on`，工作台每点一次「画」都是 `TypeError` → 500。
        单元测试全绿，因为桩函数自己写了 `**k` / 少写的参数**吞掉了这个错**。
        这条不靠桩，直接对签名。
        """
        import inspect
        params = inspect.signature(R._enqueue_job).parameters
        self.assertIn("lowres_on", params)
        self.assertNotIn("lowres", params)

        # 真正带这个关键字调一次，签名对不上会 TypeError
        with mock.patch.object(R, "_run", lambda job: None):
            job, err = R._enqueue_job(
                {"skill": "silver", "prompt": "x"}, owner="1",
                lowres_on=True)
        self.assertIsNone(err)
        self.assertTrue(job["lowres"])
        R._JOBS.pop(job["id"], None)


if __name__ == "__main__":
    unittest.main()
