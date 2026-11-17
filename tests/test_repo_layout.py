# -*- coding: utf-8 -*-
"""仓库结构守卫：根目录只允许真正的入口与运维脚本。

一次性探针、验证脚本、生成脚本统一放在 tools/ 下，避免接手的人
在根目录看到几十个 `_probe_*`、`_verify_*` 不知道哪个能删。
"""
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

ALLOWED_ROOT_PY = {
    "agent.py",          # 5174 主站入口
    "admin_app.py",      # 管理站入口
    "friend_app.py",     # 5175 公网网关入口
    "friend_tunnel.py",  # 隧道地址维护
    "clean_zombies.py",  # 运维：清残留进程
    "export_presets.py", # 运维：导出预设
    "import_presets.py", # 运维：导入预设
    "napcat_qr.py",      # 运维：QQ 协议端登录
    "restart_servers.py",# 运维：手动重启
}

TOOLS_DIRS = ("legacy", "generators")


class RepoLayoutTest(unittest.TestCase):
    def test_root_has_no_underscore_scratch_scripts(self):
        root_py = sorted(
            name for name in os.listdir(ROOT)
            if name.endswith(".py") and os.path.isfile(os.path.join(ROOT, name))
        )
        scratch = [name for name in root_py if name.startswith("_")]
        self.assertEqual(
            [], scratch,
            "根目录仍有一次性脚本：%s；请移到 tools/legacy 或 tools/generators"
            % ", ".join(scratch),
        )

    def test_root_python_is_limited_to_entrypoints_and_ops(self):
        root_py = sorted(
            name for name in os.listdir(ROOT)
            if name.endswith(".py") and os.path.isfile(os.path.join(ROOT, name))
        )
        extra = [name for name in root_py if name not in ALLOWED_ROOT_PY]
        self.assertEqual(
            [], extra,
            "根目录出现未登记的 Python 文件：%s；入口/运维脚本需先登记到 ALLOWED_ROOT_PY"
            % ", ".join(extra),
        )

    def test_tools_subdirs_exist(self):
        for name in TOOLS_DIRS:
            self.assertTrue(
                os.path.isdir(os.path.join(ROOT, "tools", name)),
                "缺少 tools/%s" % name,
            )


if __name__ == "__main__":
    unittest.main()
