"""图生图**没有关键词硬闸**（2026-10-06 用户拍板把三处硬编码全拆了）。

用户原话：「什么出现图生图、垫图、改图、重绘，这个硬编码给我去掉就行了，改成让
AI 它自己去判断」「没有兜底没有问题。因为大模型它自己就知道怎么去做，我们不需要
代码去给它硬兜底的。」

拆掉的是同一份词表的三个消费点：
  · `generate_image._i2i_gate` —— 模型传了 `source_image` 但本轮原话没命中机制词
    就当场拒；
  · `generate_image._i2i_force` —— 原话命中而模型没传，代码抢在模型前面补上垫图；
  · `direct_gen._i2i_intent` + `_redraw_capable` —— 引用图轮扫原话认机制词，再
    因为「垫图只认动漫档」把渠道**静默降回 anima_clear**（「一说图生图就掉回
    anima」那个怪事的病根）。

于是这里钉四件事：
  1. 那些符号**不许回来**——回来了就是又拿关键词替模型判了一次；
  2. 模型传了 `source_image` 就走图生图，**本轮原话里有没有「图生图」三个字一样**；
  3. 唯一还留着的判据是**能力**：默认渠道 silver 没有垫图骨架，被误拿去垫图要
     当场报错并指向 qwen，不许静默换个渠道硬垫；
  4. 「什么时候该传」只写在文案里（工具描述 / `_TOOL_HINTS` / direct_gen 两个
     模板），口径是明说「qwen 图生图 + 怎么改」才填，**引用一张图本身不是垫图
     要求**；动漫档重绘那条路已经从文案里撤掉，后端 `_I2I_SKILLS` 照旧放行。

（文件名还叫 `test_image_i2i_gate.py`：里面已经没有任何 gate 了。改名要删旧
文件，按规矩得先经用户点头，所以先把名字留着、内容换成新契约。）
"""

import os
import unittest
from unittest import mock

from app import direct_gen, qq_api
from app.tools.normal import generate_image as gi


class HardCodesGoneTest(unittest.TestCase):
    """三道关键词硬编码的符号必须一个都不剩（回来了就是又替模型判了一次）。"""

    def test_generate_image_carries_no_keyword_gate(self):
        for name in ("_i2i_gate", "_i2i_force", "_I2I_EXPLICIT_RE",
                     "_I2I_ASKING_RE", "_I2I_NO_INTENT_NOTE"):
            self.assertFalse(hasattr(gi, name), name + " 又回来了")

    def test_direct_gen_carries_no_keyword_router(self):
        """`_i2i_intent` 认机制词、`_redraw_capable` 再按「垫图只认动漫档」
        静默降档——这一对就是「一说图生图就掉回 anima」的成因。"""
        for name in ("_i2i_intent", "_redraw_capable"):
            self.assertFalse(hasattr(direct_gen, name), name + " 又回来了")


class NoKeywordGateTest(unittest.TestCase):
    """传了 `source_image` 就走图生图：本轮原话里有没有「图生图」三个字一样。"""

    def setUp(self):
        qq_api.clear_context()
        self.addCleanup(qq_api.clear_context)

    def _call(self, text, **kw):
        """`load_skill` 当探针：返回 None 时那句「找不到 Skill」就是「没被拦」
        ——真被拦的话根本走不到读 skill 这一步。"""
        with mock.patch.object(gi, "is_cancelled", lambda: False), \
                mock.patch.object(gi, "_qq_gate", lambda: None), \
                mock.patch.object(gi.char_guard, "check", lambda p: None), \
                mock.patch.object(gi, "load_skill", lambda skill: None):
            if text is not None:
                qq_api.bind_context("group_9", "group", "9", user_text=text)
            return gi._generate_image(prompt="change her coat to red", **kw)

    def test_qwen_i2i_passes_without_the_mechanism_word(self):
        """以前这条是「当场拒」（原话没命中词表）。现在传了就垫：该不该传是
        模型的事，代码不越权。"""
        out = self._call("这张真好看", skill="qwen_image_v1", source_image="1")
        self.assertIn("qwen_image_v1", out)          # 「找不到 Skill」= 过了
        self.assertNotIn("不支持图生图", out)

    def test_a_turn_with_no_text_at_all_passes(self):
        """一个字没打的轮（只引用了张图）也不再是拒绝理由。"""
        out = self._call("", skill="qwen_image_v1", source_image="1")
        self.assertIn("qwen_image_v1", out)

    def test_unbound_turn_passes_too(self):
        """网页端 / 单测直接调工具：连「本轮原话」这个证据源都不存在。"""
        out = self._call(None, skill="qwen_image_v1", source_image="1")
        self.assertIn("qwen_image_v1", out)

    def test_text_to_image_still_lands_on_the_default_channel(self):
        """不传 source_image 就跟这条路无关——文生图照旧落到默认渠道。"""
        out = self._call("这张真好看")
        self.assertIn(gi.T2I_DEFAULT_SKILL, out)


