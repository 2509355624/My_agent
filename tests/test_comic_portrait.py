import copy
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import comic_portrait
from app.comic_portrait import PortraitError


# ─── 提示词解析 ───────────────────────────────────────

class ParseLinesTest(unittest.TestCase):
    def test_strips_fences_ordinals_and_blank_lines(self):
        text = ("```\n"
                "1. 1girl, long black hair, standing in a wheat field\n"
                "\n"
                "2、1girl, school uniform, by the window at dusk\n"
                "- 1girl, white dress, rooftop, sunset backlight\n"
                "3) 1girl, cardigan, café, morning light\n"
                "```")
        self.assertEqual(comic_portrait.parse_lines(text), [
            "1girl, long black hair, standing in a wheat field",
            "1girl, school uniform, by the window at dusk",
            "1girl, white dress, rooftop, sunset backlight",
            "1girl, cardigan, café, morning light"])

    def test_strips_leading_labels(self):
        text = ("提示词：1girl, sundress, beach\n"
                "画面: 1girl, apron, kitchen\n")
        self.assertEqual(comic_portrait.parse_lines(text), [
            "1girl, sundress, beach", "1girl, apron, kitchen"])

    def test_empty_text_is_empty(self):
        self.assertEqual(comic_portrait.parse_lines(""), [])
        self.assertEqual(comic_portrait.parse_lines("```\n\n```"), [])


class WritePromptsTest(unittest.TestCase):
    """提示词员没交齐作业就报错重跑——不猜不补（用户拍板的哲学）。"""

    def _llm(self, text):
        return mock.patch("app.llm.call_llm", return_value=text)

    def test_returns_n_lines(self):
        text = "\n".join("1girl, scene %d" % i for i in range(4))
        with self._llm(text):
            out = comic_portrait.write_prompts("花田写真", "1girl, black hair", 4)
        self.assertEqual(len(out), 4)
        self.assertTrue(all(o.startswith("1girl") for o in out))

    def test_too_few_lines_is_an_error_not_a_patch(self):
        text = "\n".join("1girl, scene %d" % i for i in range(2))
        with self._llm(text):
            with self.assertRaises(PortraitError) as cm:
                comic_portrait.write_prompts("花田写真", "", 4)
        self.assertIn("只交了 2 条", str(cm.exception))

    def test_empty_reply_is_an_error(self):
        with self._llm("   "):
            with self.assertRaises(PortraitError):
                comic_portrait.write_prompts("花田写真", "", 3)

    def test_empty_brief_is_an_error(self):
        with self.assertRaises(PortraitError):
            comic_portrait.write_prompts("  ", "", 3)

    def test_n_is_clamped(self):
        text = "\n".join("1girl, scene %d" % i for i in range(30))
        with self._llm(text):
            out = comic_portrait.write_prompts("x", "", 99)
        self.assertEqual(len(out), comic_portrait.MAX_SHOTS)


# ─── 跑图与账本 ───────────────────────────────────────

def _fake_download(name, dst):
    """假下载：写个占位 PNG 就完事（句柄要关，不然 ResourceWarning）。"""
    with open(dst, "wb") as f:
        f.write(b"png")


class RunTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="comic_portrait_")
        self.subs = []          # 记录每次提交的 (workflow, prompt, seed)
        self.seeds = iter(range(1000, 1000 + 100))

        def _fake_submit(wf, skill, prompt, seed):
            self.subs.append((copy.deepcopy(wf), prompt, seed, skill))
            return "fake_%d.png" % len(self.subs)

        p1 = mock.patch.object(comic_portrait.comic, "_submit_retry",
                               side_effect=lambda make, what: make())
        p2 = mock.patch.object(comic_portrait.comic, "_submit",
                               side_effect=_fake_submit)
        p3 = mock.patch.object(comic_portrait.comic, "_download",
                               side_effect=_fake_download)
        p4 = mock.patch.object(comic_portrait.comic, "_new_seed",
                               side_effect=lambda: next(self.seeds))
        p5 = mock.patch.object(comic_portrait.comic, "_require_comfy")
        p6 = mock.patch.object(comic_portrait, "write_prompts",
                               return_value=["1girl, wheat field, smile",
                                             "1girl, rooftop, sunset",
                                             "1girl, classroom, nap"])
        for p in (p1, p2, p3, p4, p5, p6):
            p.start()
            self.addCleanup(p.stop)

    def _run(self):
        return comic_portrait.run(self.tmp, "花田写真", "1girl, long black hair", 3)

    def test_submits_each_prompt_with_its_own_seed(self):
        shots = self._run()
        self.assertEqual(len(shots), 3)
        self.assertEqual([s["no"] for s in shots], ["01", "02", "03"])
        self.assertEqual([s[1] for s in self.subs],
                         ["1girl, wheat field, smile", "1girl, rooftop, sunset",
                          "1girl, classroom, nap"])
        # 每张种子不同（LLM 写的三条不同画面，不能共用种子）
        self.assertEqual(len({s[2] for s in self.subs}), 3)
        # 全部走 anima-line 渠道
        self.assertEqual({s[3] for s in self.subs}, {comic_portrait.SKILL})

    def test_workflow_carries_prompt_and_render_overrides(self):
        self._run()
        wf, prompt, seed, _skill = self.subs[0]
        texts = [v for n in wf.values()
                 for v in (n.get("inputs") or {}).values() if isinstance(v, str)]
        self.assertTrue(any(prompt in t for t in texts))
        # 种子落在第一段 KSampler 上（int，不是字符串）
        seeds = [v for n in wf.values()
                 for v in (n.get("inputs") or {}).values()
                 if n.get("class_type") == "KSampler" and v == seed]
        self.assertTrue(seeds)

    def test_meta_and_ledger_land_on_disk(self):
        self._run()
        with open(comic_portrait.portrait_path(self.tmp), encoding="utf-8") as f:
            meta = json.load(f)
        self.assertEqual(meta["n"], 3)
        self.assertEqual(meta["heroine"], "1girl, long black hair")
        ledger = comic_portrait.load_prompts(self.tmp)
        self.assertEqual(len(ledger), 3)
        self.assertEqual([s["state"] for s in ledger], ["rendered"] * 3)
        for s in ledger:
            self.assertTrue(os.path.isfile(
                comic_portrait.shot_path(self.tmp, s["no"])))

    def test_single_failure_does_not_sink_the_batch(self):
        real = comic_portrait._render_one

        def flaky(task_dir, no, prompt, seed, **kw):
            if no == "02":
                raise RuntimeError("ComfyUI 炸了")
            return real(task_dir, no, prompt, seed, **kw)

        with mock.patch.object(comic_portrait, "_render_one", side_effect=flaky):
            shots = self._run()
        states = {s["no"]: s["state"] for s in shots}
        self.assertEqual(states, {"01": "rendered", "02": "error", "03": "rendered"})
        # 失败态要「能看见原因」
        self.assertEqual(shots[1]["error"], "ComfyUI 炸了")


class ReseedTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="comic_portrait_reseed_")
        comic_portrait.save_prompts(self.tmp, [
            {"no": "01", "prompt": "1girl, wheat field", "seed": 111,
             "state": "error", "error": "上次炸了"},
            {"no": "02", "prompt": "1girl, rooftop", "seed": 222,
             "state": "rendered", "error": None}])
        p1 = mock.patch.object(comic_portrait.comic, "_require_comfy")
        p2 = mock.patch.object(comic_portrait.comic, "_new_seed",
                               return_value=999)
        p3 = mock.patch.object(comic_portrait.comic, "_submit_retry",
                               side_effect=lambda make, what: make())
        p4 = mock.patch.object(comic_portrait.comic, "_submit",
                               return_value="x.png")
        p5 = mock.patch.object(comic_portrait.comic, "_download",
                               side_effect=_fake_download)
        for p in (p1, p2, p3, p4, p5):
            p.start()
            self.addCleanup(p.stop)

    def test_reseed_keeps_prompt_changes_seed(self):
        shot = comic_portrait.reseed_shot(self.tmp, "01")
        self.assertEqual(shot["seed"], 999)
        self.assertEqual(shot["prompt"], "1girl, wheat field")
        self.assertEqual(shot["state"], "rendered")
        ledger = comic_portrait.load_prompts(self.tmp)
        self.assertEqual(ledger[0]["seed"], 999)
        self.assertEqual(ledger[1]["seed"], 222)      # 别的张不动

    def test_unknown_shot_is_an_error(self):
        with self.assertRaises(PortraitError):
            comic_portrait.reseed_shot(self.tmp, "09")

    def test_reseed_failure_records_error_and_raises(self):
        with mock.patch.object(comic_portrait.comic, "_submit",
                               side_effect=RuntimeError("又炸了")):
            with self.assertRaises(RuntimeError):
                comic_portrait.reseed_shot(self.tmp, "02")
        ledger = comic_portrait.load_prompts(self.tmp)
        self.assertEqual(ledger[1]["state"], "error")
        self.assertEqual(ledger[1]["error"], "又炸了")


if __name__ == "__main__":
    unittest.main()
