# -*- coding: utf-8 -*-
"""ComfyUI 单实例启动护栏（2026-11-18）。

现象：ComfyUI 启动几十秒内端口还没开，控制台、看门狗、QQ 机器人各自
`process_pids()` 都看到“没端口”，于是各拉一套 main.py。两套抢 8188 时
一套占端口、一套卡启动，用户反复点重启，桌面 cmd 窗口跟着一直闪。
"""

import os
import unittest
from unittest import mock


class ProcessPidsTest(unittest.TestCase):
    def test_matches_only_the_configured_main_py(self):
        from app.features.image import comfy_launch

        root = r"D:\AI\ComfyUI-nightly"
        main = os.path.join(root, "main.py")
        fake = [
            mock.Mock(pid=111, info={"cmdline": [
                r"D:\AI\confyui_env\Scripts\python.exe", main,
                "--listen", "127.0.0.1"]}),
            mock.Mock(pid=222, info={"cmdline": [
                r"D:\AI\confyui_env\Scripts\python.exe", r"D:\AI\other\main.py"]}),
            mock.Mock(pid=333, info={"cmdline": [
                r"D:\AI\confyui_env\Scripts\python.exe", "-c",
                "print('not main')"]}),
        ]
        with mock.patch.object(comfy_launch, "COMFYUI_ROOT", root), \
             mock.patch.object(comfy_launch, "psutil") as ps:
            ps.process_iter.return_value = fake
            pids = comfy_launch.process_pids()
        self.assertEqual([111], pids)

    def test_no_psutil_returns_empty(self):
        from app.features.image import comfy_launch

        with mock.patch.object(comfy_launch, "psutil", None):
            self.assertEqual([], comfy_launch.process_pids())


class ForceRestartGuardTest(unittest.TestCase):
    def test_existing_booting_instance_is_not_duplicated(self):
        from app.features.image import jobs

        with mock.patch.object(jobs.comfy_gate, "busy_others", return_value=False), \
             mock.patch.object(jobs, "_comfy_pid_by_port", return_value=None), \
             mock.patch.object(jobs.comfy_launch, "process_pids",
                               return_value=[123]), \
             mock.patch.object(jobs, "_wait_comfy_port", return_value=True) as wait:
            self.assertTrue(jobs._force_restart_comfy(timeout=1))
        wait.assert_called_once_with(1)

    def test_stale_duplicate_instances_are_all_killed_with_tree(self):
        from app.features.image import jobs

        with mock.patch.object(jobs.comfy_gate, "busy_others", return_value=False), \
             mock.patch.object(jobs, "_comfy_pid_by_port",
                               side_effect=[456, None, None, 789]), \
             mock.patch.object(jobs.comfy_launch, "process_pids",
                               return_value=[123, 456]), \
             mock.patch.object(jobs, "_kill_comfy", return_value=True) as kill, \
             mock.patch.object(jobs.requests, "get",
                               return_value=mock.Mock(status_code=200)), \
             mock.patch.object(jobs, "_wait_comfy_port", return_value=True):
            # 只验证“两套都杀”，不真的拉起进程：后面 Popen 会被 mock。
            with mock.patch.object(jobs.subprocess, "Popen"):
                self.assertTrue(jobs._force_restart_comfy(timeout=3))
        self.assertEqual([mock.call(123), mock.call(456)], kill.call_args_list)


if __name__ == "__main__":
    unittest.main()
