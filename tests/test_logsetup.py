"""统一日志配置：落盘文件、幂等、入口点必须调用。

零网络零显卡。日志文件写进临时目录，绝不碰仓库里的 logs/。
"""

import inspect
import logging
import os
import tempfile
import unittest
from unittest import mock

from app import logsetup


class SetupTest(unittest.TestCase):
    """setup() 的行为：写文件、幂等、目录建不起来也不炸。"""

    def setUp(self):
        # 记下原有的 root handlers，用完还原——否则文件 handler 会挂在整个测试
        # 进程上，污染别的用例的输出。
        self._before = list(logging.getLogger().handlers)
        self._saved = set(logsetup._configured)
        logsetup._configured.clear()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def tearDown(self):
        root = logging.getLogger()
        for h in list(root.handlers):
            if h not in self._before:
                root.removeHandler(h)
                h.close()
        logsetup._configured.clear()
        logsetup._configured.update(self._saved)

    def _setup(self, name="unit"):
        with mock.patch.object(logsetup, "LOG_DIR", self._tmp.name):
            logsetup.setup(name)
        return os.path.join(self._tmp.name, name + ".log")

    def _read(self, path):
        for h in logging.getLogger().handlers:
            h.flush()
        with open(path, "r", encoding="utf-8") as f:
            return f.read()

    def test_info_lands_in_the_file(self):
        path = self._setup()
        logging.getLogger("llm").info("[llm] volc / m1 stream [1/2]")
        self.assertIn("[llm] volc / m1 stream [1/2]", self._read(path))

    def test_file_line_carries_timestamp_and_level(self):
        """行格式要和 `INFO qq_bot:` 那批一致——这是当初「看不到模型日志」的
        症结：print 的行长得跟别的不一样，混在一起就被当成杂音漏过去了。"""
        path = self._setup()
        logging.getLogger("llm").info("[llm] x / y sync [1/1]")
        line = [l for l in self._read(path).splitlines() if "[llm]" in l][0]
        self.assertIn(" INFO llm: ", line)

    def test_second_setup_does_not_duplicate_lines(self):
        path = self._setup()
        with mock.patch.object(logsetup, "LOG_DIR", self._tmp.name):
            logsetup.setup("unit")               # 重复调用应当直接返回
        logging.getLogger("llm").info("[llm] once")
        self.assertEqual(self._read(path).count("[llm] once"), 1)

    def test_unwritable_log_dir_does_not_raise(self):
        """logs/ 建不起来（比如被一个同名文件占住）不能拦启动。"""
        blocker = os.path.join(self._tmp.name, "blocker")
        with open(blocker, "w", encoding="utf-8") as f:
            f.write("x")
        with mock.patch.object(logsetup, "LOG_DIR",
                               os.path.join(blocker, "logs")):
            logsetup.setup("unit")               # 不抛
        # 控制台 handler 仍然挂上了，日志不至于全哑
        console = [h for h in logging.getLogger().handlers
                   if isinstance(h, logging.StreamHandler)
                   and not isinstance(h, logging.FileHandler)]
        self.assertTrue(console)


class EntryPointTest(unittest.TestCase):
    """新加入口最容易漏的就是 setup()：漏了之后 `log.info` 会被 lastResort
    悄悄丢掉，**而且不报错**——只能靠这两条断言拦住。"""

    def test_qq_bot_main_configures_logging(self):
        from app import qq_bot
        self.assertIn("logsetup.setup(", inspect.getsource(qq_bot.main))

    def test_web_run_configures_logging(self):
        from app import main as main_mod
        self.assertIn("logsetup.setup(", inspect.getsource(main_mod.run))


class ModelCallLogTest(unittest.TestCase):
    """模型调用 / 降级 / 识图三条路都要留下可 grep 的行。"""

    def test_effective_model_is_logged_with_attempt(self):
        from app import llm
        with self.assertLogs("llm", level="INFO") as cm:
            llm._log_effective({"provider": "volc", "model": "m1"},
                               stream=True, attempt=(1, 2))
        self.assertIn("[llm] volc / m1 stream [1/2]", cm.output[0])

    def test_blacklist_is_logged(self):
        from app import llm
        saved = dict(llm._DEAD)
        self.addCleanup(lambda: (llm._DEAD.clear(), llm._DEAD.update(saved)))
        with self.assertLogs("llm", level="INFO") as cm:
            llm._mark_dead(("volc", "m1"), "HTTP 500")
        self.assertIn("[chain] volc / m1 拉黑", cm.output[0])

    def test_vision_call_is_logged(self):
        """识图绕开 llm.py 自己发请求，从前完全不产生 [llm] 行。"""
        from app import vision
        resp = mock.Mock(status_code=200)
        resp.json.return_value = {"choices": [{"message": {"content": "猫"}}]}
        pid = sorted(vision.PROVIDERS)[0]
        with mock.patch.object(vision, "VISION_PROVIDER", pid), \
                mock.patch.object(vision, "VISION_MODEL", "vm-test"), \
                mock.patch.object(vision, "active_choice", return_value=(None, "")), \
                mock.patch.object(vision._session, "post", return_value=resp):
            with self.assertLogs("vision", level="INFO") as cm:
                vision.describe("data:image/jpeg;base64,AAA")
        self.assertIn("[llm] %s / vm-test vision" % pid, "\n".join(cm.output))


if __name__ == "__main__":
    unittest.main()