class CapabilityRefusalTest(unittest.TestCase):
    """唯一还留着的判据 = 那个渠道**有没有**垫图骨架，跟原话无关。

    默认渠道 `silver` 是文生图专用（没有 `workflow_i2i.json`）。模型把
    `source_image` 配到它身上时要**当场报错并指向 qwen**，不许静默换一个能垫图
    的渠道硬垫——那是「越垫越糊」的来源，也是从前「一说图生图就掉回 anima」的
    成因（那时是 `_redraw_capable` 在背后偷偷降档）。
    """

    def setUp(self):
        qq_api.clear_context()
        self.addCleanup(qq_api.clear_context)

    def _refuse(self, **kw):
        """源图一读就炸：拒绝必须发生在任何下游动作之前。"""
        boom = mock.Mock(side_effect=AssertionError("不该读源图 / 往 ComfyUI 传图"))
        with mock.patch.object(gi, "is_cancelled", lambda: False), \
                mock.patch.object(gi, "_qq_gate", lambda: None), \
                mock.patch.object(gi.char_guard, "check", lambda p: None), \
                mock.patch.object(gi, "load_skill", lambda skill: {
                    "workflow": {"9": {"class_type": "EmptyLatentImage",
                                       "inputs": {"width": 1024,
                                                  "height": 1024}}},
                    "path": os.path.join("skills", skill)}), \
                mock.patch.object(gi.comfy_src, "resolve", boom), \
                mock.patch.object(gi.comfy_src, "upload", boom):
            qq_api.bind_context("group_9", "group", "9", user_text="这张真好看")
            return gi._generate_image(prompt="change her coat to red", **kw)

    def test_default_channel_refuses_and_names_qwen(self):
        out = self._refuse(source_image="1")            # 不点名 = 落默认渠道
        self.assertIn("不支持图生图", out)
        self.assertIn(gi.T2I_DEFAULT_SKILL, out)        # 报的是实际被拿来的那个渠道
        self.assertIn("qwen_image_v1", out)             # 给出去路
        self.assertIn("不要换个渠道硬垫", out)

    def test_naming_the_default_channel_refuses_the_same(self):
        out = self._refuse(skill="silver", source_image="1")
        self.assertIn("silver 不支持图生图", out)

    def test_a_capable_channel_reaches_the_i2i_branch(self):
        """正控制：会垫图的渠道在「没说要图生图」的轮里照样垫得成。

        一路走到 ComfyUI 探活那一步（这里让它报「没在线」），中间没有一句
        「不支持图生图」——拒绝话术只跟**能力**挂钩，不再跟关键词挂钩。
        """
        with mock.patch.object(gi, "is_cancelled", lambda: False), \
                mock.patch.object(gi, "_qq_gate", lambda: None), \
                mock.patch.object(gi.char_guard, "check", lambda p: None), \
                mock.patch.object(gi, "load_skill", lambda skill: {
                    "workflow": {"9": {"class_type": "EmptyLatentImage",
                                       "inputs": {"width": 728,
                                                  "height": 1024}}},
                    "path": os.path.join("skills", skill)}), \
                mock.patch.object(gi, "load_workflow",
                                  lambda p: {"30": {"class_type": "LoadImage"}}), \
                mock.patch.object(gi.comfy_src, "resolve",
                                  lambda spec: (b"RAW", "（垫图：那张）")), \
                mock.patch.object(gi.comfy_src, "fit",
                                  lambda raw, max_side=None: (b"FIT", (728, 1024))), \
                mock.patch.object(gi.comfy_src, "upload",
                                  lambda raw: "i2isrc_x.png"), \
                mock.patch.object(gi.image_jobs, "comfy_alive", lambda: False):
            qq_api.bind_context("group_9", "group", "9", user_text="这张真好看")
            out = gi._generate_image(prompt="change her coat to red",
                                     skill="anima_clear", source_image="1")
        self.assertNotIn("不支持图生图", out)
        self.assertIn("ComfyUI 现在没在线", out)


