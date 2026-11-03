"""清理僵尸进程：不再服务的那些，连同它们的窗口一起关掉。

## 什么是「僵尸」

一个服务起过好几轮之后，旧的进程没退干净——它不干活，但还占着内存、还挂着
一个 cmd 窗口。典型来源：ComfyUI 假死被 watchdog 重启、一键启动重复双击、
重启 QQ 机器人只动了适配层。

## 判活只认端口（这条是硬约束）

一个端口同一时刻只能被一个进程绑住，所以「谁绑着端口」是**唯一的可靠判据**：

    8188  → ComfyUI
    5174  → agent.py
    5175  → friend_app.py

绑着的那一组（主进程 + launcher 父进程）保留，同名的其余进程是确定僵尸。

## 不绑端口的服务：只报告，不自动清

qq_bot / watchdog / friend_tunnel 都不监听端口，**没有可靠办法区分「僵尸」
和「正常」**。试过两个替代判据，都不可靠：

  · 「保留最新一组」——如果僵尸是刚起就失败的那种，它比真进程还新，反过来
    把正在服务的判成僵尸。实测踩过：假进程比真的新，脚本要清掉真 qq_bot。
  · 「有没有活跃 TCP 连接」——friend_app 明明在监听，查询那一瞬间没有请求，
    连接数就是 0。

误杀正在服务的进程，比留一个僵尸危害大得多。所以这类只列出来、给启动时间，
由人判断。脚本靠 `--apply` 只清绑端口那一类。

## 用法

    python clean_zombies.py            只看，列出会清掉谁
    python clean_zombies.py --apply    真清（只清「绑端口」那一类）
"""
import os
import subprocess
import sys

# 端口 → 命令行特征。绑着这个端口的进程算活的。
PORT_SERVICES = {
    8188: "ComfyUI-nightly",
    5174: "agent.py",
    5175: "friend_app.py",
}

# 不绑端口的服务：命令行特征 → 中文名。只留最新一组。
#
# 这些服务不监听端口，没法用「端口在不在」判活。规则改成「同一特征只保留
# 启动时间最新的一组（launcher + 主进程）」，多的当僵尸清掉。
#
# `app.qq_bot` 是 QQ 适配层——用户重启它时最容易留旧的（他有个
# `重启QQ机器人.bat` 只动这一个进程）。它有 `.qq_bot.pid` 记着当前两个 PID，
# 但那是 bat 自己的账本，别依赖它：进程意外退出时账本会过期。
NO_PORT_SERVICES = {
    "-m app.watchdog": "watchdog（看门狗）",
    "-m app.qq_bot": "qq_bot（QQ 适配层）",
    "friend_tunnel.py": "friend_tunnel（群友隧道）",
    "napcat": "NapCat（协议端）",
    "snowluma": "SnowLuma（协议端）",
}


def _ps(cmd):
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", cmd],
                           capture_output=True, text=True, timeout=30,
                           encoding="utf-8", errors="replace")
        return (r.stdout or "") + (r.stderr or "")
    except Exception:                              # noqa: BLE001
        return ""


def list_python():
    """所有 python 进程：[{pid, ppid, mb, start, cmd}]"""
    raw = _ps(
        "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
        "ForEach-Object { '{0}|{1}|{2}|{3}' -f $_.ProcessId,$_.ParentProcessId,"
        "$_.CommandLine,$_.CreationDate }")
    out = []
    for line in raw.splitlines():
        line = line.strip()
        if not line.count("|"):
            continue
        parts = line.split("|", 3)
        if len(parts) != 4:
            continue
        pid, ppid, cmd, cre = parts
        if not pid.strip().isdigit():
            continue
        out.append({
            "pid": int(pid.strip()),
            "ppid": int(ppid.strip()) if ppid.strip().isdigit() else 0,
            "cmd": cmd.strip(),
            "cre": cre.strip(),
        })
    return out


def mem_of(pid):
    txt = _ps("(Get-Process -Id %d -ErrorAction SilentlyContinue)."
              "WorkingSet64" % pid).strip()
    try:
        return int(float(txt) / 1024 / 1024)
    except Exception:                              # noqa: BLE001
        return 0


