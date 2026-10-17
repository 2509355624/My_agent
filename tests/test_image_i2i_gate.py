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
  3. 没有 i2i 骨架的渠道（如 `nffa`）被误拿去垫图 → **降级文生图**（2026-10-11
     起；原先是当场报错并指向 qwen）。不许静默换个渠道硬垫。
     ⚠️ 2026-10-10 起**默认渠道 `silver` 不再落在这条上**——它进了锁构图组
     （`_I2I_LLLITE_SKILLS`），所以「引用图 + 不点名渠道」现在能直接跑通；
  4. 「什么时候该传」只写在文案里（工具描述 / `_TOOL_HINTS` / direct_gen 两个
     模板）。2026-10-11 口径再翻转：**明说「图生图 / 照着画 / 复刻」才填
     `source_image`**（渠道不变，LLLite 锁住构图）；**引用 / 附图 + 只是提需求
     = 照这张改词重画、不垫图**。只有「在这张图上改」那条路依旧只给 qwen。
     动漫档的**垫图重绘**已经从文案里撤掉，但后端 `_I2I_SKILLS` 照旧放行。

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
    """唯一还留着的判据 = 那个渠道**有没有** i2i 骨架，跟原话无关。

    ⚠️ 2026-10-10 起**默认渠道 `silver` 不再拒收**：它进了 `_I2I_LLLITE_SKILLS`，
    走的是锁构图那条路。所以拒收样例换成 `nffa`（文生图专用、没有骨架）。

    机制本身没变：模型把 `source_image` 配到没有骨架的渠道上时要**当场报错并指向
    qwen**，不许静默换一个能垫图的渠道硬垫——那是「越垫越糊」的来源，也是从前
    「一说图生图就掉回 anima」的成因（那时是 `_redraw_capable` 在背后偷偷降档）。
    """

    def setUp(self):
        qq_api.clear_context()
        self.addCleanup(qq_api.clear_context)

    def _call(self, boom=True, **kw):
        """`boom=True` 时源图一读就炸：拒绝必须发生在任何下游动作之前。"""
        fail = mock.Mock(side_effect=AssertionError("不该读源图 / 往 ComfyUI 传图"))
        with mock.patch.object(gi, "is_cancelled", lambda: False), \
                mock.patch.object(gi, "_qq_gate", lambda: None), \
                mock.patch.object(gi.char_guard, "check", lambda p: None), \
                mock.patch.object(gi, "load_skill", lambda skill: {
                    "workflow": {"9": {"class_type": "EmptyLatentImage",
                                       "inputs": {"width": 1024,
                                                  "height": 1024}}},
                    "path": os.path.join("skills", skill)}), \
                mock.patch.object(gi.comfy_src, "resolve",
                                  fail if boom else (lambda spec: (b"RAW", "那张"))), \
                mock.patch.object(gi.comfy_src, "fit",
                                  lambda raw, max_side=None: (b"FIT", (1024, 1536))), \
                mock.patch.object(gi.comfy_src, "upload",
                                  fail if boom else (lambda raw: "i2isrc_x.png")), \
                mock.patch.object(gi.image_jobs, "comfy_alive", lambda: False):
            qq_api.bind_context("group_9", "group", "9", user_text="这张真好看")
            return gi._generate_image(prompt="change her coat to red", **kw)

    def test_a_channel_without_a_skeleton_degrades_and_names_qwen(self):
        """没有骨架的渠道**不再拒**：丢掉垫图、按文生图重画。

        2026-10-11 改的（原先钉的是「nffa 不支持图生图」那句拒收）。垫不垫图
        是模型判的，判错一次就让对方白等一轮、还得自己补一句「文生图」重发——
        233 群实录：`anima-real` + 引用图被垫了图，弹「不支持图生图」。
        （降级说明里仍指向 qwen，那条断言在 `test_image_i2i.py`。）

        `boom=True`（默认）：源图一读就炸。降级必须发生在读源图**之前**，
        所以能走到下游的「ComfyUI 没在线」就证明一步都没碰源图。
        """
        out = self._call(skill="nffa", source_image="1")
        self.assertNotIn("不支持图生图", out)
        # 走到下游了（降级没拦下这单）；`boom=True` 同时保证源图一步都没读
        self.assertIn("ComfyUI 现在没在线", out)

    def test_the_default_channel_no_longer_refuses(self):
        """正控制：`silver` 有锁构图骨架了，垫图请求要一路走到下游。

        这里让 ComfyUI 探活返回「没在线」——那是**下游**的错误。源图读取 / 缩放 /
        上传都真的发生了（不再 boom），中间没有一句「不支持图生图」。
        """
        out = self._call(boom=False, skill="silver", source_image="1")
        self.assertNotIn("不支持图生图", out)
        self.assertIn("ComfyUI 现在没在线", out)

    def test_not_naming_a_channel_now_lands_on_a_capable_default(self):
        """不点名 → 落默认渠道 `silver`，而它现在**能**垫图。

        2026-10-10 之前这条断言的是「默认渠道拒收」。口径翻转了：默认渠道恰好
        就是最常用的锁构图渠道，所以「引用图 + 不点名渠道」能直接跑通。
        """
        out = self._call(boom=False, source_image="1")
        self.assertNotIn("不支持图生图", out)
        self.assertIn("ComfyUI 现在没在线", out)

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

    钉的是**新口径**（2026-10-11 再翻转：明说「图生图 / 照着画 / 复刻」才填
    `source_image`、渠道不用改；**引用 / 附图 + 只是提需求 = 照这张改词重画、
    不垫图**；只有「只改一处、其余原样」才走 qwen 系），以及**旧口径不许回来**：
    「两条入口」「默认走动漫重绘」「引用图要画就填」那套是旧口径的产物，
    留着就是教模型去垫一张人家没让垫的图。
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

    def test_hints_teach_both_paths(self):
        h = self._hints()
        self.assertTrue(h, "generate_image 的使用提示不见了")
        for key in ("`source_image` 明说「图生图」才传",
                    # 2026-10-17 用户拍板「图生图就走 qwen 这一条路」：
                    # 原渠道 / 点名渠道都没有时配 qwen，不再是「渠道不用改」
                    "两样都没有就配",
                    # 2026-10-11：引用 / 附图 + 只是提需求 = 改词重画，不垫图
                    "照这张改词重画",
                    "只是让你看图点评",
                    "没有骨架",
                    # 2026-10-10：口径翻转后 qwen 那条降级成「只改一处」的编辑路
                    "qwen 系两条路",
                    "`skill=qwen-hd`",
                    "动漫档（anima_* / hd_*）的垫图重绘已从用法里撤掉",
                    "1~2 分钟"):
            self.assertIn(key, h)
        # 旧口径不许回来
        self.assertNotIn("要画就填", h)

    def test_source_image_param_teaches_the_same(self):
        p = self._param("source_image")
        for key in ("明说「图生图」才填", "两样都没有就配",
                    "只是提需求", "沿用原渠道文生图重画",
                    "只改一处", "`skill=qwen_image_v1`",
                    "只是让你看图点评", "没有骨架", "用法上已撤掉"):
            self.assertIn(key, p)
        self.assertNotIn("要画就填", p)

    def test_direct_gen_templates_teach_the_same(self):
        for name in ("_MASTER_TEMPLATE", "_REVISE_TEMPLATE"):
            text = self._flat(getattr(direct_gen, name))
            self.assertIn("【图生图：明说才垫", text, name)
            # 新口径那半句：引用 / 附图 + 只提需求 = 改词重画，不垫图
            self.assertIn("照这张改词重画", text, name)
            self.assertNotIn("引用/附图 + 要画", text, name)
            self.assertIn("qwen 图生图", text, name)
        # 明说垫图时渠道怎么走，两份模板的落点不同：
        #   _MASTER_TEMPLATE 是**没图轮**，① 那句只是兜住「用户嘴上说图生图
        #   但没给图」，照旧「渠道不用改」；
        #   _REVISE_TEMPLATE 是**带图轮**，2026-10-17 起改成「沿用原渠道 /
        #   用户点名渠道，两样都没有（或原渠道没骨架）才落 qwen_image_v1」——
        #   用户拍板把 10-06 的「图生图只走 qwen 这一条路」改回来。
        self.assertIn("渠道不用改", self._flat(direct_gen._MASTER_TEMPLATE))
        revise = self._flat(direct_gen._REVISE_TEMPLATE)
        self.assertIn("沿用原渠道或用户点名的渠道", revise)
        self.assertIn("qwen_image_v1", revise)

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

    def test_hd_3_now_goes_through_the_lllite_route(self):
        """三档 2026-10-10 起给了 i2i——但走的是**锁构图**那条，不是垫图重绘。

        当年「不给」的理由（1.5× + 二段 10 步 ≈ 150 秒）是在 3060 笔记本上量的；
        5070 上实测 ~50 秒，用户明确要求上。`_I2I_ANIMA_SKILLS`（垫图重绘那 12 份）
        里**依旧没有**它——两组的骨架形状正好相反。
        """
        for style in ("clear", "soft", "gloss", "curvy"):
            skill = "hd_3_" + style
            self.assertIn(skill, gi._I2I_SKILLS)
            self.assertIn(skill, gi._I2I_LLLITE_SKILLS)
            self.assertNotIn(skill, gi._I2I_ANIMA_SKILLS)

    def test_redraw_strength_unchanged(self):
        """垫图重绘那 12 档的强度没动。**锁构图那 6 档不吃这个数**（denoise 写死 1）。"""
        self.assertEqual(0.6, gi.I2I_DENOISE)


if __name__ == "__main__":
    unittest.main()
