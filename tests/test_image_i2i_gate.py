"""图生图的触发硬闸 `generate_image._i2i_gate`（2026-10-02 用户拍板）。

用户的原话：「除非用户说到要图生图，不然就只是反推提示词生成；图生图没有明确
说明，AI 就不要使用。」这条光写在提示词里兜不住（2026-09-27 整条链路停用就是
因为模型「一看见引用图就往改图上想」），所以做成代码闸：**传了 `source_image`
但本轮对方自己打的那段话里没有改图的意思，当场拒，一个字节都不往 ComfyUI 送。**

这里钉三件事：
  1. 判据是**对方原话**，不是模型的判断——所以「没绑定 QQ 轮」（网页端、别的
     测试里直接调工具）必须放行，不能当成「没说要改」；
  2. 词表**不收裸指示代词**（「这张 / 这图 / 原图」）——引用图的话十句八句带
     「这张」，收进来等于给闸门开后门；
  3. 动漫档图生图**一条行为都没变**：闸只拦「没明说」，明说之后走哪条渠道是
     模型自己的事（qwen 不再是默认，靠的是文案 de-bias，不是代码）。
"""

import unittest
from unittest import mock

from app import qq_api
from app.tools.normal import generate_image as gi


class GateUnitTest(unittest.TestCase):
    """直接测判据本身，不碰任何下游。"""

    def setUp(self):
        qq_api.clear_context()
        self.addCleanup(qq_api.clear_context)

    def _bind(self, text):
        qq_api.bind_context("group_9", "group", "9", user_text=text)

    # ① 点名了机制（图生图/垫图/重绘/改图/修图/p图/i2i…）→ 放行
    EXPLICIT = (
        "图生图",
        "帮她垫个图",
        "垫这张图",
        "照这张重绘一遍",
        "帮我修一下图",
        "帮我p个图",
        "i2i 一下",
        "IMG2IMG",
        "图生图 把衣服换成jk",
    )
    # ② 只说改动内容 / 没说机制 / 只是引用了图 / 只要看 → 拦（10-05 收紧：
    # 「换成/去掉」这类动词句一律改提示词重新画，反复垫图会越改越糊）
    NOT_EXPLICIT = (
        "",                                # 只引用了一张图、一个字没打（他自己发图另算，见 OwnImagePassesTest）
        "看看这张图，她是什么发色",
        "这张真好看，谢谢你",
        "这张图的构图怎么样",
        "参考这个风格画一张新的",
        "照着画一张新的",
        "帮我写一份这张图的提示词",
        "这张是谁画的",
        "她改了什么？我没看出来",           # 「改」在聊剧情，不是改图
        "换我做头像吧",                     # 「换」是换话题，没要动这张图
        "her hair color? nice work",
        "把图里这个角色换成 XXX",
        "去掉她手里那把伞",
        "基于这张重新画一张",
        "背景改成海边",
        "给她换一件红色的外套",
        "改为夜景",
        "把衣服换成jk",
        "change her coat to red",
        "REMOVE the umbrella",
    )

    def test_explicit_requests_pass(self):
        for text in self.EXPLICIT:
            with self.subTest(text=text):
                self._bind(text)
                self.assertEqual(gi._i2i_gate(True), "")

    def test_bare_quotes_are_refused(self):
        for text in self.NOT_EXPLICIT:
            with self.subTest(text=text):
                self._bind(text)
                self.assertEqual(gi._i2i_gate(True), gi._I2I_NO_INTENT_NOTE)

    def test_refusal_note_gives_a_way_out(self):
        """话术三句都在：不画、改走反推提示词、拿不准就问一句。

        少了第 2 句模型会卡在「那这轮干什么」；少了第 3 句它会猜。
        """
        note = gi._I2I_NO_INTENT_NOTE
        for key in ("没有说要图生图", "没画任何东西", "反推提示词",
                    "去掉 source_image", "先回一句问"):
            self.assertIn(key, note)

    def test_not_i2i_never_gated(self):
        """没传 source_image 就跟这道闸无关——文生图一个字都没变。"""
        self._bind("")
        self.assertEqual(gi._i2i_gate(False), "")

    def test_unbound_turn_skips_the_gate(self):
        """不在 QQ 轮里（网页端 / 单元测试直接调工具）一律放行。

        `current_turn_text()` 的 None 和 "" 的区分是整个判据的地基：None 是
        「没有原话这个证据源」，不能读成「对方没说要改」。
        """
        qq_api.clear_context()
        self.assertIsNone(qq_api.current_turn_text())
        self.assertEqual(gi._i2i_gate(True), "")

    def test_word_list_carries_no_bare_demonstratives(self):
        """触发正则里不许出现「这张 / 这图 / 原图」这类裸指示代词。

        它们出现在**看图**的话里比出现在改图的话里还频繁，收进来后这道闸等于
        没有：对方只要引用图时说了「这张」，模型就能垫图。
        """
        pattern = gi._I2I_EXPLICIT_RE.pattern
        for word in ("这张", "这图", "原图", "这幅", "这个图", "那张",
                     "参考这张", "用这张", "拿这张"):
            self.assertNotIn(word, pattern)


