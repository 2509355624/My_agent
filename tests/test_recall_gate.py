"""反推闸门：对方要「看图反推提示词」时代码直接调识图接管回复。

背景（2026-10-04 用户实测）：发图说「反推提示词」，9B 经常把**历史里上次
生图的提示词**搬出来交差，给了明确指令也时灵时不灵。修法沿用守卫家族的
方法论——判据代码判，模型不参与（`app/recall_gate.py`）。

这几条钉的是**分流本身**：

- 反推意图 + 本轮有图 + 不是自家 HT 图 → 接管（返回文本）；
- 普通生图请求（「画一张黑丝」「用这个提示词再画一张」）绝不接管——
  闸门误吞生图请求 = 白白不干活，这条是最不能破的红线；
- 自家 HT 图放行（账本里存着当时真正的提示词，比看图现推准）；
- 识图失败放行正常流程，闸门绝不把轮子弄丢。
"""

import unittest
from unittest import mock

from app import recall_gate

_DU = "data:image/jpeg;base64,AAA"


class IntentTest(unittest.TestCase):
    """意图判定：命中什么该接管、什么绝不能接管。"""

    def test_explicit_reverse_verbs_hit(self):
        for t in ("反推一下这张图的提示词", "帮我提取提示词",
                  "还原这张图的提示词", "把这张图转成提示词",
                  "图生文一下", "识别这张图"):
            self.assertIsNotNone(recall_gate._intent(t), t)

    def test_noun_plus_ask_combo_no_longer_hits(self):
        """「提示词/种子 + 给我/是什么」的组合判据**已删**（2026-10-06）。

        它跟已删除的 `_prompt_ask_guard` 是同一套判据：只认关键词，把
        「引用这张图，手势改成抓手，角色换成花火」这类**改图请求**吞成
        「反推提示词」。现在只认上面那些**显式**反推动词——对方没明说要反推，
        就交回模型自己判。
        """
        for t in ("这个的提示词是什么", "提示词给我", "这张的种子是多少",
                  "停下来，给我提示词"):
            self.assertIsNone(recall_gate._intent(t), t)

    def test_normal_generation_never_hits(self):
        """红线：生图请求不能被反推闸门吞掉。"""
        for t in ("画一张黑丝", "再来一张，同一个种子",
                  "用这个提示词再画一张", "把提示词改成白发然后重画"):
            self.assertIsNone(recall_gate._intent(t), t)

    def test_reverse_word_without_image_context_still_judged_on_text(self):
        """意图判定只看文本本身——有没有图是 decide 的事，分层别混。"""
        self.assertIsNotNone(recall_gate._intent("反推"))

    def test_empty_text_is_safe(self):
        self.assertIsNone(recall_gate._intent(""))
        self.assertIsNone(recall_gate._intent(None))


class DecideTest(unittest.TestCase):
    """decide 的三条件与放行分支。"""

    def test_takes_over_when_intent_and_image(self):
        with mock.patch("app.vision.describe",
                               return_value="1girl, white hair, smile\n白发的女孩在笑"):
            out = recall_gate.decide("反推提示词", "反推提示词", [_DU])
        self.assertIsNotNone(out)
        self.assertIn("1girl, white hair", out)

    def test_no_image_lets_it_through(self):
        """没图没得反推——放行正常流程（模型会问对方要图）。"""
        self.assertIsNone(recall_gate.decide("反推提示词", "反推提示词", []))
        self.assertIsNone(recall_gate.decide("反推提示词", "反推提示词", None))

    def test_no_intent_lets_it_through(self):
        with mock.patch("app.vision.describe") as desc:
            self.assertIsNone(recall_gate.decide("画一张黑丝", "画一张黑丝", [_DU]))
        self.assertFalse(desc.called)

    def test_own_image_with_ht_tag_lets_it_through(self):
        """引用块里有 HT 图号 = 自家图，账本 recall_image 才是权威，不接管。"""
        full = "HT-20261001-081132-772 · 1024×1536 · anima_soft\n反推提示词"
        with mock.patch("app.vision.describe") as desc:
            self.assertIsNone(recall_gate.decide("反推提示词", full, [_DU]))
        self.assertFalse(desc.called)

    def test_describe_failure_falls_through(self):
        """识图挂了不能把整轮弄丢——返回 None 放行，正常流程有自己的兜底。"""
        with mock.patch("app.vision.describe",
                               side_effect=RuntimeError("llama 睡死了")):
            self.assertIsNone(recall_gate.decide("反推提示词", "反推提示词", [_DU]))

    def test_describe_returns_junk_is_fall_through_too(self):
        with mock.patch("app.vision.describe",
                               return_value="   "):
            self.assertIsNone(recall_gate.decide("反推提示词", "反推提示词", [_DU]))

    def test_multiple_images_described_each(self):
        """两张图都反推，带（第 N 张）标记；钉住别只看第一张。"""
        with mock.patch("app.vision.describe",
                               return_value="1girl\n女孩") as desc:
            out = recall_gate.decide("这两张的提示词都反推一下",
                                     "这两张的提示词都反推一下", [_DU, _DU])
        self.assertEqual(desc.call_count, 2)
        self.assertIn("（第 1 张）", out)
        self.assertIn("（第 2 张）", out)

    def test_voluntary_turn_never_takes_over(self):
        """主动接话轮的「原话」是机器人自己的提示词，不是用户需求——不判。"""
        with mock.patch("app.vision.describe") as desc:
            self.assertIsNone(recall_gate.decide(
                "反推提示词", "反推提示词", [_DU], voluntary=True))
        self.assertFalse(desc.called)

    def test_uses_the_reverse_prompt_not_the_default_caption(self):
        """识图必须用反推专用指令整段替换，不能混进默认的「看图说话」模板。"""
        with mock.patch("app.vision.describe",
                               return_value="x") as desc:
            recall_gate.decide("反推提示词", "反推提示词", [_DU])
        prompt = desc.call_args.kwargs.get("prompt") or desc.call_args[1].get("prompt")
        self.assertIn("反推", prompt)
        self.assertIn("Danbooru", prompt)


if __name__ == "__main__":
    unittest.main()