class PromptSurfaceTest(unittest.TestCase):
    """闸拆了，口径就得在文案里说死——四处文案跟「交 AI 判」这条边界一起改。

    钉的是**新口径**（明说「qwen 图生图 + 怎么改」才填 `source_image`；引用图
    本身不是垫图要求；图生图只有 qwen 一条路），以及**旧口径不许回来**：
    「两条入口」「默认走动漫重绘」那套是代码闸时代的产物，留着就是教模型去走
    一条已经撤掉的渠道。
    """

    @staticmethod
    def _flat(text):
        """`**` 的落点不该进断言（那是给模型看的强调，不是文案内容）。"""
        return text.replace("**", "")

    def _hints(self):
        from app.agent_prompt import _TOOL_HINTS
        return self._flat("\n".join(text for needs, text in _TOOL_HINTS
                                    if "generate_image" in needs))

    def _param(self, name):
        props = gi.tool["parameters"]["properties"]
        return self._flat(props[name]["description"])

    def test_hints_teach_the_one_path(self):
        h = self._hints()
        self.assertTrue(h, "generate_image 的使用提示不见了")
        for key in ("`source_image` 默认一律不传",
                    # 2026-10-07 加了 qwen-hd（4x 超清）后，提示词从「只有一条路」
                    # 改成了「qwen 系两条路」；这条断言当时没跟着改，一直是红的。
                    "图生图只有 qwen 系两条路",
                    "`skill=qwen-hd`",
                    "引用一张图本身永远不是垫图要求",
                    "动漫档（anima_* / hd_*）的重绘已从用法里撤掉",
                    "1~2 分钟"):
            self.assertIn(key, h)

    def test_source_image_param_teaches_the_same(self):
        p = self._param("source_image")
        for key in ("默认不传", "传了就必须配 `skill=qwen_image_v1`",
                    "引用一张图本身永远不是垫图要求", "用法上已撤掉"):
            self.assertIn(key, p)

    def test_direct_gen_templates_teach_the_same(self):
        for name in ("_MASTER_TEMPLATE", "_REVISE_TEMPLATE"):
            text = self._flat(getattr(direct_gen, name))
            self.assertIn("【图生图：默认不做", text, name)
            self.assertIn("qwen 图生图", text, name)

    def test_retired_wording_stays_out(self):
        surfaces = (self._hints(), self._param("source_image"),
                    self._flat(direct_gen.GUIDE_TEXT))
        for text in surfaces:
            for dead in ("两条入口", "默认走动漫重绘", "改图最强",
                         "本机改图（图生图）最强的渠道"):
                self.assertNotIn(dead, text)


class BackendUnchangedTest(unittest.TestCase):
    """「关掉 Anima 的图生图」= 提示词里不写它，**不是**后端把能力删了。

    用户原话：「关掉其实很简单，就提示词里面跟 AI 说一下就行了，你不需要后端
    这边去关，你只要提示词不要有这个 Anima 就行了。」所以这里钉住后端没被顺手
    拆掉：动漫档的垫图骨架照旧在放行名单里，重绘强度还是那个实测值。
    """

    def test_anima_i2i_capability_is_still_declared(self):
        self.assertIn("anima_clear", gi._I2I_SKILLS)
        self.assertIn("hd_2_gloss", gi._I2I_SKILLS)
        self.assertIn("qwen_image_v1", gi._I2I_SKILLS)

    def test_hd_3_is_still_out(self):
        """三档不给图生图这条**没**跟着放宽：它本身就 150 秒起。"""
        self.assertNotIn("hd_3_clear", gi._I2I_SKILLS)

    def test_redraw_strength_unchanged(self):
        self.assertEqual(0.6, gi.I2I_DENOISE)


if __name__ == "__main__":
    unittest.main()
