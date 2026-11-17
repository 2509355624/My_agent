# -*- coding: utf-8 -*-
"""画廊的「选择文件夹」入口。

浏览器拿不到本机绝对路径：`<input type="file" webkitdirectory>` 只能拿到
相对路径，File System Access API 也不把真实路径交给网页。所以站主在网页上
选目录这件事，只能由服务端在自己运行的 Windows 会话里弹一个原生对话框，
再把选中的绝对路径回给前端。

对话框放在子进程里跑：Tk 必须在有窗口会话的进程里，Flask 的请求线程直接
建 Tk 容易和主线程抢消息循环。子进程正常退出时 stdout 只有一行路径；用户
取消返回码 2，前端拿到「没有选择文件夹」。
"""

import os
import subprocess
import sys

# app/features/gallery/folder_picker.py → 往上四层是项目根
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
TIMEOUT = 180


def pick(initial=""):
    """打开 Windows 原生选文件夹窗口。→ {"path": ...} 或 {"error": ...}"""
    if os.name != "nt":
        return {"error": "当前系统不支持原生文件夹选择"}
    initial = str(initial or "").strip()
    if initial and not os.path.isdir(initial):
        initial = ""

    cmd = [sys.executable, "-m", "app.features.gallery.folder_picker"]
    if initial:
        cmd.append(initial)
    try:
        proc = subprocess.run(
            cmd, cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
            timeout=TIMEOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        return {"error": "选择文件夹超时，请再试一次"}
    except OSError as e:
        return {"error": "打不开文件夹选择窗口：%s" % e}

    if proc.returncode == 2:
        return {"error": "没有选择文件夹"}
    path = (proc.stdout or "").strip().splitlines()
    path = path[-1].strip() if path else ""
    if proc.returncode != 0 or not path:
        detail = (proc.stderr or "").strip().splitlines()
        detail = detail[-1] if detail else "未知错误"
        return {"error": "选择文件夹失败：%s" % detail}
    return {"path": path}


def _dialog():
    """子进程入口：只做一件事，弹窗、打印路径。"""
    import tkinter as tk
    from tkinter import filedialog

    initial = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        root.update()
        path = filedialog.askdirectory(
            parent=root, title="选择要扫描的文件夹",
            initialdir=initial if os.path.isdir(initial) else None,
            mustexist=True)
        root.destroy()
    except Exception as e:                       # noqa: BLE001
        print(str(e), file=sys.stderr)
        raise SystemExit(1)
    if not path:
        raise SystemExit(2)
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass
    print(os.path.abspath(path))


if __name__ == "__main__":
    _dialog()
