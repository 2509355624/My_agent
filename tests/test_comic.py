# -*- coding: utf-8 -*-
"""漫画管道测试：气泡2 解析 / 两段渲染 / 编剧规范注入。

2026-10-11 加。背景：用户给了一份《连续剧情提示词生成规范》要求**全文**
喂给编剧 AI（单次请求，不做压缩合并），台词丰富化靠新增 `气泡2:` 字段
（第二遍 i2i 在右上角补第二个气泡）。这份测试钉死三件事：
  1. `气泡2:` 被正确解析，且不会因正则交替顺序被「气泡」吃掉；
  2. render_bubble 有 bubble2 时串两遍 i2i、没有时只一遍；
  3. 编剧 system prompt 里含规范全文与契约映射，气泡2 超长会被打回。
"""

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import comic, comic_writer


def _panel_dict(no, **kw):
    d = {"no": no, "scene": "1girl, solo, test", "bubble": "台词",
         "bubble2": "", "label": ""}
    d.update(kw)
    return d


class ParseBubble2Test(unittest.TestCase):
    """`气泡2:` 字段解析——正则交替顺序的坑就在这里。"""

    def test_bubble2_parsed(self):
        text = "# 标题：测试\n\n## 01\n画面: 1girl, solo, silver hair\n" \
               "气泡: 第一句\n气泡2: 第二句\n标签: 好感度 0\n"
        _, panels = comic.parse_script_text(text)
        self.assertEqual(len(panels), 1)
        p = panels[0]
        self.assertEqual(p["bubble"], "第一句")
        self.assertEqual(p["bubble2"], "第二句")
        self.assertEqual(p["label"], "好感度 0")

    def test_bubble2_not_eaten_by_bubble(self):
        # 「气泡2」必须在交替里排在「气泡」前面，否则值会剩成 "2: 第二句"
        text = "## 01\n画面: x\n气泡: 一\n气泡2: 二\n"
        _, panels = comic.parse_script_text(text)
        self.assertEqual(panels[0]["bubble2"], "二")
        self.assertNotIn("2", panels[0]["bubble"])

    def test_bubble2_continuation_lines(self):
        # 气泡2 也支持续行（契约里字段都可续行）
        text = "## 01\n画面: x\n气泡: 一\n气泡2: 二\n继续二\n"
        _, panels = comic.parse_script_text(text)
        self.assertEqual(panels[0]["bubble2"], "二 继续二")

    def test_alias_taici2(self):
        text = "## 01\n画面: x\n气泡: 一\n台词2: 二\n"
        _, panels = comic.parse_script_text(text)
        self.assertEqual(panels[0]["bubble2"], "二")

    def test_no_bubble2_defaults_empty(self):
        _, panels = comic.parse_script_text("## 01\n画面: x\n气泡: 一\n")
        self.assertEqual(panels[0]["bubble2"], "")


