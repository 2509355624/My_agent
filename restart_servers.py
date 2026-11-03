"""一键重启 5174 / 5175：先停旧的，再起新的。

## 为什么要有它

手动开窗口的问题不是「窗口多」，是**旧进程没死**：

    旧进程占着 5174 → 新进程绑端口失败（WinError 10048）→ 新窗口一闪而过
    → 你以为重启了，实际跑的还是半小时前的代码

这个脚本按端口找进程、杀掉、等端口真正释放，再起新的，并断言「新进程真的
绑上端口了」。失败会明确告诉你，而不是静默留一个旧进程。

## 用法

    python restart_servers.py            重启两个
    python restart_servers.py 5174       只重启主进程
    python restart_servers.py 5175       只重启群友页
    python restart_servers.py --status   只看状态，不动手
"""
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable

# 端口 → 启动脚本
SERVICES = {
    5174: "agent.py",
    5175: "friend_app.py",
}

WAIT_STOP = 15        # 等进程退出的上限（秒）
WAIT_BIND = 20        # 等新进程绑上端口的上限（秒）


def _run(cmd):
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=20,
                             encoding="utf-8", errors="replace")
        return (out.stdout or "") + (out.stderr or "")
    except Exception:                              # noqa: BLE001
        return ""


def pid_on_port(port):
    """谁在占这个端口。没有返回 0。"""
    txt = _run(["powershell", "-NoProfile", "-Command",
                "(Get-NetTCPConnection -LocalPort %d -State Listen "
                "-ErrorAction SilentlyContinue).OwningProcess" % port])
    for line in txt.splitlines():
        line = line.strip()
        if line.isdigit():
            return int(line)
    return 0


def kill_tree(pid):
    """杀掉进程**以及它的父进程链**。

    venv 的 `python.exe` 是个启动器，真正干活的是它另起的子进程。只杀子进程
    的话，启动器留在那里，下次判断「端口空了吗」会出现竞态。
    `taskkill /T` 连整棵树一起收。
    """
    _run(["taskkill", "/PID", str(pid), "/T", "/F"])


def wait_port_free(port, timeout=WAIT_STOP):
    end = time.time() + timeout
    while time.time() < end:
        if not pid_on_port(port):
            return True
        time.sleep(0.4)
    return False


def wait_port_up(port, timeout=WAIT_BIND):
    end = time.time() + timeout
    while time.time() < end:
        if pid_on_port(port):
            return True
        time.sleep(0.4)
    return False


def status():
    for port, script in SERVICES.items():
        pid = pid_on_port(port)
        if pid:
            print("  %d  %-16s PID %-7d 在跑" % (port, script, pid))
        else:
            print("  %d  %-16s 没在跑" % (port, script))


def restart(port, script):
    print("== %d / %s ==" % (port, script))

    pid = pid_on_port(port)
    if pid:
        print("  停掉旧的（PID %d）…" % pid, end=" ")
        kill_tree(pid)
        if not wait_port_free(port):
            print("失败")
            print("  !! 端口 %d 还被占着，手动看一下：" % port)
            print("     Get-NetTCPConnection -LocalPort %d -State Listen" % port)
            return False
        print("端口已释放")
    else:
        print("  没有旧进程，直接起")

    # 用独立进程起，不挂在当前终端上（关掉这个窗口服务也还在）
    flags = 0
    if os.name == "nt":
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    try:
        subprocess.Popen([PY, script], cwd=ROOT,
                         stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL,
                         creationflags=flags)
    except Exception as exc:                       # noqa: BLE001
        print("  !! 起不来：%s" % exc)
        return False

    if wait_port_up(port):
        print("  起来了（PID %d）" % pid_on_port(port))
        return True
    print("  !! 没绑上 %d 端口，看日志：" % port)
    print("     logs/%s.log" % ("agent" if port == 5174 else "friend"))
    return False


def main(argv):
    if "--status" in argv or "-s" in argv:
        print("当前状态：")
        status()
        return 0

    want = [int(a) for a in argv if a.isdigit()]
    ports = want or sorted(SERVICES)
    bad = 0
    for p in ports:
        if p not in SERVICES:
            print("!! 不认识端口 %d（只认 %s）" % (p, "/".join(map(str, SERVICES))))
            bad += 1
            continue
        if not restart(p, SERVICES[p]):
            bad += 1
        print()

    print("=" * 46)
    status()
    if bad:
        print("\n有 %d 个没起来，看上面提示。" % bad)
        return 1
    print("\n全部就绪。")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
