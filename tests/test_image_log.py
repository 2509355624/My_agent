"""生图编号（app/image_log.py）的格式约束 + 编号→提示词账本。

最要紧的一条是 **编号必须原样活过 `qq_api.to_qq_text`**：发图时它是 caption，
会先过一遍 Markdown 降级——行首的 `-`/`*`/`+` 会被当列表符剥掉、行首 `#`
会被当标题剥掉、`__x__` 会被当粗体剥掉。编号要是被吃掉一个字符，正则就再也
匹配不上，整条「引用 → 查账本」的路就断了，而且断得**很安静**（群里看着一切
正常，只是模型永远查不到）。所以这里用一条测试把这个约束钉死。
"""

import os
import tempfile
import time
import unittest

import app.config as config
from app import image_log
from app import qq_api


class StateIsolationTest(unittest.TestCase):
    """跑测试不许往真实 state/ 里灌假记录（2026-10-03）。

    实测：全量测试会把脚本里的 "a cat" 图写进真实 state/image_log.jsonl，
    累计 80 行（10-01 起）——对账时全是噪声。模块默认路径已改由
    config.state_path 兜住，这条测试钉住它别再退回去。
    """

    def test_default_path_is_not_the_real_ledger(self):
        # setUp 里 no patch 时读的是模块原始常量，这里直接看它指向哪
        self.assertNotEqual(
            os.path.dirname(image_log.PATH),
            os.path.join(config.BASE_DIR, "state"),
            "测试进程的账本路径落在真实 state/ 下了，跑测试会污染真实数据")


class NewTagTest(unittest.TestCase):
    """编号长什么样。"""

    def test_shape(self):
        self.assertRegex(image_log.new_tag(), r"^HT-\d{8}-\d{6}-\d{3}$")

    def test_fixed_timestamp_is_deterministic(self):
        ts = time.mktime((2026, 10, 1, 7, 41, 12, 0, 0, 0)) + 0.384
        self.assertEqual(image_log.new_tag(ts), "HT-20261001-074112-384")

    def test_two_tags_a_second_apart_differ(self):
        a = image_log.new_tag(1000.0)
        b = image_log.new_tag(1001.0)
        self.assertNotEqual(a, b)

    def test_two_tags_in_the_same_second_still_differ(self):
        """毫秒那段的意义：同一秒里连开两张必须还是两个号。

        入队是瞬间的，同一秒来两张完全可能（一个群连点，或两个群各来一单）。
        没有毫秒的话这两张会拿到同一个号，账本里后一张会把前一张顶掉。
        """
        a = image_log.new_tag(1000.0)
        b = image_log.new_tag(1000.5)
        self.assertNotEqual(a, b)
        self.assertEqual(a[:16], b[:16])      # 只差毫秒那段

    def test_prefix_starts_with_a_letter(self):
        """前缀必须以字母开头：否则行首会被 to_qq_text 当成列表符 / 标题。

        改成 `-HT` 会被 `_BULLET_RE` 剥掉短横、改成 `#HT` 会被 `_HEADING_RE`
        剥掉井号——两种都会让编号在到达群聊之前就变形。
        """
        self.assertTrue(image_log.TAG_PREFIX[:1].isalpha(),
                        "编号前缀必须以字母开头，见 qq_api.to_qq_text")

    def test_prefix_has_no_markdown_metacharacters(self):
        for ch in "_*`#<>":
            self.assertNotIn(ch, image_log.TAG_PREFIX)


class ToQqTextTest(unittest.TestCase):
    """编号必须活着走出 Markdown 降级。"""

    def test_survives_stripping(self):
        tag = image_log.new_tag()
        self.assertEqual(qq_api.to_qq_text(tag), tag)

    def test_survives_inside_a_quote_block(self):
        """真实形态是「引用别人的消息 + 编号」一整段，不是光秃秃一个编号。"""
        tag = image_log.new_tag()
        body = "[引用 胡桃桃 的消息] " + tag
        self.assertIn(tag, qq_api.to_qq_text(body))

    def test_survives_next_to_the_image_placeholder(self):
        """发图那条消息的真实形态：编号 + 图片（解析出来是编号 + [图片]）。"""
        tag = image_log.new_tag()
        body = tag + "[图片]"
        self.assertIn(tag, qq_api.to_qq_text(body))

    def test_the_full_caption_keeps_the_tag_intact(self):
        """真实 caption 是「编号 · 分辨率 · 渠道」——后面两项不能把编号挤变形，
        也不能让正则抠不出来（群友引用的是这一整行）。"""
        tag = image_log.new_tag()
        body = "%s · 1024×1536 · anima_soft" % tag
        self.assertEqual(image_log.find_tags(qq_api.to_qq_text(body)), [tag])

    def test_the_seed_segment_survives_too(self):
        """种子上 caption 就得算一条通路：降级后那串数字还要**读得出来**。

        编号有正则兜底，种子没有——它靠模型直接读那一行。所以这里盯两件事：
        渠道名里的 `_` 没被当成 emphasis 吃掉（anima_soft 原样），数字没被拆散。
        """
        tag = image_log.new_tag()
        body = "%s · 1024×1536 · anima_soft · seed 4100493889" % tag
        out = qq_api.to_qq_text(body)
        self.assertEqual(image_log.find_tags(out), [tag])
        self.assertIn("anima_soft", out)
        self.assertIn("seed 4100493889", out)


