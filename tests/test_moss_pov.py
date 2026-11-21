# -*- coding: utf-8 -*-
"""MOSS 第一人称 POV：画面里只有女生，男主是镜头后的人、绝不出镜（2026-11-21）。

用户事故：能力一样的剧本，漫画工作台 POV 出图只有女主一个真人；
MOSS 却从角色台账 `boy: 1boy...` 与编译 caption 里把完整男人画进画面
（图1 拜山的男的、图3 caption 里的 `the boy leaning back`）。

本测试守住三处防线：
1. 编剧提示词要求角色台账只写 girl、画面写 pov hands、示例无 boy 行；
2. 编译提示词过滤掉台账里的 boy 行，不改写成画面里的人；
3. autofix 不把 boy 外形串补进 caption。

只做源码级 / 纯函数断言，不调模型、不出图。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.features.comic import moss_twopass as m  # noqa: E402


SCRIPT = """# 拜师
## 角色
- girl: 1girl, long white hair, golden eyes
- boy: 1boy, short dark hair
## 场景 1
A mountain gate, stone steps, hard light from above.
## 页 1
场景：1
格1｜中景｜女孩
画面：女孩站在山门下抬头看，镜头是男主的眼睛。
台词：女「我来了。」
      男「起来吧。」
旁白：「爬了八千级台阶。」
状态：女孩在山门下。
"""


class WriterPovRuleTest(unittest.TestCase):
    def test_writer_forbids_boy_in_cast(self):
        s = m.SCRIPT_SYSTEM
        self.assertIn("只写女生，绝不写 boy", s)
        self.assertIn("镜头后面的人就是男主本人", s)
        self.assertIn("绝不写 `1boy` / `boy`", s)
        self.assertIn("pov hands", s)

    def test_writer_example_has_no_boy_line(self):
        s = m.SCRIPT_SYSTEM
        self.assertNotIn("- boy: 1boy", s)
        self.assertIn("镜头外伸进来一只手（pov hands）", s)

    def test_script_user_carries_pov(self):
        u = m.build_script_user("一个女生在山门下拜师", 3)
        self.assertIn("画面里只能有女生", u)
        self.assertIn("绝不写 boy/1boy", u)
        self.assertIn("pov hands / disembodied hand", u)


class CompilerPovGuardTest(unittest.TestCase):
    def test_compiler_contract_forbids_male_in_caption(self):
        c = m.MOSS_CONTRACT
        self.assertIn("绝不出现 1boy / boy / 男人", c)
        self.assertIn("`pov hands` / `disembodied hand`", c)

    def test_build_moss_user_filters_boy_cast_line(self):
        sc = m.parse_script(SCRIPT)
        self.assertIn("boy", sc["cast"])  # 解析仍保留，别改数据结构
        u = m.build_moss_user(sc, 1, "", None, "S1")
        cast_part = u.split("角色外形串")[1].split("上一页版式")[0]
        self.assertNotIn("boy:", cast_part)
        self.assertIn("绝不画 1boy", cast_part)


class AutofixPovGuardTest(unittest.TestCase):
    def test_stamp_cast_does_not_inject_boy(self):
        # 模型把 caption 写成 1boy、autofix 也不该从台账把 boy 外形串补回去
        js = {"summary": {"meta": {}, "canvas": "x", "caption": "Manga page, monochrome."},
              "items": [{"label": "character", "caption": "1boy, monochrome greyscale screentone",
                         "bbox": [0, 0, 100, 100]}],
              "bbox_coord_space": "x", "bbox_format": "x"}
        cast = {"girl": "1girl, long white hair", "boy": "1boy, short dark hair"}
        notes = m.autofix(js, cast, [], "S1")
        # 核心：autofix 绝不从台账把 boy 外形串补回去（补了=把男主画进画面）
        self.assertFalse(any("补齐 boy" in n for n in notes))
        self.assertNotIn("short dark hair", js["items"][0]["caption"])


if __name__ == "__main__":
    unittest.main()
