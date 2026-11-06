"""state/ 路径的测试隔离判据：只认测试 runner 进程。

2026-11-06 事故：原判据是 `"unittest" in sys.modules`，而实测 `import
matplotlib` 就会把 unittest 带进 sys.modules。QQ 机器人运行途中一旦加载
了这类库，之后**懒加载**的模块（image_review 的 PATH / REVIEW_DIR 是模块
import 那一刻算的）就把队列和图写进系统临时目录，网页端读的却是项目
state/ —— 人工二审列表当晚少掉全部新增记录，图本身还躺在 temp 里。

钉住三件事：测试 runner 仍隔离；生产入口即使 sys.modules 里有 unittest
也不隔离；命令行参数里出现 unittest 字样不算测试进程。
"""

import os
import sys
import unittest
from contextlib import ExitStack
from unittest import mock

from app import config


class StatePathTest(unittest.TestCase):

    def setUp(self):
        p = mock.patch.object(config, "_TEST_STATE_DIR", None)
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.dict(os.environ, {"AGENT_STATE_TEST": ""})
        p.start()
        self.addCleanup(p.stop)

    def _production_entry(self):
        """把当前进程伪装成 `python -m app.qq_bot` 启动的生产进程。"""
        stack = ExitStack()
        main = sys.modules["__main__"]
        stack.enter_context(mock.patch.object(
            main, "__file__", "D:/AI/agent_my_test/app/qq_bot.py",
            create=True))
        stack.enter_context(mock.patch.object(sys, "argv", ["app/qq_bot.py"]))
        stack.enter_context(mock.patch.object(
            sys, "orig_argv", ["python.exe", "-m", "app.qq_bot"]))
        return stack

    def test_runner_process_is_isolated(self):
        """本文件由 `python -m unittest discover` 驱动，仍走临时目录。"""
        self.assertTrue(config._is_test_process())
        a = config.state_path("image_review.jsonl")
        b = config.state_path("image_review.jsonl")
        self.assertEqual(a, b)
        self.assertIn("agent_state_test_", a)
        self.assertNotIn(os.path.join("agent_my_test", "state"), a)

    def test_production_entry_uses_real_state_dir(self):
        """生产入口：即使 sys.modules 里有 unittest，也写项目 state/。"""
        with self._production_entry():
            self.assertIn("unittest", sys.modules)
            self.assertFalse(config._is_test_process())
            self.assertEqual(
                config.state_path("image_review.jsonl"),
                os.path.join(config.BASE_DIR, "state", "image_review.jsonl"))

    def test_matplotlib_does_not_redirect_production_state(self):
        """回归：matplotlib 会把 unittest 带进 sys.modules，路径不能变。"""
        with self._production_entry():
            try:
                import matplotlib  # noqa: F401
            except ImportError:
                self.skipTest("本机没有 matplotlib")
            self.assertIn("unittest", sys.modules)
            self.assertEqual(
                config.state_path("image_review.jsonl"),
                os.path.join(config.BASE_DIR, "state", "image_review.jsonl"))

    def test_script_text_with_unittest_word_is_not_test_process(self):
        """-c 脚本正文 / 业务参数里带 unittest 字样，不算测试进程。"""
        with ExitStack() as stack:
            main = sys.modules["__main__"]
            stack.enter_context(mock.patch.object(
                main, "__file__", "", create=True))
            stack.enter_context(mock.patch.object(sys, "argv", ["-c"]))
            stack.enter_context(mock.patch.object(
                sys, "orig_argv",
                ["python.exe", "-c", "print('unittest in sys.modules')"]))
            self.assertFalse(config._is_test_process())
            self.assertEqual(
                config.state_path("x.jsonl"),
                os.path.join(config.BASE_DIR, "state", "x.jsonl"))

    def test_explicit_env_switch_forces_isolation(self):
        """临时脚本想强制隔离，显式设 AGENT_STATE_TEST=1。"""
        with self._production_entry(), \
                mock.patch.dict(os.environ, {"AGENT_STATE_TEST": "1"}):
            self.assertTrue(config._is_test_process())
            self.assertIn("agent_state_test_",
                          config.state_path("image_review.jsonl"))


if __name__ == "__main__":
    unittest.main()