class FindTagsTest(unittest.TestCase):
    """从引用回来的正文里把编号抠出来。"""

    def test_pulls_the_tag_out_of_a_quote(self):
        text = "[引用 胡桃桃 的消息] HT-20261001-074112-384"
        self.assertEqual(image_log.find_tags(text), ["HT-20261001-074112-384"])

    def test_finds_several_and_keeps_order(self):
        text = "对比 HT-20261001-074112-384 和 HT-20261001-080500-017 这两张"
        self.assertEqual(image_log.find_tags(text),
                         ["HT-20261001-074112-384", "HT-20261001-080500-017"])

    def test_dedupes(self):
        text = "HT-20261001-074112-384 就是 HT-20261001-074112-384"
        self.assertEqual(image_log.find_tags(text), ["HT-20261001-074112-384"])

    def test_no_tag_means_empty(self):
        for text in ("", None, "这张图好看", "20261001 074112 384"):
            self.assertEqual(image_log.find_tags(text), [])

    def test_plain_numbers_are_not_tags(self):
        """光有日期时间不算编号——必须有前缀，否则群里任何长数字都会误命中。"""
        self.assertEqual(image_log.find_tags("20261001-074112-384"), [])

    def test_a_real_tag_round_trips(self):
        """端到端：真生成一个，写进「引用正文」，还能原样抠回来。"""
        tag = image_log.new_tag()
        self.assertEqual(image_log.find_tags("[引用 胡桃桃 的消息] " + tag),
                         [tag])


class LedgerTest(unittest.TestCase):
    """账本：编号 → 提示词。

    单开一份文件而不是塞进会话历史，是因为历史会被 trim_history 到预算就
    「整段摘要替换」，逐字提示词在那一步就没了。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._old = image_log.PATH
        image_log.PATH = os.path.join(self.tmp.name, "image_log.jsonl")
        self.addCleanup(setattr, image_log, "PATH", self._old)

    def test_round_trip(self):
        image_log.save("HT-20261001-081132-772", prompt="1girl, silver hair",
                       skill="hd_fast")
        row = image_log.lookup("HT-20261001-081132-772")
        self.assertEqual(row["prompt"], "1girl, silver hair")
        self.assertEqual(row["skill"], "hd_fast")
        self.assertEqual(row["tag"], "HT-20261001-081132-772")

    def test_unknown_tag_returns_none(self):
        image_log.save("HT-20261001-081132-772", prompt="x")
        self.assertIsNone(image_log.lookup("HT-20261001-000000-000"))

    def test_nothing_saved_yet(self):
        """账本文件都还没建，也不能炸。"""
        self.assertIsNone(image_log.lookup("HT-20261001-081132-772"))

    def test_last_row_wins(self):
        """同号记两行时取最新的那行。

        清空 ComfyUI output 会让编号从头再来，那时最新一行才是这张图。
        """
        image_log.save("HT-20261001-081132-772", prompt="旧")
        image_log.save("HT-20261001-081132-772", prompt="新")
        self.assertEqual(image_log.lookup("HT-20261001-081132-772")["prompt"],
                         "新")

    def test_a_bad_line_does_not_spoil_the_ledger(self):
        """一行坏了（写入中断 / 手改过）不能让整本账读不出来。"""
        os.makedirs(os.path.dirname(image_log.PATH), exist_ok=True)
        with open(image_log.PATH, "w", encoding="utf-8") as f:
            f.write('{"tag": "HT-20261001-081132-772", "prompt": "断了\n')
        image_log.save("HT-20261001-090000-001", prompt="好的")
        self.assertEqual(image_log.lookup("HT-20261001-090000-001")["prompt"],
                         "好的")

    def test_no_tag_writes_nothing(self):
        image_log.save("", prompt="x")
        image_log.save(None, prompt="x")
        self.assertFalse(os.path.exists(image_log.PATH))

    def test_disk_failure_does_not_raise(self):
        """账本写不进去不能把发图也搭进去——调用方是在发图路径上。"""
        blocker = os.path.join(self.tmp.name, "iam_a_file")
        with open(blocker, "w", encoding="utf-8") as f:
            f.write("x")
        image_log.PATH = os.path.join(blocker, "sub", "image_log.jsonl")
        image_log.save("HT-20261001-081132-772", prompt="x")   # 不许抛
        self.assertIsNone(image_log.lookup("HT-20261001-081132-772"))
