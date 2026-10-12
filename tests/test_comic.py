# -*- coding: utf-8 -*-
"""漫画管道测试：气泡2 解析 / 两段渲染 / 编剧规范注入。

2026-10-11 加。背景：用户给了一份《连续剧情提示词生成规范》要求**全文**
喂给编剧 AI（单次请求，不做压缩合并），台词丰富化靠新增 `气泡2:` 字段。
这份测试钉死三件事：
  1. `气泡2:` 被正确解析，且不会因正则交替顺序被「气泡」吃掉；
  2. render_bubble **默认一遍画完两个气泡+旁白**（2026-10-11 合并，省一次
     i2i）；`split=True` 才走老的分两遍——那是画崩后的退路；
  3. 编剧 system prompt 里含规范全文与契约映射，气泡2 超长会被打回。
"""

import os
import re
import requests
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import comic, comic_writer, skills


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
    """render_bubble：默认一遍画完两个气泡+旁白；split=True 才分两遍。"""

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

    def _task(self, bubble2="", label=""):
        text = "## 01\n画面: 1girl, solo, test\n气泡: 第一句\n"
        if label:
            text += "标签: %s\n" % label
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

    def test_bubble2_one_pass_by_default(self):
        """有第二句台词也**一次画完**——这是 2026-10-11 合并掉第二次 i2i 的核心。

        合并前是「第一遍气泡1+旁白，第二遍在成图上补气泡2」，每个这样的格多花
        一次 50~70 秒。合并的依据：当初会崩是因为模板没声明条数和归属，现在每条
        气泡都声明「文字 + 尾巴指向 + 位置」，真跑 3/3 全对、热机耗时不变。
        """
        task = self._task(bubble2="第二句")
        p = task.panels[0]
        os.makedirs(task.raw_dir)
        with open(task.raw_path(p), "wb") as f:
            f.write(b"raw")
        comic.render_bubble(task, p)
        self.assertEqual(len(self.submits), 1, "默认必须只提交一次")
        self.assertIn("第一句", self.submits[0])
        self.assertIn("第二句", self.submits[0])
        self.assertEqual(p.state, "done")

    def test_merged_prompt_declares_both_bubbles_explicitly(self):
        """合并版必须把**每个**气泡的文字、位置、归属都写死。

        含糊就挂错人（用户原话「箭头指向谁必须是确定的」）——当初「一次只画一个」
        的取舍正是为了绕开这份含糊，现在两件事可以同时满足。

        2026-10-11 用户拍板后两类写法分家：角色的气泡写明「指向画面中的女生」，
        「我们」的话换成白色圆角矩形框 + 右下角、**一个字都不提指向**。
        """
        p = comic.Panel("01", "1girl, solo", "走吧", "车门合上了",
                        "回家再说", speaker="女", speaker2="我")
        prompt = p.bubble_prompt()
        self.assertIn('"走吧"', prompt)
        self.assertIn('"回家再说"', prompt)
        self.assertIn("左上方的对话框", prompt)                  # 女生的气泡
        self.assertIn("指向画面中的女生", prompt)                # 归属写死
        self.assertIn("仅在画面右下角添加一个白色的圆角矩形框", prompt)   # 我们的话
        self.assertNotIn("尾巴", prompt)
        self.assertNotIn("箭头", prompt)
        self.assertIn("车门合上了", prompt)                     # 旁白也在这一遍里

    def test_merged_prompt_count_is_not_contradictory(self):
        """合并版里不能出现「只有一个气泡」这种自相矛盾的句子。

        `_label_sec` 原来把数量约束写死成「左上角一个气泡」——合并版直接复用，
        同一段提示词就会一边说一个、一边说两个，模型只能二选一。
        （同类前科：提示词里写「`stepping off` 用 `standing on one leg` 之类」，
        模型逐字执行、画成单脚站立的芭蕾姿。）
        """
        p = comic.Panel("01", "1girl, solo", "走吧", "旁白一\n旁白二",
                        "回家再说", speaker="女", speaker2="我")
        prompt = p.bubble_prompt()
        self.assertNotIn("左上角一个气泡", prompt)
        self.assertIn("左上方的那个气泡", prompt)
        self.assertIn("右下角的那个气泡", prompt)         # 旁白占了右上角，它让位
        self.assertIn("画面里的文字一共这三处", prompt)

    def test_split_flag_keeps_the_old_two_passes(self):
        """split=True 是退路：某一格一遍画崩了，单独切回两遍重跑。"""
        task = self._task(bubble2="第二句")
        p = task.panels[0]
        os.makedirs(task.raw_dir)
        with open(task.raw_path(p), "wb") as f:
            f.write(b"raw")
        comic.render_bubble(task, p, split=True)
        self.assertEqual(len(self.submits), 2)
        self.assertIn("第一句", self.submits[0])
        self.assertNotIn("第二句", self.submits[0])      # 第一遍只管一个气泡
        self.assertIn("第二句", self.submits[1])
        # 第二遍模板必须按「图里已有第一个气泡」写约束
        self.assertIn("已经有一个对话框", self.submits[1])

    def test_split_does_not_add_a_pass_without_bubble2(self):
        """没有第二句台词时 split 不该凭空多跑一遍。"""
        task = self._task()
        p = task.panels[0]
        os.makedirs(task.raw_dir)
        with open(task.raw_path(p), "wb") as f:
            f.write(b"raw")
        comic.render_bubble(task, p, split=True)
        self.assertEqual(len(self.submits), 1)

    def test_bubble2_moves_to_bottom_right_with_narration(self):
        """旁白条占着右上角，第二个气泡必须让位到右下角。

        2026-10-11：`标签:` 改成每格必写的剧情旁白后，它和第二个气泡的默认
        位置都是右上角——照原样会把两个文字元素挤在同一个角落。
        """
        task = self._task(bubble2="第二句", label="我攥住了她的手腕")
        p = task.panels[0]
        os.makedirs(task.raw_dir)
        with open(task.raw_path(p), "wb") as f:
            f.write(b"raw")
        comic.render_bubble(task, p)
        self.assertEqual(len(self.submits), 1)
        self.assertIn("右下角", self.submits[0])
        self.assertIn("右上角的一行旁白", self.submits[0])     # 右上角留给了旁白

    def test_bubble2_stays_top_right_without_narration(self):
        task = self._task(bubble2="第二句")
        p = task.panels[0]
        os.makedirs(task.raw_dir)
        with open(task.raw_path(p), "wb") as f:
            f.write(b"raw")
        comic.render_bubble(task, p)
        self.assertIn("右上角", self.submits[0])


