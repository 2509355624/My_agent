# -*- coding: utf-8 -*-
"""5177 服务控制台：只监听 127.0.0.1，给浏览器一张九宫格看板。"""

from __future__ import annotations

import hmac
import logging
import os
import secrets
import threading
import time
from pathlib import Path

from flask import Flask, Response, jsonify, request

from app.features.control.manager import ServiceManager
from app.features.control.specs import ROOT

log = logging.getLogger("control.panel")

CONTROL_PORT = int(os.getenv("CONTROL_PORT", "5177"))
HTML_PATH = ROOT / "web" / "control_panel.html"


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
        return Response(html, content_type="text/html; charset=utf-8")

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


def main():
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
    log.info("control panel on http://127.0.0.1:%d", CONTROL_PORT)
    app.run(host="127.0.0.1", port=CONTROL_PORT, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