class TwoPassBubbleTest(unittest.TestCase):
    """render_bubble：有 bubble2 串两遍 i2i，没有只一遍。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        self.submits = []

        def _upload(path):
            return "uploaded.png"

        def _build(skill, prompt, seed, uploaded):
            return {"prompt": prompt}

        def _submit(wf, skill, prompt, seed):
            self.submits.append(prompt)
            return "out_%d.png" % len(self.submits)

        def _download(name, dst):
            with open(dst, "wb") as f:
                f.write(b"png")

        for target, repl in (("_upload_source", _upload),
                             ("_build_i2i", _build),
                             ("_submit", _submit),
                             ("_download", _download)):
            p = mock.patch.object(comic, target, repl)
            p.start()
            self.addCleanup(p.stop)

    def _task(self, bubble2=""):
        text = "## 01\n画面: 1girl, solo, test\n气泡: 第一句\n"
        if bubble2:
            text += "气泡2: %s\n" % bubble2
        path = os.path.join(self.dir, "script.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        meta, panels = comic.parse_script(path)
        task = comic.Task(path, meta, panels)
        os.makedirs(task.final_dir, exist_ok=True)   # mock 的 _download 不做 makedirs
        return task

    def test_single_bubble_one_pass(self):
        task = self._task()
        p = task.panels[0]
        os.makedirs(task.raw_dir)
        with open(task.raw_path(p), "wb") as f:
            f.write(b"raw")
        comic.render_bubble(task, p)
        self.assertEqual(len(self.submits), 1)
        self.assertEqual(p.state, "done")
        self.assertTrue(os.path.exists(task.final_path(p)))

    def test_bubble2_two_passes(self):
        task = self._task(bubble2="第二句")
        p = task.panels[0]
        os.makedirs(task.raw_dir)
        with open(task.raw_path(p), "wb") as f:
            f.write(b"raw")
        comic.render_bubble(task, p)
        self.assertEqual(len(self.submits), 2)
        self.assertIn("第一句", self.submits[0])
        self.assertIn("第二句", self.submits[1])
        # 第二遍模板必须按「图里已有第一个气泡」写约束
        self.assertIn("已经有一个对话框", self.submits[1])
        self.assertEqual(p.state, "done")


class StateRoundtripTest(unittest.TestCase):
    """bubble2 进出 state.json；改 bubble2 让 done 格退回重加气泡。"""

    def test_change_bubble2_resets_done(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        text = "## 01\n画面: 1girl, solo, test\n气泡: 一\n气泡2: 二\n"
        path = os.path.join(tmp.name, "script.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        meta, panels = comic.parse_script(path)
        task = comic.Task(path, meta, panels)
        p = task.panels[0]
        p.state, p.seed = "done", 42
        task.save_state()

        # 剧本里 bubble2 改了 → 重新解析后 load_state 应把 done 退回 rendered
        text2 = text.replace("气泡2: 二", "气泡2: 改过的二")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text2)
        meta2, panels2 = comic.parse_script(path)
        task2 = comic.Task(path, meta2, panels2)
        task2.load_state()
        self.assertEqual(task2.panels[0].state, "rendered")

        # 没改 → 保持 done
        meta3, panels3 = comic.parse_script(path)
        task3 = comic.Task(path, meta3, panels3)
        task3.load_state()
        self.assertEqual(task3.panels[0].state, "rendered")  # 上一步已退回


class WriterSpecTest(unittest.TestCase):
    """编剧 system prompt 必须含规范全文；气泡2 超长被打回。"""

    def test_system_prompt_contains_full_spec(self):
        prompt = comic_writer._system_prompt(3)
        # 规范全文的标志性段落必须原样在场（用户拍板：不做压缩合并）
        for marker in ("连续剧情提示词生成规范",
                       "动作位置副词规则",
                       "局部动作与表情互斥规则",
                       "girl holding a gift box in front of her chest",
                       "气泡2"):
            self.assertIn(marker, prompt)

    def test_validate_rejects_long_bubble2(self):
        text = ("## 01\n画面: 1girl, solo, test\n气泡: 一\n"
                "气泡2: " + "长" * 17 + "\n")
        ok, why = comic_writer._validate(text, 1)
        self.assertFalse(ok)
        self.assertIn("气泡2", why)

    def test_validate_accepts_bubble2(self):
        text = "## 01\n画面: 1girl, solo, test\n气泡: 一\n气泡2: 二\n"
        ok, why = comic_writer._validate(text, 1)
        self.assertTrue(ok, why)

    def test_write_script_passes_spec_via_llm_messages(self):
        good = "# 标题：测试\n\n## 01\n画面: 1girl, solo, test\n气泡: 一\n"
        with mock.patch.object(comic_writer.llm, "call_llm",
                               return_value=good) as m_llm:
            comic_writer.write_script("妹妹收到礼物", panels=1)
        system = m_llm.call_args.args[0][0]["content"]
        self.assertIn("连续剧情提示词生成规范", system)
        self.assertIn("======== 规范全文开始 ========", system)


if __name__ == "__main__":
    unittest.main()