class ManualRenderTest(unittest.TestCase):
    """render_manual：手动改字。

    源图必须是**当前成品**（final）——用户的实际用法是「在最后那张加了字幕的
    图上，告诉 qwen 某个字糊了、把它重画成 XX」，所以这一遍要能累积着改。
    只有在还没有成品（只有底图）时才退到底图，这样手动入口对「待加气泡」的格
    也能用。改坏了有回退路：拿剧本提示词从底图重画（/api/comic/bubble）。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        self.seen = {}

        def _upload(path):
            self.seen["src"] = path
            return "uploaded.png"

        def _build(skill, prompt, seed, uploaded, denoise=comic.I2I_DENOISE):
            self.seen["prompt"] = prompt
            self.seen["denoise"] = denoise
            return {"prompt": prompt}

        def _submit(wf, skill, prompt, seed):
            return "out.png"

        def _download(name, dst):
            with open(dst, "wb") as f:
                f.write(b"NEW")

        for target, repl in (("_require_comfy", lambda: None),
                             ("_upload_source", _upload),
                             ("_build_i2i", _build),
                             ("_submit", _submit),
                             ("_download", _download)):
            p = mock.patch.object(comic, target, repl)
            p.start()
            self.addCleanup(p.stop)

    def _task(self):
        text = "## 01\n画面: 1girl, solo, test\n气泡: 第一句\n标签1: 甲\n"
        path = os.path.join(self.dir, "script.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        meta, panels = comic.parse_script(path)
        task = comic.Task(path, meta, panels)
        os.makedirs(task.raw_dir, exist_ok=True)
        os.makedirs(task.final_dir, exist_ok=True)
        p = task.panels[0]
        with open(task.raw_path(p), "wb") as f:
            f.write(b"RAW")
        return task, p

    def test_source_is_the_current_final(self):
        task, p = self._task()
        with open(task.final_path(p), "wb") as f:
            f.write(b"OLDFINAL")
        comic.render_manual(task, p, "把「松手啦」重画清楚")
        self.assertEqual(self.seen["src"], task.final_path(p))
        with open(task.final_path(p), "rb") as f:
            self.assertEqual(f.read(), b"NEW")
        self.assertEqual(p.state, "done")

    def test_falls_back_to_raw_when_no_final_yet(self):
        task, p = self._task()
        comic.render_manual(task, p, "加个气泡")
        self.assertEqual(self.seen["src"], task.raw_path(p))

    def test_raises_when_there_is_no_image_at_all(self):
        task, p = self._task()
        os.remove(task.raw_path(p))
        with self.assertRaises(RuntimeError) as cm:
            comic.render_manual(task, p, "改")
        self.assertIn("还没有任何图", str(cm.exception))

    def test_no_tmp_file_left_behind(self):
        """先落 .tmp 再原子替换——半张图写坏就等于把上一版好成品丢了。"""
        task, p = self._task()
        with open(task.final_path(p), "wb") as f:
            f.write(b"OLD")
        comic.render_manual(task, p, "改")
        self.assertEqual(os.listdir(task.final_dir), ["01_final.png"])

    def test_instruction_comes_first_then_the_keep_note(self):
        task, p = self._task()
        comic.render_manual(task, p, "  把「疼」改成「痛」  ")
        prompt = self.seen["prompt"]
        self.assertTrue(prompt.startswith("把「疼」改成「痛」"))
        self.assertIn(comic.MANUAL_KEEP_NOTE, prompt)

    def test_denoise_override_reaches_the_workflow(self):
        task, p = self._task()
        comic.render_manual(task, p, "改", denoise="0.35")
        self.assertEqual(self.seen["denoise"], "0.35")

    def test_denoise_defaults_to_pipeline_value(self):
        task, p = self._task()
        comic.render_manual(task, p, "改")
        self.assertEqual(self.seen["denoise"], comic.I2I_DENOISE)

    def test_state_is_persisted_so_the_panel_shows_done(self):
        task, p = self._task()
        comic.render_manual(task, p, "改")
        again = comic.Task(task.script_path, *comic.parse_script(task.script_path))
        again.load_state()
        self.assertEqual(again.panels[0].state, "done")


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

    def test_failure_reason_survives_reload(self):
        """失败原因必须能读回来，失败的格才能重跑（2026-10-12）。

        不读回来的后果是一条完整的死路：失败的格**停在 pending**（失败时不改
        state），error 又是 None → 界面既不显示「出错」，也不给重跑按钮，只剩
        一句「排队中…」。实测 comics/C20261012-03 格01 的 ConnectionResetError
        就是这么在界面上凭空消失的。
        """
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "script.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write("## 01\n画面: 1girl, solo, test\n气泡: 一\n")
        meta, panels = comic.parse_script(path)
        task = comic.Task(path, meta, panels)
        task.panels[0].state = "pending"
        task.panels[0].error = "跑超时了：ComfyUI 没反应"
        task.save_state()

        again = comic.Task(path, *comic.parse_script(path))
        again.load_state()
        self.assertEqual(again.panels[0].state, "pending")
        self.assertEqual(again.panels[0].error, "跑超时了：ComfyUI 没反应")

    def test_changing_scene_clears_stale_failure_reason(self):
        """画面改了 → 整格重来，上一版的失败原因必须一起清掉。

        留着的话，重跑成功后界面上还挂着一条属于旧剧本的「出错」。
        """
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "script.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write("## 01\n画面: 1girl, solo, test\n气泡: 一\n")
        meta, panels = comic.parse_script(path)
        task = comic.Task(path, meta, panels)
        task.panels[0].state = "pending"
        task.panels[0].error = "上一版的错"
        task.save_state()

        with open(path, "w", encoding="utf-8") as f:
            f.write("## 01\n画面: 1girl, solo, changed\n气泡: 一\n")
        again = comic.Task(path, *comic.parse_script(path))
        again.load_state()
        self.assertEqual(again.panels[0].state, "pending")
        self.assertIsNone(again.panels[0].error)


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
        text = ("## 01\n画面: 1girl, solo, test\n气泡: 一\n气泡2: 二\n"
                "标签: 我没敢松手\n")
        ok, why = comic_writer._validate(text, 1)
        self.assertTrue(ok, why)

    def test_write_script_passes_spec_via_llm_messages(self):
        good = ("# 标题：测试\n\n## 01\n画面: 1girl, solo, test\n气泡: 一\n"
                "标签: 她拆开了盒子\n")
        with mock.patch.object(comic_writer.llm, "call_llm",
                               return_value=good) as m_llm:
            comic_writer.write_script("妹妹收到礼物", panels=1)
        system = m_llm.call_args.args[0][0]["content"]
        self.assertIn("连续剧情提示词生成规范", system)
        self.assertIn("======== 规范全文开始 ========", system)

    def test_label_prompt_does_not_say_small_font(self):
        """「小字」对 qwen 就是个字号指令——原文写了它，就真画成很小的字。

        2026-10-11 用户反馈「作为标签的字体有点小」：模板原话是「添加一行**小**
        字」。字号必须显式写清楚，不能留这种会被当指令执行的形容词。
        旁白段后来改由 `_label_sec()` 现拼（单条 / 多条措辞不同），所以这里
        查的是**真正发给 qwen 的那段提示词**，而不是模板字面量。
        """
        prompt = comic.Panel("01", "1girl, solo", "台词", "旁白").bubble_prompt()
        self.assertNotIn("小字", prompt)
        self.assertIn("字号清晰可读", prompt)
        self.assertIn('这行旁白写 "旁白"', prompt)


class MultiLabelTest(unittest.TestCase):
    """多条旁白：`标签1:` `标签2:` `标签3:` 按出现顺序各占一行。

    用户 2026-10-11 拍板：一格要能讲好几段剧情（「我带她回了家 / 安慰了她 /
    她的手在袖子里攥紧了」），所以 label 从单值变成多行字符串。
    """

    def test_numbered_labels_become_lines(self):
        text = ("## 01\n画面: 1girl, solo\n气泡: （女）一\n"
                "标签1: 妹妹生闷气离家出走\n标签2: 一只脚已经踩上车门\n")
        _, panels = comic.parse_script_text(text)
        self.assertEqual(panels[0]["label"],
                         "妹妹生闷气离家出走\n一只脚已经踩上车门")

    def test_bare_label_and_numbered_mix(self):
        _, panels = comic.parse_script_text(
            "## 01\n画面: 1girl\n气泡: 一\n标签: 前情\n标签2: 当下\n")
        self.assertEqual(panels[0]["label"], "前情\n当下")

    def test_three_labels_kept_in_order(self):
        text = ("## 01\n画面: 1girl\n气泡: 一\n"
                "标签1: 我带她回了家\n标签2: 安慰了她\n标签3: 她的手攥紧了\n")
        _, panels = comic.parse_script_text(text)
        self.assertEqual(panels[0]["label"].split("\n"),
                         ["我带她回了家", "安慰了她", "她的手攥紧了"])

    def test_labels_do_not_leak_into_bubble(self):
        _, panels = comic.parse_script_text(
            "## 01\n画面: 1girl\n气泡: 一\n标签1: 甲\n标签2: 乙\n")
        self.assertEqual(panels[0]["bubble"], "一")

    def test_panel_defaults_speaker_keys(self):
        _, panels = comic.parse_script_text("## 01\n画面: 1girl\n气泡: 一\n")
        self.assertEqual(panels[0]["speaker"], "")
        self.assertEqual(panels[0]["speaker2"], "")


class SpeakerTest(unittest.TestCase):
    """说话人：气泡行前缀 `（女）` / `（我）`，剥出来只决定尾巴指向谁。

    前缀**绝不能留在台词里**（会被 qwen 当文字画上去），也**绝不能误剥**——
    `（笑）你说什么` 这种括号开头不是说话人。
    """

    def test_bracket_prefix_stripped(self):
        _, panels = comic.parse_script_text(
            "## 01\n画面: 1girl\n气泡: （女）别跟着我……\n")
        self.assertEqual(panels[0]["speaker"], "女")
        self.assertEqual(panels[0]["bubble"], "别跟着我……")

    def test_wo_means_camera_person(self):
        _, panels = comic.parse_script_text(
            "## 01\n画面: 1girl\n气泡: （我）为什么……\n")
        self.assertEqual(panels[0]["speaker"], "我")
        self.assertEqual(panels[0]["bubble"], "为什么……")

    def test_colon_form(self):
        _, panels = comic.parse_script_text(
            "## 01\n画面: 1girl\n气泡: 我：为什么……\n")
        self.assertEqual(panels[0]["speaker"], "我")
        self.assertEqual(panels[0]["bubble"], "为什么……")

    def test_speaker_line_applies_to_previous_bubble(self):
        _, panels = comic.parse_script_text(
            "## 01\n画面: 1girl\n气泡: 为什么……\n说话人: 我\n")
        self.assertEqual(panels[0]["speaker"], "我")

    def test_speaker2_line_applies_to_bubble2(self):
        _, panels = comic.parse_script_text(
            "## 01\n画面: 1girl\n气泡: 女：一\n气泡2: 二\n说话人2: 女\n")
        self.assertEqual(panels[0]["speaker2"], "女")

    def test_non_speaker_bracket_left_alone(self):
        _, panels = comic.parse_script_text(
            "## 01\n画面: 1girl\n气泡: （笑）你说什么\n")
        self.assertEqual(panels[0]["bubble"], "（笑）你说什么")
        self.assertEqual(panels[0]["speaker"], "")


class TailDirectionTest(unittest.TestCase):
    """气泡归属必须写死，但**只用最短的正向说法**。

    含糊的话 qwen 一律挂到画面里的女生头上——镜头外「我」说的话就变成她说的
    （2026-10-11 用户实测反馈）。2026-10-11 晚用户拿两张实测图把写法定死：
    角色的气泡写「指向画面中的女生」；「我们」的话不写指向，改由
    **白色圆角矩形框 + 右下角**表身份（形状和位置本身就是语义）。
    """

    def test_girl_bubble_points_at_girl(self):
        prompt = comic.Panel("01", "1girl, solo", "别跟着我",
                             speaker="女").bubble_prompt()
        self.assertIn("指向画面中的女生", prompt)
        self.assertNotIn("尾巴", prompt)       # 「尾巴」这个词也一并删了
        self.assertNotIn("箭头", prompt)

    def test_self_bubble_is_a_plain_rounded_box(self):
        """「我」的台词：白色圆角矩形框 + 右下角，**整段不提任何指向**。

        用户拿实测图拍板：形状和位置就是约定。更早那版写的「尾巴向下指向画面
        下缘，说话人是画面外的「我」」被整句删掉——用户原话「我们参杂了大量的
        无效内容给 qwen，他不是思考模型，他是画图的模型，所以没有用的词，比如
        禁止什么、不要什么，都不要说，只说我们要什么即可」。
        否定句（旧版的「不是女生说的，不要指向女生」）同样禁——白熊效应。
        """
        prompt = comic.Panel("01", "1girl, solo", "为什么",
                             speaker="我").bubble_prompt()
        self.assertIn("仅在画面右下角添加一个白色的圆角矩形框", prompt)
        self.assertNotIn("尾巴", prompt)
        self.assertNotIn("箭头", prompt)
        self.assertNotIn("指向", prompt)       # 我方一个字都不提指向
        self.assertNotIn("画面外", prompt)     # 解释性语句一律不要
        self.assertNotIn("不要指向女生", prompt)
        self.assertNotIn("不是女生说的", prompt)

    def test_speaker_defaults_to_girl(self):
        self.assertEqual(comic.Panel("01", "s", "b").speaker, "女")

    def test_positions_come_from_the_speaker_not_the_order(self):
        """落位由**谁说的**决定（`_layout`），不是「第几个气泡」。

        2026-10-11 之前位置是写死的「第一个左上、第二个让位」，所以只有「我」
        说话的那一格里，我们的话会被丢到左上角——用户实测图里它在右下角。
        """
        self.assertEqual(comic._layout(["我"], False), ["右下角"])
        self.assertEqual(comic._layout(["我"], True), ["右下角"])
        self.assertEqual(comic._layout(["女"], False), ["左上方"])
        self.assertEqual(comic._layout(["女"], True), ["左上方"])
        # 两个角色：第二个依次让位，右上被旁白占掉就跳过
        self.assertEqual(comic._layout(["女", "女"], False), ["左上方", "右上角"])
        self.assertEqual(comic._layout(["女", "女"], True), ["左上方", "右下角"])
        # 角色 + 我们：各回各家，不撞
        self.assertEqual(comic._layout(["女", "我"], True), ["左上方", "右下角"])
        self.assertEqual(comic._layout(["我", "女"], True), ["右下角", "左上方"])

    def test_bubble2_uses_its_own_speaker(self):
        p = comic.Panel("01", "s", "一", speaker="我",
                        bubble2="二", speaker2="女")
        self.assertIn("仅在画面右下角添加一个白色的圆角矩形框",
                      p.bubble_prompt())                  # 镜头外的「我」
        self.assertIn("指向画面中的女生", p.bubble2_prompt())  # 女生的气泡


class MultiLabelPromptTest(unittest.TestCase):
    """多条旁白必须在提示词里**逐行点名**，否则 qwen 只画一行。"""

    def test_two_lines_listed_by_index(self):
        prompt = comic.Panel("01", "s", "台词", "甲\n乙").bubble_prompt()
        self.assertIn("旁白一共 2 行", prompt)
        self.assertIn('第 1 行写 "甲"', prompt)
        self.assertIn('第 2 行写 "乙"', prompt)

    def test_single_line_uses_single_wording(self):
        prompt = comic.Panel("01", "s", "台词", "甲").bubble_prompt()
        self.assertIn("一行旁白", prompt)
        self.assertNotIn("旁白一共", prompt)

    def test_labels_helper_skips_blank(self):
        self.assertEqual(comic.Panel("01", "s", "b", "甲\n\n乙 ").labels(),
                         ["甲", "乙"])


class WriterMultiLabelValidateTest(unittest.TestCase):
    """校验：旁白至少一条、最多 MAX_LABELS 条、每条不超 MAX_LABEL 字。"""

    def test_accepts_three_labels(self):
        text = ("## 01\n画面: 1girl, solo\n气泡: （女）一\n"
                "标签1: 甲\n标签2: 乙\n标签3: 丙\n")
        ok, why = comic_writer._validate(text, 1)
        self.assertTrue(ok, why)

    def test_rejects_too_many_labels(self):
        text = ("## 01\n画面: 1girl, solo\n气泡: （女）一\n"
                "标签1: 甲\n标签2: 乙\n标签3: 丙\n标签4: 丁\n")
        ok, why = comic_writer._validate(text, 1)
        self.assertFalse(ok)
        self.assertIn("最多", why)

    def test_rejects_overlong_label_line(self):
        text = ("## 01\n画面: 1girl, solo\n气泡: （女）一\n"
                "标签1: %s\n" % ("长" * (comic_writer.MAX_LABEL + 1)))
        ok, why = comic_writer._validate(text, 1)
        self.assertFalse(ok)
        self.assertIn("太长", why)

    def test_rejects_missing_label(self):
        ok, why = comic_writer._validate(
            "## 01\n画面: 1girl, solo\n气泡: （女）一\n", 1)
        self.assertFalse(ok)
        self.assertIn("标签1", why)

    def test_accepts_label_at_exactly_the_cap(self):
        """上限是闭区间：正好 MAX_LABEL 字要过，多一字才挂。"""
        text = ("## 01\n画面: 1girl, solo\n气泡: （女）一\n"
                "标签1: %s\n" % ("长" * comic_writer.MAX_LABEL))
        ok, why = comic_writer._validate(text, 1)
        self.assertTrue(ok, why)

    def test_rejects_dialogue_written_as_a_label(self):
        """`标签2: （女）…` 是台词写错了栏——必须判不合格并点名 `气泡2`。"""
        text = ("## 01\n画面: 1girl, solo\n气泡: （女）一\n"
                "标签1: 她一只脚踏上车门\n标签2: （女）我不要回去！\n")
        ok, why = comic_writer._validate(text, 1)
        self.assertFalse(ok)
        self.assertIn("（女）我不要回去！", why)
        self.assertIn("气泡2", why)

    def test_accepts_a_label_that_merely_opens_with_a_bracket(self):
        """`（她把书包扔过来）` 是正常旁白，括号里不是纯说话人词，不能误判。"""
        text = ("## 01\n画面: 1girl, solo\n气泡: （女）一\n"
                "标签1: （她把书包扔过来）\n")
        ok, why = comic_writer._validate(text, 1)
        self.assertTrue(ok, why)

    def test_reports_every_dialogue_leak_at_once(self):
        """两格都写错时要在一条错因里同时点名——只报一格就只改得动一格。"""
        text = ("## 01\n画面: 1girl, solo\n气泡: （女）一\n"
                "标签1: （女）甲\n"
                "## 02\n画面: 1girl, solo\n气泡: （女）二\n"
                "标签1: 乙\n标签2: （我）丙\n")
        ok, why = comic_writer._validate(text, 2)
        self.assertFalse(ok)
        self.assertIn("格 01", why)
        self.assertIn("格 02", why)


class QwenI2iParamsTest(unittest.TestCase):
    """qwen 图生图工作流的固定参数。

    这份工作流是**共用**的：漫画「加气泡」「手动修复」「手动改字」走的都是它，
    QQ 生图的 qwen 渠道也是它——所以这里改的是全局效果，不只是漫画。

    步数两轮：2026-10-11 先 12 → 18（用户手动改字时亲眼看出 12 步重画的字糊），
    同一晚又拍板 → **25**。25 不是拍的，是查证出来的：Qwen-Image 2.1 的官方
    day-0 联调参数就是「25 步 / cfg 1.0 / euler + simple」（ComfyUI v0.37.0
    原生模板默认值；diffusers 官方默认更是 40 步）。用户原话「我跑重新生成都
    耗费几分钟，还不如加长步长，一次搞定」。

    底模同一晚从 **UC**（无内容过滤，2026-10-05 换上的）换回**官方**
    `qwen-image-2.1-Q4_K_M.gguf`——用户 10-11 拍板撤回，且只换 qwen_image_v1
    这一个渠道（qwen-hd / qwen-restore / qwen-charsheet 继续用 UC）。
    """

    def _wf(self):
        sd = skills.load_skill(comic.BUBBLE_SKILL)
        self.assertTrue(sd, "找不到 skill %s" % comic.BUBBLE_SKILL)
        wf = skills.load_workflow(os.path.join(sd["path"], "workflow_i2i.json"))
        self.assertIsNotNone(wf, "qwen 图生图工作流读不出来")
        return wf

    def test_steps_is_25(self):
        steps = [n["inputs"]["steps"] for n in self._wf().values()
                 if "steps" in (n.get("inputs") or {})]
        self.assertEqual(
            steps, [25],
            "qwen 图生图的步数必须是 25（官方 day-0 联调值，用户 2026-10-11 拍板）")

    def test_base_model_is_the_official_one_not_uc(self):
        """底模不能是 `-UC`（无内容过滤）那个。

        UC 是 2026-10-05 换上的（动机：模型侧不拒答，拦截交给 image_audit），
        用户 10-11 拍板撤回。这里钉住文件名，免得哪天顺手又被换回去。
        """
        unet = [n["inputs"]["unet_name"] for n in self._wf().values()
                if n.get("class_type") == "UnetLoaderGGUF"]
        self.assertEqual(unet, ["qwen-image-2.1-Q4_K_M.gguf"])
        self.assertNotIn("UC", "".join(unet))


class BubblePromptOverrideTest(unittest.TestCase):
    """render_bubble 的 prompt 覆盖：「完整提示词」框手改的那段要原样发出去。

    用户 2026-10-11 的要求是「我需要看到完整的发给 qwen 的文字的内容是什么，
    我可以进行修改」。这条链的两个端点必须严丝合缝：**取出来的 = 模板拼的**，
    **发出去的 = 你改的那段**，中间不许再有任何二次加工（比如又拼一句「保持
    人物服装不变」——那就变成「你改的 + 系统塞的」，跟看到的不是一回事了）。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        self.seen = {}

        def _upload(path):
            self.seen["src"] = path
            return "uploaded.png"

        def _build(skill, prompt, seed, uploaded, denoise=comic.I2I_DENOISE):
            self.seen["prompt"] = prompt
            return {"prompt": prompt}

        def _download(name, dst):
            with open(dst, "wb") as f:
                f.write(b"NEW")

        for target, repl in (("_upload_source", _upload),
                             ("_build_i2i", _build),
                             ("_submit", lambda wf, skill, prompt, seed: "out.png"),
                             ("_download", _download)):
            p = mock.patch.object(comic, target, repl)
            p.start()
            self.addCleanup(p.stop)

    def _task(self):
        text = "## 01\n画面: 1girl, solo, test\n气泡: 第一句\n标签1: 甲\n"
        path = os.path.join(self.dir, "script.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        meta, panels = comic.parse_script(path)
        task = comic.Task(path, meta, panels)
        os.makedirs(task.raw_dir, exist_ok=True)
        os.makedirs(task.final_dir, exist_ok=True)
        p = task.panels[0]
        with open(task.raw_path(p), "wb") as f:
            f.write(b"RAW")
        return task, p

    def test_default_uses_the_template(self):
        task, p = self._task()
        comic.render_bubble(task, p)
        self.assertEqual(self.seen["prompt"], p.bubble_prompt(split=False))

    def test_override_replaces_the_prompt_verbatim(self):
        task, p = self._task()
        mine = '在画面左上角添加一个对话框，里面写 "我改过的"。'
        comic.render_bubble(task, p, prompt=mine)
        self.assertEqual(self.seen["prompt"], mine)

    def test_override_is_trimmed_but_not_otherwise_touched(self):
        """首尾空白剥掉，中间的换行/引号一个字符都不许动。"""
        task, p = self._task()
        mine = '加一个对话框："甲"。\n加第二个对话框："乙"。'
        comic.render_bubble(task, p, prompt="  \n" + mine + "  \n ")
        self.assertEqual(self.seen["prompt"], mine)

    def test_blank_override_falls_back_to_the_template(self):
        """空白串（前端框里只有空格）当没给，否则会把空提示词发给 qwen。"""
        task, p = self._task()
        comic.render_bubble(task, p, prompt="   ")
        self.assertEqual(self.seen["prompt"], p.bubble_prompt(split=False))

    def test_override_keeps_the_source_image_the_clean_raw(self):
        """源图仍是**干净底图**——那几行文字是「新增对话框」，在已有气泡的成图上
        再跑一次会叠出第二个气泡。想累积着改字走 render_manual（源图＝成品）。"""
        task, p = self._task()
        with open(task.final_path(p), "wb") as f:
            f.write(b"OLDFINAL")
        comic.render_bubble(task, p, prompt="改")
        self.assertEqual(self.seen["src"], task.raw_path(p))


class StudioPageTest(unittest.TestCase):
    """漫画工作台页面的接线：签名缺字段 = 界面不刷新，后端再多字段也白搭。"""

    def _page(self):
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "web", "comic_studio.html")
        with open(path, encoding="utf-8") as f:
            return f.read()

    def test_refresh_signature_includes_image_rev(self):
        """逐格 diff 的签名必须带 `p.rev`（图片文件指纹）。

        手动跑 Qwen 是**就地覆盖成品**：state 还是 done、seed 没变、final_url
        一字不差 —— 签名里不带 rev 的话这一格永远不重建，新图刷不出来
        （2026-10-11 用户报的「重跑的结果没有给到界面」）。
        """
        page = self._page()
        # 签名数组是跨行的，`.join` 收尾正好把它夹住（re.S 让 `.` 吃到换行）
        m = re.search(r"const sig = \[(.+?)\]\.join", page, re.S)
        self.assertIsNotNone(m, "找不到逐格 diff 的签名")
        self.assertIn("p.rev", m.group(1))

    def test_method_switch_is_wired_to_the_request(self):
        """画法开关必须真的把 split 发出去——只画下拉、不发参数等于摆设。

        2026-10-11 用户拍板：一遍画完做默认，但要留「分开画」当退路。前端这个
        开关若没接到 /api/comic/bubble 的 split 上，切了也不生效，而且不会有
        任何报错——典型的静默失效。
        """
        page = self._page()
        self.assertIn("meth-", page)                       # 每格一个开关
        m = re.search(r"api\('/api/comic/bubble',\s*\{[^}]*\}", page)
        self.assertIsNotNone(m, "找不到页面里调 /api/comic/bubble 的地方")
        self.assertIn("split", m.group(0))

    def test_method_switch_survives_panel_rebuild(self):
        """开关选中的值也要像手输指令一样存住。

        这一格的状态一变就整块重建 DOM；不填回来的话跑到第二遍时它会悄悄弹回
        「一遍画完」，用户以为自己在用分开画，其实不是。
        """
        page = self._page()
        self.assertIn("const _method", page)
        self.assertIn("_method[p.no]", page)

    def test_prompt_peek_pulls_the_exact_text_from_the_backend(self):
        """「完整提示词」框必须真的去 `/api/comic/prompt` 拉，而不是前端自己拼一份。

        用户 2026-10-11 的要求：「我需要看到完整的发给 qwen 的文字的内容是什么，
        我可以进行修改」。前端若自己拼，模型模板一改这里就悄悄跟不上了——看到的
        和发出去的就成了两段话。
        """
        page = self._page()
        self.assertIn("/api/comic/prompt", page)
        self.assertIn("_peekOpen", page)              # 展开/收起的开关
        self.assertIn("_promptText[p.no]", page)      # 重建后不丢（2 秒轮询会重建）

    def test_prompt_peek_sends_the_edited_text_verbatim(self):
        """框里的文字要当 `prompt` 发回 /api/comic/bubble，源图仍是干净底图。

        只画一个输入框、不把它发出去，等于摆设——而且不会有任何报错。
        """
        page = self._page()
        calls = re.findall(r"api\('/api/comic/bubble',\s*\{[^}]*\}", page)
        self.assertTrue(calls, "找不到页面里调 /api/comic/bubble 的地方")
        self.assertTrue(any("prompt" in c for c in calls),
                        "手改的提示词必须真的发出去（只画框不发送等于摆设）")
        self.assertIn("mpeek", page)                  # 提示词框的单独样式/选择器


