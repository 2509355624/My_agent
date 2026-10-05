#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""随机口令 tag 池（app/random_tags.py）测试。

核心契约：
- 池子 ⊆ 库：每一个内容 tag 都能在 skills/anima-tags/data/tags.tsv 查到
  （与 anima-tags skill 铁律同源——tag 必须真实存在，不许编）。
- 采样确定性：给定同一个 rng，同一主题产出完全一致（可回放）。
- 抽老婆：默认池每个角色的 tag+look 也全在库里；用户私货池写坏回落默认。
"""
import json
import random
import tempfile
import unittest
from unittest import mock

from app import random_tags


class PoolValidateTest(unittest.TestCase):
    def test_every_pool_tag_exists_in_library(self):
        # 池子 ⊆ 库：改池子忘验库这里直接红。库 32.8 万行，加载 ~0.5s。
        self.assertEqual(random_tags.validate(), [])

    def test_themes_cover_all_three(self):
        self.assertEqual(set(random_tags.THEMES),
                         {"萝莉", "兽耳", "女仆"})


class SamplePromptTest(unittest.TestCase):
    def test_deterministic_with_seed(self):
        a = random_tags.sample_prompt("萝莉", random.Random(7))
        b = random_tags.sample_prompt("萝莉", random.Random(7))
        self.assertEqual(a, b)

    def test_loli_contains_base(self):
        p = random_tags.sample_prompt("萝莉", random.Random(1))
        for tag in ("1girl", "loli", "solo"):
            self.assertIn(tag, p)
        self.assertNotIn("animal_ears", p)

    def test_kemono_has_ear_choice_and_tail(self):
        p = random_tags.sample_prompt("兽耳", random.Random(3))
        self.assertIn("animal_ears", p)
        self.assertTrue(("cat_ears" in p) ^ ("fox_ears" in p))
        self.assertIn("tail", p)

    def test_maid_contains_maid_set(self):
        p = random_tags.sample_prompt("女仆", random.Random(5))
        for tag in ("maid", "maid_apron", "maid_headdress"):
            self.assertIn(tag, p)


class WaifuPoolTest(unittest.TestCase):
    def test_draw_returns_cn_and_prompt(self):
        cn, p = random_tags.draw_waifu(random.Random(2))
        valid = {e["cn"] for e in random_tags.DEFAULT_WAIFU_POOL}
        self.assertIn(cn, valid)
        first_tag = p.split(",")[0].strip()
        self.assertIn(first_tag,
                      {e["tag"] for e in random_tags.DEFAULT_WAIFU_POOL})

    def test_bad_pool_file_falls_back_to_default(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json",
                                         delete=False, encoding="utf-8") as f:
            f.write("{ 坏 json")
            path = f.name
        with mock.patch.object(random_tags, "WAIFU_POOL_PATH", path):
            pool = random_tags.load_waifu_pool()
        self.assertIs(pool, random_tags.DEFAULT_WAIFU_POOL)

    def test_custom_pool_overrides_and_is_cleaned(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json",
                                         delete=False, encoding="utf-8") as f:
            json.dump([{"tag": " custom_girl ", "cn": "自定义",
                        "look": ["white_hair", ""]}],
                      f, ensure_ascii=False)
            path = f.name
        self.addCleanup(lambda: __import__("os").unlink(path))
        with mock.patch.object(random_tags, "WAIFU_POOL_PATH", path):
            pool = random_tags.load_waifu_pool()
        self.assertEqual(pool, [{"tag": "custom_girl", "cn": "自定义",
                                 "look": ["white_hair"]}])

    def test_empty_pool_file_falls_back(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json",
                                         delete=False, encoding="utf-8") as f:
            json.dump([], f)
            path = f.name
        self.addCleanup(lambda: __import__("os").unlink(path))
        with mock.patch.object(random_tags, "WAIFU_POOL_PATH", path):
            self.assertIs(random_tags.load_waifu_pool(),
                          random_tags.DEFAULT_WAIFU_POOL)


if __name__ == "__main__":
    unittest.main()
