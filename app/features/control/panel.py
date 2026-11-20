# -*- coding: utf-8 -*-
"""5177 服务控制台：只监听 127.0.0.1，给浏览器一张九宫格看板。"""

from __future__ import annotations

import hmac
import logging
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

from flask import Flask, Response, jsonify, request

from app.features.control.manager import ServiceManager
from app.features.control.specs import ROOT

log = logging.getLogger("control.panel")

CONTROL_PORT = int(os.getenv("CONTROL_PORT", "5177"))
HTML_PATH = ROOT / "web" / "control_panel.html"

_PANEL_RESTART_LOCK = threading.Lock()
_PANEL_RESTARTING = False


def _flag(name: str) -> bool:
    return os.getenv(name, "0").lower() in ("1", "true", "yes")


def _wait_for_port_free(port: int, timeout: float = 15.0) -> bool:
    """新控制台先等旧 5177 退出，避免抢不到端口又被 autostart 线程抢先拉服务。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.25):
                time.sleep(0.25)
        except OSError:
            return True
    return False


def _spawn_replacement():
    """拉起一个只负责 5177 的新控制台，不碰其它服务。

    关键环境变量：
    - CONTROL_AUTOSTART=0：不再自动启动隧道/网页服务，只接管已有进程；
    - CONTROL_NO_BROWSER=1：不额外开标签页，页面恢复后由前端自行 reload；
    - CONTROL_WAIT_FREE=1：新进程等旧 5177 释放端口后再 bind。
    """
    env = os.environ.copy()
    env["CONTROL_AUTOSTART"] = "0"
    env["CONTROL_NO_BROWSER"] = "1"
    env["CONTROL_WAIT_FREE"] = "1"
    out_path = ROOT / "logs" / "control-panel-restart.out"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out = open(out_path, "ab", buffering=0)
    try:
        subprocess.Popen(
            [sys.executable, "-m", "app.features.control.panel"],
            cwd=str(ROOT),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=out,
            close_fds=True,
            creationflags=getattr(subprocess, "DETACHED_PROCESS", 0),
        )
    finally:
        out.close()


def _exit_process():
    # 让先发出的 HTTP 响应有时间落回浏览器，再退出当前 5177。
    os._exit(0)


def _begin_panel_restart() -> bool:
    global _PANEL_RESTARTING
    with _PANEL_RESTART_LOCK:
        if _PANEL_RESTARTING:
            return False
        _PANEL_RESTARTING = True
    try:
        _spawn_replacement()
    except Exception:
        with _PANEL_RESTART_LOCK:
            _PANEL_RESTARTING = False
        raise
    timer = threading.Timer(1.2, _exit_process)
    timer.daemon = True
    timer.start()
    return True


def _authorized(app: Flask) -> bool:
    got = request.headers.get("X-Control-Token", "")
    want = app.config["CONTROL_TOKEN"]
    return bool(got) and hmac.compare_digest(str(got), str(want))


def _need_auth(app: Flask):
    if _authorized(app):
        return None
    return jsonify({"ok": False, "error": "forbidden"}), 401


def _register_page_routes(app: Flask) -> None:
    @app.get("/")
    def index():
        try:
            html = app.config["CONTROL_HTML"].read_text(encoding="utf-8")
        except OSError as e:
            return Response("control_panel.html missing: %s" % e, status=500,
                            mimetype="text/plain")
        html = html.replace("__CONTROL_TOKEN__", app.config["CONTROL_TOKEN"])
        return Response(html, content_type="text/html; charset=utf-8",
                        headers={"Cache-Control": "no-store"})

    @app.get("/health")
    def health():
        return jsonify({"ok": True, "service": "control-panel"})


def _register_api_routes(app: Flask, manager: ServiceManager) -> None:
    @app.get("/api/status")
    def api_status():
        denied = _need_auth(app)
        if denied:
            return denied
        try:
            return jsonify(manager.status())
        except Exception as e:                            # noqa: BLE001
            log.warning("status failed: %s", e)
            return jsonify({"ok": False, "error": str(e)}), 500

    @app.post("/api/services/<sid>/<action>")
    def api_service(sid: str, action: str):
        denied = _need_auth(app)
        if denied:
            return denied
        if action not in ("start", "stop", "restart"):
            return jsonify({"ok": False, "error": "unknown action"}), 404
        try:
            result = getattr(manager, action)(sid)
        except KeyError:
            return jsonify({"ok": False, "error": "unknown service"}), 404
        except Exception as e:                            # noqa: BLE001
            log.warning("%s %s failed: %s", action, sid, e)
            return jsonify({"ok": False, "error": str(e)}), 500
        return jsonify(result)

    @app.post("/api/all/restart")
    def api_restart_all():
        denied = _need_auth(app)
        if denied:
            return denied
        try:
            return jsonify(manager.restart_all())
        except Exception as e:                            # noqa: BLE001
            log.warning("restart all failed: %s", e)
            return jsonify({"ok": False, "error": str(e)}), 500

    @app.post("/api/panel/restart")
    def api_restart_panel():
        """只重启 5177 控制台自身，不重启它托管的隧道/网页/QQ 服务。"""
        denied = _need_auth(app)
        if denied:
            return denied
        try:
            started = _begin_panel_restart()
        except Exception as e:                            # noqa: BLE001
            log.warning("restart panel failed: %s", e)
            return jsonify({"ok": False, "error": str(e)}), 500
        if not started:
            return jsonify({"ok": True, "status": "restarting", "busy": True})
        return jsonify({"ok": True, "status": "restarting"})


def create_app(manager: ServiceManager, token: str | None = None,
               html_path: Path | None = None) -> Flask:
    app = Flask(__name__, static_folder=None)
    app.config["CONTROL_TOKEN"] = token or secrets.token_urlsafe(24)
    app.config["CONTROL_MANAGER"] = manager
    app.config["CONTROL_HTML"] = Path(html_path or HTML_PATH)
    _register_page_routes(app)
    _register_api_routes(app, manager)
    return app


def _autostart(manager: ServiceManager):
    """只拉起标了 autostart 的服务；看门狗/ComfyUI 保持手动。"""
    for spec in sorted(manager.specs.values(), key=lambda s: s.order):
        if not spec.autostart:
            continue
        try:
            manager.start(spec.id)
        except Exception as e:                            # noqa: BLE001
            log.warning("autostart %s failed: %s", spec.id, e)
        time.sleep(1.2)


def _open_browser():
    if os.getenv("CONTROL_NO_BROWSER", "0").lower() in ("1", "true", "yes"):
        return
    try:
        webbrowser.open("http://127.0.0.1:%d" % CONTROL_PORT)
    except Exception:                                    # noqa: BLE001
        log.warning("open browser failed", exc_info=True)


def main():
    # 由旧控制台拉起的替换进程会等到 5177 释放后再 bind；否则新进程
    # 直接启动会撞端口，旧进程退出后控制台就真的没了。
    if _flag("CONTROL_WAIT_FREE"):
        _wait_for_port_free(CONTROL_PORT)
    log_dir = ROOT / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(str(log_dir / "control-panel.log"),
                                encoding="utf-8"),
        ],
    )
    manager = ServiceManager()
    app = create_app(manager)
    if os.getenv("CONTROL_AUTOSTART", "1").lower() not in ("0", "false", "no", ""):
        threading.Thread(target=_autostart, args=(manager,), daemon=True).start()
    browser_timer = threading.Timer(1.5, _open_browser)
    browser_timer.daemon = True
    browser_timer.start()
    log.info("control panel on http://127.0.0.1:%d", CONTROL_PORT)
    app.run(host="127.0.0.1", port=CONTROL_PORT, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