def port_pid(port):
    txt = _ps("(Get-NetTCPConnection -LocalPort %d -State Listen "
              "-ErrorAction SilentlyContinue).OwningProcess" % port)
    for line in txt.splitlines():
        line = line.strip()
        if line.isdigit():
            return int(line)
    return 0


def kill(pid):
    try:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True, timeout=20)
        return True
    except Exception:                              # noqa: BLE001
        return False


def scan():
    """返回 (要保留的 pid 集合, 确定僵尸列表, 只报告不动的列表)。"""
    procs = list_python()
    by_pid = {p["pid"]: p for p in procs}
    keep, dead, report = set(), [], []

    # ── 1. 绑端口的：端口那个 + 它的父进程链 = 活的 ──
    live_ports = {}
    for port, mark in PORT_SERVICES.items():
        live = port_pid(port)
        if not live:
            continue
        live_ports[port] = live
        keep.add(live)
        # 往上找 launcher（venv 的 python.exe 会另起一个真解释器）
        cur = by_pid.get(live, {}).get("ppid", 0)
        depth = 0
        while cur and cur in by_pid and depth < 3:
            keep.add(cur)
            cur = by_pid[cur].get("ppid", 0)
            depth += 1

    # ── 2. 不绑端口的：只统计，不判死活 ──
    for mark, label in NO_PORT_SERVICES.items():
        group = [p for p in procs if mark.lower() in p["cmd"].lower()]
        if not group:
            continue
        # 一组正常 = launcher + 主进程（2 个）。多于 2 个才值得看一眼。
        if len(group) > 2:
            report.append((label, group))

    # ── 3. 命中了绑端口服务、但不在保留集合里 = 确定僵尸 ──
    port_marks = list(PORT_SERVICES.values())
    for p in procs:
        low = p["cmd"].lower()
        if not any(m.lower() in low for m in port_marks):
            continue
        if p["pid"] in keep:
            continue
        dead.append(p)

    return keep, dead, report


def main(argv):
    apply_ = "--apply" in argv
    keep, dead, report = scan()

    print("=== 正在服务的（保留）===")
    for port, mark in PORT_SERVICES.items():
        pid = port_pid(port)
        if pid:
            print("  %-5d  %-16s PID %-7d %d MB"
                  % (port, mark.split("\\")[0], pid, mem_of(pid)))
        else:
            print("  %-5d  %-16s 没在跑" % (port, mark.split("\\")[0]))

    if report:
        print("\n=== 不绑端口的服务：进程数偏多，请自己看一眼 ===")
        print("（这类没法可靠判死活，脚本不会自动清）")
        for label, group in report:
            print("  %s：%d 个进程" % (label, len(group)))
            for p in sorted(group, key=lambda x: x["cre"]):
                t = p["cre"][8:14] if len(p["cre"]) > 14 else "?"
                print("     PID %-7d %5d MB  启动 %s"
                      % (p["pid"], mem_of(p["pid"]), t))
        print("  处理建议：用 restart_servers.py 重启对应服务，它会先杀干净再起。")

    if not dead:
        print("\n没有确定僵尸（绑端口的服务都正常）。")
        return 0

    print("\n=== 确定僵尸（同名但没绑端口）===")
    total = 0
    for p in dead:
        mb = mem_of(p["pid"])
        total += mb
        name = "?"
        low = p["cmd"].lower()
        for port, mark in PORT_SERVICES.items():
            if mark.lower() in low:
                name = mark.split("\\")[0]
                break
        print("  PID %-7d %5d MB  %s" % (p["pid"], mb, name))
    print("  合计 %d MB" % total)

    if not apply_:
        print("\n（只是看看。要真清就加 --apply）")
        return 0

    print("\n=== 清理 ===")
    ok = 0
    for p in dead:
        if kill(p["pid"]):
            print("  已停 PID %d" % p["pid"])
            ok += 1
        else:
            print("  !! 停不掉 PID %d（可能已经退出）" % p["pid"])
    print("\n清掉 %d 个，释放约 %d MB。" % (ok, total))
    print("它们的 cmd 窗口会自动关掉。")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
