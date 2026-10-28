"""ComfyUI 启动命令：看门狗和强杀重启必须用同一份。

这组测试钉的是「换到 nightly 之后别把旧实例拉起来」——2026-10-28 之前，
强杀重启走的是 `启动ComfyUI.bat`，里面写死 `ComfyUI_v037` 加
`--vram-headroom 1 --force-fp16`，既没有 nightly 必需的
`--disable-comfy-compiler`，又会去抢 8188。
"""

import os
import sys
import tempfile
import unittest
from unittest import mock

from app import comfy_launch


class LaunchArgsTest(unittest.TestCase):
    def test_nightly_flags_are_passed_through(self):
        raw = ("--listen 127.0.0.1 --port 8188 --disable-comfy-compiler "
               "--enable-triton-backend --enable-cors-header *")
        with mock.patch.object(comfy_launch, "COMFYUI_LAUNCH_ARGS", raw):
            args = comfy_launch.launch_args()
        for flag in ("--disable-comfy-compiler", "--enable-triton-backend"):
            self.assertIn(flag, args)
        self.assertIn("*", args)

    def test_port_follows_comfyui_url(self):
        with mock.patch.object(comfy_launch, "COMFYUI_LAUNCH_ARGS", "--port 8188"), \
                mock.patch.object(comfy_launch, "COMFYUI_URL",
                                  "http://127.0.0.1:8199"):
            args = comfy_launch.launch_args()
        self.assertEqual(args[args.index("--port") + 1], "8199")

    def test_port_is_added_when_absent(self):
        with mock.patch.object(comfy_launch, "COMFYUI_LAUNCH_ARGS",
                               "--listen 127.0.0.1"), \
                mock.patch.object(comfy_launch, "COMFYUI_URL",
                                  "http://127.0.0.1:8200"):
            args = comfy_launch.launch_args()
        self.assertEqual(args[-2:], ["--port", "8200"])


class CommandTest(unittest.TestCase):
    def test_command_is_python_plus_main_py(self):
        with tempfile.TemporaryDirectory() as d:
            open(os.path.join(d, "main.py"), "w").close()
            with mock.patch.object(comfy_launch, "COMFYUI_ROOT", d), \
                    mock.patch.object(comfy_launch, "COMFYUI_PYTHON",
                                      sys.executable), \
                    mock.patch.object(comfy_launch, "COMFYUI_LAUNCH_ARGS",
                                      "--port 8188"):
                cmd = comfy_launch.command()
        self.assertEqual(cmd[0], sys.executable)
        self.assertTrue(cmd[1].endswith("main.py"))
        self.assertEqual(cmd[-2:], ["--port", "8188"])

    def test_command_none_when_main_py_missing(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(comfy_launch, "COMFYUI_ROOT", d), \
                    mock.patch.object(comfy_launch, "COMFYUI_PYTHON",
                                      sys.executable):
                self.assertIsNone(comfy_launch.command())

    def test_command_none_when_python_missing(self):
        with tempfile.TemporaryDirectory() as d:
            open(os.path.join(d, "main.py"), "w").close()
            with mock.patch.object(comfy_launch, "COMFYUI_ROOT", d), \
                    mock.patch.object(comfy_launch, "COMFYUI_PYTHON",
                                      os.path.join(d, "nope.exe")):
                self.assertIsNone(comfy_launch.command())


class EnvTest(unittest.TestCase):
    def test_defaults_expandable_segments(self):
        """12GB 卡缺了这段很容易碎片化 OOM，重启拉起的实例必须带上。"""
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(comfy_launch.env()["PYTORCH_CUDA_ALLOC_CONF"],
                             "expandable_segments:True")

    def test_keeps_parent_setting(self):
        with mock.patch.dict(os.environ,
                             {"PYTORCH_CUDA_ALLOC_CONF": "max_split_size_mb:64"},
                             clear=True):
            self.assertEqual(comfy_launch.env()["PYTORCH_CUDA_ALLOC_CONF"],
                             "max_split_size_mb:64")


if __name__ == "__main__":
    unittest.main()