class OfficialTextRulesTest(unittest.TestCase):
    """按 Qwen 官方文本渲染规范守住的几条底线（2026-10-11 第 9 轮重整）。

    官方（Qwen-Image-Edit 官方 Space 的 Edit Instruction Rewriter）原话：文字
    内容必须用英文双引号包住；总则要求简洁、避免长句；否定内容走
    `negative_prompt` 参数、正向只写要什么。用户实测反馈：`文字（EXACT）："…"`
    里的 EXACT 被当正文，画进了成品气泡。

    这个类的意义是**反向锁死**：以后谁再往模板里加标记词、序号或否定句，这里
    直接红——因为抑制这类改动的成本，远低于再跑一轮 50 秒的图去发现它。
    """

    def _prompts(self):
        """四种形状各来一份：单气泡 / 单气泡+旁白 / 一次画两个 / 第二遍补气泡。"""
        single = comic.Panel("01", "s", "一", speaker="女")
        with_label = comic.Panel("01", "s", "一", "甲\n乙", speaker="女")
        merged = comic.Panel("01", "s", "一", "甲", "二",
                             speaker="女", speaker2="我")
        second = comic.Panel("01", "s", "一", "二",
                             speaker="女", speaker2="我")
        return {
            "单气泡": single.bubble_prompt(),
            "单气泡+旁白": with_label.bubble_prompt(),
            "一次画两个": merged.bubble_prompt(),
            "第二遍": second.bubble2_prompt(),
        }

    def test_no_marker_words(self):
        """引号就是唯一标记，没有 EXACT 之类的前缀。"""
        for name, prompt in self._prompts().items():
            self.assertNotIn("EXACT", prompt, name)

    def test_no_negative_sentences(self):
        """正文里一句否定都不许有——否定会把那个词再喂一遍给模型（白熊效应）。"""
        for name, prompt in self._prompts().items():
            self.assertNotIn("不要", prompt, name)

    def test_no_bracketed_numbering(self):
        """`【气泡1】` 这类序号是给人看的，模型会当素材。"""
        for name, prompt in self._prompts().items():
            self.assertNotIn("【", prompt, name)
            self.assertNotIn("】", prompt, name)

    def test_dialogue_is_wrapped_in_english_double_quotes(self):
        prompts = self._prompts()
        for name, prompt in prompts.items():
            self.assertIn('"', prompt, name)
        self.assertIn('"一"', prompts["单气泡"])
        self.assertIn('"甲"', prompts["单气泡+旁白"])
        self.assertIn('"二"', prompts["一次画两个"])

    def test_counts_are_positive(self):
        """条数与归属一律正面清点，不再用「不要出现第三个气泡」。"""
        p = self._prompts()
        self.assertIn("画面里的文字一共这一处", p["单气泡"])
        self.assertIn("画面里的文字一共这两处", p["单气泡+旁白"])
        self.assertIn("画面里的文字一共这三处", p["一次画两个"])
        self.assertIn("画面里的文字一共这两处", p["第二遍"])

    def test_label_style_is_stated_positively(self):
        prompt = self._prompts()["单气泡+旁白"]
        self.assertIn("纯文字加白色描边", prompt)

    def test_keep_unchanged_clause_present(self):
        """官方/社区都要求显式声明「保持不变」的那部分。"""
        for name, prompt in self._prompts().items():
            self.assertIn("保持不变", prompt, name)