class GenerateImageGateCallTest(unittest.TestCase):
    """闸在 `_generate_image` 的哪一道：得在**任何下游动作之前**，也在 NAI 分流之前。"""

    def setUp(self):
        qq_api.clear_context()
        self.addCleanup(qq_api.clear_context)

    def _patched(self):
        """下游全上雷：谁被碰到就说明闸没拦住（或拦晚了）。"""
        boom = mock.Mock(side_effect=AssertionError("不该走到这一步"))
        return (mock.patch.object(gi, "is_cancelled", lambda: False),
                mock.patch.object(gi, "_qq_gate", lambda: None),
                mock.patch.object(gi, "load_skill", boom),
                mock.patch.object(gi, "comfy_src",
                                  mock.Mock(resolve=boom)),
                mock.patch("app.agents.nai_allowed", boom),
                boom)

    def _call(self, text, **kw):
        ctxs = self._patched()
        with ctxs[0], ctxs[1], ctxs[2], ctxs[3], ctxs[4]:
            qq_api.bind_context("group_9", "group", "9", user_text=text)
            return gi._generate_image(prompt="1girl", **kw)

    def test_local_channel_refused_before_anything(self):
        """qwen + 垫图但对方没明说 → 拒，且 skill 文件 / 源图一个都没读。"""
        out = self._call("这张图真好看", skill="qwen_image_v1", source_image="1")
        self.assertEqual(out, gi._I2I_NO_INTENT_NOTE)

    def test_default_channel_refused_too(self):
        """不点名 skill（落动漫默认档）也一样拦：闸管的是垫不垫，不是走哪条。"""
        out = self._call("看看她什么发色", source_image="1")
        self.assertEqual(out, gi._I2I_NO_INTENT_NOTE)

    def test_nai_is_gated_by_the_same_rule(self):
        """云端 NAI 吃的是同一个 `source_image` 参数，不该因为走云就漏判。

        `nai_allowed` 被当成雷：拒绝话术先回来，就证明闸在 NAI 分流**前面**。
        """
        out = self._call("点评一下这张图", skill="nai", source_image="1")
        self.assertEqual(out, gi._I2I_NO_INTENT_NOTE)

    def test_explicit_request_passes_the_gate(self):
        """明说要改 → 放行，往下走到真正的图生图分支（这里用 load_skill 当探针）。"""
        with mock.patch.object(gi, "is_cancelled", lambda: False), \
                mock.patch.object(gi, "_qq_gate", lambda: None), \
                mock.patch.object(gi, "load_skill", lambda skill: None):
            qq_api.bind_context("group_9", "group", "9",
                                user_text="图生图 把她的外套换成红色")
            out = gi._generate_image(prompt="change her coat to red",
                                     skill="qwen_image_v1", source_image="1")
        self.assertNotEqual(out, gi._I2I_NO_INTENT_NOTE)
        self.assertIn("qwen_image_v1", out)          # 「找不到 Skill」= 已过闸

    def test_text_to_image_untouched(self):
        """文生图（没传 source_image）在「一个字没打」的轮里照旧过：闸不掺和。"""
        with mock.patch.object(gi, "is_cancelled", lambda: False), \
                mock.patch.object(gi, "_qq_gate", lambda: None), \
                mock.patch.object(gi, "load_skill", lambda skill: None):
            qq_api.bind_context("group_9", "group", "9", user_text="")
            out = gi._generate_image(prompt="1girl")
        self.assertNotEqual(out, gi._I2I_NO_INTENT_NOTE)
        self.assertIn(gi.T2I_DEFAULT_SKILL, out)


class PromptSurfaceTest(unittest.TestCase):
    """文案那道也得对上硬闸的口径，别一边拦一边教。"""

    def test_stable_prompt_teaches_the_gate(self):
        from app.agent_prompt import _TOOL_HINTS
        block = "\n".join(text for needs, text in _TOOL_HINTS
                          if "generate_image" in needs)
        self.assertTrue(block, "generate_image 的使用提示不见了")
        self.assertIn("图生图有**两条入口**", block)
        self.assertIn("对方这一轮自己发了图", block)
        self.assertIn("系统会把这次调用直接拒掉", block)   # 跟硬闸同一口径
        self.assertIn("图生图**默认走动漫重绘**", block)
        self.assertIn("1~2 分钟", block)
        # 旧口径（「改图就选 qwen / 本机最强」）不该再出现在提示词里
        self.assertNotIn("改图最强", block)
        self.assertNotIn("本机改图（图生图）最强的渠道", block)


class OwnImagePassesTest(unittest.TestCase):
    """**对方自己发了图 = 放行**（2026-10-04 用户拍板）。

    他的原话：「你把图生图的二次确认给我直接去除」「确认个gb，直接就跑了」。
    01:48~01:54 连着三轮没出图，就是因为这里只认原话——他发了图还得再补一句
    「图生图」。把素材递过来本身就是「改这张」，不该再要一次确认。
    """

    def setUp(self):
        qq_api.clear_context()
        self.addCleanup(qq_api.clear_context)

    def _bind(self, own=None, quoted=None, text=""):
        qq_api.bind_context("group_9", "group", "9",
                            quoted_images=quoted or [], own_images=own or [],
                            user_text=text)

    def test_own_image_passes_without_a_word(self):
        self._bind(own=["http://img/a.jpg"])
        self.assertEqual(gi._i2i_gate(True), "")

    def test_own_image_passes_even_with_a_look_only_sentence(self):
        """「这张真好看」平时是拦的；他自己发了图，那就是要改。"""
        self._bind(own=["http://img/a.jpg"], text="这张真好看")
        self.assertEqual(gi._i2i_gate(True), "")

    def test_lone_quote_is_still_blocked(self):
        """只**引用**别人的图、一个字没说要改 → 仍然拦（10-02 的保护没丢）。"""
        self._bind(quoted=["http://img/a.jpg"])
        self.assertNotEqual(gi._i2i_gate(True), "")


if __name__ == "__main__":
    unittest.main()