class DownloadFallbackTest(unittest.TestCase):
    """取图失败不再一次判死（2026-10-12 加）。

    两次丢图的现场（logs/agent.log）一模一样——RAM 守卫重启 ComfyUI 的同一
    秒，客户端正在 `/view` 取那张**已经画好**的图，`ConnectionResetError(10054)`
    直接把整格判死：

        00:36:34,105 系统可用内存只剩 2.6GB（低于 3.0GB 水位），重启 ComfyUI 释放
        00:36:34,870 [格 01] 场景渲染失败：ConnectionResetError(10054 ...)

    钉住三层行为：先重试；窗口用尽再从 ComfyUI output 目录复制同名文件；
    两样都没有才抛原异常。另外钉住兜底**只认纯文件名**。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.dst = os.path.join(self.tmp, "raw", "01_raw.png")
        for name, value in (("_DOWNLOAD_RETRY_GAP", 0),
                            ("_DOWNLOAD_RETRY_SECS", 60)):
            p = mock.patch.object(comic, name, value)   # 真等 3 秒没意义
            p.start()
            self.addCleanup(p.stop)

    class _Resp(object):
        content = b"PNG"

        def raise_for_status(self):
            pass

    def test_retries_then_succeeds(self):
        """第一次被掐、第二次拿到 → 不惊动兜底，图也不该少一个字节。"""
        calls = []

        def fake_get(url, **kw):
            calls.append(url)
            if len(calls) == 1:
                raise requests.exceptions.ConnectionError(
                    ("Connection aborted.", ConnectionResetError(10054)))
            return self._Resp()

        with mock.patch.object(comic._session, "get", fake_get):
            got = comic._download("01_raw.png", self.dst)
        self.assertEqual(2, len(calls))
        self.assertEqual(self.dst, got)
        with open(self.dst, "rb") as f:
            self.assertEqual(b"PNG", f.read())

    def test_falls_back_to_comfy_output_dir(self):
        """窗口用尽 → 从 ComfyUI output 目录复制同名文件（图本来就在那儿）。"""
        out_dir = os.path.join(self.tmp, "comfy_output")
        os.makedirs(out_dir)
        with open(os.path.join(out_dir, "01_raw.png"), "wb") as f:
            f.write(b"FROM_DISK")

        def boom(url, **kw):
            raise requests.exceptions.ConnectionError("ComfyUI 挂了")

        with mock.patch.object(comic._session, "get", boom), \
                mock.patch.object(comic, "COMFYUI_OUTPUT_DIR", out_dir), \
                mock.patch.object(comic, "_DOWNLOAD_RETRY_SECS", 0):
            got = comic._download("01_raw.png", self.dst)
        self.assertEqual(self.dst, got)
        with open(self.dst, "rb") as f:
            self.assertEqual(b"FROM_DISK", f.read())

    def test_raises_when_no_fallback_configured(self):
        """没配 COMFYUI_OUTPUT_DIR（默认空）→ 只重试，取不到照旧抛。"""
        def boom(url, **kw):
            raise requests.exceptions.ConnectionError("ComfyUI 挂了")

        with mock.patch.object(comic._session, "get", boom), \
                mock.patch.object(comic, "COMFYUI_OUTPUT_DIR", ""), \
                mock.patch.object(comic, "_DOWNLOAD_RETRY_SECS", 0):
            with self.assertRaises(requests.exceptions.ConnectionError):
                comic._download("01_raw.png", self.dst)
        self.assertFalse(os.path.exists(self.dst))

    def test_rejects_name_with_path(self):
        """兜底只认纯文件名——带路径成分的一律不复制（否则能读到任意文件）。"""
        out_dir = os.path.join(self.tmp, "comfy_output")
        os.makedirs(out_dir)
        with open(os.path.join(self.tmp, "secret.png"), "wb") as f:
            f.write(b"SECRET")

        def boom(url, **kw):
            raise requests.exceptions.ConnectionError("ComfyUI 挂了")

        with mock.patch.object(comic._session, "get", boom), \
                mock.patch.object(comic, "COMFYUI_OUTPUT_DIR", out_dir), \
                mock.patch.object(comic, "_DOWNLOAD_RETRY_SECS", 0):
            with self.assertRaises(requests.exceptions.ConnectionError):
                comic._download("../secret.png", self.dst)
        self.assertFalse(os.path.exists(self.dst))

    def test_missing_file_is_not_faked(self):
        """output 目录里没有同名文件时**不猜**（不取最新一张），照旧抛。"""
        out_dir = os.path.join(self.tmp, "comfy_output")
        os.makedirs(out_dir)
        with open(os.path.join(out_dir, "999_other.png"), "wb") as f:
            f.write(b"OTHER")

        def boom(url, **kw):
            raise requests.exceptions.ConnectionError("ComfyUI 挂了")

        with mock.patch.object(comic._session, "get", boom), \
                mock.patch.object(comic, "COMFYUI_OUTPUT_DIR", out_dir), \
                mock.patch.object(comic, "_DOWNLOAD_RETRY_SECS", 0):
            with self.assertRaises(requests.exceptions.ConnectionError):
                comic._download("01_raw.png", self.dst)
        self.assertFalse(os.path.exists(self.dst))


class UpdatePanelSceneTest(unittest.TestCase):
    """单格快改画面行（2026-10-12 加）。

    起因：补正员照构图冲突清单把 03 格的 `navy pleated skirt` 删了，裙子从
    画面上消失。用户要「只改一个词就重画这一格」，而不是整份剧本推倒重跑。
    钉住：只动目标格的画面行、其他格一字不动、no 归一、找不到返回 False、
    旧续行一并作废。
    """

    SCRIPT = (
        "# 标题：测试\n"
        "\n"
        "## 01\n"
        "画面: safe, 1girl, solo, black hair, full body\n"
        "气泡: （女）你好。\n"
        "标签1: 第一格旁白\n"
        "## 02\n"
        "画面: safe, 1girl, solo, blonde hair,\n"
        "green eyes, upper body\n"
        "气泡: （女）第二格。\n"
        "## 03\n"
        "画面: safe, 1girl, solo focus, 1other, close-up\n"
        "气泡: （女）第三格。\n"
    )

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "script.md")
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(self.SCRIPT)

    def _read(self):
        with open(self.path, encoding="utf-8") as f:
            return f.read()

    def test_replaces_only_target_scene_line(self):
        ok = comic.update_panel_scene(
            self.path, "03",
            "safe, 1girl, solo focus, 1other, close-up, navy pleated skirt")
        self.assertTrue(ok)
        _, panels = comic.parse_script(self.path)
        by_no = {p["no"]: p for p in panels}
        # 目标格整体换成了新画面行
        self.assertEqual(by_no["03"]["scene"],
                         "safe, 1girl, solo focus, 1other, close-up, "
                         "navy pleated skirt")
        # 其他格一字不动（含气泡/旁白）
        self.assertEqual(by_no["01"]["scene"],
                         "safe, 1girl, solo, black hair, full body")
        self.assertEqual(by_no["01"]["bubble"], "你好。")
        self.assertEqual(by_no["01"]["speaker"], "女")
        self.assertEqual(by_no["02"]["scene"],
                         "safe, 1girl, solo, blonde hair, green eyes, upper body")

    def test_panel_number_is_normalized(self):
        """no 传 "2" 和 "02" 等效。"""
        self.assertTrue(comic.update_panel_scene(
            self.path, "2", "safe, 1girl, solo, red hair"))
        _, panels = comic.parse_script(self.path)
        by_no = {p["no"]: p for p in panels}
        self.assertIn("red hair", by_no["02"]["scene"])

    def test_missing_panel_returns_false(self):
        self.assertFalse(comic.update_panel_scene(
            self.path, "09", "safe, 1girl"))
        self.assertEqual(self._read(), self.SCRIPT)   # 没找到就不许动文件

    def test_continuation_lines_are_replaced_too(self):
        """02 的画面行带折行续行：整行替换后续行必须一起消失。"""
        ok = comic.update_panel_scene(
            self.path, "02", "safe, 1girl, solo, red hair, green eyes")
        self.assertTrue(ok)
        _, panels = comic.parse_script(self.path)
        by_no = {p["no"]: p for p in panels}
        self.assertEqual(by_no["02"]["scene"],
                         "safe, 1girl, solo, red hair, green eyes")
        # 续行内容不能再漏进 scene 或气泡
        self.assertNotIn("upper body", by_no["02"]["scene"])
        self.assertEqual(by_no["02"]["bubble"], "第二格。")

    def test_scene_change_resets_panel_via_state_contract(self):
        """改画面行 + load_state 契约 = 该格自动退回 pending（重跑链的前提）。

        顺序很重要：先用**旧** scene 存一份 done 状态，再改画面行——契约
        （s["scene"] != p.scene → 退回 pending）正是在这个差值上触发的。
        """
        meta, panels = comic.parse_script(self.path)
        task = comic.Task(self.path, meta, panels)
        task.panels[0].state = "done"          # 假装 01 已经画完（旧 scene 入账）
        task.save_state()
        ok = comic.update_panel_scene(
            self.path, "01", "safe, 1girl, solo, white dress, full body")
        self.assertTrue(ok)
        _, panels2 = comic.parse_script(self.path)   # 重新解析拿到新 scene
        task2 = comic.Task(self.path, meta, panels2)
        task2.load_state()
        self.assertEqual(task2.panels[0].state, "pending")   # 契约自动作废旧图


if __name__ == "__main__":
    unittest.main()
